"""The calibration run picker (calibration-run-picker §4.3): `GET /api/calibration/candidate-runs`.

The new-task dialog lists one agent's batch evaluator runs with the number of samples
each would give for the chosen criterion. The count uses the grouping the task itself
uses (one item per scenario + attempt), so what the picker shows is what the task gets.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from app.models.dlc import CriterionResult, ReleaseRecord
from app.models.ledger import Agent
from tests.test_dlc_api import (  # noqa: F401 — fixtures
    ENG,
    admin,
    app,
    engineer,
    owner,
)

WS = DEFAULT_WORKSPACE_ID
URL = "/api/calibration/candidate-runs"
T0 = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


def _agent(name: str) -> str:
    db = SessionLocal()
    try:
        row = Agent(workspace_id=WS, name=name, method="harness", status="active",
                    spec={"name": name}, version="1", resource_id="h-api",
                    endpoint_mode="default", owner="engineer")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _run(agent_id: str, name: str, *, minutes: int, status: str = "completed",
         split: str | None = None, mode: str = "evaluators",
         verdicts: dict[str, list[tuple[str, int]]] | None = None) -> str:
    """A run with `verdicts`: criterion key → (scenario, attempt) per verdict row."""
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=WS, agent_id=agent_id, agent_name="a", status=status,
                      name=name, mode=mode, split=split,
                      created_at=T0 + timedelta(minutes=minutes),
                      updated_at=T0 + timedelta(minutes=minutes + 5))
        db.add(run)
        db.flush()
        for key, units in (verdicts or {}).items():
            for scenario_id, attempt in units:
                db.add(CriterionResult(workspace_id=WS, run_id=run.id, criterion_key=key,
                                       scenario_id=scenario_id, attempt=attempt,
                                       session_id=f"{run.id}-{scenario_id}-{attempt}",
                                       evaluator_id="judge", level="trace", verdict="pass"))
        db.commit()
        return run.id
    finally:
        db.close()


def _seed() -> dict[str, str]:
    agent = _agent("api-picker")
    other = _agent("api-picker-other")
    ids = {"agent": agent, "other_agent": other}
    # two traces of one session collapse into one sample: 3 rows, 2 items
    ids["gate"] = _run(agent, "release v7 · regression", minutes=50, split="regression",
                       verdicts={"R1": [("s1", 1), ("s1", 1), ("s2", 1)]})
    ids["manual"] = _run(agent, "adp-r1-calibration-v2-20261014", minutes=40,
                         verdicts={"R1": [("s1", 1), ("s1", 2), ("s2", 1)], "R2": [("s1", 1)]})
    ids["no_r1"] = _run(agent, "dev-iteration-3", minutes=30, verdicts={"R2": [("s1", 1)]})
    ids["running"] = _run(agent, "still going", minutes=60, status="evaluating")
    ids["holdout"] = _run(agent, "release v7 · holdout", minutes=50, split="holdout",
                          verdicts={"R1": [("h1", 1)]})
    ids["insights"] = _run(agent, "clusters", minutes=20, mode="insights",
                           verdicts={"R1": [("s1", 1)]})
    ids["foreign"] = _run(other, "another agent's run", minutes=55,
                          verdicts={"R1": [("s1", 1)]})
    db = SessionLocal()
    try:
        record = ReleaseRecord(workspace_id=WS, agent_id=agent,
                               run_ids=[ids["gate"], ids["holdout"]])
        db.add(record)
        db.commit()
        ids["release"] = record.id
    finally:
        db.close()
    return ids


def _get(client: TestClient, **params):
    res = client.get(URL, params=params)
    assert res.status_code == 200, res.text
    return res.json()


def test_the_dropdown_lists_this_agents_eligible_runs_newest_first(engineer: TestClient):  # noqa: F811
    ids = _seed()
    body = _get(engineer, agent_id=ids["agent"], criterion_key="R1")
    assert [r["id"] for r in body["runs"]] == [ids["gate"], ids["manual"]]
    assert body["total"] == 2
    gate, manual = body["runs"]
    # the count is the task's sample count: scenario + attempt, traces collapsed
    assert (gate["items"], manual["items"]) == (2, 3)
    assert gate["release_id"] == ids["release"] and manual["release_id"] is None
    assert gate["eligible"] and gate["reason"] is None
    assert gate["finished_at"].startswith("2026-10-14T08:55")
    assert gate["name"] == "release v7 · regression"


def test_switching_the_criterion_recounts(engineer: TestClient):  # noqa: F811
    ids = _seed()
    body = _get(engineer, agent_id=ids["agent"], criterion_key="R2")
    assert [(r["id"], r["items"]) for r in body["runs"]] == [(ids["manual"], 1), (ids["no_r1"], 1)]


def test_the_full_list_greys_out_what_cannot_be_picked_and_says_why(engineer: TestClient):  # noqa: F811
    ids = _seed()
    body = _get(engineer, agent_id=ids["agent"], criterion_key="R1", only_eligible="false")
    got = {r["id"]: (r["eligible"], r["reason"], r["items"]) for r in body["runs"]}
    assert got == {
        ids["gate"]: (True, None, 2),
        ids["manual"]: (True, None, 3),
        ids["no_r1"]: (False, "no_results", 0),
        ids["running"]: (False, "not_completed", 0),
    }
    # the pickable ones lead, even when a newer run cannot be picked
    assert [r["eligible"] for r in body["runs"]] == [True, True, False, False]
    assert body["total"] == 4
    # never a holdout run (D-2), an insights run or another agent's run
    assert not {ids["holdout"], ids["insights"], ids["foreign"]} & set(got)


def test_search_matches_the_name_or_the_id(engineer: TestClient):  # noqa: F811
    ids = _seed()
    by_name = _get(engineer, agent_id=ids["agent"], criterion_key="R1",
                   q="adp-r1-calibration-v2-20261014")
    assert [r["id"] for r in by_name["runs"]] == [ids["manual"]]
    by_id = _get(engineer, agent_id=ids["agent"], criterion_key="R1", q=ids["gate"][:8].upper())
    assert [r["id"] for r in by_id["runs"]] == [ids["gate"]]
    # LIKE wildcards are literal
    assert _get(engineer, agent_id=ids["agent"], criterion_key="R1", q="%")["runs"] == []


def test_source_since_and_paging(engineer: TestClient):  # noqa: F811
    ids = _seed()
    agent = ids["agent"]
    assert [r["id"] for r in _get(engineer, agent_id=agent, criterion_key="R1",
                                  source="gate")["runs"]] == [ids["gate"]]
    assert [r["id"] for r in _get(engineer, agent_id=agent, criterion_key="R1",
                                  source="manual")["runs"]] == [ids["manual"]]
    since = (T0 + timedelta(minutes=50)).isoformat()
    assert [r["id"] for r in _get(engineer, agent_id=agent, criterion_key="R1",
                                  since=since)["runs"]] == [ids["gate"]]
    page = _get(engineer, agent_id=agent, criterion_key="R1", limit=1, offset=1)
    assert page["total"] == 2 and [r["id"] for r in page["runs"]] == [ids["manual"]]


def test_an_unknown_agent_is_not_found(engineer: TestClient):  # noqa: F811
    _seed()
    res = engineer.get(URL, params={"agent_id": "nope", "criterion_key": "R1"})
    assert res.status_code == 404 and res.json()["code"] == "agent.not_found"
    assert engineer.get(URL, params={"agent_id": "nope"}).status_code == 422


def test_it_takes_the_key_that_creates_the_task(owner: TestClient):  # noqa: F811
    ids = _seed()
    res = owner.get(URL, params={"agent_id": ids["agent"], "criterion_key": "R1"})
    assert res.status_code == 403


def test_the_count_shown_is_the_tasks_sample_count(engineer: TestClient, monkeypatch):  # noqa: F811
    from app.services import observability

    monkeypatch.setattr(observability, "get_session_transcript",
                        lambda *a, **k: {"transcript": {"turns": []}})
    ids = _seed()
    [picked, *_] = _get(engineer, agent_id=ids["agent"], criterion_key="R1",
                        source="manual")["runs"]
    res = engineer.post("/api/annotation-tasks", json={
        "agent_id": ids["agent"], "criterion_key": "R1", "run_id": picked["id"],
        "annotators": [ENG["username"], "root"]})
    assert res.status_code == 201, res.text
    assert res.json()["total"] == picked["items"] == 3


def test_a_runs_calibratable_criteria_leave_metrics_out(engineer: TestClient):  # noqa: F811
    ids = _seed()
    db = SessionLocal()
    try:
        run = db.get(EvalRun, ids["manual"])
        run.criteria_summary = {"criteria": {"R1": {"key": "R1", "kind": "evaluator"},
                                             "R2": {"key": "R2", "kind": "metric"}}}
        db.commit()
    finally:
        db.close()
    res = engineer.get(f"/api/calibration/run-criteria/{ids['manual']}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["criteria"] == [{"key": "R1", "items": 3}]
    assert body["agent_id"] == ids["agent"] and body["status"] == "completed"
    # no summary (an older run): every criterion with verdicts is offered
    res = engineer.get(f"/api/calibration/run-criteria/{ids['no_r1']}")
    assert res.json()["criteria"] == [{"key": "R2", "items": 1}]
    assert engineer.get("/api/calibration/run-criteria/nope").status_code == 404
