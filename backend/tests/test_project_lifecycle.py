"""Project archive / delete lifecycle (projects design §11.5, G11-life): archived means
read-only, the deletion request writes an export bundle and starts a cooling-off that
can be cancelled, the resumable `project_purge` job tears the agents down through the
injectable agent teardown (no AWS here), deletes the project's ledger rows and leaves
a tombstone; the default project cannot be deleted."""

import json
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project, project_context
from app.models.ledger import Agent, ApiKey, AuditEvent, ChatSession, Job
from app.models.platform_settings import PlatformSetting
from app.models.projects import Project, ProjectMember, ProjectWorkspace
from app.services import inflight, project_purge
from app.services.audit import record_audit_event
from tests.conftest import TEST_DEPARTMENT, TEST_PROJECT
from tests.test_project_scope import (  # noqa: F401
    ADMIN,
    PEER,
    _agent,
    _login,
    _member_of,
    _user,
    app,
)

WS = DEFAULT_WORKSPACE_ID


def _admin(app):  # noqa: F811
    from fastapi.testclient import TestClient

    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


@pytest.fixture
def world(app, tmp_path, monkeypatch):  # noqa: F811
    """alpha (dev a developer) with two agents and some rows; default keeps one agent.
    Exports go to tmp, the teardown is a fake and the job runs inline."""
    monkeypatch.setattr(project_purge, "EXPORT_DIR", tmp_path / "exports")
    torn: list[tuple[str, str | None]] = []
    fail: set[str] = set()

    def teardown(db, agent):
        torn.append((agent.id, current_project()))
        if agent.id in fail:
            fail.discard(agent.id)
            raise RuntimeError("DeleteAgentRuntime throttled")
        agent.status = "deleted"
        db.commit()
        return True

    monkeypatch.setattr(project_purge, "AGENT_TEARDOWN", teardown)
    monkeypatch.setattr(project_purge, "_spawn", project_purge.run_purge)
    admin = _admin(app)
    res = admin.post("/api/projects", json={"id": "alpha", "name": "Alpha",
                                            "workspaces": [WS],
                                            "department_id": TEST_DEPARTMENT})
    assert res.status_code == 201, res.text
    dev = _user("dev", grant=False)
    _member_of("alpha", dev)
    a1, a2 = _agent("alpha-one", "alpha"), _agent("alpha-two", "alpha")
    keep = _agent("default-agent", TEST_PROJECT)
    db = SessionLocal()
    try:
        with project_context("alpha"):
            db.add(ChatSession(workspace_id=WS, agent_id=a1, session_id="s" * 40))
            db.add(ApiKey(workspace_id=WS, name="k", prefix="lp_z", key_hash="h" * 64,
                          created_by=dev))
            db.add(Job(workspace_id=WS, type="deploy_agent", status="succeeded",
                       payload={"agent_id": a1}))
            db.commit()
            record_audit_event(workspace_id=WS, actor="dev", action="agent.create",
                               target=f"agent:{a1}", db=db, project_id="alpha")
            db.commit()
    finally:
        db.close()
    return SimpleNamespace(app=app, admin=admin, dev=dev, a1=a1, a2=a2, keep=keep,
                           torn=torn, fail=fail)


def _row(model, ident):
    db = SessionLocal()
    try:
        with project_context(None):
            return db.get(model, ident)
    finally:
        db.close()


def _set_cooling_off(days: str) -> None:
    db = SessionLocal()
    try:
        db.add(PlatformSetting(key=project_purge.COOLING_OFF_SETTING, value=days))
        db.commit()
    finally:
        db.close()


def _expire_cooling_off(project_id: str = "alpha") -> None:
    db = SessionLocal()
    try:
        row = db.get(Project, project_id)
        state = project_purge.lifecycle(row)
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        state["deletion"] = {**state["deletion"], "purge_after": past}
        project_purge.set_lifecycle(row, state)
        db.commit()
    finally:
        db.close()


def _request(world):
    assert world.admin.post("/api/projects/alpha/archive").status_code == 200
    res = world.admin.post("/api/projects/alpha/delete-request")
    assert res.status_code == 200, res.text
    return res.json()


# ── archived is read-only ─────────────────────────────────────────────────────


def test_an_archived_project_is_read_only_everywhere(world):
    assert world.admin.post("/api/projects/alpha/archive").status_code == 200
    dev = _login(world.app, "dev")
    alpha = {"X-Project": "alpha"}
    for client in (dev, world.admin):  # a data bypass does not lift it either
        res = client.delete(f"/api/agents/{world.a1}", headers=alpha)
        assert res.status_code == 409 and res.json()["code"] == "project.archived"
        assert client.get(f"/api/agents/{world.a1}", headers=alpha).status_code == 200
    for method, path, body in (
        ("patch", "/api/projects/alpha", {"description": "x"}),
        ("put", "/api/projects/alpha/members", {"upsert": []}),
        ("put", "/api/projects/alpha/roles/qa", {"name": "QA"}),
    ):
        res = getattr(world.admin, method)(path, json=body)
        assert res.status_code == 409 and res.json()["code"] == "project.archived", path
    res = world.admin.post("/api/projects/alpha/transfer",
                           json={"resource_id": world.a1, "to_project_id": TEST_PROJECT})
    assert res.status_code == 409


# ── preconditions ─────────────────────────────────────────────────────────────


def test_the_default_project_cannot_be_deleted(world):
    res = world.admin.post("/api/projects/default/delete-request")
    assert res.status_code == 409 and res.json()["code"] == "project.default_locked"


def test_only_an_archived_project_can_be_requested(world):
    res = world.admin.post("/api/projects/alpha/delete-request")
    assert res.status_code == 409 and res.json()["code"] == "project.not_archived"
    assert world.admin.post("/api/projects/nope/delete-request").status_code == 404


def test_a_workspace_default_project_must_hand_that_over_first(world):
    res = world.admin.put("/api/projects/alpha/workspaces", json={"make_default": [WS]})
    assert res.status_code == 200, res.text
    assert world.admin.post("/api/projects/alpha/archive").status_code == 200
    res = world.admin.post("/api/projects/alpha/delete-request")
    assert res.status_code == 409 and res.json()["code"] == "project.workspace_default"


def test_lifecycle_routes_are_admin_only(world):
    dev = _login(world.app, "dev")
    for method, path in (("get", "/api/projects/alpha/lifecycle"),
                         ("post", "/api/projects/alpha/export"),
                         ("post", "/api/projects/alpha/delete-request"),
                         ("post", "/api/projects/alpha/delete-request/cancel"),
                         ("post", "/api/projects/alpha/purge?dry_run=true")):
        assert getattr(dev, method)(path).status_code == 403, path


# ── request, export, cooling-off, cancel ──────────────────────────────────────


def test_the_request_writes_the_export_and_starts_the_cooling_off(world):
    view = _request(world)
    deletion = view["deletion"]
    assert view["cooling_off_days"] == project_purge.DEFAULT_COOLING_OFF_DAYS
    assert deletion["purge_ready"] is False and deletion["seconds_left"] > 6 * 86400
    assert deletion["requested_by"] == ADMIN["username"]
    export = view["exports"][0]
    assert export["id"] == deletion["export_id"]
    path = project_purge.export_path("alpha", export["id"])
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        agents = zf.read("tables/agents.jsonl").decode().splitlines()
        audit = zf.read("audit.csv").decode()
    assert manifest["project"]["id"] == "alpha" and manifest["tables"]["agents"] == 2
    assert len(agents) == 2 and world.keep not in "".join(agents)
    assert sum(n.startswith("agents/") and n.endswith(".spec.yaml") for n in names) == 2
    assert "agent.create" in audit and "project.archive" in audit
    assert "tables/criteria.jsonl" in names and "tables/eval_datasets.jsonl" in names
    res = world.admin.get(f"/api/projects/alpha/exports/{export['id']}")
    assert res.status_code == 200 and res.content == path.read_bytes()
    # an export id of another project, or an unknown one, reads as missing
    res = world.admin.post("/api/projects", json={"id": "beta", "name": "Beta",
                                                  "workspaces": [WS],
                                                  "department_id": TEST_DEPARTMENT})
    assert res.status_code == 201
    assert world.admin.get(f"/api/projects/beta/exports/{export['id']}").status_code == 404
    assert world.admin.get("/api/projects/alpha/exports/" + "0" * 32).status_code == 404
    # cooling-off: the purge waits, and the request cannot be doubled
    res = world.admin.post("/api/projects/alpha/purge")
    assert res.status_code == 409 and res.json()["code"] == "project.cooling_off"
    res = world.admin.post("/api/projects/alpha/delete-request")
    assert res.status_code == 409 and res.json()["code"] == "project.deletion_pending"
    assert world.torn == []


def test_cancel_during_the_cooling_off(world):
    _request(world)
    res = world.admin.post("/api/projects/alpha/unarchive")
    assert res.status_code == 409 and res.json()["code"] == "project.deletion_pending"
    res = world.admin.post("/api/projects/alpha/delete-request/cancel")
    assert res.status_code == 200 and res.json()["deletion"] is None
    assert res.json()["exports"]  # the bundle stays
    res = world.admin.post("/api/projects/alpha/purge")
    assert res.status_code == 409 and res.json()["code"] == "project.deletion_not_requested"
    assert world.admin.post("/api/projects/alpha/unarchive").json()["status"] == "active"
    res = world.admin.post("/api/projects/alpha/delete-request/cancel")
    assert res.status_code == 409


def test_the_cooling_off_is_a_platform_setting(world):
    _set_cooling_off("0")
    view = _request(world)
    assert view["cooling_off_days"] == 0 and view["deletion"]["purge_ready"] is True


# ── the purge job ─────────────────────────────────────────────────────────────


def test_the_purge_deletes_what_the_dry_run_listed_and_leaves_a_tombstone(world):
    _request(world)
    _expire_cooling_off()
    plan = world.admin.post("/api/projects/alpha/purge?dry_run=true").json()
    assert plan["dry_run"] is True
    assert {a["id"] for a in plan["agents"]} == {world.a1, world.a2}
    assert plan["rows"]["agents"] == 2 and plan["rows"]["chat_sessions"] == 1
    assert plan["rows"]["project_members"] == 1 and "audit_events" not in plan["rows"]
    res = world.admin.post("/api/projects/alpha/purge")
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]
    assert res.json()["rows"] == plan["rows"]
    # each agent went through the teardown, inside the project's context
    assert sorted(world.torn) == sorted([(world.a1, "alpha"), (world.a2, "alpha")])
    job = _row(Job, job_id)
    assert job.status == "succeeded", job.error
    assert job.payload["rows"] == plan["rows"]
    project = _row(Project, "alpha")
    assert project.status == "deleted" and project.name == "Alpha"
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.scalars(select(Agent.id).where(Agent.project_id == "alpha")).all() == []
            assert db.get(Agent, world.keep) is not None
            assert db.scalars(select(ProjectMember).where(
                ProjectMember.project_id == "alpha")).all() == []
            assert db.scalars(select(ProjectWorkspace).where(
                ProjectWorkspace.project_id == "alpha")).all() == []
            kept = db.scalars(select(AuditEvent.action).where(
                AuditEvent.project_id == "alpha")).all()
            assert "agent.create" in kept  # the journal outlives the project
            assert db.scalars(select(AuditEvent).where(
                AuditEvent.action == "project.delete",
                AuditEvent.target == "project:alpha")).first() is not None
    finally:
        db.close()
    # a tombstone: listed only when asked for, still readable by an administrator
    listed = {p["id"] for p in world.admin.get("/api/projects").json()["projects"]}
    assert "alpha" not in listed
    tombs = world.admin.get("/api/projects?status=deleted").json()["projects"]
    assert [p["id"] for p in tombs] == ["alpha"]
    view = world.admin.get("/api/projects/alpha/lifecycle").json()
    assert view["status"] == "deleted" and view["deleted"]["job_id"] == job_id
    assert view["purge_job"]["status"] == "succeeded"
    for path in ("/api/projects/alpha/delete-request", "/api/projects/alpha/purge",
                 "/api/projects/alpha/archive", "/api/projects/alpha/unarchive"):
        assert world.admin.post(path).status_code == 409, path
    res = world.admin.put("/api/projects/alpha/workspaces", json={"bind": [WS]})
    assert res.status_code == 409 and res.json()["code"] == "project.deleted"
    # the export bundle survives the project
    export_id = view["exports"][0]["id"]
    assert world.admin.get(f"/api/projects/alpha/exports/{export_id}").status_code == 200
    # the member lost it
    dev = _login(world.app, "dev")
    assert dev.get("/api/projects/alpha").status_code == 404


def test_a_failed_purge_returns_to_archived_and_resumes_from_its_checkpoints(world):
    _request(world)
    _expire_cooling_off()
    world.fail.add(world.a2)
    res = world.admin.post("/api/projects/alpha/purge")
    assert res.status_code == 202
    first = _row(Job, res.json()["job_id"])
    assert first.status == "failed" and first.error.startswith("agents:")
    assert first.payload["agents_done"] == [world.a1]
    project = _row(Project, "alpha")
    assert project.status == "archived"
    assert project_purge.lifecycle(project)["deletion"]  # the request is kept
    res = world.admin.post("/api/projects/alpha/purge")
    assert res.status_code == 202
    second = _row(Job, res.json()["job_id"])
    assert second.status == "succeeded", second.error
    # a1 was not torn down twice
    assert [a for a, _ in world.torn] == [world.a1, world.a2, world.a2]
    assert _row(Project, "alpha").status == "deleted"


def test_a_running_purge_is_not_started_twice_but_a_stale_one_is_taken_over(world):
    _request(world)
    _expire_cooling_off()
    db = SessionLocal()
    try:
        with project_context(None):
            job = Job(type=project_purge.JOB_TYPE, workspace_id=None, status="running",
                      project_id="alpha", payload={"project_id": "alpha", "stage": "ledger",
                                                   "agents_done": [world.a1, world.a2]})
            db.add(job)
            db.commit()
            job_id = job.id
    finally:
        db.close()
    res = world.admin.post("/api/projects/alpha/purge")
    assert res.status_code == 409 and res.json()["code"] == "project.purge_running"
    db = SessionLocal()
    try:
        with project_context(None):
            stale = db.get(Job, job_id)
            stale.updated_at = datetime.now(UTC) - project_purge.STALE_AFTER * 2
            db.commit()
    finally:
        db.close()
    res = world.admin.post("/api/projects/alpha/purge")
    assert res.status_code == 202, res.text
    assert _row(Job, job_id).status == "failed"
    assert _row(Job, res.json()["job_id"]).status == "succeeded"
    assert world.torn == []  # both agents were in the stale job's checkpoint


def test_due_purges_start_after_the_cooling_off(world):
    _set_cooling_off("0")
    _request(world)
    db = SessionLocal()
    try:
        started = project_purge.start_due_purges(db)
    finally:
        db.close()
    assert len(started) == 1
    assert _row(Job, started[0]).status == "succeeded"
    assert _row(Project, "alpha").status == "deleted"


def test_a_restart_resumes_a_live_purge(world):
    db = SessionLocal()
    try:
        with project_context(None):
            db.add(Job(type=project_purge.JOB_TYPE, workspace_id=None, status="running",
                       project_id="alpha", payload={"project_id": "alpha"}))
            db.commit()
        items = [i for i in inflight.collect_inflight(db)
                 if i.label.startswith(project_purge.JOB_TYPE)]
    finally:
        db.close()
    assert len(items) == 1 and items[0].restart_outcome == inflight.RESUMES
    assert items[0].handled_by == "resume_pending_jobs"


def test_the_default_teardown_is_the_agent_delete_pipeline(monkeypatch):
    from app.routers import agents as agents_router
    from app.services import workspace as workspace_service

    calls = []
    monkeypatch.setattr(workspace_service, "context_for_workspace", lambda ws: f"ctx:{ws}")
    monkeypatch.setattr(agents_router, "delete_agent_row",
                        lambda db, agent, ctx: calls.append((agent.id, ctx)) or True)
    agent = SimpleNamespace(id="a1", workspace_id=WS)
    assert project_purge._default_agent_teardown(None, agent) is True
    assert calls == [("a1", f"ctx:{WS}")]
