"""The structure-only platform administrator — steward (projects design §11.4,
G11-steward): the role and its two convergence functions, the ADMIN / ADMIN_DATA split
(every data route refused, every structure route let through), the platform-role
guards on account management, no grant or membership of its own, the self guard,
the journal + owner notice of every membership change, the optional two-person rule,
and the last-admin guard counting administrators only."""

from types import SimpleNamespace

import pytest
from fastapi import Request
from sqlalchemy import select, text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.route_policy import (
    _DATA_ROUTES,
    _STRUCTURE_ROUTES,
    ADMIN_DATA,
    WORKSPACE_EXEMPT,
    admin_kind,
    enforce_route_policy,
    require_admin_kind,
)
from app.models.ledger import AuditEvent
from app.models.projects import ProjectMember
from app.routers.auth import Identity
from app.services import access
from app.services import users as users_service
from tests.test_project_scope import _login, _user, app  # noqa: F401
from tests.test_projects_api import _admin, _create

WS = DEFAULT_WORKSPACE_ID
STEWARD = users_service.ROLE_STEWARD


def _targets(action: str) -> list[str]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(AuditEvent.target).where(AuditEvent.action == action)))
    finally:
        db.close()


def _direct_members(project_id: str) -> set[str]:
    db = SessionLocal()
    try:
        return set(db.scalars(
            select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)))
    finally:
        db.close()


@pytest.fixture
def world(app):  # noqa: F811
    """`shop` owned by olivia; sam and sue are stewards; pat is a plain member with
    no project yet (the puppet a steward could make)."""
    admin = _admin(app)
    olivia = _user("olivia", grant=False)
    pat = _user("pat", grant=False)
    sam = _user("sam", role=STEWARD, grant=False)
    sue = _user("sue", role=STEWARD, grant=False)
    _create(admin, "shop", owners=[olivia])
    return SimpleNamespace(
        app=app, admin=admin, olivia_id=olivia, pat_id=pat, sam_id=sam, sue_id=sue,
        sam=_login(app, "sam"), sue=_login(app, "sue"), olivia=_login(app, "olivia"),
    )


# ── the role ──────────────────────────────────────────────────────────────────


def test_the_role_manages_structure_and_bypasses_no_data():
    steward = SimpleNamespace(role=STEWARD, user_id="s1", username="s")
    admin = SimpleNamespace(role="admin", user_id="a1", username="a")
    assert STEWARD in users_service.ROLES
    assert access.has_data_bypass(steward) is False
    assert access.can_manage_structure(steward) is True
    assert access.has_data_bypass(admin) and access.can_manage_structure(admin)
    assert users_service.default_permissions(STEWARD) == frozenset()


def test_a_steward_holds_no_grant_or_membership(world):
    res = world.admin.post("/api/users", json={
        "username": "stan", "email": "stan@acme-corp.com", "role": STEWARD,
        "workspaces": [WS],
    })
    assert res.status_code == 400 and res.json()["code"] == "users.steward_no_grants"
    res = world.admin.post("/api/users", json={
        "username": "stan", "email": "stan@acme-corp.com", "role": STEWARD,
    })
    assert res.status_code == 201, res.text
    assert res.json()["role"] == STEWARD and res.json()["workspaces"] == []
    # nor through a later patch
    res = world.admin.patch(f"/api/users/{world.sam_id}", json={"workspaces": [WS]})
    assert res.status_code == 400 and res.json()["code"] == "users.steward_no_grants"


def test_becoming_a_steward_clears_every_grant_and_membership(world):
    uid = _user("mia")  # a default workspace grant → the default project's inherit row
    res = world.admin.put("/api/projects/shop/members",
                          json={"upsert": [{"user_id": uid, "role_key": "developer"}]})
    assert res.status_code == 200, res.text
    assert uid in _direct_members("shop") and uid in _direct_members("default")
    res = world.admin.patch(f"/api/users/{uid}", json={"role": STEWARD})
    assert res.status_code == 200, res.text
    assert uid not in _direct_members("shop") | _direct_members("default")
    assert res.json()["workspaces"] == []
    assert _targets("steward.access_cleared") == [f"user:mia ({uid}) rows=3"]
    assert _targets("steward.grant") == [f"user:mia ({uid}) role=steward"]
    mia = _login(world.app, "mia")
    body = mia.get("/api/me/access").json()
    assert body["is_steward"] is True and body["is_admin"] is False
    assert body["can_manage_structure"] is True and body["workspaces"] == []


def test_a_steward_s_snapshot_is_always_empty(world):
    db = SessionLocal()
    try:  # a row that somehow survived still opens nothing
        db.execute(text("INSERT INTO project_members (project_id, user_id, role_key, added_at)"
                        " VALUES ('shop', :u, 'developer', '2026-10-15 00:00:00')"),
                   {"u": world.sam_id})
        access.bump_epoch(db)
        db.commit()
        who = SimpleNamespace(user_id=world.sam_id, role=STEWARD, permission_overrides=None)
        assert access.snapshot_for(db, who).memberships == {}
    finally:
        db.close()


# ── the route split ───────────────────────────────────────────────────────────


def _request(identity: Identity, method: str, path_format: str) -> Request:
    path = "/".join("probe" if s.startswith("{") else s for s in path_format.split("/"))
    request = Request({
        "type": "http", "method": method, "path": path, "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 4321),
        "route": SimpleNamespace(path_format=path_format), "path_params": {},
    })
    request.state.identity = identity
    return request


STEWARD_IDENTITY = Identity(username="sam", role=STEWARD, user_id="steward-probe",
                            permissions=frozenset())


@pytest.mark.parametrize(("method", "path_format"), sorted(set(_DATA_ROUTES)))
def test_a_steward_is_refused_every_data_route(app, method, path_format):  # noqa: F811
    assert admin_kind(method, path_format) == ADMIN_DATA
    with pytest.raises(AppError) as err:
        enforce_route_policy(_request(STEWARD_IDENTITY, method, path_format))
    assert (err.value.status_code, err.value.code) == (403, "auth.forbidden")


@pytest.mark.parametrize(("method", "path_format"), sorted(set(_STRUCTURE_ROUTES)))
def test_a_steward_passes_every_structure_route(app, method, path_format):  # noqa: F811
    require_admin_kind(_request(STEWARD_IDENTITY, method, path_format), method, path_format)
    if (method, path_format) in WORKSPACE_EXEMPT:  # hub-global: the whole policy passes
        enforce_route_policy(_request(STEWARD_IDENTITY, method, path_format))


def test_a_member_still_meets_both_kinds_as_admin_only(app):  # noqa: F811
    member = Identity(username="m", role="member", user_id="member-probe")
    for method, path_format in (_STRUCTURE_ROUTES[0], _DATA_ROUTES[0]):
        with pytest.raises(AppError) as err:
            require_admin_kind(_request(member, method, path_format), method, path_format)
        assert err.value.code == "auth.forbidden"


def test_structure_reads_answer_200_and_data_403_404_over_http(world):
    sam = world.sam
    for path in ("/api/users", "/api/users/stats", "/api/admins", "/api/project-groups",
                 "/api/projects", "/api/projects/shop", "/api/projects/shop/members",
                 "/api/projects/shop/roles", "/api/projects/shop/lifecycle",
                 "/api/projects/shop/candidates", f"/api/users/{world.olivia_id}",
                 f"/api/users/{world.olivia_id}/access", "/api/models/settings",
                 "/api/announcements/manage", "/api/videos/manage", "/api/steward/settings",
                 "/api/departments"):
        res = sam.get(path)
        assert res.status_code == 200, (path, res.text)
    assert {p["id"] for p in sam.get("/api/projects").json()["projects"]} >= {"default", "shop"}
    # data: the ADMIN_DATA routes, the project audit view, and the project-scoped surface
    for path in ("/api/audit", "/api/costs", "/api/fleet", "/api/inbox", "/api/overview/ttfa"):
        res = sam.get(path)
        assert (res.status_code, res.json()["code"]) == (403, "auth.forbidden"), path
    res = sam.get("/api/projects/shop/audit")
    assert (res.status_code, res.json()["code"]) == (404, "project.not_found")
    for path in ("/api/agents", "/api/observability/sessions"):
        assert sam.get(path).status_code in (403, 404), path
    res = sam.post("/api/projects/shop/export")
    assert res.status_code == 403
    # a steward edits the structure of any project: its members and roles
    res = sam.put("/api/projects/shop/roles/auditor",
                  json={"name": "Auditor", "permissions": ["project.use"]})
    assert res.status_code == 200, res.text


# ── accounts: the platform roles are the administrators' ──────────────────────


def test_a_steward_manages_accounts_but_not_platform_roles(world):
    sam = world.sam
    res = sam.post("/api/users", json={"username": "newbie", "email": "newbie@acme-corp.com"})
    assert res.status_code == 201, res.text
    newbie = res.json()["id"]
    assert sam.patch(f"/api/users/{newbie}", json={"role": "operator"}).status_code == 200
    for body in ({"username": "boss", "email": "boss@acme-corp.com", "role": "admin"},
                 {"username": "stew", "email": "stew@acme-corp.com", "role": STEWARD}):
        res = sam.post("/api/users", json=body)
        assert (res.status_code, res.json()["code"]) == (403, "users.platform_role_admin_only")
    for patch in ({"role": "admin"}, {"role": STEWARD}):
        res = sam.patch(f"/api/users/{newbie}", json=patch)
        assert res.status_code == 403, patch
    # another steward, itself, the built-in admin: an administrator's to edit
    for uid in (world.sue_id, world.sam_id, "builtin-admin"):
        assert sam.patch(f"/api/users/{uid}", json={"password": None}).status_code == 403
        assert sam.post(f"/api/users/{uid}/sessions/revoke").status_code in (400, 403)
    assert sam.delete(f"/api/users/{world.sue_id}").status_code == 403
    assert sam.delete(f"/api/users/{newbie}").status_code == 200
    # the admin page lists both types; a steward manages neither
    body = sam.get("/api/admins").json()
    roles = {i["username"]: i["role"] for i in body["items"] if not i["builtin"]}
    assert roles == {"sam": STEWARD, "sue": STEWARD}
    assert body["can_manage"] is False and not any(i["can_demote"] for i in body["items"])
    assert world.admin.get("/api/admins").json()["can_manage"] is True
    # a steward cannot loosen its own two-person rule
    res = sam.put("/api/steward/settings", json={"steward_two_person": False})
    assert (res.status_code, res.json()["code"]) == (403, "auth.forbidden")


def test_the_last_admin_guard_counts_administrators_only(world):
    body = world.admin.get("/api/admins").json()
    assert body["can_sign_in"] == 1  # the built-in admin; the stewards are not counted
    db = SessionLocal()
    try:
        assert users_service.admins_who_can_sign_in(db) == 1
    finally:
        db.close()
    # an administrator demotes a steward back to member, journaled
    res = world.admin.patch(f"/api/users/{world.sue_id}", json={"role": "member"})
    assert res.status_code == 200, res.text
    assert _targets("steward.revoke") == [f"user:sue ({world.sue_id}) role=member"]


# ── memberships: the self guard, the journal and the owners' notice ───────────


def test_a_steward_cannot_join_a_project(world):
    sam = world.sam
    res = sam.put("/api/projects/shop/members",
                  json={"upsert": [{"user_id": world.sam_id, "role_key": "viewer"}]})
    assert (res.status_code, res.json()["code"]) == (403, "project.self_membership")
    res = sam.post("/api/projects", json={"id": "mine", "name": "Mine", "workspaces": [WS],
                                          "owners": [world.sam_id]})
    assert (res.status_code, res.json()["code"]) == (403, "project.self_membership")
    # nor can anyone make another steward a member
    res = world.admin.put("/api/projects/shop/members",
                          json={"upsert": [{"user_id": world.sue_id, "role_key": "viewer"}]})
    assert (res.status_code, res.json()["code"]) == (409, "project.steward_member")
    res = world.admin.post("/api/users/bulk/projects", json={
        "user_ids": [world.sue_id, world.pat_id], "project_id": "shop", "role_key": "viewer"})
    assert res.status_code == 200, res.text
    assert res.json()["skipped"] == [{"user_id": world.sue_id, "reason": "steward"}]
    assert res.json()["added"] == [world.pat_id]
    # candidates never offer a steward
    names = {c["username"] for c in world.sam.get("/api/projects/shop/candidates?q=s"
                                                   ).json()["candidates"]}
    assert not names & {"sam", "sue"}


def test_the_puppet_path_leaves_a_journal_line_and_an_owner_notice(world):
    res = world.sam.put("/api/projects/shop/members",
                        json={"upsert": [{"user_id": world.pat_id, "role_key": "developer"}]})
    assert res.status_code == 200, res.text
    assert world.pat_id in res.json()["added"]
    assert _targets("steward.members.change") == ["project:shop upsert:pat:developer"]
    notices = world.olivia.get("/api/steward/notices").json()
    assert notices["unread"] == 1
    (notice,) = notices["items"]
    assert notice["project_id"] == "shop" and notice["actor"] == "sam"
    assert notice["kind"] == "changed" and notice["changes"][0]["username"] == "pat"
    # someone else's notice does not exist for the caller
    assert world.sue.post(f"/api/steward/notices/{notice['id']}/ack").status_code == 404
    assert world.olivia.post(f"/api/steward/notices/{notice['id']}/ack").status_code == 200
    assert world.olivia.get("/api/steward/notices").json()["unread"] == 0
    # through the account side too (PATCH /api/users {projects}) and on removal
    res = world.sam.delete(f"/api/projects/shop/members/{world.pat_id}")
    assert res.status_code == 204
    assert _targets("steward.members.change")[-1] == "project:shop remove:pat"
    res = world.sam.patch(f"/api/users/{world.pat_id}",
                          json={"projects": [{"project_id": "shop", "role_key": "viewer"}]})
    assert res.status_code == 200, res.text
    assert world.olivia.get("/api/steward/notices").json()["unread"] == 2
    # an administrator's change is the ordinary journal only
    world.admin.put("/api/projects/shop/members",
                    json={"upsert": [{"user_id": world.pat_id, "role_key": "developer"}]})
    assert len(_targets("steward.members.change")) == 3


# ── the two-person rule ───────────────────────────────────────────────────────


def _two_person(world, on: bool = True) -> None:
    res = world.admin.put("/api/steward/settings", json={"steward_two_person": on})
    assert res.status_code == 200 and res.json() == {"steward_two_person": on}


def test_two_person_stages_a_steward_s_change_until_someone_else_confirms(world):
    _two_person(world)
    res = world.sam.put("/api/projects/shop/members",
                        json={"upsert": [{"user_id": world.pat_id, "role_key": "developer"}]})
    assert res.status_code == 200, res.text
    (pending_id,) = res.json()["pending"]
    assert res.json()["added"] == [] and world.pat_id not in _direct_members("shop")
    # the owner is told and may confirm; the requester sees it but may not
    (notice,) = world.olivia.get("/api/steward/notices").json()["items"]
    assert notice["kind"] == "pending" and notice["pending_id"] == pending_id
    mine = world.sam.get("/api/steward/pending").json()["items"]
    assert [(i["id"], i["mine"], i["can_confirm"]) for i in mine] == [(pending_id, True, False)]
    res = world.sam.post(f"/api/steward/pending/{pending_id}/confirm")
    assert (res.status_code, res.json()["code"]) == (403, "steward.confirm_self")
    # a plain member who is no owner learns nothing
    stranger = _login(world.app, "pat")
    assert stranger.get("/api/steward/pending").json()["items"] == []
    assert stranger.post(f"/api/steward/pending/{pending_id}/confirm").status_code == 404
    res = world.olivia.post(f"/api/steward/pending/{pending_id}/confirm")
    assert res.status_code == 200, res.text
    assert res.json()["added"] == [world.pat_id] and world.pat_id in _direct_members("shop")
    assert _targets("steward.members.change") == [
        "project:shop upsert:pat:developer confirmed_by:olivia"]
    assert _targets("steward.pending.confirm")[0].startswith(f"project:shop pending:{pending_id}")
    assert world.olivia.post(f"/api/steward/pending/{pending_id}/confirm").status_code == 404


def test_two_person_rejection_and_withdrawal(world):
    _two_person(world)
    first = world.sam.put("/api/projects/shop/members", json={
        "upsert": [{"user_id": world.pat_id, "role_key": "viewer"}]}).json()["pending"][0]
    # another steward may confirm or reject it
    assert world.sue.get("/api/steward/pending").json()["items"][0]["can_confirm"] is True
    res = world.sue.post(f"/api/steward/pending/{first}/reject")
    assert res.status_code == 200 and res.json()["rejected"] is True
    second = world.sam.put("/api/projects/shop/members", json={
        "upsert": [{"user_id": world.pat_id, "role_key": "viewer"}]}).json()["pending"][0]
    assert world.sam.post(f"/api/steward/pending/{second}/reject").status_code == 200
    assert _targets("steward.pending.reject")[0].startswith(f"project:shop pending:{first}")
    assert _targets("steward.pending.withdraw")[0].startswith(f"project:shop pending:{second}")
    assert world.pat_id not in _direct_members("shop")
    # a bad change is refused up front, not staged
    res = world.sam.put("/api/projects/shop/members",
                        json={"upsert": [{"user_id": world.pat_id, "role_key": "nope"}]})
    assert res.status_code >= 400
    assert world.sam.get("/api/steward/pending").json()["items"] == []
    # off again: applied at once
    _two_person(world, on=False)
    res = world.sam.put("/api/projects/shop/members",
                        json={"upsert": [{"user_id": world.pat_id, "role_key": "viewer"}]})
    assert res.json()["added"] == [world.pat_id] and "pending" not in res.json()


def test_an_administrator_s_change_is_never_staged(world):
    _two_person(world)
    res = world.admin.put("/api/projects/shop/members",
                          json={"upsert": [{"user_id": world.pat_id, "role_key": "viewer"}]})
    assert res.json()["added"] == [world.pat_id] and "pending" not in res.json()
