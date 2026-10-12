"""T31 - the GitOps export is deterministic, and the `launchpad` CLI drives the API."""

import importlib.util
import io
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, Deployment, ReleaseBundle, SpecSnapshot, Workspace
from app.services import release_export

_CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "launchpad.py"
_spec = importlib.util.spec_from_file_location("launchpad_cli", _CLI_PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

SPEC = {"name": "gitops-agent", "method": "harness", "system_prompt": "be brief",
        "model_id": "anthropic.claude", "display_name": "Äpfel ü"}
ARN = "arn:aws:bedrock-agentcore:us-west-2:1:harness/h"


def add_workspace(ws_id: str, tier: str = "dev") -> None:
    db = SessionLocal()
    try:
        if db.get(Workspace, ws_id) is None:
            db.add(Workspace(id=ws_id, name=ws_id, account_id=f"9{abs(hash(ws_id)) % 10**11:011d}",
                             region="us-west-2", bootstrap_status="ready", tier=tier))
            db.commit()
    finally:
        db.close()


def seed_agent(ws_id=DEFAULT_WORKSPACE_ID, name="gitops-agent", spec=None, version="3") -> str:
    add_workspace(ws_id)
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=ws_id, name=name, method="harness", status="active",
                      spec=dict(spec or SPEC), arn=ARN, resource_id="h-1", version=version)
        db.add(agent)
        db.flush()
        dep = Deployment(workspace_id=ws_id, agent_id=agent.id, status="succeeded")
        db.add(dep)
        db.flush()
        db.add(SpecSnapshot(workspace_id=ws_id, agent_id=agent.id, seq=1,
                            spec=dict(spec or SPEC), aws_version=version, deployment_id=dep.id))
        db.commit()
        return agent.id
    finally:
        db.close()


@pytest.fixture
def client():
    return TestClient(create_app())


def bundle_via_api(client, agent_id, ws=None):
    headers = {"X-Workspace": ws} if ws else {}
    res = client.post(f"/api/agents/{agent_id}/release-bundles", json={}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


# ── export ────────────────────────────────────────────────────────────────


def test_export_is_yaml_with_the_identity_and_an_evidence_reference(client):
    bundle = bundle_via_api(client, seed_agent())
    res = client.get(f"/api/release-bundles/{bundle['id']}/export")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/yaml")
    assert res.headers["x-bundle-digest"] == bundle["digest"]
    doc = yaml.safe_load(res.text)
    assert doc["kind"] == "ReleaseBundle"
    assert doc["metadata"] == {"name": "gitops-agent", "digest": f"sha256:{bundle['digest']}"}
    assert doc["spec"]["method"] == "harness" and doc["spec"]["agent"]["display_name"] == "Äpfel ü"
    assert doc["artifact"]["aws_version"] == "3" and doc["evidence"] == {"evaluated": False}
    # nothing that changes between two exports of the same release
    assert not {"created_at", "created_by", "id", "workspace_id"} & (
        set(doc) | set(doc["metadata"]))


def test_same_digest_is_byte_identical_yaml(client):
    first = bundle_via_api(client, seed_agent())
    a = client.get(f"/api/release-bundles/{first['id']}/export").content
    assert a == client.get(f"/api/release-bundles/{first['id']}/export").content
    # An unrelated row of the same release in another workspace, written at another
    # time by someone else, exports to the same bytes.
    other_agent = seed_agent(ws_id="mirror-ws")
    db = SessionLocal()
    try:
        db.get(Agent, other_agent).arn = ARN
        db.commit()
    finally:
        db.close()
    second = bundle_via_api(client, other_agent, ws="mirror-ws")
    assert second["id"] != first["id"] and second["digest"] == first["digest"]
    b = client.get(f"/api/release-bundles/{second['id']}/export",
                   headers={"X-Workspace": "mirror-ws"}).content
    assert a == b


def test_export_ignores_key_order_and_timestamps():
    def row(spec, evaluation):
        return ReleaseBundle(agent_name="x", method="harness", spec=spec, artifact={"b": 1, "a": 2},
                             evaluation=evaluation, digest="d" * 64, created_by="someone")

    one = row({"z": 1, "a": {"y": 1, "b": 2}}, {"run_id": "r1", "scores": [1], "finished_at": "t1"})
    two = row({"a": {"b": 2, "y": 1}, "z": 1}, {"run_id": "r1", "scores": [9], "finished_at": "t2"})
    assert release_export.export_yaml(one) == release_export.export_yaml(two)
    text = release_export.export_yaml(one)
    assert text.index("a:") < text.index("z:") and "t1" not in text


def test_export_is_scoped_to_the_bundles_workspace(client):
    bundle = bundle_via_api(client, seed_agent())
    add_workspace("elsewhere")
    res = client.get(f"/api/release-bundles/{bundle['id']}/export",
                     headers={"X-Workspace": "elsewhere"})
    assert res.status_code == 404


# ── CLI ───────────────────────────────────────────────────────────────────


def bridge(client):
    def transport(method, url, headers, body):
        res = client.request(method, url.replace("http://testserver", ""), headers=headers,
                             content=body)
        return res.status_code, res.headers, res.content

    return transport


def run_cli(client, *argv):
    out = io.StringIO()
    code = cli.main(["--url", "http://testserver", *argv], transport=bridge(client), out=out)
    return code, out.getvalue()


def test_cli_bundle_prints_the_export_and_matches_the_api(client, tmp_path):
    agent_id = seed_agent()
    code, text = run_cli(client, "bundle", "--agent", "gitops-agent")
    assert code == 0
    listed = client.get(f"/api/agents/{agent_id}/release-bundles").json()["bundles"]
    assert len(listed) == 1  # bundling is idempotent: the CLI did not fork anything
    assert text == client.get(f"/api/release-bundles/{listed[0]['id']}/export").text
    target = tmp_path / "out.yaml"
    assert run_cli(client, "bundle", "--agent", "gitops-agent", "-o", str(target))[0] == 0
    assert target.read_text() == text


def test_cli_promote_opens_a_request_with_both_notes(client):
    seed_agent()
    add_workspace("prod-ws", "prod")
    code, out = run_cli(client, "promote", "--agent", "gitops-agent", "--to", "prod-ws",
                        "--change-note", "ship v3", "--rollback-note", "redeploy v2")
    assert code == 0 and "-> prod-ws [pending]" in out
    promos = client.get("/api/promotions").json()["promotions"]
    assert len(promos) == 1
    assert promos[0]["target_workspace_id"] == "prod-ws"
    assert promos[0]["change_note"] == "ship v3" and promos[0]["rollback_note"] == "redeploy v2"


def test_cli_reports_failures_with_exit_code_one(client, capsys):
    assert run_cli(client, "bundle", "--agent", "no-such-agent")[0] == 1
    assert "no agent named 'no-such-agent'" in capsys.readouterr().err
    code, _ = run_cli(client, "promote", "--agent", "x", "--to", "y",
                      "--change-note", "c", "--rollback-note", "r")
    assert code == 1


def test_cli_logs_in_and_pins_the_session_cookie(monkeypatch):
    seen = []

    def transport(method, url, headers, body):
        seen.append((url, headers.get("Cookie")))
        if url.endswith("/api/auth/login"):
            return 200, {"set-cookie": "launchpad_session=tok123; Path=/; Secure; HttpOnly"}, b"{}"
        return 200, {}, b'{"agents": []}'

    out = io.StringIO()
    code = cli.main(["--url", "http://h", "--user", "u", "--password", "p", "bundle",
                     "--agent", "a"], transport=transport, out=out)
    assert code == 1  # no such agent path is fine here; we only inspect the cookie
    assert seen[0][1] is None
    assert all(cookie == "launchpad_session=tok123" for _, cookie in seen[1:])
