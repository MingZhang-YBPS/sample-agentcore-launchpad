"""Project groups and departments API (projects design §7.7, §7.10, P1F-group): group
CRUD and archive, group members (built-in templates only), putting projects into a
group (0..1), the epoch and API-key effects of every access write, the structure
journal, departments CRUD / archive / in-use delete, and who may call any of it."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.models.ledger import ApiKey, AuditEvent
from app.models.projects import Project
from tests.test_project_scope import (  # noqa: F401
    _login,
    _member_of,
    _project,
    _user,
    app,
)
from tests.test_projects_api import _admin, _create, _epoch

WS = DEFAULT_WORKSPACE_ID


def _actions() -> set[str]:
    db = SessionLocal()
    try:
        return set(db.scalars(select(AuditEvent.action)))
    finally:
        db.close()


def _group_of(pid: str) -> str | None:
    db = SessionLocal()
    try:
        return db.get(Project, pid).group_id
    finally:
        db.close()


def _key(project_id: str, created_by: str, prefix: str) -> str:
    db = SessionLocal()
    try:
        with project_context(project_id):
            key = ApiKey(workspace_id=WS, name="k", prefix=prefix, key_hash=prefix * 16,
                         created_by=created_by)
            db.add(key)
            db.commit()
            return key.id
    finally:
        db.close()


def _key_enabled(key_id: str) -> bool:
    db = SessionLocal()
    try:
        with project_context(None):
            return bool(db.get(ApiKey, key_id).enabled)
    finally:
        db.close()


@pytest.fixture
def world(app):  # noqa: F811
    """alpha and beta, a group `risk` holding alpha, gina a member of the group."""
    admin = _admin(app)
    gina, dora = _user("gina", grant=False), _user("dora", grant=False)
    _create(admin, "alpha")
    _create(admin, "beta")
    res = admin.post("/api/project-groups", json={"id": "risk", "name": "Risk platform"})
    assert res.status_code == 201, res.text
    assert admin.put("/api/project-groups/risk/projects",
                     json={"add": ["alpha"]}).status_code == 200
    res = admin.put("/api/project-groups/risk/members",
                    json={"upsert": [{"user_id": gina, "role_key": "developer"}]})
    assert res.status_code == 200, res.text
    return SimpleNamespace(admin=admin, gina=gina, dora=dora, app=app)


def _my_projects(client) -> set[str]:
    return {p["id"] for p in client.get("/api/projects").json()["projects"]}


# ── groups ────────────────────────────────────────────────────────────────────


def test_group_crud_validates_and_journals(app):  # noqa: F811
    admin = _admin(app)
    bad = admin.post("/api/project-groups", json={"id": "Bad_Id", "name": "x"})
    assert bad.status_code == 400 and bad.json()["code"] == "group.invalid_id"
    missing = admin.post("/api/project-groups", json={"id": "g1", "name": "x",
                                                      "department_id": "nowhere"})
    assert missing.status_code == 404 and missing.json()["code"] == "department.not_found"
    assert admin.post("/api/departments", json={"id": "retail", "name": "Retail"}
                      ).status_code == 201
    made = admin.post("/api/project-groups", json={"id": "g1", "name": "G1",
                                                   "department_id": "retail"})
    assert made.status_code == 201 and made.json()["department_id"] == "retail"
    dup = admin.post("/api/project-groups", json={"id": "g1", "name": "again"})
    assert dup.status_code == 409 and dup.json()["code"] == "group.exists"
    epoch = _epoch()
    patched = admin.patch("/api/project-groups/g1", json={"name": "G one",
                                                          "department_id": None})
    assert patched.status_code == 200
    assert patched.json()["name"] == "G one" and patched.json()["department_id"] is None
    assert _epoch() == epoch  # names and departments do not move access
    assert admin.get("/api/project-groups/nope").status_code == 404
    listed = admin.get("/api/project-groups").json()["groups"]
    assert [g["id"] for g in listed] == ["g1"]
    assert {"group.create", "group.update", "department.create"} <= _actions()


def test_group_members_reach_every_project_of_the_group(world):
    gina = _login(world.app, "gina")
    assert _my_projects(gina) == {"alpha"}
    perms = gina.get("/api/projects/alpha").json()["my_permissions"]
    assert "agents.deploy" in perms  # developer, via the group
    assert gina.get("/api/projects/beta").status_code == 404
    # beta joins the group → gina's next request sees it (epoch moved)
    epoch = _epoch()
    res = world.admin.put("/api/project-groups/risk/projects", json={"add": ["beta"]})
    assert res.status_code == 200 and res.json()["added"] == ["beta"]
    assert _epoch() > epoch
    assert _my_projects(gina) == {"alpha", "beta"}
    members = gina.get("/api/projects/beta/members").json()["members"]
    assert members[0]["sources"] == [{"source": "group:risk", "role_key": "developer"}]
    detail = world.admin.get("/api/project-groups/risk").json()
    assert {p["id"] for p in detail["projects"]} == {"alpha", "beta"}
    assert detail["members"][0]["username"] == "gina" and detail["member_count"] == 1
    assert {"group.projects.add", "group.members.add"} <= _actions()


def test_group_members_take_built_in_templates_only(world):
    world.admin.put("/api/projects/alpha/roles/outsourced",
                    json={"name": "Outsourced", "permissions": ["project.use"]})
    for role_key in ("outsourced", "inherit", "nope"):
        res = world.admin.put("/api/project-groups/risk/members",
                              json={"upsert": [{"user_id": world.dora, "role_key": role_key}]})
        assert res.status_code == 400 and res.json()["code"] == "group.role_invalid", role_key
    res = world.admin.put("/api/project-groups/risk/members",
                          json={"upsert": [{"user_id": "ghost", "role_key": "viewer"}]})
    assert res.status_code == 404
    res = world.admin.put("/api/project-groups/risk/members", json={"remove": [world.dora]})
    assert res.status_code == 404 and res.json()["code"] == "group.member_not_found"


def test_member_update_and_removal_move_access_and_journal(world):
    epoch = _epoch()
    res = world.admin.put("/api/project-groups/risk/members",
                          json={"upsert": [{"user_id": world.gina, "role_key": "viewer"}]})
    assert res.json()["updated"] == [world.gina] and _epoch() > epoch
    gina = _login(world.app, "gina")
    assert gina.get("/api/projects/alpha").json()["my_permissions"] == []
    res = world.admin.put("/api/project-groups/risk/members", json={"remove": [world.gina]})
    assert res.json()["removed"] == [world.gina]
    assert gina.get("/api/projects/alpha").status_code == 404
    assert {"group.members.update", "group.members.remove"} <= _actions()


def test_a_project_is_in_at_most_one_group(world):
    world.admin.post("/api/project-groups", json={"id": "fraud", "name": "Fraud"})
    res = world.admin.put("/api/project-groups/fraud/projects", json={"add": ["alpha"]})
    assert res.status_code == 409 and res.json()["code"] == "group.project_in_other_group"
    res = world.admin.put("/api/project-groups/fraud/projects", json={"remove": ["alpha"]})
    assert res.status_code == 404 and res.json()["code"] == "group.project_not_found"
    res = world.admin.put("/api/project-groups/fraud/projects", json={"add": ["no-such"]})
    assert res.status_code == 404 and res.json()["code"] == "project.not_found"
    # leaving one group, then joining another
    world.admin.put("/api/project-groups/risk/projects", json={"remove": ["alpha"]})
    res = world.admin.put("/api/project-groups/fraud/projects", json={"add": ["alpha"]})
    assert res.status_code == 200 and _group_of("alpha") == "fraud"


def test_removing_a_project_revokes_group_access_only(world):
    """Direct members keep theirs; the group's lose it, and a key whose creator lost
    the project is disabled in the same transaction (§5.7)."""
    _member_of("alpha", world.dora, "developer")
    gina_key = _key("alpha", world.gina, "lp_g")
    dora_key = _key("alpha", world.dora, "lp_d")
    epoch = _epoch()
    res = world.admin.put("/api/project-groups/risk/projects", json={"remove": ["alpha"]})
    assert res.status_code == 200 and res.json()["removed"] == ["alpha"]
    assert _epoch() > epoch and _group_of("alpha") is None
    assert _login(world.app, "gina").get("/api/projects/alpha").status_code == 404
    assert _login(world.app, "dora").get("/api/projects/alpha").status_code == 200
    assert not _key_enabled(gina_key) and _key_enabled(dora_key)
    assert "group.projects.remove" in _actions()


def test_removing_a_member_disables_their_keys(world):
    key = _key("alpha", world.gina, "lp_m")
    world.admin.put("/api/project-groups/risk/members", json={"remove": [world.gina]})
    assert not _key_enabled(key)


def test_archive_revokes_and_unarchive_restores(world):
    key = _key("alpha", world.gina, "lp_a")
    epoch = _epoch()
    res = world.admin.delete("/api/project-groups/risk")
    assert res.status_code == 200, res.text
    assert res.json()["archived"] and res.json()["keys_disabled"] == 1
    assert _epoch() > epoch
    assert _group_of("alpha") == "risk"  # kept for an unarchive
    gina = _login(world.app, "gina")
    assert gina.get("/api/projects/alpha").status_code == 404
    assert not _key_enabled(key)
    # archived: no new members or projects, not in the default list, not assignable
    res = world.admin.put("/api/project-groups/risk/members",
                          json={"upsert": [{"user_id": world.dora, "role_key": "viewer"}]})
    assert res.status_code == 409 and res.json()["code"] == "group.archived"
    res = world.admin.put("/api/project-groups/risk/projects", json={"add": ["beta"]})
    assert res.status_code == 409
    assert world.admin.get("/api/project-groups").json()["groups"] == []
    assert [g["id"] for g in world.admin.get(
        "/api/project-groups?status=archived").json()["groups"]] == ["risk"]
    assert world.admin.patch("/api/projects/beta",
                             json={"group_id": "risk"}).status_code == 404
    res = world.admin.post("/api/project-groups/risk/unarchive")
    assert res.status_code == 200 and not res.json()["archived"]
    assert gina.get("/api/projects/alpha").status_code == 200
    assert "group.archive" in _actions()


def test_groups_are_administrators_only(world):
    _member_of("alpha", world.dora, "project_owner")
    dora = _login(world.app, "dora")
    assert dora.get("/api/project-groups").status_code == 403
    assert dora.get("/api/project-groups/risk").status_code == 403
    assert dora.post("/api/project-groups", json={"id": "x1", "name": "x"}).status_code == 403
    assert dora.put("/api/project-groups/risk/members",
                    json={"upsert": [{"user_id": world.dora, "role_key": "release_ops"}]}
                    ).status_code == 403
    assert dora.put("/api/project-groups/risk/projects",
                    json={"add": ["beta"]}).status_code == 403
    assert dora.delete("/api/project-groups/risk").status_code == 403


def test_the_routes_need_no_workspace_or_project(world):
    """Hub-global: an unknown X-Workspace / X-Project changes nothing."""
    res = world.admin.get("/api/project-groups",
                          headers={"X-Workspace": "nowhere", "X-Project": "nothing"})
    assert res.status_code == 200
    res = world.admin.get("/api/departments", headers={"X-Workspace": "nowhere"})
    assert res.status_code == 200


# ── departments ───────────────────────────────────────────────────────────────


def test_department_crud_archive_and_in_use_delete(world):
    admin = world.admin
    assert admin.post("/api/departments", json={"id": "Bad_Id", "name": "x"}
                      ).json()["code"] == "department.invalid_id"
    made = admin.post("/api/departments", json={"id": "retail", "name": "Retail",
                                                "description": "banking"})
    assert made.status_code == 201 and made.json()["project_count"] == 0
    assert admin.post("/api/departments", json={"id": "retail", "name": "x"}
                      ).status_code == 409
    epoch = _epoch()
    assert admin.patch("/api/departments/retail", json={"name": "Retail banking"}
                       ).json()["name"] == "Retail banking"
    assert admin.patch("/api/projects/alpha", json={"department_id": "retail"}
                       ).status_code == 200
    assert admin.patch("/api/project-groups/risk", json={"department_id": "retail"}
                       ).status_code == 200
    listed = admin.get("/api/departments").json()["departments"]
    assert listed[0]["project_count"] == 1 and listed[0]["group_count"] == 1
    assert _epoch() == epoch  # a department grants no access
    res = admin.delete("/api/departments/retail")
    assert res.status_code == 409 and res.json()["code"] == "department.in_use"
    assert res.json()["detail"] == {"department_id": "retail", "projects": 1, "groups": 1}
    # archived: kept where it is named, refused for new assignments
    assert admin.post("/api/departments/retail/archive").json()["archived"]
    res = admin.patch("/api/projects/beta", json={"department_id": "retail"})
    assert res.status_code == 409 and res.json()["code"] == "department.archived"
    assert admin.get("/api/departments").json()["departments"] == []
    assert len(admin.get("/api/departments?status=all").json()["departments"]) == 1
    assert not admin.post("/api/departments/retail/unarchive").json()["archived"]
    admin.patch("/api/projects/alpha", json={"department_id": None})
    admin.patch("/api/project-groups/risk", json={"department_id": None})
    assert admin.delete("/api/departments/retail").status_code == 204
    assert admin.patch("/api/departments/retail", json={"name": "x"}).status_code == 404
    assert {"department.create", "department.update", "department.archive",
            "department.unarchive", "department.delete"} <= _actions()


def test_departments_list_for_members_writes_for_administrators(world):
    world.admin.post("/api/departments", json={"id": "retail", "name": "Retail"})
    gina = _login(world.app, "gina")
    assert [d["id"] for d in gina.get("/api/departments").json()["departments"]] == ["retail"]
    assert gina.post("/api/departments", json={"id": "x1", "name": "x"}).status_code == 403
    assert gina.patch("/api/departments/retail", json={"name": "x"}).status_code == 403
    assert gina.post("/api/departments/retail/archive").status_code == 403
    assert gina.delete("/api/departments/retail").status_code == 403
    assert world.admin.get("/api/departments?status=bogus").status_code == 400
