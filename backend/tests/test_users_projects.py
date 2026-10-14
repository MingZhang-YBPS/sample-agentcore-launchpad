"""User management with projects (projects design §7.4–§7.6, §7.10, P1F-users).

`POST` / `PATCH /api/users` with `projects` / `groups` (the approval flow places a
pending account into projects in the same transaction), the default project's
`inherit` row still following `workspaces`, the list's `projects` / `groups` fields
and its `role` / `project` / `group` filters, `GET /api/users/{id}` and
`/api/users/{id}/access`, the bulk `POST /api/users/bulk/projects`, and an admin's
membership rows kept across a promotion and back."""

import pytest
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import AuditEvent, User
from app.models.projects import GroupMember, Project, ProjectGroup, ProjectMember
from app.services import access
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


def _admin(app):  # noqa: F811
    from fastapi.testclient import TestClient

    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


def _group(gid: str, *projects: str, archived: bool = False) -> None:
    from datetime import UTC, datetime

    db = SessionLocal()
    try:
        db.add(ProjectGroup(id=gid, name=gid.upper(),
                            archived_at=datetime.now(UTC) if archived else None))
        for pid in projects:
            db.get(Project, pid).group_id = gid
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()


def _rows(user_id: str) -> dict[str, str]:
    db = SessionLocal()
    try:
        return {m.project_id: m.role_key for m in db.scalars(
            select(ProjectMember).where(ProjectMember.user_id == user_id))}
    finally:
        db.close()


def _group_rows(user_id: str) -> dict[str, str]:
    db = SessionLocal()
    try:
        return {m.group_id: m.role_key for m in db.scalars(
            select(GroupMember).where(GroupMember.user_id == user_id))}
    finally:
        db.close()


def _actions() -> list[str]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(AuditEvent.action)))
    finally:
        db.close()


def _new(admin, username: str, **extra) -> dict:
    res = admin.post("/api/users", json={
        "username": username, "email": f"{username}@acme-corp.com",
        "password": "sufficient-pass", **extra,
    })
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def admin(app):  # noqa: F811
    _project("alpha")
    _project("beta")
    return _admin(app)


# ── create ────────────────────────────────────────────────────────────────────


def test_create_places_the_account_into_projects_and_groups(admin):
    _group("risk", "beta")
    body = _new(admin, "zhang", projects=[{"project_id": "alpha", "role_key": "developer"}],
                groups=[{"group_id": "risk", "role_key": "viewer"}])
    # workspaces omitted ⇒ `default` and its `inherit` row, as before
    assert body["workspaces"] == [WS]
    assert _rows(body["id"]) == {"alpha": "developer", "default": "inherit"}
    assert _group_rows(body["id"]) == {"risk": "viewer"}
    projects = {p["id"]: p for p in body["projects"]}
    assert body["projects"][0]["id"] == "default"  # the default project first
    assert projects["alpha"]["role"] == "developer"
    assert projects["alpha"]["sources"] == [{"source": "direct", "role_key": "developer"}]
    assert projects["beta"]["sources"] == [{"source": "group:risk", "role_key": "viewer"}]
    assert body["groups"] == [{"id": "risk", "name": "RISK", "role": "viewer",
                               "archived": False}]
    assert {"project.members.add", "group.members.add", "user.create"} <= set(_actions())


def test_create_refuses_a_bad_item_and_writes_nothing(admin):
    res = admin.post("/api/users", json={
        "username": "ghost", "email": "ghost@acme-corp.com", "password": "sufficient-pass",
        "projects": [{"project_id": "nowhere", "role_key": "developer"}]})
    assert res.status_code == 404 and res.json()["code"] == "project.not_found"
    res = admin.post("/api/users", json={
        "username": "ghost", "email": "ghost@acme-corp.com", "password": "sufficient-pass",
        "projects": [{"project_id": "alpha", "role_key": "no_such_role"}]})
    assert res.status_code == 400 and res.json()["code"] == "project.role_unknown"
    res = admin.post("/api/users", json={
        "username": "ghost", "email": "ghost@acme-corp.com", "password": "sufficient-pass",
        "projects": [{"project_id": "alpha", "role_key": "developer"},
                     {"project_id": "alpha", "role_key": "viewer"}]})
    assert res.status_code == 400 and res.json()["code"] == "users.invalid_projects"
    # `inherit` is the default project's compatibility role only
    res = admin.post("/api/users", json={
        "username": "ghost", "email": "ghost@acme-corp.com", "password": "sufficient-pass",
        "projects": [{"project_id": "alpha", "role_key": "inherit"}]})
    assert res.status_code == 400 and res.json()["code"] == "project.role_unknown"
    _group("risk")
    res = admin.post("/api/users", json={
        "username": "ghost", "email": "ghost@acme-corp.com", "password": "sufficient-pass",
        "groups": [{"group_id": "risk", "role_key": "custom_role"}]})
    assert res.status_code == 400 and res.json()["code"] == "group.role_unknown"
    res = admin.post("/api/users", json={
        "username": "ghost", "email": "ghost@acme-corp.com", "password": "sufficient-pass",
        "groups": [{"group_id": "nope", "role_key": "viewer"}]})
    assert res.status_code == 404 and res.json()["code"] == "group.not_found"
    db = SessionLocal()
    try:
        assert db.scalars(select(User).where(User.username_key == "ghost")).first() is None
        assert not db.scalars(select(ProjectMember)).all()
    finally:
        db.close()


def test_an_admin_is_created_without_memberships(admin):
    res = admin.post("/api/users", json={
        "username": "boss", "email": "boss@acme-corp.com", "password": "sufficient-pass",
        "role": "admin", "workspaces": [],
        "projects": [{"project_id": "alpha", "role_key": "developer"}]})
    assert res.status_code == 400 and res.json()["code"] == "users.admin_no_grants"


def test_joining_an_archived_group_is_refused(admin):
    _group("old", archived=True)
    res = admin.post("/api/users", json={
        "username": "late", "email": "late@acme-corp.com", "password": "sufficient-pass",
        "groups": [{"group_id": "old", "role_key": "viewer"}]})
    assert res.status_code == 409 and res.json()["code"] == "group.archived"


# ── approval and PATCH ────────────────────────────────────────────────────────


def test_approval_places_a_pending_account_into_projects(admin, app):  # noqa: F811
    uid = _user("newbie", grant=False)
    db = SessionLocal()
    try:
        row = db.get(User, uid)
        row.status, row.expires_at = "pending", None
        db.commit()
    finally:
        db.close()
    _group("risk", "beta")
    res = admin.patch(f"/api/users/{uid}", json={
        "status": "active",
        "projects": [{"project_id": "alpha", "role_key": "developer"}],
        "groups": [{"group_id": "risk", "role_key": "sme"}],
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == "active" and body["days_remaining"] is not None
    assert {p["id"] for p in body["projects"]} == {"alpha", "beta"}
    assert body["workspaces"] == []  # no grant: membership is the reach (D-6)
    # …and the account works inside it on its next request
    client = _login(app, "newbie")
    me = client.get("/api/me/access").json()
    ids = {p["id"] for w in me["workspaces"] for p in w["projects"]}
    assert ids == {"alpha", "beta"}
    db = SessionLocal()
    try:
        targets = list(db.scalars(
            select(AuditEvent.target).where(AuditEvent.action == "user.update")))
    finally:
        db.close()
    assert any("projects=alpha:developer" in t and "groups=risk:sme" in t for t in targets)


def test_the_workshop_approval_still_joins_the_default_project(admin):
    uid = _user("workshop", grant=False)
    res = admin.patch(f"/api/users/{uid}", json={"status": "active", "workspaces": [WS]})
    assert res.status_code == 200
    assert _rows(uid) == {"default": "inherit"}
    assert res.json()["projects"][0]["role"] == "inherit"


def test_patch_projects_replaces_the_direct_rows_but_spares_inherit(admin):
    body = _new(admin, "rep", projects=[{"project_id": "alpha", "role_key": "developer"}])
    uid = body["id"]
    res = admin.patch(f"/api/users/{uid}", json={
        "projects": [{"project_id": "beta", "role_key": "viewer"}]})
    assert res.status_code == 200
    assert _rows(uid) == {"beta": "viewer", "default": "inherit"}
    # naming the default project replaces its `inherit` role
    admin.patch(f"/api/users/{uid}", json={
        "projects": [{"project_id": "default", "role_key": "release_ops"}]})
    assert _rows(uid) == {"default": "release_ops"}
    # null clears every direct row except a grant's `inherit` one
    admin.patch(f"/api/users/{uid}", json={"projects": None})
    assert _rows(uid) == {}
    admin.patch(f"/api/users/{uid}", json={"workspaces": []})
    admin.patch(f"/api/users/{uid}", json={"workspaces": [WS]})
    admin.patch(f"/api/users/{uid}", json={"projects": []})
    assert _rows(uid) == {"default": "inherit"}
    assert "project.members.remove" in _actions()


def test_patch_keeps_overrides_unless_sent(admin):
    uid = _new(admin, "ovr")["id"]
    admin.patch(f"/api/users/{uid}", json={"projects": [
        {"project_id": "alpha", "role_key": "developer", "overrides": {"release.sign": True}}]})
    admin.patch(f"/api/users/{uid}", json={"projects": [
        {"project_id": "alpha", "role_key": "viewer"}]})
    db = SessionLocal()
    try:
        row = db.get(ProjectMember, ("alpha", uid))
        assert row.role_key == "viewer" and row.overrides == {"release.sign": True}
    finally:
        db.close()
    admin.patch(f"/api/users/{uid}", json={"projects": [
        {"project_id": "alpha", "role_key": "viewer", "overrides": None}]})
    db = SessionLocal()
    try:
        assert db.get(ProjectMember, ("alpha", uid)).overrides is None
    finally:
        db.close()


def test_patch_groups_replaces_the_group_rows(admin):
    _group("g1")
    _group("g2")
    uid = _new(admin, "grp", groups=[{"group_id": "g1", "role_key": "viewer"}])["id"]
    admin.patch(f"/api/users/{uid}", json={"groups": [
        {"group_id": "g1", "role_key": "developer"}, {"group_id": "g2", "role_key": "sme"}]})
    assert _group_rows(uid) == {"g1": "developer", "g2": "sme"}
    admin.patch(f"/api/users/{uid}", json={"groups": []})
    assert _group_rows(uid) == {}
    assert {"group.members.update", "group.members.remove"} <= set(_actions())


def test_a_bad_patch_item_rolls_back_the_whole_patch(admin):
    uid = _new(admin, "atomic")["id"]
    res = admin.patch(f"/api/users/{uid}", json={
        "status": "disabled",
        "projects": [{"project_id": "alpha", "role_key": "developer"},
                     {"project_id": "beta", "role_key": "no_such_role"}]})
    assert res.status_code == 400
    assert _rows(uid) == {"default": "inherit"}
    assert admin.get(f"/api/users/{uid}").json()["status"] == "active"


def test_the_builtin_admin_takes_no_memberships(admin):
    res = admin.patch("/api/users/builtin-admin", json={
        "projects": [{"project_id": "alpha", "role_key": "developer"}]})
    assert res.status_code == 400 and res.json()["code"] == "users.builtin_immutable"


def test_promotion_keeps_the_rows_and_demotion_restores_them(admin):
    uid = _new(admin, "promo", projects=[{"project_id": "alpha", "role_key": "developer"}])["id"]
    assert admin.patch(f"/api/users/{uid}", json={"role": "admin"}).status_code == 200
    assert _rows(uid) == {"alpha": "developer", "default": "inherit"}
    detail = admin.get(f"/api/users/{uid}/access").json()
    projects = {p["id"]: p for w in detail["workspaces"] for p in w["projects"]}
    # every project by role; the kept row still shows where alpha would come from
    assert detail["is_admin"] and {"default", "alpha", "beta"} <= set(projects)
    assert projects["beta"]["bypass"] and not projects["alpha"]["bypass"]
    assert sorted(projects["alpha"]["permissions"]) == sorted(access.ALL_PROJECT_KEYS)
    assert admin.patch(f"/api/users/{uid}", json={"role": "member"}).status_code == 200
    assert _rows(uid) == {"alpha": "developer", "default": "inherit"}


# ── list, detail, access ──────────────────────────────────────────────────────


def test_list_filters_by_project_group_and_role(admin):
    _group("risk", "beta")
    a = _new(admin, "aaa", projects=[{"project_id": "alpha", "role_key": "developer"}])["id"]
    b = _new(admin, "bbb", groups=[{"group_id": "risk", "role_key": "viewer"}])["id"]
    op = _new(admin, "ccc", role="operator")["id"]

    def ids(**params):
        res = admin.get("/api/users", params=params)
        assert res.status_code == 200, res.text
        return {row["id"] for row in res.json()["items"]}, res.json()

    assert ids(project="alpha")[0] == {a}
    assert ids(project_id="beta")[0] == {b}  # through the group
    assert ids(group="risk")[0] == {b}
    assert ids(role="operator")[0] == {op}
    found, page = ids(project="alpha")
    assert page["builtin"] is None  # no membership to match
    assert ids()[1]["builtin"] is not None
    row = next(r for r in admin.get("/api/users").json()["items"] if r["id"] == a)
    assert [p["id"] for p in row["projects"]] == ["default", "alpha"]
    res = admin.get("/api/users", params={"project": "nowhere"})
    assert res.status_code == 404 and res.json()["code"] == "project.not_found"
    res = admin.get("/api/users", params={"group": "nowhere"})
    assert res.status_code == 404 and res.json()["code"] == "group.not_found"
    assert admin.get("/api/users", params={"role": "boss"}).status_code == 422


def test_detail_and_access(admin, app):  # noqa: F811
    uid = _new(admin, "dee", projects=[{"project_id": "alpha", "role_key": "viewer"}])["id"]
    detail = admin.get(f"/api/users/{uid}")
    assert detail.status_code == 200
    assert {p["id"] for p in detail.json()["projects"]} == {"default", "alpha"}
    assert admin.get("/api/users/nobody").status_code == 404
    assert admin.get("/api/users/builtin-admin").json()["builtin"] is True

    res = admin.get(f"/api/users/{uid}/access")
    assert res.status_code == 200
    body = res.json()
    assert body["user_id"] == uid and body["state"] == "active" and not body["is_admin"]
    projects = {p["id"]: p for w in body["workspaces"] for p in w["projects"]}
    assert set(projects) == {"default", "alpha"}
    assert projects["alpha"]["permissions"] == []  # viewer
    assert "agents.deploy" in projects["default"]["permissions"]  # inherit = member keys
    # the same map the account sees for itself
    assert _login(app, "dee").get("/api/me/access").json()["workspaces"] == body["workspaces"]

    builtin = admin.get("/api/users/builtin-admin/access").json()
    assert builtin["is_admin"] and {p["id"] for w in builtin["workspaces"]
                                    for p in w["projects"]} >= {"default", "alpha", "beta"}
    assert admin.get("/api/users/nobody/access").status_code == 404


def test_member_cannot_reach_the_new_routes(admin, app):  # noqa: F811
    _user("plain")
    client = _login(app, "plain")
    uid = _user("other")
    assert client.get(f"/api/users/{uid}").status_code == 403
    assert client.get(f"/api/users/{uid}/access").status_code == 403
    assert client.post("/api/users/bulk/projects", json={
        "user_ids": [uid], "project_id": "alpha", "role_key": "developer"}).status_code == 403


# ── bulk ──────────────────────────────────────────────────────────────────────


def test_bulk_add_to_project(admin):
    a, b = _user("bulk-a"), _user("bulk-b")
    boss = _new(admin, "bulk-boss", role="admin", workspaces=[])["id"]
    _member_of("alpha", b, "viewer")
    res = admin.post("/api/users/bulk/projects", json={
        "user_ids": [a, b, boss, "builtin-admin", a],
        "project_id": "alpha", "role_key": "developer"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["added"] == [a] and body["updated"] == [b] and body["unchanged"] == []
    assert {s["user_id"] for s in body["skipped"]} == {boss, "builtin-admin"}
    assert _rows(a)["alpha"] == "developer" and _rows(b)["alpha"] == "developer"
    assert "alpha" not in _rows(boss)
    # grants untouched; a second run changes nothing
    assert admin.get(f"/api/users/{a}").json()["workspaces"] == [WS]
    again = admin.post("/api/users/bulk/projects", json={
        "user_ids": [a, b], "project_id": "alpha", "role_key": "developer"}).json()
    assert again["added"] == [] and again["updated"] == [] and set(again["unchanged"]) == {a, b}
    assert {"project.members.add", "project.members.update"} <= set(_actions())


def test_bulk_refuses_unknown_ids_and_roles(admin):
    a = _user("bulk-c")
    res = admin.post("/api/users/bulk/projects", json={
        "user_ids": [a, "ghost"], "project_id": "alpha", "role_key": "developer"})
    assert res.status_code == 404 and res.json()["code"] == "users.not_found"
    res = admin.post("/api/users/bulk/projects", json={
        "user_ids": [a], "project_id": "nowhere", "role_key": "developer"})
    assert res.status_code == 404 and res.json()["code"] == "project.not_found"
    res = admin.post("/api/users/bulk/projects", json={
        "user_ids": [a], "project_id": "alpha", "role_key": "nope"})
    assert res.status_code == 400 and res.json()["code"] == "project.role_unknown"
    assert "alpha" not in _rows(a)
    assert admin.post("/api/users/bulk/projects", json={
        "user_ids": [], "project_id": "alpha", "role_key": "developer"}).status_code == 422


def test_workspace_grants_still_drive_the_default_project(admin):
    uid = _user("grantee", grant=False)
    res = admin.put(f"/api/workspaces/{WS}/grants", json={"grant": [uid]})
    assert res.status_code == 200, res.text
    assert _rows(uid) == {"default": "inherit"}
    admin.put(f"/api/workspaces/{WS}/grants", json={"revoke": [uid]})
    assert _rows(uid) == {}
