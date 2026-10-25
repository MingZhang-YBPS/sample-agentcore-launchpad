"""Evaluation cost: estimate every entry, record what a run cost (eval-cost §1–§2)."""

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.dlc import compare as compare_svc
from app.dlc import cost
from app.evaluation import online, run_cost
from app.evaluation.models import EvalDataset, EvalRun
from app.models.dlc import CriteriaSet, Criterion, CriterionResult
from app.models.ledger import Agent, Workspace
from app.services import project_quotas

WS = DEFAULT_WORKSPACE_ID
ITEMS = [{"scenario_id": f"s{n}", "turns": [{"input": "hi"}, {"input": "more"}]}
         for n in range(10)]
PRICES = {"global.openai.gpt-6-sol": {"input": 2.0, "output": 10.0},
          "claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}
JUDGE_ROWS = [Criterion(set_id="s", key="J1", text="t", dimension="quality", tier="observe",
                        level="session", denominator="sessions",
                        executor={"kind": "evaluator", "evaluator_id": "custom-judge",
                                  "evaluator_kind": "judge"})]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(cost, "_price_of", lambda model: next(
        (v for k, v in PRICES.items() if model and k in model), None))
    monkeypatch.setattr(cost, "_default_judge_model", lambda: "global.openai.gpt-6-sol")
    session = SessionLocal()
    try:
        for model in (CriterionResult, EvalRun, Criterion, CriteriaSet):
            session.query(model).delete()
        session.query(Agent).filter(Agent.name.like("ec-%")).delete()
        session.get(Workspace, WS).release_policy = {}
        session.commit()
        yield session
    finally:
        session.rollback()
        session.get(Workspace, WS).release_policy = {}
        session.commit()
        session.close()


def _agent(db, name="ec-a", model="anthropic.claude-sonnet-5-5"):
    agent = Agent(workspace_id=WS, name=name, method="zip_runtime", status="active",
                  arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/rt-ec",
                  resource_id="rt-ec", spec={"name": name, "model_id": model}, version="1")
    db.add(agent)
    db.commit()
    return agent


def _estimate(db, agent, **kw):
    kw.setdefault("items", ITEMS)
    kw.setdefault("evaluators", ["Builtin.Correctness", "custom-judge"])
    kw.setdefault("criteria_rows", JUDGE_ROWS)
    return cost.estimate(db, agent=agent, workspace=db.get(Workspace, WS),
                         workspace_ctx=SimpleNamespace(), **kw)


# ── the estimate, every entry ─────────────────────────────────────────────────


@pytest.mark.parametrize("scope,kw,sessions,agent_zero", [
    ("replay", {"repeats": 2}, 20, False),
    ("passive", {"sessions": 30}, 30, True),
    ("online", {"sessions": 12, "period": "day"}, 12, True),
])
def test_every_entry_is_priced_in_four_parts(db, monkeypatch, scope, kw, sessions, agent_zero):
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.02, "history_7d"))
    out = _estimate(db, agent, scope=scope, **kw)
    parts = {p["key"]: p for p in out["components"]}
    assert set(parts) == {"agent", "runtime", "judge", "managed"}
    assert out["sessions"] == sessions and out["scope"] == scope
    if agent_zero:  # nothing re-invoked: no agent tokens, no runtime seconds
        assert out["agent_usd"] == 0 and out["runtime_usd"] == 0
        assert out["agent_basis"] == "not_invoked"
    else:
        assert out["agent_usd"] == pytest.approx(0.02 * sessions)
        assert out["runtime_usd"] > 0 and out["runtime_basis"] == "rough"
    assert out["judge_usd"] > 0 and out["managed_usd"] > 0
    total = sum(p["usd"] or 0 for p in parts.values())
    assert out["total_usd"] == pytest.approx(total, abs=1e-3)
    assert out["range"]["low"] <= out["range"]["mid"] <= out["range"]["high"]
    assert out["basis"]["prices"]["source"] and out["basis"]["prices"]["updated_at"]
    assert out["unpriced_models"] == []


def test_a_session_level_judge_judges_once_a_trace_level_per_turn(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.0, "history_7d"))
    out = _estimate(db, agent)
    by_id = {j["evaluator_id"]: j for j in out["judges"]}
    assert by_id["custom-judge"]["judgements"] == 10  # SESSION: one per session
    assert by_id["Builtin.Correctness"]["judgements"] == 20  # TRACE: two turns each


def test_unpriced_models_are_listed_not_counted_as_zero(db, monkeypatch):
    agent = _agent(db, model="example.unpriced-agent")
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (None, "no_history"))
    monkeypatch.setattr(cost, "_default_judge_model", lambda: "example.unpriced-judge")
    out = _estimate(db, agent)
    assert out["agent_usd"] is None and out["unpriced"] is True
    assert set(out["unpriced_models"]) == {"example.unpriced-agent", "example.unpriced-judge"}


def test_over_the_limit_blocks_and_over_the_threshold_asks(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.5, "history_7d"))
    db.get(Workspace, WS).release_policy = {"eval_cost_confirm_usd": 1.0,
                                            "eval_cost_max_usd": 3.0}
    db.commit()
    out = _estimate(db, agent)
    assert out["confirm_required"] and out["over_limit"]
    passive = _estimate(db, agent, scope="passive", sessions=1, evaluators=[])
    assert not passive["confirm_required"] and not passive["over_limit"]


def test_measured_runs_feed_the_next_estimate(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (None, "no_history"))
    cold = _estimate(db, agent)
    assert cold["agent_basis"] == "rough" and cold["runtime_basis"] == "rough"
    custom = next(j for j in cold["judges"] if j["evaluator_id"] == "custom-judge")
    assert custom["tokens_per_item"] == cost.ROUGH_JUDGE_TOKENS and custom["samples"] == 0
    for usd in (0.01, 0.012, 0.011):
        db.add(EvalRun(workspace_id=WS, agent_id=agent.id, agent_name=agent.name,
                       status="completed", cost_actual={
                           "final": True, "source": "usage_events",
                           "agent_usd_per_session": usd, "runtime_seconds_per_session": 20.0,
                           "judge_tokens": {"custom-judge": 900.0}}))
    # a live record and an estimate fallback are not measurements
    db.add(EvalRun(workspace_id=WS, agent_id=agent.id, agent_name=agent.name, status="running",
                   cost_actual={"final": False, "live": {"total_usd": 9}}))
    db.add(EvalRun(workspace_id=WS, agent_id=agent.id, agent_name=agent.name,
                   status="failed", cost_actual={"final": True, "source": "estimate_fallback",
                                                 "agent_usd_per_session": 99.0}))
    db.commit()
    warm = _estimate(db, agent)
    assert warm["agent_basis"] == "eval_runs"
    assert warm["agent_usd"] == pytest.approx(0.011 * 10)
    assert warm["runtime_basis"] == "eval_runs" and warm["runtime_seconds"] == 200.0
    custom = next(j for j in warm["judges"] if j["evaluator_id"] == "custom-judge")
    assert custom["tokens_per_item"] == 900.0 and custom["samples"] == 3


def test_estimate_route_prices_passive_and_online_entries(client, db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(cost, "window_sessions", lambda *a, hours, **k: int(hours))
    body = client.post("/api/eval/runs/estimate", json={
        "agent_id": agent.id, "scope": "passive", "lookback_hours": 48,
        "evaluators": ["Builtin.Helpfulness"]}).json()
    assert body["scope"] == "passive" and body["sessions"] == 48
    assert body["sessions_basis"] == "history_7d" and body["agent_usd"] == 0
    online_est = client.post("/api/eval/runs/estimate", json={
        "agent_id": agent.id, "scope": "online", "sampling_percentage": 50,
        "evaluators": ["Builtin.Helpfulness"]}).json()
    assert online_est["period"] == "day" and online_est["sessions"] == 12  # 24 × 50 %
    given = client.post("/api/eval/runs/estimate", json={
        "agent_id": agent.id, "scope": "passive", "sessions": 5,
        "evaluators": ["Builtin.Helpfulness"]}).json()
    assert given["sessions"] == 5 and given["sessions_basis"] == "given"


# ── the actual cost, at every terminal state ──────────────────────────────────


def _run(db, agent, **fields):
    fields.setdefault("status", "completed")
    fields.setdefault("mode", "evaluators")
    fields.setdefault("evaluators", ["Builtin.Correctness"])
    fields.setdefault("session_ids", [f"sess-{n:02d}" + "x" * 30 for n in range(4)])
    fields.setdefault("attempts", [{"scenario_id": f"s{n}", "attempt": 1} for n in range(4)])
    fields.setdefault("batch_eval_id", "be-1")
    run = EvalRun(workspace_id=WS, agent_id=agent.id, agent_name=agent.name, **fields)
    db.add(run)
    db.commit()
    return run


def _live(model="anthropic.claude-sonnet-5-5", sessions=4):
    spend = run_cost.LiveSpend(model)
    invoke = spend.metered(lambda prompt: {
        "text": "ok", "session_id": "s", "usage": {"input_tokens": 1000, "output_tokens": 100}})
    for _ in range(sessions):
        invoke("hi")
        record = spend.session_done()
    return record


def test_live_spend_meters_usage_and_time():
    record = _live(sessions=3)
    assert record["final"] is False and "agent_usd" not in record
    live = record["live"]
    assert live["sessions"] == 3 and live["invokes"] == 3 and live["usage_invokes"] == 3
    assert live["tokens"] == {"input_tokens": 3000, "output_tokens": 300}
    assert live["seconds"] >= 0 and live["total_usd"] >= live["runtime_usd"]


def test_usage_events_are_the_first_source(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(run_cost, "SPANS_READER", lambda *a: pytest.fail("spans read"))
    run = _run(db, agent, cost_actual=_live(), scores=[
        {"evaluatorId": "Builtin.Correctness", "score": 0.9, "count": 8}],
        cost_estimate={"total_usd": 0.05, "sessions": 4, "agent_usd": 0.04})
    out = run_cost.record(run.id, SimpleNamespace())
    assert out["source"] == "usage_events" and out["final"] is True
    # 4 × (1000 × $3 + 100 × $15) / 1M
    assert out["agent_usd"] == pytest.approx(0.018)
    assert out["agent_tokens"] == {"input": 4000, "output": 400}
    assert out["judgements"] == {"Builtin.Correctness": 8}
    assert out["managed_evaluations_usd"] > 0
    assert out["total_usd"] == pytest.approx(
        out["agent_usd"] + out["runtime_usd"] + out["judge_usd"]
        + out["managed_evaluations_usd"], abs=1e-6)
    assert out["estimate_total_usd"] == 0.05 and "delta_pct" in out
    assert out["agent_usd_per_session"] == pytest.approx(0.0045)
    db.expire_all()
    assert db.get(EvalRun, run.id).cost_actual["total_usd"] == out["total_usd"]


def test_spans_are_the_second_source(db, monkeypatch):
    agent = _agent(db)
    seen = {}

    def spans(session_ids, workspace):
        seen["sids"] = session_ids
        return {sid: {"cost_usd": 0.01, "tokens": 500} for sid in session_ids}

    monkeypatch.setattr(run_cost, "SPANS_READER", spans)
    run = _run(db, agent)  # no usage events (a harness invoke reports none)
    out = run_cost.record(run.id, SimpleNamespace())
    assert out["source"] == "spans" and out["agent_usd"] == pytest.approx(0.04)
    assert len(seen["sids"]) == 4


def test_the_estimate_is_the_fallback_and_never_a_sample(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(run_cost, "SPANS_READER", lambda *a: {})
    run = _run(db, agent, cost_estimate={"total_usd": 1.0, "sessions": 8, "agent_usd": 0.8,
                                         "runtime_seconds": 80.0})
    out = run_cost.record(run.id, SimpleNamespace())
    assert out["source"] == "estimate_fallback"
    assert out["agent_usd"] == pytest.approx(0.4)  # 4 of the 8 planned sessions ran
    assert "agent_usd_per_session" not in out


def test_a_failed_run_records_what_it_spent(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(run_cost, "SPANS_READER", lambda *a: pytest.fail("spans read"))
    run = _run(db, agent, status="failed", batch_eval_id=None, cost_actual=_live(sessions=2),
               session_ids=["a" * 33, "b" * 33])
    out = run_cost.record(run.id, None)
    assert out["partial"] is True and out["status"] == "failed"
    assert out["agent_usd"] == pytest.approx(0.009) and out["judge_usd"] == 0
    assert out["judgements"] == {}  # no batch: nothing was judged


def test_a_passive_run_pays_only_its_judges(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(run_cost, "SPANS_READER", lambda *a: pytest.fail("spans read"))
    run = _run(db, agent, attempts=None, dataset_name="window:24",
               scores=[{"evaluatorId": "Builtin.Correctness", "score": 1, "count": 6}])
    out = run_cost.record(run.id, SimpleNamespace())
    assert out["invoked"] is False and out["agent_usd"] == 0 and out["runtime_usd"] == 0
    assert out["managed_evaluations_usd"] > 0 and out["total_usd"] > 0


def test_judge_tokens_from_the_results_stream_are_recorded(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(run_cost, "SPANS_READER", lambda *a: {})
    run = _run(db, agent)
    records = [{"gen_ai.evaluation.name": "Builtin.Correctness",
                "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 200}] * 3
    out = run_cost.record(run.id, SimpleNamespace(), records_reader=lambda: records)
    assert out["judge_tokens"] == {"Builtin.Correctness": 1200.0}
    assert out["judgements"] == {"Builtin.Correctness": 3}
    assert cost._judge_tokens(db, "Builtin.Correctness") == 1200.0


def test_record_keeps_an_earlier_final_record_when_asked(db, monkeypatch):
    agent = _agent(db)
    run = _run(db, agent, status="failed", cost_actual={"final": True, "total_usd": 7})
    assert run_cost.record(run.id, None, only_if_missing=True)["total_usd"] == 7
    active = _run(db, agent, status="evaluating")
    assert run_cost.record(active.id, None) is None


def test_the_recorded_cost_counts_against_the_project_month(db, monkeypatch):
    """Only the judges count here: the project's agent traffic is priced from its
    traces, so a run's agent spend would be counted twice."""
    agent = _agent(db)
    monkeypatch.setattr(run_cost, "SPANS_READER", lambda *a: {})
    run = _run(db, agent, cost_actual=_live(),
               cost_estimate={"total_usd": 5.0, "judge_usd": 2.0, "managed_usd": 1.0})
    with project_context(None):
        before = project_quotas.run_judge_usd(db.get(EvalRun, run.id))
    assert before == 3.0  # live record: the judges' estimate still counts
    out = run_cost.record(run.id, SimpleNamespace())
    db.expire_all()
    with project_context(None):
        after = project_quotas.run_judge_usd(db.get(EvalRun, run.id))
    assert after == pytest.approx(out["judge_usd"] + out["managed_evaluations_usd"])


def test_execute_run_writes_live_then_final_cost(client, db, monkeypatch):
    """A replay end to end: usage events land in the live record, the finish writes
    the final one."""
    agent = _agent(db, name="ec-flow")
    dataset = EvalDataset(workspace_id=WS, name="ec-mini",
                          items=[{"prompt": "a"}, {"prompt": "b"}])
    db.add(dataset)
    db.commit()
    from unittest.mock import MagicMock

    data = MagicMock()
    calls = {"n": 0}
    lives: list[dict] = []
    real_update = svc._update

    def spy(run_id, **fields):
        if "cost_actual" in fields:
            lives.append(fields["cost_actual"])
        real_update(run_id, **fields)

    def invoke(client_, arn, prompt, session_id=None, actor_id="default",
               runtime_user_id=None):
        calls["n"] += 1
        return {"text": "ok", "session_id": f"sess-{calls['n']:03d}" + "x" * 30,
                "usage": {"input_tokens": 2000, "output_tokens": 200}}

    monkeypatch.setattr(svc, "_update", spy)
    monkeypatch.setattr(svc.rt, "invoke_runtime_text", invoke)
    monkeypatch.setattr(svc.rt, "get_runtime", lambda c, rid: {"agentRuntimeName": "ec_rt"})
    monkeypatch.setattr(svc, "control_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(svc, "_wait_for_fresh_telemetry", MagicMock())
    monkeypatch.setattr(run_cost, "SPANS_READER", lambda *a: pytest.fail("spans read"))
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.01, "history_7d"))
    from app.dlc import engine

    monkeypatch.setattr(engine, "finalize_run", lambda *a, **k: None)
    data.start_batch_evaluation.return_value = {"batchEvaluationId": "be-ec"}
    data.get_batch_evaluation.return_value = {"status": "COMPLETED", "evaluationResults": {
        "evaluatorSummaries": [{"evaluatorId": "Builtin.Correctness",
                                "statistics": {"averageScore": 0.9}, "totalEvaluated": 2}]}}
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0})
    assert res.status_code == 201, res.text
    run_id = res.json()["id"]
    for _ in range(100):
        db.expire_all()
        row = db.get(EvalRun, run_id)
        if row.status in ("completed", "failed") and (row.cost_actual or {}).get("final"):
            break
        time.sleep(0.1)
    assert row.status == "completed", row.error
    assert [entry["live"]["sessions"] for entry in lives] == [1, 2]
    actual = row.cost_actual
    assert actual["final"] and actual["source"] == "usage_events"
    assert actual["agent_tokens"] == {"input": 4000, "output": 400}
    assert actual["judgements"] == {"Builtin.Correctness": 2}
    card = client.get(f"/api/eval/runs/{run_id}/cost").json()
    assert card["final"] is True and card["actual"]["total_usd"] == actual["total_usd"]
    assert card["estimate_total_usd"] is not None
    costs = client.get(f"/api/eval/run-costs?runs={run_id},nope").json()["costs"]
    assert costs == {run_id: {"usd": actual["total_usd"], "kind": "actual",
                              "estimate_usd": actual["estimate_total_usd"]}}


def test_cost_routes_answer_404_for_an_unknown_run(client):
    assert client.get("/api/eval/runs/nope/cost").status_code == 404


def test_compare_reports_the_cost_delta(db):
    agent = _agent(db)
    first = _run(db, agent, cost_actual={"final": True, "total_usd": 1.0, "agent_usd": 0.6,
                                         "runtime_usd": 0.1, "judge_usd": 0.2,
                                         "managed_evaluations_usd": 0.1})
    second = _run(db, agent, cost_actual={"final": True, "total_usd": 1.5, "agent_usd": 0.9,
                                          "runtime_usd": 0.2, "judge_usd": 0.3,
                                          "managed_evaluations_usd": 0.1})
    out = compare_svc.compare(db, WS, [first.id, second.id])
    assert [r["cost_actual_usd"] for r in out["runs"]] == [1.0, 1.5]
    delta = out["cost_delta"]
    assert delta["delta_usd"] == pytest.approx(0.5) and delta["delta_pct"] == 50.0
    assert delta["parts"]["agent_usd"] == pytest.approx(0.3)
    assert delta["parts"]["managed_evaluations_usd"] == 0
    # a record from before the parts existed still compares on its agent spend
    legacy = _run(db, agent, cost_actual={"agent_usd": 0.5})
    assert compare_svc.run_cost_usd(legacy) == 0.5


def test_online_daily_cost(client, db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(online, "get_config", lambda *a, **k: {
        "config_id": "oe_x-1", "evaluators": ["Builtin.Helpfulness", "custom-judge"],
        "agent_id": agent.id, "arn": "arn:x", "mode": "scores"})
    seen = {}

    def fake_run(queries, hours, log_groups=None, workspace=None):
        seen.update(queries=queries, hours=hours, log_groups=log_groups)
        return {"daily": [
            {"evaluator": "Builtin.Helpfulness", "day": "2026-10-13 00:00:00.000", "count": "100"},
            {"evaluator": "custom-judge", "day": "2026-10-13 00:00:00.000", "count": "50"},
            {"evaluator": "Builtin.Helpfulness", "day": "2026-10-14 00:00:00.000", "count": "10"},
        ]}

    monkeypatch.setattr(online, "run_insights_queries", fake_run)
    db.add(EvalRun(workspace_id=WS, agent_id=agent.id, agent_name=agent.name,
                   status="completed", dataset_name="online:oe_x-1", mode="insights",
                   created_at=datetime.now(UTC),
                   cost_actual={"final": True, "total_usd": 0.25}))
    db.commit()
    body = client.get("/api/eval/online/oe_x-1/cost?days=7").json()
    assert seen["hours"] == 168 and "bin(1d)" in seen["queries"]["daily"]
    days = {d["day"]: d for d in body["days"]}
    assert days["2026-10-13"]["judgements"] == 150
    assert days["2026-10-13"]["by_evaluator"]["custom-judge"]["billed_by"] == "your_account"
    assert days["2026-10-14"]["judge_usd"] < days["2026-10-13"]["judge_usd"]
    today = datetime.now(UTC).date().isoformat()
    assert days[today]["reports_usd"] == 0.25
    assert body["total_usd"] == pytest.approx(sum(d["usd"] for d in body["days"]), abs=1e-6)
    assert client.get("/api/eval/online/oe_x-1/cost?days=90").status_code == 422
