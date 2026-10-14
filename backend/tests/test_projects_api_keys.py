"""Projects phase 1 (design §5.7): an API key belongs to the project it was minted in.

The key carries `project_id` from the console's current project; the key routes
only ever see the current project's keys; every /v1 call runs as the key's project
(handlers and the streaming generator alike), so another project's agent reads as
missing; and a key whose creator is no longer a member of its project — or whose
project is archived or unbound — stops working (401, disabled, not re-enableable).
A single-project workspace behaves as before (`test_api_key_lifecycle.py`).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.routers.public_api as public_api
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project, project_context
from app.models.ledger import Agent, ApiKey
from app.models.projects import ProjectMember
from app.services import access, visibility
from app.services import api_keys as api_keys_service
from app.services.visibility import Viewer
from tests.test_project_scope import (  # noqa: F401
    ADMIN,
    PEER,
    _login,
    _member_of,
    _project,
    _user,
    app,
)

WS = DEFAULT_WORKSPACE_ID
ALPHA = {"X-Project": "alpha"}


@pytest.fixture(autouse=True)
def stub_invoke(monkeypatch):
    """Record the project each invoke path runs as."""
    api_keys_service.reset_rate_limits()
    seen: list[tuple[str, str | None]] = []

    def invoke(agent, prompt, **_kw):
        seen.append(("invoke", current_project()))
        return {"text": f"hi from {agent.name}", "session_id": "s" * 40}

    def stream(agent, prompt, **_kw):
        seen.append(("stream", current_project()))
        yield {"event": "done", "data": {"text": "ok"}}

    monkeypatch.setattr(public_api, "invoke_agent_text", invoke)
    monkeypatch.setattr(public_api, "chat_stream", stream)
    yield seen
    api_keys_service.reset_rate_limits()


def _agent(name: str, pid: str) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(
                workspace_id=WS, name=name, method="harness", status="active",
                arn=f"arn:aws:bedrock-agentcore:us-west-2:111:harness/{name}",
                spec={"name": name, "method": "harness", "system_prompt": "x"},
            )
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _admin(app) -> TestClient:  # noqa: F811
    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


def _mint(client: TestClient, headers: dict | None = None, **body) -> dict:
    res = client.post("/api/apikeys", json={"name": "k", **body}, headers=headers or {})
    assert res.status_code == 201, res.text
    return res.json()


def _listed(client: TestClient, headers: dict | None = None) -> dict[str, dict]:
    res = client.get("/api/apikeys", headers=headers or {})
    assert res.status_code == 200, res.text
    return {k["id"]: k for k in res.json()["keys"]}


def _v1(app, raw: str, path: str = "/v1/agents", method: str = "GET"):  # noqa: F811
    with TestClient(app, client=PEER) as client:
        if method == "GET":
            return client.get(path, headers={"X-Api-Key": raw})
        return client.post(path, headers={"X-Api-Key": raw}, json={"prompt": "hi"})


def _row(key_id: str) -> ApiKey:
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(ApiKey, key_id)
            db.expunge(row)
            return row
    finally:
        db.close()


@pytest.fixture
def world(app):  # noqa: F811
    """Project alpha beside the default project; joan develops in alpha only, mei
    in both (default through her workspace grant)."""
    _project("alpha")
    joan = _user("joan", grant=False)
    _member_of("alpha", joan)
    mei = _user("mei")
    _member_of("alpha", mei)
    return {
        "joan": joan, "mei": mei,
        "alpha_agent": _agent("alpha-agent", "alpha"),
        "default_agent": _agent("default-agent", "default"),
    }


# ── minting and the key routes ───────────────────────────────────────────────


def test_a_key_is_stamped_with_the_current_project_and_listed_only_there(app, world):  # noqa: F811
    mei = _login(app, "mei")
    alpha_key = _mint(mei, ALPHA)
    default_key = _mint(mei, {"X-Project": "default"})
    assert alpha_key["project_id"] == "alpha"
    assert default_key["project_id"] == "default"
    assert _row(alpha_key["id"]).project_id == "alpha"

    assert set(_listed(mei, ALPHA)) == {alpha_key["id"]}
    assert set(_listed(mei, {"X-Project": "default"})) == {default_key["id"]}
    # the admin, too, sees one project at a time
    admin = _admin(app)
    assert set(_listed(admin, ALPHA)) == {alpha_key["id"]}
    assert set(_listed(admin)) == {default_key["id"]}  # header-less: the default project


def test_another_projects_key_id_reads_as_missing(app, world):  # noqa: F811
    mei = _login(app, "mei")
    alpha_key = _mint(mei, ALPHA)
    here = {"X-Project": "default"}
    for method, path in (
        ("POST", f"/api/apikeys/{alpha_key['id']}/disable"),
        ("POST", f"/api/apikeys/{alpha_key['id']}/enable"),
        ("PATCH", f"/api/apikeys/{alpha_key['id']}"),
        ("GET", f"/api/apikeys/{alpha_key['id']}/usage"),
    ):
        res = mei.request(method, path, headers=here, json={"name": "x"} if method == "PATCH"
                          else None)
        assert res.status_code == 404, (path, res.text)
        assert res.json()["code"] == "apikey.not_found"
    assert _row(alpha_key["id"]).enabled is True


def test_a_key_scope_names_only_its_projects_agents(app, world):  # noqa: F811
    mei = _login(app, "mei")
    res = mei.post("/api/apikeys", json={"name": "k", "agent_ids": [world["default_agent"]]},
                   headers=ALPHA)
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "apikey.unknown_agent"
    assert _mint(mei, ALPHA, agent_ids=[world["alpha_agent"]])["agent_ids"] == [
        world["alpha_agent"]
    ]


# ── /v1 runs as the key's project ────────────────────────────────────────────


def test_v1_lists_and_invokes_only_the_keys_project(app, world, stub_invoke):  # noqa: F811
    joan = _login(app, "joan")
    raw = _mint(joan, ALPHA)["key"]
    listed = _v1(app, raw)
    assert listed.status_code == 200, listed.text
    assert {a["id"] for a in listed.json()["agents"]} == {world["alpha_agent"]}

    for suffix in ("invoke", "invoke-stream"):
        res = _v1(app, raw, f"/v1/agents/{world['default_agent']}/{suffix}", "POST")
        assert res.status_code == 404, res.text
        assert res.json()["code"] == "agent.not_found"

    assert _v1(app, raw, f"/v1/agents/{world['alpha_agent']}/invoke", "POST").status_code == 200
    streamed = _v1(app, raw, f"/v1/agents/{world['alpha_agent']}/invoke-stream", "POST")
    assert streamed.status_code == 200, streamed.text
    # the sync handler and the streaming generator both ran as the key's project
    assert stub_invoke == [("invoke", "alpha"), ("stream", "alpha")]


def test_a_single_project_key_runs_as_the_default_project(app, stub_invoke):  # noqa: F811
    _user("solo")
    solo = _login(app, "solo")
    agent = _agent("solo-agent", "default")
    raw = _mint(solo)["key"]
    assert {a["id"] for a in _v1(app, raw).json()["agents"]} == {agent}
    assert _v1(app, raw, f"/v1/agents/{agent}/invoke", "POST").status_code == 200
    assert stub_invoke == [("invoke", "default")]


def test_usage_rows_carry_the_keys_project(app, world):  # noqa: F811
    joan = _login(app, "joan")
    minted = _mint(joan, ALPHA)
    assert _v1(app, minted["key"]).status_code == 200
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT project_id FROM api_key_usage WHERE key_id = :k"), {"k": minted["id"]},
        ).fetchall()
    finally:
        db.close()
    assert [r[0] for r in rows] == ["alpha"]


# ── leaving the project ends the key ─────────────────────────────────────────


def _remove_member(pid: str, user_id: str, *, disable: bool) -> None:
    db = SessionLocal()
    try:
        db.query(ProjectMember).filter(
            ProjectMember.project_id == pid, ProjectMember.user_id == user_id,
        ).delete()
        access.bump_epoch(db)
        if disable:
            assert api_keys_service.disable_offboarded_keys_in_project(db, pid) == 1
        db.commit()
    finally:
        db.close()


def test_a_creator_removed_from_the_project_is_refused_at_use(app, world):  # noqa: F811
    joan = _login(app, "joan")
    minted = _mint(joan, ALPHA)
    assert _v1(app, minted["key"]).status_code == 200
    _remove_member("alpha", world["joan"], disable=False)

    res = _v1(app, minted["key"])
    assert res.status_code == 401
    assert res.json()["code"] == "auth.invalid_api_key"  # /v1 never says why
    assert _row(minted["id"]).enabled is False
    admin = _admin(app)
    assert _listed(admin, ALPHA)[minted["id"]]["creator_offboarded"] == "no_workspace_grant"


def test_membership_removal_disables_in_the_same_transaction(app, world):  # noqa: F811
    mei = _login(app, "mei")
    alpha_key = _mint(mei, ALPHA)
    default_key = _mint(mei, {"X-Project": "default"})
    _remove_member("alpha", world["mei"], disable=True)

    assert _row(alpha_key["id"]).enabled is False
    assert _row(default_key["id"]).enabled is True  # still a member of default
    admin = _admin(app)
    # still reaches the workspace (default project), just not this project
    listed = _listed(admin, ALPHA)[alpha_key["id"]]
    assert listed["creator_offboarded"] == api_keys_service.REASON_NO_PROJECT
    res = admin.post(f"/api/apikeys/{alpha_key['id']}/enable", headers=ALPHA)
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "apikey.creator_offboarded"
    assert res.json()["detail"] == {"reason": "not_a_project_member"}
    assert _v1(app, default_key["key"]).status_code == 200


def test_user_paths_disable_keys_across_projects(app, world):  # noqa: F811
    """`users.delete_user` / a disable reach the account's keys in every project."""
    mei = _login(app, "mei")
    alpha_key = _mint(mei, ALPHA)
    default_key = _mint(mei, {"X-Project": "default"})
    admin = _admin(app)
    assert admin.patch(f"/api/users/{world['mei']}", json={"status": "disabled"}).status_code \
        == 200
    assert _row(alpha_key["id"]).enabled is False
    assert _row(default_key["id"]).enabled is False


def test_a_group_member_keeps_the_key_through_the_group(app, world):  # noqa: F811
    """Reach through a project group counts as membership (design §2.4)."""
    db = SessionLocal()
    try:
        db.execute(text("INSERT INTO project_groups (id, name, description, created_at, updated_at)"
                        " VALUES ('risk', 'Risk', '', '2026-01-01', '2026-01-01')"))
        db.execute(text("UPDATE projects SET group_id = 'risk' WHERE id = 'alpha'"))
        db.execute(text("INSERT INTO group_members (group_id, user_id, role_key, added_at)"
                        " VALUES ('risk', :u, 'developer', '2026-01-01')"), {"u": world["joan"]})
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()
    joan = _login(app, "joan")
    minted = _mint(joan, ALPHA)
    _remove_member("alpha", world["joan"], disable=False)  # the direct row only
    assert _v1(app, minted["key"]).status_code == 200


# ── the project itself ───────────────────────────────────────────────────────


def _set_project(pid: str, sql: str) -> None:
    db = SessionLocal()
    try:
        db.execute(text(sql), {"p": pid})
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()


def test_an_archived_projects_keys_stop_working_whoever_minted_them(app, world):  # noqa: F811
    joan = _login(app, "joan")
    member_key = _mint(joan, ALPHA)
    admin = _admin(app)
    admin_key = _mint(admin, ALPHA)
    _set_project("alpha", "UPDATE projects SET status = 'archived' WHERE id = :p")

    for minted in (member_key, admin_key):
        assert _v1(app, minted["key"]).status_code == 401
        assert _row(minted["id"]).enabled is False
    listed = _listed(admin, ALPHA)
    assert listed[admin_key["id"]]["creator_offboarded"] == "project_archived"
    # un-archiving does not revive them (design §11.5)
    _set_project("alpha", "UPDATE projects SET status = 'active' WHERE id = :p")
    assert _v1(app, admin_key["key"]).status_code == 401


def test_an_unbound_projects_keys_stop_working(app, world):  # noqa: F811
    admin = _admin(app)
    minted = _mint(admin, ALPHA)
    _set_project("alpha", "DELETE FROM project_workspaces WHERE project_id = :p")
    assert _v1(app, minted["key"]).status_code == 401
    db = SessionLocal()
    try:
        with project_context(None):
            key = db.get(ApiKey, minted["id"])
            assert api_keys_service.creator_offboarded(db, key) == "project_unbound"
    finally:
        db.close()


# ── scope_for_key ────────────────────────────────────────────────────────────


def test_scope_for_key_carries_the_keys_project(app, world):  # noqa: F811
    db = SessionLocal()
    try:
        def key(created_by, pid="alpha"):
            return ApiKey(workspace_id=WS, created_by=created_by, project_id=pid)

        assert visibility.scope_for_key(db, key(world["joan"])) == Viewer(None, False, "alpha")
        assert visibility.scope_for_key(db, key(None, None)) is Viewer.ALL
        db.execute(text("UPDATE workspaces SET settings = :s WHERE id = :w"),
                   {"s": '{"resource_visibility": "isolated"}', "w": WS})
        db.commit()
        assert visibility.scope_for_key(db, key(world["joan"])) == Viewer(
            world["joan"], True, "alpha",
        )
        assert visibility.scope_for_key(db, key(None)) == Viewer(None, False, "alpha")
        assert visibility.viewer_for_key is visibility.scope_for_key
    finally:
        db.close()
