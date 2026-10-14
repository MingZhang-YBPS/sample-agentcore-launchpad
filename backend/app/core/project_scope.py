"""The project boundary inside the ORM (design §5.2): filter on read, stamp on write.

Three pieces, each failing closed:

* ``ProjectOwned`` / ``ProjectShared`` mixins give a model ``project_id``.
  *Owned* rows always belong to one project; *shared* rows may carry NULL, meaning
  workspace- or platform-level (a system preset agent, a governance change, a
  bootstrap job) — readable from every project.
* A ``do_orm_execute`` listener adds ``project_id = :current`` (shared: ``OR IS NULL``)
  to every ORM SELECT / UPDATE / DELETE while a project is current. The current
  project is a context variable set once per request by the route policy
  (``bind_request_project``); background threads never set it, so a deploy worker or
  the scheduler sees every row exactly as before.
* A ``before_flush`` listener stamps a new row's ``project_id``: from its parent row
  (``__project_parent__``: an eval run from its agent, a message from its
  conversation…), else the current project, else — only when the row's workspace
  is bound to exactly one project — that project. Otherwise the flush fails: an
  owned row with no project would be invisible everywhere, and silently filing it
  into some project would leak it.

``Session.get`` answers from the identity map without SQL, so the listener cannot
see it: request code loads owned rows by id through ``owned_get``.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar

from sqlalchemy import String, event, or_, text
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column, with_loader_criteria

logger = logging.getLogger(__name__)

_CURRENT_PROJECT: ContextVar[str | None] = ContextVar("launchpad_current_project", default=None)


class ProjectStampError(RuntimeError):
    """A new project-owned row whose project could not be determined."""


class _ProjectColumn:
    """The column both mixins share. Indexed per table (``ix_<table>_project_id``)."""

    project_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # (attribute naming the parent row, parent table or tables). A derived row takes
    # its parent's project; None = stamped from the current project.
    __project_parent__: ClassVar[tuple[str, tuple[str, ...]] | None] = None

    def project_platform_row(self) -> bool:
        """True for a row that is platform-level by nature and stays NULL (shared only)."""
        return False


class ProjectOwned(_ProjectColumn):
    """Rows that always belong to exactly one project."""


class ProjectShared(_ProjectColumn):
    """Rows that belong to a project, or to no project (NULL = workspace / platform level)."""


# ── the current project ───────────────────────────────────────────────────────


def current_project() -> str | None:
    return _CURRENT_PROJECT.get()


def set_current_project(project_id: str | None) -> None:
    """Set for the rest of the current context (one request's task)."""
    _CURRENT_PROJECT.set(project_id)


@contextmanager
def project_context(project_id: str | None) -> Iterator[None]:
    """Run a block as ``project_id`` (None = unscoped), restoring what was current.

    Background workers enter it with their job's project; a few cross-project
    reads (an admin's fleet view, name-uniqueness checks) enter it with None."""
    token = _CURRENT_PROJECT.set(project_id)
    try:
        yield
    finally:
        _CURRENT_PROJECT.reset(token)


# ── read: the automatic criteria ──────────────────────────────────────────────


@event.listens_for(Session, "do_orm_execute")
def _filter_to_current_project(state: ORMExecuteState) -> None:
    pid = _CURRENT_PROJECT.get()
    if pid is None:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.is_column_load or state.is_relationship_load:
        return  # lazy loads of an already-loaded (already-filtered) row
    state.statement = state.statement.options(
        with_loader_criteria(
            ProjectOwned, lambda cls: cls.project_id == pid, include_aliases=True,
        ),
        with_loader_criteria(
            ProjectShared,
            lambda cls: or_(cls.project_id == pid, cls.project_id.is_(None)),
            include_aliases=True,
        ),
    )


def visible_in_current_project(row: Any) -> bool:
    """Whether a loaded row may be shown while the current project is current."""
    pid = _CURRENT_PROJECT.get()
    if pid is None or not isinstance(row, _ProjectColumn):
        return True
    if row.project_id == pid:
        return True
    return isinstance(row, ProjectShared) and row.project_id is None


def owned_get[ModelT](db: Session, model: type[ModelT], ident: Any) -> ModelT | None:
    """``db.get`` that honours the current project (an identity-map hit runs no SQL,
    so the automatic criteria never see it). A row of another project reads as missing."""
    row = db.get(model, ident)
    if row is None or not visible_in_current_project(row):
        return None
    return row


# ── write: stamping ───────────────────────────────────────────────────────────


def _parent_project(session: Session, obj: _ProjectColumn) -> str | None:
    spec = type(obj).__project_parent__
    if spec is None:
        return None
    attr, tables = spec
    parent_id = getattr(obj, attr, None)
    if not parent_id:
        return None
    # a parent created in this same flush is not in the database yet
    for pending in session.new:
        if (
            isinstance(pending, _ProjectColumn)
            and getattr(pending, "__tablename__", None) in tables
            and getattr(pending, "id", None) == parent_id
        ):
            return pending.project_id
    conn = session.connection()
    for table in tables:
        found = conn.execute(
            text(f"SELECT project_id FROM {table} WHERE id = :id"),  # noqa: S608 — fixed names
            {"id": parent_id},
        ).first()
        if found is not None:
            return found[0]
    return None


def _sole_workspace_project(session: Session, workspace_id: str | None) -> str | None:
    """The project a workspace's row belongs to when there is no other candidate, only
    where that is unambiguous: the workspace's one bound project, or — for a workspace
    bound to none yet (registered by an older build, or only named by the row) — the
    platform's `default` project, which every workspace starts with."""
    if not workspace_id:
        # a row with no workspace already fails the workspace invariant at startup
        # (db.assert_every_row_has_a_workspace); file it like an unbound workspace's
        return _default_project(session)
    rows = session.connection().execute(
        text("SELECT project_id FROM project_workspaces WHERE workspace_id = :ws"),
        {"ws": workspace_id},
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        return _default_project(session)
    return None


def _default_project(session: Session) -> str | None:
    found = session.connection().execute(
        text("SELECT id FROM projects WHERE id = 'default'")
    ).first()
    return found[0] if found else None


@event.listens_for(Session, "before_flush")
def _stamp_new_rows(session: Session, _flush_context: Any, _instances: Any) -> None:
    pending = [
        obj for obj in session.new
        if isinstance(obj, _ProjectColumn) and not obj.project_id
    ]
    if not pending:
        return
    with session.no_autoflush:
        for obj in pending:
            if isinstance(obj, ProjectShared) and obj.project_platform_row():
                continue
            pid = (
                _parent_project(session, obj)
                or _CURRENT_PROJECT.get()
                or _sole_workspace_project(session, getattr(obj, "workspace_id", None))
            )
            if pid:
                obj.project_id = pid
            elif isinstance(obj, ProjectOwned):
                raise ProjectStampError(
                    f"new {type(obj).__name__} has no project: no parent row carries one, "
                    "no project is current, and its workspace is not bound to exactly one — "
                    "run the write inside project_context(<project id>)"
                )
