"""Department administrators (projects design §11.1, G11-dept): appointing them and
opening workspaces (platform administrators only), what a department administrator
may do inside their department (projects, bindings to open workspaces, members,
roles, groups, invitations) and the overreach matrix outside it (another
department's project 404, platform settings 403, an unopened workspace 403), the
self-membership journal line, `departments.settings` and its migration."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, _migrate_department_settings
from app.core.route_policy import (
    ADMIN,
    DEPARTMENT_ADMIN_ROUTES,
    PPERM_PREFIX,
    ROUTE_POLICY,
    WORKSPACE_EXEMPT,
)
from app.models.ledger import AuditEvent, User, Workspace
from app.models.projects import ProjectMember
from app.services import access
from tests.test_project_scope import _login, _user, app  # noqa: F401
from tests.test_projects_api import _admin, _create

WS = DEFAULT_WORKSPACE_ID


def _targets(action: str) -> list[str]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(AuditEvent.target).where(AuditEvent.action == action)))
    finally:
        db.close()


def _workspace(ws_id: str) -> None:
    db = SessionLocal()
    try:
        db.add(Workspace(id=ws_id, name=ws_id, account_id="222233334444", region="us-east-2"))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def world(app):  # noqa: F811
    """retail (dana administers it; `default` open to it) and logistics; workspace
    `lab` exists but is opened to nobody. shop is retail's, truck logistics', misc
    has no department; groups retail-g and log-g."""
    admin = _admin(app)
    _workspace("lab")
    dana, ola = _user("dana", grant=False), _user("ola", grant=False)
    for did in ("retail", "logistics"):
        assert admin.post("/api/departments", json={"id": did, "name": did.title()}
                          ).status_code == 201
    _create(admin, "shop", department_id="retail")
    _create(admin, "truck", department_id="logistics")
    _create(admin, "misc")
    for gid, did in (("retail-g", "retail"), ("log-g", "logistics")):
        assert admin.post("/api/project-groups", json={"id": gid, "name": gid,
                                                       "department_id": did}).status_code == 201
    res = admin.put("/api/departments/retail/admins", json={"add": [dana]})
    assert res.status_code == 200, res.text
    assert admin.put("/api/departments/retail/workspaces", json={"add": [WS]}
                     ).status_code == 200
    return SimpleNamespace(admin=admin, dana_id=dana, ola=ola, app=app,
                           dana=_login(app, "dana"))


# ── appointing and opening (platform administrators) ─────────────────────────


def test_appointing_and_opening_are_the_platforms(world):
    admins = world.admin.get("/api/departments/retail/admins").json()["admins"]
    assert [a["username"] for a in admins] == ["dana"]
    opened = world.admin.get("/api/departments/retail/workspaces").json()["workspaces"]
    assert [w["workspace_id"] for w in opened] == [WS]
    assert _targets("department.admins.add") == ["department:retail user:dana"]
    assert _targets("department.workspaces.add") == [f"department:retail workspace:{WS}"]
    # a department administrator cannot appoint, open, or read either list
    dana = world.dana
    assert dana.put("/api/departments/retail/admins",
                    json={"add": [world.ola]}).status_code == 403
    assert dana.put("/api/departments/retail/workspaces",
                    json={"add": ["lab"]}).status_code == 403
    assert dana.get("/api/departments/retail/admins").status_code == 403
    # nor create, edit or archive a department
    assert dana.post("/api/departments", json={"id": "mine", "name": "x"}).status_code == 403
    assert dana.patch("/api/departments/retail", json={"name": "x"}).status_code == 403
    assert dana.patch("/api/departments/retail",
                      json={"settings": {"can_create_users": True}}).status_code == 403
    # validation
    unknown = world.admin.put("/api/departments/retail/workspaces", json={"add": ["nowhere"]})
    assert unknown.status_code == 404 and unknown.json()["code"] == "workspace.not_found"
    gone = world.admin.put("/api/departments/retail/admins", json={"remove": [world.ola]})
    assert gone.status_code == 404 and gone.json()["code"] == "department.admin_not_found"
    removed = world.admin.put("/api/departments/retail/admins", json={"remove": [world.dana_id]})
    assert removed.status_code == 200 and removed.json()["admins"] == []
    assert _targets("department.admins.remove") == ["department:retail user:dana"]
    # removed: the next request meets the routes' own entries again
    assert dana.patch("/api/projects/shop", json={"name": "x"}).status_code == 404


def test_the_access_map_names_the_managed_departments(world):
    body = world.dana.get("/api/me/access").json()
    assert body["managed_departments"] == [
        {"id": "retail", "name": "Retail", "workspaces": [WS]}]
    assert body["can_manage_structure"] is False  # the platform's is still the admin's
    assert world.admin.get("/api/me/access").json()["managed_departments"] == []


# ── projects ──────────────────────────────────────────────────────────────────


def test_a_department_admin_creates_projects_in_open_workspaces_only(world):
    dana = world.dana

    def create(pid, **body):
        return dana.post("/api/projects", json={"id": pid, "name": pid, "workspaces": [WS],
                                                **body})

    made = create("shop2", department_id="retail", group_id="retail-g", owners=[world.ola])
    assert made.status_code == 201, made.text
    assert made.json()["can_manage"] is True and made.json()["my_permissions"] == []
    cases = {
        "other department": (create("x1", department_id="logistics"),
                             403, "department.not_managed"),
        "no department": (create("x2"), 403, "department.not_managed"),
        "unopened workspace": (create("x3", department_id="retail", workspaces=["lab"]),
                               403, "department.workspace_not_open"),
        "another department's group": (create("x4", department_id="retail", group_id="log-g"),
                                       404, "group.not_found"),
        "platform settings": (create("x5", department_id="retail",
                                     settings={"quotas": {"v1_rate_per_minute": 5}}),
                              403, "project.setting_forbidden"),
    }
    for name, (res, status, code) in cases.items():
        assert (res.status_code, res.json().get("code")) == (status, code), name
    # strictness is the department's to set
    assert create("shop3", department_id="retail",
                  settings={"sod_strict": True}).status_code == 201
    # someone who administers no department is refused as before
    ola = _login(world.app, "ola")
    assert ola.post("/api/projects", json={"id": "o1", "name": "o", "workspaces": [WS],
                                           "department_id": "retail"}).status_code == 403


def test_a_department_admin_sees_their_departments_projects_not_their_data(world):
    dana = world.dana
    listed = {p["id"] for p in dana.get("/api/projects").json()["projects"]}
    assert listed == {"shop"}
    shop = dana.get("/api/projects/shop")
    assert shop.status_code == 200
    assert shop.json()["my_permissions"] == [] and shop.json()["can_manage"] is True
    assert dana.get("/api/projects/shop/members").status_code == 200
    assert dana.get("/api/projects/shop/workspaces").status_code == 200
    assert dana.get("/api/projects/shop/roles").status_code == 200
    # no data access without a membership: the workspace's project does not resolve
    res = dana.get("/api/agents", headers={"X-Workspace": WS, "X-Project": "shop"})
    assert res.status_code in (403, 404)
    for pid in ("truck", "misc"):
        assert dana.get(f"/api/projects/{pid}").status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), [
    ("PATCH", "/api/projects/{p}", {"name": "x"}),
    ("POST", "/api/projects/{p}/archive", None),
    ("POST", "/api/projects/{p}/unarchive", None),
    ("PUT", "/api/projects/{p}/workspaces", {"bind": [WS]}),
    ("PUT", "/api/projects/{p}/members", {"upsert": []}),
    ("DELETE", "/api/projects/{p}/members/u1", None),
    ("GET", "/api/projects/{p}/candidates", None),
    ("PUT", "/api/projects/{p}/roles/qa", {"name": "QA", "permissions": []}),
    ("DELETE", "/api/projects/{p}/roles/qa", None),
])
def test_another_departments_project_is_404(world, method, path, body):
    for pid in ("truck", "misc", "nowhere"):
        res = world.dana.request(method, path.format(p=pid), json=body)
        assert (res.status_code, res.json()["code"]) == (404, "project.not_found"), pid


def test_a_department_admin_manages_their_departments_project(world):
    dana = world.dana
    res = dana.patch("/api/projects/shop", json={"name": "Shop front", "group_id": "retail-g",
                                                  "settings": {"sod_strict": True}})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Shop front" and res.json()["group_id"] == "retail-g"
    for body, status, code in (
        ({"department_id": "logistics"}, 403, "department.not_managed"),
        ({"department_id": None}, 403, "department.not_managed"),
        ({"group_id": "log-g"}, 404, "group.not_found"),
        ({"settings": {"inbound_auth_default": None}}, 403, "project.setting_forbidden"),
    ):
        res = dana.patch("/api/projects/shop", json=body)
        assert (res.status_code, res.json()["code"]) == (status, code), body
    # members and roles
    res = dana.put("/api/projects/shop/members",
                   json={"upsert": [{"user_id": world.ola, "role_key": "developer"}]})
    assert res.status_code == 200 and res.json()["added"] == [world.ola]
    assert dana.get("/api/projects/shop/candidates?q=ol").status_code == 200
    assert dana.put("/api/projects/shop/roles/qa",
                    json={"name": "QA", "permissions": ["project.use"]}).status_code == 200
    assert dana.delete("/api/projects/shop/roles/qa").status_code == 204
    # bindings: open workspaces only, never a workspace's default project
    closed = dana.put("/api/projects/shop/workspaces", json={"bind": ["lab"]})
    assert (closed.status_code, closed.json()["code"]) == (403, "department.workspace_not_open")
    default = dana.put("/api/projects/shop/workspaces", json={"make_default": [WS]})
    assert (default.status_code, default.json()["code"]) == (403, "auth.forbidden")
    assert world.admin.put("/api/departments/retail/workspaces",
                           json={"add": ["lab"]}).status_code == 200
    bound = dana.put("/api/projects/shop/workspaces", json={"bind": ["lab"]})
    assert bound.status_code == 200 and bound.json()["bound"] == ["lab"]
    # archive: writes to an archived project are refused like everyone's
    assert dana.post("/api/projects/shop/archive").status_code == 200
    late = dana.patch("/api/projects/shop", json={"description": "x"})
    assert (late.status_code, late.json()["code"]) == (409, "project.archived")
    assert dana.post("/api/projects/shop/unarchive").status_code == 200
    # the lifecycle beyond archive and moving agents stay the platform's
    assert dana.post("/api/projects/shop/archive").status_code == 200
    assert dana.post("/api/projects/shop/delete-request").status_code == 403
    assert dana.get("/api/projects/shop/lifecycle").status_code == 403


def test_a_department_admin_joining_a_project_is_journalled(world):
    res = world.dana.put("/api/projects/shop/members",
                         json={"upsert": [{"user_id": world.dana_id, "role_key": "viewer"}]})
    assert res.status_code == 200, res.text
    assert _targets("department.admins.self_membership") == [
        "department:retail project:shop user:dana role:viewer"]
    # a member now: the project resolves for them
    assert "shop" in {p["id"] for p in world.dana.get("/api/projects").json()["projects"]}
    # an owner who is no department administrator still cannot edit themselves
    ola = _login(world.app, "ola")
    assert world.admin.put("/api/projects/truck/members", json={
        "upsert": [{"user_id": world.ola, "role_key": "project_owner"}]}).status_code == 200
    mine = ola.put("/api/projects/truck/members",
                   json={"upsert": [{"user_id": world.ola, "role_key": "developer"}]})
    assert (mine.status_code, mine.json()["code"]) == (403, "project.self_membership")


def test_a_department_admin_with_the_owner_key_elsewhere_keeps_it(world):
    """`dadmin or the existing policy`: outside the department, pperm still decides."""
    assert world.admin.put("/api/projects/truck/members", json={
        "upsert": [{"user_id": world.dana_id, "role_key": "project_owner"}]}).status_code == 200
    res = world.dana.put("/api/projects/truck/members",
                         json={"upsert": [{"user_id": world.ola, "role_key": "developer"}]})
    assert res.status_code == 200, res.text
    # …but not the structure routes, whose own entry is ADMIN
    assert world.dana.post("/api/projects/truck/archive").status_code == 404


# ── groups ────────────────────────────────────────────────────────────────────


def test_a_department_admin_manages_their_departments_groups(world):
    dana = world.dana
    listed = dana.get("/api/project-groups?department_id=retail")
    assert listed.status_code == 200
    assert [g["id"] for g in listed.json()["groups"]] == ["retail-g"]
    assert dana.get("/api/project-groups").status_code == 403  # unfiltered: the platform's
    assert dana.get("/api/project-groups?department_id=logistics").status_code == 403
    made = dana.post("/api/project-groups", json={"id": "retail-2", "name": "R2",
                                                  "department_id": "retail"})
    assert made.status_code == 201, made.text
    for body in ({"id": "xa", "name": "x"}, {"id": "xb", "name": "x",
                                            "department_id": "logistics"}):
        res = dana.post("/api/project-groups", json=body)
        assert (res.status_code, res.json()["code"]) == (403, "department.not_managed")
    assert dana.patch("/api/project-groups/retail-2", json={"name": "R two"}).status_code == 200
    moved = dana.patch("/api/project-groups/retail-2", json={"department_id": None})
    assert (moved.status_code, moved.json()["code"]) == (403, "department.not_managed")
    put = dana.put("/api/project-groups/retail-g/projects", json={"add": ["shop"]})
    assert put.status_code == 200 and put.json()["added"] == ["shop"]
    for pid in ("truck", "misc"):
        res = dana.put("/api/project-groups/retail-g/projects", json={"add": [pid]})
        assert (res.status_code, res.json()["code"]) == (404, "project.not_found"), pid
    res = dana.put("/api/project-groups/retail-g/members",
                   json={"upsert": [{"user_id": world.ola, "role_key": "developer"}]})
    assert res.status_code == 200
    assert dana.delete("/api/project-groups/retail-2").status_code == 200
    assert dana.post("/api/project-groups/retail-2/unarchive").status_code == 200
    for method, path in (("GET", ""), ("PATCH", ""), ("DELETE", ""), ("POST", "/unarchive"),
                         ("PUT", "/members"), ("PUT", "/projects")):
        res = dana.request(method, f"/api/project-groups/log-g{path}",
                           json={} if method in ("PATCH", "PUT") else None)
        assert (res.status_code, res.json()["code"]) == (404, "group.not_found"), path


# ── invitations and settings ──────────────────────────────────────────────────


def test_invitations_follow_the_departments_settings(world):
    dana = world.dana
    body = {"username": "newbie", "email": "newbie@acme-corp.com", "project_id": "shop"}
    off = dana.post("/api/departments/retail/invitations", json=body)
    assert (off.status_code, off.json()["code"]) == (403, "department.invitations_disabled")
    res = world.admin.patch("/api/departments/retail", json={
        "settings": {"can_create_users": True, "user_validity_max_days": 30}})
    assert res.status_code == 200
    assert res.json()["settings"] == {"can_create_users": True, "max_projects": None,
                                      "user_validity_max_days": 30}
    bad = world.admin.patch("/api/departments/retail", json={"settings": {"nope": 1}})
    assert (bad.status_code, bad.json()["code"]) == (400, "department.setting_invalid")
    long = dana.post("/api/departments/retail/invitations", json={**body, "valid_days": 60})
    assert (long.status_code, long.json()["code"]) == (422, "department.validity_too_long")
    other = dana.post("/api/departments/retail/invitations", json={**body, "project_id": "truck"})
    assert (other.status_code, other.json()["code"]) == (404, "project.not_found")
    elsewhere = dana.post("/api/departments/logistics/invitations", json=body)
    assert elsewhere.status_code == 403  # not their department: the route's own ADMIN
    made = dana.post("/api/departments/retail/invitations", json=body)
    assert made.status_code == 201, made.text
    assert made.json()["generated_password"]
    db = SessionLocal()
    try:
        user = db.get(User, made.json()["user_id"])
        assert user.role == "member"
        assert db.get(ProjectMember, ("shop", user.id)).role_key == "developer"
        assert access.reachable_workspace_ids(db, SimpleNamespace(
            user_id=user.id, role="member", permission_overrides=None)) == {WS}
    finally:
        db.close()
    assert _targets("department.invite") == [
        "department:retail user:newbie project:shop role:developer"]


def test_max_projects_caps_a_department_admins_creations(world):
    assert world.admin.patch("/api/departments/retail",
                             json={"settings": {"max_projects": 1}}).status_code == 200
    res = world.dana.post("/api/projects", json={"id": "shop2", "name": "s", "workspaces": [WS],
                                                 "department_id": "retail"})
    assert (res.status_code, res.json()["code"]) == (409, "department.project_limit")
    # the platform administrator is not capped
    _create(world.admin, "shop3", department_id="retail")


def test_an_archived_department_is_managed_by_nobody_but_the_platform(world):
    assert world.admin.post("/api/departments/retail/archive").status_code == 200
    assert world.dana.patch("/api/projects/shop", json={"name": "x"}).status_code == 404
    assert world.dana.get("/api/me/access").json()["managed_departments"] == []


def test_deleting_a_department_drops_its_admins_and_workspaces(world):
    assert world.admin.post("/api/departments", json={"id": "tmp", "name": "Tmp"}
                            ).status_code == 201
    assert world.admin.put("/api/departments/tmp/admins",
                           json={"add": [world.ola]}).status_code == 200
    assert world.admin.put("/api/departments/tmp/workspaces",
                           json={"add": [WS]}).status_code == 200
    assert world.admin.delete("/api/departments/tmp").status_code == 204
    db = SessionLocal()
    try:
        for table in ("department_admins", "department_workspaces"):
            assert not db.execute(
                text(f"SELECT 1 FROM {table} WHERE department_id = 'tmp'")).first()
    finally:
        db.close()


# ── the overlay's own drift checks, and the migration ─────────────────────────


def test_every_dadmin_route_is_classified_hub_global_and_admin_or_pperm():
    import app.main  # noqa: F401 — every router's block appended

    for key, where in DEPARTMENT_ADMIN_ROUTES.items():
        assert key in ROUTE_POLICY, key
        assert key in WORKSPACE_EXEMPT, key
        role = ROUTE_POLICY[key]
        assert role == ADMIN or role.startswith(PPERM_PREFIX), key
        assert where in ("project", "group", "department", "query", "any"), key
        if where in ("project", "group", "department"):
            assert "{" + f"{where}_id" + "}" in key[1], key


def test_the_settings_column_is_added_to_an_old_ledger(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE departments (id VARCHAR(32) PRIMARY KEY,"
                          " name VARCHAR(64))"))
    _migrate_department_settings(engine)
    _migrate_department_settings(engine)  # idempotent
    assert "settings" in {c["name"] for c in inspect(engine).get_columns("departments")}
