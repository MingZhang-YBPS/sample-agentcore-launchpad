"""What an evaluation run will cost, before it is submitted (§5.3, eval-cost §1.1).

pass^k multiplies the bill by k, so the task wizard asks for it only with the number in
view. Every evaluation entry — dataset replay, passive session / look-back runs, an
online evaluation (per day), the release gate — is priced with the same four parts,
each labelled with its basis:

    agent     invoked sessions × agent cost per session          (0 when nothing is invoked)
    runtime   invoked sessions × seconds per session × Runtime vCPU / memory price
    judge     Σ your-account LLM judges: judgements × tokens per judgement × judge price
    managed   Σ Builtin / ThirdParty evaluators: judgements × tokens × Evaluations price

* **agent cost per session** — this agent's mean spend per session over the last 7
  days (span token usage × the price map); else what its previous evaluation runs
  measured (`cost_actual`); else the model's price × the dataset's average turn count
  × a default token budget, labelled `rough`.
* **seconds per session** — the median of what this agent's previous runs measured,
  else a per-turn default.
* **tokens per judgement** — the median `judge_tokens` previous runs recorded for the
  evaluator (`run_cost` writes them), else a default prompt budget.

The total is a range (low / mid / high): measured parts are tighter than rough ones.
`total_usd` is the mid value, the number the limits are enforced against. A model with
no price is listed in `unpriced_models`, never silently counted as zero.

After the run, `app.evaluation.run_cost` measures the same parts (`cost_actual`).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.evaluation.models import EvalRun
from app.models.dlc import Criterion
from app.models.ledger import Agent, Workspace

logger = logging.getLogger(__name__)

# a turn with no history to go on: input + output tokens assumed per exchange
ROUGH_TOKENS_PER_TURN = 2500
ROUGH_JUDGE_TOKENS = 1800
# a judge prompt is mostly input (the trace it reads); its verdict is short
JUDGE_INPUT_SHARE = 0.85
# a turn's wall time on the runtime with no history (model calls + tools)
ROUGH_SECONDS_PER_TURN = 8.0
# a passive / online session's traces, when nothing says how many turns it has
ROUGH_TRACES_PER_SESSION = 2.0
DEFAULT_CONFIRM_USD = 5.0
MAX_REPEATS = 10
HISTORY_RUNS = 20

# Configurable unit prices (launchpad.yaml `eval_cost_prices` overrides any key).
# AgentCore Runtime bills per second of vCPU and memory; AgentCore Evaluations bills
# its built-in evaluators per judge token. List prices, us-east-1 — an estimate is
# labelled with this source and date so a stale number is visible as such.
DEFAULT_EVAL_PRICES: dict[str, Any] = {
    "runtime_vcpu_hour_usd": 0.0895,
    "runtime_gb_hour_usd": 0.00945,
    "runtime_vcpu": 1.0,
    "runtime_memory_gb": 2.0,
    "managed_input_per_1k_usd": 0.0024,
    "managed_output_per_1k_usd": 0.012,
    "source": "AgentCore list prices (us-east-1)",
    "updated_at": "2026-10-15",
}

# how far a part may land from its mid value, by basis: (low factor, high factor)
BANDS: dict[str, tuple[float, float]] = {
    "measured": (0.8, 1.3),
    "history_7d": (0.75, 1.5),
    "eval_runs": (0.75, 1.5),
    "rough": (0.5, 2.0),
}


def eval_prices() -> dict[str, Any]:
    """The unit prices the estimate and the actual cost use, with their source."""
    override = getattr(get_settings(), "eval_cost_prices", None) or {}
    prices = {**DEFAULT_EVAL_PRICES, **override}
    if override and "source" not in override:
        prices["source"] = "launchpad.yaml eval_cost_prices"
    return prices


def runtime_usd(seconds: float, prices: dict[str, Any] | None = None) -> float:
    """AgentCore Runtime compute for `seconds` of session time (vCPU + memory)."""
    p = prices or eval_prices()
    per_hour = (float(p["runtime_vcpu"]) * float(p["runtime_vcpu_hour_usd"])
                + float(p["runtime_memory_gb"]) * float(p["runtime_gb_hour_usd"]))
    return max(0.0, seconds) * per_hour / 3600


def managed_usd(input_tokens: float, output_tokens: float,
                prices: dict[str, Any] | None = None) -> float:
    """AgentCore Evaluations' charge for built-in judge tokens."""
    p = prices or eval_prices()
    return (input_tokens * float(p["managed_input_per_1k_usd"])
            + output_tokens * float(p["managed_output_per_1k_usd"])) / 1000


def judge_usd(input_tokens: float, output_tokens: float,
              price: dict[str, float]) -> float:
    """An LLM judge in this account: its tokens at the judge model's price (per 1M)."""
    return (input_tokens * float(price.get("input", 0.0))
            + output_tokens * float(price.get("output", 0.0))) / 1e6


def split_judge_tokens(total: float) -> tuple[float, float]:
    return total * JUDGE_INPUT_SHARE, total * (1 - JUDGE_INPUT_SHARE)


def is_managed(evaluator_id: str) -> bool:
    return evaluator_id.startswith(("Builtin.", "ThirdParty."))


def _policy(
    db: Session, workspace: Workspace | None, project_id: str | None = None,
) -> dict[str, float | None]:
    """The confirm threshold and the per-run limit, both the workspace release
    policy's (`eval_cost_confirm_usd`, `eval_cost_max_usd`). A project's one money
    limit is its monthly budget, checked when the run starts (projects §11.3)."""
    from app.services import release_gates

    policy = (workspace.release_policy if workspace else None) or {}
    confirm = policy.get("eval_cost_confirm_usd", DEFAULT_CONFIRM_USD)
    max_usd, max_source = release_gates.eval_cost_limit(db, workspace, project_id)
    return {
        "confirm_usd": float(confirm) if confirm is not None else None,
        "max_usd": max_usd,
        "max_source": max_source,
    }


def _price_of(model: str | None) -> dict[str, float] | None:
    from app.services.observability import match_price

    return match_price(model, get_settings().model_prices or {})


def _per_session_from_history(
    db: Session, agent: Agent, workspace_ctx: Any, *, days: int = 7
) -> tuple[float | None, str]:
    """Mean cost per session from this agent's own recent traffic."""
    try:
        from app.services import costs as cost_service

        report = cost_service.cost_report(db, workspace_ctx, range_key="7d")
    except Exception as exc:  # noqa: BLE001 - estimation never blocks the wizard
        logger.debug("cost history unavailable: %s", exc)
        return None, "unavailable"
    # One agent can report under several service names (the DEFAULT endpoint, then
    # live / candidate after the migration). Its cost is divided by its own sessions:
    # the workspace-wide session count (every other agent's traffic) would understate
    # the per-session cost by however many other agents the workspace runs.
    rows = [r for r in report.get("by_agent") or [] if r.get("agent_id") == agent.id]
    usd = sum(float(r.get("est_cost_usd") or 0.0) for r in rows)
    sessions = sum(int(r.get("sessions") or 0) for r in rows)
    if not usd or not sessions:
        return None, "no_history"
    return usd / sessions, "history_7d"


def _median(samples: list[float]) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


def _band(samples: list[float], basis: str) -> tuple[float, float]:
    """(low, high) factors around the median: the samples' own quartiles once there
    are a few of them, the basis's default band otherwise."""
    low, high = BANDS.get(basis, BANDS["rough"])
    if len(samples) >= 3:
        ordered = sorted(samples)
        mid = ordered[len(ordered) // 2]
        if mid > 0:
            q1 = ordered[len(ordered) // 4]
            q3 = ordered[(3 * len(ordered)) // 4]
            low = max(0.5, min(low, q1 / mid))
            high = min(3.0, max(high, q3 / mid))
    return low, high


def _run_history(db: Session, agent_id: str | None) -> list[dict[str, Any]]:
    """The measured `cost_actual` of this agent's latest runs (any project's view:
    the agent id already scopes them)."""
    if not agent_id:
        return []
    with project_context(None):
        rows = db.scalars(
            select(EvalRun.cost_actual).where(
                EvalRun.agent_id == agent_id, EvalRun.cost_actual.isnot(None)
            ).order_by(EvalRun.created_at.desc()).limit(HISTORY_RUNS)
        ).all()
    # a live (unfinished) record and a record the estimate itself filled in are not
    # measurements: sampling them would feed the estimate its own guesses
    return [r for r in rows if isinstance(r, dict) and r.get("final")
            and r.get("source") != "estimate_fallback"]


def _rough_per_session(spec: dict[str, Any], items: list[dict[str, Any]]) -> float | None:
    price = _price_of((spec or {}).get("model_id"))
    if not price:
        return None
    rate = (float(price.get("input", 0.0)) + float(price.get("output", 0.0))) / 2
    return _avg_turns(items) * ROUGH_TOKENS_PER_TURN * rate / 1e6


def _avg_turns(items: list[dict[str, Any]]) -> float:
    turns = [len(i.get("turns") or []) or 1 for i in items] or [1]
    return sum(turns) / len(turns)


def _judge_evaluators(evaluators: list[str], rows: list[Criterion]) -> list[dict[str, Any]]:
    """Which of the run's evaluators are LLM judges, who bills them, at what level."""
    from app.evaluation.agentcore_eval import ALL_BUILTIN_EVALUATORS

    kinds: dict[str, str] = {}
    levels: dict[str, str] = {}
    for row in rows:
        executor = row.executor or {}
        if executor.get("kind") == "evaluator" and executor.get("evaluator_id"):
            kinds[executor["evaluator_id"]] = executor.get("evaluator_kind") or "judge"
            if row.level:
                levels[executor["evaluator_id"]] = str(row.level).upper()
    out = []
    for evaluator_id in evaluators:
        managed = is_managed(evaluator_id)
        kind = kinds.get(evaluator_id) or "judge"
        if evaluator_id.startswith("Builtin.Trajectory") or kind in ("code", "trajectory"):
            continue
        level = ALL_BUILTIN_EVALUATORS.get(evaluator_id) or levels.get(evaluator_id) or "TRACE"
        out.append({"evaluator_id": evaluator_id, "level": level, "billed_by":
                    "agentcore_evaluations" if managed else "your_account"})
    return out


def _judge_samples(db: Session, evaluator_id: str) -> list[float]:
    """Judge tokens per judgement previous runs measured for this evaluator.

    Sampled across every project (a token count is not project data), so a project's
    estimate does not shrink to its own few runs."""
    with project_context(None):
        rows = db.scalars(
            select(EvalRun.cost_actual).where(
                EvalRun.cost_actual.isnot(None)
            ).order_by(EvalRun.created_at.desc()).limit(HISTORY_RUNS)
        ).all()
    samples = []
    for actual in rows:
        per_evaluator = ((actual or {}).get("judge_tokens") or {}).get(evaluator_id)
        if per_evaluator:
            samples.append(float(per_evaluator))
    return samples


def _judge_tokens(db: Session, evaluator_id: str) -> float | None:
    """Median judge tokens per judgement, from what previous runs recorded."""
    return _median(_judge_samples(db, evaluator_id))


def _units_per_session(level: str, turns: float) -> float:
    """Judgements one session yields: one per session, per trace (turn) or tool call."""
    return 1.0 if level == "SESSION" else max(1.0, turns)


def _part(key: str, mid: float | None, band: tuple[float, float], basis: str,
          **extra: Any) -> dict[str, Any]:
    if mid is None:
        return {"key": key, "usd": None, "low": None, "high": None, "basis": basis, **extra}
    return {"key": key, "usd": round(mid, 4), "low": round(mid * band[0], 4),
            "high": round(mid * band[1], 4), "basis": basis, **extra}


def estimate(
    db: Session,
    *,
    agent: Agent | None,
    workspace: Workspace | None,
    workspace_ctx: Any,
    items: list[dict[str, Any]],
    evaluators: list[str],
    repeats: int = 1,
    criteria_rows: list[Criterion] | None = None,
    judge_model_id: str | None = None,
    scope: str = "replay",
    sessions: int | None = None,
    period: str = "run",
) -> dict[str, Any]:
    """The estimate every evaluation entry shows before it is submitted.

    `scope` is `replay` (dataset items invoked × `repeats`, also the gate), `passive`
    (`sessions` existing sessions judged, nothing invoked) or `online` (`sessions`
    sampled sessions per `period` = day, nothing invoked by the platform)."""
    repeats = max(1, int(repeats or 1))
    if repeats > MAX_REPEATS:
        raise AppError("run.repeats_range", f"repeats is 1..{MAX_REPEATS}")
    invoked = scope == "replay"
    if invoked:
        n_sessions = len(items) * repeats
    else:
        n_sessions = max(0, int(sessions or 0))
    prices = eval_prices()
    agent_id = getattr(agent, "id", None)
    history = _run_history(db, agent_id) if agent is not None else []
    unpriced_models: list[str] = []
    turns = _avg_turns(items) if items else ROUGH_TRACES_PER_SESSION

    # ── the agent under test ──
    basis = "n/a"
    per_session = None
    agent_samples: list[float] = []
    if not invoked:
        basis = "not_invoked"
    elif agent is not None and items:
        per_session, basis = _per_session_from_history(db, agent, workspace_ctx)
        if per_session is None:
            agent_samples = [float(h["agent_usd_per_session"]) for h in history
                             if h.get("agent_usd_per_session") is not None]
            per_session = _median(agent_samples)
            basis = "eval_runs" if per_session is not None else basis
        if per_session is None:
            per_session = _rough_per_session(getattr(agent, "spec", None) or {}, items)
            basis = "rough" if per_session is not None else "unpriced"
            if per_session is None:
                model = (getattr(agent, "spec", None) or {}).get("model_id")
                if model:
                    unpriced_models.append(model)
    agent_usd = round(per_session * n_sessions, 4) if per_session is not None else None
    if not invoked:
        agent_usd = 0.0
    agent_part = _part("agent", agent_usd, _band(agent_samples, basis), basis,
                       per_session_usd=per_session, samples=len(agent_samples))

    # ── runtime compute (invoked sessions only) ──
    duration_samples = [float(h["runtime_seconds_per_session"]) for h in history
                        if h.get("runtime_seconds_per_session")]
    if invoked and n_sessions:
        seconds_per_session = _median(duration_samples)
        runtime_basis = "eval_runs" if seconds_per_session is not None else "rough"
        if seconds_per_session is None:
            seconds_per_session = turns * ROUGH_SECONDS_PER_TURN
        runtime_seconds = seconds_per_session * n_sessions
        rt_usd = runtime_usd(runtime_seconds, prices)
    else:
        runtime_basis, runtime_seconds, rt_usd = "not_invoked", 0.0, 0.0
    runtime_part = _part("runtime", rt_usd, _band(duration_samples, runtime_basis),
                         runtime_basis, seconds=round(runtime_seconds, 1))

    # ── LLM judges ──
    judges = _judge_evaluators(evaluators, criteria_rows or [])
    judge_model = judge_model_id or _default_judge_model()
    judge_price = _price_of(judge_model)
    judge_total = managed_total = 0.0
    priced_judges = priced_managed = 0
    judge_lo = judge_hi = managed_lo = managed_hi = 0.0
    judge_samples_n: dict[str, int] = {}
    details = []
    for judge in judges:
        samples = _judge_samples(db, judge["evaluator_id"])
        judge_samples_n[judge["evaluator_id"]] = len(samples)
        measured = _median(samples)
        tokens = measured or ROUGH_JUDGE_TOKENS
        band = _band(samples, "measured" if measured else "rough")
        judgements = n_sessions * _units_per_session(judge["level"], turns)
        tin, tout = split_judge_tokens(tokens * judgements)
        note = "estimated from previous runs" if measured else "rough token budget"
        if judge["billed_by"] == "agentcore_evaluations":
            usd = managed_usd(tin, tout, prices)
            managed_total += usd
            managed_lo += usd * band[0]
            managed_hi += usd * band[1]
            priced_managed += 1
            details.append({**judge, "usd": round(usd, 4), "tokens_per_item": tokens,
                            "judgements": judgements, "samples": len(samples),
                            "note": f"AgentCore Evaluations price · {note}"})
            continue
        if not judge_price:
            if judge_model and judge_model not in unpriced_models:
                unpriced_models.append(judge_model)
            details.append({**judge, "usd": None, "model": judge_model,
                            "note": "judge model is not in the price map"})
            continue
        usd = judge_usd(tin, tout, judge_price)
        judge_total += usd
        judge_lo += usd * band[0]
        judge_hi += usd * band[1]
        priced_judges += 1
        details.append({**judge, "usd": round(usd, 4), "model": judge_model,
                        "tokens_per_item": tokens, "judgements": judgements,
                        "samples": len(samples), "note": note})
    judge_part = {"key": "judge", "usd": round(judge_total, 4) if priced_judges else None,
                  "low": round(judge_lo, 4), "high": round(judge_hi, 4),
                  "basis": "judges", "model": judge_model}
    managed_part = {"key": "managed", "usd": round(managed_total, 4) if priced_managed else None,
                    "low": round(managed_lo, 4), "high": round(managed_hi, 4),
                    "basis": "agentcore_evaluations"}

    parts = [agent_part, runtime_part, judge_part, managed_part]
    priced_any = agent_usd is not None or priced_judges or priced_managed or rt_usd > 0
    total = sum(p["usd"] or 0.0 for p in parts)
    low = sum(p["low"] or 0.0 for p in parts)
    high = sum(p["high"] or 0.0 for p in parts)
    limits = _policy(db, workspace, getattr(agent, "project_id", None))
    confirm_usd, max_usd = limits["confirm_usd"], limits["max_usd"]
    concurrency = max(1, get_settings().eval_max_concurrent_runs or 1)
    minutes = (round(n_sessions * 1.5 / concurrency) + 5 if invoked  # replay + wait + batch
               else 5 + round(n_sessions / 20))
    prices_meta = get_settings().model_prices_meta or {}
    return {
        "scope": scope,
        "period": period,
        "items": len(items),
        "repeats": repeats,
        "sessions": n_sessions,
        "agent_usd": agent_usd,
        "agent_basis": basis,
        "runtime_usd": round(rt_usd, 4),
        "runtime_seconds": round(runtime_seconds, 1),
        "runtime_basis": runtime_basis,
        "judge_usd": round(judge_total, 4) if priced_judges else None,
        "managed_usd": round(managed_total, 4) if priced_managed else None,
        "judges": details,
        "components": parts,
        "total_usd": round(total, 4) if priced_any else None,
        "range": ({"low": round(low, 4), "mid": round(total, 4), "high": round(high, 4)}
                  if priced_any else None),
        "basis": {
            "agent_samples": len(agent_samples),
            "duration_samples": len(duration_samples),
            "judge_samples": judge_samples_n,
            "prices": {"source": prices.get("source"), "updated_at": prices.get("updated_at")},
            "model_prices_updated_at": prices_meta.get("updated_at"),
        },
        "unpriced_models": unpriced_models,
        "estimate": True,
        "duration_minutes": minutes,
        "confirm_required": bool(confirm_usd is not None and total > confirm_usd),
        "confirm_usd": confirm_usd,
        "over_limit": bool(max_usd is not None and total > max_usd),
        "max_usd": max_usd,
        # whose limit `max_usd` is: `workspace` (the release policy), or None
        "max_source": limits["max_source"],
        # zero sessions cost nothing: "unpriced" means a model without a price
        "unpriced": invoked and bool(items) and agent_usd is None,
    }


def window_sessions(
    db: Session, agent: Agent | None, workspace_ctx: Any, *, hours: float,
) -> int:
    """How many sessions a look-back window over the agent's traffic holds, from its
    last 7 days (0 when there is no history to scale)."""
    if agent is None:
        return 0
    try:
        from app.services import costs as cost_service

        report = cost_service.cost_report(db, workspace_ctx, range_key="7d")
    except Exception as exc:  # noqa: BLE001 - estimation never blocks the wizard
        logger.debug("cost history unavailable: %s", exc)
        return 0
    week = sum(int(r.get("sessions") or 0) for r in report.get("by_agent") or []
               if r.get("agent_id") == agent.id)
    return round(week * min(hours, 168) / 168)


def online_sessions_per_day(
    db: Session, agent: Agent | None, workspace_ctx: Any, *, sampling_percentage: float,
) -> int:
    """Sessions an online evaluation would judge per day at this sampling rate."""
    daily = window_sessions(db, agent, workspace_ctx, hours=24)
    return round(daily * max(0.0, min(100.0, sampling_percentage)) / 100)


def _default_judge_model() -> str:
    from app.evaluation.agentcore_eval import judge_default_model_id

    return judge_default_model_id()


def assert_allowed(
    estimate_out: dict[str, Any], *, confirmed: bool, is_admin: bool
) -> None:
    """Refuse a run the workspace's limits do not allow (the UI asks first)."""
    if not estimate_out:
        return
    if estimate_out.get("over_limit") and not is_admin:
        raise AppError(
            "run.cost_over_limit",
            f"the estimated cost (${estimate_out['total_usd']}) exceeds this workspace's "
            f"limit (${estimate_out['max_usd']}) — an administrator can override it",
            {"estimate": estimate_out}, status_code=409,
        )
    if estimate_out.get("confirm_required") and not confirmed:
        raise AppError(
            "run.cost_confirm_required",
            f"this run is estimated at ${estimate_out['total_usd']} "
            f"({estimate_out['sessions']} sessions) — confirm to submit it",
            {"estimate": estimate_out}, status_code=409,
        )


def actual_cost(
    db: Session, run: EvalRun, workspace_ctx: Any, metrics: dict[str, dict] | None = None
) -> dict[str, Any]:
    """What the run really cost (`app.evaluation.run_cost.compute`)."""
    from app.evaluation import run_cost

    return run_cost.compute(db, run, workspace_ctx, metrics=metrics)
