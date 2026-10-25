"""Errors that say where to go next (storyline-ux §4, guided-console §3.1 / §3.5).

Asserted: ``AppError(next=…, needs=…)`` reaches the console envelope; the public
``/v1`` API and the ``/share`` pages never carry either field; and the first batch of
Agent-DLC error codes (release.*, golden.*, criteria.*, admission.*, waiver.*,
calibration.*) name their next action with the parameters the console needs —
table-driven over the real service functions, plus one end to end over HTTP.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, envelope, next_step, register_error_handlers
from app.core.project_scope import project_context
from app.dlc import criteria as criteria_svc
from app.dlc import golden as golden_svc
from app.dlc import releases as release_svc
from app.evaluation.models import EvalDataset
from app.models.dlc import CriteriaSet, ReleaseRecord, Waiver
from app.models.ledger import Agent
from tests.conftest import TEST_PROJECT

GUIDED = AppError("release.no_criteria", "publish a criteria set first", status_code=409,
                  next=[next_step("criteria.publish", agent_id="ag-1"),
                        next_step("doc.flywheel_evaluate")],
                  needs="perm:criteria.manage")


def _client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    for path in ("/api/agents/x/release/evaluate", "/v1/agents/x/invoke", "/share/tok",
                 "/share/annotate/tok"):
        app.add_api_route(path, lambda: (_ for _ in ()).throw(GUIDED), methods=["GET"])
    return TestClient(app, raise_server_exceptions=False)


def test_next_step_drops_empty_params():
    assert next_step("golden.open", agent_id="a", dataset_id=None) == {
        "action": "golden.open", "params": {"agent_id": "a"}}
    assert next_step("agent.create") == {"action": "agent.create", "params": {}}


def test_the_envelope_carries_next_and_needs_only_when_given():
    assert envelope("x.y", "m") == {"code": "x.y", "message": "m", "detail": None}
    body = envelope("x.y", "m", next=[next_step("agent.create")], needs="perm:golden.admit")
    assert body["next"] == [{"action": "agent.create", "params": {}}]
    assert body["needs"] == "perm:golden.admit"


def test_the_console_handler_outputs_next_and_needs():
    res = _client().get("/api/agents/x/release/evaluate")
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "release.no_criteria"
    assert body["next"][0] == {"action": "criteria.publish", "params": {"agent_id": "ag-1"}}
    assert body["next"][1]["action"] == "doc.flywheel_evaluate"
    assert body["needs"] == "perm:criteria.manage"


@pytest.mark.parametrize("path", ["/v1/agents/x/invoke", "/share/tok", "/share/annotate/tok"])
def test_public_surfaces_never_output_them(path):
    res = _client().get(path)
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "release.no_criteria"
    assert "next" not in body and "needs" not in body


def test_an_error_without_guidance_keeps_the_old_envelope():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api/boom")
    def boom():
        raise AppError("agent.invalid_state", "nope", {"a": 1})

    res = TestClient(app).get("/api/boom")
    assert res.json() == {"code": "agent.invalid_state", "message": "nope", "detail": {"a": 1}}


# ── the first batch of Agent-DLC codes (guided-console §3.5) ─────────────────────


def _agent(**fields) -> Agent:
    base = {"id": "ag-1", "workspace_id": DEFAULT_WORKSPACE_ID, "name": "a", "method": "harness",
            "status": "active", "spec": {}, "endpoint_mode": "default"}
    return Agent(**{**base, **fields})


def _raises(fn) -> AppError:
    with pytest.raises(AppError) as info:
        fn()
    return info.value


def _published(**fields) -> CriteriaSet:
    base = {"id": "cs-1", "lineage_id": "lin-1", "kind": "agent", "agent_id": "ag-1",
            "name": "n", "version": 1, "status": "published", "updated_by": "ann",
            "created_by": "ann"}
    return CriteriaSet(**{**base, **fields})


def _split(split: str, items=None, parent: str = "gs-1") -> EvalDataset:
    return EvalDataset(id=f"{parent}-{split}", name=split, role="golden", split=split,
                       split_of=parent, items=items or [])


CASES = [
    # (code, raise it, expected first next action or None, expected params, needs)
    ("criteria.agent_required",
     lambda: criteria_svc.create_set(None, DEFAULT_WORKSPACE_ID, kind="agent", name="n", actor="a"),
     "agent.create", {}, None),
    ("criteria.sign_unpublished",
     lambda: criteria_svc.sign(None, _published(status="draft"), actor="bob", note="",
                               is_admin=False),
     "criteria.publish", {"agent_id": "ag-1", "lineage_id": "lin-1"}, None),
    ("criteria.self_sign",
     lambda: criteria_svc.sign(None, _published(), actor="ann", note="", is_admin=False),
     None, None, "perm:criteria.sign"),
    ("release.gate_not_passed",
     lambda: release_svc.sign(None, ReleaseRecord(gate_report={"verdict": "FAIL"},
                                                  decision="pending"), _agent(), None,
                              actor="ops", note=""),
     "gate.open", {"agent_id": "ag-1"}, None),
    ("release.self_sign",
     lambda: release_svc.sign(None, ReleaseRecord(gate_report={"verdict": "PASS"},
                                                  decision="pending", requested_by="dev"),
                              _agent(), None, actor="dev", note=""),
     None, None, "perm:release.sign"),
    ("release.not_live",
     lambda: release_svc.rollback(None, _agent(), None, actor="ops"),
     "release.migrate", {"agent_id": "ag-1"}, None),
    ("release.not_gateable",
     lambda: release_svc.migrate_agent(None, _agent(resource_id=None), None, actor="ops"),
     "agent.open", {"agent_id": "ag-1"}, None),
    ("release.no_criteria",
     lambda: release_svc._gate_plan(None, ReleaseRecord(criteria_set_id=None), _agent()),
     "criteria.publish", {"agent_id": "ag-1"}, None),
    ("waiver.self_approve",
     lambda: release_svc.decide_waiver(None, Waiver(status="requested", requested_by="dev"),
                                       actor="dev", approve=True),
     None, None, "perm:waiver.approve"),
    ("golden.holdout_closed",
     lambda: golden_svc.add_items(None, _split("holdout"), [], actor="a"),
     "admission.open", {}, None),
    ("golden.holdout_closed",
     lambda: golden_svc.move_item(None, EvalDataset(id="gs-1"), "s1", to="holdout", actor="a"),
     "golden.open", {"dataset_id": "gs-1"}, None),
    ("golden.duplicate_scenario",
     lambda: golden_svc._prepare(_split("dev", [{"scenario_id": "s1"}]),
                                 [{"scenario_id": "s1"}], actor="a", origin="manual"),
     "golden.open", {"dataset_id": "gs-1"}, None),
    ("golden.bad_case_tier",
     lambda: golden_svc._prepare(_split("dev"), [{"scenario_id": "s2", "metadata": {
         "dlc": {"case_tier": "nope"}}}], actor="a", origin="manual"),
     "golden.open", {"dataset_id": "gs-1"}, None),
]


@pytest.mark.parametrize(("code", "fn", "action", "params", "needs"), CASES,
                         ids=[f"{c[0]}#{i}" for i, c in enumerate(CASES)])
def test_first_batch_codes_name_their_next_step(code, fn, action, params, needs):
    err = _raises(fn)
    assert err.code == code
    if action is None:
        assert not err.next
    else:
        assert err.next and err.next[0]["action"] == action
        assert err.next[0]["params"] == params
    assert err.needs == needs


def _seed_db_rows():
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="gate-me", method="harness",
                          status="active", resource_id="h-1", version="2", spec={})
            db.add(agent)
            db.flush()
            draft = CriteriaSet(workspace_id=DEFAULT_WORKSPACE_ID, lineage_id="lin-d",
                                kind="agent", agent_id=agent.id, name="n", version=1,
                                status="draft")
            db.add(draft)
            db.add(ReleaseRecord(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
                                 candidate_version="3", decision="pending"))
            db.commit()
            return agent.id
    finally:
        db.close()


def test_criteria_draft_exists_opens_the_draft():
    agent_id = _seed_db_rows()
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            err = _raises(lambda: criteria_svc.new_version(
                db, DEFAULT_WORKSPACE_ID, "lin-d", actor="a"))
    finally:
        db.close()
    assert err.code == "criteria.draft_exists"
    assert err.next == [{"action": "criteria.open_draft",
                         "params": {"agent_id": agent_id, "lineage_id": "lin-d"}}]


def test_over_http_a_gate_without_criteria_points_at_publishing_them(client):
    agent_id = _seed_db_rows()
    res = client.post(f"/api/agents/{agent_id}/release/evaluate", json={},
                      headers={"X-Project": TEST_PROJECT})
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "release.no_criteria"
    assert body["next"] == [{"action": "criteria.publish", "params": {"agent_id": agent_id}}]


def test_admission_without_a_golden_set_points_at_creating_one(client):
    from app.models.dlc import AdmissionCandidate

    agent_id = _seed_db_rows()
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            row = AdmissionCandidate(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                                     source="feedback", source_ref="1")
            db.add(row)
            db.commit()
            cid = row.id
    finally:
        db.close()
    res = client.post(f"/api/admission/{cid}/admit", headers={"X-Project": TEST_PROJECT},
                      json={"split": "dev", "expected_response": "x",
                            "expected_source": "annotator", "criteria_ids": [],
                            "case_tier": "known_good"})
    assert res.status_code == 409, res.text
    assert res.json()["next"][0] == {"action": "golden.create", "params": {"agent_id": agent_id}}
