"""The Agent-DLC release gate (docs/agent-dlc-design.md §5.4).

Four gates, in a fixed order — the order matters, a red line must never be averaged
away and an incomplete denominator must never read as a pass:

1. **Red line** — every red-line criterion has zero violations. Not waivable.
2. **Denominator** — every red-line and gate criterion was evaluated on every expected
   item, and the undetermined (inconclusive) share is ≤ 5%. Otherwise INVALID: the
   results support no conclusion, including PASS.
3. **Per-dimension gates** — every criterion whose *effective* tier is gate (a judge only
   once calibrated) clears its threshold (pass rate, or pass^k when declared; metric
   criteria compare their aggregate). No weighted total. An approved, unexpired waiver
   turns a failure into WAIVED.
4. **Observe** — recorded with the trend against the previous release; never blocks.

Provenance checks make the report reproducible: what was evaluated is the candidate,
the criteria version is signed, and nothing in the lineage is stale.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.dlc import criteria as criteria_svc
from app.dlc.engine import UNDETERMINED_INVALID
from app.evaluation import stats
from app.models.dlc import Criterion, Waiver

PASS = "PASS"
BLOCKED = "BLOCKED"
INVALID = "INVALID"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def waiver_active(waiver: Waiver, now: datetime | None = None) -> bool:
    now = now or _now()
    expires = _aware(waiver.expires_on)
    return waiver.status == "approved" and expires is not None and expires > now


def _merge_summaries(summaries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Combine per-criterion entries of several runs (regression + holdout)."""
    merged: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        for key, entry in (summary.get("criteria") or {}).items():
            target = merged.setdefault(key, {"runs": 0})
            target["runs"] += 1
            if entry.get("kind") == "metric":
                values = [v for v in (target.get("values") or []) if v is not None]
                if entry.get("value") is not None:
                    values.append(entry["value"])
                target.update({
                    "kind": "metric", "values": values, "rule": entry.get("rule"),
                    "n": (target.get("n") or 0) + (entry.get("n") or 0),
                    "expected": (target.get("expected") or 0) + (entry.get("expected") or 0),
                    "missing": (target.get("missing") or 0) + (entry.get("missing") or 0),
                })
                continue
            for field in ("pass", "fail", "inconclusive", "error", "n", "expected", "missing"):
                target[field] = (target.get(field) or 0) + (entry.get(field) or 0)
            if entry.get("pass_k"):
                target.setdefault("pass_k_entries", []).append(entry["pass_k"])
    for target in merged.values():
        if target.get("kind") == "metric":
            values = target.get("values") or []
            target["value"] = max(values) if values else None  # worst run decides
            continue
        n = target.get("n") or 0
        target["rate"] = (target.get("pass") or 0) / n if n else None
        low, high = stats.wilson_interval(target.get("pass") or 0, n) if n else (None, None)
        target["wilson_low"], target["wilson_high"] = low, high
        undetermined_base = n + (target.get("inconclusive") or 0)
        target["undetermined_rate"] = (
            (target.get("inconclusive") or 0) / undetermined_base if undetermined_base else None
        )
        entries = target.pop("pass_k_entries", [])
        if entries:
            scenarios = sum(e.get("scenarios") or 0 for e in entries)
            weighted = sum((e.get("pass_k") or 0) * (e.get("scenarios") or 0) for e in entries)
            mean_w = sum((e.get("mean_k") or 0) * (e.get("scenarios") or 0) for e in entries)
            target["pass_k"] = {
                "k": entries[0].get("k"),
                "pass_k": weighted / scenarios if scenarios else None,
                "mean_k": mean_w / scenarios if scenarios else None,
                "scenarios": scenarios,
            }
    return merged


def decide(
    *,
    rows: list[Criterion],
    summaries: list[dict[str, Any]],
    calibration: dict[str, dict[str, Any]],
    waivers: list[Waiver],
    provenance: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The gate report. `summaries` are the runs' `criteria_summary` dicts."""
    now = now or _now()
    merged = _merge_summaries(summaries)
    active_waivers = {w.criterion_key: w for w in waivers if waiver_active(w, now)}
    previous_rows = {r["key"]: r for r in (previous or {}).get("criteria") or []}
    report_rows: list[dict[str, Any]] = []
    redline_violations: list[str] = []
    invalid_reasons: list[str] = []
    gate_failures: list[str] = []
    waived: list[str] = []

    for row in rows:
        entry = merged.get(row.key) or {}
        status = calibration.get(row.key) or {}
        calibrated = bool(status.get("calibrated"))
        effective = criteria_svc.effective_tier(row, calibrated=calibrated)
        executor = row.executor or {}
        out: dict[str, Any] = {
            "key": row.key,
            "text": row.text,
            "dimension": row.dimension,
            "tier": row.tier,
            "effective_tier": effective,
            "is_judge": criteria_svc.is_judge(row),
            "calibrated": calibrated if criteria_svc.is_judge(row) else None,
            "calibration_reason": status.get("reason"),
            "kind": executor.get("kind"),
            "threshold": row.threshold,
            "metric_rule": row.metric_rule,
            "pass_k_declared": row.pass_k,
            "n": entry.get("n"),
            "expected": entry.get("expected"),
            "missing": entry.get("missing"),
            "pass": entry.get("pass"),
            "fail": entry.get("fail"),
            "inconclusive": entry.get("inconclusive"),
            "error": entry.get("error"),
            "rate": entry.get("rate"),
            "wilson_low": entry.get("wilson_low"),
            "wilson_high": entry.get("wilson_high"),
            "undetermined_rate": entry.get("undetermined_rate"),
            "pass_k": entry.get("pass_k"),
            "value": entry.get("value"),
            "waiver": None,
        }
        prev = previous_rows.get(row.key)
        if prev and prev.get("rate") is not None and entry.get("rate") is not None:
            out["trend"] = entry["rate"] - prev["rate"]

        blocking = effective in ("redline", "gate")
        # 2 — denominator (checked first so a missing item never reads as a pass)
        if blocking:
            if not entry:
                out["verdict"] = INVALID
                out["reason"] = "no results for this criterion"
                invalid_reasons.append(row.key)
                report_rows.append(out)
                continue
            if (entry.get("missing") or 0) > 0 or (entry.get("n") or 0) == 0:
                out["verdict"] = INVALID
                out["reason"] = (
                    f"{entry.get('missing') or 0} of {entry.get('expected') or 0} expected "
                    "items have no verdict"
                )
                invalid_reasons.append(row.key)
                report_rows.append(out)
                continue
            if (entry.get("undetermined_rate") or 0) > UNDETERMINED_INVALID:
                out["verdict"] = INVALID
                out["reason"] = (
                    f"undetermined rate {entry['undetermined_rate']:.1%} exceeds 5%"
                )
                invalid_reasons.append(row.key)
                report_rows.append(out)
                continue

        if effective == "redline":
            violations = entry.get("fail") or 0
            if executor.get("kind") == "metric":
                ok = _metric_ok(entry)
                violations = 0 if ok else 1
            out["violations"] = violations
            if violations:
                out["verdict"] = BLOCKED
                out["reason"] = f"{violations} violation(s) — red lines are never waived"
                redline_violations.append(row.key)
            else:
                out["verdict"] = PASS
        elif effective == "gate":
            ok, measured = _gate_ok(row, entry)
            out["measured"] = measured
            if measured is not None and row.threshold is not None and entry.get("n"):
                out["threshold_inside_ci"] = stats.threshold_inside_interval(
                    entry.get("pass") or 0, entry.get("n") or 0, float(row.threshold)
                ) if executor.get("kind") != "metric" else False
            if ok:
                out["verdict"] = PASS
            elif row.key in active_waivers:
                waiver = active_waivers[row.key]
                out["verdict"] = "WAIVED"
                out["waiver"] = {
                    "id": waiver.id, "expires_on": _aware(waiver.expires_on).isoformat(),
                    "risk_owner": waiver.risk_owner, "approved_by": waiver.approved_by,
                }
                waived.append(row.key)
            else:
                out["verdict"] = BLOCKED
                out["reason"] = "below threshold"
                gate_failures.append(row.key)
        else:
            ok, measured = _gate_ok(row, entry) if entry else (None, None)
            out["measured"] = measured
            out["verdict"] = "OBSERVED"
            if row.tier in ("redline", "gate") and criteria_svc.is_judge(row) and not calibrated:
                out["reason"] = "judge not calibrated — observed until calibrated"
        report_rows.append(out)

    provenance = provenance or {}
    provenance_issues = [i for i in provenance.get("issues") or [] if i]
    if redline_violations:
        verdict = BLOCKED
    elif invalid_reasons or provenance_issues:
        verdict = INVALID
    elif gate_failures:
        verdict = BLOCKED
    else:
        verdict = PASS
    return {
        "verdict": verdict,
        "decided_at": now.isoformat(),
        "criteria": report_rows,
        "redline_violations": redline_violations,
        "invalid": invalid_reasons,
        "gate_failures": gate_failures,
        "waived": waived,
        "provenance": provenance,
        "order": ["redline", "denominator", "gate", "observe"],
    }


def _metric_ok(entry: dict[str, Any]) -> bool:
    rule = entry.get("rule") or {}
    value = entry.get("value")
    if value is None:
        return False
    bound = float(rule.get("value", 0))
    return value <= bound if rule.get("op", "<=") == "<=" else value >= bound


def _gate_ok(row: Criterion, entry: dict[str, Any]) -> tuple[bool, float | None]:
    executor = row.executor or {}
    if executor.get("kind") == "metric":
        return _metric_ok(entry), entry.get("value")
    declared = row.pass_k or {}
    if declared and entry.get("pass_k") and entry["pass_k"].get("pass_k") is not None:
        measured = entry["pass_k"]["pass_k"]
    else:
        measured = entry.get("rate")
    if measured is None or row.threshold is None:
        return False, measured
    # thresholds compare at integer-percent precision (a 0.949 rate does not clear 95%)
    return round(measured * 100, 6) >= round(float(row.threshold) * 100, 6), measured
