"""Deploying with network profiles (design projects §12.6, §12.11, §12.13; V12-deploy):
every method resolves `spec.network` into the runtime's networkConfiguration (no
hard-coded PUBLIC left in `runtime.py`), the Harness carries it in its backing-runtime
environment, the deploy gate refuses a forbidden PUBLIC agent or a profile whose
preflight blocks THIS agent before any job exists, a network change on re-publish is
an ordinary update, and `GET /api/agents/{id}/network` reports the placement and the
ENIs through an injected reader. No AWS call is made: EC2 and AgentCore are fakes."""

import copy
import inspect
import re

import pytest

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.deployer import pipeline
from app.deployer.harness import build_create_params
from app.deployer.network import NETWORK_FROM_KEY, spec_vpc
from app.deployer.pipeline import StageContext, network_gate
from app.models.ledger import Agent, Job
from app.models.network import NetworkProfile
from app.schemas.agent import AgentSpec
from app.services import agent_network
from app.services import network_enforcement as enforcement
from app.services import network_profiles as service
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt
from tests.conftest import TEST_PROJECT, client_error, ws_ctx
from tests.test_network_profile_check import BODY, SG, SUBNETS, FakeEc2, _vpc
from tests.test_project_scope import (
    _login,
    _project,
    _user,
    app,  # noqa: F401 — the fixture
)

VPC_CONFIG = {"networkMode": "VPC",
              "networkModeConfig": {"subnets": SUBNETS, "securityGroups": [SG]}}
PUBLIC = {"networkMode": "PUBLIC"}


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    yield launched


@pytest.fixture
def ec2(monkeypatch):
    """The deploy-time preflight reads this fake (a VPC that passes every check)."""
    fake = FakeEc2(_vpc())
    previous = pipeline.set_preflight_ec2(lambda _ws: fake)
    yield fake
    pipeline.set_preflight_ec2(previous)


def _profile(name: str = "core-vpc", *, status: str = "ok", project: str | None = TEST_PROJECT,
             workspace: str = DEFAULT_WORKSPACE_ID) -> str:
    db = SessionLocal()
    try:
        pid = service.create_profile(db, workspace, {**BODY, "name": name}, "operator")["id"]
        db.get(NetworkProfile, pid).status = status
        db.commit()
        if project is not None:
            service.set_project_profiles(db, workspace, project, [pid], None)
        return pid
    finally:
        db.close()


def _forbid_public() -> None:
    db = SessionLocal()
    try:
        enforcement.set_network_settings(db, DEFAULT_WORKSPACE_ID,
                                         {enforcement.PUBLIC_ALLOWED_KEY: False})
    finally:
        db.close()


def _agent_row(spec: AgentSpec, **over) -> str:
    db = SessionLocal()
    try:
        row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=spec.name, method=spec.method,
                    status="active", spec=spec.model_dump(), **over)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _load(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        return db.get(Agent, agent_id)
    finally:
        db.close()


# ── runtime.py: one builder, no hard-coded PUBLIC (§12.13) ────────────────────


def test_runtime_py_builds_network_configuration_in_one_place_only():
    source = inspect.getsource(rt)
    # the literal PUBLIC configuration appears exactly once: inside the builder
    assert len(re.findall(r'"networkMode":\s*"PUBLIC"', source)) == 1
    assert '"networkMode": "PUBLIC"' in inspect.getsource(rt._network_configuration)
    assert source.count('"networkConfiguration": _network_configuration(vpc)') == 4


@pytest.mark.parametrize("vpc, expected", [
    (None, PUBLIC),
    ({"subnets": SUBNETS, "security_groups": [SG]}, VPC_CONFIG),
])
def test_every_runtime_call_carries_the_resolved_network(vpc, expected):
    from tests.test_zip_runtime_deployer import StubRuntimeControl

    stub = StubRuntimeControl(["READY"])
    common = {"role_arn": "arn:role", "vpc": vpc}
    rt.create_code_runtime(stub, runtime_name="a_1", s3_bucket="b", s3_key="k", **common)
    assert stub.created_with["networkConfiguration"] == expected
    rt.update_code_runtime(stub, runtime_id="rt-1", s3_bucket="b", s3_key="k", **common)
    assert stub.updated_with["networkConfiguration"] == expected
    rt.create_container_runtime(stub, runtime_name="a_1", container_uri="img", **common)
    assert stub.created_with["networkConfiguration"] == expected
    rt.update_container_runtime(stub, runtime_id="rt-1", container_uri="img", **common)
    assert stub.updated_with["networkConfiguration"] == expected


# ── deployers resolve spec.network (§12.6) ────────────────────────────────────


def test_spec_vpc_resolves_a_profile_and_none_is_public():
    pid = _profile()
    spec = AgentSpec(name="net-a", method="zip_runtime", system_prompt="hi",
                     network={"profile_id": pid})
    assert spec_vpc(spec, DEFAULT_WORKSPACE_ID) == {"subnets": SUBNETS,
                                                    "security_groups": [SG]}
    public = AgentSpec(name="net-b", method="zip_runtime", system_prompt="hi")
    assert spec_vpc(public, DEFAULT_WORKSPACE_ID) is None


def test_container_vpc_no_longer_needs_byo_mounts():
    from app.deployer.container import _vpc as container_vpc

    pid = _profile()
    spec = AgentSpec(name="net-c", method="container", system_prompt="hi",
                     network={"profile_id": pid})
    assert container_vpc(spec, DEFAULT_WORKSPACE_ID) == {"subnets": SUBNETS,
                                                         "security_groups": [SG]}


@pytest.mark.parametrize("mode", ["create", "update"])
def test_zip_deploy_places_the_runtime_in_the_profile(monkeypatch, mode):
    from app.deployer import zip_runtime as zr
    from tests.test_zip_runtime_deployer import StubRuntimeControl, _fake_settings, _fake_ws

    pid = _profile()
    spec = AgentSpec(name=f"net-zip-{mode}", method="zip_runtime", system_prompt="hi",
                     network={"profile_id": pid})
    over = {"resource_id": "rt-1", "arn": "arn:rt-1", "version": "1"} if mode == "update" else {}
    agent_id = _agent_row(spec, **over)
    stub = StubRuntimeControl(["READY"])
    monkeypatch.setattr(zr, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(zr, "get_settings", _fake_settings)
    lines: list[str] = []
    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1", workspace=_fake_ws())
    ctx.scratch["mode"] = mode
    ctx.log = lines.append
    zr._stage_deploy(ctx, _load(agent_id))

    sent = stub.updated_with if mode == "update" else stub.created_with
    assert sent["networkConfiguration"] == VPC_CONFIG
    assert any(line.startswith("network: VPC") for line in lines)


@pytest.mark.parametrize("kind", ["code_zip", "container_image"])
def test_byoc_deploy_places_both_artifact_kinds(monkeypatch, kind):
    from app.deployer import byoc as byoc_dep
    from tests.test_byoc import (
        ECR_IMAGE,
        RESOURCES,
        StubRuntimeControl,
        _byoc_spec,
        _get_agent,
        _mk_agent,
        _stage_ctx,
    )

    pid = _profile()
    byoc = ({"artifact_kind": "code_zip", "upload_id": "u1"} if kind == "code_zip"
            else {"artifact_kind": "container_image", "image_uri": ECR_IMAGE})
    spec = AgentSpec(**_byoc_spec(byoc=byoc, memory={"short_term": False, "long_term": False},
                                  network={"profile_id": pid}))
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({"s3_bucket": "bkt", "s3_key": "k.zip",
                        "execution_role_arn": "arn:agent-role"})
    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    assert stub.created_with["networkConfiguration"] == VPC_CONFIG


# ── harness (§12.6; request shape unverified live) ────────────────────────────


def test_harness_create_carries_the_network_in_its_environment():
    spec = AgentSpec(name="net-h", method="harness", system_prompt="hi")
    vpc = {"subnets": SUBNETS, "security_groups": [SG]}
    params = build_create_params(spec, "arn:role", None, vpc=vpc)
    env = params["environment"]["agentCoreRuntimeEnvironment"]
    assert env["networkConfiguration"] == VPC_CONFIG
    assert env["filesystemConfigurations"] == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]
    # PUBLIC with no storage: the member stays omitted (the service default)
    bare = AgentSpec(name="net-h2", method="harness", system_prompt="hi",
                     filesystem={"session_storage": None})
    assert "environment" not in build_create_params(bare, "arn:role", None)
    only_net = build_create_params(bare, "arn:role", None, vpc=vpc)
    assert only_net["environment"] == {
        "agentCoreRuntimeEnvironment": {"networkConfiguration": VPC_CONFIG}}


def test_harness_update_sets_keeps_or_clears_the_network():
    live = {"environment": {"agentCoreRuntimeEnvironment": {
        "networkConfiguration": {"networkMode": "VPC", "networkModeConfig": {
            "subnets": ["subnet-old"], "securityGroups": ["sg-old"]}}}}}
    kept = hc.environment_for_update(live, [])
    assert kept["agentCoreRuntimeEnvironment"]["networkConfiguration"]["networkModeConfig"][
        "subnets"] == ["subnet-old"]
    moved = hc.environment_for_update(live, [], {"subnets": SUBNETS, "security_groups": [SG]})
    assert moved["agentCoreRuntimeEnvironment"]["networkConfiguration"] == VPC_CONFIG
    cleared = hc.environment_for_update(live, [], None)
    assert cleared["agentCoreRuntimeEnvironment"]["networkConfiguration"] == PUBLIC


@pytest.mark.parametrize("network_from, expected", [
    # the spec still names no network and the job says nothing: the live value stays
    (None, "keep"),
    # a re-publish that moved the agent off its profile: PUBLIC is sent
    ({"profile_id": "np-0000000000ab"}, PUBLIC),
])
def test_harness_republish_off_a_profile_goes_public(monkeypatch, network_from, expected):
    from app.deployer import harness as harness_deploy
    from tests.test_session_storage_methods import EnvStubControl

    spec = AgentSpec(name="net-h-upd", method="harness", system_prompt="hi")
    agent_id = _agent_row(spec, resource_id="h-123", arn="arn:h-123", version="1")
    live_net = {"networkMode": "VPC", "networkModeConfig": {
        "subnets": ["subnet-x"], "securityGroups": ["sg-x"]}}
    stub = EnvStubControl(["READY"], {"agentCoreRuntimeEnvironment": {
        "networkConfiguration": live_net}})
    monkeypatch.setattr(harness_deploy, "control_client", lambda _ws=None: stub)
    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1", workspace=ws_ctx())
    ctx.scratch["mode"] = "update"
    if network_from is not None:
        ctx.scratch[NETWORK_FROM_KEY] = network_from
    harness_deploy._stage_deploy(ctx, _load(agent_id))
    sent = stub.updated_with["environment"]["agentCoreRuntimeEnvironment"]["networkConfiguration"]
    assert sent == (live_net if expected == "keep" else expected)


def test_harness_republish_into_a_profile_sets_vpc(monkeypatch):
    from app.deployer import harness as harness_deploy
    from tests.test_session_storage_methods import EnvStubControl

    pid = _profile()
    spec = AgentSpec(name="net-h-in", method="harness", system_prompt="hi",
                     network={"profile_id": pid})
    agent_id = _agent_row(spec, resource_id="h-123", arn="arn:h-123", version="1")
    stub = EnvStubControl(["READY"], {"agentCoreRuntimeEnvironment": {}})
    monkeypatch.setattr(harness_deploy, "control_client", lambda _ws=None: stub)
    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1", workspace=ws_ctx())
    ctx.scratch["mode"] = "update"
    harness_deploy._stage_deploy(ctx, _load(agent_id))
    env = stub.updated_with["environment"]["agentCoreRuntimeEnvironment"]
    assert env["networkConfiguration"] == VPC_CONFIG


# ── the deploy gate (§12.4, §12.5) ────────────────────────────────────────────


def test_gate_refuses_public_where_the_workspace_forbids_it():
    db = SessionLocal()
    try:
        assert network_gate(db, ws_ctx(), {"method": "harness"}) == {}
        _forbid_public()
        with pytest.raises(AppError) as err:
            network_gate(db, ws_ctx(), {"method": "harness"})
        assert err.value.code == "network.public_not_allowed"
    finally:
        db.close()


def test_gate_runs_the_preflight_for_this_agents_features(ec2):
    pid = _profile()
    db = SessionLocal()
    try:
        spec = {"method": "harness", "network": {"profile_id": pid}}
        assert network_gate(db, ws_ctx(), spec) == {}
        assert "DescribeSubnets" in ec2.calls
        # a missing ECR endpoint blocks a container-backed agent (the harness), and
        # the stored profile is left as it was (a deploy-time check only reports)
        ec2.data = copy.deepcopy(ec2.data)
        ec2.data["endpoints"] = [e for e in ec2.data["endpoints"]
                                 if not e["ServiceName"].endswith(".ecr.dkr")]
        with pytest.raises(AppError) as err:
            network_gate(db, ws_ctx(), spec)
        assert err.value.code == "network.preflight_failed"
        assert err.value.status_code == 409
        checks = [r["check"] for r in err.value.detail["results"]]
        assert "endpoint.ecr.dkr" in checks
        assert db.get(NetworkProfile, pid).status == "ok"
    finally:
        db.close()


def test_gate_skips_the_preflight_for_a_legacy_profile(ec2):
    db = SessionLocal()
    try:
        spec = {"method": "container", "network": {"profile_id": "legacy-0123abcd"}}
        assert network_gate(db, ws_ctx(), spec) == {}
        assert ec2.calls == []
    finally:
        db.close()


def test_gate_records_a_move_off_a_profile():
    db = SessionLocal()
    try:
        old = {"method": "harness", "network": {"profile_id": "legacy-0123abcd"}}
        assert network_gate(db, ws_ctx(), {"method": "harness"}, previous_spec=old) == {
            NETWORK_FROM_KEY: {"profile_id": "legacy-0123abcd"}}
        assert network_gate(db, ws_ctx(), old, previous_spec=old) == {}
    finally:
        db.close()


def test_pipeline_hands_the_old_network_to_the_stages(monkeypatch):
    """`network_from` on the job lands in the stage scratch (the harness reads it)."""
    from app.deployer.pipeline import create_deployment, execute_deploy_job

    seen: dict = {}

    def deploy(ctx, _agent):
        seen.update(ctx.scratch)
        return pipeline.StageResult(detail="ok")

    monkeypatch.setitem(pipeline._METHODS, "net-probe-method", {"deploy": deploy})
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="net-pipe", status="deploying",
                      method="net-probe-method", spec={"name": "net-pipe"})
        db.add(agent)
        db.flush()
        _dep, job = create_deployment(
            db, agent, mode="update",
            payload_extra={NETWORK_FROM_KEY: {"profile_id": "np-0000000000ab"}})
        job_id = job.id
    finally:
        db.close()
    monkeypatch.setattr(pipeline, "_finish", lambda *a, **k: None)
    execute_deploy_job(job_id)
    assert seen[NETWORK_FROM_KEY] == {"profile_id": "np-0000000000ab"}


# ── routes ────────────────────────────────────────────────────────────────────

SPEC = {"name": "net-api-agent", "method": "harness", "system_prompt": "Answer."}


def test_create_refuses_before_any_row_when_the_preflight_blocks(client, ec2,
                                                                 no_real_deploy):
    pid = _profile()
    ec2.data = copy.deepcopy(ec2.data)
    ec2.data["endpoints"] = [e for e in ec2.data["endpoints"]
                             if not e["ServiceName"].endswith(".logs")]
    res = client.post("/api/agents", json={**SPEC, "network": {"profile_id": pid}})
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "network.preflight_failed"
    assert no_real_deploy == []
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.query(Agent).count() == 0
            assert db.query(Job).count() == 0
    finally:
        db.close()


def test_create_with_a_passing_profile_and_a_forbidden_public(client, ec2, no_real_deploy):
    pid = _profile()
    _forbid_public()
    res = client.post("/api/agents", json=SPEC)
    assert res.status_code == 400 and res.json()["code"] == "network.public_not_allowed"
    res = client.post("/api/agents", json={**SPEC, "network": {"profile_id": pid}})
    assert res.status_code == 202, res.text
    assert len(no_real_deploy) == 1


def test_redeploy_off_a_profile_carries_the_old_network(client, ec2, no_real_deploy):
    pid = _profile()
    agent_id = client.post("/api/agents", json={**SPEC, "network": {"profile_id": pid}}
                           ).json()["agent"]["id"]
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).status = "active"
        db.commit()
    finally:
        db.close()
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=SPEC)
    assert res.status_code == 202, res.text
    db = SessionLocal()
    try:
        job = db.get(Job, res.json()["job_id"])
        assert job.payload["mode"] == "update"
        assert job.payload[NETWORK_FROM_KEY] == {"profile_id": pid}
    finally:
        db.close()


def test_network_route_reports_public_vpc_and_enis(client, ec2):
    pid = _profile()
    public_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    body = client.get(f"/api/agents/{public_id}/network").json()
    assert body["mode"] == "PUBLIC" and body["profile"] is None and body["enis"] is None

    vpc_id = client.post("/api/agents", json={**SPEC, "name": "net-api-vpc",
                                              "network": {"profile_id": pid}}
                         ).json()["agent"]["id"]
    asked: list = []

    def reader(_ws, vpc):
        asked.append(vpc)
        return [{"NetworkInterfaceId": "eni-1", "SubnetId": SUBNETS[0], "Status": "in-use",
                 "AvailabilityZone": "us-west-2a", "PrivateIpAddress": "10.0.0.5"}]

    previous = agent_network.set_eni_reader(reader)
    try:
        body = client.get(f"/api/agents/{vpc_id}/network").json()
        assert body["mode"] == "VPC"
        assert body["profile"]["id"] == pid and body["profile"]["status"] == "ok"
        assert body["vpc"] == {"subnets": SUBNETS, "security_groups": [SG]}
        assert body["enis"]["count"] == 1 and body["enis"]["in_use"] == 1
        assert body["enis"]["interfaces"][0]["id"] == "eni-1"
        assert asked == [{"subnets": SUBNETS, "security_groups": [SG]}]

        def denied(_ws, _vpc):
            raise client_error("UnauthorizedOperation", "no", "DescribeNetworkInterfaces")

        agent_network.set_eni_reader(denied)
        enis = client.get(f"/api/agents/{vpc_id}/network").json()["enis"]
        assert enis == {"status": "unavailable", "error": "UnauthorizedOperation",
                        "interfaces": []}
    finally:
        agent_network.set_eni_reader(previous)
    assert client.get("/api/agents/nope/network").status_code == 404


def test_network_route_hides_another_projects_agent(app):  # noqa: F811
    _project("alpha")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="alpha-net", status="active",
                        method="harness", spec={"name": "alpha-net"})
            db.add(row)
            db.commit()
            alpha_agent = row.id
        with project_context(TEST_PROJECT):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="default-net",
                        status="active", method="harness", spec={"name": "default-net"})
            db.add(row)
            db.commit()
            own_agent = row.id
    finally:
        db.close()
    assert _user("nell")  # granted the workspace: a member of its default project
    nell = _login(app, "nell")
    assert nell.get(f"/api/agents/{own_agent}/network").json()["mode"] == "PUBLIC"
    hidden = nell.get(f"/api/agents/{alpha_agent}/network")
    assert hidden.status_code == 404 and hidden.json()["code"] == "agent.not_found"
