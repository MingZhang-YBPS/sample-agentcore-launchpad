"""The coding event stream: SSE over the runtime's seq-numbered event log
(parent design §7.4).

A turn never depends on this connection: the browser reads ``GET …/events?after=
<seq>`` again and again, and every request replays everything after its cursor,
then tails live. Frames:

* ``event: status`` first — ``{repo_status, active_turn}`` from the ledger
  (again whenever the stream wakes up for a new turn);
* each event-log record as ``id: <seq>`` + ``event: event`` + ``data: <record>``;
* ``: keep-alive`` comments while nothing happens;
* ``event: reconnect`` after ``MAX_STREAM_SECONDS`` — the client reconnects
  after its last seq;
* ``event: error`` ``{code, message}`` on a failure, then the stream ends (never an
  exception mid-stream).

**Idle mode.** Every runtime read keeps the repo's microVM alive (billed
memory), so an open IDE tab on an idle repo must not long-poll it. Once the
replay is drained and the runtime reports no active turn, the stream polls the
LEDGER (``coding_repos.status``) every ``IDLE_POLL_SECONDS`` and switches back to
runtime long-polls only when a new turn is claimed there. A stream that resumes an
idle repo at or past its last turn's end record (``coding_repos.end_seq`` —
the client's periodic ``reconnect``) starts in idle mode: no replay, no wake-up.

A turn-end record (``launchpad/turnUsage`` / ``launchpad/turnAbandoned``) the
stream forwards also settles that turn (``turns.settle`` — idempotent, races the
watcher safely). Records are read ``compact``: a delta whose item already
completed is dropped (the ``item/completed`` record carries the whole item).

**Worker threads.** Starlette runs every ``next()`` of a *sync* body in the shared
worker pool (40 tokens, the same pool every sync route uses), so the route serves
``body`` — an async iterator that runs each blocking step (a ledger read, a
runtime long-poll) in a worker thread but awaits the idle-mode pauses on the event
loop: an open idle IDE tab holds no worker thread. ``events`` is the same stream
as a plain iterator (pauses sleep in place).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import anyio

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

from . import runtime_client, turns
from .models import REPO_RUNNING, CodingRepo
from .runtime_client import CodingRuntimeError

logger = logging.getLogger("launchpad.coding")

MAX_STREAM_SECONDS = 1500  # 25 min, then `reconnect`
POLL_WAIT_MS = 20000  # one runtime long-poll (the runtime caps at 25 s)
PAGE_LIMIT = 500
IDLE_POLL_SECONDS = 2.0
KEEPALIVE_SECONDS = 15.0
_READ_TIMEOUT_S = 60.0  # > POLL_WAIT_MS, < the data client's 1000 s default

_now = time.monotonic  # time seams for tests
_sleep = time.sleep
_async_sleep = anyio.sleep


def frame(event: str, data: Any, *, seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


KEEPALIVE = ": keep-alive\n\n"


def _status(row: CodingRepo) -> dict[str, Any]:
    running = row.status == REPO_RUNNING
    return {
        "repo_status": "running" if running else "idle",
        "active_turn": row.active_turn_id if running else None,
    }


def _load(repo_id: str) -> CodingRepo | None:
    db = SessionLocal()
    try:
        return db.get(CodingRepo, repo_id)
    finally:
        db.close()


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, CodingRuntimeError):
        mapped = runtime_client.api_error(exc)
        return {"code": mapped.code, "message": mapped.message}
    if isinstance(exc, AppError):
        return {"code": exc.code, "message": exc.message}
    return {"code": runtime_client.RUNTIME_ERROR, "message": str(exc)[:300]}


@dataclass(frozen=True)
class _Pause:
    """A step of the stream that waits without reading anything."""

    seconds: float


def _steps(ctx: WorkspaceContext, repo_id: str, after: int) -> Iterator[str | _Pause]:
    """The stream as SSE chunks and pauses. Never raises: an unexpected failure
    (a ledger read, a bug) is the last ``error`` frame."""
    try:
        yield from _run(ctx, repo_id, after)
    except Exception:  # noqa: BLE001 — a stream ends with an error frame
        logger.exception("coding repo %s: event stream failed", repo_id)
        yield frame("error", {"code": runtime_client.RUNTIME_ERROR,
                              "message": "the event stream failed — reconnecting"})


def events(ctx: WorkspaceContext, repo_id: str, after: int) -> Iterator[str]:
    """The SSE body for one repo as a plain iterator (pauses sleep in place)."""
    for step in _steps(ctx, repo_id, after):
        if isinstance(step, _Pause):
            _sleep(step.seconds)
        else:
            yield step


async def body(ctx: WorkspaceContext, repo_id: str, after: int) -> AsyncIterator[str]:
    """The route's SSE body (the route already checked ownership): each blocking
    step runs in a worker thread, pauses await on the event loop."""
    steps = _steps(ctx, repo_id, after)
    try:
        while True:
            step = await anyio.to_thread.run_sync(next, steps, None)
            if step is None:
                return
            if isinstance(step, _Pause):
                await _async_sleep(step.seconds)
            else:
                yield step
    finally:
        steps.close()


def _run(ctx: WorkspaceContext, repo_id: str, after: int) -> Iterator[str | _Pause]:
    """The stream's logic: replay, live tail, idle mode (module docstring)."""
    started = _now()
    row = _load(repo_id)
    if row is None:
        yield frame("error", {"code": "coding.repo_not_found",
                              "message": "coding repo not found"})
        return
    yield frame("status", _status(row))
    cursor = max(0, int(after or 0))
    completions: dict[str, Any] = {}
    finished: set[str] = set()  # turns this stream saw end (or the runtime no longer runs)
    # The replay drains from the runtime first — unless an idle repo's client
    # already holds everything up to its last turn's end (a `reconnect` of an open
    # idle tab): then nothing is replayed and the microVM is left asleep.
    runtime_mode = not (row.status != REPO_RUNNING and cursor > 0
                        and cursor >= int(row.end_seq or 0))
    runtime_active: str | None = None
    last_beat = _now()

    while True:
        if _now() - started >= MAX_STREAM_SECONDS:
            yield frame("reconnect", {})
            return
        row = _load(repo_id)
        if row is None:
            yield frame("error", {"code": "coding.repo_not_found",
                                  "message": "coding repo not found"})
            return

        if not runtime_mode:
            if (row.status == REPO_RUNNING and row.active_turn_id
                    and row.active_turn_id not in finished):
                runtime_mode = True
                yield frame("status", _status(row))
                continue
            if _now() - last_beat >= KEEPALIVE_SECONDS:
                last_beat = _now()
                yield KEEPALIVE
            yield _Pause(IDLE_POLL_SECONDS)
            continue

        # an idle repo's replay drains without waiting; a live one long-polls
        idle = row.status != REPO_RUNNING and runtime_active is None
        try:
            page = runtime_client.repo_call(
                ctx, row, "events", after=cursor, wait_ms=0 if idle else POLL_WAIT_MS,
                limit=PAGE_LIMIT, compact=True, read_timeout=_READ_TIMEOUT_S,
            ) or {}
        except Exception as exc:  # noqa: BLE001 — a stream ends with an error frame
            logger.info("coding repo %s: events read failed: %s", repo_id, exc)
            yield frame("error", _error(exc))
            return
        records = [r for r in page.get("events") or [] if isinstance(r, dict)]
        for record in records:
            seq = record.get("seq")
            yield frame("event", record, seq=seq if isinstance(seq, int) else None)
        if records:
            last_beat = _now()
            ended = turns.settle_from_records(repo_id, records, completions, ctx=ctx)
            finished.update(t for t in ended if t)
        next_after = page.get("next_after")
        if isinstance(next_after, int) and next_after > cursor:
            cursor = next_after
        seq_now = page.get("seq")
        drained = not records or (isinstance(seq_now, int) and cursor >= seq_now)
        active = page.get("active_turn")
        runtime_active = str(active.get("turn_id") or "") or None \
            if isinstance(active, dict) else None

        if drained and runtime_active is None:
            fresh = _load(repo_id) or row
            if fresh.status == REPO_RUNNING:
                before = row.active_turn_id if row.status == REPO_RUNNING else None
                if not fresh.active_turn_id or fresh.active_turn_id != before:
                    # a start is being claimed, or was recorded while this page was
                    # read (a long-poll woken by `turn/started` can answer before the
                    # runtime marks the turn active): keep reading the runtime
                    continue
                # it was running before this read and the runtime no longer runs it:
                # its end is behind the cursor, or it is gone — the watcher settles it
                finished.add(fresh.active_turn_id)
            runtime_mode = False
            continue
        if not records:
            last_beat = _now()
            yield KEEPALIVE
