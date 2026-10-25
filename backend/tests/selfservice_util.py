"""Shared seeding for the T33-T36 tests: ledger sessions with real message rows."""

import itertools

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import ChatMessage, ChatSession

_counter = itertools.count(1)


def seed_turn(agent_id: str, question: str, answer: str, *, session_id: str | None = None,
              role: str = "agent", answered_by: str | None = None,
              workspace_id: str = DEFAULT_WORKSPACE_ID) -> tuple[str, int]:
    """One user turn + one reply in a fresh session; returns (session_id, reply message id)."""
    sid = session_id or f"sess-{next(_counter):04d}-" + "x" * 30
    db = SessionLocal()
    try:
        db.add(ChatSession(workspace_id=workspace_id, agent_id=agent_id, session_id=sid,
                           actor_id="river", turns=1))
        db.add(ChatMessage(workspace_id=workspace_id, agent_id=agent_id, session_id=sid,
                           role="user", text=question))
        reply = ChatMessage(workspace_id=workspace_id, agent_id=agent_id, session_id=sid,
                            role=role, text=answer, answered_by=answered_by)
        db.add(reply)
        db.commit()
        return sid, reply.id
    finally:
        db.close()
