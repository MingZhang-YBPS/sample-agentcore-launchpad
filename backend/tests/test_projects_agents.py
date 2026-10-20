"""Projects phase 1 (design §4.3, §5.4, §5.5) for the agents surfaces: live names stay
unique across projects without naming another project's holder, discovery never offers
(or re-imports) a resource another project manages and offers unmanaged ones to
importers only, spec pins and tool Connections stay inside the project, and jobs,
uploads and the preset name check answer like a missing id across projects."""

from unittest.mock import MagicMock

import pytest

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Agent, IdentityProvider, Job, ResourceCreator
from app.schemas.agent import AgentSpec
from app.services import agent_access, agent_names, creators
from app.services.visibility import Viewer
from app.services.workspace import context_for_workspace
from app.system_agents import service as system_agents
from tests.conftest import TEST_PROJECT
from tests.test_project_scope import (
    _agent,
    _login,
    _member_of,
    _project,
    _user,
    app,  # noqa: F401 — the fixture
)
from tests.test_runtime_discovery import _detail, _mock_control

ALPHA = {"X-Project": "alpha"}
DEFAULT = {"X-Project": TEST_PROJECT}


def _runtime_agent(name: str, pid: str, detail: dict, *, method: str = "zip_runtime") -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method,
                        status="active", spec={"name": name}, arn=detail["agentRuntimeArn"],
                        resource_id=detail["agentRuntimeId"])
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _rows_named(name: str) -> list[Agent]:
    db = SessionLocal()
    try:
        with project_context(None):
            return db.query(Agent).filter(Agent.name.like(f"{name}%")).all()
    finally:
        db.close()


# ── names are workspace-wide (§4.3) ───────────────────────────────────────────


def test_a_name_held_in_another_project_is_taken_but_unnamed():
    _project("alpha")
    holder = _agent("shared-name", "alpha")
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            found = agent_names.live_holder(db, DEFAULT_WORKSPACE_ID, "shared-name")
            assert found is not None and found.id == holder
            assert agent_names.holder_id_for(db, Viewer.ALL, found) is None
            with pytest.raises(AppError) as exc:
                agent_names.claim_agent_name(db, DEFAULT_WORKSPACE_ID, "shared-name", "x" * 32)
            assert exc.value.code == "agent.name_exists"
            assert exc.value.detail == {"agent_id": None}
        with project_context("alpha"):
            with pytest.raises(AppError) as exc:
                agent_names.claim_agent_name(db, DEFAULT_WORKSPACE_ID, "shared-name", "x" * 32)
            assert exc.value.detail == {"agent_id": holder}
    finally:
        db.rollback()
        db.close()


def test_create_refuses_another_projects_name_without_naming_it(client):
    _project("alpha")
    _agent("taken-agent", "alpha")
    res = client.post("/api/agents", headers=DEFAULT, json={
        "name": "taken-agent", "method": "harness", "system_prompt": "p",
    })
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "agent.name_exists"
    assert res.json()["detail"] == {"agent_id": None}


# ── discovery (§5.5) ──────────────────────────────────────────────────────────


def test_a_runtime_managed_by_another_project_is_managed_and_unnamed(client, monkeypatch):
    _project("alpha")
    detail = _detail("alpha-rt-abcdefghij", name="alpha-rt")
    owner = _runtime_agent("alpha-owner", "alpha", detail)
    _mock_control(monkeypatch, [detail])

    mine = client.get("/api/agents/discovery", headers=ALPHA).json()["runtimes"]
    assert mine[0]["managed_agent_id"] == owner

    theirs = client.get("/api/agents/discovery", headers=DEFAULT).json()["runtimes"]
    assert len(theirs) == 1
    row = theirs[0]
    assert row["managed"] is True and row["importable"] is False
    assert row["reason_code"] == "already-managed"
    assert row["managed_agent_id"] is None and row["managed_agent_name"] is None
    assert row["managed_agent_method"] is None

    res = client.post("/api/agents/discovery/import", headers=DEFAULT,
                      json={"runtime_ids": [detail["agentRuntimeId"]]})
    assert res.status_code == 200, res.text
    assert res.json()["imported"] == []
    assert res.json()["already_managed"] == [
        {"runtime_id": detail["agentRuntimeId"], "agent_id": None, "agent_name": None,
         "managed": True},
    ]
    assert [a.id for a in _rows_named("alpha-")] == [owner]


def test_another_projects_import_is_never_duplicated(client, monkeypatch):
    _project("alpha")
    detail = _detail("ext-rt-abcdefghij", name="ext-rt")
    _mock_control(monkeypatch, [detail])
    first = client.post("/api/agents/discovery/import", headers=ALPHA,
                        json={"runtime_ids": [detail["agentRuntimeId"]]})
    assert len(first.json()["imported"]) == 1
    again = client.post("/api/agents/discovery/import", headers=DEFAULT,
                        json={"runtime_ids": [detail["agentRuntimeId"]]})
    assert again.json()["imported"] == [] and again.json()["updated"] == []
    assert again.json()["already_managed"][0]["agent_id"] is None
    rows = _rows_named("ext-rt")
    assert len(rows) == 1 and rows[0].project_id == "alpha"


def test_an_import_never_takes_a_name_live_in_another_project(client, monkeypatch):
    _project("alpha")
    _agent("clash", "alpha")
    detail = _detail("clash-abcdefghij", name="clash")
    _mock_control(monkeypatch, [detail])
    res = client.post("/api/agents/discovery/import", headers=DEFAULT,
                      json={"runtime_ids": [detail["agentRuntimeId"]]})
    imported = res.json()["imported"]
    assert len(imported) == 1 and imported[0]["agent_name"] == "clash-abcdefghij"


def test_unmanaged_runtimes_are_offered_to_importers_only(app, monkeypatch):  # noqa: F811
    _project("alpha")
    managed = _detail("mine-abcdefghij", name="mine")
    loose = _detail("loose-abcdefghij", name="loose")
    owner = _runtime_agent("mine-agent", "alpha", managed)
    _mock_control(monkeypatch, [managed, loose])
    viewer_id = _user("vera", grant=False)
    _member_of("alpha", viewer_id, "viewer")
    dev_id = _user("dana", grant=False)
    _member_of("alpha", dev_id, "developer")

    seen = _login(app, "vera").get("/api/agents/discovery")
    assert seen.status_code == 200, seen.text
    assert [r["managed_agent_id"] for r in seen.json()["runtimes"]] == [owner]

    importer = _login(app, "dana").get("/api/agents/discovery").json()["runtimes"]
    assert {r["name"] for r in importer} == {"mine", "loose"}


# ── spec pins and Connections (§5.4) ──────────────────────────────────────────


def _kb_owner(kb_id: str, pid: str) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(ResourceCreator(workspace_id=DEFAULT_WORKSPACE_ID, kind=creators.KB,
                                   resource_id=kb_id))
            db.commit()
    finally:
        db.close()


def _pin(kb_id: str) -> dict:
    return {"knowledge_bases": [{"kb_id": kb_id}]}


def test_a_kb_of_another_project_cannot_be_pinned():
    _project("alpha")
    _kb_owner("KBALPHA", "alpha")
    ctx = context_for_workspace(DEFAULT_WORKSPACE_ID)
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            with pytest.raises(NotFoundError) as exc:
                agent_access.check_pins(db, Viewer.ALL, ctx, _pin("KBALPHA"))
            assert exc.value.code == "kb.not_found"
            # an unowned KB belongs to the workspace default project
            agent_access.check_pins(db, Viewer.ALL, ctx, _pin("KBLOOSE"))
            # a re-publish only checks what it adds
            agent_access.check_pins(db, Viewer.ALL, ctx, _pin("KBALPHA"),
                                    previous_spec=_pin("KBALPHA"))
        with project_context("alpha"):
            agent_access.check_pins(db, Viewer.ALL, ctx, _pin("KBALPHA"))
            with pytest.raises(NotFoundError):
                agent_access.check_pins(db, Viewer.ALL, ctx, _pin("KBLOOSE"))
    finally:
        db.close()


def test_a_single_project_workspace_pins_any_kb():
    ctx = context_for_workspace(DEFAULT_WORKSPACE_ID)
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            agent_access.check_pins(db, Viewer.ALL, ctx, _pin("KBLOOSE"))
    finally:
        db.close()


def test_a_connection_of_another_project_reads_as_unknown():
    _project("alpha")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            db.add(IdentityProvider(workspace_id=DEFAULT_WORKSPACE_ID, name="gh",
                                    kind="oauth2"))
            db.commit()
    finally:
        db.close()
    spec = AgentSpec(name="tooluser", method="zip_runtime", system_prompt="p",
                     tools=[{"type": "rest", "name": "repo",
                             "config": {"url": "https://repo.example"},
                             "auth": {"connection": "gh", "kind": "oauth2",
                                      "scopes": ["repo"]}}])
    ws = MagicMock(id=DEFAULT_WORKSPACE_ID)
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            with pytest.raises(AppError) as exc:
                agents_router._require_project_connections(db, spec, ws)
            assert exc.value.code == "identity.connection_unknown"
        with project_context("alpha"):
            agents_router._require_project_connections(db, spec, ws)
    finally:
        db.close()


# ── ProjectAgents, jobs, uploads ──────────────────────────────────────────────


def test_project_agents_is_an_allow_set_that_fails_closed():
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            single = agent_access.ProjectAgents(db, DEFAULT_WORKSPACE_ID)
            assert not single and single.allows("unknown") and single.allows_actor("default")
    finally:
        db.close()
    _project("alpha")
    a = _agent("pa-alpha", "alpha")
    d = _agent("pa-default", TEST_PROJECT)
    preset = _agent("pa-preset", None, system_key="aws-agent-solution-architect")
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            agents = agent_access.ProjectAgents(db, DEFAULT_WORKSPACE_ID)
            assert agents
            assert agents.allows(d) and agents.allows(preset) and not agents.allows(a)
            assert not agents.allows("unknown")  # unattributed, several projects
            assert agents.allows_actor(f"{d}__someone") and not agents.allows_actor(f"{a}__x")
            assert not agents.allows_actor("default")
        with project_context(None):
            assert agent_access.ProjectAgents(db, DEFAULT_WORKSPACE_ID).allows(a)
    finally:
        db.close()


def test_a_job_of_another_projects_agent_reads_as_missing(client):
    _project("alpha")
    alpha_agent = _agent("job-agent", "alpha")
    db = SessionLocal()
    try:
        # unscoped in a two-project workspace: a platform-level (NULL) job row
        job = Job(workspace_id=DEFAULT_WORKSPACE_ID, type="deploy_agent",
                  payload={"agent_id": alpha_agent})
        db.add(job)
        db.commit()
        assert job.project_id is None
        job_id = job.id
    finally:
        db.close()
    assert client.get(f"/api/jobs/{job_id}", headers=ALPHA).status_code == 200
    res = client.get(f"/api/jobs/{job_id}", headers=DEFAULT)
    assert res.status_code == 404 and res.json()["code"] == "job.not_found"


def test_an_upload_staged_in_another_project_reads_as_missing(client, monkeypatch):
    _project("alpha")
    manifest = {"upload_id": "u1", "project_id": "alpha"}
    monkeypatch.setattr(agents_router.byoc_uploads, "get_manifest",
                        lambda _ctx, _uid: dict(manifest))
    assert client.get("/api/agents/uploads/u1", headers=ALPHA).status_code == 200
    res = client.get("/api/agents/uploads/u1", headers=DEFAULT)
    assert res.status_code == 404 and res.json()["code"] == "byoc.upload_not_found"
    manifest.pop("project_id")  # staged before projects: readable as before
    assert client.get("/api/agents/uploads/u1", headers=DEFAULT).status_code == 200


# ── the preset's reserved name ────────────────────────────────────────────────


def test_the_preset_squatter_is_named_only_inside_its_project():
    _project("alpha")
    squatter = _agent("squatter", "alpha")
    db = SessionLocal()
    try:
        holder = db.get(Agent, squatter)
        named = {"agent_id": squatter, "agent_name": "squatter", "method": "zip_runtime"}
        hidden = {"agent_id": None, "agent_name": None, "method": None}
        # the status read is project-exempt: no project current, several bound
        assert system_agents._holder_out(db, DEFAULT_WORKSPACE_ID, holder,
                                         is_admin=False) == hidden
        assert system_agents._holder_out(db, DEFAULT_WORKSPACE_ID, holder,
                                         is_admin=True) == named
        with project_context("alpha"):
            assert system_agents._holder_out(db, DEFAULT_WORKSPACE_ID, holder,
                                             is_admin=False) == named
        with project_context(TEST_PROJECT):
            assert system_agents._holder_out(db, DEFAULT_WORKSPACE_ID, holder,
                                             is_admin=False) == hidden
    finally:
        db.close()
