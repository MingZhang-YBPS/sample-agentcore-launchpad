"""Chat transcript persistence shared by every entrance that keeps a thread.

The console chat (`routers/chat.py`) and the external share page
(`routers/share.py`) both wrap the one `chat_stream` and persist the same
`ChatSession` / `ChatMessage` rows through `persist_events`, so a shared
conversation is replayable, observable and rateable exactly like a console one.
"""

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from app.core.db import SessionLocal
from app.models.ledger import ChatMessage, ChatSession


def save_message(
    workspace_id: str,
    agent_id: str,
    session_id: str,
    role: str,
    text: str,
    name: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    answered_by: str | None = None,
) -> int:
    """Append one thread item; returns its id (the replay order and feedback key)."""
    db = SessionLocal()
    try:
        row = ChatMessage(
            workspace_id=workspace_id, agent_id=agent_id, session_id=session_id,
            role=role, text=text[:100000], name=name, attachments=attachments,
            answered_by=answered_by,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def track_session(
    workspace_id: str, agent_id: str, session_id: str, actor_id: str,
    runtime_version: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        row = (
            db.query(ChatSession)
            .filter(
                ChatSession.workspace_id == workspace_id,
                ChatSession.agent_id == agent_id,
                ChatSession.session_id == session_id,
            )
            .first()
        )
        if row is None:
            row = ChatSession(workspace_id=workspace_id, agent_id=agent_id,
                              session_id=session_id, actor_id=actor_id,
                              runtime_version=runtime_version)
            db.add(row)
        elif row.ended_at:
            row.runtime_version = runtime_version
        row.turns = (row.turns or 0) + 1
        row.last_at = datetime.now(UTC)
        # A new turn under an ended id starts a fresh AgentCore session with the
        # same id, so the row is live again — "ended" must not outlast that.
        row.ended_at = None
        db.commit()
    finally:
        db.close()


def persist_events(
    events: Iterable[dict[str, Any]],
    *,
    workspace_id: str,
    agent_id: str,
    prompt: str,
    actor_id: str,
    session_id: str | None,
    runtime_version: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Pass a `chat_stream` through, persisting thread items in event order
    (int-pk = replay order) so a session's history replays exactly as rendered.

    Every persisted agent answer is announced with a `saved` event
    (`{"message_id": n}`) placed just before the event that closed the bubble, so
    a client can attach thumbs feedback to the message it just watched stream.
    """
    answer_parts: list[str] = []
    answered_by: str | None = None  # T35: `rule:<id>` when a curated answer replied
    # as_user consent asks raised while an answer bubble is open; saved after it
    pending_asks: list[tuple[str, str | None]] = []

    def flush_answer() -> dict[str, Any] | None:
        if not session_id:
            return None
        message_id = None
        if answer_parts:
            message_id = save_message(workspace_id, agent_id, session_id, "agent",
                                      "".join(answer_parts), answered_by=answered_by)
            answer_parts.clear()
        for provider, tool in pending_asks:
            save_message(workspace_id, agent_id, session_id, "auth", provider, name=tool)
        pending_asks.clear()
        if message_id is None:
            return None
        data: dict[str, Any] = {"message_id": message_id}
        if answered_by:
            data["answered_by"] = answered_by
        return {"event": "saved", "data": data}

    for event in events:
        kind, data = event["event"], event["data"]
        if kind == "meta":
            session_id = data["session_id"]
            track_session(workspace_id, agent_id, session_id, actor_id,
                          runtime_version=runtime_version)
            save_message(workspace_id, agent_id, session_id, "user", prompt,
                         attachments=attachments)
        elif kind == "rule":
            answered_by = f"rule:{data.get('rule_id', '')}"[:48]
        elif kind == "tool" and session_id:
            # a tool call splits the answer bubble live — mirror it
            saved = flush_answer()
            if saved:
                yield saved
            save_message(workspace_id, agent_id, session_id, "tool", "",
                         name=data.get("name"))
        elif kind == "auth_required" and session_id:
            # as_user consent ask: the Connection name and tool only — the
            # authorization URL is single-use and is never persisted, so a
            # restored card re-asks by retrying the previous user message.
            # The card does not close an open answer bubble live, so it is
            # saved after that bubble's text, not between its halves.
            ask = (str(data.get("provider") or ""), data.get("tool"))
            if answer_parts:
                pending_asks.append(ask)
            else:
                save_message(workspace_id, agent_id, session_id, "auth", ask[0], name=ask[1])
        elif kind == "policy_denied" and session_id:
            # a Gateway tool call a Cedar policy denied: the tool event already
            # closed the open bubble, so the card follows its tool row; the
            # reason carries the policy id, which a restored card is rebuilt from
            save_message(workspace_id, agent_id, session_id, "policy",
                         data.get("reason", ""), name=data.get("tool"))
        elif kind == "delta":
            answer_parts.append(data.get("text", ""))
        elif kind == "error" and session_id:
            saved = flush_answer()  # keep the partial answer the user saw
            if saved:
                yield saved
            save_message(workspace_id, agent_id, session_id, "error",
                         data.get("message", ""))
        elif kind == "done" and session_id:
            saved = flush_answer()
            if saved:
                yield saved
        yield event
    # a stream that ended without `done` still keeps its answer and asks
    saved = flush_answer()
    if saved:
        yield saved
