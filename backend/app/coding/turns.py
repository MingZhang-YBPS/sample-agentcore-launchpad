"""Coding turns: start, interrupt, and the idempotent settlement (parent design §7.5).

**Start.** The handler validates the prompt, model and effort, claims the repo
with a conditional ``UPDATE … WHERE status='idle'`` (one turn per repo — a
second start is ``409 coding.turn_running``), asks the runtime to start the turn
(it returns at once; the turn runs in the microVM under ``/ping HealthyBusy``)
and records the ``coding_turns`` row.

**Settle.** The runtime appends ``launchpad/turnUsage`` (or
``launchpad/turnAbandoned``) right after a turn ends, carrying the turn's token
usage (codex ``TokenUsageBreakdown``). Whichever reader sees it first — the SSE
proxy or the watcher — settles: one ``UPDATE coding_turns SET settled_at=…,
usage=… WHERE … AND settled_at IS NULL``; only the winner requests a snapshot.
The repo goes back to ``idle`` on every attempt (conditional on its active
turn), so an interrupted settlement heals on the next one.

**Usage.** The turn row keeps codex's counts as reported: ``inputTokens``
*includes* ``cachedInputTokens`` and ``cacheWriteInputTokens`` (live 2026-10-19:
94 630 = 58 007 + 36 613 + 10), and ``outputTokens`` includes
``reasoningOutputTokens``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

from . import infra, install, runtime_client, snapshots
from .models import (
    PROMPT_STORED_MAX,
    REPO_IDLE,
    REPO_RUNNING,
    TURN_ABANDONED,
    TURN_COMPLETED,
    TURN_FAILED,
    TURN_INTERRUPTED,
    TURN_RUNNING,
    CodingRepo,
    CodingTurn,
)
from .runtime_client import CodingRuntimeError

logger = logging.getLogger("launchpad.coding")

MAX_PROMPT_BYTES = 32 * 1024
TURN_COMPLETED_EVENT = "turn/completed"
TURN_USAGE_EVENT = "launchpad/turnUsage"
TURN_ABANDONED_EVENT = "launchpad/turnAbandoned"
SETTLE_EVENTS = (TURN_USAGE_EVENT, TURN_ABANDONED_EVENT)
_ERROR_MAX = 2000
_TERMINAL = (TURN_COMPLETED, TURN_INTERRUPTED, TURN_FAILED)


def _now() -> datetime:
    return datetime.now(UTC)


# ── models and efforts ──────────────────────────────────────────────────────


def allowed_models(ctx: WorkspaceContext) -> list[str]:
    """The configured coding models the installed runtime also allows (its role and
    container were built for the list recorded at install; a model added since
    needs a repair first)."""
    models = infra.coding_models()
    installed = (ctx.resources or {}).get(infra.KEY_MODELS)
    if isinstance(installed, list):
        models = [m for m in models if m in installed]
    return models


def resolve_model_and_effort(ctx: WorkspaceContext, model: str | None,
                             effort: str | None) -> tuple[str, str]:
    settings = get_settings()
    models = allowed_models(ctx)
    chosen = (model or "").strip() or (
        settings.coding_default_model if settings.coding_default_model in models else ""
    )
    if chosen not in models:
        raise AppError("coding.model_invalid",
                       f"model {chosen or '(none)'} is not offered by the coding runtime",
                       {"model": chosen, "models": models}, status_code=422)
    efforts = list(settings.coding_efforts)
    level = (effort or "").strip() or settings.coding_default_effort
    if level not in efforts:
        raise AppError("coding.effort_invalid", f"reasoning effort must be one of {efforts}",
                       {"effort": level, "efforts": efforts}, status_code=422)
    return chosen, level


# ── usage ───────────────────────────────────────────────────────────────────


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return 0
    return int(value)


def ledger_usage(raw: Any) -> dict[str, int] | None:
    """codex ``TokenUsageBreakdown`` → the ledger shape (codex counts kept as is)."""
    if not isinstance(raw, Mapping):
        return None
    return {
        "input": _count(raw.get("inputTokens")),
        "cached_input": _count(raw.get("cachedInputTokens")),
        "cache_write": _count(raw.get("cacheWriteInputTokens")),
        "output": _count(raw.get("outputTokens")),
        "reasoning_output": _count(raw.get("reasoningOutputTokens")),
    }


# ── start / interrupt ───────────────────────────────────────────────────────


def _release_claim(db: Session, repo_id: str) -> None:
    db.rollback()
    db.execute(
        update(CodingRepo)
        .where(CodingRepo.id == repo_id, CodingRepo.status == REPO_RUNNING,
               CodingRepo.active_turn_id.is_(None))
        .values(status=REPO_IDLE, updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _adopt_running_turn(db: Session, ctx: WorkspaceContext, repo: CodingRepo) -> bool:
    """The runtime runs a turn the ledger does not know (the backend died between
    the runtime's answer and its commit): record it on the claimed repo so it is
    watched and settled. True when adopted."""
    try:
        state = runtime_client.repo_call(ctx, repo, "status") or {}
    except Exception as exc:  # noqa: BLE001 — the 409 stands either way
        logger.info("coding repo %s: status for adoption failed: %s", repo.id, exc)
        return False
    active = state.get("active_turn") if isinstance(state, dict) else None
    turn_id = str((active or {}).get("turn_id") or "")
    if not turn_id:
        return False
    known = (
        db.query(CodingTurn)
        .filter(CodingTurn.repo_id == repo.id, CodingTurn.turn_id == turn_id)
        .first()
    )
    if known is None:
        db.add(CodingTurn(
            workspace_id=repo.workspace_id, repo_id=repo.id, turn_id=turn_id,
            thread_id=(active or {}).get("thread_id"), prompt="",
            model=str((active or {}).get("model") or ""),
            effort=str((active or {}).get("effort") or ""), status=TURN_RUNNING,
        ))
    adopted = db.execute(
        update(CodingRepo)
        .where(CodingRepo.id == repo.id, CodingRepo.status == REPO_RUNNING,
               CodingRepo.active_turn_id.is_(None))
        .values(active_turn_id=turn_id, updated_at=_now())
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    if not adopted:
        db.rollback()
        return False
    db.commit()
    logger.warning("coding repo %s: adopted running turn %s", repo.id, turn_id)
    return True


def start_turn(
    db: Session,
    ctx: WorkspaceContext,
    repo: CodingRepo,
    *,
    prompt: str,
    model: str | None,
    effort: str | None,
) -> CodingTurn:
    """Start one turn asynchronously and return its ledger row (``running``)."""
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AppError("coding.prompt_too_long", "the prompt is larger than 32 KB",
                       {"max_bytes": MAX_PROMPT_BYTES}, status_code=413)
    model_id, level = resolve_model_and_effort(ctx, model, effort)
    runtime_client.require_runtime_arn(ctx)
    live = install.live_job(db, repo.workspace_id or "")
    if live is not None and live.type == install.UNINSTALL_JOB_TYPE:
        raise AppError("coding.runtime_busy", "the coding runtime is being uninstalled",
                       {"job_id": live.id, "type": live.type}, status_code=409)

    claimed = db.execute(
        update(CodingRepo)
        .where(CodingRepo.id == repo.id, CodingRepo.status == REPO_IDLE)
        .values(status=REPO_RUNNING, active_turn_id=None, updated_at=_now())
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if claimed != 1:
        raise AppError("coding.turn_running", "a turn is running in this repo",
                       status_code=409)
    try:
        result = runtime_client.repo_call(
            ctx, repo, "start_turn", prompt=prompt, model=model_id, effort=level
        ) or {}
        turn_id = str(result.get("turn_id") or "")
        if not turn_id:
            raise CodingRuntimeError(runtime_client.RUNTIME_PROTOCOL,
                                     "start_turn returned no turn id")
    except CodingRuntimeError as exc:
        # a turn the ledger lost keeps the claim (adopted); anything else frees it
        if not (exc.code == "turn_running" and _adopt_running_turn(db, ctx, repo)):
            _release_claim(db, repo.id)
        raise runtime_client.api_error(exc) from exc
    except BaseException:
        _release_claim(db, repo.id)
        raise

    started = _now()
    turn = CodingTurn(
        workspace_id=repo.workspace_id, repo_id=repo.id, turn_id=turn_id,
        thread_id=result.get("thread_id"), prompt=prompt[:PROMPT_STORED_MAX], model=model_id,
        effort=level, status=TURN_RUNNING, started_at=started,
    )
    db.add(turn)
    seq = _count(result.get("seq"))
    db.execute(
        update(CodingRepo)
        .where(CodingRepo.id == repo.id)
        .values(active_turn_id=turn_id, model=model_id, effort=level, updated_at=started)
        .execution_options(synchronize_session=False)
    )
    # the watcher reads this turn's events, not the whole history before it
    db.execute(
        update(CodingRepo)
        .where(CodingRepo.id == repo.id, CodingRepo.last_seq < seq)
        .values(last_seq=seq)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(turn)
    return turn


def interrupt(ctx: WorkspaceContext, repo: CodingRepo) -> dict[str, Any]:
    """Interrupt the running turn; the runtime then completes it as ``interrupted``
    and the settlement follows the ordinary path."""
    if repo.status != REPO_RUNNING:
        return {"interrupted": False}
    result = runtime_client.run(ctx, repo, "interrupt") or {}
    return {"interrupted": bool(result.get("interrupted"))}


# ── settlement ──────────────────────────────────────────────────────────────


def _timestamp(ms: Any) -> datetime | None:
    if isinstance(ms, bool) or not isinstance(ms, int | float) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def settle(
    repo_id: str,
    turn_id: str,
    *,
    status: str,
    error: str | None = None,
    usage: Any = None,
    completed_at: datetime | None = None,
    end_seq: int | None = None,
    ctx: WorkspaceContext | None = None,
) -> bool:
    """Settle one turn; True when this call won (usage recorded + snapshot requested).

    ``usage`` is codex's ``TokenUsageBreakdown`` (or None: nothing reported);
    ``end_seq`` the event-log seq of the turn's end record. Idempotent, and safe
    to race: exactly one caller wins."""
    if not turn_id:
        return False
    db = SessionLocal()
    stamp = _now()
    won = False
    try:
        ledger = ledger_usage(usage)
        values: dict[str, Any] = {
            "settled_at": stamp,
            "status": status,
            "completed_at": completed_at or stamp,
            "error": (error or None) and str(error)[:_ERROR_MAX],
        }
        if ledger is not None:
            values["usage"] = ledger
        won = db.execute(
            update(CodingTurn)
            .where(CodingTurn.repo_id == repo_id, CodingTurn.turn_id == turn_id,
                   CodingTurn.settled_at.is_(None))
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount == 1
        db.commit()
        # every attempt (not only the winner) frees the repo, so a settlement
        # that died between its two commits heals on the next reader
        db.execute(
            update(CodingRepo)
            .where(CodingRepo.id == repo_id, CodingRepo.status == REPO_RUNNING,
                   CodingRepo.active_turn_id == turn_id)
            .values(status=REPO_IDLE, active_turn_id=None, updated_at=stamp)
            .execution_options(synchronize_session=False)
        )
        if end_seq:
            db.execute(
                update(CodingRepo)
                .where(CodingRepo.id == repo_id, CodingRepo.end_seq < end_seq)
                .values(end_seq=end_seq)
                .execution_options(synchronize_session=False)
            )
        db.commit()
    finally:
        db.close()
    if won:
        logger.info("coding repo %s: turn %s settled (%s)", repo_id, turn_id, status)
        snapshots.request(repo_id, ctx=ctx, reason="settle")
    return won


def _seq_of(record: Mapping[str, Any]) -> int | None:
    seq = record.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 else None


def _status_of(value: Any) -> str:
    return value if value in _TERMINAL else TURN_FAILED


def settle_from_records(
    repo_id: str,
    records: Iterable[Mapping[str, Any]],
    completions: dict[str, Mapping[str, Any]],
    *,
    ctx: WorkspaceContext | None = None,
    failed_seqs: list[int] | None = None,
) -> list[str]:
    """Settle every turn whose end record is in ``records`` (event-log order).
    ``completions`` carries ``turn/completed`` turns across pages (their error
    message); returns the turn ids whose end was settled (or found settled). Never
    raises: the seq of an end record whose settlement failed goes to
    ``failed_seqs``, so a cursor owner (the watcher) reads it again instead of
    skipping past it."""
    ended: list[str] = []
    for record in records:
        method = record.get("method")
        params = record.get("params") if isinstance(record.get("params"), Mapping) else {}
        try:
            if method == TURN_COMPLETED_EVENT:
                turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
                if turn.get("id"):
                    completions[str(turn["id"])] = turn
            elif method == TURN_USAGE_EVENT:
                turn_id = str(params.get("turnId") or "")
                done = completions.get(turn_id) or {}
                err = done.get("error") if isinstance(done.get("error"), Mapping) else {}
                settle(repo_id, turn_id, status=_status_of(params.get("status")
                                                              or done.get("status")),
                       error=err.get("message"), usage=params.get("usage"),
                       completed_at=_timestamp(record.get("ts")),
                       end_seq=_seq_of(record), ctx=ctx)
                ended.append(turn_id)
            elif method == TURN_ABANDONED_EVENT:
                turn_id = str(params.get("turnId") or "")
                reason = str(params.get("reason") or "unknown")
                settle(repo_id, turn_id, status=TURN_ABANDONED,
                       error=f"the turn was abandoned ({reason})", usage=params.get("usage"),
                       completed_at=_timestamp(record.get("ts")),
                       end_seq=_seq_of(record), ctx=ctx)
                ended.append(turn_id)
        except Exception:  # noqa: BLE001 — a reader keeps reading; the watcher retries
            logger.exception("coding repo %s: settling from %s failed", repo_id, method)
            seq = _seq_of(record)
            if failed_seqs is not None and seq is not None:
                failed_seqs.append(seq)
    return ended
