"""Watch: scheduled re-evaluation and drift triage (§7.7).

A model can be updated under an agent without a single line changing, so a standard
that is only applied at release time stops being a standard. A watch config re-runs the
regression (or holdout) split on a schedule and keeps a per-dimension time series next
to the online scores, with three separate alert families:

* **count** — red-line violations and errors: an absolute threshold, page immediately;
* **score** — against a rolling median baseline, quiet until the baseline has ≥8 days,
  reported as a digest;
* **distribution** — intent mix via total variation distance, naming the component that
  moved ("returns 8% → 21%").

"No alert" is itself an output, so a dead monitor cannot read as fourteen quiet days.
Drift triage separates agent regression from judge drift by re-running the frozen
calibration set.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.dlc import criteria as criteria_svc
from app.dlc import golden as golden_svc
from app.evaluation import stats
from app.evaluation.models import EvalRun
from app.models.dlc import WatchConfig
from app.models.ledger import Agent

logger = logging.getLogger(__name__)

# the rolling baseline looks back this many runs and will not judge a drop until it has
# that many: a baseline built from two points is noise wearing a baseline's name
BASELINE_WINDOW = 8
BASELINE_MIN_POINTS = 8
SCORE_DROP_PP = 0.05
TVD_ALERT = 0.10
MAX_REPEATS = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def next_due(config: WatchConfig, *, after: datetime | None = None) -> datetime:
    base = after or _now()
    step = timedelta(days=7 if config.every == "weekly" else 1)
    due = base.replace(hour=config.at_hour or 3, minute=0, second=0, microsecond=0)
    while due <= base:
        due += step
    return due


def upsert(
    db: Session,
    *,
    workspace_id: str,
    agent_id: str,
    criteria_set_id: str | None,
    dataset_id: str | None,
    every: str,
    at_hour: int,
    tz: str,
    repeats: int,
    enabled: bool,
    actor: str,
    max_cost_usd: float | None = None,
) -> WatchConfig:
    if every not in ("daily", "weekly"):
        raise AppError("watch.bad_schedule", "every must be daily or weekly")
    if not 0 <= at_hour <= 23:
        raise AppError("watch.bad_hour", "at_hour is 0..23")
    if not 1 <= repeats <= MAX_REPEATS:
        raise AppError("watch.bad_repeats", f"repeats is 1..{MAX_REPEATS}")
    row = db.scalar(
        select(WatchConfig).where(
            WatchConfig.workspace_id == workspace_id, WatchConfig.agent_id == agent_id
        )
    )
    if row is None:
        row = WatchConfig(workspace_id=workspace_id, agent_id=agent_id, created_by=actor)
        db.add(row)
    row.criteria_set_id = criteria_set_id
    row.dataset_id = dataset_id
    row.every = every
    row.at_hour = at_hour
    row.tz = tz or "UTC"
    row.repeats = repeats
    row.enabled = enabled
    row.max_cost_usd = max_cost_usd
    row.next_due_at = next_due(row)
    db.flush()
    return row


def get_for_agent(db: Session, workspace_id: str, agent_id: str) -> WatchConfig | None:
    return db.scalar(
        select(WatchConfig).where(
            WatchConfig.workspace_id == workspace_id, WatchConfig.agent_id == agent_id
        )
    )


def due_configs(db: Session, *, now: datetime | None = None, limit: int = 20) -> list:
    now = now or _now()
    rows = db.scalars(
        select(WatchConfig).where(WatchConfig.enabled.is_(True)).limit(200)
    ).all()
    return [
        r for r in rows
        if r.next_due_at is None or (_aware(r.next_due_at) or now) <= now
    ][:limit]


def config_out(row: WatchConfig) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "criteria_set_id": row.criteria_set_id,
        "dataset_id": row.dataset_id,
        "every": row.every,
        "at_hour": row.at_hour,
        "tz": row.tz,
        "repeats": row.repeats,
        "max_cost_usd": row.max_cost_usd,
        "enabled": row.enabled,
        "last_run_id": row.last_run_id,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "next_due_at": row.next_due_at.isoformat() if row.next_due_at else None,
        "last_status": row.last_status,
        "last_detail": row.last_detail,
    }


# ── running ────────────────────────────────────────────────────────────────────


def run_now(
    db: Session,
    config: WatchConfig,
    *,
    workspace_ctx: Any,
    actor: str = "scheduler",
    submit: Any = None,
    split: str = "regression",
) -> str:
    """Queue the scheduled re-evaluation; returns the run id."""
    agent = db.get(Agent, config.agent_id)
    if agent is None or agent.status != "active":
        raise AppError("watch.agent_inactive", "the agent is not active", status_code=409)
    cset = (
        db.get(criteria_svc.CriteriaSet, config.criteria_set_id)
        if config.criteria_set_id else criteria_svc.agent_set(db, config.workspace_id, agent.id)
    )
    if cset is None:
        raise AppError("watch.no_criteria", "the agent has no published criteria set",
                       status_code=409)
    rows = criteria_svc.criteria_of(db, cset.id)
    evaluators = sorted({
        (r.executor or {}).get("evaluator_id") for r in rows
        if (r.executor or {}).get("kind") == "evaluator" and (r.executor or {}).get("evaluator_id")
    })

    # `get_parent` is the workspace check: a pinned dataset id is never trusted on
    # its own, however it got onto the config row
    parent = (
        golden_svc.get_parent(db, config.workspace_id, config.dataset_id)
        if config.dataset_id
        else golden_svc.for_criteria(db, config.workspace_id, cset.lineage_id)
    )
    if parent is None:
        raise AppError("watch.no_golden", "no golden set for these criteria", status_code=409)
    splits = golden_svc.splits_of(db, parent)
    target = splits.get(split)
    items = golden_svc.active_items(target) if target else []
    if not items:
        raise AppError("watch.empty_split", f"the {split} split has no active items",
                       status_code=409)
    if submit is None:
        from app.evaluation import service as eval_service

        submit = eval_service.submit_run
    # cost ceiling: a scheduled run that would exceed it alerts instead of spending
    if config.max_cost_usd is not None:
        from app.dlc import cost as cost_svc
        from app.models.ledger import Workspace

        estimate = cost_svc.estimate(
            db, agent=agent, workspace=db.get(Workspace, config.workspace_id),
            workspace_ctx=workspace_ctx, items=items, evaluators=evaluators,
            repeats=config.repeats or 1, criteria_rows=rows,
        )
        if (estimate.get("total_usd") or 0) > config.max_cost_usd:
            config.last_status = "skipped_cost"
            config.last_detail = (
                f"estimated ${estimate['total_usd']} exceeds the configured ceiling "
                f"${config.max_cost_usd}"
            )
            config.last_checked_at = _now()
            config.next_due_at = next_due(config)
            db.flush()
            raise AppError("watch.over_cost_ceiling", config.last_detail,
                           {"estimate": estimate}, status_code=409)
    run = submit(
        agent=agent,
        workspace=workspace_ctx,
        dataset_items=items,
        dataset_id=target.id,
        dataset_name=target.name,
        evaluators=evaluators,
        name=f"watch {split} {_now():%Y-%m-%d}"[:64],
        description=f"scheduled re-evaluation ({actor})",
        repeats=config.repeats or 1,
        criteria_set_id=cset.id,
        criteria_set_version=cset.version,
        split=split,
        agent_version=agent.version,
    )
    config.last_run_id = run.id
    config.last_checked_at = _now()
    config.last_status = "queued"
    config.last_detail = f"run {run.id}"
    config.next_due_at = next_due(config)
    db.flush()
    return run.id


# ── signals ────────────────────────────────────────────────────────────────────


def history(db: Session, workspace_id: str, agent_id: str, *, limit: int = 30) -> list[EvalRun]:
    return list(reversed(db.scalars(
        select(EvalRun).where(
            EvalRun.workspace_id == workspace_id,
            EvalRun.agent_id == agent_id,
            EvalRun.criteria_set_id.isnot(None),
            EvalRun.status == "completed",
        ).order_by(EvalRun.created_at.desc()).limit(limit)
    ).all()))


def dimension_series(runs: list[EvalRun]) -> dict[str, list[dict[str, Any]]]:
    """Per-dimension pass rate over time, with the rolling-median baseline band."""
    series: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        summary = (run.criteria_summary or {}).get("criteria") or {}
        per_dimension: dict[str, list[float]] = {}
        for entry in summary.values():
            if entry.get("kind") == "metric" or entry.get("rate") is None:
                continue
            per_dimension.setdefault(entry.get("dimension") or "quality", []).append(
                entry["rate"]
            )
        for dimension, rates in per_dimension.items():
            series.setdefault(dimension, []).append({
                "run_id": run.id,
                "at": run.created_at.isoformat() if run.created_at else None,
                "rate": sum(rates) / len(rates),
                "agent_version": run.agent_version,
                "criteria_set_version": run.criteria_set_version,
            })
    for points in series.values():
        for index, point in enumerate(points):
            window = [p["rate"] for p in points[max(0, index - BASELINE_WINDOW):index]]
            point["baseline"] = stats.rolling_median(window) if window else None
            point["baseline_points"] = len(window)
    return series


def alerts(
    runs: list[EvalRun],
    *,
    intents_before: dict[str, float] | None = None,
    intents_after: dict[str, float] | None = None,
) -> dict[str, Any]:
    """The three families, each judged its own way; silence is reported too."""
    count_alerts: list[dict[str, Any]] = []
    score_alerts: list[dict[str, Any]] = []
    distribution: list[dict[str, Any]] = []
    latest = runs[-1] if runs else None
    if latest is not None:
        summary = (latest.criteria_summary or {}).get("criteria") or {}
        for key, entry in summary.items():
            if entry.get("tier") == "redline" and (entry.get("fail") or 0) > 0:
                count_alerts.append({
                    "family": "count", "criterion_key": key, "severity": "page",
                    "detail": f"{entry['fail']} red-line violation(s)",
                })
            if (entry.get("error") or 0) > 0:
                count_alerts.append({
                    "family": "count", "criterion_key": key, "severity": "page",
                    "detail": f"{entry['error']} evaluation error(s)",
                })
    series = dimension_series(runs)
    for dimension, points in series.items():
        if not points:
            continue
        point = points[-1]
        if point["baseline"] is None or point["baseline_points"] < BASELINE_MIN_POINTS:
            continue
        drop = point["baseline"] - point["rate"]
        if drop >= SCORE_DROP_PP:
            score_alerts.append({
                "family": "score", "dimension": dimension, "severity": "digest",
                "detail": f"{dimension} is {drop:.1%} below its rolling median",
                "rate": point["rate"], "baseline": point["baseline"],
            })
    if intents_before and intents_after:
        tvd = stats.total_variation_distance(intents_before, intents_after)
        if tvd >= TVD_ALERT:
            moved = stats.largest_shift(intents_before, intents_after)
            distribution.append({
                "family": "distribution", "severity": "digest", "tvd": round(tvd, 4),
                "detail": (f"{moved[0]} moved {moved[1]:.0%} → {moved[2]:.0%}"
                           if moved else "intent mix changed"),
            })
    firing = count_alerts + score_alerts + distribution
    return {
        "checked_at": _now().isoformat(),
        "runs_considered": len(runs),
        "count": count_alerts,
        "score": score_alerts,
        "distribution": distribution,
        "firing": len(firing),
        # a monitor that reports nothing when nothing is wrong is indistinguishable
        # from a monitor that has stopped — so quiet is an explicit answer
        "quiet": not firing,
        "baseline_ready": {
            dimension: (points[-1]["baseline_points"] >= BASELINE_MIN_POINTS)
            for dimension, points in series.items() if points
        },
    }


def drift_triage(
    db: Session, workspace_id: str, agent_id: str, *, runs: list[EvalRun] | None = None
) -> dict[str, Any]:
    """Agent regression or judge drift? The frozen calibration set answers it."""
    from app.dlc import calibration as cal

    runs = runs if runs is not None else history(db, workspace_id, agent_id)
    cset = criteria_svc.agent_set(db, workspace_id, agent_id)
    rows = criteria_svc.criteria_of(db, cset.id) if cset else []
    from app.models.ledger import Workspace

    workspace = db.get(Workspace, workspace_id)
    policy = cal.policy_of(workspace.release_policy if workspace else None)
    judges = []
    for row in rows:
        if not criteria_svc.is_judge(row):
            continue
        record = cal.latest_record(db, workspace_id, agent_id, row.key,
                                  (row.executor or {}).get("evaluator_id"))
        status = cal.status_of(record, policy)
        judges.append({
            "criterion_key": row.key,
            "evaluator_id": (row.executor or {}).get("evaluator_id"),
            "calibrated": status.get("calibrated"),
            "reason": status.get("reason"),
            "judge_human_kappa": record.judge_human_kappa if record else None,
            "task_id": record.task_id if record else None,
            "recalibrate": not status.get("calibrated"),
        })
    model_changed = False
    versions = [r.agent_version for r in runs if r.agent_version]
    if len(set(versions)) > 1:
        model_changed = True
    return {
        "judges": judges,
        "needs_recalibration": [j["criterion_key"] for j in judges if j["recalibrate"]],
        "agent_versions_in_window": sorted(set(versions)),
        "versions_changed": model_changed,
        "hint": (
            "re-run the frozen calibration set: if the judge now disagrees with the human "
            "labels, this is judge drift, not an agent regression"
        ),
    }


def view(db: Session, workspace_id: str, agent_id: str) -> dict[str, Any]:
    """Everything the drift bench shows for one agent."""
    config = get_for_agent(db, workspace_id, agent_id)
    runs = history(db, workspace_id, agent_id)
    return {
        "config": config_out(config) if config else None,
        "series": dimension_series(runs),
        "alerts": alerts(runs),
        "drift": drift_triage(db, workspace_id, agent_id, runs=runs),
        "runs": [
            {"id": r.id, "at": r.created_at.isoformat() if r.created_at else None,
             "split": r.split, "agent_version": r.agent_version,
             "criteria_set_version": r.criteria_set_version}
            for r in runs
        ],
    }
