"""The privileged-action journal (`audit_events`, T05).

One row per administrator break-glass agent mutation on a `prod` workspace and per
workspace tier change. Deliberately tiny: an append, nothing else — reading it back
is a ledger query, and any richer outcome lives on the Job / Deployment rows the
action itself produces.
"""

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.ledger import AuditEvent


def record_audit_event(
    *,
    workspace_id: str,
    actor: str,
    action: str,
    target: str = "",
    db: Session | None = None,
) -> None:
    """Append one journal row.

    With `db` the row joins the caller's transaction (commit is the caller's);
    without it the row is committed on its own short-lived session, so the journal
    entry survives even when the action it precedes fails.
    """
    row = AuditEvent(
        workspace_id=workspace_id, actor=actor[:64], action=action[:160], target=target[:256]
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
