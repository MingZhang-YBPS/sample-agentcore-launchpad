"""Phase-1 project boundary (design §5.5) for the architect assistant and the
hub-global account / workspace paths: deny-lists and safety refusals read across
projects, workers run as their row's project, permission re-checks use the project's
keys, the catalog never offers another project's KB, and removing a workspace or a
user leaves no project binding or membership behind."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.assistant import evaluation_assets as assets
from app.assistant import purge as purge_mod
from app.assistant import service
from app.assistant.principal import project_can
from app.assistant.sessions import PrivateSessions, is_assistant_session
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project, project_context
from app.main import create_app
from app.models.assistant import (
    AssistantConversation,
    AssistantMessage,
    AssistantProposal,
    EvaluationAssetOperation,
)
from app.models.ledger import Agent, Job, ResourceCreator, User, Workspace
from app.models.projects import Project, ProjectMember, ProjectWorkspace
from app.services import access, creators
from app.services import users as users_service
from app.services.visibility import Viewer
from tests.conftest import TEST_PROJECT

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)


# ── fixtures (the tests/test_project_scope.py shapes) ─────────────────────────


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


def _member_of(pid: str, user_id: str, role_key: str = "developer") -> None:
    db = SessionLocal()
    try:
        db.add(ProjectMember(project_id=pid, user_id=user_id, role_key=role_key))
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()


def _user(username: str, *, grant: bool = False) -> str:
    db = SessionLocal()
    try:
        user, _ = users_service.create_user(
            db, username=username, email=f"{username}@acme-corp.com", role="member",
            password="sufficient-pass", workspaces=[DEFAULT_WORKSPACE_ID] if grant else [],
            created_by="test",
        )
        return user.id
    finally:
        db.close()


def _conversation(pid: str, principal: str, session_id: str | None = None) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            conv = AssistantConversation(workspace_id=DEFAULT_WORKSPACE_ID, owner="river",
                                         owner_principal=principal, title=f"{pid} chat")
            db.add(conv)
            db.flush()
            if session_id:
                db.add(AssistantMessage(workspace_id=DEFAULT_WORKSPACE_ID,
                                        conversation_id=conv.id, turn=1, role="assistant",
                                        text="hi", runtime_session_id=session_id))
            db.commit()
            return conv.id
    finally:
        db.close()


class _Who:
    def __init__(self, user_id, role="member"):
        self.user_id, self.role = user_id, role

    def can(self, key):  # the account keys: a default member holds agents.deploy
        return key in users_service.default_permissions(self.role)


# ── deny-lists and safety refusals read across projects ───────────────────────


def test_private_sessions_hide_every_other_projects_assistant_turns():
    _project("alpha")
    _conversation(TEST_PROJECT, "user:bob", "s-default-bob")
    _conversation(TEST_PROJECT, "user:ann", "s-default-ann")
    _conversation("alpha", "user:ann", "s-alpha-ann")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            private = PrivateSessions(db, DEFAULT_WORKSPACE_ID, "user:ann")
            # another principal's, in a project the filter would have hidden: still hidden
            assert not private.visible("s-default-bob")
            # the caller's own conversation, but in another project
            assert not private.visible("s-default-ann")
            assert private.visible("s-alpha-ann")
            assert is_assistant_session("s-default-bob")
        # unscoped (workers, single-project consoles): the principal rule alone
        with project_context(None):
            assert PrivateSessions(db, DEFAULT_WORKSPACE_ID, "user:ann").visible("s-default-ann")
    finally:
        db.close()


def test_a_single_project_console_keeps_its_own_sessions_visible():
    _conversation(TEST_PROJECT, "user:ann", "s-ann")
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            private = PrivateSessions(db, DEFAULT_WORKSPACE_ID, "user:ann")
            assert private.visible("s-ann")
    finally:
        db.close()


def test_clearing_refusal_sees_a_purge_job_of_any_project():
    _project("alpha")
    cid = _conversation(TEST_PROJECT, "user:ann")
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="kid", method="harness",
                          status="active", spec={"name": "kid"})
            db.add(agent)
            db.flush()
            db.add(AssistantProposal(workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=cid,
                                     revision=1, source="model", content={}, content_hash="h" * 64,
                                     status="approved", created_by="river", agent_id=agent.id))
            job = Job(workspace_id=DEFAULT_WORKSPACE_ID, type=purge_mod.JOB_TYPE,
                      status="running", payload={"conversation_id": cid})
            db.add(job)
            db.flush()
            db.get(AssistantConversation, cid).purge_job_id = job.id
            db.commit()
            agent_id, job_id = agent.id, job.id
    finally:
        db.close()
    with project_context("alpha"), pytest.raises(Exception) as err:
        purge_mod.refuse_while_clearing(agent_id, "delete")
    assert getattr(err.value, "code", None) == "assistant.agent_clearing"
    assert job_id in str(err.value.detail)


def _operation(pid: str, cid: str, *, name: str, status: str = "succeeded",
               approver: str | None = None) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            op = EvaluationAssetOperation(
                workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=cid, plan_id=f"plan-{name}",
                plan_hash="p" * 64, owner_principal="user:ann", approved_by="ann",
                approver_user_id=approver, account_id="123456789012", region="us-east-1",
                status=status,
                resources=[{"kind": "evaluator", "key": name, "name": name, "status": "created",
                            "result": {"evaluator_id": f"ev-{name}"}}],
            )
            db.add(op)
            db.commit()
            return op.id
    finally:
        db.close()


def test_evaluator_names_stay_unique_across_projects():
    _project("alpha")
    _operation(TEST_PROJECT, _conversation(TEST_PROJECT, "user:ann"), name="tone")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            assert "tone" in assets.ledger_evaluator_names(db, DEFAULT_WORKSPACE_ID)
            # the managed projection stays the project's own
            assert assets.managed_evaluator(db, DEFAULT_WORKSPACE_ID, "ev-tone") is None
    finally:
        db.close()


# ── workers run as their row's project ────────────────────────────────────────


def test_the_asset_worker_runs_as_its_operations_project(monkeypatch):
    _project("alpha")
    op_id = _operation("alpha", _conversation("alpha", "user:ann"), name="w", status="queued")
    seen: list[str | None] = []

    class _FakeRunner:
        def __init__(self, *_args):
            pass

        def run(self):
            seen.append(current_project())

    monkeypatch.setattr(assets, "_Runner", _FakeRunner)
    assert assets.run_operation(op_id) is True
    assert seen == ["alpha"]
    assert current_project() is None  # restored


def test_the_purge_worker_runs_as_its_jobs_project(monkeypatch):
    _project("alpha")
    cid = _conversation("alpha", "user:ann")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            job = Job(workspace_id=DEFAULT_WORKSPACE_ID, type=purge_mod.JOB_TYPE,
                      status="queued", payload={"conversation_id": cid})
            db.add(job)
            db.commit()
            job_id = job.id
    finally:
        db.close()
    seen: list[str | None] = []
    monkeypatch.setattr(purge_mod, "_execute_purge_job",
                        lambda *_a, **_k: seen.append(current_project()))
    purge_mod.execute_purge_job(job_id)
    assert seen == ["alpha"]


# ── permission re-checks use the project's keys ───────────────────────────────


def test_project_can_reads_the_project_role_not_the_account():
    _project("alpha")
    uid = _user("sam")
    _member_of("alpha", uid, "sme")
    db = SessionLocal()
    try:
        who = _Who(uid)
        assert who.can("agents.deploy")  # the account would allow it
        assert not project_can(db, who, DEFAULT_WORKSPACE_ID, "agents.deploy", "alpha")
        assert project_can(db, who, DEFAULT_WORKSPACE_ID, "project.use", "alpha")
        assert not project_can(db, who, DEFAULT_WORKSPACE_ID, "project.use", TEST_PROJECT)
        with project_context("alpha"):  # by default: the current project
            assert not project_can(db, who, DEFAULT_WORKSPACE_ID, "agents.deploy")
        assert project_can(db, who, DEFAULT_WORKSPACE_ID, "agents.deploy", None)
        assert project_can(db, _Who(None, "admin"), DEFAULT_WORKSPACE_ID, "agents.deploy",
                           "alpha")
    finally:
        db.close()


def test_the_worker_stops_an_approver_whose_project_role_lost_the_key():
    _project("alpha")
    uid = _user("tia")
    _member_of("alpha", uid, "developer")
    op_id = _operation("alpha", _conversation("alpha", f"user:{uid}"), name="x",
                       approver=uid)
    db = SessionLocal()
    try:
        with project_context(None):
            op = db.get(EvaluationAssetOperation, op_id)
            assert assets.approver_authorized(db, op) is None
            db.execute(text("UPDATE project_members SET role_key = 'sme' WHERE user_id = :u"),
                       {"u": uid})
            access.bump_epoch(db)
            db.commit()
            assert "no longer holds" in (assets.approver_authorized(db, op) or "")
    finally:
        db.close()


def test_the_claim_predicate_uses_the_project_role():
    _project("alpha")
    uid = _user("uma")
    _member_of("alpha", uid, "sme")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            predicate = assets._authorized_user(db, uid, DEFAULT_WORKSPACE_ID)
            assert db.scalar(select(predicate)) is False
        _member_of(TEST_PROJECT, uid, "developer")
        db.execute(text("UPDATE project_members SET role_key = 'developer' "
                        "WHERE user_id = :u AND project_id = 'alpha'"), {"u": uid})
        access.bump_epoch(db)
        db.commit()
        with project_context("alpha"):
            predicate = assets._authorized_user(db, uid, DEFAULT_WORKSPACE_ID)
            assert db.scalar(select(predicate)) is True
    finally:
        db.close()


# ── the catalog never offers another project's KB ─────────────────────────────


def test_the_catalog_drops_another_projects_knowledge_bases():
    _project("alpha")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            db.add(ResourceCreator(workspace_id=DEFAULT_WORKSPACE_ID, kind=creators.KB,
                                   resource_id="kb-alpha"))
            db.commit()
        catalog = {"knowledge_bases": [{"kb_id": "kb-alpha"}, {"kb_id": "kb-unowned"}]}
        with project_context("alpha"):
            kept = service.restrict_catalog(db, DEFAULT_WORKSPACE_ID, Viewer.ALL, catalog)
            assert [k["kb_id"] for k in kept["knowledge_bases"]] == ["kb-alpha"]
        with project_context(TEST_PROJECT):  # an unowned KB is the default project's
            kept = service.restrict_catalog(db, DEFAULT_WORKSPACE_ID, Viewer.ALL, catalog)
            assert [k["kb_id"] for k in kept["knowledge_bases"]] == ["kb-unowned"]
        with project_context(None):  # workers: unrestricted, as before
            assert service.restrict_catalog(db, DEFAULT_WORKSPACE_ID, Viewer.ALL,
                                            catalog) is catalog
    finally:
        db.close()


# ── removing a workspace or a user leaves nothing behind ──────────────────────


def test_deleting_a_workspace_drops_its_project_bindings():
    from app.routers.workspaces import delete_workspace

    db = SessionLocal()
    try:
        db.add(Workspace(id="w2", name="W2", account_id="999999999999", region="us-east-1",
                         bootstrap_status="registered", resources={}))
        db.commit()
    finally:
        db.close()
    _project("alpha", workspaces=("w2",))
    db = SessionLocal()
    try:
        before = access.current_epoch(db)
        assert db.scalar(text("SELECT COUNT(*) FROM project_workspaces "
                              "WHERE workspace_id = 'w2'")) >= 1
        delete_workspace("w2", db=db, _=None)
        assert db.scalar(text("SELECT COUNT(*) FROM project_workspaces "
                              "WHERE workspace_id = 'w2'")) == 0
        assert access.current_epoch(db) > before
    finally:
        db.close()


def test_deleting_a_user_drops_their_project_memberships():
    _project("alpha")
    uid = _user("vic", grant=True)
    _member_of("alpha", uid)
    db = SessionLocal()
    try:
        users_service.delete_user(db, db.get(User, uid))
        assert db.scalar(text("SELECT COUNT(*) FROM project_members WHERE user_id = :u"),
                         {"u": uid}) == 0
    finally:
        db.close()


# ── through the API ───────────────────────────────────────────────────────────


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


def test_assistant_status_reports_the_project_roles_deploy_key(app):
    _project("alpha")
    sme = _user("wes")
    _member_of("alpha", sme, "sme")
    dev = _user("xia")
    _member_of("alpha", dev, "developer")
    res = _login(app, "wes").get("/api/assistant/architect")
    assert res.status_code == 200, res.text
    assert res.json()["can_deploy"] is False
    assert res.json()["can_materialize_evaluation_assets"] is False
    res = _login(app, "xia").get("/api/assistant/architect")
    assert res.status_code == 200, res.text
    assert res.json()["can_deploy"] is True


def test_a_workshop_member_still_holds_the_deploy_key(app):
    _user("yan", grant=True)
    res = _login(app, "yan").get("/api/assistant/architect")
    assert res.status_code == 200, res.text
    assert res.json()["can_deploy"] is True
