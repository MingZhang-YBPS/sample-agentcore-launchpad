"""What an evaluation run really cost (eval-cost §1.2): `EvalRun.cost_actual`.

Two moments write it:

* **while the run replays** (`LiveSpend`) — the token usage every invoke reports
  (the runtime's normalized `usage` event, §16.3-3) and each invoke's wall time,
  under `cost_actual.live`. The record is not `final`, carries no top-level
  `agent_usd`, so the project's monthly budget keeps counting the estimate until
  the run ends; the task detail shows it as the live spend.
* **at every terminal state** (`record`) — completed, failed or stopped — the
  measured record:

      agent_usd / agent_tokens       usage events → the sessions' spans → estimate
      runtime_usd / runtime_seconds  invoke wall time × the Runtime price, else estimate
      judge_usd / managed_evaluations_usd
                                     judgements × tokens per judgement × price
      judge_tokens                   per evaluator, measured tokens per judgement only —
                                     the next estimate samples them
      total_usd, unpriced_models, source, computed_at, final: true

A failed or stopped run records what it had already spent. A passive run (past
sessions, a look-back window, an online report) invoked nothing: its agent and
runtime parts are 0, only the judges cost.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.project_scope import project_context
from app.dlc import cost as cost_svc
from app.evaluation.models import EvalRun

logger = logging.getLogger(__name__)

TERMINAL = frozenset({"completed", "failed", "stopped"})
SOURCES = ("usage_events", "spans", "estimate_fallback")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _agent_model(db: Session, agent_id: str | None) -> str | None:
    from app.models.ledger import Agent

    if not agent_id:
        return None
    with project_context(None):
        agent = db.get(Agent, agent_id)
    return ((agent.spec if agent is not None else None) or {}).get("model_id")


def _tokens_usd(model: str | None, usage: dict[str, Any]) -> float | None:
    """Usage-event tokens at the model's price (cache reads / writes when priced)."""
    price = cost_svc._price_of(model)
    if not price:
        return None
    usd = (float(usage.get("input_tokens") or 0) * float(price.get("input", 0.0))
           + float(usage.get("output_tokens") or 0) * float(price.get("output", 0.0)))
    for field, key in (("cache_read_input_tokens", "cache_read"),
                       ("cache_write_input_tokens", "cache_write")):
        if usage.get(field) and price.get(key) is not None:
            usd += float(usage[field]) * float(price[key])
    return usd / 1e6


# ── live spend (while the run replays) ─────────────────────────────────────────


class LiveSpend:
    """Accumulates what the replay has spent so far, for `cost_actual.live`."""

    def __init__(self, model: str | None) -> None:
        self.model = model
        self.usage: dict[str, int] = {}
        self.seconds = 0.0
        self.invokes = 0
        self.reported = 0  # invokes whose response carried a usage event
        self.sessions = 0

    @classmethod
    def for_run(cls, run_id: str) -> LiveSpend:
        db = SessionLocal()
        try:
            run = db.get(EvalRun, run_id)
            return cls(_agent_model(db, run.agent_id if run else None))
        except Exception:  # noqa: BLE001 - metering never fails a run
            return cls(None)
        finally:
            db.close()

    def metered(self, invoke: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        """`invoke`, timed, with the usage its response reports added up."""

        def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            started = time.monotonic()
            try:
                result = invoke(*args, **kwargs)
            finally:
                self.seconds += time.monotonic() - started
                self.invokes += 1
            usage = result.get("usage") if isinstance(result, dict) else None
            if isinstance(usage, dict):
                self.reported += 1
                for field, value in usage.items():
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        self.usage[field] = self.usage.get(field, 0) + int(value)
            return result

        return wrapped

    def session_done(self) -> dict[str, Any]:
        """Count a finished session; the `cost_actual` value to store now."""
        self.sessions += 1
        return {"final": False, "live": self.snapshot()}

    def snapshot(self) -> dict[str, Any]:
        agent = _tokens_usd(self.model, self.usage) if self.usage else None
        runtime = cost_svc.runtime_usd(self.seconds)
        return {
            "sessions": self.sessions,
            "invokes": self.invokes,
            "usage_invokes": self.reported,
            "tokens": dict(self.usage),
            "model": self.model,
            "seconds": round(self.seconds, 2),
            "agent_usd": round(agent, 6) if agent is not None else None,
            "runtime_usd": round(runtime, 6),
            "total_usd": round((agent or 0.0) + runtime, 6),
            "updated_at": _now(),
        }


# ── the measured record (terminal state) ───────────────────────────────────────


def _spans_metrics(session_ids: list[str], workspace: Any) -> dict[str, dict]:
    from app.services import observability

    return observability.session_metrics_bulk(session_ids, workspace)


# The spans reader, replaceable (tests; a caller that already holds the metrics
# passes them to `compute`).
SPANS_READER: Callable[[list[str], Any], dict[str, dict]] = _spans_metrics


def _record_usage(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per evaluator: judgements in the results stream, and the judge tokens the
    records report (`gen_ai.usage.*`, when AWS writes them)."""
    from app.evaluation import agentcore_eval as ac

    out: dict[str, dict[str, float]] = {}
    for attrs in records:
        evaluator_id = ac._record_evaluator_id(attrs)
        if not evaluator_id:
            continue
        acc = out.setdefault(evaluator_id, {"judgements": 0, "measured": 0, "tokens": 0.0})
        acc["judgements"] += 1
        tin = attrs.get("gen_ai.usage.input_tokens")
        tout = attrs.get("gen_ai.usage.output_tokens")
        if isinstance(tin, int | float) or isinstance(tout, int | float):
            acc["measured"] += 1
            acc["tokens"] += float(tin or 0) + float(tout or 0)
    return out


def _score_counts(run: EvalRun) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in run.scores or []:
        if isinstance(entry, dict) and entry.get("evaluatorId") and entry.get("count"):
            counts[str(entry["evaluatorId"])] = int(entry["count"])
    return counts


def _criteria_rows(db: Session, run: EvalRun) -> list[Any]:
    if not run.criteria_set_id:
        return []
    from app.dlc import criteria as criteria_svc

    try:
        return list(criteria_svc.criteria_of(db, run.criteria_set_id))
    except Exception:  # noqa: BLE001
        return []


def _invoked(run: EvalRun, live: dict[str, Any]) -> bool:
    """Did the platform invoke the agent for this run (a dataset replay)?"""
    return bool(run.attempts) or bool(live.get("invokes"))


def _estimate_of(db: Session, run: EvalRun, workspace: Any, invoked: bool) -> dict[str, Any]:
    """The run's own estimate; a passive run never stored one — price it now."""
    if run.cost_estimate:
        return run.cost_estimate
    from app.models.ledger import Agent, Workspace

    try:
        with project_context(None):
            agent = db.get(Agent, run.agent_id) if run.agent_id else None
            workspace_row = db.get(Workspace, run.workspace_id) if run.workspace_id else None
        return cost_svc.estimate(
            db, agent=agent, workspace=workspace_row, workspace_ctx=workspace,
            items=[{"turns": [{}]}] * len(run.session_ids or []) if invoked else [],
            evaluators=list(run.evaluators or []) if run.mode == "evaluators" else [],
            criteria_rows=_criteria_rows(db, run),
            scope="replay" if invoked else "passive",
            sessions=len(run.session_ids or []),
        )
    except Exception as exc:  # noqa: BLE001 - a fallback, never a failure
        logger.debug("estimate fallback for run %s failed: %s", run.id, exc)
        return {}


def compute(
    db: Session,
    run: EvalRun,
    workspace: Any,
    *,
    metrics: dict[str, dict] | None = None,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The run's measured cost (see the module doc); reads, never writes."""
    previous = run.cost_actual or {}
    live = previous.get("live") or {}
    invoked = _invoked(run, live)
    session_ids = [s for s in (run.session_ids or []) if s]
    sessions = len(session_ids)
    estimate = _estimate_of(db, run, workspace, invoked)
    est_sessions = int(estimate.get("sessions") or 0)
    done_share = (min(1.0, sessions / est_sessions) if est_sessions else 1.0)
    unpriced: list[str] = []
    sources: list[str] = []
    notes: list[str] = []

    # ── the agent under test ──
    agent_usd: float | None = 0.0
    agent_tokens: dict[str, int] = {"input": 0, "output": 0}
    if invoked and sessions:
        tokens = live.get("tokens") or {}
        live_complete = tokens and live.get("usage_invokes", 0) >= live.get("invokes", 0) > 0
        agent_usd = None
        if live_complete:
            agent_usd = _tokens_usd(live.get("model"), tokens)
            agent_tokens = {"input": int(tokens.get("input_tokens") or 0),
                            "output": int(tokens.get("output_tokens") or 0)}
            if agent_usd is None and live.get("model"):
                unpriced.append(str(live["model"]))
            sources.append("usage_events")
        if agent_usd is None and workspace is not None:
            try:
                metrics = metrics if metrics is not None else SPANS_READER(session_ids, workspace)
            except Exception as exc:  # noqa: BLE001 - spans are one source of three
                metrics = None
                notes.append(f"spans unreadable: {type(exc).__name__}")
            measured = [m for m in (metrics or {}).values() if m]
            if measured:
                priced = [m for m in measured if m.get("cost_usd") is not None]
                if priced:
                    agent_usd = sum(float(m["cost_usd"]) for m in priced)
                    if len(priced) < len(measured):
                        notes.append(f"{len(measured) - len(priced)} sessions unpriced")
                    if "usage_events" in sources:
                        sources.remove("usage_events")
                    sources.append("spans")
                agent_tokens = {"input": 0, "output": 0,
                                "total": int(sum(float(m.get("tokens") or 0) for m in measured))}
        if agent_usd is None:
            fallback = estimate.get("agent_usd")
            if fallback is not None:
                agent_usd = float(fallback) * done_share
                sources = [src for src in sources if src != "usage_events"]
                sources.append("estimate_fallback")
    # ── runtime compute ──
    runtime_seconds = 0.0
    if invoked and sessions:
        if live.get("seconds"):
            runtime_seconds = float(live["seconds"])
        else:
            runtime_seconds = float(estimate.get("runtime_seconds") or 0.0) * done_share
            if runtime_seconds:
                sources.append("estimate_fallback")
    runtime = cost_svc.runtime_usd(runtime_seconds)

    # ── the judges ──
    judge_total = managed_total = 0.0
    judge_tokens: dict[str, float] = {}
    judgements: dict[str, int] = {}
    details: list[dict[str, Any]] = []
    # judges ran only once a batch existed (a run that failed before it judged nothing)
    if run.mode == "evaluators" and run.evaluators and (
        run.batch_eval_id or run.status == "completed"
    ):
        per_record = _record_usage(records or [])
        counts = _score_counts(run)
        judges = cost_svc._judge_evaluators(list(run.evaluators or []), _criteria_rows(db, run))
        judge_model = cost_svc._default_judge_model()
        judge_price = cost_svc._price_of(judge_model)
        estimated = {j.get("evaluator_id"): j for j in estimate.get("judges") or []}
        for judge in judges:
            eid = judge["evaluator_id"]
            stream = per_record.get(eid) or {}
            n = int(stream.get("judgements") or counts.get(eid) or 0)
            if not n and run.status != "completed":
                continue
            if not n:  # completed but AWS reported no count: what the estimate assumed
                n = int(float((estimated.get(eid) or {}).get("judgements") or 0) * done_share)
            judgements[eid] = n
            if stream.get("measured"):
                per_judgement = stream["tokens"] / stream["measured"]
                judge_tokens[eid] = round(per_judgement, 1)
                basis = "measured"
            else:
                per_judgement = (cost_svc._judge_tokens(db, eid)
                                 or float((estimated.get(eid) or {}).get("tokens_per_item") or 0)
                                 or cost_svc.ROUGH_JUDGE_TOKENS)
                basis = "estimated"
            tin, tout = cost_svc.split_judge_tokens(per_judgement * n)
            if judge["billed_by"] == "agentcore_evaluations":
                usd = cost_svc.managed_usd(tin, tout)
                managed_total += usd
            elif judge_price:
                usd = cost_svc.judge_usd(tin, tout, judge_price)
                judge_total += usd
            else:
                usd = None
                if judge_model and judge_model not in unpriced:
                    unpriced.append(judge_model)
            details.append({**judge, "judgements": n,
                            "tokens_per_judgement": round(per_judgement, 1), "tokens_basis": basis,
                            "usd": round(usd, 6) if usd is not None else None})

    parts = [agent_usd, runtime, judge_total, managed_total]
    total = sum(p or 0.0 for p in parts)
    if invoked:
        # the agent's part is the run's main bill: its source names the record's
        source = next((src for src in SOURCES if src in sources), "estimate_fallback")
    else:
        source = "usage_events" if judge_tokens else "estimate_fallback"
    out: dict[str, Any] = {
        "agent_usd": round(agent_usd, 6) if agent_usd is not None else None,
        "agent_tokens": agent_tokens,
        "runtime_usd": round(runtime, 6),
        "runtime_seconds": round(runtime_seconds, 1),
        "judge_usd": round(judge_total, 6),
        "judge_tokens": judge_tokens,
        "judgements": judgements,
        "judges": details,
        "managed_evaluations_usd": round(managed_total, 6),
        "total_usd": round(total, 6),
        "unpriced_models": unpriced,
        "sessions": sessions,
        "invoked": invoked,
        "source": source,
        "sources": sorted(set(sources)),
        "notes": notes,
        "status": run.status,
        "partial": run.status != "completed",
        "computed_at": _now(),
        "final": True,
    }
    # samples the next estimate reads: measured only (cost._run_history skips fallbacks)
    if invoked and sessions and agent_usd is not None and source != "estimate_fallback":
        out["agent_usd_per_session"] = round(agent_usd / sessions, 6)
    if invoked and sessions and live.get("seconds"):
        out["runtime_seconds_per_session"] = round(float(live["seconds"]) / sessions, 2)
    if live:
        out["live"] = live
    est_total = estimate.get("total_usd")
    if est_total is not None:
        out["estimate_total_usd"] = est_total
        out["delta_usd"] = round(total - float(est_total), 6)
        out["delta_pct"] = (round((total - float(est_total)) / float(est_total) * 100, 1)
                            if float(est_total) > 0 else None)
    return out


def record(
    run_id: str,
    workspace: Any,
    *,
    records_reader: Callable[[], list[dict[str, Any]]] | None = None,
    only_if_missing: bool = False,
) -> dict[str, Any] | None:
    """Write `cost_actual` for a run in a terminal state. Never raises.

    `only_if_missing` keeps a record already written (the finish path wrote it with
    the results stream at hand; a later fallback call must not replace it)."""
    db = SessionLocal()
    try:
        with project_context(None):
            run = db.get(EvalRun, run_id)
        if run is None or run.status not in TERMINAL:
            return None
        if only_if_missing and (run.cost_actual or {}).get("final"):
            return run.cost_actual
        # the row's project, not the worker thread's (none): every read is the run's
        with project_context(run.project_id):
            records = None
            if records_reader is not None:
                try:
                    records = records_reader() or []
                except Exception:  # noqa: BLE001
                    records = None
            out = compute(db, run, workspace, records=records)
            run.cost_actual = out
            db.commit()
            return out
    except Exception as exc:  # noqa: BLE001 - the run itself already ended
        db.rollback()
        logger.warning("cost of run %s not recorded: %s", run_id, exc)
        return None
    finally:
        db.close()


def summary(run: EvalRun) -> dict[str, Any]:
    """The one number a list row shows: the measured total once final, else the live
    spend while running (marked), else the estimate (marked)."""
    actual = run.cost_actual or {}
    if actual.get("final"):
        return {"usd": actual.get("total_usd"), "kind": "actual",
                "estimate_usd": actual.get("estimate_total_usd")}
    live = actual.get("live") or {}
    estimate = (run.cost_estimate or {}).get("total_usd")
    if live and run.status not in TERMINAL:
        return {"usd": live.get("total_usd"), "kind": "live", "estimate_usd": estimate}
    return {"usd": estimate, "kind": "estimate", "estimate_usd": estimate}


def card(run: EvalRun) -> dict[str, Any]:
    """The task detail's "This run's cost" card."""
    actual = run.cost_actual or {}
    estimate = run.cost_estimate or {}
    final = bool(actual.get("final"))
    est_total = actual.get("estimate_total_usd", estimate.get("total_usd"))
    return {
        "run_id": run.id,
        "status": run.status,
        "final": final,
        "running": run.status not in TERMINAL,
        "actual": actual if final else None,
        "live": actual.get("live") if not final else None,
        "estimate": estimate or None,
        "estimate_total_usd": est_total,
        "estimate_range": estimate.get("range"),
        "summary": summary(run),
        "prices": {k: cost_svc.eval_prices().get(k) for k in ("source", "updated_at")},
    }
