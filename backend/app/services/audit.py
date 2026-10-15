"""The privileged-action journal (`audit_events`, T05).

One row per administrator break-glass agent mutation on a `prod` workspace, per
workspace tier change, and per console-account change (create / update / delete /
password change — hub-global, journaled under the hub's own `default` workspace,
because `audit_events` rows must name one; a password is never part of a row).
Deliberately small: an append, plus the one project-scoped read the project's audit
view needs (`project_events`, design §11.6); any richer outcome lives on the Job /
Deployment rows the action itself produces.

Each row carries the project it happened in (design §5.10): the current project
(`project_context` / the request's), else NULL — a workspace-level action. The ORM's
usual fallback (the workspace's sole project) does not apply here: a tier change or
an account edit is not any project's event. Structure events — changes to projects,
groups, departments and administrators themselves (`record_structure_event`) — are
platform-level and always NULL.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, insert, or_, select
from sqlalchemy.orm import Session

from app.core.actor import user_id_for
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import current_project, project_context
from app.models.ledger import AuditEvent

logger = logging.getLogger(__name__)

_CURRENT = object()  # `project_id` omitted: the current project

# `audit_events.outcome` (design §11.6)
OUTCOME_SUCCESS, OUTCOME_REFUSED, OUTCOME_FAILED = "success", "refused", "failed"
OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_REFUSED, OUTCOME_FAILED)

# The structure events of design §5.10 (P1F writes them). A `*` family in the design
# is a prefix here; anything else is a typo that would split the journal's vocabulary.
STRUCTURE_ACTIONS = frozenset({
    "project.create", "project.update", "project.archive", "project.unarchive",
    "project.delete",
    "project.members.add", "project.members.update", "project.members.remove",
    "project.roles.upsert", "project.roles.delete",
    "project.workspaces.bind", "project.workspaces.unbind",
    "group.create", "group.update", "group.archive", "group.unarchive",
    "admin.grant", "admin.revoke",
    "user.sessions.revoke",
    "project.transfer",
    "project.quotas.update",
})
# `system.update.*` — the platform's online update (online-update §5); `steward.*` —
# a structure-only administrator's membership changes and two-person requests (§11.4);
# `project.share.*` — cross-project sharing grants (§11.2: grant / request / approve /
# reject / revoke / expire, filed under the owner project with the grantee in the target)
STRUCTURE_ACTION_PREFIXES = (
    "group.members.", "group.projects.", "department.", "system.update.", "steward.",
    "project.share.",
)


def is_structure_action(action: str) -> bool:
    return action in STRUCTURE_ACTIONS or (
        action.startswith(STRUCTURE_ACTION_PREFIXES)
        and not action.endswith(".")
    )


# What a steward (§11.4) reads in `GET /api/audit`: the structure journal above, plus
# the other platform-shape events it may cause itself — accounts, a workspace's tier,
# the network / storage catalogues. Never a data event (an agent, a release, a storage
# write confirmation naming an agent).
STEWARD_VISIBLE_ACTIONS = STRUCTURE_ACTIONS | {
    "user.create", "user.update", "user.delete", "workspace.tier_change",
    "storage.register", "storage.check", "storage.project_set", "storage.update",
    "storage.archive",
}
STEWARD_VISIBLE_PREFIXES = (*STRUCTURE_ACTION_PREFIXES, "network_profile.")


def steward_visible(action: str) -> bool:
    return action in STEWARD_VISIBLE_ACTIONS or (
        action.startswith(STEWARD_VISIBLE_PREFIXES) and not action.endswith(".")
    )


def steward_clause():
    """`steward_visible` as a WHERE clause on `audit_events.action`."""
    # the prefixes carry no LIKE wildcard (`[a-z_.]` only)
    return or_(AuditEvent.action.in_(sorted(STEWARD_VISIBLE_ACTIONS)),
               *(AuditEvent.action.like(f"{p}_%") for p in STEWARD_VISIBLE_PREFIXES))


def record_audit_event(
    *,
    workspace_id: str,
    actor: str,
    action: str,
    target: str = "",
    db: Session | None = None,
    actor_user_id: str | None = None,
    project_id: Any = _CURRENT,
    outcome: str = OUTCOME_SUCCESS,
) -> None:
    """Append one journal row.

    `actor_user_id` is the actor's `users.id` (design §5.10); omitted, it is the
    request caller's when `actor` is the caller (`app.core.actor`), else NULL — the
    built-in admin and system actors have none. `project_id` omitted is the current
    project (None when there is none); pass None for a workspace-level event.

    With `db` the row joins the caller's transaction (commit is the caller's);
    without it the row is committed on its own short-lived session, so the journal
    entry survives even when the action it precedes fails.

    `outcome` (design §11.6): `success` — the action was let through (most rows are
    written at admission); `refused` — the platform turned the request away
    (`record_refusal`); `failed` — it ran and failed.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"not an audit outcome: {outcome!r}")
    values = {
        "workspace_id": workspace_id,
        "project_id": current_project() if project_id is _CURRENT else project_id,
        "actor": actor[:64],
        "actor_user_id": user_id_for(actor, actor_user_id),
        "action": action[:160],
        "target": target[:256],
        "outcome": outcome,
    }
    # a Core insert, not `db.add`: the flush-time stamping would file a NULL-project
    # row into the workspace's sole project, and NULL is the point here
    statement = insert(AuditEvent).values(**values)
    if db is not None:
        db.execute(statement)
        return
    session = SessionLocal()
    try:
        session.execute(statement)
        session.commit()
    finally:
        session.close()


def record_structure_event(
    *,
    actor: str,
    action: str,
    target: str = "",
    db: Session | None = None,
    actor_user_id: str | None = None,
) -> None:
    """Journal a change to the platform's structure (design §5.10, §7.3): a project,
    its members / roles / workspace bindings, a group, a department, an administrator
    grant, a forced logout. Hub-global like the account journal — filed under the
    hub's `default` workspace (`audit_events` rows must name one) with no project, so
    `GET /api/audit?project=workspace` there lists them. `target` names what changed
    (`project:<id>`, `user:<username>`…), never a password or a token."""
    if not is_structure_action(action):
        raise ValueError(f"not a structure audit action: {action!r}")
    record_audit_event(
        workspace_id=DEFAULT_WORKSPACE_ID, actor=actor, action=action, target=target,
        db=db, actor_user_id=actor_user_id, project_id=None,
    )


def record_refusal(
    *,
    actor: str,
    action: str,
    target: str = "",
    project_id: str | None,
    workspace_id: str | None = None,
    actor_user_id: str | None = None,
) -> None:
    """Journal a request the platform turned away (`outcome = refused`, design §11.6):
    a project permission it lacked, a project it is not a member of, a prod workspace's
    protection. On its own short-lived session, so the row survives the error response
    that follows; best-effort — a journal failure never changes the refusal the caller
    gets. A hub-global refusal (a path-project route) files under the hub's `default`
    workspace like the structure journal."""
    try:
        record_audit_event(
            workspace_id=workspace_id or DEFAULT_WORKSPACE_ID, actor=actor, action=action,
            target=target, actor_user_id=actor_user_id, project_id=project_id,
            outcome=OUTCOME_REFUSED,
        )
    except Exception:  # noqa: BLE001 — the refusal stands whatever the journal does
        logger.warning("could not journal a refusal of %s", action, exc_info=True)


# ── the project's audit view (design §11.6) ───────────────────────────────────

# a project's own rows, plus the structure rows naming it: `project.*` / `share.*` /
# `steward.*`
# actions whose target holds `project:<id>` as a whole token (`project:a` must not
# match `project:a-b`) — at the end, before a space, a comma or a transfer's `->`
STRUCTURE_FAMILIES = ("project.", "share.", "steward.")
_TOKEN_ENDINGS = ("", " %", ",%", "->%")


@dataclass(frozen=True)
class AuditFilter:
    action: str | None = None  # a prefix: `release.` is every release action
    actor: str | None = None  # a username or a `users.id`
    since: datetime | None = None
    until: datetime | None = None
    outcome: str | None = None


def _utc(moment: datetime) -> datetime:
    # stored UTC; SQLite hands it back naive, and a caller's `from` may carry an offset
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


def _project_clause(project_id: str):
    # project ids are slugs (`[a-z0-9-]`), so they carry no LIKE wildcard
    token = f"project:{project_id}"
    named = or_(*(AuditEvent.target.like(f"%{token}{end}") for end in _TOKEN_ENDINGS))
    family = or_(*(AuditEvent.action.like(f"{f}%") for f in STRUCTURE_FAMILIES))
    return or_(AuditEvent.project_id == project_id, and_(family, named))


def _filtered(project_id: str, flt: AuditFilter):
    query = select(AuditEvent).where(_project_clause(project_id))
    if flt.action:
        query = query.where(AuditEvent.action.like(f"{flt.action}%"))
    if flt.actor:
        query = query.where(or_(AuditEvent.actor == flt.actor,
                                AuditEvent.actor_user_id == flt.actor))
    if flt.since:
        query = query.where(AuditEvent.created_at >= _utc(flt.since))
    if flt.until:
        query = query.where(AuditEvent.created_at < _utc(flt.until))
    if flt.outcome:
        if flt.outcome not in OUTCOMES:
            raise AppError("audit.invalid_outcome", f"unknown outcome {flt.outcome!r}",
                           {"outcomes": list(OUTCOMES)})
        query = query.where(AuditEvent.outcome == flt.outcome)
    return query


def encode_cursor(row: AuditEvent) -> str:
    return f"{_utc(row.created_at).isoformat()}|{row.id}"


def _after(query, cursor: str | None):
    """Keyset paging, newest first: the rows strictly older than the cursor's row
    (an append-only journal shifts offsets; a key does not)."""
    if not cursor:
        return query
    at, _, row_id = cursor.partition("|")
    try:
        moment = _utc(datetime.fromisoformat(at))
    except ValueError:
        raise AppError("audit.invalid_cursor", "the paging cursor is not valid") from None
    return query.where(or_(AuditEvent.created_at < moment,
                           and_(AuditEvent.created_at == moment, AuditEvent.id < row_id)))


def project_events(
    db: Session, project_id: str, flt: AuditFilter, *, limit: int, cursor: str | None = None,
) -> tuple[list[AuditEvent], str | None]:
    """One page of the project's journal, newest first, and the next page's cursor
    (None on the last page). Every workspace the project is bound to: a project spans
    workspaces, and so does its history."""
    query = _after(_filtered(project_id, flt), cursor).order_by(
        AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
    with project_context(None):  # the clause names the project; no ORM scope applies
        rows = list(db.scalars(query))
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (encode_cursor(rows[-1]) if more and rows else None)


def iter_project_events(
    db: Session, project_id: str, flt: AuditFilter, *, cap: int, chunk: int = 500,
) -> Iterator[AuditEvent]:
    """Every row `project_events` would page through, up to `cap` — the export reads the
    same query the view does, so the two never disagree."""
    cursor: str | None = None
    left = cap
    while left > 0:
        rows, cursor = project_events(db, project_id, flt, limit=min(chunk, left),
                                      cursor=cursor)
        yield from rows
        left -= len(rows)
        if cursor is None:
            return


def project_facets(db: Session, project_id: str, *, limit: int = 200) -> dict[str, list[str]]:
    """The actors and actions present in the project's journal: the view's filter lists."""
    clause = _project_clause(project_id)
    with project_context(None):
        actors = db.scalars(select(AuditEvent.actor).where(clause).distinct()
                            .order_by(AuditEvent.actor).limit(limit)).all()
        actions = db.scalars(select(AuditEvent.action).where(clause).distinct()
                             .order_by(AuditEvent.action).limit(limit)).all()
    return {"actors": list(actors), "actions": list(actions)}


EVENT_FIELDS = ("id", "at", "actor", "actor_user_id", "action", "target", "outcome",
                "project_id", "workspace_id")


def event_dict(row: AuditEvent) -> dict[str, Any]:
    return {
        "id": row.id, "at": _utc(row.created_at).isoformat() if row.created_at else None,
        "actor": row.actor, "actor_user_id": row.actor_user_id, "action": row.action,
        "target": row.target, "outcome": row.outcome or OUTCOME_SUCCESS,
        "project_id": row.project_id, "workspace_id": row.workspace_id,
    }
