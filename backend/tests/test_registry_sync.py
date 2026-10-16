"""`POST /api/registry/sync-defaults` queues the `registry_sync_defaults` job (202) and
`GET /api/registry/sync-defaults/{job_id}` reports its per-record results — the sync
waits every new record out of CREATING, longer than a 60 s origin timeout allows."""

import pytest

from app.core.db import SessionLocal
from app.models.ledger import Job
from app.services import registry_console, registry_sync
from tests.conftest import ws_ctx

MCP = {"name": "weather", "type": "MCP", "record_id": "r-mcp", "created": True}
SKILL = {"name": "sample-skill", "type": "AGENT_SKILLS", "record_id": "r-sk",
         "created": False}


@pytest.fixture
def started(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(registry_sync, "start_async", calls.append)
    return calls


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        return db.get(Job, job_id)
    finally:
        db.close()


def test_post_queues_a_job_and_returns_202(client, started):
    res = client.post("/api/registry/sync-defaults")
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "queued" and body["done"] is False
    assert body["results"] == [] and body["started"] is True
    assert started == [body["job_id"]]
    assert _job(body["job_id"]).type == registry_sync.JOB_TYPE


def test_a_second_post_returns_the_sync_in_flight(client, started):
    first = client.post("/api/registry/sync-defaults").json()
    again = client.post("/api/registry/sync-defaults")
    assert again.status_code == 202
    assert again.json()["job_id"] == first["job_id"]
    assert again.json()["started"] is False
    assert started == [first["job_id"]]  # not started twice


def test_the_job_records_each_result_and_the_status_route_reports_them(
        client, started, monkeypatch):
    job_id = client.post("/api/registry/sync-defaults").json()["job_id"]
    seen: list[str] = []

    def fake_ensure(_ws, on_result=None):
        for row in (MCP, SKILL):
            on_result(row)
            seen.append(_job(job_id).status)
        return [MCP, SKILL]

    monkeypatch.setattr(registry_console, "ensure_default_records", fake_ensure)
    done: list[str] = []
    registry_sync.execute(job_id, workspace_factory=lambda _wid: ws_ctx(),
                          on_done=done.append)
    assert seen == ["running", "running"]  # progress is visible while it runs
    res = client.get(f"/api/registry/sync-defaults/{job_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "succeeded" and body["done"] is True
    assert body["results"] == [MCP, SKILL] and body["error"] is None
    assert done == ["default"]  # the attachables cache is dropped after the sync
    # the generic job route reads it too
    assert client.get(f"/api/jobs/{job_id}").json()["payload"]["results"] == [MCP, SKILL]


def test_a_failed_sync_ends_failed_with_the_error(client, started, monkeypatch):
    job_id = client.post("/api/registry/sync-defaults").json()["job_id"]

    def boom(_ws, on_result=None):
        on_result(MCP)
        raise RuntimeError("registry throttled")

    monkeypatch.setattr(registry_console, "ensure_default_records", boom)
    registry_sync.execute(job_id, workspace_factory=lambda _wid: ws_ctx())
    body = client.get(f"/api/registry/sync-defaults/{job_id}").json()
    assert body["status"] == "failed" and body["done"] is True
    assert body["results"] == [MCP]
    assert "registry throttled" in body["error"]
    # a new POST after a finished sync starts a fresh job
    again = client.post("/api/registry/sync-defaults").json()
    assert again["job_id"] != job_id and again["started"] is True


def test_a_resumed_job_re_runs_from_scratch(client, started, monkeypatch):
    job_id = client.post("/api/registry/sync-defaults").json()["job_id"]
    db = SessionLocal()
    row = db.get(Job, job_id)
    row.status, row.payload = "running", {**row.payload, "results": [MCP]}
    db.commit()
    db.close()
    monkeypatch.setattr(registry_console, "ensure_default_records",
                        lambda _ws, on_result=None: [on_result(r) for r in (MCP, SKILL)])
    registry_sync.execute(job_id, workspace_factory=lambda _wid: ws_ctx())
    job = _job(job_id)
    assert job.status == "succeeded"
    assert job.payload["results"] == [MCP, SKILL]  # not duplicated
    assert "resumed after a restart" in job.log


def test_status_of_an_unknown_or_foreign_job_is_404(client):
    assert client.get("/api/registry/sync-defaults/nope").status_code == 404
    db = SessionLocal()
    other = Job(workspace_id="default", type="deploy_agent", payload={})
    db.add(other)
    db.commit()
    other_id = other.id
    db.close()
    res = client.get(f"/api/registry/sync-defaults/{other_id}")
    assert res.status_code == 404 and res.json()["code"] == "job.not_found"


def test_ensure_default_records_reports_each_result(monkeypatch):
    from app.services.agentcore import registry as reg

    monkeypatch.setattr(registry_console, "registry_control_client", lambda _ws: object())
    monkeypatch.setattr(registry_console, "_registry_id", lambda _ws: "reg-1")
    monkeypatch.setattr(registry_console.mcp_client, "tools_list", lambda _ws: [
        {"name": "weather___get", "description": "d", "inputSchema": {}}])
    monkeypatch.setattr(registry_console, "upload_skill_bundle", lambda _ws: {
        "skill_md": "---\nname: s\n---", "definition": {"description": "x"}})
    monkeypatch.setattr(reg, "upsert_record",
                        lambda *_a, name, **_k: ({"recordId": f"r-{name}"}, False))
    monkeypatch.setattr(reg, "build_mcp_descriptors", lambda **_k: {})
    monkeypatch.setattr(reg, "build_skills_descriptors", lambda **_k: {})
    seen: list[dict] = []
    results = registry_console.ensure_default_records(ws_ctx(), on_result=seen.append)
    assert seen == results and [r["type"] for r in results] == ["MCP", "AGENT_SKILLS"]
