"""Criterion verdicts for a completed run (docs/agent-dlc-design.md §5.1–5.3).

When a run tied to a criteria-set version completes, `finalize_run` reads its
results stream once, maps every record to the criterion whose evaluator produced it,
turns each into a verdict (pass / fail / inconclusive / error) and persists the lot as
`CriterionResult` rows. The per-criterion summary — n, rate, Wilson interval, pass^k,
undetermined rate — and the denominator accounting are cached on the run.

Verdict rules:

* a categorical label goes through the criterion's `label_map` (unmapped ⇒
  inconclusive); a numeric value goes through `score_rule` (default: polarity-normal
  value ≥ 0.5 passes);
* a judge error or a missing target is `error` — never a pass;
* metric criteria are computed from the run's own spans (latency, tokens, cost);
* `denominator = sessions` collapses a session's units: any fail ⇒ fail, else any
  error ⇒ error, else all pass ⇒ pass, else inconclusive.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.dlc import criteria as criteria_svc
from app.evaluation import agentcore_eval as ac
from app.evaluation import stats
from app.evaluation.models import EvalRun
from app.models.dlc import Criterion, CriterionResult

logger = logging.getLogger(__name__)

INCONCLUSIVE_LABELS = {"inconclusive", "n/a", "na", "not applicable", "not_applicable"}
PASS_LABELS = {"pass", "passed", "yes", "true", "ok", "compliant", "correct"}
FAIL_LABELS = {"fail", "failed", "no", "false", "violation", "non-compliant", "incorrect"}
UNDETERMINED_INVALID = 0.05
UNDETERMINED_ADVISE = 0.02
SNAPSHOT_MAX_RECORDS = 50000

# session_id → {latency_ms, tokens, cost_usd}; injected by the caller
MetricsProvider = Callable[[EvalRun, list[str]], dict[str, dict[str, float | None]]]


def record_evaluator_id(attrs: dict[str, Any]) -> str:
    arn = attrs.get("aws.bedrock_agentcore.evaluator.arn")
    if isinstance(arn, str) and "/" in arn:
        return arn.rsplit("/", 1)[-1]
    return str(attrs.get("gen_ai.evaluation.name") or "")


def item_verdict(
    criterion: Criterion | dict[str, Any], record: dict[str, Any]
) -> tuple[str, str | None]:
    """One results-stream record → (verdict, error_code)."""
    if record.get("error_type") or record.get("error_message"):
        return "error", str(record.get("error_type") or "evaluator_error")[:64]
    executor = (
        criterion.executor if isinstance(criterion, Criterion) else criterion.get("executor")
    ) or {}
    label = record.get("label")
    label_map = executor.get("label_map") or {}
    if label_map and label is not None:
        text = str(label).strip().lower()
        for verdict in ("pass", "fail", "inconclusive"):
            if text in {str(v).strip().lower() for v in label_map.get(verdict) or []}:
                return verdict, None
        return "inconclusive", None
    if label is not None and str(label).strip().lower() in INCONCLUSIVE_LABELS:
        return "inconclusive", None
    value = record.get("score")
    if value is None:
        if label is not None:
            text = str(label).strip().lower()
            if text in PASS_LABELS:
                return "pass", None
            if text in FAIL_LABELS:
                return "fail", None
        return "inconclusive", None
    rule = executor.get("score_rule") or {}
    op = rule.get("op", ">=")
    bound = float(rule.get("value", 0.5))
    oriented = float(value)
    if ac.evaluator_polarity(executor.get("evaluator_id") or "") < 0 and not rule:
        oriented = 1.0 - oriented  # a penalty score: lower is better
    passed = oriented >= bound if op == ">=" else oriented <= bound
    return ("pass" if passed else "fail"), None


def collapse(verdicts: list[str]) -> str:
    """Session verdict from its units' verdicts."""
    if not verdicts:
        return "error"
    if "fail" in verdicts:
        return "fail"
    if "error" in verdicts:
        return "error"
    if all(v == "pass" for v in verdicts):
        return "pass"
    return "inconclusive"


def _attempt_index(run: EvalRun) -> dict[str, tuple[str, int]]:
    """session_id → (scenario_id, attempt). Older runs pair sessions by position."""
    out: dict[str, tuple[str, int]] = {}
    for entry in run.attempts or []:
        sid = entry.get("session_id")
        if sid:
            out[str(sid)] = (
                str(entry.get("scenario_id") or ""), int(entry.get("attempt") or 1)
            )
    if not out:
        for index, sid in enumerate(run.session_ids or []):
            out[str(sid)] = (f"#{index + 1}", 1)
    return out


def _metric_verdict(rule: dict[str, Any], value: float | None) -> str:
    if value is None:
        return "error"
    bound = float(rule.get("value", 0))
    within = value <= bound if rule.get("op", "<=") == "<=" else value >= bound
    return "pass" if within else "fail"


def build_results(
    run: EvalRun,
    rows: list[Criterion],
    records: list[dict[str, Any]],
    metrics: dict[str, dict[str, float | None]] | None = None,
) -> list[dict[str, Any]]:
    """The verdict rows for a run (pure; `finalize_run` persists them)."""
    index = _attempt_index(run)
    by_evaluator: dict[str, list[Criterion]] = defaultdict(list)
    for row in rows:
        executor = row.executor or {}
        if executor.get("kind") == "evaluator" and executor.get("evaluator_id"):
            by_evaluator[executor["evaluator_id"]].append(row)
    out: list[dict[str, Any]] = []
    for attrs in records:
        sid = str(attrs.get("session.id") or "")
        evaluator_id = record_evaluator_id(attrs)
        if not sid or not evaluator_id:
            continue
        normalized = ac.normalize_result_record(attrs)
        scenario_id, attempt = index.get(sid, ("", 1))
        unit = str(attrs.get("span.id") or attrs.get("trace.id") or attrs.get("trace_id") or "")
        for row in by_evaluator.get(evaluator_id, []):
            verdict, code = item_verdict(row, normalized)
            out.append({
                "criterion_key": row.key,
                "scenario_id": scenario_id,
                "attempt": attempt,
                "session_id": sid,
                "evaluator_id": evaluator_id,
                "level": str(normalized.get("level") or ""),
                "unit_ref": unit[:160],
                "raw_value": normalized.get("score"),
                "raw_label": (str(normalized["label"])[:64] if normalized.get("label") is not None
                              else None),
                "explanation": str(normalized.get("explanation") or "")[:4000],
                "verdict": verdict,
                "error_code": code,
            })
    if metrics is not None:
        for row in rows:
            executor = row.executor or {}
            if executor.get("kind") != "metric":
                continue
            rule = row.metric_rule or {}
            metric = rule.get("metric")
            field = {
                "latency_p95_ms": "latency_ms",
                "tokens_per_session": "tokens",
                "cost_per_success_usd": "cost_usd",
            }.get(str(metric))
            for sid, (scenario_id, attempt) in index.items():
                value = (metrics.get(sid) or {}).get(field) if field else None
                out.append({
                    "criterion_key": row.key,
                    "scenario_id": scenario_id,
                    "attempt": attempt,
                    "session_id": sid,
                    "evaluator_id": f"metric:{metric}",
                    "level": "session",
                    "unit_ref": "",
                    "raw_value": value,
                    "raw_label": None,
                    "explanation": "",
                    # the per-session value is recorded; the gate judges the aggregate
                    "verdict": "pass" if value is not None else "error",
                    "error_code": None if value is not None else "metric_missing",
                })
    return out


def _percentile(values: list[float], q: float) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    pos = (len(clean) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(clean) - 1)
    return clean[lo] + (clean[hi] - clean[lo]) * (pos - lo)


def summarize(
    run: EvalRun, rows: list[Criterion], results: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(criteria_summary, denominator) for a run's verdict rows."""
    index = _attempt_index(run)
    expected_sessions = len(index)
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_key[r["criterion_key"]].append(r)
    summary: dict[str, Any] = {}
    denominator = {
        "expected_items": expected_sessions,
        "invoked": len(run.session_ids or []),
        "evaluated": 0,
        "inconclusive": 0,
        "errored": 0,
        "guardrail_blocked": 0,
        "missing": 0,
    }
    for row in rows:
        items = by_key.get(row.key, [])
        executor = row.executor or {}
        entry: dict[str, Any] = {
            "key": row.key, "dimension": row.dimension, "tier": row.tier,
            "kind": executor.get("kind"),
        }
        if executor.get("kind") == "metric":
            rule = row.metric_rule or {}
            values = [i["raw_value"] for i in items if i["raw_value"] is not None]
            metric = str(rule.get("metric"))
            if metric == "cost_per_success_usd":
                aggregate = (sum(values) / len(values)) if values else None
            elif metric == "tokens_per_session":
                aggregate = (sum(values) / len(values)) if values else None
            else:
                aggregate = _percentile(values, 0.95)
            entry.update({
                "n": len(values), "expected": expected_sessions, "value": aggregate,
                "rule": rule,
                "verdict": _metric_verdict(rule, aggregate) if values else "error",
                "missing": expected_sessions - len(values),
            })
            summary[row.key] = entry
            continue
        if not items:
            entry.update({"n": 0, "expected": expected_sessions, "pass": 0, "fail": 0,
                          "inconclusive": 0, "error": 0, "rate": None, "missing":
                          expected_sessions, "undetermined_rate": None})
            summary[row.key] = entry
            continue
        # collapse to the denominator's unit
        units: dict[tuple, list[str]] = defaultdict(list)
        for item in items:
            if row.denominator == "sessions":
                unit_key = (item["session_id"],)
            else:
                unit_key = (item["session_id"], item["unit_ref"], len(units))
            units[unit_key].append(item["verdict"])
        verdicts = {k: collapse(v) for k, v in units.items()}
        counts = {v: 0 for v in ("pass", "fail", "inconclusive", "error")}
        for verdict in verdicts.values():
            counts[verdict] += 1
        n = counts["pass"] + counts["fail"]
        rate = counts["pass"] / n if n else None
        low, high = stats.wilson_interval(counts["pass"], n) if n else (None, None)
        undetermined = counts["inconclusive"] / (n + counts["inconclusive"]) if (
            n + counts["inconclusive"]) else None
        seen_sessions = {item["session_id"] for item in items}
        entry.update({
            **counts, "n": n, "rate": rate, "wilson_low": low, "wilson_high": high,
            "expected": expected_sessions,
            "missing": max(0, expected_sessions - len(seen_sessions)),
            "undetermined_rate": undetermined,
        })
        # pass^k over attempts (session-level verdicts)
        if (run.repeats or 1) > 1:
            per_session = {}
            for item in items:
                per_session.setdefault(item["session_id"], []).append(item["verdict"])
            by_scenario: dict[str, list[bool]] = defaultdict(list)
            for sid, vs in per_session.items():
                scenario_id, _ = index.get(sid, (sid, 1))
                collapsed = collapse(vs)
                if collapsed in ("pass", "fail"):
                    by_scenario[scenario_id].append(collapsed == "pass")
            mode = (row.pass_k or {}).get("mode") or run.repeat_mode or "all"
            entry["pass_k"] = stats.pass_k(by_scenario, mode=mode)
        summary[row.key] = entry
        denominator["evaluated"] = max(denominator["evaluated"], len(seen_sessions))
        denominator["inconclusive"] += counts["inconclusive"]
        denominator["errored"] += counts["error"]
        denominator["missing"] = max(denominator["missing"], entry["missing"])
    return summary, denominator


def snapshot_rows(db: Session, run: EvalRun, results: list[dict[str, Any]]) -> None:
    db.execute(delete(CriterionResult).where(CriterionResult.run_id == run.id))
    for r in results:
        db.add(CriterionResult(workspace_id=run.workspace_id, run_id=run.id, **r))


def read_records(run: EvalRun, workspace: Any) -> list[dict[str, Any]]:
    from app.services.agentcore.client import data_client

    detail = ac.get_batch_evaluation(data_client(workspace), batch_id=run.batch_eval_id)
    location = ac.results_stream(detail)
    if location is None:
        return []
    return ac.read_result_records(
        workspace.client("logs"), *location, max_events=SNAPSHOT_MAX_RECORDS
    )


def finalize_run(
    run_id: str,
    workspace: Any,
    *,
    records: list[dict[str, Any]] | None = None,
    metrics_provider: MetricsProvider | None = None,
) -> dict[str, Any] | None:
    """Snapshot and summarize a completed run's criterion verdicts. Never raises."""
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        if run is None or not run.criteria_set_id or run.status != "completed":
            return None
        rows = criteria_svc.criteria_of(db, run.criteria_set_id)
        try:
            if records is None:
                records = read_records(run, workspace)
            metrics = None
            if any((r.executor or {}).get("kind") == "metric" for r in rows):
                provider = metrics_provider or default_metrics_provider(workspace)
                metrics = provider(run, list(_attempt_index(run)))
            results = build_results(run, rows, records, metrics)
            summary, denominator = summarize(run, rows, results)
            snapshot_rows(db, run, results)
            run.criteria_summary = {"criteria": summary, "records": len(records)}
            run.denominator = denominator
            db.commit()
            return run.criteria_summary
        except Exception as exc:  # noqa: BLE001 - the run itself already completed
            db.rollback()
            logger.warning("criteria snapshot of run %s failed: %s", run_id, exc)
            run = db.get(EvalRun, run_id)
            run.criteria_summary = {"error": f"{type(exc).__name__}: {exc}"[:400]}
            db.commit()
            return None
    finally:
        db.close()


def default_metrics_provider(workspace: Any) -> MetricsProvider:
    """Per-session latency / tokens / cost from the spans the observability views read."""

    def provider(run: EvalRun, session_ids: list[str]) -> dict[str, dict[str, float | None]]:
        from app.services import observability

        return observability.session_metrics_bulk(session_ids, workspace)

    return provider


def results_for(
    db: Session, run_id: str, criterion_key: str | None = None
) -> list[CriterionResult]:
    query = select(CriterionResult).where(CriterionResult.run_id == run_id)
    if criterion_key:
        query = query.where(CriterionResult.criterion_key == criterion_key)
    return list(db.scalars(query.order_by(CriterionResult.scenario_id,
                                          CriterionResult.attempt)).all())


def result_out(row: CriterionResult) -> dict[str, Any]:
    return {
        "criterion_key": row.criterion_key,
        "scenario_id": row.scenario_id,
        "attempt": row.attempt,
        "session_id": row.session_id,
        "evaluator_id": row.evaluator_id,
        "level": row.level,
        "unit_ref": row.unit_ref,
        "raw_value": row.raw_value,
        "raw_label": row.raw_label,
        "explanation": row.explanation,
        "verdict": row.verdict,
        "error_code": row.error_code,
    }
