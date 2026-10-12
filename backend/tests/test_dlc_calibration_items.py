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
