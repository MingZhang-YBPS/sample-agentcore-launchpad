"""The project boundary's foundation (design §3–§5): effective permissions, the
automatic ORM criteria, write stamping, project resolution, the default-project
membership sync — and, through the API, that a member of project A sees none of
project B while a workshop-shaped single-project console behaves as before."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import (
    ProjectStampError,
    owned_get,
    project_context,
)
from app.evaluation.models import EvalRun
from app.main import create_app
from app.models.ledger import Agent, ChatSession, Deployment
from app.models.projects import Project, ProjectMember, ProjectWorkspace
from app.services import access
from app.services import users as users_service
from tests.conftest import TEST_DEPARTMENT, TEST_PROJECT

# the `default` workspace's one project (conftest): the "home" project of these tests
HOME = TEST_PROJECT
ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)


# ── effective permissions (§3.4) ───────────────────────────────────────────────


def _keys(role_key, *, custom=None, account_role="member", account_overrides=None):
    return access.template_keys(role_key, custom or {}, account_role=account_role,
                                account_overrides=account_overrides)


def test_templates_follow_the_design():
    owner = access.ROLE_TEMPLATES["project_owner"].keys
    assert "project.members" in owner and "agents.deploy" in owner
    # the owner assigns approvals and signatures, never holds them (§3.2)
    assert not owner & {"criteria.sign", "golden.admit", "judge.calibrate",
                        "promotion.approve", "waiver.approve", "release.sign"}
    assert access.ROLE_TEMPLATES["viewer"].keys == frozenset()
    assert access.ROLE_TEMPLATES["sme"].keys == {"project.use", "identity.grant"}
    assert "release.sign" in access.ROLE_TEMPLATES["release_ops"].keys
    assert "criteria.sign" in access.ROLE_TEMPLATES["biz_owner"].keys


def test_inherit_reads_the_global_keys_plus_use_and_write():
    member = _keys("inherit")
    assert {"project.use", "project.write", "agents.deploy"} <= member
    assert "promotion.approve" not in member
    granted = _keys("inherit", account_overrides={"criteria.sign": True, "agents.deploy": False})
    assert "criteria.sign" in granted and "agents.deploy" not in granted
    assert "promotion.approve" in _keys("inherit", account_role="operator")


def test_custom_roles_and_overrides():
    custom = {"outsourced": frozenset({"project.use", "agents.deploy"})}
    assert _keys("outsourced", custom=custom) == {"project.use", "agents.deploy"}
    assert _keys("unknown-role") == frozenset()
    base = access.ROLE_TEMPLATES["developer"].keys
    narrowed = access.apply_overrides(base, {"agents.delete": False, "release.sign": True,
                                             "not.a.key": True})
    assert "agents.delete" not in narrowed and "release.sign" in narrowed
    assert "not.a.key" not in narrowed


# ── ledger helpers ────────────────────────────────────────────────────────────


def _project(pid: str, *, workspaces=(DEFAULT_WORKSPACE_ID,), default=False,
             department_id: str | None = TEST_DEPARTMENT) -> None:
    """A business project (a department, so agents may be created in it) bound to
    ``workspaces``; ``_project("default")`` binds the existing default project back."""
    db = SessionLocal()
    try:
        with project_context(None):
            if db.get(Project, pid) is None:
                db.add(Project(id=pid, name=pid.upper(), status="active",
                               department_id=department_id))
            for ws in workspaces:
                if db.get(ProjectWorkspace, (pid, ws)) is None:
                    db.add(ProjectWorkspace(project_id=pid, workspace_id=ws,
                                            is_workspace_default=default))
            access.bump_epoch(db)
            db.commit()
    finally:
        db.close()


def _member_of(pid: str, user_id: str, role_key: str = "developer") -> None:
    db = SessionLocal()
    try:
        db.add(ProjectMember(project_id=pid, user_id=user_id, role_key=role_key))
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()


def _agent(name: str, pid: str | None, *, system_key: str | None = None) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime",
                        status="active", spec={"name": name}, system_key=system_key)
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


# ── the ORM criteria and stamping (§5.2) ──────────────────────────────────────


def test_reads_are_confined_to_the_current_project_and_platform_rows():
    _project("alpha")
    a = _agent("alpha-agent", "alpha")
    d = _agent("default-agent", HOME)
    preset = _agent("preset", None, system_key="aws-agent-solution-architect")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            seen = set(db.scalars(select(Agent.id)))
            assert seen == {a, preset}  # never default's agent; the preset is platform
            assert owned_get(db, Agent, d) is None
            assert owned_get(db, Agent, a) is not None
        with project_context(None):
            assert {a, d, preset} <= set(db.scalars(select(Agent.id)))
    finally:
        db.close()


def test_owned_get_also_guards_an_identity_map_hit():
    _project("alpha")
    d = _agent("default-agent", HOME)
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.get(Agent, d) is not None  # now in the identity map
        with project_context("alpha"):
            assert owned_get(db, Agent, d) is None
    finally:
        db.close()


def test_the_app_session_get_is_guarded_too():
    """`db.get` in request code needs no owned_get: SessionLocal's class re-checks it."""
    _project("alpha")
    d = _agent("default-agent", HOME)
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.get(Agent, d) is not None
        with project_context("alpha"):
            assert db.get(Agent, d) is None
    finally:
        db.close()


def test_bulk_update_and_delete_are_confined_too():
    _project("alpha")
    a = _agent("alpha-agent", "alpha")
    d = _agent("default-agent", HOME)
    db = SessionLocal()
    try:
        with project_context("alpha"):
            db.query(Agent).filter(Agent.status == "active").update({"status": "failed"})
            db.commit()
        with project_context(None):
            assert db.get(Agent, a).status == "failed"
            db.expire_all()
            assert db.get(Agent, d).status == "active"
    finally:
        db.close()


def test_a_child_row_takes_its_parents_project():
    _project("alpha")
    a = _agent("alpha-agent", "alpha")
    db = SessionLocal()
    try:
        with project_context(HOME):  # the parent wins over the current project
            db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=a,
                               session_id="s" * 40, actor_id="river"))
            run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=a, agent_name="x",
                          mode="dataset", status="pending")
            db.add(run)
            db.commit()
        with project_context(None):
            assert db.scalar(select(ChatSession.project_id)) == "alpha"
            assert db.get(EvalRun, run.id).project_id == "alpha"
    finally:
        db.close()


def test_a_platform_parent_keeps_its_shared_children_platform_level():
    _project("alpha")
    preset = _agent("preset", None, system_key="aws-agent-solution-architect")
    db = SessionLocal()
    try:
        with project_context("alpha"):  # even while some project is current
            row = Deployment(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=preset)
            db.add(row)
            db.commit()
        assert row.project_id is None
    finally:
        db.close()


def test_a_row_with_no_project_source_fails_in_a_multi_project_workspace():
    _project("alpha")  # default now holds two projects
    db = SessionLocal()
    try:
        db.add(EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="ghost", agent_name="x",
                       mode="dataset", status="pending"))
        with pytest.raises(ProjectStampError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_a_single_project_workspace_files_unscoped_rows_unambiguously():
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="ghost", agent_name="x",
                      mode="dataset", status="pending")
        db.add(run)
        db.commit()
        assert run.project_id == HOME
    finally:
        db.close()


# ── resolving the request's project (§2.6, §5.1.1) ────────────────────────────


class _Who:
    def __init__(self, user_id, role="member"):
        self.user_id, self.role = user_id, role


def _user(username: str, *, role: str = "member", grant: bool = True) -> str:
    db = SessionLocal()
    try:
        user, _ = users_service.create_user(
            db, username=username, email=f"{username}@acme-corp.com", role=role,
            password="sufficient-pass", workspaces=[DEFAULT_WORKSPACE_ID] if grant else [],
            created_by="test",
        )
        return user.id
    finally:
        db.close()


def _resolve(identity, requested="", method="GET"):
    db = SessionLocal()
    try:
        return access.resolve_project(db, identity, DEFAULT_WORKSPACE_ID, requested, method)
    finally:
        db.close()


def test_resolution_rules():
    _project("alpha")
    uid = _user("dana", grant=False)
    with pytest.raises(AppError) as err:
        _resolve(_Who(uid))
    assert err.value.code == "project.none"
    _member_of("alpha", uid, "sme")
    scope = _resolve(_Who(uid))  # one reachable project: no header needed
    assert (scope.id, scope.bypass) == ("alpha", False)
    assert scope.can("project.use") and not scope.can("agents.deploy")
    with pytest.raises(NotFoundError) as err:
        _resolve(_Who(uid), HOME)  # bound here, but not theirs → 404, never 403
    assert err.value.code == "project.not_found"
    with pytest.raises(NotFoundError):
        _resolve(_Who(uid), "no-such-project")
    admin = _resolve(_Who(None, "admin"))
    assert (admin.id, admin.bypass) == (HOME, True)
    assert _resolve(_Who(None, "admin"), "alpha").can("release.sign")


def test_an_archived_project_serves_reads_only():
    _project("alpha")
    uid = _user("erin", grant=False)
    _member_of("alpha", uid)
    db = SessionLocal()
    try:
        db.execute(text("UPDATE projects SET status = 'archived' WHERE id = 'alpha'"))
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()
    assert _resolve(_Who(uid), "alpha").status == "archived"
    with pytest.raises(AppError) as err:
        _resolve(_Who(uid), "alpha", "POST")
    assert err.value.code == "project.archived"


def test_a_membership_change_is_seen_on_the_next_resolution():
    _project("alpha")
    uid = _user("fay", grant=False)
    _member_of("alpha", uid, "viewer")
    assert not _resolve(_Who(uid)).can("project.use")
    db = SessionLocal()
    try:
        db.execute(text("UPDATE project_members SET role_key = 'developer' WHERE user_id = :u"),
                   {"u": uid})
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()
    assert _resolve(_Who(uid)).can("agents.deploy")


# ── the default project follows workspace grants (§8.5) ───────────────────────


def _membership(uid: str) -> str | None:
    db = SessionLocal()
    try:
        return db.scalar(select(ProjectMember.role_key).where(
            ProjectMember.project_id == HOME, ProjectMember.user_id == uid))
    finally:
        db.close()


def test_grants_and_revocations_keep_the_default_project_in_step():
    uid = _user("gus")
    assert _membership(uid) == "inherit"
    db = SessionLocal()
    try:
        user = db.get(users_service.User, uid)
        users_service.set_workspace_grants(db, user, [])
        db.commit()
    finally:
        db.close()
    assert _membership(uid) is None
    admin_id = _user("hal", role="admin", grant=False)
    assert _membership(admin_id) is None  # admins bypass, they never hold rows


def test_an_inherit_membership_reaches_only_the_accounts_own_workspaces():
    from app.services.workspace import granted_workspace_ids

    db = SessionLocal()
    try:
        db.execute(text(
            "INSERT INTO workspaces (id, name, account_id, region, bootstrap_status, resources,"
            " created_at, updated_at) VALUES ('prod', 'Prod', '999', 'us-east-1', 'ready', '{}',"
            " '2026-10-14 00:00:00', '2026-10-14 00:00:00')"
        ))
        db.execute(text(
            "INSERT INTO project_workspaces (project_id, workspace_id, is_workspace_default,"
            " enabled_at) VALUES ('default', 'prod', 1, '2026-10-14 00:00:00')"
        ))
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()
    uid = _user("ivy")  # granted `default` only
    db = SessionLocal()
    try:
        assert granted_workspace_ids(db, uid) == [DEFAULT_WORKSPACE_ID]
    finally:
        db.close()
    with pytest.raises(AppError):
        db = SessionLocal()
        try:
            access.resolve_project(db, _Who(uid), "prod", "", "GET")
        finally:
            db.close()


# ── end to end through the API ────────────────────────────────────────────────


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _login(app, username: str) -> TestClient:
    client = TestClient(app, client=PEER)
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        db.commit()
    finally:
        db.close()
    res = client.post("/api/auth/login", json={"username": username, "password": "sufficient-pass"})
    assert res.status_code == 200, res.text
    return client


def test_a_member_of_one_project_never_sees_another(app):
    _project("alpha")
    alpha_agent = _agent("alpha-agent", "alpha")
    default_agent = _agent("default-agent", HOME)
    uid = _user("joan", grant=False)
    _member_of("alpha", uid)
    jo = _login(app, "joan")
    names = {a["name"] for a in jo.get("/api/agents").json()["agents"]}
    assert "alpha-agent" in names and "default-agent" not in names
    assert jo.get(f"/api/agents/{default_agent}").status_code == 404
    assert jo.get(f"/api/agents/{alpha_agent}").status_code == 200
    other = jo.get("/api/agents", headers={"X-Project": HOME})
    assert other.status_code == 404 and other.json()["code"] == "project.not_found"


def test_a_workshop_shaped_console_needs_no_project_header(app):
    _agent("default-agent", HOME)
    _user("kim")  # granted the default workspace, as the workshop seeds its accounts
    kim = _login(app, "kim")
    res = kim.get("/api/agents")
    assert res.status_code == 200, res.text
    assert [a["name"] for a in res.json()["agents"]] == ["default-agent"]
    created = kim.post("/api/eval/datasets", json={"name": "ds-1", "items": [{"prompt": "hi"}]})
    assert created.status_code in (200, 201), created.text
    db = SessionLocal()
    try:
        assert db.execute(text("SELECT project_id FROM eval_datasets")).scalar_one() == HOME
    finally:
        db.close()
