"""Calibration items built from a run show what an annotator needs, blind.

Annotators label the agent's reply against the reference answer. The items used to
carry the scenario id as the question and the judge's explanation as the "answer",
which hid the reply and showed the judge's reasoning during blind labelling.
"""

from types import SimpleNamespace

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset, EvalRun
from app.models.dlc import CriterionResult
from app.routers import dlc as dlc_router

WS = DEFAULT_WORKSPACE_ID


def test_items_carry_question_expected_and_the_agents_reply(monkeypatch):
    db = SessionLocal()
    try:
        ds = EvalDataset(workspace_id=WS, name="cal-items", kind="predefined", items=[
            {"scenario_id": "kg-12",
             "turns": [{"input": "能退多少？", "expected_response": "退 ¥980"}]},
        ])
        db.add(ds)
        db.flush()
        run = EvalRun(workspace_id=WS, agent_id="agent-x", agent_name="a", status="completed",
                      name="r", dataset_id=ds.id, session_ids=["sess-1"])
        db.add(run)
        db.flush()
        db.add(CriterionResult(workspace_id=WS, run_id=run.id, criterion_key="S2",
                               scenario_id="kg-12", attempt=1, session_id="sess-1",
                               evaluator_id="judge", level="trace", verdict="fail",
                               explanation="JUDGE REASONING"))
        db.commit()

        from app.services import observability

        monkeypatch.setattr(observability, "get_session_transcript", lambda *a, **k: {
            "transcript": {"turns": [{"role": "USER", "text": "能退多少？"},
                                     {"role": "ASSISTANT", "text": "可以退 ¥980"}]}})
        ws = SimpleNamespace(id=WS, context=SimpleNamespace(id=WS))
        [item] = dlc_router._items_from_run(db, ws, run, "S2")

        assert item["input"] == "能退多少？"
        assert item["expected"] == "退 ¥980"
        assert item["answer"] == "可以退 ¥980"
        assert "JUDGE REASONING" not in item["answer"]
        assert item["judge_explanation"] == "JUDGE REASONING"
        assert item["judge_label"] == "fail"
    finally:
        db.rollback()
        db.query(CriterionResult).filter(CriterionResult.scenario_id == "kg-12").delete()
        db.query(EvalRun).filter(EvalRun.name == "r", EvalRun.agent_id == "agent-x").delete()
        db.query(EvalDataset).filter(EvalDataset.name == "cal-items").delete()
        db.commit()
        db.close()


def _conversation(text):
    return [{"eventTimestamp": "2026-10-12T11:40:00", "payload": [
        {"conversational": {"role": "USER", "content": {"text": "能退多少？"}}},
        {"conversational": {"role": "ASSISTANT", "content": {"text": text}}},
    ]}]


def _seed(db, *, attempts, sessions):
    ds = EvalDataset(workspace_id=WS, name="cal-items", kind="predefined", items=[
        {"scenario_id": f"kg-{n}", "turns": [{"input": "能退多少？",
                                               "expected_response": "退 ¥980"}]}
        for n in range(1, len(sessions) + 1)
    ])
    db.add(ds)
    db.flush()
    run = EvalRun(workspace_id=WS, agent_id="agent-x", agent_name="a", status="completed",
                  name="r", dataset_id=ds.id, session_ids=sessions,
                  attempts=attempts(sessions))
    db.add(run)
    db.flush()
    for n, sid in enumerate(sessions, start=1):
        db.add(CriterionResult(workspace_id=WS, run_id=run.id, criterion_key="S2",
                               scenario_id=f"kg-{n}", attempt=1, session_id=sid,
                               evaluator_id="judge", level="trace", verdict="pass"))
    db.commit()
    return run


def _cleanup(db):
    from app.models.dlc import AnnotationTask

    db.rollback()
    db.query(CriterionResult).filter(CriterionResult.scenario_id.like("kg-%")).delete(
        synchronize_session=False)
    run_ids = [r.id for r in db.query(EvalRun).filter(EvalRun.agent_id == "agent-x")]
    db.query(AnnotationTask).filter(AnnotationTask.run_id.in_(run_ids)).delete(
        synchronize_session=False)
    db.query(EvalRun).filter(EvalRun.agent_id == "agent-x").delete()
    db.query(EvalDataset).filter(EvalDataset.name == "cal-items").delete()
    db.commit()
    db.close()


def _memory(monkeypatch, by_actor):
    from app.services import observability

    def fake_events(_ws, actor_id, session_id, max_results=20, memory_id=None):
        return _conversation(by_actor[(actor_id, session_id)]) \
            if (actor_id, session_id) in by_actor else []

    monkeypatch.setattr(observability.memory, "list_events", fake_events)


def test_items_of_a_per_session_actor_run_carry_the_agents_answers(monkeypatch):
    """Live: replays write memory under `<agent>__eval-<run>-<n>` while the reader
    looked under "default", so every calibration item's agent answer was empty."""
    from app.evaluation.service import eval_actor

    db = SessionLocal()
    try:
        run = _seed(db, sessions=["s-1", "s-2"], attempts=lambda sessions: [
            {"scenario_id": f"kg-{n}", "attempt": 1, "session_id": sid}
            for n, sid in enumerate(sessions, start=1)
        ])
        _memory(monkeypatch, {
            (eval_actor("agent-x", run.id, 1), "s-1"): "退 ¥980",
            (eval_actor("agent-x", run.id, 2), "s-2"): "退 ¥120",
        })
        ws = SimpleNamespace(id=WS, context=SimpleNamespace(id=WS))
        items = dlc_router._items_from_run(db, ws, run, "S2")
        assert [i["answer"] for i in items] == ["退 ¥980", "退 ¥120"]
    finally:
        _cleanup(db)


def test_items_of_an_old_default_actor_run_still_carry_answers(monkeypatch):
    db = SessionLocal()
    try:
        run = _seed(db, sessions=["s-1"], attempts=lambda _sessions: None)
        _memory(monkeypatch, {("default", "s-1"): "退 ¥980"})
        ws = SimpleNamespace(id=WS, context=SimpleNamespace(id=WS))
        [item] = dlc_router._items_from_run(db, ws, run, "S2")
        assert item["answer"] == "退 ¥980"
    finally:
        _cleanup(db)


def test_a_task_created_with_empty_answers_is_backfilled_once(monkeypatch, client):
    """A task created before the fix froze "" for every answer; opening it reads the
    replies again (once per item) — closed tasks included."""
    from app.evaluation.service import eval_actor
    from app.models.dlc import AnnotationTask

    db = SessionLocal()
    try:
        run = _seed(db, sessions=["s-1", "s-2"], attempts=lambda sessions: [
            {"scenario_id": f"kg-{n}", "attempt": 1, "session_id": sid}
            for n, sid in enumerate(sessions, start=1)
        ])
        task = AnnotationTask(workspace_id=WS, agent_id="agent-x", criterion_key="S2",
                              run_id=run.id, status="closed", annotators=["sme"], items=[
                                  {"ref": "kg-1#1", "session_id": "s-1", "input": "q",
                                   "answer": ""},
                                  {"ref": "kg-2#1", "session_id": "s-2", "input": "q",
                                   "answer": ""},
                              ])
        db.add(task)
        db.commit()
        reads: list[str] = []
        _memory(monkeypatch, {(eval_actor("agent-x", run.id, 1), "s-1"): "退 ¥980"})
        from app.services import observability

        inner = observability.memory.list_events

        def counted(ws, actor_id, session_id, **kwargs):
            reads.append(session_id)
            return inner(ws, actor_id, session_id, **kwargs)

        monkeypatch.setattr(observability.memory, "list_events", counted)
        body = client.get(f"/api/annotation-tasks/{task.id}").json()
        assert [i["answer"] for i in body["items"]] == ["退 ¥980", ""]
        first = len(reads)
        assert first > 0
        body = client.get(f"/api/annotation-tasks/{task.id}").json()
        assert [i["answer"] for i in body["items"]] == ["退 ¥980", ""]
        assert len(reads) == first  # nothing re-read on the second view
    finally:
        _cleanup(db)


def test_a_session_with_two_judged_traces_is_one_item(monkeypatch):
    """A trace-level judge scores every trace; a replayed session that holds two
    traces must still be one calibration item (one ref), its verdict collapsed the
    way the gate collapses a session: any fail fails it."""
    db = SessionLocal()
    try:
        ds = EvalDataset(workspace_id=WS, name="cal-items-2t", kind="predefined", items=[
            {"scenario_id": "kb-77", "turns": [{"input": "q", "expected_response": "e"}]},
            {"scenario_id": "kg-77", "turns": [{"input": "q2", "expected_response": "e2"}]},
        ])
        db.add(ds)
        db.flush()
        run = EvalRun(workspace_id=WS, agent_id="agent-x", agent_name="a", status="completed",
                      name="r2t", dataset_id=ds.id, session_ids=["s-a", "s-b"])
        db.add(run)
        db.flush()
        for sid, scen, unit, verdict, why in [("s-a", "kb-77", "t1", "pass", "first trace"),
                                              ("s-a", "kb-77", "t2", "fail", "second trace"),
                                              ("s-b", "kg-77", "t3", "pass", "only trace")]:
            db.add(CriterionResult(workspace_id=WS, run_id=run.id, criterion_key="S2",
                                   scenario_id=scen, attempt=1, session_id=sid, unit_ref=unit,
                                   evaluator_id="judge", level="trace", verdict=verdict,
                                   explanation=why))
        db.commit()
        monkeypatch.setattr(dlc_router, "_agent_reply", lambda *a, **k: "reply")
        ws = SimpleNamespace(id=WS, context=SimpleNamespace(id=WS))
        items = dlc_router._items_from_run(db, ws, run, "S2")

        assert [i["ref"] for i in items] == ["kb-77#1", "kg-77#1"]
        assert items[0]["judge_label"] == "fail"
        assert "first trace" in items[0]["judge_explanation"]
        assert "second trace" in items[0]["judge_explanation"]
        assert items[1]["judge_label"] == "pass"
    finally:
        db.rollback()
        db.query(CriterionResult).filter(
            CriterionResult.scenario_id.in_(["kb-77", "kg-77"])).delete()
        db.query(EvalRun).filter(EvalRun.name == "r2t").delete()
        db.query(EvalDataset).filter(EvalDataset.name == "cal-items-2t").delete()
        db.commit()
        db.close()


def test_reading_replies_stops_at_the_budget_and_leaves_the_rest_to_backfill(monkeypatch):
    """A stalled memory read must not hold the create request past the proxy's 60 s:
    once the budget is spent the remaining items keep an empty answer (filled on the
    first view by `_backfill_answers`)."""
    db = SessionLocal()
    try:
        ds = EvalDataset(workspace_id=WS, name="cal-items-budget", kind="predefined", items=[
            {"scenario_id": "kb-88", "turns": [{"input": "q", "expected_response": "e"}]},
            {"scenario_id": "kg-88", "turns": [{"input": "q2", "expected_response": "e2"}]},
        ])
        db.add(ds)
        db.flush()
        run = EvalRun(workspace_id=WS, agent_id="agent-x", agent_name="a", status="completed",
                      name="rbudget", dataset_id=ds.id, session_ids=["s-c", "s-d"])
        db.add(run)
        db.flush()
        for sid, scen in (("s-c", "kb-88"), ("s-d", "kg-88")):
            db.add(CriterionResult(workspace_id=WS, run_id=run.id, criterion_key="S2",
                                   scenario_id=scen, attempt=1, session_id=sid, unit_ref=sid,
                                   evaluator_id="judge", level="trace", verdict="pass"))
        db.commit()
        reads = []
        clock = {"now": 0.0}
        monkeypatch.setattr(dlc_router.time, "monotonic", lambda: clock["now"])

        def slow_reply(_db, _ws, _run, session_id):
            reads.append(session_id)
            clock["now"] += dlc_router.REPLY_BUDGET_S + 1  # this read stalled past the budget
            return "reply"

        monkeypatch.setattr(dlc_router, "_agent_reply", slow_reply)
        ws = SimpleNamespace(id=WS, context=SimpleNamespace(id=WS))
        items = dlc_router._items_from_run(db, ws, run, "S2")
        assert len(items) == 2
        assert reads == ["s-c"]
        assert [i["answer"] for i in items] == ["reply", ""]
    finally:
        db.rollback()
        db.query(CriterionResult).filter(
            CriterionResult.scenario_id.in_(["kb-88", "kg-88"])).delete()
        db.query(EvalRun).filter(EvalRun.name == "rbudget").delete()
        db.query(EvalDataset).filter(EvalDataset.name == "cal-items-budget").delete()
        db.commit()
        db.close()


def test_memory_calls_use_a_short_read_timeout_not_the_runtimes():
    from app.services import memory
    from app.services.agentcore import client as ac_client

    captured = {}

    class Ctx:
        def client(self, name, cache_token=None, config=None):
            captured.update(name=name, read=config.read_timeout,
                            attempts=config.retries["max_attempts"])
            return object()

    assert memory.data_client is ac_client.memory_data_client
    ac_client.memory_data_client(Ctx())
    assert captured["name"] == "bedrock-agentcore"
    assert captured["read"] <= 30 and captured["attempts"] == 2
