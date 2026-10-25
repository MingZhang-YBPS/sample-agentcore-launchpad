"""T18 — ledger spec snapshots: write on publish, diff, rollback and its refusals."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, Deployment, SpecSnapshot, Workspace
from app.services import snapshots as snap_service
from app.services import users as users_service

SPEC = {"name": "snap-agent", "method": "harness", "system_prompt": "Be brief."}


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    return launched


def _activate(agent_id: str) -> None:
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status = "active"
    agent.resource_id = "harness-xyz"
    agent.arn = "arn:aws:bedrock-agentcore:us-west-2:111:harness/xyz"
    db.commit()
    db.close()


def _create(client, spec=SPEC) -> str:
    agent_id = client.post("/api/agents", json=spec).json()["agent"]["id"]
    _activate(agent_id)
    return agent_id


def _redeploy(client, agent_id, spec):
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=spec)
    assert res.status_code == 202, res.text
    _activate(agent_id)
    return res


def test_snapshot_written_on_create_and_every_redeploy(client):
    agent_id = _create(client)
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."})
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose.", "max_iterations": 20})
    rows = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    assert [r["seq"] for r in rows] == [3, 2, 1]
    assert "spec" not in rows[0]
    assert all(r["created_by"] and r["deployment_id"] for r in rows)
    first = client.get(f"/api/agents/{agent_id}/snapshots/1").json()
    assert first["spec"]["system_prompt"] == "Be brief."
    assert client.get(f"/api/agents/{agent_id}/snapshots/9").json()["code"] == "snapshot.not_found"
    assert client.get("/api/agents/nope/snapshots").status_code == 404


def test_aws_version_is_stamped_when_the_deploy_finishes(client):
    from app.deployer.pipeline import _finish

    agent_id = _create(client)
    db = SessionLocal()
    dep = db.query(Deployment).filter(Deployment.agent_id == agent_id).one()
    agent = db.get(Agent, agent_id)
    agent.version = "3"
    db.commit()
    _finish(db, dep.job_id, dep.id, agent_id, None)
    assert db.query(SpecSnapshot).one().aws_version == "3"
    db.close()


def test_diff_is_field_level_and_human_readable(client):
    agent_id = _create(client)
    _redeploy(client, agent_id, {
        **SPEC,
        "system_prompt": "Be verbose.",
        "model_id": "global.other-model",
        "skills": ["s1"],
        "memory": {"short_term": False, "long_term": False},
    })
    diff = client.get(
        f"/api/agents/{agent_id}/snapshots/diff", params={"from_seq": 1, "to_seq": 2}
    ).json()
    by_field = {c["field"]: c for c in diff["changes"]}
    assert by_field["system_prompt"]["before"] == "Be brief."
    assert by_field["system_prompt"]["after"] == "Be verbose."
    assert by_field["system_prompt"]["group"] == "prompt"
    assert by_field["model_id"]["group"] == "model"
    assert by_field["skills"]["added"] == ["s1"] and by_field["skills"]["removed"] == []
    assert by_field["memory.short_term"]["kind"] == "changed"
    assert "name" not in by_field  # unchanged fields are omitted
    assert diff["from"]["seq"] == 1 and diff["to"]["seq"] == 2
    same = client.get(
        f"/api/agents/{agent_id}/snapshots/diff", params={"from_seq": 2, "to_seq": 2}
    ).json()
    assert same["changes"] == []


def test_diff_specs_marks_added_and_removed_fields():
    changes = snap_service.diff_specs({"a": 1, "tools": [1, 2]}, {"b": 2, "tools": [2, 3]})
    kinds = {c["field"]: c["kind"] for c in changes}
    assert kinds == {"a": "removed", "b": "added", "tools": "changed"}
    tools = next(c for c in changes if c["field"] == "tools")
    assert tools["added"] == [3] and tools["removed"] == [1]


def test_rollback_redeploys_the_old_spec_as_a_new_snapshot(client, no_real_deploy):
    from app.models.ledger import Job

    agent_id = _create(client)
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."})
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["agent"]["status"] == "deploying"
    assert body["agent"]["spec"]["system_prompt"] == "Be brief."
    assert no_real_deploy[-1] == body["job_id"]
    db = SessionLocal()
    assert db.get(Job, body["job_id"]).payload["mode"] == "update"  # never an AWS revert
    db.close()
    rows = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    assert [r["seq"] for r in rows] == [3, 2, 1]
    assert rows[0]["note"] == "rollback to #1"
    assert client.get(f"/api/agents/{agent_id}/snapshots/3").json()["spec"]["system_prompt"] \
        == "Be brief."


def test_rollback_refusals(client):
    # unknown snapshot / agent
    agent_id = _create(client)
    assert client.post(f"/api/agents/{agent_id}/snapshots/9/rollback").json()["code"] \
        == "snapshot.not_found"
    assert client.post("/api/agents/nope/snapshots/1/rollback").status_code == 404
    # in-flight deploy
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."})
    db = SessionLocal()
    db.get(Agent, agent_id).status = "deploying"
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 409 and res.json()["code"] == "agent.deploy_in_progress"
    # system-managed preset
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status = "active"
    agent.system_key = "architect"
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code in (400, 403, 409)
    assert res.json()["code"] == "agent.system_managed"
    # discovered runtimes are external
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.system_key = None
    agent.method = "discovered_runtime"
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.json()["code"] == "agent.redeploy_external"


def test_rollback_refused_when_a_converted_agent_would_change_baked_fields(client):
    converted = {
        **SPEC, "name": "conv-agent", "method": "zip_runtime",
        "code_bundle": {"main.py": "print(1)\n"},
        "source_harness": {"agent_id": "src", "harness_arn": "arn:aws:x:1:1:harness/h"},
    }
    agent_id = _create(client, converted)
    # an older snapshot whose baked prompt differs from the live spec
    db = SessionLocal()
    old = snap_service.get_snapshot(db, agent_id, 1)
    old.spec = {**old.spec, "system_prompt": "An older baked prompt."}
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 400 and res.json()["code"] == "agent.converted_locked"
    assert res.json()["detail"]["fields"] == ["system_prompt"]


def test_rollback_of_a_snapshot_that_no_longer_validates_is_409(client):
    agent_id = _create(client)
    db = SessionLocal()
    snap_service.get_snapshot(db, agent_id, 1).spec = {"name": "x"}
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 409 and res.json()["code"] == "snapshot.spec_invalid"


def test_snapshots_do_not_cross_workspaces(client):
    agent_id = _create(client)
    db = SessionLocal()
    db.add(Workspace(id="other", name="Other", account_id="222233334444", region="us-east-1"))
    db.commit()
    db.close()
    res = client.get(f"/api/agents/{agent_id}/snapshots", headers={"X-Workspace": "other"})
    assert res.status_code == 404


def test_rollback_is_refused_for_members_on_prod(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), client=("127.0.0.1", 4321)) as member:
            creds = {"username": "snap-member", "email": "snap-member@acme-corp.com",
                     "password": "sufficient-pass"}
            assert member.post("/api/auth/register", json=creds).status_code == 201
            db = SessionLocal()
            user = users_service.find_by_username(db, "snap-member")
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.get(Workspace, DEFAULT_WORKSPACE_ID).tier = "prod"
            db.commit()
            db.close()
            login = member.post(
                "/api/auth/login",
                json={"username": "snap-member", "password": "sufficient-pass"},
            )
            assert login.status_code == 200
            res = member.post("/api/agents/x/snapshots/1/rollback")
            assert res.status_code == 403
            assert res.json()["code"] == "workspace.prod_protected"
            # reads stay open
            assert member.get("/api/agents/x/snapshots").status_code == 404
    finally:
        db = SessionLocal()
        db.get(Workspace, DEFAULT_WORKSPACE_ID).tier = "dev"
        db.commit()
        db.close()
        get_settings.cache_clear()
