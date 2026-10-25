"""Creating a calibration task from a run: the refusals say what went wrong.

People paste a run's name where its id belongs and got a bare "run not found". The
codes are unchanged; `detail` now carries what was looked up (the run id, the run's
own agent, the criterion) so the console can explain the mix-up.
"""

from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from app.models.ledger import Agent
from tests.test_dlc_api import (  # noqa: F401 — fixtures
    ENG,
    admin,
    app,
    engineer,
)

WS = DEFAULT_WORKSPACE_ID


def _seed() -> tuple[str, str]:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=WS, name="api-cal-err", method="harness",
                      status="active", spec={"name": "api-cal-err"}, version="1",
                      resource_id="h-api", endpoint_mode="default", owner="engineer")
        db.add(agent)
        run = EvalRun(workspace_id=WS, agent_id="other-agent", agent_name="adp-order-query",
                      status="completed", name="adp-r1-calibration-v2-20261014")
        db.add(run)
        db.commit()
        return agent.id, run.id
    finally:
        db.close()


def _body(agent_id: str, run_id: str) -> dict:
    return {"agent_id": agent_id, "criterion_key": "R1", "run_id": run_id,
            "annotators": [ENG["username"], "root"]}


def test_a_run_name_is_not_found_and_the_detail_names_what_was_looked_up(
    engineer: TestClient,  # noqa: F811
):
    agent_id, _ = _seed()
    res = engineer.post("/api/annotation-tasks",
                        json=_body(agent_id, "adp-r1-calibration-v2-20261014"))
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "run.not_found"
    assert body["detail"] == {"run_id": "adp-r1-calibration-v2-20261014"}
    assert "not a run name" in body["message"]


def test_a_name_longer_than_any_id_still_reaches_run_not_found(engineer: TestClient):  # noqa: F811
    agent_id, _ = _seed()
    name = "release v7 regression calibration run for the order assistant"
    res = engineer.post("/api/annotation-tasks", json=_body(agent_id, name))
    assert res.status_code == 404 and res.json()["detail"] == {"run_id": name}
    # beside supplied items the id is stored as-is, so it is refused there too
    supplied = engineer.post("/api/annotation-tasks", json={
        **_body(agent_id, name),
        "items": [{"ref": "i0", "input": "q", "answer": "a", "judge_label": "pass"}]})
    assert supplied.status_code == 404 and supplied.json()["code"] == "run.not_found"


def test_another_agents_run_names_that_agent(engineer: TestClient):  # noqa: F811
    agent_id, run_id = _seed()
    res = engineer.post("/api/annotation-tasks", json=_body(agent_id, run_id))
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "annotation.run_mismatch"
    assert body["detail"] == {"run_id": run_id, "agent_id": "other-agent",
                              "agent_name": "adp-order-query"}


def test_a_run_without_the_criterions_verdicts_names_the_criterion(engineer: TestClient):  # noqa: F811
    agent_id, run_id = _seed()
    db = SessionLocal()
    try:
        db.get(EvalRun, run_id).agent_id = agent_id
        db.commit()
    finally:
        db.close()
    res = engineer.post("/api/annotation-tasks", json=_body(agent_id, run_id))
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "annotation.no_items"
    assert body["detail"] == {"run_id": run_id, "criterion_key": "R1"}
