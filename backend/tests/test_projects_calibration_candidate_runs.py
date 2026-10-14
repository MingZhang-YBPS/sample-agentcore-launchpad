"""The calibration run picker stays inside the caller's project: another project's agent
answers 404 and its runs never appear."""

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.evaluation.models import EvalRun
from app.models.dlc import CriterionResult
from tests.test_projects_release_dlc import (  # noqa: F401 — fixtures
    _agent,
    _alpha_member,
    _project,
    app,
)

URL = "/api/calibration/candidate-runs"


def _run(agent_id: str, pid: str) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name="a",
                          status="completed", name=f"run-{pid}", mode="evaluators")
            db.add(run)
            db.flush()
            db.add(CriterionResult(workspace_id=DEFAULT_WORKSPACE_ID, run_id=run.id,
                                   criterion_key="R1", scenario_id="s1", attempt=1,
                                   session_id="sess", verdict="pass"))
            db.commit()
            return run.id
    finally:
        db.close()


def test_another_projects_agent_is_not_found_and_its_runs_never_show(app):  # noqa: F811
    _project("alpha")
    theirs = _agent("their-picker", "default")
    mine = _agent("my-picker", "alpha")
    _run(theirs, "default")
    my_run = _run(mine, "alpha")
    client = _alpha_member(app, "pia")
    res = client.get(URL, params={"agent_id": theirs, "criterion_key": "R1"})
    assert res.status_code == 404, res.text
    res = client.get(URL, params={"agent_id": mine, "criterion_key": "R1"})
    assert res.status_code == 200, res.text
    assert [r["id"] for r in res.json()["runs"]] == [my_run]
