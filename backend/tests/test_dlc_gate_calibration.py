"""The four-gate release decision and judge calibration."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.dlc import calibration as cal
from app.dlc import gate
from app.models.dlc import Annotation, AnnotationTask, CalibrationRecord, Criterion, Waiver


def crit(key, tier="gate", threshold=0.9, kind="code", **kw):
    executor = {"kind": "evaluator", "evaluator_id": f"ev-{key}", "evaluator_kind": kind}
    data = {"key": key, "text": key, "dimension": "quality", "tier": tier,
            "threshold": None if tier == "redline" else threshold, "level": "session",
            "denominator": "sessions", "executor": executor}
    data.update(kw)
    return Criterion(set_id="s", **data)


def entry(passes, fails, expected=None, inconclusive=0, **extra):
    n = passes + fails
    return {"kind": "evaluator", "pass": passes, "fail": fails, "inconclusive": inconclusive,
            "error": 0, "n": n, "expected": expected if expected is not None else n,
            "missing": (expected - n) if expected is not None else 0, **extra}


def summary(**entries):
    return {"criteria": entries}


def test_all_clear_passes():
    rows = [crit("R1", tier="redline"), crit("G1")]
    report = gate.decide(rows=rows, summaries=[summary(R1=entry(20, 0), G1=entry(19, 1))],
                         calibration={}, waivers=[])
    assert report["verdict"] == "PASS"
    assert [r["verdict"] for r in report["criteria"]] == ["PASS", "PASS"]


def test_a_red_line_violation_blocks_even_with_a_great_average():
    rows = [crit("R1", tier="redline"), crit("G1")]
    report = gate.decide(rows=rows, summaries=[summary(R1=entry(99, 1), G1=entry(100, 0))],
                         calibration={}, waivers=[])
    assert report["verdict"] == "BLOCKED" and report["redline_violations"] == ["R1"]


def test_the_48_to_34_denominator_trap_is_invalid_not_a_pass():
    rows = [crit("R1", tier="redline")]
    # 48 golden items, 14 never got a verdict, the 34 that did all passed
    report = gate.decide(rows=rows, summaries=[summary(R1=entry(34, 0, expected=48))],
                         calibration={}, waivers=[])
    assert report["verdict"] == "INVALID"
    assert "14 of 48" in report["criteria"][0]["reason"]


def test_too_many_inconclusive_is_invalid():
    rows = [crit("G1")]
    report = gate.decide(rows=rows, summaries=[summary(G1=entry(18, 0, inconclusive=2,
                                                            undetermined_rate=0.1))],
                         calibration={}, waivers=[])
    assert report["verdict"] == "INVALID"


def test_a_gate_below_threshold_blocks_and_a_waiver_lets_it_through():
    rows = [crit("G1", threshold=0.95)]
    s = summary(G1=entry(18, 2))
    assert gate.decide(rows=rows, summaries=[s], calibration={}, waivers=[])["verdict"] == "BLOCKED"
    waiver = Waiver(id="w1", agent_id="a", criterion_key="G1", status="approved",
                    expires_on=datetime.now(UTC) + timedelta(days=5), risk_owner="risk",
                    approved_by="ops")
    report = gate.decide(rows=rows, summaries=[s], calibration={}, waivers=[waiver])
    assert report["verdict"] == "PASS" and report["waived"] == ["G1"]
    expired = Waiver(id="w2", agent_id="a", criterion_key="G1", status="approved",
                     expires_on=datetime.now(UTC) - timedelta(days=1))
    assert gate.decide(rows=rows, summaries=[s], calibration={},
                       waivers=[expired])["verdict"] == "BLOCKED"


def test_a_waiver_never_covers_a_red_line():
    rows = [crit("R1", tier="redline")]
    waiver = Waiver(id="w", agent_id="a", criterion_key="R1", status="approved",
                    expires_on=datetime.now(UTC) + timedelta(days=5))
    report = gate.decide(rows=rows, summaries=[summary(R1=entry(9, 1))], calibration={},
                         waivers=[waiver])
    assert report["verdict"] == "BLOCKED"


def test_an_uncalibrated_judge_is_observed_and_does_not_block():
    rows = [crit("J1", kind="judge")]
    s = summary(J1=entry(10, 10))
    report = gate.decide(rows=rows, summaries=[s], calibration={}, waivers=[])
    assert report["verdict"] == "PASS"
    assert report["criteria"][0]["verdict"] == "OBSERVED"
    calibrated = gate.decide(rows=rows, summaries=[s],
                             calibration={"J1": {"calibrated": True}}, waivers=[])
    assert calibrated["verdict"] == "BLOCKED"


def test_pass_k_gates_use_pass_k_when_declared():
    rows = [crit("G1", threshold=0.8, pass_k={"k": 3, "mode": "all"})]
    s = summary(G1=entry(9, 1, pass_k={"k": 3, "pass_k": 0.6, "mean_k": 0.9, "scenarios": 10}))
    report = gate.decide(rows=rows, summaries=[s], calibration={}, waivers=[])
    assert report["verdict"] == "BLOCKED"  # 90% per attempt, but only 60% on every attempt


def test_metric_criteria_and_provenance():
    rows = [Criterion(set_id="s", key="P1", text="p95", dimension="performance", tier="gate",
                      level="session", denominator="sessions", executor={"kind": "metric"},
                      metric_rule={"metric": "latency_p95_ms", "op": "<=", "value": 3000})]
    ok = summary(P1={"kind": "metric", "value": 2500, "rule": rows[0].metric_rule, "n": 10,
                     "expected": 10, "missing": 0})
    assert gate.decide(rows=rows, summaries=[ok], calibration={}, waivers=[])["verdict"] == "PASS"
    report = gate.decide(rows=rows, summaries=[ok], calibration={}, waivers=[],
                         provenance={"issues": ["criteria version is not signed"]})
    assert report["verdict"] == "INVALID"


def test_threshold_precision_is_integer_percent():
    rows = [crit("G1", threshold=0.95)]
    report = gate.decide(rows=rows, summaries=[summary(G1=entry(949, 51))], calibration={},
                         waivers=[])
    assert report["verdict"] == "BLOCKED"


# ── calibration ───────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        for model in (Annotation, AnnotationTask, CalibrationRecord):
            session.query(model).delete()
        session.commit()
        yield session
    finally:
        session.rollback()
        session.close()


def _task(db, judge_labels):
    items = [{"ref": f"i{n}", "input": f"q{n}", "answer": f"a{n}", "judge_label": label}
             for n, label in enumerate(judge_labels)]
    return cal.create_task(db, workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a",
                           criteria_set_id="cs", criterion_key="J1",
                           purpose="judge_calibration", items=items,
                           annotators=["ann1", "ann2"], adjudicator="lead", actor="eng")


def test_a_task_needs_two_annotators(db):
    with pytest.raises(AppError):
        cal.create_task(db, workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a",
                        criteria_set_id=None, criterion_key="J1", purpose="judge_calibration",
                        items=[{"ref": "x"}], annotators=["solo"], adjudicator=None, actor="e")


def test_blind_labelling(db):
    task = _task(db, ["pass", "fail"])
    cal.record_label(db, task, annotator="ann1", item_ref="i0", label="pass")
    view = cal.task_view(db, task, viewer="ann2", privileged=False)
    assert all("judge_label" not in item for item in view["items"])
    assert all("labels" not in item for item in view["items"])
    assert view["items"][0]["my_label"] is None
    with pytest.raises(AppError) as exc:
        cal.record_label(db, task, annotator="outsider", item_ref="i0", label="pass")
    assert exc.value.code == "annotation.not_annotator"


def test_kappa_and_verdict_rules(db):
    human = ["pass"] * 8 + ["fail"] * 6
    task = _task(db, human)  # the judge agrees with humans on every item
    for n, label in enumerate(human):
        for who in ("ann1", "ann2"):
            cal.record_label(db, task, annotator=who, item_ref=f"i{n}", label=label)
    measured = cal.agreement(db, task)
    assert measured["n"] == 14 and measured["judge_human_kappa"] == pytest.approx(1.0)
    policy = cal.policy_of(None)
    assert cal.suggested_verdict(measured, policy) == "aligned"
    record = cal.decide(db, task, verdict="aligned", actor="reviewer", policy=policy,
                        evaluator_id="ev-J1", evaluator_updated_at="v1",
                        criteria_lineage_id="L", criteria_set_version=1)
    assert record.verdict == "aligned"
    status = cal.status_of(record, policy, evaluator_updated_at="v1")
    assert status["calibrated"] is True
    assert cal.status_of(record, policy, evaluator_updated_at="v2")["reason"] == (
        "evaluator_changed"
    )
    late = datetime.now(UTC) + timedelta(days=91)
    assert cal.status_of(record, policy, now=late)["reason"] == "expired"


def test_a_rubber_stamp_judge_cannot_be_recorded_aligned(db):
    human = ["pass"] * 8 + ["fail"] * 4
    task = _task(db, ["pass"] * 12)
    for n, label in enumerate(human):
        for who in ("ann1", "ann2"):
            cal.record_label(db, task, annotator=who, item_ref=f"i{n}", label=label)
    with pytest.raises(AppError) as exc:
        cal.decide(db, task, verdict="aligned", actor="reviewer", policy=cal.policy_of(None),
                   evaluator_id="ev-J1", evaluator_updated_at=None, criteria_lineage_id=None,
                   criteria_set_version=None)
    assert exc.value.code == "calibration.not_supported"
    assert exc.value.detail["judge_human_kappa"] == pytest.approx(0.0)


def test_policy_bounds():
    assert cal.policy_of({"calibration": {"period_days": 30, "kappa_floor": 0.7}}) == {
        "period_days": 30, "kappa_floor": 0.7}
    with pytest.raises(AppError):
        cal.normalize_policy({"period_days": 3})


def test_one_rater_is_not_a_consensus_so_a_judge_cannot_be_self_certified(db):
    """Security review finding: a single annotator's vote counted as consensus and the
    human ceiling was skipped when there was no second rater, so one person could label
    a run to match the judge and have it certified."""
    task = _task(db, ["pass" if n % 2 else "fail" for n in range(14)])
    for n in range(14):
        cal.record_label(db, task, annotator="ann1", item_ref=f"i{n}",
                         label="pass" if n % 2 else "fail")
    db.commit()
    measured = cal.agreement(db, task)
    policy = cal.policy_of(None)
    # nothing to compare the judge against: no item has two raters
    assert measured["n"] == 0 and measured["pairs"] == 0
    assert measured["human_human_kappa"] is None
    assert cal.suggested_verdict(measured, policy) == "insufficient_n"
    with pytest.raises(AppError) as exc:
        cal.decide(db, task, verdict="aligned", actor="ops", policy=policy,
                   evaluator_id="Builtin.Helpfulness", evaluator_updated_at=None,
                   criteria_lineage_id=None, criteria_set_version=None)
    assert exc.value.code == "calibration.not_supported"

    # with the second rater in, the same labels do support it
    for n in range(14):
        cal.record_label(db, task, annotator="ann2", item_ref=f"i{n}",
                         label="pass" if n % 2 else "fail")
    db.commit()
    measured = cal.agreement(db, task)
    assert measured["pairs"] == 14 and measured["human_human_kappa"] == pytest.approx(1.0)
    assert cal.suggested_verdict(measured, policy) == "aligned"


def test_an_annotator_cannot_rule_on_their_own_labels(db):
    """The labels are the evidence, so the person who wrote them does not rule on them."""
    labels = ["pass", "fail"] * 6
    task = _task(db, labels)
    for who in ("ann1", "ann2"):
        for n, label in enumerate(labels):
            cal.record_label(db, task, annotator=who, item_ref=f"i{n}", label=label)
    db.commit()
    policy = cal.policy_of(None)
    assert cal.suggested_verdict(cal.agreement(db, task), policy) == "aligned"
    with pytest.raises(AppError) as exc:
        cal.decide(db, task, verdict="aligned", actor="ann1", policy=policy,
                   evaluator_id="Builtin.Helpfulness", evaluator_updated_at=None,
                   criteria_lineage_id=None, criteria_set_version=None)
    assert exc.value.code == "calibration.own_labels"
    record = cal.decide(db, task, verdict="aligned", actor="ops", policy=policy,
                        evaluator_id="Builtin.Helpfulness", evaluator_updated_at=None,
                        criteria_lineage_id=None, criteria_set_version=None)
    assert record.verdict == "aligned"


def test_a_sample_judged_all_one_way_cannot_certify_a_judge(db):
    """κ is 1.0 by convention when everyone said "pass" every time; that shows nothing
    about telling a pass from a fail, so "aligned" is refused and the reason is named."""
    task = _task(db, ["pass"] * 15)
    for who in ("ann1", "ann2"):
        for n in range(15):
            cal.record_label(db, task, annotator=who, item_ref=f"i{n}", label="pass")
    db.commit()
    policy = cal.policy_of(None)
    measured = cal.agreement(db, task)
    assert measured["judge_human_kappa"] == 1.0 and measured["human_classes"] == ["pass"]
    assert cal.suggested_verdict(measured, policy) == "one_class"
    with pytest.raises(AppError) as exc:
        cal.decide(db, task, verdict="aligned", actor="ops", policy=policy,
                   evaluator_id="Builtin.Helpfulness", evaluator_updated_at=None,
                   criteria_lineage_id=None, criteria_set_version=None)
    assert exc.value.code == "calibration.not_supported"
    record = cal.decide(db, task, verdict="not_aligned", actor="ops", policy=policy,
                        evaluator_id="Builtin.Helpfulness", evaluator_updated_at=None,
                        criteria_lineage_id=None, criteria_set_version=None)
    assert record.verdict == "not_aligned"
