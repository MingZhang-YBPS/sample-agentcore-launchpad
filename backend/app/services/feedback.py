"""Thumbs feedback on agent answers (T15).

A thumbs-down is a *bad case* the evaluation flow already knows how to consume:
`list_feedback` exposes the down-voted **session ids**, which the V2 "add to
dataset" modal posts to `POST /api/eval/datasets/from-sessions` unchanged. No
dataset-building logic lives here.
"""

import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.ledger import Agent, ChatFeedback, ChatMessage

logger = logging.getLogger("launchpad.feedback")

VERDICTS = ("up", "down")
MAX_COMMENT = 1000
MAX_CORRECTION = 4000
# what one from-sessions call accepts (pipelines.MAX_SESSIONS_PER_CALL)
MAX_SESSION_IDS = 50


def _answer_in(
    db: Session, workspace_id: str, agent_id: str, session_id: str, message_id: int
) -> ChatMessage:
    """The agent answer being rated, or 404 — a message of another workspace,
    agent or session reads exactly like a missing one."""
    row = db.get(ChatMessage, message_id)
    if (
        row is None
        or row.workspace_id != workspace_id
        or row.agent_id != agent_id
        or row.session_id != session_id
        or row.role != "agent"
    ):
        raise NotFoundError("feedback.message_not_found", "message not found")
    return row


def record_feedback(
    db: Session,
    *,
    workspace_id: str,
    agent_id: str,
    session_id: str,
    message_id: int,
    verdict: str,
    comment: str | None,
    actor: str,
    source: str,
    correction: str | None = None,
) -> dict[str, Any]:
    """Upsert one actor's verdict on one answer. `verdict="none"` withdraws it.

    `correction` is the answer a reviewer says it should have been (T34). A thumbs-down
    opens an issue-box item (T36) -- best effort, never a reason to lose the verdict."""
    _answer_in(db, workspace_id, agent_id, session_id, message_id)
    row = (
        db.query(ChatFeedback)
        .filter(ChatFeedback.message_id == message_id, ChatFeedback.actor == actor)
        .first()
    )
    if verdict == "none":
        if row is not None:
            db.delete(row)
            db.commit()
        return {"message_id": message_id, "verdict": None}
    comment = (comment or "").strip()[:MAX_COMMENT] or None
    correction = (correction or "").strip()[:MAX_CORRECTION] or None
    if row is None:
        row = ChatFeedback(
            workspace_id=workspace_id, agent_id=agent_id, session_id=session_id,
            message_id=message_id, verdict=verdict, comment=comment, actor=actor,
            source=source, correction=correction,
        )
        db.add(row)
    else:
        row.verdict = verdict
        row.comment = comment
        row.correction = correction
    db.commit()
    if verdict == "down":
        _open_issue(db, row)
    return {"message_id": message_id, "verdict": verdict}


def _open_issue(db: Session, row: ChatFeedback) -> None:
    from app.services import issues  # local: issues reads this module's tables

    try:
        issues.open_from_feedback(db, row)
    except Exception:  # noqa: BLE001 - the verdict is already saved
        db.rollback()
        logger.warning("could not open an issue for feedback %s", row.id, exc_info=True)


def verdicts_for(
    db: Session, workspace_id: str, actor: str, message_ids: list[int]
) -> dict[int, str]:
    """This actor's current verdicts, to restore thumb state on a replayed thread."""
    if not message_ids:
        return {}
    rows = (
        db.query(ChatFeedback.message_id, ChatFeedback.verdict)
        .filter(
            ChatFeedback.workspace_id == workspace_id,
            ChatFeedback.actor == actor,
            ChatFeedback.message_id.in_(message_ids),
        )
        .all()
    )
    return {message_id: verdict for message_id, verdict in rows}


def _preview(text: str | None, limit: int = 240) -> str:
    return (text or "")[:limit]


def list_feedback(
    db: Session,
    workspace_id: str,
    *,
    verdict: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    base = db.query(ChatFeedback).filter(ChatFeedback.workspace_id == workspace_id)
    if agent_id:
        base = base.filter(ChatFeedback.agent_id == agent_id)

    counts = {v: 0 for v in VERDICTS}
    for found, n in (
        base.with_entities(ChatFeedback.verdict, func.count(ChatFeedback.id))
        .group_by(ChatFeedback.verdict)
        .all()
    ):
        counts[found] = int(n)

    query = base
    if verdict:
        query = query.filter(ChatFeedback.verdict == verdict)
    rows = query.order_by(ChatFeedback.updated_at.desc()).limit(limit).all()

    names = {
        a.id: a.name
        for a in db.query(Agent).filter(
            Agent.id.in_({r.agent_id for r in rows} or {""})
        )
    }
    items = []
    for r in rows:
        answer = db.get(ChatMessage, r.message_id)
        question = (
            db.query(ChatMessage.text)
            .filter(
                ChatMessage.workspace_id == workspace_id,
                ChatMessage.session_id == r.session_id,
                ChatMessage.role == "user",
                ChatMessage.id < r.message_id,
            )
            .order_by(ChatMessage.id.desc())
            .first()
        )
        items.append({
            "id": r.id,
            "agent_id": r.agent_id,
            "agent_name": names.get(r.agent_id, r.agent_id),
            "session_id": r.session_id,
            "message_id": r.message_id,
            "verdict": r.verdict,
            "comment": r.comment,
            "correction": r.correction,
            "actor": r.actor,
            "source": r.source,
            "question": _preview(question[0] if question else ""),
            "answer": _preview(answer.text if answer else ""),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        })

    latest = func.max(ChatFeedback.updated_at)
    down_sessions = [
        sid
        for sid, _at in base.filter(ChatFeedback.verdict == "down")
        .with_entities(ChatFeedback.session_id, latest)
        .group_by(ChatFeedback.session_id)
        .order_by(latest.desc())
        .limit(MAX_SESSION_IDS)
        .all()
    ]
    return {"counts": counts, "items": items, "down_session_ids": down_sessions}
