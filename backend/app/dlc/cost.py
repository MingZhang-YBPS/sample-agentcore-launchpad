"""What an evaluation run will cost, before it is submitted (§5.3).

pass^k multiplies the bill by k, so the task wizard asks for it only with the number in
view. The estimate is deliberately simple and always labelled with its basis:

    items × k × (agent cost per session + Σ judge evaluators × judge cost per item)

* **agent cost per session** — this agent's mean spend per session over the last 7
  days (span token usage × the price map). With no history: the model's price × the
  dataset's average turn count × a default token budget, labelled `rough`.
* **judge cost per item** — the median `tokenUsage` the evaluator's own past results
  recorded × the judge model's price. Builtin / ThirdParty evaluators run on service
  capacity: they are listed as billed by AgentCore Evaluations, with no dollar figure
  the platform can honestly claim.

After the run, `actual_cost` reads the same sources so the estimate can be judged.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.evaluation.models import EvalRun
from app.models.dlc import Criterion
from app.models.ledger import Agent, Workspace

logger = logging.getLogger(__name__)

# a turn with no history to go on: input + output tokens assumed per exchange
ROUGH_TOKENS_PER_TURN = 2500
ROUGH_JUDGE_TOKENS = 1800
DEFAULT_CONFIRM_USD = 5.0
MAX_REPEATS = 10


def _policy(workspace: Workspace | None) -> dict[str, float | None]:
    policy = (workspace.release_policy if workspace else None) or {}
    confirm = policy.get("eval_cost_confirm_usd", DEFAULT_CONFIRM_USD)
    return {
        "confirm_usd": float(confirm) if confirm is not None else None,
        "max_usd": (float(policy["eval_cost_max_usd"])
                    if policy.get("eval_cost_max_usd") is not None else None),
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


def _rough_per_session(spec: dict[str, Any], items: list[dict[str, Any]]) -> float | None:
    price = _price_of((spec or {}).get("model_id"))
    if not price:
        return None
    turns = [len(i.get("turns") or []) or 1 for i in items] or [1]
    per_turn_tokens = ROUGH_TOKENS_PER_TURN
    avg_turns = sum(turns) / len(turns)
    rate = (float(price.get("input", 0.0)) + float(price.get("output", 0.0))) / 2
    return avg_turns * per_turn_tokens * rate / 1e6


def _judge_evaluators(evaluators: list[str], rows: list[Criterion]) -> list[dict[str, Any]]:
    """Which of the run's evaluators are LLM judges, and who bills them."""
    kinds: dict[str, str] = {}
    for row in rows:
        executor = row.executor or {}
        if executor.get("kind") == "evaluator" and executor.get("evaluator_id"):
            kinds[executor["evaluator_id"]] = executor.get("evaluator_kind") or "judge"
    out = []
    for evaluator_id in evaluators:
        managed = evaluator_id.startswith(("Builtin.", "ThirdParty."))
        kind = kinds.get(evaluator_id) or ("judge" if managed else "judge")
        if evaluator_id.startswith("Builtin.Trajectory") or kind in ("code", "trajectory"):
            continue
        out.append({"evaluator_id": evaluator_id, "billed_by":
                    "agentcore_evaluations" if managed else "your_account"})
    return out


def _judge_tokens(db: Session, evaluator_id: str) -> float | None:
    """Median judge tokens per item, from what previous runs recorded."""
    rows = db.scalars(
        select(EvalRun.cost_actual).where(
            EvalRun.cost_actual.isnot(None)
        ).order_by(EvalRun.created_at.desc()).limit(20)
    ).all()
    samples = []
    for actual in rows:
        per_evaluator = ((actual or {}).get("judge_tokens") or {}).get(evaluator_id)
        if per_evaluator:
            samples.append(float(per_evaluator))
    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]


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
) -> dict[str, Any]:
    """The estimate the wizard shows before a run is submitted."""
    repeats = max(1, int(repeats or 1))
    if repeats > MAX_REPEATS:
        raise AppError("run.repeats_range", f"repeats is 1..{MAX_REPEATS}")
    sessions = len(items) * repeats
    basis = "n/a"
    per_session = None
    if agent is not None and items:
        per_session, basis = _per_session_from_history(db, agent, workspace_ctx)
        if per_session is None:
            per_session = _rough_per_session(agent.spec or {}, items)
            basis = "rough" if per_session is not None else "unpriced"
    agent_usd = round(per_session * sessions, 4) if per_session is not None else None

    judges = _judge_evaluators(evaluators, criteria_rows or [])
    judge_price = _price_of(judge_model_id or _default_judge_model())
    judge_usd = 0.0
    priced_judges = 0
    details = []
    for judge in judges:
        tokens = _judge_tokens(db, judge["evaluator_id"]) or ROUGH_JUDGE_TOKENS
        if judge["billed_by"] == "agentcore_evaluations":
            details.append({**judge, "usd": None,
                            "note": "billed by AgentCore Evaluations (service capacity)"})
            continue
        if not judge_price:
            details.append({**judge, "usd": None, "note": "judge model is not in the price map"})
            continue
        rate = (float(judge_price.get("input", 0.0)) + float(judge_price.get("output", 0.0))) / 2
        usd = tokens * rate / 1e6 * sessions
        judge_usd += usd
        priced_judges += 1
        details.append({**judge, "usd": round(usd, 4),
                        "tokens_per_item": tokens,
                        "note": "estimated from previous runs" if _judge_tokens(
                            db, judge["evaluator_id"]) else "rough token budget"})
    total = (agent_usd or 0.0) + judge_usd
    limits = _policy(workspace)
    confirm_usd, max_usd = limits["confirm_usd"], limits["max_usd"]
    concurrency = max(1, get_settings().eval_max_concurrent_runs or 1)
    minutes = round(sessions * 1.5 / concurrency) + 5  # replay + telemetry wait + batch
    return {
        "items": len(items),
        "repeats": repeats,
        "sessions": sessions,
        "agent_usd": agent_usd,
        "agent_basis": basis,
        "judge_usd": round(judge_usd, 4) if priced_judges else None,
        "judges": details,
        "total_usd": round(total, 4) if (agent_usd is not None or priced_judges) else None,
        "estimate": True,
        "duration_minutes": minutes,
        "confirm_required": bool(confirm_usd is not None and total > confirm_usd),
        "confirm_usd": confirm_usd,
        "over_limit": bool(max_usd is not None and total > max_usd),
        "max_usd": max_usd,
        "unpriced": agent_usd is None,
    }


def _default_judge_model() -> str:
    from app.evaluation.agentcore_eval import JUDGE_DEFAULT_MODEL_ID

    return JUDGE_DEFAULT_MODEL_ID


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
    """What the run really cost: agent spend from its sessions, judge tokens from results."""
    out: dict[str, Any] = {"sessions": len(run.session_ids or []), "estimate": True}
    try:
        if metrics is None:
            from app.services import observability

            metrics = observability.session_metrics_bulk(
                list(run.session_ids or []), workspace_ctx
            )
        agent_usd = sum(float(m.get("cost_usd") or 0.0) for m in (metrics or {}).values())
        priced = [m for m in (metrics or {}).values() if m.get("cost_usd") is not None]
        out["agent_usd"] = round(agent_usd, 4) if priced else None
        out["unpriced_sessions"] = len(metrics or {}) - len(priced)
    except Exception as exc:  # noqa: BLE001
        out["agent_error"] = f"{type(exc).__name__}: {exc}"[:200]
    judge_tokens: dict[str, float] = {}
    from app.models.dlc import CriterionResult

    rows = db.scalars(
        select(CriterionResult).where(CriterionResult.run_id == run.id)
    ).all()
    counts: dict[str, int] = {}
    for row in rows:
        if row.evaluator_id.startswith("metric:"):
            continue
        counts[row.evaluator_id] = counts.get(row.evaluator_id, 0) + 1
    for evaluator_id, count in counts.items():
        judge_tokens[evaluator_id] = ROUGH_JUDGE_TOKENS  # per item, pending AWS token usage
        out.setdefault("evaluated_items", {})[evaluator_id] = count
    out["judge_tokens"] = judge_tokens
    estimate_out = run.cost_estimate or {}
    if estimate_out.get("total_usd") is not None and out.get("agent_usd") is not None:
        out["estimate_total_usd"] = estimate_out["total_usd"]
        out["delta_usd"] = round(out["agent_usd"] - estimate_out["total_usd"], 4)
    return out
