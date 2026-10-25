"""Chat transcript persistence shared by every entrance that keeps a thread.

The console chat (`routers/chat.py`) and the external share page
(`routers/share.py`) both wrap the one `chat_stream` and persist the same
`ChatSession` / `ChatMessage` rows through `persist_events`, so a shared
conversation is replayable, observable and rateable exactly like a console one.
"""

import json
import logging
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from app.core.db import SessionLocal
from app.models.ledger import ChatMessage, ChatSession

log = logging.getLogger(__name__)

INTERRUPTED_PREFIX = "interrupted: "
# strands-builder SB-1: an ``output_schema`` agent's parsed answer, saved as its own
# row (JSON text) right after the agent row it belongs to; ``fold_structured`` puts
# it back on that message for the history
ROLE_STRUCTURED = "structured"
# strands-builder SB-2: a tool on the approval list paused for the user (tool name +
# the runtime-masked input as JSON), so a restored thread shows the approval card
ROLE_APPROVAL = "approval"
STRUCTURED_MAX_CHARS = 100000


def fold_structured(rows: list[ChatMessage]) -> list[tuple[ChatMessage, Any]]:
    """``(row, structured)`` per thread item: a ``structured`` row is attached to the
    agent row before it (and not listed itself); a stray one is dropped."""
    out: list[tuple[ChatMessage, Any]] = []
    for row in rows:
        if row.role == ROLE_STRUCTURED:
            if out and out[-1][0].role == "agent" and out[-1][1] is None:
                try:
                    out[-1] = (out[-1][0], json.loads(row.text))
                except ValueError:
                    log.warning("chat: unreadable structured answer row %s", row.id)
            continue
        out.append((row, None))
    return out


def visible_rows(rows: list[ChatMessage]) -> list[ChatMessage]:
    """A session's thread without the turns a retry replaced (chat-streaming §4.1).

    A turn runs from its user row to the next one; a later user row whose
    ``retry_of`` names it means the failed turn was retried, so a restored session
    shows the retry only — the user's message once, not twice."""
    retried = {r.retry_of for r in rows if r.role == "user" and r.retry_of is not None}
    shown: list[ChatMessage] = []
    hidden = False
    for row in rows:
        if row.role == "user":
            hidden = row.id in retried
        if not hidden:
            shown.append(row)
    return shown


def save_message(
    workspace_id: str,
    agent_id: str,
    session_id: str,
    role: str,
    text: str,
    name: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    answered_by: str | None = None,
    retry_of: int | None = None,
) -> int:
    """Append one thread item; returns its id (the replay order and feedback key)."""
    db = SessionLocal()
    try:
        row = ChatMessage(
            workspace_id=workspace_id, agent_id=agent_id, session_id=session_id,
            role=role, text=text[:100000], name=name, attachments=attachments,
            answered_by=answered_by, retry_of=retry_of,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def add_usage(workspace_id: str, agent_id: str, session_id: str, usage: dict[str, Any]) -> None:
    """Add one turn's runtime-reported token usage to its session (projects §16.3-3)."""
    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and value > 0 else 0

    db = SessionLocal()
    try:
        db.query(ChatSession).filter(
            ChatSession.workspace_id == workspace_id,
            ChatSession.agent_id == agent_id,
            ChatSession.session_id == session_id,
        ).update({
            ChatSession.input_tokens: ChatSession.input_tokens + count("input_tokens"),
            ChatSession.output_tokens: ChatSession.output_tokens + count("output_tokens"),
            ChatSession.total_tokens: ChatSession.total_tokens + count("total_tokens"),
        }, synchronize_session=False)
        db.commit()
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
    retry_of: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Pass a `chat_stream` through, persisting thread items in event order
    (int-pk = replay order) so a session's history replays exactly as rendered.

    Every persisted agent answer is announced with a `saved` event
    (`{"message_id": n}`) placed just before the event that closed the bubble, so
    a client can attach thumbs feedback to the message it just watched stream.

    chat-streaming §4.1: `meta` gains `user_message_id` (the turn's user row, what a
    retry names) and a persisted `error` its row's `message_id`. ``retry_of`` (the
    failed turn's user row) is recorded on this turn's user row.
    """
    answer_parts: list[str] = []
    answered_by: str | None = None  # T35: `rule:<id>` when a curated answer replied
    # as_user consent asks raised while an answer bubble is open; saved after it
    pending_asks: list[tuple[str, str | None]] = []
    # the turn's structured answer (SB-1), saved with the answer it rides along
    structured: list[Any] = []

    def flush_answer() -> dict[str, Any] | None:
        if not session_id:
            return None
        message_id = None
        if answer_parts:
            message_id = save_message(workspace_id, agent_id, session_id, "agent",
                                      "".join(answer_parts), answered_by=answered_by)
            answer_parts.clear()
            if structured:
                _save_structured(workspace_id, agent_id, session_id, structured.pop())
        structured.clear()
        for provider, tool in pending_asks:
            save_message(workspace_id, agent_id, session_id, "auth", provider, name=tool)
        pending_asks.clear()
        if message_id is None:
            return None
        data: dict[str, Any] = {"message_id": message_id}
        if answered_by:
            data["answered_by"] = answered_by
        return {"event": "saved", "data": data}

    user_id: int | None = None  # this turn's user row, once `meta` saved it
    source = iter(events)
    closed = False  # `done` or `error` passed: the turn has its ending recorded
    try:
        for event in source:
            kind, data = event["event"], event["data"]
            if kind == "meta":
                session_id = data["session_id"]
                track_session(workspace_id, agent_id, session_id, actor_id,
                              runtime_version=runtime_version)
                user_id = save_message(workspace_id, agent_id, session_id, "user", prompt,
                                       attachments=attachments, retry_of=retry_of)
                event = {"event": "meta", "data": {**data, "user_message_id": user_id}}
            elif kind == "rule":
                answered_by = f"rule:{data.get('rule_id', '')}"[:48]
            elif kind == "usage" and session_id:
                add_usage(workspace_id, agent_id, session_id, data)
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
            elif kind == "approval_required" and session_id:
                # the agent stops after asking; the card follows the bubble so far
                saved = flush_answer()
                if saved:
                    yield saved
                save_message(workspace_id, agent_id, session_id, ROLE_APPROVAL,
                             json.dumps(data.get("input") or {}, default=str),
                             name=str(data.get("tool") or "")[:80] or None)
            elif kind == "structured":
                structured[:] = [data.get("value")]
            elif kind == "delta":
                answer_parts.append(data.get("text", ""))
            elif kind == "error" and session_id:
                closed = True
                saved = flush_answer()  # keep the partial answer the user saw
                if saved:
                    yield saved
                error_id = save_message(workspace_id, agent_id, session_id, "error",
                                        data.get("message", ""))
                event = {"event": "error", "data": {**data, "message_id": error_id}}
            elif kind == "done" and session_id:
                closed = True
                saved = flush_answer()
                if saved:
                    yield saved
            yield event
    except BaseException as exc:
        # §5.1: the client went away (GeneratorExit at a yield) or the stream broke —
        # keep the partial answer the user already saw, and say why it stops there,
        # so a reload replays it and the turn can be retried from its user message
        if not closed:
            # a stopped background turn (chat-streaming §5.2) names its own reason
            interruption = getattr(exc, "interruption", None)
            reason = (
                interruption if isinstance(interruption, str) and interruption
                else "client disconnected" if isinstance(exc, GeneratorExit)
                else f"{type(exc).__name__}: {exc}"
            )
            _record_interruption(flush_answer, workspace_id, agent_id, session_id, reason,
                                 user_id=user_id)
        raise
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()  # closing the chat stream closes the upstream invocation
    # a stream that ended without `done` still keeps its answer and asks
    saved = flush_answer()
    if saved:
        yield saved


def _save_structured(workspace_id: str, agent_id: str, session_id: str, value: Any) -> None:
    text = json.dumps(value, default=str)
    if len(text) > STRUCTURED_MAX_CHARS:
        return  # too large to replay; the text answer still is
    save_message(workspace_id, agent_id, session_id, ROLE_STRUCTURED, text)


def _retried(workspace_id: str, session_id: str, user_id: int | None) -> bool:
    """A retry already replaced this turn (chat-streaming §4.1)."""
    if user_id is None:
        return False
    db = SessionLocal()
    try:
        return db.query(ChatMessage.id).filter(
            ChatMessage.workspace_id == workspace_id,
            ChatMessage.session_id == session_id,
            ChatMessage.retry_of == user_id,
        ).first() is not None
    finally:
        db.close()


def _record_interruption(
    flush_answer: Callable[[], dict[str, Any] | None],
    workspace_id: str, agent_id: str, session_id: str | None, reason: str,
    *, user_id: int | None = None,
) -> None:
    """Save the open answer and an ``error`` item ``interrupted: <reason>``.

    Not when a retry already replaced the turn: the browser gave up on a silent
    stream and retried before this one noticed the disconnect, so its rows would
    land inside the retry's turn. Runs while the stream is being torn down, so it
    never raises: a failed write is logged and the original exit (disconnect or
    stream error) goes on."""
    if not session_id:
        return  # nothing reached the ledger yet (no `meta`)
    try:
        if _retried(workspace_id, session_id, user_id):
            return
        flush_answer()
        save_message(workspace_id, agent_id, session_id, "error",
                     f"{INTERRUPTED_PREFIX}{reason}"[:2000])
    except Exception:
        log.exception("chat: could not save the interrupted turn of %s", session_id)
