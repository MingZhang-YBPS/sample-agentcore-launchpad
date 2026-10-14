"""Project administration API (projects design §7.10, P1F-proj): CRUD and slug rules,
the permissions matrix (admin / project owner / developer / non-member), the access
epoch, workspace bindings (unbind refused while rows remain), custom roles, archive
side effects, moving an agent between projects, and GET /api/me/access."""

import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.models.ledger import Agent, ApiKey, AuditEvent, ChatSession, ShareLink
from app.services import access
from app.services import share_links as share_links_service
from app.skill_lab import samples
from app.skill_lab import tasksets as taskset_svc
from app.skill_lab.models import SkillLabTaskset
from tests.test_project_scope import (  # noqa: F401
    ADMIN,
    PEER,
    _agent,
    _login,
    _member_of,
    _project,
    _user,
    app,
)

WS = DEFAULT_WORKSPACE_ID


def _admin(app):  # noqa: F811
    from fastapi.testclient import TestClient

    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


def _epoch() -> int:
    db = SessionLocal()
    try:
        return access.current_epoch(db)
    finally:
        db.close()


def _create(client, pid="alpha", **body):
    res = client.post("/api/projects", json={
        "id": pid, "name": pid.title(), "workspaces": [WS], **body,
    })
    assert res.status_code == 201, res.text
    return res.json()


def _project_of(table_model, row_id) -> str | None:
    db = SessionLocal()
    try:
        with project_context(None):
            return db.get(table_model, row_id).project_id
    finally:
        db.close()


@pytest.fixture
def world(app):  # noqa: F811
    """alpha owned by olga (project_owner), dev a developer, out in no project."""
    admin = _admin(app)
    olga, dev, out = (_user(n, grant=False) for n in ("olga", "dev", "out"))
    _create(admin, owners=[olga])
    _member_of("alpha", dev)
    return SimpleNamespace(admin=admin, olga=olga, dev=dev, out=out, app=app)


# ── CRUD ──────────────────────────────────────────────────────────────────────


def test_create_validates_and_journals(app):  # noqa: F811
    admin = _admin(app)
    assert admin.post("/api/projects", json={
        "id": "Bad_Id", "name": "x", "workspaces": [WS]}).json()["code"] == "project.invalid_id"
    created = _create(admin, description="risk")
    assert created["workspaces"][0]["workspace_id"] == WS
    assert not created["workspaces"][0]["is_workspace_default"]
    dup = admin.post("/api/projects", json={"id": "alpha", "name": "x", "workspaces": [WS]})
    assert dup.status_code == 409 and dup.json()["code"] == "project.exists"
    assert admin.post("/api/projects", json={"id": "default", "name": "x",
                                             "workspaces": [WS]}).status_code == 409
    missing = admin.post("/api/projects", json={"id": "beta", "name": "x",
                                                "workspaces": ["nowhere"]})
    assert missing.status_code == 404
    ids = [p["id"] for p in admin.get("/api/projects").json()["projects"]]
    assert ids[0] == "default" and "alpha" in ids
    patched = admin.patch("/api/projects/alpha", json={"name": "Alpha 2",
                                                       "settings": {"sod_strict": True}})
    assert patched.status_code == 200 and patched.json()["name"] == "Alpha 2"
    assert patched.json()["settings"] == {"sod_strict": True}
    db = SessionLocal()
    try:
        actions = set(db.scalars(select(AuditEvent.action)))
    finally:
        db.close()
    assert {"project.create", "project.workspaces.bind", "project.update"} <= actions


def test_the_default_project_is_locked(app):  # noqa: F811
    admin = _admin(app)
    res = admin.post("/api/projects/default/archive")
    assert res.status_code == 409 and res.json()["code"] == "project.default_locked"
    res = admin.put("/api/projects/default/workspaces", json={"unbind": [WS]})
    assert res.status_code == 409 and res.json()["code"] == "project.default_locked"


# ── who may ───────────────────────────────────────────────────────────────────


def test_permissions_matrix(world):
    olga, dev, out = (_login(world.app, n) for n in ("olga", "dev", "out"))
    # structure is the administrator's
    assert olga.post("/api/projects", json={"id": "mine", "name": "m",
                                            "workspaces": [WS]}).status_code == 403
    assert olga.post("/api/projects/alpha/archive").status_code == 403
    assert olga.put("/api/projects/alpha/workspaces", json={}).status_code == 403
    # a non-member sees nothing: 404, never 403
    for res in (out.get("/api/projects/alpha"), out.get("/api/projects/alpha/members"),
                out.put("/api/projects/alpha/members", json={}),
                out.get("/api/projects/alpha/candidates")):
        assert res.status_code == 404, res.text
        assert res.json()["code"] == "project.not_found"
    assert [p["id"] for p in out.get("/api/projects").json()["projects"]] == []
    # a developer reads but does not manage members
    assert dev.get("/api/projects/alpha/members").status_code == 200
    res = dev.put("/api/projects/alpha/members", json={"upsert": [
        {"user_id": world.out, "role_key": "viewer"}]})
    assert res.status_code == 403 and res.json()["code"] == "auth.permission_required"
    # the owner manages members, the description and owner settings — not structure
    res = olga.put("/api/projects/alpha/members", json={"upsert": [
        {"user_id": world.out, "role_key": "sme"}]})
    assert res.status_code == 200, res.text
    assert world.out in res.json()["added"]
    assert olga.patch("/api/projects/alpha", json={"description": "d"}).status_code == 200
    assert olga.patch("/api/projects/alpha", json={
        "settings": {"resource_visibility": "isolated"}}).status_code == 200
    assert olga.patch("/api/projects/alpha", json={"name": "x"}).status_code == 403
    assert olga.patch("/api/projects/alpha", json={
        "settings": {"sod_strict": False}}).status_code == 403
    # …and never their own membership (design D-22)
    res = olga.put("/api/projects/alpha/members", json={"upsert": [
        {"user_id": world.olga, "role_key": "release_ops"}]})
    assert res.status_code == 403 and res.json()["code"] == "project.self_membership"
    cands = olga.get("/api/projects/alpha/candidates?q=ou").json()["candidates"]
    assert [c["email"] for c in cands] == ["o***@acme-corp.com"]
    # inherit is the default project's compatibility role only
    res = olga.put("/api/projects/alpha/members", json={"upsert": [
        {"user_id": world.dev, "role_key": "inherit"}]})
    assert res.json()["code"] == "project.role_unknown"


def test_members_view_has_sources_and_effective_keys(world):
    world.admin.put("/api/projects/alpha/members", json={"upsert": [
        {"user_id": world.dev, "role_key": "developer",
         "overrides": {"agents.delete": False, "release.sign": True}}]})
    members = {m["user_id"]: m for m in
               world.admin.get("/api/projects/alpha/members").json()["members"]}
    dev = members[world.dev]
    assert dev["sources"] == [{"source": "direct", "role_key": "developer"}]
    assert "release.sign" in dev["permissions"] and "agents.delete" not in dev["permissions"]
    assert "project.members" in members[world.olga]["permissions"]
    # removing a member with no direct row is a 404
    assert world.admin.delete(f"/api/projects/alpha/members/{world.out}").status_code == 404


def test_a_membership_write_moves_the_epoch_and_reaches_the_next_request(world):
    out = _login(world.app, "out")
    assert out.get("/api/me/access").json()["workspaces"] == []
    before = _epoch()
    world.admin.put("/api/projects/alpha/members", json={"upsert": [
        {"user_id": world.out, "role_key": "viewer"}]})
    assert _epoch() > before
    view = out.get("/api/me/access").json()
    [ws] = view["workspaces"]
    assert ws["workspace_id"] == WS and ws["default_project_id"] == "alpha"
    assert [(p["id"], p["permissions"]) for p in ws["projects"]] == [("alpha", [])]
    before = _epoch()
    assert world.admin.delete(f"/api/projects/alpha/members/{world.out}").status_code == 204
    assert _epoch() > before
    assert out.get("/api/projects/alpha").status_code == 404


def test_admin_access_view_lists_every_bound_project(world):
    view = world.admin.get("/api/me/access").json()
    assert view["is_admin"]
    ws = next(w for w in view["workspaces"] if w["workspace_id"] == WS)
    assert ws["default_project_id"] == "default"
    assert {p["id"] for p in ws["projects"]} >= {"default", "alpha"}
    assert all(p["bypass"] for p in ws["projects"])


# ── roles ─────────────────────────────────────────────────────────────────────


def test_custom_roles(world):
    olga = _login(world.app, "olga")
    templates = olga.get("/api/role-templates").json()
    assert {t["key"] for t in templates["templates"]} >= {"project_owner", "viewer"}
    bad = olga.put("/api/projects/alpha/roles/outsourced", json={
        "name": "Outsourced", "permissions": ["agents.deploy", "not.a.key"]})
    assert bad.json()["code"] == "project.override_unknown"
    assert olga.put("/api/projects/alpha/roles/developer", json={
        "name": "x", "permissions": []}).json()["code"] == "project.role_invalid_key"
    res = olga.put("/api/projects/alpha/roles/outsourced", json={
        "name": "Outsourced", "permissions": ["project.use", "agents.deploy"],
        "based_on": "developer"})
    assert res.status_code == 200, res.text
    assert olga.put("/api/projects/alpha/members", json={"upsert": [
        {"user_id": world.dev, "role_key": "outsourced"}]}).status_code == 200
    dev = _login(world.app, "dev")
    assert dev.get("/api/me/access").json()["workspaces"][0]["projects"][0]["permissions"] == [
        "agents.deploy", "project.use"]
    res = olga.delete("/api/projects/alpha/roles/outsourced")
    assert res.status_code == 409 and res.json()["code"] == "project.role_in_use"
    olga.put("/api/projects/alpha/members", json={"upsert": [
        {"user_id": world.dev, "role_key": "developer"}]})
    assert olga.delete("/api/projects/alpha/roles/outsourced").status_code == 204
    assert dev.put("/api/projects/alpha/roles/x1", json={
        "name": "x", "permissions": []}).status_code == 403


# ── bindings ──────────────────────────────────────────────────────────────────


def test_unbind_is_refused_while_the_workspace_holds_project_rows(world):
    agent_id = _agent("alpha-agent", "alpha")
    res = world.admin.put("/api/projects/alpha/workspaces", json={"unbind": [WS]})
    assert res.status_code == 409 and res.json()["code"] == "project.workspace_in_use"
    assert res.json()["detail"]["rows"]["agents"] == 1
    db = SessionLocal()
    try:
        with project_context(None):
            db.delete(db.get(Agent, agent_id))
            db.commit()
    finally:
        db.close()
    before = _epoch()
    res = world.admin.put("/api/projects/alpha/workspaces", json={"unbind": [WS]})
    assert res.status_code == 200 and res.json()["workspaces"] == []
    assert _epoch() > before
    assert _login(world.app, "dev").get("/api/projects/alpha").status_code == 404


def test_set_workspace_default(world):
    res = world.admin.put("/api/projects/alpha/workspaces", json={"make_default": [WS]})
    assert res.status_code == 200
    assert res.json()["workspaces"][0]["is_workspace_default"]
    db = SessionLocal()
    try:
        defaults = db.execute(text("SELECT project_id FROM project_workspaces"
                                   " WHERE workspace_id = :ws AND is_workspace_default = 1"),
                              {"ws": WS}).scalars().all()
    finally:
        db.close()
    assert defaults == ["alpha"]
    # the default binding cannot be dropped until another project takes over
    res = world.admin.put("/api/projects/alpha/workspaces", json={"unbind": [WS]})
    assert res.json()["code"] == "project.workspace_default"


# ── archive ───────────────────────────────────────────────────────────────────


def test_archive_revokes_links_disables_keys_and_freezes_writes(world):
    agent_id = _agent("alpha-agent", "alpha")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            agent = db.get(Agent, agent_id)
            link, _ = share_links_service.create_link(
                db, workspace_id=WS, agent=agent, label="l", created_by="olga",
                expires_in_days=None)
            key = ApiKey(workspace_id=WS, name="k", prefix="lp_x", key_hash="h" * 64,
                         created_by=world.olga)
            db.add(key)
            db.commit()
            link_id, key_id = link.id, key.id
    finally:
        db.close()
    res = world.admin.post("/api/projects/alpha/archive")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "archived"
    assert res.json()["links_revoked"] == 1 and res.json()["keys_disabled"] == 1
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.get(ShareLink, link_id).revoked_at is not None
            assert db.get(ApiKey, key_id).enabled is False
    finally:
        db.close()
    olga = _login(world.app, "olga")
    res = olga.put("/api/projects/alpha/members", json={"upsert": []})
    assert res.status_code == 409 and res.json()["code"] == "project.archived"
    assert olga.get("/api/projects/alpha/members").status_code == 200  # reads stay
    assert world.admin.post("/api/projects/alpha/unarchive").json()["status"] == "active"


# ── transfer ──────────────────────────────────────────────────────────────────


def test_transfer_moves_the_agent_and_its_derived_rows(world):
    agent_id = _agent("mover", "default")
    db = SessionLocal()
    try:
        with project_context("default"):
            session = ChatSession(workspace_id=WS, agent_id=agent_id, session_id="s" * 40)
            key = ApiKey(workspace_id=WS, name="k", prefix="lp_y", key_hash="k" * 64,
                         agent_ids=[agent_id])
            db.add_all([session, key])
            db.commit()
            session_id, key_id = session.id, key.id
    finally:
        db.close()
    res = world.admin.post("/api/projects/default/transfer", json={
        "kind": "agent", "resource_id": agent_id, "to_project_id": "alpha"})
    assert res.status_code == 200, res.text
    assert res.json()["rows"]["agents"] == 1 and res.json()["rows"]["chat_sessions"] == 1
    assert _project_of(Agent, agent_id) == "alpha"
    assert _project_of(ChatSession, session_id) == "alpha"
    db = SessionLocal()
    try:
        with project_context(None):
            key = db.get(ApiKey, key_id)
            assert key.agent_ids == [] and key.enabled is False  # never "every agent"
    finally:
        db.close()
    # the agent is now alpha's: moving it "from default" again is a 404
    again = world.admin.post("/api/projects/default/transfer", json={
        "resource_id": agent_id, "to_project_id": "alpha"})
    assert again.status_code == 404
    _project("beta", workspaces=())
    res = world.admin.post("/api/projects/alpha/transfer", json={
        "resource_id": agent_id, "to_project_id": "beta"})
    assert res.status_code == 409 and res.json()["code"] == "project.not_bound"
    assert world.admin.post("/api/projects/alpha/transfer", json={
        "kind": "dataset", "resource_id": "x", "to_project_id": "beta",
    }).json()["code"] == "project.transfer_kind"
    assert _login(world.app, "olga").post("/api/projects/alpha/transfer", json={
        "resource_id": agent_id, "to_project_id": "default"}).status_code == 403


# ── Skill Lab samples on bind ─────────────────────────────────────────────────


def test_a_newly_bound_project_gets_samples_where_the_workspace_has_them(
    app, tmp_path, monkeypatch,  # noqa: F811
):
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        taskset_svc, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )
    admin = _admin(app)
    _create(admin, "early")  # no samples in the workspace yet: nothing to catch up on
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.scalars(select(SkillLabTaskset)).first() is None
        with project_context("default"):
            samples._seed_project_tasksets(db, WS, "default", lambda _m: None)
    finally:
        db.close()
    _create(admin, "late")
    db = SessionLocal()
    try:
        with project_context("late"):
            names = set(db.scalars(select(SkillLabTaskset.name)))
    finally:
        db.close()
    assert names == {s.name for s in samples.SAMPLE_TASKSETS}
