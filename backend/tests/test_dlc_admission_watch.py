"""Admission review, scheduled watch, drift triage and the background tick."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.dlc import admission, scheduler
from app.dlc import criteria as criteria_svc
from app.dlc import golden as golden_svc
from app.dlc import watch as watch_svc
from app.evaluation.models import EvalDataset, EvalRun
from app.models.dlc import (
    AdmissionCandidate,
    CalibrationRecord,
    CriteriaSet,
    Criterion,
    SchedulerClaim,
    Waiver,
    WatchConfig,
)
from app.models.ledger import Agent, ChatFeedback, ChatMessage, Workspace
from app.models.selfservice import Issue

WS = DEFAULT_WORKSPACE_ID


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        for model in (AdmissionCandidate, WatchConfig, Waiver, CalibrationRecord,
                      SchedulerClaim, EvalRun, Criterion, CriteriaSet, Issue, ChatFeedback,
                      ChatMessage):
            session.query(model).delete()
        session.query(EvalDataset).filter(EvalDataset.role == "golden").delete()
        session.query(Agent).filter(Agent.name.like("aw-%")).delete()
        session.commit()
        yield session
    finally:
        session.rollback()
        session.close()


def _agent(db, name="aw-a"):
    agent = Agent(workspace_id=WS, name=name, method="harness", status="active",
                  spec={"name": name}, version="2", resource_id="h-aw")
    db.add(agent)
    db.commit()
    return agent


def _criteria(db, agent):
    s = criteria_svc.create_set(db, WS, kind="agent", agent_id=agent.id, name="c", actor="biz")
    criteria_svc.save_draft(db, s, [
        {"key": "R1", "text": "agent never leaks", "dimension": "responsibility",
         "tier": "redline", "executor": {"kind": "evaluator", "evaluator_id": "code-pii"},
         "notes": "n/a:cognition n/a:cost n/a:performance"},
        {"key": "G1", "text": "agent answers correctly", "dimension": "quality",
         "tier": "gate", "threshold": 0.9,
         "executor": {"kind": "evaluator", "evaluator_id": "Builtin.Correctness",
                      "evaluator_kind": "judge"}},
    ], actor="biz", kind_of=lambda e: "code" if e.startswith("code-") else "judge")
    criteria_svc.publish(db, s, actor="biz")
    criteria_svc.sign(db, s, actor="own", note="", is_admin=False)
    db.commit()
    return s


def _golden(db, cset, items=3):
    parent = golden_svc.create(db, WS, name="g", criteria_lineage_id=cset.lineage_id,
                               actor="biz")
    splits = golden_svc.splits_of(db, parent)
    golden_svc.add_items(db, splits["regression"], [
        {"scenario_id": f"r{n}", "turns": [{"input": f"q{n}"}]} for n in range(items)
    ], actor="biz")
    db.commit()
    return parent


# ── admission ─────────────────────────────────────────────────────────────────


def test_normalize_folds_case_space_punctuation_and_width():
    assert admission.normalize_text("6205 能换吗？") == admission.normalize_text("6205能换吗")
    assert admission.normalize_text("能 换 吗") == "能换吗"
    assert admission.normalize_text("Hello,  WORLD!") == "hello world"
    assert admission.normalize_text("ＡＢＣ") == "abc"


def test_collect_gathers_thumbs_down_and_issues(db):
    agent = _agent(db)
    msg_q = ChatMessage(workspace_id=WS, agent_id=agent.id, session_id="s1", role="user",
                        text="where is my order")
    db.add(msg_q)
    db.flush()
    msg_a = ChatMessage(workspace_id=WS, agent_id=agent.id, session_id="s1", role="agent",
                        text="it shipped")
    db.add(msg_a)
    db.flush()
    db.add(ChatFeedback(workspace_id=WS, agent_id=agent.id, session_id="s1",
                        message_id=msg_a.id, verdict="down", actor="mei", source="console",
                        comment="wrong"))
    db.add(Issue(workspace_id=WS, agent_id=agent.id, session_id="s2", message_id=99,
                 kind="unanswered", status="open", question="do you ship to SG",
                 answer="", opened_by="mei"))
    db.commit()
    found = admission.collect(db, WS, agent_id=agent.id)
    db.commit()
    assert {c.source for c in found} == {"feedback", "issue"}
    row = next(c for c in found if c.source == "feedback")
    assert row.question == "where is my order" and row.answer == "it shipped"
    # collecting twice does not duplicate
    again = admission.collect(db, WS, agent_id=agent.id)
    assert len(again) == 2 and db.query(AdmissionCandidate).count() == 2


def test_insight_clusters_carry_their_impact(db):
    agent = _agent(db)
    insights = {
        "failures": [{"name": "wrong tool", "subCategories": [{"rootCauses": [{
            "clusterId": "c1", "name": "no stock check", "affectedSessionCount": 12,
            "recommendation": "describe the tool", "affectedSessions": [{"sessionId": "s9"}],
        }]}]}],
        "userIntents": [{"clusterId": "i1", "name": "returns", "affectedSessionCount": 30,
                         "affectedSessions": [{"sessionId": "s8", "userMessages": ["refund?"]}]}],
    }
    rows = admission.collect_from_insights(db, WS, agent.id, insights)
    db.commit()
    assert {r.affected_sessions for r in rows} == {12, 30}
    out = admission.queue(db, WS, agent_id=agent.id)
    assert out["clusters"][0]["affected_sessions"] == 30
    assert out["counts"]["new"] == 2


def test_judged_wrong_candidates_rank_first(db):
    agent = _agent(db)
    low = AdmissionCandidate(workspace_id=WS, agent_id=agent.id, source="manual",
                             source_ref="a", affected_sessions=50)
    wrong = AdmissionCandidate(workspace_id=WS, agent_id=agent.id, source="manual",
                               source_ref="b", affected_sessions=1,
                               existing_judgement={"G1": "pass"})
    db.add_all([low, wrong])
    db.commit()
    out = admission.queue(db, WS, agent_id=agent.id)
    assert out["priority"][0] == wrong.id


def test_nearest_items_flags_duplicates(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    _golden(db, cset)
    hits = admission.nearest_items(db, WS, "q1")
    assert hits and hits[0]["match"] == "exact"
    assert admission.nearest_items(db, WS, " Q1 ")[0]["match"] == "normalized"
    assert admission.nearest_items(db, WS, "unrelated") == []


def test_admission_needs_a_human_answer_and_never_writes_holdout(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    parent = _golden(db, cset)
    splits = golden_svc.splits_of(db, parent)
    candidate = AdmissionCandidate(workspace_id=WS, agent_id=agent.id, source="feedback",
                                   source_ref="f1", question="do you ship to SG",
                                   answer="no idea", session_id="s1")
    db.add(candidate)
    db.commit()
    with pytest.raises(AppError) as exc:
        admission.admit(db, candidate, split_dataset=splits["holdout"],
                        expected_response="yes", expected_source="annotator",
                        criteria_ids=["G1"], case_tier="known_bad", actor="biz")
    assert exc.value.code == "golden.holdout_closed"
    with pytest.raises(AppError) as exc:
        admission.admit(db, candidate, split_dataset=splits["regression"],
                        expected_response="yes", expected_source="agent_observed",
                        criteria_ids=["G1"], case_tier="known_bad", actor="biz")
    assert exc.value.code == "admission.expected_source"
    with pytest.raises(AppError):
        admission.admit(db, candidate, split_dataset=splits["regression"],
                        expected_response="  ", expected_source="annotator",
                        criteria_ids=["G1"], case_tier="known_bad", actor="biz")
    item = admission.admit(db, candidate, split_dataset=splits["regression"],
                           expected_response="yes, 3–5 days", expected_source="annotator",
                           criteria_ids=["G1"], case_tier="known_bad", actor="biz")
    db.commit()
    meta = item["metadata"]["dlc"]
    assert meta["expected_source"] == "annotator" and meta["criteria_ids"] == ["G1"]
    assert candidate.status == "admitted"
    assert admission.queue(db, WS, agent_id=agent.id)["counts"]["admitted"] == 1


def test_a_candidate_that_cannot_be_redacted_is_refused(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    parent = _golden(db, cset)
    splits = golden_svc.splits_of(db, parent)
    candidate = AdmissionCandidate(workspace_id=WS, agent_id=agent.id, source="feedback",
                                   source_ref="f2", question="q", answer="a",
                                   redaction={"status": "blocked", "reason": "guardrail.blocked"})
    db.add(candidate)
    db.commit()
    with pytest.raises(AppError) as exc:
        admission.admit(db, candidate, split_dataset=splits["regression"],
                        expected_response="x", expected_source="annotator",
                        criteria_ids=[], case_tier="known_bad", actor="biz")
    assert exc.value.code == "admission.redaction_blocked"


def test_reject_needs_a_reason(db):
    agent = _agent(db)
    candidate = AdmissionCandidate(workspace_id=WS, agent_id=agent.id, source="manual",
                                   source_ref="m1")
    db.add(candidate)
    db.commit()
    with pytest.raises(AppError):
        admission.reject(db, candidate, actor="biz", note="  ")
    admission.reject(db, candidate, actor="biz", note="duplicate of a known issue")
    assert candidate.status == "rejected"


# ── watch ─────────────────────────────────────────────────────────────────────


def test_watch_schedule_and_run(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    _golden(db, cset)
    config = watch_svc.upsert(db, workspace_id=WS, agent_id=agent.id,
                              criteria_set_id=cset.id, dataset_id=None, every="daily",
                              at_hour=3, tz="UTC", repeats=2, enabled=True, actor="ops")
    db.commit()
    assert config.next_due_at is not None
    submitted = []

    def submit(**kw):
        run = EvalRun(workspace_id=WS, agent_id=agent.id, agent_name="a", status="queued",
                      split=kw["split"], repeats=kw["repeats"],
                      criteria_set_id=kw["criteria_set_id"])
        db.add(run)
        db.flush()
        submitted.append(kw)
        return run

    run_id = watch_svc.run_now(db, config, workspace_ctx=SimpleNamespace(), submit=submit)
    db.commit()
    assert submitted[0]["split"] == "regression" and submitted[0]["repeats"] == 2
    assert config.last_run_id == run_id and config.last_status == "queued"


def test_a_cost_ceiling_skips_the_scheduled_run(db, monkeypatch):
    agent = _agent(db)
    cset = _criteria(db, agent)
    _golden(db, cset)
    config = watch_svc.upsert(db, workspace_id=WS, agent_id=agent.id, criteria_set_id=cset.id,
                              dataset_id=None, every="daily", at_hour=3, tz="UTC", repeats=1,
                              enabled=True, actor="ops", max_cost_usd=0.001)
    db.commit()
    from app.dlc import cost as cost_svc

    monkeypatch.setattr(cost_svc, "estimate", lambda *a, **k: {"total_usd": 9.0})
    with pytest.raises(AppError) as exc:
        watch_svc.run_now(db, config, workspace_ctx=SimpleNamespace(),
                          submit=lambda **kw: None)
    assert exc.value.code == "watch.over_cost_ceiling"
    assert config.last_status == "skipped_cost"


def _run(db, agent_id, cset, rate, *, days_ago=0, version="2", redline_fail=0):
    run = EvalRun(workspace_id=WS, agent_id=agent_id, agent_name="a", status="completed",
                  criteria_set_id=cset.id, criteria_set_version=cset.version,
                  split="regression", agent_version=version,
                  created_at=datetime.now(UTC) - timedelta(days=days_ago))
    run.criteria_summary = {"criteria": {
        "G1": {"kind": "evaluator", "tier": "gate", "dimension": "quality", "rate": rate,
               "pass": int(rate * 10), "fail": 10 - int(rate * 10), "n": 10, "error": 0},
        "R1": {"kind": "evaluator", "tier": "redline", "dimension": "responsibility",
               "rate": 1.0, "pass": 10 - redline_fail, "fail": redline_fail, "n": 10,
               "error": 0},
    }}
    db.add(run)
    db.flush()
    return run


def test_count_alerts_page_immediately_and_quiet_is_reported(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    clean = watch_svc.alerts([_run(db, agent.id, cset, 1.0)])
    assert clean["firing"] == 0 and clean["quiet"] is True
    noisy = watch_svc.alerts([_run(db, agent.id, cset, 1.0, redline_fail=2)])
    assert noisy["count"][0]["severity"] == "page" and noisy["quiet"] is False


def test_score_alerts_wait_for_a_baseline(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    runs = [_run(db, agent.id, cset, 0.95, days_ago=10 - n) for n in range(4)]
    runs.append(_run(db, agent.id, cset, 0.70))
    thin = watch_svc.alerts(runs)
    assert thin["score"] == []  # fewer than 8 baseline points: stay quiet
    runs = [_run(db, agent.id, cset, 0.95, days_ago=20 - n) for n in range(9)]
    runs.append(_run(db, agent.id, cset, 0.70))
    ready = watch_svc.alerts(runs)
    assert ready["score"] and ready["score"][0]["dimension"] == "quality"
    assert ready["score"][0]["severity"] == "digest"


def test_distribution_alerts_name_the_component(db):
    out = watch_svc.alerts([], intents_before={"returns": 8, "orders": 92},
                           intents_after={"returns": 21, "orders": 79})
    assert out["distribution"] and "returns" in out["distribution"][0]["detail"]


def test_drift_triage_lists_judges_needing_recalibration(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    _run(db, agent.id, cset, 0.8, version="2")
    _run(db, agent.id, cset, 0.6, version="3")
    db.commit()
    out = watch_svc.drift_triage(db, WS, agent.id)
    assert out["needs_recalibration"] == ["G1"]  # judge never calibrated
    assert out["versions_changed"] is True
    assert "judge drift" in out["hint"]


# ── scheduler ─────────────────────────────────────────────────────────────────


def test_a_claim_is_single_writer_and_reclaimable(db):
    assert scheduler.claim(db, "t1", every=timedelta(seconds=0), worker="w1") is True
    assert scheduler.claim(db, "t1", every=timedelta(seconds=0), worker="w2") is False
    scheduler.release(db, "t1")
    assert scheduler.claim(db, "t1", every=timedelta(hours=1), worker="w2") is False  # too soon
    row = db.get(SchedulerClaim, "t1")
    row.last_done_at = datetime.now(UTC) - timedelta(hours=2)
    row.claimed_at = datetime.now(UTC) - timedelta(minutes=30)  # stale claim
    row.claimed_by = "dead"
    db.commit()
    assert scheduler.claim(db, "t1", every=timedelta(hours=1), worker="w3") is True


def test_the_tick_runs_every_task_and_survives_failure(db, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "TASKS", (
        ("t.ok", lambda session: calls.append("ok") or {"ok": True}, 0),
        ("t.boom", lambda session: (_ for _ in ()).throw(RuntimeError("nope")), 0),
    ))
    out = scheduler.tick(worker="test")
    assert out["t.ok"] == {"ok": True}
    assert "RuntimeError" in out["t.boom"]["error"]
    assert calls == ["ok"]


def test_expiry_sweep_expires_waivers(db):
    agent = _agent(db)
    db.add(Waiver(workspace_id=WS, agent_id=agent.id, criterion_key="G1", status="approved",
                  expires_on=datetime.now(UTC) - timedelta(days=1)))
    db.add(Waiver(workspace_id=WS, agent_id=agent.id, criterion_key="G2", status="approved",
                  expires_on=datetime.now(UTC) + timedelta(days=1)))
    db.commit()
    out = scheduler.sweep_expiries(db)
    assert out["waivers_expired"] == 1
    statuses = sorted(w.status for w in db.query(Waiver).all())
    assert statuses == ["approved", "expired"]


def test_due_watch_configs_are_run_by_the_tick(db, monkeypatch):
    agent = _agent(db)
    cset = _criteria(db, agent)
    _golden(db, cset)
    config = watch_svc.upsert(db, workspace_id=WS, agent_id=agent.id, criteria_set_id=cset.id,
                              dataset_id=None, every="daily", at_hour=3, tz="UTC", repeats=1,
                              enabled=True, actor="ops")
    config.next_due_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    monkeypatch.setattr(watch_svc, "run_now", lambda *a, **k: "run-1")
    monkeypatch.setattr("app.services.workspace.context_for_workspace",
                        lambda wid: SimpleNamespace(id=wid))
    out = scheduler.run_due_watches(db)
    assert out["started"] == [{"agent_id": agent.id, "run_id": "run-1"}]


def test_alert_evaluation_is_per_workspace(db, monkeypatch):
    seen = []
    monkeypatch.setattr("app.services.alerts.evaluate_rules",
                        lambda session, ctx, notify_transitions: seen.append(ctx.id) or
                        {"firing": 0})
    monkeypatch.setattr("app.services.workspace.context_for_workspace",
                        lambda wid: SimpleNamespace(id=wid))
    out = scheduler.evaluate_alerts(db)
    assert seen and out["workspaces"][0]["firing"] == 0
    assert {w.id for w in db.query(Workspace).all()} >= set(seen)


# ── deferred AWS teardown ──────────────────────────────────────────────────────


def _row(out, agent_id):
    """This agent's verdict — the sweep covers every deleted gated row at once."""
    return next(r["status"] for r in out["results"] if r["agent_id"] == agent_id)


def _deleted_gated(db, name="aw-gone"):
    agent = Agent(workspace_id=WS, name=name, method="harness", status="deleted",
                  spec={"name": name}, version="2", resource_id="h-gone",
                  endpoint_mode="live")
    db.add(agent)
    db.commit()
    return agent


def test_the_sweep_waits_while_an_endpoint_is_still_deleting(db, monkeypatch):
    """A harness endpoint can stay DELETING for minutes, so the delete request gives up
    and this finishes the job — it must not delete the harness while they are there."""
    from app.dlc import releases as release_svc

    agent = _deleted_gated(db)
    monkeypatch.setattr("app.services.workspace.context_for_workspace",
                        lambda wid: SimpleNamespace(id=wid))
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    monkeypatch.setattr(release_svc, "endpoint_status",
                        lambda a, c, name: "DELETING" if name == "candidate" else None)
    reissued = []
    monkeypatch.setattr(release_svc, "delete_endpoints",
                        lambda a, c, **kw: reissued.append(kw) or [])
    deleted = []
    monkeypatch.setattr("app.services.agentcore.harness.delete_harness",
                        lambda c, rid: deleted.append(rid))
    out = scheduler.sweep_deleted_resources(db)
    assert _row(out, agent.id) == "waiting"
    assert deleted == []  # the harness is NOT deleted while an endpoint lingers
    assert reissued == [{"timeout_s": 0}]  # re-issued, in case the delete never landed
    assert agent.resource_id == "h-gone"  # still to be retried


def test_the_sweep_deletes_the_harness_once_the_endpoints_are_gone(db, monkeypatch):
    from app.dlc import releases as release_svc

    agent = _deleted_gated(db)
    monkeypatch.setattr("app.services.workspace.context_for_workspace",
                        lambda wid: SimpleNamespace(id=wid))
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    monkeypatch.setattr(release_svc, "endpoint_status", lambda a, c, name: None)
    monkeypatch.setattr("app.services.agentcore.harness.get_harness",
                        lambda c, rid, version=None: {"harness": {"status": "READY"}})
    deleted = []
    monkeypatch.setattr("app.services.agentcore.harness.delete_harness",
                        lambda c, rid: deleted.append(rid))
    out = scheduler.sweep_deleted_resources(db)
    assert deleted == ["h-gone"]
    assert _row(out, agent.id) == "deleted"
    # the done-marker: `endpoint_mode` is the sweep's own filter, and `resource_id`
    # stays as the historical pointer every other deleted row keeps
    assert agent.endpoint_mode == "default"
    assert agent.resource_id == "h-gone"


def test_an_already_gone_resource_stops_being_retried(db, monkeypatch):
    from app.dlc import releases as release_svc

    agent = _deleted_gated(db)
    monkeypatch.setattr("app.services.workspace.context_for_workspace",
                        lambda wid: SimpleNamespace(id=wid))
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    monkeypatch.setattr(release_svc, "endpoint_status", lambda a, c, name: None)
    monkeypatch.setattr("app.services.agentcore.harness.get_harness",
                        lambda c, rid, version=None: {"harness": {"status": "READY"}})

    def gone(c, rid):
        raise type("ResourceNotFoundException", (Exception,), {})("already gone")

    monkeypatch.setattr("app.services.agentcore.harness.delete_harness", gone)
    scheduler.sweep_deleted_resources(db)
    assert agent.endpoint_mode == "default"


def test_an_ungated_deleted_agent_is_not_swept(db):
    agent = Agent(workspace_id=WS, name="aw-plain", method="harness", status="deleted",
                  spec={}, version="1", resource_id="h-plain", endpoint_mode="default")
    db.add(agent)
    db.commit()
    # the ordinary delete path already finished; nothing here to retry
    assert scheduler.sweep_deleted_resources(db)["checked"] == 0
    assert agent.resource_id == "h-plain"


def test_an_already_gone_harness_is_detected_by_asking_not_by_the_delete_error(db,
                                                                              monkeypatch):
    """AgentCore answers DeleteHarness on an already-gone harness with **AccessDenied**,
    not ResourceNotFound (observed on real AWS). Inferring "gone" from the delete error
    would either retry a vanished resource forever or swallow a real permission problem,
    so the sweep asks GetHarness first."""
    from app.dlc import releases as release_svc

    agent = _deleted_gated(db, name="aw-accessdenied")
    monkeypatch.setattr("app.services.workspace.context_for_workspace",
                        lambda wid: SimpleNamespace(id=wid))
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    monkeypatch.setattr(release_svc, "endpoint_status", lambda a, c, name: None)

    def not_found(c, rid, version=None):
        raise type("ResourceNotFoundException", (Exception,), {})("no such harness")

    denied = []

    def access_denied(c, rid):
        denied.append(rid)
        raise type("AccessDeniedException", (Exception,), {})("not authorized")

    monkeypatch.setattr("app.services.agentcore.harness.get_harness", not_found)
    monkeypatch.setattr("app.services.agentcore.harness.delete_harness", access_denied)
    out = scheduler.sweep_deleted_resources(db)
    assert _row(out, agent.id) == "deleted"
    assert denied == []  # never even attempted: GetHarness already said it is gone
    assert agent.endpoint_mode == "default"


def test_a_real_permission_failure_keeps_being_reported(db, monkeypatch):
    from app.dlc import releases as release_svc

    agent = _deleted_gated(db, name="aw-denied-real")
    monkeypatch.setattr("app.services.workspace.context_for_workspace",
                        lambda wid: SimpleNamespace(id=wid))
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    monkeypatch.setattr(release_svc, "endpoint_status", lambda a, c, name: None)
    monkeypatch.setattr("app.services.agentcore.harness.get_harness",
                        lambda c, rid, version=None: {"harness": {"status": "READY"}})

    def access_denied(c, rid):
        raise type("AccessDeniedException", (Exception,), {})("not authorized")

    monkeypatch.setattr("app.services.agentcore.harness.delete_harness", access_denied)
    out = scheduler.sweep_deleted_resources(db)
    assert _row(out, agent.id) == "error:AccessDeniedException"
    # still to be retried: the resource is really there and really undeletable
    assert agent.resource_id == "h-gone"
