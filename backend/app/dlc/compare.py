"""Comparing runs: the fix ladder and the three transition lists (§5.7).

`compare` answers two different questions and never mixes them:

* **did the agent get better** — same criteria version, same golden split version:
  per criterion Δrate with a significance test, and per item the lists **fixed**
  (fail→pass), **new failures** (pass→fail) and **still failing**. "One change at a
  time" is checked, not assumed: when the compared runs differ in more than one
  attribution layer, the report says so.
* **how much harder did the standard get** — criteria or golden versions differ: the
  newer run is also scored under the older version's criteria on the items both share,
  so a pass-rate drop caused by a longer ruler reads as progress, not regression.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.dlc import criteria as criteria_svc
from app.evaluation import stats
from app.evaluation.models import EvalRun
from app.models.dlc import CriterionResult

LAYERS = {
    "01": "structured context",
    "02": "decision rules and thresholds",
    "03": "tools and skills",
    "04": "orchestration and sub-agents",
    "05": "runtime and middleware",
    "06": "model and parameters",
    "07": "permissions and guardrails",
}
SPEC_LAYER = {
    "system_prompt": "01", "knowledge_bases": "01", "memory": "01", "skills": "01",
    "tools": "03", "toolkits": "03", "gateway": "03",
    "max_iterations": "04", "sub_agents": "04",
    "timeout": "05", "max_tokens": "05",
    "model_id": "06",
    "guardrail": "07", "inbound_auth": "07",
}


def _verdicts(db: Session, run_id: str) -> dict[tuple[str, str], str]:
    """(criterion, scenario) → collapsed verdict across attempts and units."""
    rows = db.scalars(
        select(CriterionResult).where(CriterionResult.run_id == run_id)
    ).all()
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        grouped[(row.criterion_key, row.scenario_id)].append(row.verdict)
    from app.dlc.engine import collapse

    return {key: collapse(v) for key, v in grouped.items()}


def _run(db: Session, workspace_id: str, run_id: str) -> EvalRun:
    run = db.get(EvalRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise NotFoundError("run.not_found", f"run {run_id} not found")
    return run


def layers_changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Which of the seven layers two agent specs differ in."""
    changed: set[str] = set()
    for field in set(before or {}) | set(after or {}):
        if (before or {}).get(field) == (after or {}).get(field):
            continue
        layer = SPEC_LAYER.get(field)
        if layer:
            changed.add(layer)
    return sorted(changed)


def compare(
    db: Session, workspace_id: str, run_ids: list[str], *, specs: dict[str, dict] | None = None
) -> dict[str, Any]:
    """The ladder over 2+ runs, in the order given (oldest first)."""
    if len(run_ids) < 2:
        raise AppError("compare.two_runs", "comparing needs at least two runs")
    runs = [_run(db, workspace_id, rid) for rid in run_ids]
    lineages = {r.criteria_set_id for r in runs}
    versions = {r.criteria_set_version for r in runs}
    splits = {r.split for r in runs}
    comparable = len(lineages) == 1 and len(versions) == 1
    rungs: list[dict[str, Any]] = []
    for index, run in enumerate(runs):
        summary = (run.criteria_summary or {}).get("criteria") or {}
        gates = [e for e in summary.values() if e.get("tier") == "gate"]
        redlines = [e for e in summary.values() if e.get("tier") == "redline"]
        rungs.append({
            "run_id": run.id,
            "name": run.name,
            "status": run.status,
            "agent_version": run.agent_version,
            "criteria_set_version": run.criteria_set_version,
            "split": run.split,
            "dataset_version": run.dataset_version,
            "repeats": run.repeats,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "mean_gate_rate": (
                sum(e.get("rate") or 0 for e in gates) / len(gates) if gates else None
            ),
            "redline_violations": sum(e.get("fail") or 0 for e in redlines),
            "cost_actual_usd": (run.cost_actual or {}).get("agent_usd"),
            "layers_changed": (
                layers_changed((specs or {}).get(runs[index - 1].id, {}),
                               (specs or {}).get(run.id, {}))
                if index and specs else []
            ),
        })
    first, last = runs[0], runs[-1]
    criteria_rows = (
        criteria_svc.criteria_of(db, last.criteria_set_id) if last.criteria_set_id else []
    )
    by_key = {row.key: row for row in criteria_rows}
    before_v, after_v = _verdicts(db, first.id), _verdicts(db, last.id)
    per_criterion: list[dict[str, Any]] = []
    fixed: list[dict[str, str]] = []
    new_failures: list[dict[str, str]] = []
    still_failing: list[dict[str, str]] = []
    keys = sorted({k for k, _ in before_v} | {k for k, _ in after_v})
    for key in keys:
        before_items = {s: v for (k, s), v in before_v.items() if k == key}
        after_items = {s: v for (k, s), v in after_v.items() if k == key}
        b_pass = sum(1 for v in before_items.values() if v == "pass")
        b_n = sum(1 for v in before_items.values() if v in ("pass", "fail"))
        a_pass = sum(1 for v in after_items.values() if v == "pass")
        a_n = sum(1 for v in after_items.values() if v in ("pass", "fail"))
        row = by_key.get(key)
        entry = {
            "key": key,
            "dimension": row.dimension if row else None,
            "tier": row.tier if row else None,
            "threshold": row.threshold if row else None,
            "before": {"pass": b_pass, "n": b_n,
                       "rate": (b_pass / b_n) if b_n else None},
            "after": {"pass": a_pass, "n": a_n, "rate": (a_pass / a_n) if a_n else None},
        }
        if b_n and a_n:
            entry["delta"] = entry["after"]["rate"] - entry["before"]["rate"]
            entry["p_value"] = stats.two_proportion_p_value(a_pass, a_n, b_pass, b_n)
            entry["significant"] = bool(
                entry["p_value"] is not None and entry["p_value"] < 0.05
            )
        for scenario, after_verdict in after_items.items():
            before_verdict = before_items.get(scenario)
            item = {"criterion_key": key, "scenario_id": scenario,
                    "before": before_verdict, "after": after_verdict}
            if before_verdict == "fail" and after_verdict == "pass":
                fixed.append(item)
            elif before_verdict == "pass" and after_verdict == "fail":
                new_failures.append(item)
            elif after_verdict == "fail":
                still_failing.append(item)
        per_criterion.append(entry)
    out: dict[str, Any] = {
        "runs": rungs,
        "comparable": comparable,
        "criteria": per_criterion,
        "fixed": fixed,
        "new_failures": new_failures,
        "still_failing": still_failing,
        "splits": sorted(s for s in splits if s),
    }
    if not comparable:
        out["incomparable_reason"] = (
            "the runs used different criteria versions"
            if len(versions) > 1 or len(lineages) > 1
            else "the runs used different golden versions"
        )
        out["two_numbers"] = two_numbers(db, first, last)
    if len(run_ids) > 2 and specs:
        multi = [r for r in rungs[1:] if len(r["layers_changed"]) > 1]
        if multi:
            out["warning"] = (
                "more than one layer changed between runs "
                + ", ".join(r["run_id"] for r in multi)
                + " — a gain cannot be attributed to a single change"
            )
    return out


def two_numbers(db: Session, old_run: EvalRun, new_run: EvalRun) -> dict[str, Any]:
    """Score the newer run under both criteria versions on the items they share.

    "Did the agent get better" is the old standard's number; "how much harder is the
    standard" is the gap to the new standard's number on the same run.
    """
    old_rows = (
        criteria_svc.criteria_of(db, old_run.criteria_set_id) if old_run.criteria_set_id else []
    )
    new_rows = (
        criteria_svc.criteria_of(db, new_run.criteria_set_id) if new_run.criteria_set_id else []
    )
    old_keys = {r.key for r in old_rows if r.tier in ("redline", "gate")}
    new_keys = {r.key for r in new_rows if r.tier in ("redline", "gate")}
    shared = old_keys & new_keys
    new_v = _verdicts(db, new_run.id)
    old_v = _verdicts(db, old_run.id)

    def rate(verdicts: dict[tuple[str, str], str], keys: set[str]) -> tuple[float | None, int]:
        passes = sum(1 for (k, _), v in verdicts.items() if k in keys and v == "pass")
        n = sum(1 for (k, _), v in verdicts.items() if k in keys and v in ("pass", "fail"))
        return ((passes / n) if n else None), n

    under_old, n_old = rate(new_v, shared)
    under_new, n_new = rate(new_v, new_keys)
    baseline, n_base = rate(old_v, shared)
    return {
        "shared_criteria": sorted(shared),
        "added_criteria": sorted(new_keys - old_keys),
        "removed_criteria": sorted(old_keys - new_keys),
        "baseline_under_old": {"rate": baseline, "n": n_base,
                               "criteria_set_version": old_run.criteria_set_version},
        "current_under_old": {"rate": under_old, "n": n_old},
        "current_under_new": {"rate": under_new, "n": n_new,
                              "criteria_set_version": new_run.criteria_set_version},
        "agent_delta": (under_old - baseline) if (under_old is not None
                                                 and baseline is not None) else None,
        "standard_delta": (under_new - under_old) if (under_new is not None
                                                     and under_old is not None) else None,
    }


def ladder(db: Session, workspace_id: str, agent_id: str, *, limit: int = 8) -> list[str]:
    """The newest completed criteria runs of one agent, oldest first — the fix ladder."""
    rows = db.scalars(
        select(EvalRun).where(
            EvalRun.workspace_id == workspace_id,
            EvalRun.agent_id == agent_id,
            EvalRun.criteria_set_id.isnot(None),
            EvalRun.status == "completed",
        ).order_by(EvalRun.created_at.desc()).limit(limit)
    ).all()
    return [r.id for r in reversed(rows)]
