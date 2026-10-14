"""Disconnect-safe console chat turns (chat-streaming §5.2).

A console chat turn runs on a background worker thread, not in the HTTP response:
the worker reads the upstream, persists the thread items (``persist_events``) and
publishes every event into the turn's in-memory buffer, numbered by ``seq``. The
``POST /api/chat/{agent_id}`` response is just the first subscriber of that buffer;
when the browser goes away the turn keeps running, and
``GET /api/chat/{agent_id}/turns/{turn_id}/events?after=<seq>`` subscribes again —
first the buffered events after ``seq``, then the live ones, closing once the turn has
ended.

* **One running turn per session.** A second turn on a session whose turn is still
  running is refused (409 ``chat.turn_in_progress``, ``detail.turn_id``), so a resumed
  page cannot interleave two turns.
* **Bounded.** A turn runs at most its agent's execution timeout + ``LIMIT_MARGIN_SECONDS``;
  past that the upstream is closed and the turn recorded as timed out. The stop
  button stops it (``stop_turn``). The buffer keeps ``MAX_EVENTS`` events or
  ``MAX_AGE_SECONDS`` of them; a subscriber asking for an older ``seq`` is told by a
  ``gap`` event first. A finished turn stays resumable for ``FINISHED_TTL_SECONDS``;
  after that the ledger (``GET …/history``) is all there is.
* **Single process (D-1).** The registry is this process's memory: a deployment with
  several workers must route a resume to the process running the turn, and a restart
  loses the running turns (their partial answers stay in the ledger up to the last
  tool call or answer flushed). Multi-process resume from the ledger is not built.
* The worker runs inside ``project_context(<the turn's project>)``.

Not verified live (chat-streaming §5.3): whether AgentCore keeps executing a turn
after ``InvokeHarness`` / ``InvokeAgentRuntime``'s stream is closed (a stop or time
limit closes it; the ledger's ``interrupted: …`` row assumes the turn ends there),
and how a runtime session behaves when a new turn arrives while the previous one is
still executing server-side (this module only prevents it from the console).
"""

import contextvars
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import AppError, envelope
from app.core.project_scope import project_context
from app.services.chat import StopSignal, TurnStopped

log = logging.getLogger(__name__)

# §5.2: the buffer's bounds while a turn runs
MAX_EVENTS = 2000
MAX_AGE_SECONDS = 600.0
# how long a finished turn can still be resumed (a reconnect racing the last events)
FINISHED_TTL_SECONDS = 60.0
# a turn's time limit is the agent's execution timeout plus this
LIMIT_MARGIN_SECONDS = 60
# the limit of an agent whose spec names no timeout: the longest one a spec may set
FALLBACK_TIMEOUT_SECONDS = 3600
# how long the stop button waits for the worker to record the stop, so the session's
# next turn is accepted as soon as the stop answers
STOP_WAIT_SECONDS = 5.0


def turn_limit_seconds(timeout_s: Any) -> int:
    """A turn's time limit (§5.2): the agent's execution timeout + 60 s."""
    base = timeout_s if isinstance(timeout_s, int) and timeout_s > 0 else FALLBACK_TIMEOUT_SECONDS
    return base + LIMIT_MARGIN_SECONDS


@dataclass(eq=False)
class Turn:
    """One running (or recently finished) console chat turn and its event buffer."""

    id: str
    workspace_id: str
    agent_id: str
    project_id: str | None
    session_id: str | None
    limit_s: int
    started: float = field(default_factory=time.monotonic)
    user_message_id: int | None = None
    finished_at: float | None = None
    stop: StopSignal = field(default_factory=StopSignal)
    thread: threading.Thread | None = None
    _cond: threading.Condition = field(default_factory=threading.Condition)
    # (position, seq — None for a heartbeat —, event, monotonic time published)
    _events: deque[tuple[int, int | None, dict[str, Any], float]] = field(default_factory=deque)
    _next_pos: int = 0
    _next_seq: int = 1

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def publish(self, event: dict[str, Any]) -> None:
        """Buffer one event for the subscribers, numbered by ``seq``. A heartbeat gets
        no ``seq`` (it is transport, not content) and two in a row are kept as one."""
        with self._cond:
            now = time.monotonic()
            if event.get("event") == "heartbeat":
                if self._events and self._events[-1][1] is None:
                    self._events.pop()
                self._events.append((self._next_pos, None, event, now))
            else:
                self._events.append((self._next_pos, self._next_seq, event, now))
                self._next_seq += 1
            self._next_pos += 1
            while len(self._events) > MAX_EVENTS or (
                self._events and now - self._events[0][3] > MAX_AGE_SECONDS
            ):
                self._events.popleft()
            self._cond.notify_all()

    def finish(self) -> None:
        with self._cond:
            self.finished_at = time.monotonic()
            self._cond.notify_all()

    def _heartbeat(self) -> dict[str, Any]:
        return {"event": "heartbeat",
                "data": {"elapsed_ms": int((time.monotonic() - self.started) * 1000)}}

    def _start(self, after: int) -> tuple[int, int | None]:
        """Where a subscriber after ``after`` starts in the buffer, and the first
        ``seq`` it still holds when events after ``after`` already left it."""
        first = next((seq for _, seq, _, _ in self._events if seq is not None), self._next_seq)
        gap = first if after + 1 < first else None
        seen = first - 1  # the seq before each entry
        for pos, seq, _, _ in self._events:
            if (seq is not None and seq > after) or (seq is None and seen >= after):
                return pos, gap
            if seq is not None:
                seen = seq
        return self._next_pos, gap

    def subscribe(
        self, after: int = 0, interval: float | None = None,
    ) -> Iterator[tuple[int | None, dict[str, Any]]]:
        """``(seq, event)`` for every event after ``after``, then the live ones, until
        the turn has ended. Heartbeats (``seq`` None) where the worker sent them, and
        whenever ``interval`` passes with nothing to send; a ``gap {after, first_seq}``
        (``seq`` None) first when events after ``after`` already left the buffer."""
        from app.services import chat as chat_service

        interval = chat_service.KEEPALIVE_SECONDS if interval is None else interval
        after = max(after, 0)
        with self._cond:
            cursor, gap = self._start(after)
        if gap is not None:
            yield None, {"event": "gap", "data": {"after": after, "first_seq": gap}}
        while True:
            with self._cond:
                if self._next_pos <= cursor and self.finished_at is None:
                    self._cond.wait(timeout=interval)
                pending = [(seq, event) for pos, seq, event, _ in self._events if pos >= cursor]
                if self._events and self._events[0][0] > cursor:
                    # this subscriber fell behind the buffer's bound
                    first = next((q for _, q, _, _ in self._events if q is not None), None)
                    pending.insert(0, (None, {"event": "gap", "data": {"first_seq": first}}))
                cursor = self._next_pos
                ended = self.finished_at is not None
            for seq, event in pending:
                # a buffered heartbeat is re-timed: it says how long the turn has run now
                yield seq, (self._heartbeat() if event.get("event") == "heartbeat" else event)
            if ended:
                return
            if not pending:
                yield None, self._heartbeat()


class TurnRegistry:
    """This process's console chat turns: by id, and the running one per session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[str, Turn] = {}
        self._running: dict[tuple[str, str, str], Turn] = {}

    def _sweep(self) -> None:
        now = time.monotonic()
        for turn_id, turn in list(self._turns.items()):
            if turn.finished_at is not None and now - turn.finished_at > FINISHED_TTL_SECONDS:
                del self._turns[turn_id]

    def get(self, turn_id: str) -> Turn | None:
        with self._lock:
            self._sweep()
            return self._turns.get(turn_id)

    def running_for(self, workspace_id: str, agent_id: str, session_id: str) -> Turn | None:
        with self._lock:
            return self._running.get((workspace_id, agent_id, session_id))

    def claim(
        self, *, workspace_id: str, agent_id: str, project_id: str | None,
        session_id: str | None, limit_s: int,
    ) -> Turn:
        """Register a new turn, or 409 ``chat.turn_in_progress`` when its session
        already runs one."""
        with self._lock:
            self._sweep()
            if session_id:
                busy = self._running.get((workspace_id, agent_id, session_id))
                if busy is not None:
                    raise AppError(
                        "chat.turn_in_progress",
                        "this conversation is still answering the previous message — "
                        "wait for it to finish, or stop it, before sending another",
                        {"turn_id": busy.id, "session_id": session_id},
                        status_code=409,
                    )
            turn = Turn(id=uuid.uuid4().hex, workspace_id=workspace_id, agent_id=agent_id,
                        project_id=project_id, session_id=session_id, limit_s=limit_s)
            self._turns[turn.id] = turn
            if session_id:
                self._running[(workspace_id, agent_id, session_id)] = turn
            return turn

    def bind_session(self, turn: Turn, session_id: str) -> None:
        """A turn that started a new session holds it from its ``meta`` on."""
        with self._lock:
            if turn.session_id == session_id:
                return
            if turn.session_id:
                self._running.pop((turn.workspace_id, turn.agent_id, turn.session_id), None)
            turn.session_id = session_id
            self._running.setdefault((turn.workspace_id, turn.agent_id, session_id), turn)

    def release(self, turn: Turn) -> None:
        with self._lock:
            if turn.session_id:
                key = (turn.workspace_id, turn.agent_id, turn.session_id)
                if self._running.get(key) is turn:
                    del self._running[key]
        turn.finish()

    def clear(self) -> None:
        """Forget every turn (tests)."""
        with self._lock:
            self._turns.clear()
            self._running.clear()


registry = TurnRegistry()

# The events of one turn: ``persist_events`` over the console chain, given the turn's
# stop signal to hand to ``chat_stream``.
TurnSource = Callable[[StopSignal], Iterator[dict[str, Any]]]


def _stopped_event(exc: TurnStopped, limit_s: int) -> dict[str, Any]:
    """The ``error`` a stopped turn ends with: a time limit is a ``timeout`` (no
    retry button: a replay runs into the same limit), the stop button a plain stop."""
    if exc.kind == "timeout":
        data = envelope("chat.turn_timeout", exc.interruption, {"limit_s": limit_s})
        data["retry"] = {"category": "timeout", "manual": False, "auto": False,
                         "timeout_s": limit_s - LIMIT_MARGIN_SECONDS}
    else:
        data = envelope("chat.turn_stopped", exc.interruption)
        data["retry"] = {"category": "other", "manual": True, "auto": False}
    return {"event": "error", "data": data}


def _work(turn: Turn, source: TurnSource) -> None:
    timer = threading.Timer(
        turn.limit_s, turn.stop.set,
        args=(f"the turn ran past its {turn.limit_s} s limit", "timeout"),
    )
    timer.daemon = True
    timer.start()
    try:
        with project_context(turn.project_id):
            for event in source(turn.stop):
                if event["event"] == "meta":
                    data = event["data"]
                    if data.get("session_id"):
                        registry.bind_session(turn, str(data["session_id"]))
                    turn.user_message_id = data.get("user_message_id")
                    event = {"event": "meta", "data": {**data, "turn_id": turn.id}}
                turn.publish(event)
    except TurnStopped as exc:
        turn.publish(_stopped_event(exc, turn.limit_s))
    except Exception as exc:  # the chain reports its own errors; this is a last resort
        log.exception("chat: background turn %s failed", turn.id)
        turn.publish({"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}})
    finally:
        timer.cancel()
        registry.release(turn)


def start_turn(
    source: TurnSource, *, workspace_id: str, agent_id: str, project_id: str | None,
    session_id: str | None, timeout_s: Any = None,
) -> Turn:
    """Claim the session (409 when it already runs a turn) and run ``source`` on a
    background worker, in a copy of the caller's context (identity, workspace) and
    inside ``project_context(project_id)``."""
    turn = registry.claim(workspace_id=workspace_id, agent_id=agent_id, project_id=project_id,
                          session_id=session_id, limit_s=turn_limit_seconds(timeout_s))
    ctx = contextvars.copy_context()
    turn.thread = threading.Thread(target=ctx.run, args=(_work, turn, source),
                                   name=f"chat-turn-{turn.id[:8]}", daemon=True)
    turn.thread.start()
    return turn


def stop_turn(turn: Turn, wait: float = STOP_WAIT_SECONDS) -> bool:
    """Stop a running turn (the stop button) and wait up to ``wait`` seconds for its
    worker to record it. False when the turn had already ended."""
    if not turn.running:
        return False
    turn.stop.set("stopped by the user")
    if turn.thread is not None and turn.thread is not threading.current_thread():
        turn.thread.join(timeout=wait)
    return True
