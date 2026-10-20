"""Combined duties in a project (projects design §3.6, D-22, P1F-sod): the member
views flag one person holding both sides of an SoD pair, `settings.sod_strict`
refuses such assignments (409 `project.sod_conflict`) on the project's members, its
custom roles, and when strictness is switched on;
the refusal leaves nothing behind. Owners still cannot change their own membership."""

from types import SimpleNamespace

import pytest

from app.core.db import SessionLocal
from app.models.projects import ProjectMember
from app.services import projects as projects_service
from tests.test_project_scope import (  # noqa: F401
    _login,
    _member_of,
    _user,
    app,
)
from tests.test_projects_api import _admin, _create


def _row(model, key):
    db = SessionLocal()
    try:
        return db.get(model, key)
    finally:
        db.close()


@pytest.fixture
def world(app):  # noqa: F811
    """alpha (lenient) and strict (sod_strict), olga owning both; zed a developer,
    rita a release operator."""
    admin = _admin(app)
    olga, zed, rita = (_user(n, grant=False) for n in ("olga", "zed", "rita"))
    _create(admin, "alpha", owners=[olga])
    _create(admin, "strict", owners=[olga], settings={"sod_strict": True})
    return SimpleNamespace(admin=admin, olga=olga, zed=zed, rita=rita, app=app)


def _put(client, pid, user_id, role_key, overrides=None):
    return client.put(f"/api/projects/{pid}/members", json={
        "upsert": [{"user_id": user_id, "role_key": role_key, "overrides": overrides}]})


def _member(client, pid, user_id):
    rows = client.get(f"/api/projects/{pid}/members").json()["members"]
    return next(m for m in rows if m["user_id"] == user_id)


def test_the_pairs():
    assert projects_service.sod_conflicts(frozenset({"agents.deploy", "release.sign"})) == [
        ["agents.deploy", "release.sign"]]
    assert projects_service.sod_conflicts(
        frozenset({"criteria.manage", "criteria.sign", "agents.deploy"})
    ) == [["criteria.manage", "criteria.sign"]]
    assert projects_service.sod_conflicts(frozenset({"agents.deploy"})) == []


def test_a_lenient_project_flags_but_allows(world):
    olga = _login(world.app, "olga")
    res = _put(olga, "alpha", world.zed, "developer", {"release.sign": True})
    assert res.status_code == 200, res.text
    assert res.json()["sod_strict"] is False
    zed = next(m for m in res.json()["members"] if m["user_id"] == world.zed)
    assert zed["sod_conflicts"] == [["agents.deploy", "release.sign"]]
    owner = _member(olga, "alpha", world.olga)
    assert owner["sod_conflicts"] == []  # the owner template combines nothing


def test_a_strict_project_refuses_a_combined_direct_role(world):
    olga = _login(world.app, "olga")
    res = _put(olga, "strict", world.zed, "developer", {"release.sign": True})
    assert res.status_code == 409 and res.json()["code"] == "project.sod_conflict"
    conflict = res.json()["detail"]["conflicts"][0]
    assert conflict["username"] == "zed"
    assert conflict["pairs"] == [["agents.deploy", "release.sign"]]
    assert _row(ProjectMember, ("strict", world.zed)) is None  # nothing left behind
    # biz_owner manages and signs criteria — strict needs the manage side taken away
    res = _put(olga, "strict", world.zed, "biz_owner")
    assert res.status_code == 409
    res = _put(olga, "strict", world.zed, "biz_owner", {"criteria.manage": False})
    assert res.status_code == 200 and res.json()["sod_strict"] is True
    assert _member(olga, "strict", world.zed)["sod_conflicts"] == []


def test_switching_strict_on_is_refused_while_a_combination_exists(world):
    olga = _login(world.app, "olga")
    _put(olga, "alpha", world.zed, "developer", {"release.sign": True})
    res = world.admin.patch("/api/projects/alpha", json={"settings": {"sod_strict": True}})
    assert res.status_code == 409 and res.json()["code"] == "project.sod_conflict"
    assert world.admin.get("/api/projects/alpha").json()["settings"] == {}
    _put(olga, "alpha", world.zed, "developer")
    res = world.admin.patch("/api/projects/alpha", json={"settings": {"sod_strict": True}})
    assert res.status_code == 200 and res.json()["settings"] == {"sod_strict": True}
    # owners cannot loosen it (design §6.3)
    res = olga.patch("/api/projects/alpha", json={"settings": {"sod_strict": False}})
    assert res.status_code == 403


def test_a_custom_role_cannot_combine_duties_in_a_strict_project(world):
    olga = _login(world.app, "olga")
    res = olga.put("/api/projects/strict/roles/shipper",
                   json={"name": "Shipper", "permissions": ["project.use", "agents.deploy"]})
    assert res.status_code == 200
    assert _put(olga, "strict", world.zed, "shipper").status_code == 200
    res = olga.put("/api/projects/strict/roles/shipper", json={
        "name": "Shipper", "permissions": ["project.use", "agents.deploy", "release.sign"]})
    assert res.status_code == 409 and res.json()["code"] == "project.sod_conflict"
    keys = next(r for r in olga.get("/api/projects/strict/roles").json()["custom"]
                if r["key"] == "shipper")["permissions"]
    assert "release.sign" not in keys


def test_an_owner_still_cannot_change_their_own_membership(world):
    olga = _login(world.app, "olga")
    res = _put(olga, "alpha", world.olga, "project_owner", {"release.sign": True})
    assert res.status_code == 403 and res.json()["code"] == "project.self_membership"
    assert world.admin.put("/api/projects/alpha/members", json={
        "upsert": [{"user_id": world.olga, "role_key": "project_owner"}]}).status_code == 200
