"""BYOC code zips and ADOT (projects §17.3-1): upload-time detection of the
aws-opentelemetry-distro, and the deploy wrapping the entrypoint in the
opentelemetry-instrument launcher only when the bundle carries it."""

import pytest

from app.deployer import byoc as byoc_dep
from app.schemas.agent import AgentSpec
from app.services import byoc_uploads
from tests.conftest import ws_ctx
from tests.test_byoc import (
    RESOURCES,
    SDK_MAIN,
    StubRuntimeControl,
    StubS3,
    _byoc_spec,
    _client_router,
    _get_agent,
    _mk_agent,
    _seed_upload,
    _stage_ctx,
    make_zip,
    stub_s3,  # noqa: F401 — fixture
    zip_bytes,
)

# ── requirements parsing ─────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "aws-opentelemetry-distro\n",
    "aws-opentelemetry-distro==0.19.*\n",
    "requests==2.32.3\naws-opentelemetry-distro>=0.10,<1 ; python_version >= '3.10'\n",
    "AWS_OpenTelemetry.Distro~=0.19\n",
    "aws-opentelemetry-distro[extra]==0.19.0 \\\n    --hash=sha256:abc\n",
    "aws-opentelemetry-distro==0.19.0  # tracing\n",
])
def test_requirements_mention_adot(text):
    assert byoc_uploads.requirements_mention_adot(text) is True


@pytest.mark.parametrize("text", [
    "",
    "# aws-opentelemetry-distro==0.19.0\n",
    "   #aws-opentelemetry-distro\n",
    "opentelemetry-distro==0.48b0\n",
    "aws-opentelemetry-distro-extras==1.0\n",
    "strands-agents[otel]==1.0  # needs aws-opentelemetry-distro too\n",
    "--extra-index-url https://example.com/aws-opentelemetry-distro\n",
])
def test_requirements_without_adot(text):
    assert byoc_uploads.requirements_mention_adot(text) is False


# ── upload detection ─────────────────────────────────────────────────────────


def test_detect_adot_from_requirements(tmp_path):
    path = make_zip(tmp_path / "a.zip", {
        "agent/main.py": SDK_MAIN,
        "agent/requirements.txt": b"strands-agents[otel]\naws-opentelemetry-distro==0.19.*\n",
    })
    detected = byoc_uploads.validate_and_detect(path)["detected"]
    assert detected["adot_detected"] is True
    assert detected["adot_source"] == "requirements"


@pytest.mark.parametrize("member", [
    "aws_opentelemetry_distro/__init__.py",
    "aws_opentelemetry_distro-0.19.0.dist-info/METADATA",
])
def test_detect_adot_vendored(tmp_path, member):
    path = make_zip(tmp_path / "a.zip", {"main.py": SDK_MAIN, member: b""})
    detected = byoc_uploads.validate_and_detect(path)["detected"]
    assert detected["adot_detected"] is True
    assert detected["adot_source"] == "vendored"


def test_detect_no_adot(tmp_path):
    path = make_zip(tmp_path / "a.zip", {
        "main.py": SDK_MAIN,
        "requirements.txt": b"# aws-opentelemetry-distro\nrequests\n",
        # a nested copy is not on the runtime's import path
        "lib/aws_opentelemetry_distro/__init__.py": b"",
    })
    detected = byoc_uploads.validate_and_detect(path)["detected"]
    assert detected["adot_detected"] is False
    assert detected["adot_source"] is None


def test_upload_manifest_carries_adot_detected(client, stub_s3, monkeypatch):  # noqa: F811
    data = zip_bytes({
        "main.py": SDK_MAIN,
        "requirements.txt": b"aws-opentelemetry-distro==0.19.*\n",
    })
    # the dry resolve is not under test here
    monkeypatch.setattr(
        byoc_uploads, "check_requirements",
        lambda *_a, **_kw: {"status": "ok", "package_count": 1, "error": None},
    )
    res = client.post(
        "/api/agents/uploads", files={"file": ("agent.zip", data, "application/zip")}
    )
    assert res.status_code == 201, res.text
    assert res.json()["detected"]["adot_detected"] is True
    upload_id = res.json()["upload_id"]
    again = client.get(f"/api/agents/uploads/{upload_id}")
    assert again.json()["detected"]["adot_detected"] is True


@pytest.mark.parametrize("detected, install, expected", [
    ({"adot_detected": True, "adot_source": "requirements"}, True, True),
    ({"adot_detected": True, "adot_source": "requirements"}, False, False),
    ({"adot_detected": True, "adot_source": "vendored"}, False, True),
    ({"adot_detected": False, "adot_source": None}, True, False),
    ({}, True, False),  # manifest staged before the check existed
    (None, True, False),
])
def test_adot_launcher_rule(detected, install, expected):
    assert byoc_uploads.adot_launcher(detected, install) is expected


# ── generate + deploy ────────────────────────────────────────────────────────


def _adot_detected(source: str | None) -> dict:
    return {
        "entrypoint_candidates": ["main.py"],
        "has_requirements": source == "requirements",
        "has_dockerfile": False,
        "agentcore_sdk_detected": True,
        "adot_detected": source is not None,
        "adot_source": source,
    }


def _generate_then_deploy(monkeypatch, upload_id: str, source: str | None,
                          install_requirements: bool = True, mode: str = "create",
                          agent_id: str | None = None,
                          lines: list[str] | None = None):
    s3 = StubS3()
    stub = StubRuntimeControl()
    _client_router(monkeypatch, {"s3": s3})
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    _seed_upload(s3, upload_id, {"main.py": SDK_MAIN}, detected=_adot_detected(source))
    byoc = {"artifact_kind": "code_zip", "upload_id": upload_id,
            "install_requirements": install_requirements}
    if agent_id is None:
        agent_id, dep_id = _mk_agent(AgentSpec(**_byoc_spec(byoc=byoc)))
    else:
        from app.core.db import SessionLocal
        from app.models.ledger import Agent

        db = SessionLocal()
        row = db.get(Agent, agent_id)
        row.spec = {**row.spec, "byoc": {**row.spec["byoc"], **byoc}}
        db.commit()
        db.close()
        dep_id = "dep-redeploy"
    log = lines.append if lines is not None else (lambda _msg: None)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.log = log
    ctx.scratch.update({"execution_role_arn": "arn:agent-role", "mode": mode})
    byoc_dep._stage_generate(ctx, _get_agent(agent_id))
    # a resumed job keeps no scratch from generate: the decision is on the row
    deploy_ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    deploy_ctx.log = log
    deploy_ctx.scratch.update({
        "execution_role_arn": "arn:agent-role", "mode": mode,
        "s3_bucket": "bkt", "s3_key": "agents/byoc-agent/byoc_package.zip",
    })
    byoc_dep._stage_deploy(deploy_ctx, _get_agent(agent_id))
    sent = stub.updated_with if mode == "update" else stub.created_with
    entry = sent["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"]
    return agent_id, entry


def test_deploy_instruments_when_adot_detected(monkeypatch):
    agent_id, entry = _generate_then_deploy(monkeypatch, "u-adot", "requirements")
    assert entry == ["opentelemetry-instrument", "main.py"]
    assert _get_agent(agent_id).spec["byoc"]["provenance"]["adot_launcher"] is True


def test_deploy_without_adot_keeps_bare_entrypoint(monkeypatch):
    _, entry = _generate_then_deploy(monkeypatch, "u-plain", None)
    assert entry == ["main.py"]


def test_deploy_requirements_adot_without_install_stays_off(monkeypatch):
    _, entry = _generate_then_deploy(
        monkeypatch, "u-noinstall", "requirements", install_requirements=False
    )
    assert entry == ["main.py"]


def test_deploy_vendored_adot_without_install_instruments(monkeypatch):
    _, entry = _generate_then_deploy(
        monkeypatch, "u-vendored", "vendored", install_requirements=False
    )
    assert entry == ["opentelemetry-instrument", "main.py"]


def test_republish_without_adot_drops_the_launcher(monkeypatch):
    agent_id, entry = _generate_then_deploy(monkeypatch, "u-first", "requirements")
    assert entry[0] == "opentelemetry-instrument"
    _, entry = _generate_then_deploy(
        monkeypatch, "u-second", None, mode="update", agent_id=agent_id
    )
    assert entry == ["main.py"]
    assert _get_agent(agent_id).spec["byoc"]["provenance"]["adot_launcher"] is False


def test_deploy_logs_telemetry_line(monkeypatch):
    lines: list[str] = []
    _generate_then_deploy(monkeypatch, "u-log", "requirements", lines=lines)
    assert "telemetry: ADOT launcher on" in lines


def test_failed_ready_with_launcher_logs_the_hint(monkeypatch):
    lines: list[str] = []

    def failed(*_a, **_kw):
        raise RuntimeError("runtime rt-1 entered CREATE_FAILED: boom")

    monkeypatch.setattr(byoc_dep.rt, "wait_runtime_ready", failed)
    with pytest.raises(RuntimeError, match="CREATE_FAILED"):
        _generate_then_deploy(monkeypatch, "u-fail", "requirements", lines=lines)
    assert any("remove aws-opentelemetry-distro" in line for line in lines)
