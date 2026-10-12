"""T32 - environment comparison and drift detection."""

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, Deployment, Workspace
from app.routers import environments as environments_router
from app.services import environments as env_service
from app.services import users as users_service
from app.services.promotion import bundle_digest

SPEC = {"name": "fleet-agent", "system_prompt": "v1", "model_id": "m"}


def add_workspace(ws_id: str, tier: str) -> None:
    db = SessionLocal()
    try:
        if db.get(Workspace, ws_id) is None:
            db.add(Workspace(id=ws_id, name=ws_id.title(), account_id=f"7{len(ws_id):011d}",
                             region="us-west-2", bootstrap_status="ready", tier=tier))
            db.commit()
    finally:
        db.close()


def add_agent(ws_id, *, name="fleet-agent", spec=None, method="zip_runtime", version="2",
              status="active", resource_id="rt-1", image=None) -> str:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=ws_id, name=name, method=method, status=status,
                      spec=dict(spec or SPEC), version=version, resource_id=resource_id,
                      arn="arn:x")
        db.add(agent)
        db.flush()
        db.add(Deployment(workspace_id=ws_id, agent_id=agent.id, status="succeeded",
                          image_digest=image))
        db.commit()
        return agent.id
    finally:
        db.close()


@pytest.fixture
def client():
    return TestClient(create_app())


# ── compare ───────────────────────────────────────────────────────────────


def test_compare_shows_each_environment_and_flags_the_one_that_differs(client):
    add_workspace("staging", "staging")
    add_workspace("prod", "prod")
    add_agent(DEFAULT_WORKSPACE_ID, version="4")
    add_agent("staging", version="7")
    add_agent("prod", spec={**SPEC, "system_prompt": "v0 - older"}, version="2")

    body = client.get("/api/environments/compare", params={"agent": "fleet-agent"}).json()
    rows = {r["workspace"]["id"]: r for r in body["environments"]}
    assert [r["workspace"]["id"] for r in body["environments"]] == [
        DEFAULT_WORKSPACE_ID, "staging", "prod"]  # dev -> staging -> prod
    assert body["reference_workspace"] == "prod"
    assert rows["prod"]["vs_reference"] == "reference"
    assert rows[DEFAULT_WORKSPACE_ID]["vs_reference"] == "differs"
    assert rows["staging"]["vs_reference"] == "differs"
    assert rows[DEFAULT_WORKSPACE_ID]["current"] is True and rows["prod"]["current"] is False
    assert rows["staging"]["agent"]["version"] == "7"
    assert rows["staging"]["agent"]["last_deploy"]["status"] == "succeeded"
    assert body["summary"] == {"present": 3, "distinct_spec_digests": 2, "aligned": False}
    # the digest is the bundle helper's, not a second definition
    assert rows["staging"]["agent"]["spec_digest"] == bundle_digest(
        agent_name="fleet-agent", method="zip_runtime", spec=SPEC, artifact={})


def test_identical_specs_are_aligned_even_at_different_aws_versions(client):
    add_workspace("prod", "prod")
    add_agent(DEFAULT_WORKSPACE_ID, version="9")
    add_agent("prod", version="1")
    body = client.get("/api/environments/compare", params={"agent": "fleet-agent"}).json()
    assert body["summary"]["aligned"] is True
    assert {r["vs_reference"] for r in body["environments"]} == {"same", "reference"}


def test_compare_marks_missing_environments_and_ignores_deleted_agents(client):
    add_workspace("prod", "prod")
    add_agent(DEFAULT_WORKSPACE_ID)
    add_agent("prod", status="deleted")
    body = client.get("/api/environments/compare", params={"agent": "fleet-agent"}).json()
    rows = {r["workspace"]["id"]: r for r in body["environments"]}
    assert rows["prod"]["present"] is False and rows["prod"]["vs_reference"] == "absent"
    assert body["reference_workspace"] == DEFAULT_WORKSPACE_ID
    assert client.get("/api/environments/compare").status_code == 422


def test_a_member_only_sees_the_workspaces_they_are_granted():
    from tests.test_promotion import _member  # reuse the session helper

    add_workspace("prod", "prod")
    add_agent(DEFAULT_WORKSPACE_ID)
    add_agent("prod", spec={**SPEC, "system_prompt": "secret prod prompt"})
    import os

    os.environ["LAUNCHPAD_AUTH_USERNAME"] = "env-admin"
    os.environ["LAUNCHPAD_AUTH_PASSWORD"] = "s3cret-pass"
    get_settings.cache_clear()
    try:
        member = _member(
            create_app(),
            {"username": "viewer", "email": "viewer@acme-corp.com", "password": "sufficient-pass"},
            users_service.ROLE_MEMBER,
            target_grant=False,  # granted on `default` only — the point of this test
        )
        body = member.get("/api/environments/compare", params={"agent": "fleet-agent"}).json()
        assert [r["workspace"]["id"] for r in body["environments"]] == [DEFAULT_WORKSPACE_ID]
        assert "prod" not in str(body)
        member.__exit__(None, None, None)
    finally:
        os.environ.pop("LAUNCHPAD_AUTH_USERNAME")
        os.environ.pop("LAUNCHPAD_AUTH_PASSWORD")
        get_settings.cache_clear()


# ── drift ─────────────────────────────────────────────────────────────────


class Control:
    """A stand-in for the bedrock-agentcore-control client, keyed by resource id."""

    def __init__(self, runtimes=None, harnesses=None):
        self.runtimes = runtimes or {}
        self.harnesses = harnesses or {}

    def get_agent_runtime(self, agentRuntimeId):
        return self._get(self.runtimes, agentRuntimeId)

    def get_harness(self, harnessId):
        return {"harness": self._get(self.harnesses, harnessId)}

    @staticmethod
    def _get(table, key):
        found = table[key]
        if isinstance(found, Exception):
            raise found
        return found


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "GetAgentRuntime")


@pytest.fixture
def stub(monkeypatch):
    def install(control):
        monkeypatch.setattr(environments_router, "control_client", lambda _ctx: control)

    return install


def drift(client, **params):
    res = client.get("/api/environments/drift", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def by_name(body):
    return {a["name"]: a for a in body["agents"]}


def test_matching_aws_state_is_in_sync(client, stub):
    add_agent(DEFAULT_WORKSPACE_ID, resource_id="rt-ok", version="2")
    stub(Control(runtimes={"rt-ok": {"status": "READY", "agentRuntimeVersion": "2"}}))
    body = drift(client)
    assert body["state"] == "in_sync" and body["counts"] == {"in_sync": 1, "drift": 0, "unknown": 0}


def test_a_console_edit_shows_up_as_a_version_change(client, stub):
    add_agent(DEFAULT_WORKSPACE_ID, resource_id="rt-1", version="2")
    stub(Control(runtimes={"rt-1": {"status": "READY", "agentRuntimeVersion": "5"}}))
    body = drift(client)
    agent = by_name(body)["fleet-agent"]
    assert body["state"] == "drift" and agent["state"] == "drift"
    assert agent["findings"] == [{"code": "version_changed", "expected": "2", "observed": "5"}]


def test_missing_and_unhealthy_resources_are_drift(client, stub):
    add_agent(DEFAULT_WORKSPACE_ID, name="gone", resource_id="rt-gone")
    add_agent(DEFAULT_WORKSPACE_ID, name="sick", resource_id="rt-sick")
    add_agent(DEFAULT_WORKSPACE_ID, name="h", method="harness", resource_id="h-1", version="3")
    stub(Control(
        runtimes={"rt-gone": error("ResourceNotFoundException"),
                  "rt-sick": {"status": "UPDATE_FAILED", "agentRuntimeVersion": "2"}},
        harnesses={"h-1": {"status": "READY", "harnessVersion": "3"}},
    ))
    agents = by_name(drift(client))
    assert agents["gone"]["findings"][0]["code"] == "resource_missing"
    assert agents["sick"]["findings"][0] == {
        "code": "unhealthy", "expected": "READY", "observed": "UPDATE_FAILED"}
    assert agents["h"]["state"] == "in_sync"


@pytest.mark.parametrize(
    "answer,reason",
    [
        (error("ThrottlingException"), "unreadable"),
        (error("AccessDeniedException"), "unreadable"),
        (RuntimeError("boom"), "unreadable"),
        ({}, "no_status_reported"),
        ({"status": "UPDATING", "agentRuntimeVersion": "3"}, "transitioning"),
        ({"status": "READY"}, "version_not_comparable"),
    ],
)
def test_anything_we_cannot_judge_is_unknown_never_in_sync(client, stub, answer, reason):
    add_agent(DEFAULT_WORKSPACE_ID, resource_id="rt-1", version="2")
    stub(Control(runtimes={"rt-1": answer}))
    body = drift(client)
    agent = by_name(body)["fleet-agent"]
    assert agent["state"] == "unknown" and agent["reason"] == reason
    assert body["state"] == "unknown" and body["counts"]["in_sync"] == 0


def test_a_running_canary_explains_a_newer_version(client, stub):
    from app.optimization.models import RuntimeCanary

    agent_id = add_agent(DEFAULT_WORKSPACE_ID, resource_id="rt-1", version="2")
    db = SessionLocal()
    try:
        db.add(RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="c",
                             champion_agent_id=agent_id, champion_agent_name="fleet-agent",
                             challenger_agent_id="x", challenger_agent_name="y",
                             status="running"))
        db.commit()
    finally:
        db.close()
    stub(Control(runtimes={"rt-1": {"status": "READY", "agentRuntimeVersion": "3"}}))
    agent = by_name(drift(client))["fleet-agent"]
    assert agent["state"] == "unknown" and agent["reason"] == "canary_active"


def test_one_bad_read_does_not_hide_the_drift_next_to_it(client, stub):
    add_agent(DEFAULT_WORKSPACE_ID, name="a-drifted", resource_id="rt-a", version="1")
    add_agent(DEFAULT_WORKSPACE_ID, name="b-broken", resource_id="rt-b")
    stub(Control(runtimes={"rt-a": {"status": "READY", "agentRuntimeVersion": "9"},
                           "rt-b": RuntimeError("x")}))
    body = drift(client)
    assert body["state"] == "drift" and body["counts"] == {"in_sync": 0, "drift": 1, "unknown": 1}


def test_drift_covers_only_this_workspaces_live_agents(client, stub):
    add_workspace("prod", "prod")
    add_agent("prod", name="elsewhere", resource_id="rt-p")
    add_agent(DEFAULT_WORKSPACE_ID, name="draft", status="draft", resource_id=None)
    add_agent(DEFAULT_WORKSPACE_ID, name="mine", resource_id="rt-m")
    stub(Control(runtimes={"rt-m": {"status": "READY", "agentRuntimeVersion": "2"}}))
    body = drift(client)
    assert list(by_name(body)) == ["mine"]
    assert drift(client, agent="nope")["checked"] == 0
    assert drift(client, agent="nope")["state"] == "in_sync"  # nothing to disagree with


def test_the_limit_is_reported_rather_than_silent():
    db = SessionLocal()
    try:
        for i in range(3):
            add_agent(DEFAULT_WORKSPACE_ID, name=f"n{i}", resource_id=f"r{i}")
        body = env_service.detect_drift(
            db, workspace_id=DEFAULT_WORKSPACE_ID, control=Control(runtimes={
                f"r{i}": {"status": "READY", "agentRuntimeVersion": "2"} for i in range(3)}),
            limit=2,
        )
    finally:
        db.close()
    assert body["truncated"] is True and body["checked"] == 2
