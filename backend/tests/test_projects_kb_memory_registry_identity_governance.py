"""Phase-1 project boundary (design §5.5) for knowledge bases, memory resources,
the registry, identity Connections, governance and the tools demos: AWS-listed
resources of another project read as missing, integrity guards count every
project and name only the caller's, and workspace-level rows stay NULL."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.main import create_app
from app.models.ledger import (
    Agent,
    ChatSession,
    IdentityProvider,
    ManagedMemory,
    OauthPendingSession,
    PolicyChange,
    PolicyDecision,
    ResourceCreator,
    UserGrant,
    UserTokenRevocation,
)
from app.models.projects import Project, ProjectMember, ProjectWorkspace
from app.routers import knowledge as kb_router
from app.routers import memory_resources as memory_router
from app.routers import registry as registry_router
from app.routers import tools as tools_router
from app.services import access, creators, knowledge, memory_ownership, oauth_sessions
from app.services import identity_gateway_targets as gateway_targets
from app.services import identity_providers as ip
from app.services import users as users_service
from app.services.creators import Creator
from app.services.visibility import Viewer
from tests.conftest import TEST_PROJECT

WS = SimpleNamespace(id=DEFAULT_WORKSPACE_ID, resources={})
ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)


# ── helpers (as tests/test_project_scope.py) ──────────────────────────────────


def _project(pid: str) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            if db.get(Project, pid) is None:
                db.add(Project(id=pid, name=pid.upper(), status="active"))
            if db.get(ProjectWorkspace, (pid, DEFAULT_WORKSPACE_ID)) is None:
                db.add(ProjectWorkspace(project_id=pid, workspace_id=DEFAULT_WORKSPACE_ID,
                                        is_workspace_default=False))
            access.bump_epoch(db)
            db.commit()
    finally:
        db.close()


def _agent(name: str, pid: str | None, spec: dict | None = None, **fields) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime",
                        status="active", spec={"name": name, **(spec or {})}, **fields)
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _kb_owner(kb_id: str, pid: str) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            creators.record(db, DEFAULT_WORKSPACE_ID, creators.KB, kb_id, Creator("u1", "ada"))
    finally:
        db.close()


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


# ── knowledge bases ───────────────────────────────────────────────────────────


def test_kb_ownership_rows_follow_the_project_and_unowned_kbs_the_default(db):
    _project("alpha")
    _kb_owner("KB-A", "alpha")
    _kb_owner("KB-D", TEST_PROJECT)
    items = [{"kb_id": k} for k in ("KB-A", "KB-D", "KB-LEGACY")]
    with project_context("alpha"):
        seen = [i["kb_id"] for i in kb_router.visible_kbs(db, DEFAULT_WORKSPACE_ID,
                                                          Viewer.ALL, items)]
        assert seen == ["KB-A"]  # an unowned KB is the workspace default project's
        with pytest.raises(NotFoundError):
            kb_router._require_visible(db, WS, Viewer.ALL, "KB-D")
        kb_router._require_visible(db, WS, Viewer.ALL, "KB-A")
    with project_context(TEST_PROJECT):
        seen = [i["kb_id"] for i in kb_router.visible_kbs(db, DEFAULT_WORKSPACE_ID,
                                                          Viewer.ALL, items)]
        assert seen == ["KB-D", "KB-LEGACY"]


def test_a_single_project_workspace_sees_every_kb(db):
    _kb_owner("KB-D", TEST_PROJECT)
    items = [{"kb_id": "KB-D"}, {"kb_id": "KB-LEGACY"}]
    with project_context(TEST_PROJECT):
        assert kb_router.visible_kbs(db, DEFAULT_WORKSPACE_ID, Viewer.ALL, items) == items


def test_record_is_idempotent_across_projects_and_files_ownerless_rows(db):
    _project("alpha")
    _kb_owner("KB-A", "alpha")
    with project_context(TEST_PROJECT):  # a retry from another project leaves alpha's row
        creators.record(db, DEFAULT_WORKSPACE_ID, creators.KB, "KB-A", Creator("u2", "bo"))
        creators.record(db, DEFAULT_WORKSPACE_ID, creators.KB, "KB-X", None)
    with project_context(None):
        rows = {r.resource_id: r for r in db.scalars(select(ResourceCreator))}
    assert rows["KB-A"].project_id == "alpha" and rows["KB-A"].created_by == "u1"
    # no principal, several projects: an ownership-only row, never the default by omission
    assert rows["KB-X"].project_id == TEST_PROJECT and rows["KB-X"].created_by is None


def test_the_kb_delete_guard_counts_every_project_and_masks_names(db):
    _project("alpha")
    _agent("alpha-agent", "alpha", {"knowledge_bases": [{"kb_id": "KB-D"}]})
    _agent("default-agent", TEST_PROJECT, {"knowledge_bases": [{"kb_id": "KB-D"}]})
    with project_context(TEST_PROJECT):
        assert sorted(knowledge.attached_agents(WS, "KB-D")) == ["alpha-agent", "default-agent"]
        hidden = kb_router._hidden_names(db, Viewer.ALL, DEFAULT_WORKSPACE_ID)
        item = kb_router._mask_attached(
            {"attached_agents": knowledge.attached_agents(WS, "KB-D")}, hidden)
    assert item == {"attached_agents": ["default-agent"], "attached_agent_count": 2}


# ── memory resources ──────────────────────────────────────────────────────────


def _managed(memory_id: str, pid: str) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            memory_ownership.register(db, WS, memory_id, origin="created")
    finally:
        db.close()


def test_another_projects_memory_is_foreign_and_cannot_be_adopted(db):
    _project("alpha")
    _managed("mem-alpha", "alpha")
    with project_context(TEST_PROJECT):
        assert memory_ownership.foreign_ids(db, WS) == {"mem-alpha"}
        assert not memory_ownership.is_managed(db, WS, "mem-alpha")
        with pytest.raises(NotFoundError) as err:
            memory_ownership.register(db, WS, "mem-alpha", origin="adopted")
        assert err.value.code == "memory.not_managed"
    with project_context("alpha"):
        assert memory_ownership.foreign_ids(db, WS) == set()
        assert memory_ownership.is_managed(db, WS, "mem-alpha")
    with project_context(None):
        assert db.query(ManagedMemory).count() == 1


def test_the_memory_delete_guard_counts_every_project(db):
    _project("alpha")
    _agent("alpha-agent", "alpha", {"memory": {"memory_id": "mem-d"}})
    mine = _agent("default-agent", TEST_PROJECT, {"memory": {"memory_id": "mem-d"}})
    with project_context(TEST_PROJECT):
        listed = memory_router._agents_by_memory(db, WS)["mem-d"]
        counted = memory_router._agents_by_memory(db, WS, every_project=True)["mem-d"]
    assert [a["id"] for a in listed] == [mine]
    assert len(counted) == 2


def test_the_memory_delete_409_names_only_the_callers_agents(db, monkeypatch):
    _project("alpha")
    _agent("alpha-agent", "alpha", {"memory": {"memory_id": "mem-d"}})
    _agent("default-agent", TEST_PROJECT, {"memory": {"memory_id": "mem-d"}})
    _managed("mem-d", TEST_PROJECT)
    monkeypatch.setattr(memory_router, "_guard", MagicMock(side_effect=AssertionError))
    ws = SimpleNamespace(id=DEFAULT_WORKSPACE_ID, context=WS)
    with project_context(TEST_PROJECT), pytest.raises(AppError) as err:
        memory_router.delete_resource("mem-d", db=db, ws=ws, viewer=Viewer.ALL)
    assert err.value.code == "memory.in_use" and "2 agent(s)" in err.value.message
    assert [a["name"] for a in err.value.detail["agents"]] == ["default-agent"]


# ── registry ──────────────────────────────────────────────────────────────────


def test_a2a_records_of_another_projects_agents_are_hidden():
    _project("alpha")
    _agent("alpha-agent", "alpha", registry_record_id="rec-alpha")
    _agent("default-agent", TEST_PROJECT, registry_record_id="rec-default")
    _agent("preset", None, system_key="aws-agent-solution-architect",
           registry_record_id="rec-preset")
    records = [{"recordId": r} for r in ("rec-alpha", "rec-default", "rec-preset", "rec-mcp")]
    with project_context(TEST_PROJECT):
        kept = registry_router._visible_records(WS, Viewer.ALL, records)
        assert [r["recordId"] for r in kept] == ["rec-default", "rec-preset", "rec-mcp"]
        with pytest.raises(NotFoundError):
            registry_router._require_record_visible(WS, Viewer.ALL, "rec-alpha")
    with project_context(None):  # workers see every record
        assert registry_router._visible_records(WS, Viewer.ALL, records) == records


def test_skill_staging_is_bound_to_the_inspecting_project():
    registry_router._staging["stg"] = {"bundles": [], "expires": 9e18,
                                       "workspace_id": DEFAULT_WORKSPACE_ID,
                                       "project_id": "alpha"}
    try:
        with project_context(TEST_PROJECT):
            assert registry_router._staged_for("stg", WS) is None
        with project_context("alpha"):
            assert registry_router._staged_for("stg", WS) is not None
    finally:
        registry_router._staging.pop("stg", None)


# ── identity Connections ──────────────────────────────────────────────────────


def _control(oauth=()):
    control = MagicMock()
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": n} for n in oauth]}
    control.list_api_key_credential_providers.return_value = {"credentialProviders": []}
    return control


def _connection(name: str, pid: str) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(IdentityProvider(workspace_id=DEFAULT_WORKSPACE_ID, name=name, kind="oauth2"))
            db.commit()
    finally:
        db.close()


def test_another_projects_connection_is_not_listed_or_reachable(db):
    _project("alpha")
    _connection("alpha-idp", "alpha")
    _connection("default-idp", TEST_PROJECT)
    control = _control(oauth=("alpha-idp", "default-idp", "hand-made"))
    with project_context(TEST_PROJECT):
        listed = {i["name"]: i["source"] for i in ip.list_connections(control, db, WS.id)}
        assert listed == {"default-idp": "launchpad", "hand-made": "external"}
        with pytest.raises(NotFoundError):
            ip.get_connection(control, db, WS.id, "oauth2", "alpha-idp")
        with pytest.raises(NotFoundError):  # not 409 external: nothing is disclosed
            ip.delete_connection(control, db, WS.id, "oauth2", "alpha-idp")
        assert ip.foreign_names(db, WS.id, "oauth2") == {"alpha-idp"}
    control.get_oauth2_credential_provider.assert_not_called()


def test_a_stale_row_of_another_project_frees_its_name(db):
    _project("alpha")
    _connection("team-idp", "alpha")  # deleted out-of-band: the vault no longer has it
    with project_context(TEST_PROJECT):
        ip._refuse_duplicate(_control(), db, WS.id, "oauth2", "team-idp")
    with project_context(None):
        assert db.query(IdentityProvider).count() == 0


def test_the_connection_delete_guard_counts_every_project(db):
    _project("alpha")
    _connection("team-idp", TEST_PROJECT)
    tool = {"name": "t", "auth": {"connection": "team-idp"}}
    _agent("alpha-agent", "alpha", {"tools": [tool]})
    _agent("default-agent", TEST_PROJECT, {"tools": [tool]})
    with project_context(TEST_PROJECT), pytest.raises(AppError) as err:
        ip.delete_connection(_control(oauth=("team-idp",)), db, WS.id, "oauth2", "team-idp")
    assert err.value.code == "identity.connection_referenced"
    assert "2 agent(s)" in err.value.message
    assert [r["name"] for r in err.value.detail["referenced_by"]] == ["default-agent"]


def test_a_gateway_target_cannot_bind_another_projects_connection():
    _project("alpha")
    _connection("alpha-idp", "alpha")
    control = MagicMock()
    with project_context(TEST_PROJECT), pytest.raises(NotFoundError) as err:
        gateway_targets.create_target(
            control, {"gateway_id": "gw"}, name="t", description=None, source="mcp",
            openapi_schema=None, mcp_endpoint="https://x", connection="alpha-idp",
            kind="oauth2", mode="m2m", scopes=[], workspace_id=DEFAULT_WORKSPACE_ID,
        )
    assert err.value.code == "identity.connection_not_found"
    control.create_gateway_target.assert_not_called()


def test_consent_completes_and_revocations_reach_across_projects(db):
    _project("alpha")
    agent_id = _agent("alpha-agent", "alpha")
    with project_context("alpha"):
        oauth_sessions.record_pending(db, DEFAULT_WORKSPACE_ID, session_uri="urn:s1",
                                      provider="team-idp", user_id="ann", agent_id=agent_id)
    with project_context(TEST_PROJECT):  # the browser switched project meanwhile
        done = oauth_sessions.complete(db, DEFAULT_WORKSPACE_ID, "urn:s1", caller="ann",
                                       data_client=MagicMock())
        assert done["completed"] is True
        oauth_sessions.revoke(db, DEFAULT_WORKSPACE_ID, user_id="ann", provider="team-idp")
    with project_context("alpha"):  # a second revoke updates the one workspace row
        oauth_sessions.revoke(db, DEFAULT_WORKSPACE_ID, user_id="ann", provider="team-idp")
    with project_context(None):
        grant = db.scalar(select(UserGrant))
        assert (grant.project_id, grant.status) == ("alpha", "revoked")
        revocations = db.scalars(select(UserTokenRevocation)).all()
        assert [r.project_id for r in revocations] == [None]
        assert db.query(OauthPendingSession).count() == 0


def test_deleting_a_connection_forgets_every_projects_grants(db):
    _project("alpha")
    agent_id = _agent("alpha-agent", "alpha")
    with project_context("alpha"):
        db.add(UserGrant(workspace_id=DEFAULT_WORKSPACE_ID, user_id="ann",
                         provider="team-idp", agent_id=agent_id))
        db.commit()
    with project_context(TEST_PROJECT):
        assert oauth_sessions.forget_connection(db, DEFAULT_WORKSPACE_ID, "team-idp") == 1
        db.commit()


# ── governance ────────────────────────────────────────────────────────────────


def test_governance_rows_stay_workspace_level(db):
    _project("alpha")
    with project_context("alpha"):
        db.add(PolicyDecision(workspace_id=DEFAULT_WORKSPACE_ID, principal="p", tool="t",
                              outcome="ALLOW"))
        db.add(PolicyChange(workspace_id=DEFAULT_WORKSPACE_ID, gateway_id="gw",
                            gateway_arn="arn", gateway_name="gw", operation="create",
                            operator="ann"))
        db.commit()
    with project_context(TEST_PROJECT):  # the other project reads the same log and lock
        assert db.query(PolicyDecision).one().project_id is None
        assert db.query(PolicyChange).filter_by(status="pending").one().project_id is None


# ── through the API: traces and the browser demo ──────────────────────────────


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _member(app, username: str, pid: str) -> TestClient:
    db = SessionLocal()
    try:
        user, _ = users_service.create_user(
            db, username=username, email=f"{username}@acme-corp.com", role="member",
            password="sufficient-pass", workspaces=[], created_by="test",
        )
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        db.add(ProjectMember(project_id=pid, user_id=user.id, role_key="developer"))
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()
    client = TestClient(app, client=PEER)
    res = client.post("/api/auth/login",
                      json={"username": username, "password": "sufficient-pass"})
    assert res.status_code == 200, res.text
    return client


def test_a_trace_is_readable_only_through_a_session_of_the_project(app, monkeypatch):
    from app.routers import governance as governance_router

    monkeypatch.setattr(governance_router.trace_service, "session_trace",
                        lambda sid, ctx, lookback_hours: {"span_count": 0, "spans": []})
    _project("alpha")
    alpha_agent = _agent("alpha-agent", "alpha")
    default_agent = _agent("default-agent", TEST_PROJECT)
    db = SessionLocal()
    try:
        for agent_id, sid in ((alpha_agent, "a" * 40), (default_agent, "d" * 40)):
            db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                               session_id=sid))
        db.commit()
    finally:
        db.close()
    jo = _member(app, "joan", "alpha")
    assert jo.get(f"/api/traces/{'a' * 40}").status_code == 200
    res = jo.get(f"/api/traces/{'d' * 40}")
    assert res.status_code == 404 and res.json()["code"] == "trace.not_found"


def test_a_browser_demo_session_stops_only_from_its_project():
    client = MagicMock()
    tools_router._browser_demo_sessions["s1"] = tools_router._BrowserDemoSession(
        client=client, browser_identifier="aws.browser.v1", profile_identifier="p",
        save_profile=True, workspace_id=DEFAULT_WORKSPACE_ID, project_id="alpha",
    )
    try:
        other = tools_router._stop_browser_demo_session("s1", DEFAULT_WORKSPACE_ID, TEST_PROJECT)
        assert other == {"stopped": False, "profile_saved": None}
        client.stop.assert_not_called()
        mine = tools_router._stop_browser_demo_session("s1", DEFAULT_WORKSPACE_ID, "alpha")
        assert mine["stopped"] is True
    finally:
        tools_router._browser_demo_sessions.pop("s1", None)
