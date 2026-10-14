"""Network profiles (design projects §12.3, §12.10, §12.11): the spec's network union,
the admin CRUD and per-project allowed set, member visibility (another project's
profile reads as missing), the deploy-side helpers, and the startup migration that
turns inline networks into `legacy-<hash8>` profiles — idempotently."""

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.core import db as db_module
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, engine
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Agent
from app.models.network import NetworkProfile, ProjectNetworkProfile, legacy_profile_id
from app.schemas.agent import AgentSpec, NetworkProfileRef, VpcNetwork
from app.services import network_profiles as service
from tests.test_project_scope import (
    _login,
    _member_of,
    _project,
    _user,
    app,  # noqa: F401 — the fixture
)

SUBNETS = ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"]
SGS = ["sg-0ccccccccccccccc3"]
BODY = {"name": "core-vpc", "vpc_id": "vpc-0123456789abcdef0", "subnet_ids": SUBNETS,
        "security_group_ids": SGS}
S3_AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-abc/access-point/ap-1"
BYO = {"s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/data"}]}


def _spec(**fields):
    return AgentSpec(name="net-agent", method="container", system_prompt="p", **fields)


# ── the spec union (§12.3) ────────────────────────────────────────────────────


def test_the_spec_network_is_public_a_profile_or_legacy_inline():
    assert _spec().network is None
    ref = _spec(network={"profile_id": "np-0123456789ab"}).network
    assert isinstance(ref, NetworkProfileRef) and ref.profile_id == "np-0123456789ab"
    legacy = _spec(network={"subnets": SUBNETS, "security_groups": SGS}).network
    assert isinstance(legacy, VpcNetwork) and legacy.subnets == SUBNETS
    # a profile satisfies the BYO-mount requirement like inline subnets did
    assert _spec(filesystem=BYO, network={"profile_id": "legacy-1a2b3c4d"}).network


def test_the_spec_names_its_network_one_way_only():
    with pytest.raises(ValidationError, match="not both"):
        _spec(network={"profile_id": "np-0123456789ab", "subnets": SUBNETS,
                       "security_groups": SGS})
    with pytest.raises(ValidationError):
        _spec(network={"profile_id": "Bad Id!"})
    with pytest.raises(ValidationError, match="require VPC network"):
        _spec(filesystem=BYO)


def test_the_legacy_id_ignores_order_and_duplicates_but_not_the_workspace():
    a = legacy_profile_id("default", SUBNETS, SGS)
    assert a.startswith("legacy-") and len(a) == len("legacy-") + 8
    assert a == legacy_profile_id("default", [*reversed(SUBNETS), SUBNETS[0]], SGS)
    assert a != legacy_profile_id("other", SUBNETS, SGS)
    assert a != legacy_profile_id("default", SUBNETS[:1], SGS)


# ── admin CRUD (§12.11; the open console is the built-in admin) ──────────────


def test_admin_creates_lists_updates_and_archives(client):
    res = client.post("/api/network-profiles", json={**BODY, "internet": "nat",
                                                     "reachable": [{"cidr": "10.1.0.0/16",
                                                                    "port": 443}]})
    assert res.status_code == 201, res.text
    created = res.json()
    pid = created["id"]
    assert pid.startswith("np-") and created["status"] == "unchecked"
    assert created["reachable"] == [{"cidr": "10.1.0.0/16", "port": 443}]
    assert created["projects"] == [] and created["agents"] == 0

    dup = client.post("/api/network-profiles", json=BODY)
    assert dup.status_code == 409 and dup.json()["code"] == "network_profile.name_exists"

    listed = client.get("/api/network-profiles").json()["profiles"]
    assert [p["id"] for p in listed] == [pid]
    assert listed[0]["allowed"] is False  # not yet in the default project's set

    # a preflight result goes stale once the network itself changes
    db = SessionLocal()
    try:
        row = db.get(NetworkProfile, pid)
        row.status, row.check_results = "ok", [{"check": "subnets", "ok": True}]
        db.commit()
    finally:
        db.close()
    renamed = client.patch(f"/api/network-profiles/{pid}", json={"name": "core-vpc-2"}).json()
    assert renamed["name"] == "core-vpc-2" and renamed["status"] == "ok"
    moved = client.patch(f"/api/network-profiles/{pid}",
                         json={"security_group_ids": ["sg-0ddddddddddddddd4"]}).json()
    assert moved["status"] == "unchecked" and moved["check_results"] is None

    assert client.delete(f"/api/network-profiles/{pid}").status_code == 204
    assert client.get("/api/network-profiles").json()["profiles"] == []
    archived = client.get("/api/network-profiles?include_archived=true").json()["profiles"]
    assert archived[0]["archived_at"] is not None
    assert client.get(f"/api/network-profiles/{pid}").status_code == 404
    # the name is free again
    assert client.post("/api/network-profiles", json=BODY).status_code == 201


@pytest.mark.parametrize("bad", [
    {"subnet_ids": SUBNETS[:1]},  # AgentCore wants two AZs
    {"subnet_ids": ["subnet-1", "subnet-2"]},
    {"security_group_ids": []},
    {"vpc_id": "vpc-zz"},
    {"purpose": "everything"},
    {"reachable": [{"cidr": "10.0.0.0/8", "host": "db.internal", "port": 5432}]},
    {"reachable": [{"port": 5432}]},
])
def test_bad_profiles_are_refused(client, bad):
    res = client.post("/api/network-profiles", json={**BODY, **bad})
    assert res.status_code == 422, res.text


def test_a_profile_in_use_cannot_be_archived(client):
    pid = client.post("/api/network-profiles", json=BODY).json()["id"]
    agent_id = _agent_with_network({"profile_id": pid}, "default")
    res = client.delete(f"/api/network-profiles/{pid}")
    assert res.status_code == 409 and res.json()["code"] == "network_profile.in_use"
    assert client.get(f"/api/network-profiles/{pid}").json()["agents"] == 1
    db = SessionLocal()
    try:
        with project_context(None):
            db.get(Agent, agent_id).status = "deleted"
            db.commit()
    finally:
        db.close()
    assert client.delete(f"/api/network-profiles/{pid}").status_code == 204


# ── the per-project allowed set ───────────────────────────────────────────────


def test_admin_sets_a_projects_allowed_set_and_default(client):
    a = client.post("/api/network-profiles", json=BODY).json()["id"]
    b = client.post("/api/network-profiles", json={**BODY, "name": "other"}).json()["id"]
    res = client.put("/api/network-profiles/projects/default",
                     json={"profile_ids": [a, b], "default_profile_id": b})
    assert res.status_code == 200, res.text
    assert {p["id"] for p in res.json()["profiles"]} == {a, b}
    assert res.json()["default_profile_id"] == b
    listed = {p["id"]: p for p in client.get("/api/network-profiles").json()["profiles"]}
    assert listed[a]["allowed"] and not listed[a]["is_default"] and listed[b]["is_default"]
    assert listed[b]["projects"] == [{"project_id": "default", "is_default": True}]

    # replace semantics
    res = client.put("/api/network-profiles/projects/default", json={"profile_ids": [a]})
    assert [p["id"] for p in res.json()["profiles"]] == [a]
    assert res.json()["default_profile_id"] is None

    bad_default = client.put("/api/network-profiles/projects/default",
                             json={"profile_ids": [a], "default_profile_id": b})
    assert bad_default.status_code == 400
    unknown = client.put("/api/network-profiles/projects/default",
                         json={"profile_ids": ["np-000000000000"]})
    assert unknown.status_code == 404 and unknown.json()["code"] == "network_profile.not_found"
    nowhere = client.get("/api/network-profiles/projects/not-bound-here")
    assert nowhere.status_code == 404 and nowhere.json()["code"] == "project.not_found"

    # archiving drops the profile from every allowed set
    assert client.delete(f"/api/network-profiles/{a}").status_code == 204
    assert client.get("/api/network-profiles/projects/default").json()["profiles"] == []


def test_a_profile_of_another_workspace_reads_as_missing(client):
    pid = client.post("/api/network-profiles", json=BODY).json()["id"]
    db = SessionLocal()
    try:
        db.get(NetworkProfile, pid).workspace_id = "elsewhere"
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/network-profiles/{pid}").status_code == 404
    assert client.patch(f"/api/network-profiles/{pid}", json={"name": "x"}).status_code == 404
    assert client.get("/api/network-profiles").json()["profiles"] == []


# ── members: the project's allowed set only (§12.3, §5.4) ─────────────────────


def _admin_profile(name: str) -> str:
    db = SessionLocal()
    try:
        created = service.create_profile(db, DEFAULT_WORKSPACE_ID, {**BODY, "name": name},
                                         "operator")
        return created["id"]
    finally:
        db.close()


def _allow(project_id: str, *profile_ids: str, default: str | None = None) -> None:
    db = SessionLocal()
    try:
        service.set_project_profiles(db, DEFAULT_WORKSPACE_ID, project_id, list(profile_ids),
                                     default)
    finally:
        db.close()


def test_a_member_sees_only_the_projects_allowed_profiles(app):  # noqa: F811
    _project("alpha")
    mine = _admin_profile("alpha-net")
    theirs = _admin_profile("default-net")
    _allow("alpha", mine, default=mine)
    _allow("default", theirs)
    uid = _user("joan", grant=False)
    _member_of("alpha", uid)
    jo = _login(app, "joan")

    listed = jo.get("/api/network-profiles").json()["profiles"]
    assert [p["id"] for p in listed] == [mine]
    assert listed[0]["is_default"] is True
    assert "projects" not in listed[0] and "agents" not in listed[0]
    assert jo.get(f"/api/network-profiles/{mine}").status_code == 200
    hidden = jo.get(f"/api/network-profiles/{theirs}")
    assert hidden.status_code == 404 and hidden.json()["code"] == "network_profile.not_found"
    # the same answer as an id that does not exist
    assert jo.get("/api/network-profiles/np-000000000000").json()["code"] == (
        "network_profile.not_found"
    )

    # writes and the allowed set are administrators'
    assert jo.post("/api/network-profiles", json=BODY).status_code == 403
    assert jo.patch(f"/api/network-profiles/{mine}", json={"name": "x"}).status_code == 403
    assert jo.delete(f"/api/network-profiles/{mine}").status_code == 403
    assert jo.put("/api/network-profiles/projects/alpha",
                  json={"profile_ids": [theirs]}).status_code == 403


def test_a_member_with_no_allowed_profiles_sees_none(app):  # noqa: F811
    _admin_profile("unbound")
    uid = _user("pat")
    assert uid
    pat = _login(app, "pat")
    res = pat.get("/api/network-profiles")
    assert res.status_code == 200 and res.json()["profiles"] == []


# ── the deploy-side helpers ───────────────────────────────────────────────────


def test_usable_profile_needs_the_allowed_set_and_a_passing_preflight():
    _project("alpha")
    pid = _admin_profile("gate")
    _allow("default", pid)
    db = SessionLocal()
    try:
        assert service.usable_profile(db, DEFAULT_WORKSPACE_ID, "default", pid).id == pid
        with pytest.raises(NotFoundError):
            service.usable_profile(db, DEFAULT_WORKSPACE_ID, "alpha", pid)
        db.get(NetworkProfile, pid).status = "warn"
        db.commit()
        assert service.usable_profile(db, DEFAULT_WORKSPACE_ID, "default", pid)
        db.get(NetworkProfile, pid).status = "blocked"
        db.commit()
        with pytest.raises(AppError) as exc:
            service.usable_profile(db, DEFAULT_WORKSPACE_ID, "default", pid)
        assert exc.value.code == "network_profile.blocked"
    finally:
        db.close()


def test_resolve_vpc_turns_any_network_shape_into_network_mode_config():
    pid = _admin_profile("resolve")
    db = SessionLocal()
    try:
        expected = {"subnets": SUBNETS, "security_groups": SGS}
        assert service.resolve_vpc(db, DEFAULT_WORKSPACE_ID, None) is None
        assert service.resolve_vpc(db, DEFAULT_WORKSPACE_ID, {"profile_id": pid}) == expected
        assert service.resolve_vpc(
            db, DEFAULT_WORKSPACE_ID, NetworkProfileRef(profile_id=pid)) == expected
        assert service.resolve_vpc(
            db, DEFAULT_WORKSPACE_ID, VpcNetwork(subnets=SUBNETS, security_groups=SGS),
        ) == expected
        with pytest.raises(NotFoundError):
            service.resolve_vpc(db, "elsewhere", {"profile_id": pid})
    finally:
        db.close()


def test_inline_networks_are_admin_only_and_land_on_a_legacy_profile():
    _project("alpha")
    inline = {"name": "a", "network": {"subnets": SUBNETS, "security_groups": SGS}}
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            service.normalize_spec_network(db, DEFAULT_WORKSPACE_ID, "alpha", inline,
                                           is_admin=False, actor="joan")
        assert exc.value.code == "network.inline_not_allowed" and exc.value.status_code == 400
        out = service.normalize_spec_network(db, DEFAULT_WORKSPACE_ID, "alpha", inline,
                                             is_admin=True, actor="operator")
        pid = legacy_profile_id(DEFAULT_WORKSPACE_ID, SUBNETS, SGS)
        assert out["network"] == {"profile_id": pid}
        assert db.get(ProjectNetworkProfile, ("alpha", pid)) is not None
        # the same network again matches the same profile
        again = service.normalize_spec_network(db, DEFAULT_WORKSPACE_ID, "alpha", inline,
                                               is_admin=True, actor="operator")
        assert again["network"] == {"profile_id": pid}
        assert db.scalar(text("SELECT COUNT(*) FROM network_profiles")) == 1
        # a stored-form spec passes through when allowed; another project's 404s
        assert service.normalize_spec_network(
            db, DEFAULT_WORKSPACE_ID, "alpha", out, is_admin=False, actor="joan") == out
        with pytest.raises(NotFoundError):
            service.normalize_spec_network(db, DEFAULT_WORKSPACE_ID, "default", out,
                                           is_admin=False, actor="joan")
        public = {"name": "a", "network": None}
        assert service.normalize_spec_network(
            db, DEFAULT_WORKSPACE_ID, "alpha", public, is_admin=False, actor="joan") == public
    finally:
        db.close()


# ── the startup migration (§12.10) ────────────────────────────────────────────


def _agent_with_network(network, project_id, *, name="byo-agent", workspace=DEFAULT_WORKSPACE_ID):
    db = SessionLocal()
    try:
        with project_context(project_id):
            row = Agent(workspace_id=workspace, name=name, method="container", status="active",
                        spec={"name": name, "method": "container", "network": network})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _spec_of(agent_id):
    db = SessionLocal()
    try:
        with project_context(None):
            return db.get(Agent, agent_id).spec
    finally:
        db.close()


def test_startup_moves_inline_networks_onto_legacy_profiles_once():
    _project("alpha")
    inline = {"subnets": SUBNETS, "security_groups": SGS}
    a = _agent_with_network(inline, "alpha", name="byo-a")
    # same network in another order: the same profile, allowed in both projects
    b = _agent_with_network({"subnets": list(reversed(SUBNETS)), "security_groups": SGS},
                            "default", name="byo-b")
    other = _agent_with_network({"subnets": SUBNETS[:1], "security_groups": SGS}, "default",
                                name="byo-c")
    public = _agent_with_network(None, "default", name="public")

    counts = db_module.convert_inline_networks(engine)
    assert counts == {"agents": 3, "profiles": 2, "bindings": 3}

    pid = legacy_profile_id(DEFAULT_WORKSPACE_ID, SUBNETS, SGS)
    assert _spec_of(a)["network"] == {"profile_id": pid}
    assert _spec_of(b)["network"] == {"profile_id": pid}
    assert _spec_of(other)["network"]["profile_id"].startswith("legacy-")
    assert _spec_of(other)["network"]["profile_id"] != pid
    assert _spec_of(public)["network"] is None
    assert _spec_of(a)["name"] == "byo-a"  # the rest of the spec is untouched

    db = SessionLocal()
    try:
        row = db.get(NetworkProfile, pid)
        assert row.workspace_id == DEFAULT_WORKSPACE_ID and row.status == "unchecked"
        assert row.subnet_ids == SUBNETS and row.security_group_ids == SGS
        assert row.vpc_id is None and row.name == pid
        bound = set(db.scalars(select(ProjectNetworkProfile.project_id)
                               .where(ProjectNetworkProfile.profile_id == pid)))
        assert bound == {"alpha", "default"}
        # deploy behaviour is unchanged: the profile resolves to the old subnets / SGs
        assert service.resolve_vpc(db, DEFAULT_WORKSPACE_ID, _spec_of(a)["network"]) == {
            "subnets": SUBNETS, "security_groups": SGS,
        }
    finally:
        db.close()
    # the rewritten spec still parses
    assert AgentSpec(**{**_spec_of(a), "system_prompt": "p"}).network.profile_id == pid

    # idempotent
    assert db_module.convert_inline_networks(engine) == {"agents": 0, "profiles": 0,
                                                         "bindings": 0}
    # an older build wrote another inline spec after a rollback: converted next startup
    late = _agent_with_network(inline, "alpha", name="byo-late")
    assert db_module.convert_inline_networks(engine) == {"agents": 1, "profiles": 0,
                                                         "bindings": 0}
    assert _spec_of(late)["network"] == {"profile_id": pid}


def test_init_db_runs_the_conversion(client):
    _agent_with_network({"subnets": SUBNETS, "security_groups": SGS}, "default")
    db_module.init_db()
    db = SessionLocal()
    try:
        with project_context(None):
            spec = db.scalars(select(Agent.spec)).one()
    finally:
        db.close()
    assert spec["network"] == {"profile_id": legacy_profile_id(DEFAULT_WORKSPACE_ID,
                                                               SUBNETS, SGS)}
