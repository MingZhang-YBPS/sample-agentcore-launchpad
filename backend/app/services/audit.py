"""The privileged-action journal (`audit_events`, T05).

One row per administrator break-glass agent mutation on a `prod` workspace, per
workspace tier change, and per console-account change (create / update / delete /
password change — hub-global, journaled under the hub's own `default` workspace,
because `audit_events` rows must name one; a password is never part of a row).
Deliberately tiny: an append, nothing else — reading it back is a ledger query, and
any richer outcome lives on the Job / Deployment rows the action itself produces.

Each row carries the project it happened in (design §5.10): the current project
(`project_context` / the request's), else NULL — a workspace-level action. The ORM's
usual fallback (the workspace's sole project) does not apply here: a tier change or
an account edit is not any project's event. Structure events — changes to projects,
groups, departments and administrators themselves (`record_structure_event`) — are
platform-level and always NULL.
"""

from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.core.actor import user_id_for
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project
from app.models.ledger import AuditEvent

_CURRENT = object()  # `project_id` omitted: the current project

# The structure events of design §5.10 (P1F writes them). A `*` family in the design
# is a prefix here; anything else is a typo that would split the journal's vocabulary.
STRUCTURE_ACTIONS = frozenset({
    "project.create", "project.update", "project.archive", "project.unarchive",
    "project.delete",
    "project.members.add", "project.members.update", "project.members.remove",
    "project.roles.upsert", "project.roles.delete",
    "project.workspaces.bind", "project.workspaces.unbind",
    "group.create", "group.update", "group.archive",
    "admin.grant", "admin.revoke",
    "user.sessions.revoke",
    "project.transfer",
})
STRUCTURE_ACTION_PREFIXES = ("group.members.", "group.projects.", "department.")


def is_structure_action(action: str) -> bool:
    return action in STRUCTURE_ACTIONS or (
        action.startswith(STRUCTURE_ACTION_PREFIXES)
        and not action.endswith(".")
    )


def record_audit_event(
    *,
    workspace_id: str,
    actor: str,
    action: str,
    target: str = "",
    db: Session | None = None,
    actor_user_id: str | None = None,
    project_id: Any = _CURRENT,
) -> None:
    """Append one journal row.

    `actor_user_id` is the actor's `users.id` (design §5.10); omitted, it is the
    request caller's when `actor` is the caller (`app.core.actor`), else NULL — the
    built-in admin and system actors have none. `project_id` omitted is the current
    project (None when there is none); pass None for a workspace-level event.

    With `db` the row joins the caller's transaction (commit is the caller's);
    without it the row is committed on its own short-lived session, so the journal
    entry survives even when the action it precedes fails.
    """
    values = {
        "workspace_id": workspace_id,
        "project_id": current_project() if project_id is _CURRENT else project_id,
        "actor": actor[:64],
        "actor_user_id": user_id_for(actor, actor_user_id),
        "action": action[:160],
        "target": target[:256],
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
