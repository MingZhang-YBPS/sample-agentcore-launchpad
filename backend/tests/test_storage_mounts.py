"""BYO mounts on every method (design storage-mounts S1, §3; §1.5) and an MCP server in
a VPC profile (projects §15.10).

The spec accepts S3 Files / EFS mounts on every method of `BYO_MOUNT_METHODS` inside
the agent's network profile; each deployer sends the full `filesystemConfigurations`
(BYOC included, which sent none before) with the resolved VPC and tells the agent its
mounts (`LAUNCHPAD_MOUNTS`, the system-prompt paragraph); the harness re-publish merges
the live list by mount-path ownership; inline mounts are administrator-only (D-2).
No AWS call is made: every AgentCore client is a stub, request shapes are checked
against the botocore service model.
"""

import importlib.util
import json
from pathlib import Path

import botocore.session
import pytest
from botocore.validate import ParamValidator
from pydantic import ValidationError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer import mounts
from app.deployer.harness import build_create_params, strip_generated_prompt
from app.deployer.pipeline import StageContext
from app.models.ledger import Agent, SpecSnapshot
from app.schemas import agent as agent_schema
from app.schemas.agent import BYO_MOUNT_METHODS, AgentSpec
from app.services import mount_policy
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt

from .conftest import ws_ctx

S3_AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-abc/access-point/ap-1"
EFS_AP = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-0123"
VPC = {"subnets": ["subnet-a", "subnet-b"], "security_groups": ["sg-1"]}
VPC_CONFIG = {"networkMode": "VPC",
              "networkModeConfig": {"subnets": VPC["subnets"], "securityGroups": ["sg-1"]}}
SESSION = {"sessionStorage": {"mountPath": "/mnt/workspace"}}
S3_ENTRY = {"s3FilesAccessPoint": {"accessPointArn": S3_AP, "mountPath": "/mnt/datasets"}}
FS = {"s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/datasets"}]}
ECR_IMAGE = "111122223333.dkr.ecr.us-west-2.amazonaws.com/my-agents:v1"


def _spec(method: str, **over) -> AgentSpec:
    base: dict = {"name": f"mnt-{method.replace('_', '-')}", "method": method}
    if method == "byoc":
        base["byoc"] = {"artifact_kind": "code_zip", "upload_id": "u1"}
    else:
        base["system_prompt"] = "You help with data."
    if method == "studio":
        base["code"] = "print('x')"
    return AgentSpec(**{**base, **over})


def _validate(operation: str, params: dict) -> None:
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    report = ParamValidator().validate(params, model.operation_model(operation).input_shape)
    assert not report.has_errors(), report.generate_report()


def _row(spec: AgentSpec, **over) -> str:
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


@pytest.fixture
def vpc(monkeypatch):
    """Every deployer resolves the spec's network to this placement."""
    from app.deployer import byoc, container, harness, zip_runtime

    for module in (byoc, container, harness, zip_runtime):
        monkeypatch.setattr(module, "spec_vpc", lambda spec, _ws: VPC if spec.network else None)
    return VPC


# ── schema (§3.2) ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", sorted(BYO_MOUNT_METHODS))
def test_every_method_takes_byo_mounts_inside_a_vpc(method):
    spec = _spec(method, filesystem=FS, network={"profile_id": "np-0123456789ab"})
    assert spec.filesystem.byo_mounts()[0][0] == "s3files"


@pytest.mark.parametrize("method", sorted(BYO_MOUNT_METHODS))
def test_byo_mounts_without_a_network_are_refused(method):
    with pytest.raises(ValidationError, match="choose a network profile"):
        _spec(method, filesystem=FS)


def test_a_method_a_probe_refutes_is_refused_with_its_reason(monkeypatch):
    monkeypatch.setattr(agent_schema, "BYO_MOUNT_METHODS", frozenset({"container"}))
    monkeypatch.setattr(agent_schema, "BYO_MOUNT_UNSUPPORTED",
                        {"harness": "probe P3: CreateHarness rejects the mount"})
    with pytest.raises(ValidationError, match="probe P3: CreateHarness rejects the mount"):
        _spec("harness", filesystem=FS, network={"profile_id": "np-0123456789ab"})
    # session storage alone is unaffected
    assert _spec("harness").filesystem.session_storage is not None


# ── runtime.py: mounts need VPC ───────────────────────────────────────────────


class RuntimeStub:
    def __init__(self):
        self.created_with = self.updated_with = None

    def create_agent_runtime(self, **kwargs):
        self.created_with = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1",
                "agentRuntimeVersion": "1", "status": "CREATING"}

    def update_agent_runtime(self, **kwargs):
        self.updated_with = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeVersion": "2", "status": "UPDATING"}

    def get_agent_runtime(self, **_kw):
        return {"agentRuntimeArn": "arn:rt-1", "agentRuntimeVersion": "1", "status": "READY"}


def test_runtime_calls_refuse_mounts_on_public():
    stub = RuntimeStub()
    with pytest.raises(ValueError, match="need the runtime in a VPC"):
        rt.create_code_runtime(stub, runtime_name="x", s3_bucket="b", s3_key="k",
                               role_arn="arn:r", filesystem_configurations=[SESSION, S3_ENTRY])
    with pytest.raises(ValueError, match="need the runtime in a VPC"):
        rt.update_container_runtime(stub, runtime_id="rt-1", container_uri=ECR_IMAGE,
                                    role_arn="arn:r", filesystem_configurations=[S3_ENTRY])
    assert stub.created_with is None and stub.updated_with is None
    # session storage alone stays fine on PUBLIC
    rt.create_code_runtime(stub, runtime_name="x", s3_bucket="b", s3_key="k",
                           role_arn="arn:r", filesystem_configurations=[SESSION])
    assert stub.created_with["networkConfiguration"] == {"networkMode": "PUBLIC"}


def test_code_runtime_with_mounts_and_vpc_matches_the_service_model():
    stub = RuntimeStub()
    rt.create_code_runtime(stub, runtime_name="mnt_zip", s3_bucket="b", s3_key="k",
                           role_arn="arn:aws:iam::111122223333:role/r",
                           filesystem_configurations=[SESSION, S3_ENTRY], vpc=VPC)
    assert stub.created_with["filesystemConfigurations"] == [SESSION, S3_ENTRY]
    assert stub.created_with["networkConfiguration"] == VPC_CONFIG
    _validate("CreateAgentRuntime", stub.created_with)


# ── what the agent is told (deployer/mounts.py) ───────────────────────────────


def test_manifest_env_and_prompt_note():
    spec = _spec("zip_runtime", network={"profile_id": "np-0123456789ab"}, filesystem={
        "s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/datasets/"}],
        "efs": [{"access_point_arn": EFS_AP, "mount_path": "/mnt/tools"}],
    })
    manifest = [{"name": "datasets", "path": "/mnt/datasets", "kind": "s3files", "mode": "rw"},
                {"name": "tools", "path": "/mnt/tools", "kind": "efs", "mode": "rw"}]
    assert mounts.mount_manifest(spec) == manifest
    assert json.loads(mounts.mounts_env(spec)[mounts.ENV_MOUNTS]) == manifest
    note = mounts.prompt_note(spec)
    assert "/mnt/datasets (S3 Files, read-write)" in note and "/mnt/tools (EFS" in note
    noted = mounts.with_prompt_note(spec)
    assert noted.system_prompt == spec.system_prompt + note
    assert spec.system_prompt == "You help with data."  # the stored spec is untouched


def test_no_byo_mounts_tell_nothing():
    spec = _spec("harness")  # session storage only
    assert mounts.mounts_env(spec) == {}
    assert mounts.prompt_note(spec) == ""
    assert mounts.with_prompt_note(spec) is spec
    env = {"A": "1"}
    assert mounts.with_mounts_env(env, spec) == {"A": "1"}


def test_the_platform_manifest_wins_over_spec_env():
    spec = _spec("container", filesystem=FS, network=VPC,
                 env={mounts.ENV_MOUNTS: "[]"})
    env = mounts.with_mounts_env(dict(spec.env), spec)
    assert json.loads(env[mounts.ENV_MOUNTS])[0]["path"] == "/mnt/datasets"


# ── zip / studio ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["create", "update"])
def test_zip_deploy_sends_mounts_vpc_and_manifest(monkeypatch, vpc, mode):
    from app.deployer import zip_runtime as zr
    from tests.test_zip_runtime_deployer import StubRuntimeControl, _fake_settings, _fake_ws

    spec = _spec("zip_runtime", name=f"mnt-zip-{mode}", filesystem=FS,
                 network={"profile_id": "np-0123456789ab"})
    over = {"resource_id": "rt-1", "arn": "arn:rt-1", "version": "1"} if mode == "update" else {}
    agent_id = _row(spec, **over)
    stub = StubRuntimeControl(["READY"])
    monkeypatch.setattr(zr, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(zr, "get_settings", _fake_settings)
    lines: list[str] = []
    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1", workspace=_fake_ws())
    ctx.scratch["mode"] = mode
    ctx.log = lines.append
    zr._stage_deploy(ctx, _load(agent_id))
    sent = stub.updated_with if mode == "update" else stub.created_with
    assert sent["filesystemConfigurations"] == [SESSION, S3_ENTRY]
    assert sent["networkConfiguration"] == VPC_CONFIG
    assert json.loads(sent["environmentVariables"][mounts.ENV_MOUNTS])[0]["path"] == "/mnt/datasets"
    assert "mounts: /mnt/datasets (s3files, rw)" in lines


def test_strands_code_names_the_mounts_studio_code_is_left_alone():
    from app.deployer.zip_runtime import _generate_code

    strands = _spec("zip_runtime", filesystem=FS, network={"profile_id": "np-0123456789ab"})
    code, _ = _generate_code(strands)
    assert "Shared directories" in code and "/mnt/datasets" in code
    plain, _ = _generate_code(_spec("zip_runtime"))
    assert "Shared directories" not in plain
    studio = _spec("studio", filesystem=FS, network={"profile_id": "np-0123456789ab"})
    assert "Shared directories" not in _generate_code(studio)[0]


# ── container ─────────────────────────────────────────────────────────────────


def test_container_template_prompt_names_the_mounts(tmp_path):
    from app.deployer.container import _build_context

    spec = _spec("container", filesystem=FS, network=VPC)
    agent = Agent(name="mnt-ctr", method="container", spec=spec.model_dump())
    context = _build_context(spec, agent, lambda _l: None, ws_ctx())
    assert "/mnt/datasets" in (context / "main.py").read_text()


def test_container_deploy_carries_the_manifest(monkeypatch, vpc):
    from app.deployer import container

    spec = _spec("container", name="mnt-ctr-dep", filesystem=FS,
                 network={"profile_id": "np-0123456789ab"})
    agent_id = _row(spec)
    stub = RuntimeStub()
    monkeypatch.setattr(container, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(rt, "wait_runtime_ready", lambda *_a, **_k: {
        "agentRuntimeArn": "arn:rt-1", "agentRuntimeVersion": "1"})
    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1",
                       workspace=ws_ctx({"execution_role_arn": "arn:role"}))
    ctx.scratch.update({"image_uri": ECR_IMAGE, "execution_role_arn": "arn:role"})
    container._stage_deploy(ctx, _load(agent_id))
    sent = stub.created_with
    assert sent["filesystemConfigurations"] == [SESSION, S3_ENTRY]
    assert sent["networkConfiguration"] == VPC_CONFIG
    assert mounts.ENV_MOUNTS in sent["environmentVariables"]


# ── BYOC (§1.5: it sent no filesystemConfigurations at all) ───────────────────


def _byoc_deploy(monkeypatch, spec: AgentSpec):
    from app.deployer import byoc as byoc_dep
    from tests.test_byoc import RESOURCES, StubRuntimeControl, _get_agent, _mk_agent, _stage_ctx

    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({"s3_bucket": "bkt", "s3_key": "k.zip",
                        "execution_role_arn": "arn:agent-role"})
    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    return stub.created_with


@pytest.mark.parametrize("kind", ["code_zip", "container_image"])
def test_byoc_sends_session_storage_and_mounts(monkeypatch, vpc, kind):
    byoc = ({"artifact_kind": "code_zip", "upload_id": "u1"} if kind == "code_zip"
            else {"artifact_kind": "container_image", "image_uri": ECR_IMAGE})
    spec = _spec("byoc", name=f"mnt-byoc-{kind.replace('_', '-')}", byoc=byoc, filesystem=FS,
                 network={"profile_id": "np-0123456789ab"},
                 memory={"short_term": False, "long_term": False})
    sent = _byoc_deploy(monkeypatch, spec)
    assert sent["filesystemConfigurations"] == [SESSION, S3_ENTRY]
    assert sent["networkConfiguration"] == VPC_CONFIG
    assert json.loads(sent["environmentVariables"][mounts.ENV_MOUNTS])[0]["name"] == "datasets"


def test_byoc_without_mounts_gains_session_storage_on_public(monkeypatch, vpc):
    spec = _spec("byoc", name="mnt-byoc-plain", memory={"short_term": False, "long_term": False})
    sent = _byoc_deploy(monkeypatch, spec)
    assert sent["filesystemConfigurations"] == [SESSION]
    assert sent["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert mounts.ENV_MOUNTS not in (sent.get("environmentVariables") or {})


# ── an MCP server in a VPC profile (projects §15.10) ──────────────────────────


@pytest.mark.parametrize("kind", ["code_zip", "container_image"])
def test_mcp_server_keeps_its_vpc_and_mounts(monkeypatch, kind):
    from app.deployer import byoc as byoc_dep
    from app.services import mcp_servers
    from tests.test_byoc import StubRuntimeControl, _get_agent, _mk_agent, _stage_ctx
    from tests.test_mcp_servers import RESOURCES, _mcp_spec

    monkeypatch.setattr(byoc_dep, "spec_vpc", lambda spec, _ws: VPC if spec.network else None)
    byoc = ({"artifact_kind": "code_zip", "upload_id": "u1"} if kind == "code_zip"
            else {"artifact_kind": "container_image", "image_uri": ECR_IMAGE})
    spec = AgentSpec(**_mcp_spec(name=f"mnt-mcp-{kind.replace('_', '-')}", byoc=byoc,
                                 network={"profile_id": "np-0123456789ab"}, filesystem=FS))
    spec.mcp.client_id = "client-1"
    spec.inbound_auth = mcp_servers.inbound_auth(ws_ctx(RESOURCES), "client-1")
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    agent_id, dep_id = _mk_agent(AgentSpec(**spec.model_dump()))
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({"s3_bucket": "bkt", "s3_key": "k", "execution_role_arn": "arn:r"})
    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    sent = stub.created_with
    assert sent["protocolConfiguration"] == {"serverProtocol": "MCP"}
    assert sent["networkConfiguration"] == VPC_CONFIG
    assert sent["filesystemConfigurations"] == [SESSION, S3_ENTRY]
    assert "MODEL_ID" not in sent["environmentVariables"]


# ── harness ───────────────────────────────────────────────────────────────────


def test_harness_create_carries_mounts_prompt_and_manifest():
    spec = _spec("harness", filesystem=FS, network={"profile_id": "np-0123456789ab"})
    params = build_create_params(spec, "arn:aws:iam::111122223333:role/r", None, vpc=VPC)
    env = params["environment"]["agentCoreRuntimeEnvironment"]
    assert env["filesystemConfigurations"] == [SESSION, S3_ENTRY]
    assert env["networkConfiguration"] == VPC_CONFIG
    prompt = params["systemPrompt"][0]["text"]
    assert prompt.startswith("You help with data.") and "/mnt/datasets" in prompt
    assert mounts.ENV_MOUNTS in params["environmentVariables"]
    _validate("CreateHarness", params)
    # read back from the live harness, the paragraph is not stored as the spec prompt
    assert strip_generated_prompt(spec, prompt) == "You help with data."


def test_harness_without_mounts_keeps_its_request():
    params = build_create_params(_spec("harness"), "arn:role", None)
    assert params["systemPrompt"] == [{"text": "You help with data."}]
    assert "environmentVariables" not in params


OUTSIDE = {"efsAccessPoint": {"accessPointArn": EFS_AP, "mountPath": "/mnt/tools"}}


def _live(*entries):
    return {"environment": {"agentCoreRuntimeEnvironment": {
        "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/old"}}, *entries]}}}


def test_update_merges_by_mount_path_ownership():
    stale = {"s3FilesAccessPoint": {"accessPointArn": S3_AP, "mountPath": "/mnt/removed"}}
    env = hc.environment_for_update(_live(OUTSIDE, stale), [SESSION, S3_ENTRY],
                                    managed_paths={"/mnt/removed", "/mnt/datasets"})
    # the spec's entries; the mount an earlier publish set is detached; outside one kept
    assert env["agentCoreRuntimeEnvironment"]["filesystemConfigurations"] == [
        SESSION, S3_ENTRY, OUTSIDE]
    _validate("UpdateHarness", {"harnessId": "h-1", **{"environment": env}})


def test_update_conflict_spec_wins_and_is_logged():
    outside_at_spec_path = {"efsAccessPoint": {"accessPointArn": EFS_AP,
                                               "mountPath": "/mnt/datasets"}}
    lines: list[str] = []
    env = hc.environment_for_update(_live(outside_at_spec_path), [S3_ENTRY],
                                    on_conflict=lines.append)
    assert env["agentCoreRuntimeEnvironment"]["filesystemConfigurations"] == [S3_ENTRY]
    assert len(lines) == 1 and "/mnt/datasets" in lines[0]
    # the platform's own entry, unchanged: no conflict
    lines.clear()
    hc.environment_for_update(_live(S3_ENTRY), [S3_ENTRY], on_conflict=lines.append)
    assert lines == []


def test_harness_republish_detaches_a_mount_the_spec_dropped(monkeypatch, vpc):
    from app.deployer import harness as harness_deploy
    from tests.test_session_storage_methods import EnvStubControl

    before = _spec("harness", name="mnt-h-drop", filesystem=FS,
                   network={"profile_id": "np-0123456789ab"})
    after = _spec("harness", name="mnt-h-drop", network={"profile_id": "np-0123456789ab"})
    agent_id = _row(after, resource_id="h-123", arn="arn:h-123", version="1")
    db = SessionLocal()
    db.add(SpecSnapshot(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, seq=1,
                        spec=before.model_dump()))
    db.commit()
    db.close()
    stub = EnvStubControl(["READY"], {"agentCoreRuntimeEnvironment": {
        "filesystemConfigurations": [SESSION, S3_ENTRY, OUTSIDE]}})
    monkeypatch.setattr(harness_deploy, "control_client", lambda _ws=None: stub)
    ctx = StageContext(agent_id=agent_id, deployment_id="d1", job_id="j1", workspace=ws_ctx())
    ctx.scratch["mode"] = "update"
    harness_deploy._stage_deploy(ctx, _load(agent_id))
    env = stub.updated_with["environment"]["agentCoreRuntimeEnvironment"]
    assert env["filesystemConfigurations"] == [SESSION, OUTSIDE]
    assert env["networkConfiguration"] == VPC_CONFIG


# ── inline mounts are administrator-only (D-2) ────────────────────────────────


def test_inline_mount_policy():
    with_mount = _spec("zip_runtime", filesystem=FS, network={"profile_id": "np-0123456789ab"})
    plain = _spec("zip_runtime")
    # an administrator may add one
    mount_policy.assert_inline_mounts_allowed(with_mount, None, is_admin=True)
    # a member may not add one, on create or on re-publish
    for previous in (None, plain.model_dump()):
        with pytest.raises(AppError) as exc:
            mount_policy.assert_inline_mounts_allowed(with_mount, previous, is_admin=False)
        assert exc.value.code == "storage.inline_not_allowed"
        assert exc.value.status_code == 400
    # a member may keep an administrator's mounts, or remove them
    mount_policy.assert_inline_mounts_allowed(with_mount, with_mount.model_dump(),
                                              is_admin=False)
    mount_policy.assert_inline_mounts_allowed(plain, with_mount, is_admin=False)
    # a changed access point at the same path counts as new
    moved = _spec("zip_runtime", network={"profile_id": "np-0123456789ab"}, filesystem={
        "s3_files": [{"access_point_arn": S3_AP.replace("ap-1", "ap-2"),
                      "mount_path": "/mnt/datasets"}]})
    assert mount_policy.new_inline_mounts(moved, with_mount.model_dump()) == ["/mnt/datasets"]


# ── the live probe script builds valid specs (it is not run here) ─────────────


def test_probe_script_specs_are_valid():
    path = Path(__file__).resolve().parents[1] / "scripts" / "probe_storage_mounts.py"
    module_spec = importlib.util.spec_from_file_location("probe_storage_mounts", path)
    probe = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(probe)
    for name in probe.ALL_PROBES:
        built = probe.probe_spec(name, access_point=S3_AP, profile_id="np-0123456789ab",
                                 suffix="abc123", byoc_image=ECR_IMAGE)
        spec = AgentSpec(**built)
        assert spec.filesystem.byo == (name != "P4")
    efs = probe.probe_spec("P1", access_point=EFS_AP, profile_id="np-0123456789ab",
                           suffix="abc123")
    assert AgentSpec(**efs).filesystem.efs[0].access_point_arn == EFS_AP
