"""Audit events carry their project and actor id (design §5.10): the current project
is stamped (else NULL — never the workspace's sole project), structure events are
platform-level, and `GET /api/audit` filters by project (across workspaces) and by
actor (username or `users.id`)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import actor as actor_mod
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.main import create_app
from app.models.ledger import AuditEvent, Workspace
from app.models.projects import Project, ProjectWorkspace
from app.services import access
from app.services.audit import is_structure_action, record_audit_event, record_structure_event

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
OTHER_WS = "other-ws"


@pytest.fixture(autouse=True)
def _no_current_actor():
    actor_mod.set_current_actor(None, None)
    yield
    actor_mod.set_current_actor(None, None)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(app) -> TestClient:
    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


def _project(pid: str, *, workspaces=(DEFAULT_WORKSPACE_ID,)) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            if db.get(Project, pid) is None:
                db.add(Project(id=pid, name=pid.upper(), status="active"))
            for ws in workspaces:
                if db.get(ProjectWorkspace, (pid, ws)) is None:
                    db.add(ProjectWorkspace(project_id=pid, workspace_id=ws,
                                            is_workspace_default=False))
            access.bump_epoch(db)
            db.commit()
    finally:
        db.close()


def _other_workspace() -> None:
    db = SessionLocal()
    try:
        db.add(Workspace(id=OTHER_WS, name="Other", account_id="444455556677",
                         region="us-west-1", bootstrap_status="ready", resources={}))
        db.commit()
    finally:
        db.close()


def _rows() -> list[AuditEvent]:
    db = SessionLocal()
    try:
        with project_context(None):
            return list(db.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
    finally:
        db.close()


# ── the write side ────────────────────────────────────────────────────────────


def test_an_event_takes_the_current_project_else_none():
    _project("alpha")
    with project_context("alpha"):
        record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x", action="in.alpha")
    # no project current: workspace-level, even though `default` is the workspace's
    # project too — the ORM's sole-project fallback must not file it there
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x", action="no.project")
    with project_context("alpha"):  # an explicit None wins over the current project
        record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x",
                           action="explicit.none", project_id=None)
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x",
                       action="explicit.alpha", project_id="alpha")
    got = {r.action: r.project_id for r in _rows()}
    assert got == {"in.alpha": "alpha", "no.project": None, "explicit.none": None,
                   "explicit.alpha": "alpha"}


def test_an_event_joins_the_callers_transaction():
    db = SessionLocal()
    try:
        with project_context("default"):
            record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x",
                               action="rolled.back", db=db)
        db.rollback()
        record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x",
                           action="kept", db=db)
        db.commit()
    finally:
        db.close()
    assert [r.action for r in _rows()] == ["kept"]


def test_the_request_caller_id_is_recorded_beside_the_name():
    actor_mod.set_current_actor("alice", "u-alice")
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="alice", action="a")
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="bob", action="b")
    got = {r.action: r.actor_user_id for r in _rows()}
    assert got == {"a": "u-alice", "b": None}


def test_a_structure_event_is_hub_global_and_of_no_project():
    _project("alpha")
    with project_context("alpha"):  # a project admin page may have one bound
        record_structure_event(actor="operator", action="project.members.add",
                               target="project:alpha/user:mona")
    record_structure_event(actor="operator", action="department.create",
                           target="department:eng", actor_user_id="u-op")
    rows = _rows()
    assert [(r.workspace_id, r.project_id) for r in rows] == [
        (DEFAULT_WORKSPACE_ID, None), (DEFAULT_WORKSPACE_ID, None)]
    assert rows[1].actor_user_id == "u-op"


@pytest.mark.parametrize("action", [
    "project.create", "project.update", "project.archive", "project.unarchive",
    "project.delete", "project.members.add", "project.members.update",
    "project.members.remove", "project.roles.upsert", "project.roles.delete",
    "project.workspaces.bind", "project.workspaces.unbind", "group.create",
    "group.update", "group.archive", "group.members.add", "group.projects.remove",
    "department.update", "admin.grant", "admin.revoke", "user.sessions.revoke",
])
def test_the_designed_structure_actions_are_accepted(action):
    assert is_structure_action(action)


@pytest.mark.parametrize("action", ["project.craete", "user.create", "department.", ""])
def test_anything_else_is_not_a_structure_event(action):
    assert not is_structure_action(action)
    with pytest.raises(ValueError):
        record_structure_event(actor="operator", action=action)
    assert _rows() == []


# ── GET /api/audit ────────────────────────────────────────────────────────────


def test_the_project_filter_spans_the_projects_workspaces(admin):
    _other_workspace()
    _project("alpha", workspaces=(DEFAULT_WORKSPACE_ID, OTHER_WS))
    for ws in (DEFAULT_WORKSPACE_ID, OTHER_WS):
        record_audit_event(workspace_id=ws, actor="x", action=f"alpha.{ws}",
                           project_id="alpha")
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x", action="d.one",
                       project_id="default")
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x", action="ws.one")
    record_structure_event(actor="operator", action="project.create", target="project:alpha")

    events = admin.get("/api/audit", params={"project": "alpha"}).json()["events"]
    assert sorted(e["action"] for e in events) == sorted(
        [f"alpha.{DEFAULT_WORKSPACE_ID}", f"alpha.{OTHER_WS}"])
    assert {e["workspace_id"] for e in events} == {DEFAULT_WORKSPACE_ID, OTHER_WS}
    assert all(e["project_id"] == "alpha" for e in events)

    # workspace-level: this workspace's rows of no project, the structure journal too
    events = admin.get("/api/audit", params={"project": "workspace"}).json()["events"]
    assert sorted(e["action"] for e in events) == ["project.create", "ws.one"]

    # unfiltered: this workspace only, every project
    events = admin.get("/api/audit").json()["events"]
    assert sorted(e["action"] for e in events) == sorted(
        [f"alpha.{DEFAULT_WORKSPACE_ID}", "d.one", "ws.one", "project.create"])
    assert admin.get("/api/audit", params={"project": "nope"}).json()["events"] == []


def test_the_actor_filter_takes_a_username_or_a_user_id(admin):
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="alice", action="a.one",
                       actor_user_id="u-alice")
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="bob", action="b.one")
    by_name = admin.get("/api/audit", params={"actor": "alice"}).json()["events"]
    by_id = admin.get("/api/audit", params={"actor": "u-alice"}).json()["events"]
    assert [e["action"] for e in by_name] == [e["action"] for e in by_id] == ["a.one"]
    assert by_id[0]["actor"] == "alice" and by_id[0]["actor_user_id"] == "u-alice"
    events = admin.get("/api/audit", params={"actor": "bob"}).json()["events"]
    assert [(e["action"], e["actor_user_id"]) for e in events] == [("b.one", None)]


def test_a_member_cannot_read_the_audit_trail(app):
    from app.services import users as users_service

    db = SessionLocal()
    try:
        users_service.create_user(
            db, username="mona", email="mona@acme-corp.com", role="member",
            password="sufficient-pass", workspaces=[DEFAULT_WORKSPACE_ID], created_by="test",
        )
        db.commit()
    finally:
        db.close()
    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login",
                       json={"username": "mona", "password": "sufficient-pass"}).status_code == 200
    assert client.get("/api/audit", params={"project": "default"}).status_code == 403
