"""The coding watcher: settles turns nobody is streaming, snapshots saved files.

A turn runs in its microVM whether or not a browser watches it, so the backend
cannot rely on the SSE proxy to settle it. One daemon thread per process
(``coding_watcher_enabled``, on by default; ``LAUNCHPAD_CODING_WATCHER_ENABLED=
false`` turns it off — the hermetic suite does) ticks every
``coding_watcher_interval_s`` (15 s) and, per workspace:

* **running repos** — reads the runtime's event log from the repo's
  ``last_seq`` (``compact``, no wait) and settles every turn whose end record it
  finds (``turns.settle`` — idempotent, races the SSE path safely), then advances
  ``last_seq``. A turn the runtime no longer runs and never reported ending (its
  microVM's session storage came back from an older snapshot) is settled
  ``abandoned``; so is one past the microVM's 8-hour lifetime.
* **stale start claims** — a repo claimed for a start whose runtime answer
  never arrived (the process died in between) is released after
  ``START_CLAIM_TTL``; a DELETE claim likewise after ``DELETE_CLAIM_TTL``.
* **dirty repos** — idle repos with a manual save older than
  ``SAVE_DEBOUNCE`` are snapshotted (``snapshots.take_quietly``, backed off after a
  failure).

Restart-safe: everything is re-read from the ledger and the runtime on the next
tick (``inflight.RESTART_SAFE``).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.services.workspace import WorkspaceContext, context_for_workspace

from . import runtime_client, snapshots, turns
from .models import (
    REPO_DELETING,
    REPO_IDLE,
    REPO_RUNNING,
    TURN_ABANDONED,
    CodingRepo,
    CodingTurn,
)

logger = logging.getLogger("launchpad.coding")

STARTUP_DELAY_SECONDS = 20
MIN_INTERVAL_SECONDS = 5
PAGE_LIMIT = 2000
MAX_PAGES_PER_TICK = 20
SAVE_DEBOUNCE = timedelta(seconds=30)
START_CLAIM_TTL = timedelta(minutes=5)
DELETE_CLAIM_TTL = timedelta(minutes=10)
# the runtime's maxLifetime (8 h) plus slack: no microVM runs a turn longer
TURN_LIFETIME = timedelta(hours=8, minutes=10)
_READ_TIMEOUT_S = 60.0

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_sleep = time.sleep  # test seam


def interval_seconds(settings: Settings | None = None) -> int:
    current = settings or get_settings()
    return max(MIN_INTERVAL_SECONDS, int(current.coding_watcher_interval_s))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _abandon(repo_id: str, turn_id: str, reason: str, ctx: WorkspaceContext,
             end_seq: int) -> bool:
    return turns.settle(repo_id, turn_id, status=TURN_ABANDONED,
                        error=f"the turn was abandoned ({reason})", end_seq=end_seq, ctx=ctx)


def check_running(repo: CodingRepo, ctx: WorkspaceContext,
                  now: datetime) -> dict[str, Any]:
    """One running repo: settle what ended, advance the cursor. Raises on a
    runtime failure (the tick logs it and moves on)."""
    turn_id = repo.active_turn_id
    outcome: dict[str, Any] = {"repo_id": repo.id, "settled": []}
    if not turn_id:
        return outcome  # a start in flight — the claim sweep handles a dead one
    cursor = int(repo.last_seq or 0)
    completions: dict[str, Any] = {}
    page: dict[str, Any] = {}
    drained = False
    failed: list[int] = []
    for _ in range(MAX_PAGES_PER_TICK):
        page = runtime_client.repo_call(
            ctx, repo, "events", after=cursor, wait_ms=0, limit=PAGE_LIMIT, compact=True,
            read_timeout=_READ_TIMEOUT_S,
        ) or {}
        records = [r for r in page.get("events") or [] if isinstance(r, dict)]
        outcome["settled"] += turns.settle_from_records(repo.id, records, completions,
                                                        ctx=ctx, failed_seqs=failed)
        next_after = page.get("next_after")
        seq_now = page.get("seq")
        if page.get("cursor_ahead") and isinstance(seq_now, int):
            next_after = seq_now  # session storage restarted below our cursor
        if isinstance(next_after, int) and next_after > cursor:
            cursor = next_after
        drained = not records or (isinstance(seq_now, int) and cursor >= seq_now)
        if drained or failed:
            break
    if failed:
        # an end record whose settlement failed (a locked ledger): read it again next
        # tick — past it, the turn would look lost and be abandoned uncharged
        cursor = min(cursor, min(failed) - 1)
    db = SessionLocal()
    try:
        db.execute(
            update(CodingRepo)
            .where(CodingRepo.id == repo.id, CodingRepo.last_seq < cursor)
            .values(last_seq=cursor)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        fresh = db.get(CodingRepo, repo.id)
        turn = (
            db.query(CodingTurn)
            .filter(CodingTurn.repo_id == repo.id, CodingTurn.turn_id == turn_id)
            .first()
        )
    finally:
        db.close()
    if (fresh is None or fresh.active_turn_id != turn_id or turn_id in outcome["settled"]
            or failed):
        return outcome
    active = page.get("active_turn")
    runtime_turn = str(active.get("turn_id") or "") if isinstance(active, dict) else ""
    if drained and runtime_turn != turn_id:
        # drained, and the runtime runs something else (or nothing) without ever
        # having reported this turn's end: it is gone
        if _abandon(repo.id, turn_id, "the runtime no longer runs it", ctx, cursor):
            outcome["settled"].append(turn_id)
        return outcome
    started = _utc(turn.started_at) if turn is not None else None
    if started is not None and now - started > TURN_LIFETIME:
        if _abandon(repo.id, turn_id, "it outlived the runtime's 8-hour limit", ctx,
                    cursor):
            outcome["settled"].append(turn_id)
    return outcome


def _release_stale_claims(now: datetime) -> int:
    db = SessionLocal()
    try:
        released = db.execute(
            update(CodingRepo)
            .where(CodingRepo.status == REPO_RUNNING,
                   CodingRepo.active_turn_id.is_(None),
                   CodingRepo.updated_at < now - START_CLAIM_TTL)
            .values(status=REPO_IDLE, updated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        released += db.execute(
            update(CodingRepo)
            .where(CodingRepo.status == REPO_DELETING,
                   CodingRepo.updated_at < now - DELETE_CLAIM_TTL)
            .values(status=REPO_IDLE, updated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        return int(released or 0)
    finally:
        db.close()


def run_once(now: datetime | None = None) -> dict[str, Any]:
    """One pass over every workspace's coding repos. Never raises."""
    now = now or datetime.now(UTC)
    outcome: dict[str, Any] = {"checked": 0, "settled": [], "snapshots": 0, "released": 0,
                               "failures": []}
    contexts: dict[str, WorkspaceContext] = {}

    def ctx_of(workspace_id: str) -> WorkspaceContext:
        if workspace_id not in contexts:
            contexts[workspace_id] = context_for_workspace(workspace_id)
        return contexts[workspace_id]

    try:
        outcome["released"] = _release_stale_claims(now)
    except Exception as exc:  # noqa: BLE001
        outcome["failures"].append(f"claims: {exc}")
    try:
        db = SessionLocal()
        try:
            running = db.query(CodingRepo).filter(
                CodingRepo.status == REPO_RUNNING,
                CodingRepo.active_turn_id.is_not(None),
            ).all()
            dirty = db.query(CodingRepo).filter(
                CodingRepo.status == REPO_IDLE,
                CodingRepo.dirty_at.is_not(None),
                CodingRepo.dirty_at < now - SAVE_DEBOUNCE,
            ).all()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        outcome["failures"].append(f"ledger: {exc}")
        return outcome
    for repo in running:
        try:
            ctx = ctx_of(repo.workspace_id or "")
            if not runtime_client.installed_runtime_arn(ctx):
                continue
            result = check_running(repo, ctx, now)
            outcome["checked"] += 1
            outcome["settled"] += result["settled"]
        except Exception as exc:  # noqa: BLE001 — one repo never stops the tick
            outcome["failures"].append(f"{repo.id}: {exc}")
    for repo in dirty:
        if snapshots.backing_off(repo.id):
            continue
        try:
            ctx = ctx_of(repo.workspace_id or "")
            if not runtime_client.installed_runtime_arn(ctx):
                continue
            if snapshots.take_quietly(repo.id, ctx=ctx, reason="save"):
                outcome["snapshots"] += 1
        except Exception as exc:  # noqa: BLE001
            outcome["failures"].append(f"{repo.id}: {exc}")
    return outcome


def start(settings: Settings | None = None) -> threading.Thread | None:
    """Start the daemon thread unless switched off; at most one per process."""
    global _THREAD
    current = settings or get_settings()
    if not current.coding_watcher_enabled:
        logger.info("coding watcher is switched off (LAUNCHPAD_CODING_WATCHER_ENABLED=false)")
        return None
    interval = interval_seconds(current)
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD

        def loop() -> None:
            _sleep(STARTUP_DELAY_SECONDS)  # nosemgrep: arbitrary-sleep
            while True:
                result = run_once()
                if result.get("failures"):
                    logger.warning("coding watcher: %d failure(s): %s",
                                   len(result["failures"]), result["failures"][:3])
                _sleep(interval)  # nosemgrep: arbitrary-sleep

        _THREAD = threading.Thread(target=loop, daemon=True, name="coding-watcher")
        _THREAD.start()
        logger.info("coding watcher started: every %d s", interval)
        return _THREAD


def running() -> bool:
    return _THREAD is not None and _THREAD.is_alive()
