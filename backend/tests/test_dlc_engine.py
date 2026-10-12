"""Criterion verdicts: label maps, score rules, collapsing, pass^k and the denominator."""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.dlc import engine
from app.evaluation.models import EvalRun
from app.models.dlc import Criterion, CriterionResult


def crit(key, evaluator_id="code-pii", **kw):
    data = {
        "key": key, "text": key, "dimension": "quality", "tier": "gate", "threshold": 0.9,
        "level": "session", "denominator": "sessions",
        "executor": {"kind": "evaluator", "evaluator_id": evaluator_id,
                     "evaluator_kind": "code"},
    }
    data.update(kw)
    return Criterion(set_id="s", **data)


def rec(session, evaluator, *, score=None, label=None, error=None, span=""):
    attrs = {
        "session.id": session,
        "gen_ai.evaluation.name": evaluator,
        "aws.bedrock_agentcore.evaluator.arn": f"arn:aws:x:::evaluator/{evaluator}",
        "aws.bedrock_agentcore.evaluation_level": "Session",
        "gen_ai.evaluation.score.value": score,
        "gen_ai.evaluation.score.label": label,
        "gen_ai.evaluation.explanation": "because",
        "span.id": span,
    }
    if error:
        attrs["error.type"] = error
    return attrs


def run_with(sessions, repeats=1, scenario_of=None):
    attempts = []
    for index, sid in enumerate(sessions):
        scenario = (scenario_of or (lambda i: f"s{i}"))(index)
        attempts.append({"scenario_id": scenario, "attempt": index % repeats + 1,
                         "session_id": sid})
    return EvalRun(id="r1", workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a", agent_name="a",
                   session_ids=list(sessions), attempts=attempts, repeats=repeats,
                   repeat_mode="all", status="completed")


def test_label_map_and_unmapped_labels():
    c = crit("C1", executor={"kind": "evaluator", "evaluator_id": "j",
                             "label_map": {"pass": ["Compliant"], "fail": ["Leak"]}})
    assert engine.item_verdict(c, {"label": "compliant"}) == ("pass", None)
    assert engine.item_verdict(c, {"label": "LEAK"}) == ("fail", None)
    assert engine.item_verdict(c, {"label": "unsure"}) == ("inconclusive", None)


def test_errors_are_never_a_pass():
    c = crit("C1")
    assert engine.item_verdict(c, {"error_type": "SpanNotFound", "score": 1.0})[0] == "error"


def test_score_rule_and_penalty_polarity():
    c = crit("C1")
    assert engine.item_verdict(c, {"score": 0.8})[0] == "pass"
    assert engine.item_verdict(c, {"score": 0.2})[0] == "fail"
    strict = crit("C2", executor={"kind": "evaluator", "evaluator_id": "x",
                                  "score_rule": {"op": ">=", "value": 0.95}})
    assert engine.item_verdict(strict, {"score": 0.9})[0] == "fail"
    refusal = crit("C3", evaluator_id="Builtin.Refusal")
    assert engine.item_verdict(refusal, {"score": 1.0})[0] == "fail"  # refused ⇒ bad
    assert engine.item_verdict(c, {"label": "INCONCLUSIVE", "score": None})[0] == "inconclusive"


def test_collapse_order():
    assert engine.collapse(["pass", "fail", "error"]) == "fail"
    assert engine.collapse(["pass", "error"]) == "error"
    assert engine.collapse(["pass", "pass"]) == "pass"
    assert engine.collapse(["pass", "inconclusive"]) == "inconclusive"
    assert engine.collapse([]) == "error"


def test_summary_rates_wilson_and_missing_sessions():
    rows = [crit("C1")]
    run = run_with(["a", "b", "c", "d"])
    records = [rec("a", "code-pii", score=1.0), rec("b", "code-pii", score=1.0),
               rec("c", "code-pii", score=0.0)]
    results = engine.build_results(run, rows, records)
    summary, denominator = engine.summarize(run, rows, results)
    entry = summary["C1"]
    assert (entry["pass"], entry["fail"], entry["n"]) == (2, 1, 3)
    assert entry["rate"] == pytest.approx(2 / 3)
    assert entry["wilson_low"] < entry["rate"] < entry["wilson_high"]
    assert entry["missing"] == 1  # session d has no verdict: the denominator check sees it
    assert denominator["expected_items"] == 4 and denominator["missing"] == 1


def test_trace_level_units_collapse_per_session():
    rows = [crit("C1", level="trace")]
    run = run_with(["a"])
    records = [rec("a", "code-pii", score=1.0, span="t1"),
               rec("a", "code-pii", score=0.0, span="t2")]
    summary, _ = engine.summarize(run, rows, engine.build_results(run, rows, records))
    assert summary["C1"]["fail"] == 1 and summary["C1"]["n"] == 1


def test_turn_denominator_counts_units():
    rows = [crit("C1", level="trace", denominator="turns")]
    run = run_with(["a"])
    records = [rec("a", "code-pii", score=1.0, span="t1"),
               rec("a", "code-pii", score=0.0, span="t2")]
    summary, _ = engine.summarize(run, rows, engine.build_results(run, rows, records))
    assert summary["C1"]["n"] == 2 and summary["C1"]["pass"] == 1


def test_pass_k_over_repeated_attempts():
    rows = [crit("C1")]
    # two scenarios × 3 attempts: x passes 3/3, y passes 2/3
    sessions = ["x1", "x2", "x3", "y1", "y2", "y3"]
    run = run_with(sessions, repeats=3, scenario_of=lambda i: "x" if i < 3 else "y")
    scores = {"x1": 1, "x2": 1, "x3": 1, "y1": 1, "y2": 0, "y3": 1}
    records = [rec(s, "code-pii", score=float(v)) for s, v in scores.items()]
    summary, _ = engine.summarize(run, rows, engine.build_results(run, rows, records))
    pk = summary["C1"]["pass_k"]
    assert pk["k"] == 3 and pk["pass_k"] == pytest.approx(0.5)
    assert pk["mean_k"] == pytest.approx(5 / 6)


def test_metric_criteria_aggregate_p95_and_cost():
    rows = [
        crit("P1", dimension="performance", executor={"kind": "metric"},
             metric_rule={"metric": "latency_p95_ms", "op": "<=", "value": 3000}),
        crit("K1", dimension="cost", executor={"kind": "metric"},
             metric_rule={"metric": "cost_per_success_usd", "op": "<=", "value": 0.01}),
    ]
    run = run_with(["a", "b"])
    metrics = {"a": {"latency_ms": 1000, "cost_usd": 0.004},
               "b": {"latency_ms": 5000, "cost_usd": 0.006}}
    summary, _ = engine.summarize(run, rows, engine.build_results(run, rows, [], metrics))
    assert summary["P1"]["value"] == pytest.approx(4800)  # interpolated p95 of 1000, 5000
    assert summary["P1"]["verdict"] == "fail"
    assert summary["K1"]["value"] == pytest.approx(0.005) and summary["K1"]["verdict"] == "pass"


def test_finalize_persists_results_and_summary():
    from app.dlc import criteria as criteria_svc
    from app.models.dlc import CriteriaSet

    db = SessionLocal()
    try:
        cs = criteria_svc.create_set(db, DEFAULT_WORKSPACE_ID, kind="agent", agent_id="eng-a",
                                     name="e", actor="t")
        criteria_svc.save_draft(db, cs, [{
            "key": "C1", "text": "agent never leaks", "dimension": "responsibility",
            "tier": "redline", "level": "session",
            "executor": {"kind": "evaluator", "evaluator_id": "code-pii"},
        }], actor="t", kind_of=lambda _e: "code")
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="eng-a", agent_name="a",
                      status="completed", session_ids=["s1", "s2"], criteria_set_id=cs.id,
                      attempts=[{"scenario_id": "a", "attempt": 1, "session_id": "s1"},
                                {"scenario_id": "b", "attempt": 1, "session_id": "s2"}])
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    out = engine.finalize_run(run_id, workspace=None, records=[
        rec("s1", "code-pii", label="PASS", score=1.0),
        rec("s2", "code-pii", label="FAIL", score=0.0),
    ])
    assert out["criteria"]["C1"]["fail"] == 1
    db = SessionLocal()
    try:
        rows = db.query(CriterionResult).filter(CriterionResult.run_id == run_id).all()
        assert {r.scenario_id: r.verdict for r in rows} == {"a": "pass", "b": "fail"}
        stored = db.get(EvalRun, run_id)
        assert stored.denominator["expected_items"] == 2
        db.query(CriterionResult).filter(CriterionResult.run_id == run_id).delete()
        db.query(Criterion).filter(Criterion.set_id == cs.id).delete()
        db.query(CriteriaSet).filter(CriteriaSet.id == cs.id).delete()
        db.commit()
    finally:
        db.close()
