"""The privileged-action journal (`audit_events`, T05).

One row per administrator break-glass agent mutation on a `prod` workspace, per
workspace tier change, and per console-account change (create / update / delete /
password change — hub-global, journaled under the hub's own `default` workspace,
because `audit_events` rows must name one; a password is never part of a row).
Deliberately tiny: an append, nothing else — reading it back is a ledger query, and
any richer outcome lives on the Job / Deployment rows the action itself produces.
"""

from sqlalchemy.orm import Session

from app.core.actor import user_id_for
from app.core.db import SessionLocal
from app.models.ledger import AuditEvent


def record_audit_event(
    *,
    workspace_id: str,
    actor: str,
    action: str,
    target: str = "",
    db: Session | None = None,
    actor_user_id: str | None = None,
) -> None:
    """Append one journal row.

    `actor_user_id` is the actor's `users.id` (design §5.10); omitted, it is the
    request caller's when `actor` is the caller (`app.core.actor`), else NULL — the
    built-in admin and system actors have none.

    With `db` the row joins the caller's transaction (commit is the caller's);
    without it the row is committed on its own short-lived session, so the journal
    entry survives even when the action it precedes fails.
    """
    row = AuditEvent(
        workspace_id=workspace_id,
        actor=actor[:64],
        actor_user_id=user_id_for(actor, actor_user_id),
        action=action[:160],
        target=target[:256],
    )
    if db is not None:
        db.add(row)
        return
    session = SessionLocal()
    try:
        session.add(row)
        session.commit()
    finally:
        session.close()
