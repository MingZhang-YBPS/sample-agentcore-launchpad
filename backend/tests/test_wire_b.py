"""WIRE-B: the cross-file follow-ups of the B8 nodes — the journal vocabulary
(`project.quotas.update`, `system.update.*`, `steward.*`) and what a steward reads of
it; department quota caps; quota checks on a key re-enable, a discovery import and an
evaluation estimate; the steward's group-membership trace and two-person rule, its
workspace reach and its structure-only views of the audit trail and the inbox; and
the route-policy entries appended for R10-kb / R10-gw."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.core.route_policy import (
    ADMIN,
    ADMIN_DATA,
    INFRA_LOWERED_WHEN_ISOLATED,
    ROUTE_POLICY,
    admin_kind,
)
from app.models.ledger import Agent, AuditEvent
from app.models.projects import GroupMember, Project
from app.services import audit, project_quotas
from app.services import users as users_service
from app.services.audit import record_audit_event, record_structure_event
from tests.test_project_scope import _login, _user, app  # noqa: F401
from tests.test_projects_api import _admin, _create

WS = DEFAULT_WORKSPACE_ID
STEWARD = users_service.ROLE_STEWARD


def _targets(action: str) -> list[str]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(AuditEvent.target).where(AuditEvent.action == action)
                               .order_by(AuditEvent.created_at)))
    finally:
        db.close()


def _set_quotas(pid: str, quotas: dict | None) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(Project, pid)
            row.settings = {"quotas": quotas} if quotas else None
            db.commit()
    finally:
        db.close()


# ── the journal's vocabulary ──────────────────────────────────────────────────


@pytest.mark.parametrize("action", [
    "project.quotas.update", "system.update.prepare", "system.update.schedule",
    "steward.members.change", "steward.pending.request", "steward.two_person",
])
def test_the_b8_actions_are_structure_events(action):
    assert audit.is_structure_action(action)
    assert audit.steward_visible(action)


@pytest.mark.parametrize("action", ["steward.", "system.update.", "release.sign",
                                    "storage.write_confirmed", "agent.delete"])
def test_data_events_stay_hidden_from_a_steward(action):
    assert not audit.steward_visible(action)


def test_steward_rows_name_their_project_in_its_audit_view(app):  # noqa: F811
    _create(_admin(app), "shop")
    record_structure_event(actor="sam", action="steward.members.change",
                           target="project:shop upsert:pat:viewer")
    db = SessionLocal()
    try:
        rows, _ = audit.project_events(db, "shop", audit.AuditFilter(action="steward."),
                                       limit=10)
    finally:
        db.close()
    assert [r.target for r in rows] == ["project:shop upsert:pat:viewer"]
    assert rows[0].workspace_id == WS and rows[0].project_id is None


# ── quotas ────────────────────────────────────────────────────────────────────


@pytest.fixture
def quota_world(app):  # noqa: F811
    admin = _admin(app)
    assert admin.post("/api/departments", json={"id": "retail", "name": "Retail"}
                      ).status_code == 201
    _create(admin, "alpha", department_id="retail")
    return SimpleNamespace(admin=admin, app=app)


def test_department_quota_caps_merge_and_bound_the_project(quota_world):
    admin = quota_world.admin
    res = admin.patch("/api/departments/retail", json={"settings": {
        "quotas": {"max_agents": 5, "max_api_keys": 3}}})
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["quotas"] == {"max_agents": 5, "max_api_keys": 3}
    # one level deep: setting one cap keeps the other, a null clears one
    res = admin.patch("/api/departments/retail", json={"settings": {
        "quotas": {"max_agents": 2, "max_api_keys": None}}})
    assert res.json()["settings"]["quotas"] == {"max_agents": 2}
    assert res.json()["settings"]["can_create_users"] is False
    db = SessionLocal()
    try:
        assert project_quotas.limit_of(db, "alpha", "max_agents") == 2
    finally:
        db.close()
    res = admin.put("/api/projects/alpha/quotas", json={"quotas": {"max_agents": 3}})
    assert (res.status_code, res.json()["code"]) == (422, "project.quota_above_department")
    assert admin.put("/api/projects/alpha/quotas",
                     json={"quotas": {"max_agents": 1}}).status_code == 200
    assert _targets("project.quotas.update") == ["project:alpha quotas:max_agents"]
    # the whole quotas object cleared
    res = admin.patch("/api/departments/retail", json={"settings": {"quotas": None}})
    assert res.json()["settings"]["quotas"] is None


@pytest.mark.parametrize("quotas", [{"max_widgets": 1}, {"max_agents": -1}, "3"])
def test_a_bad_department_quota_is_a_department_error(quota_world, quotas):
    res = quota_world.admin.patch("/api/departments/retail",
                                  json={"settings": {"quotas": quotas}})
    assert (res.status_code, res.json()["code"]) == (400, "department.setting_invalid")
    assert res.json()["detail"]["key"] == "quotas"


def test_re_enabling_a_key_takes_a_slot(quota_world):
    admin, alpha = quota_world.admin, {"X-Project": "alpha"}
    _set_quotas("alpha", {"max_api_keys": 1})
    first = admin.post("/api/apikeys", headers=alpha, json={"name": "k1"}).json()["id"]
    assert admin.post(f"/api/apikeys/{first}/disable", headers=alpha).status_code == 200
    assert admin.post("/api/apikeys", headers=alpha, json={"name": "k2"}).status_code == 201
    res = admin.post(f"/api/apikeys/{first}/enable", headers=alpha)
    assert (res.status_code, res.json()["code"]) == (409, "project.quota_exceeded")
    assert res.json()["detail"]["quota"] == "max_api_keys"
    # enabling an enabled key is not a new slot
    second = next(k["id"] for k in admin.get("/api/apikeys", headers=alpha).json()["keys"]
                  if k["name"] == "k2")
    assert admin.post(f"/api/apikeys/{second}/enable", headers=alpha).status_code == 200


def _fake_import(created: list[str]):
    def import_runtimes(control, db, runtime_ids, *, workspace_id):
        out = {"imported": [], "updated": [], "already_managed": [], "failed": []}
        for rid in runtime_ids:
            row = Agent(workspace_id=workspace_id, name=f"imp-{rid}", method="zip_runtime",
                        status="active", spec={"name": rid})
            db.add(row)
            db.flush()
            created.append(row.id)
            out["imported"].append({"id": row.id, "name": row.name})
        return out
    return import_runtimes


def test_a_discovery_import_past_max_agents_is_rolled_back(quota_world, monkeypatch):
    from app.routers import agents as agents_router

    created: list[str] = []
    monkeypatch.setattr(agents_router, "import_runtimes", _fake_import(created))
    monkeypatch.setattr(agents_router, "import_harnesses", lambda *a, **k: {})
    monkeypatch.setattr(agents_router, "control_client", lambda ctx: None)
    _set_quotas("alpha", {"max_agents": 1})
    alpha = {"X-Project": "alpha"}
    res = quota_world.admin.post("/api/agents/discovery/import", headers=alpha,
                                 json={"runtime_ids": ["r1", "r2"]})
    assert (res.status_code, res.json()["code"]) == (409, "project.quota_exceeded")
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.scalar(select(Agent.id).where(Agent.id.in_(created))) is None
    finally:
        db.close()
    res = quota_world.admin.post("/api/agents/discovery/import", headers=alpha,
                                 json={"runtime_ids": ["r3"]})
    assert res.status_code == 200, res.text
    assert len(res.json()["imported"]) == 1


def test_the_estimate_limit_is_the_agents_projects(quota_world):
    from app.dlc import cost as cost_svc

    _set_quotas("alpha", {"eval_run_max_usd": 4})
    db = SessionLocal()
    try:
        workspace = SimpleNamespace(release_policy={"eval_cost_max_usd": 10})
        estimate = cost_svc.estimate(
            db, agent=SimpleNamespace(project_id="alpha", spec={}), workspace=workspace,
            workspace_ctx=None, items=[], evaluators=[])
        assert estimate["max_usd"] == 4
        estimate = cost_svc.estimate(db, agent=None, workspace=workspace, workspace_ctx=None,
                                     items=[], evaluators=[])
        assert estimate["max_usd"] == 10
    finally:
        db.close()


# ── the steward ───────────────────────────────────────────────────────────────


@pytest.fixture
def world(app):  # noqa: F811
    """`shop` (owned by olivia) and `cafe` (owned by otto) in group `retail`; sam and
    sue are stewards; pat a plain member with no project."""
    admin = _admin(app)
    olivia, otto = _user("olivia", grant=False), _user("otto", grant=False)
    pat = _user("pat", grant=False)
    sam = _user("sam", role=STEWARD, grant=False)
    sue = _user("sue", role=STEWARD, grant=False)
    _create(admin, "shop", owners=[olivia])
    _create(admin, "cafe", owners=[otto])
    assert admin.post("/api/project-groups", json={"id": "retail", "name": "Retail"}
                      ).status_code == 201
    assert admin.put("/api/project-groups/retail/projects",
                     json={"add": ["shop", "cafe"]}).status_code == 200
    return SimpleNamespace(
        app=app, admin=admin, olivia_id=olivia, otto_id=otto, pat_id=pat, sam_id=sam,
        sue_id=sue, sam=_login(app, "sam"), olivia=_login(app, "olivia"),
        otto=_login(app, "otto"),
    )


def _group_members(group_id: str) -> set[str]:
    db = SessionLocal()
    try:
        return set(db.scalars(select(GroupMember.user_id)
                              .where(GroupMember.group_id == group_id)))
    finally:
        db.close()


def test_no_steward_joins_a_group(world):
    res = world.sam.put("/api/project-groups/retail/members",
                        json={"upsert": [{"user_id": world.sam_id, "role_key": "viewer"}]})
    assert (res.status_code, res.json()["code"]) == (403, "project.self_membership")
    res = world.admin.put("/api/project-groups/retail/members",
                          json={"upsert": [{"user_id": world.sue_id, "role_key": "viewer"}]})
    assert (res.status_code, res.json()["code"]) == (409, "project.steward_member")
    assert _group_members("retail") == set()


def test_a_steward_s_group_change_is_noticed_in_every_project(world):
    res = world.sam.put("/api/project-groups/retail/members",
                        json={"upsert": [{"user_id": world.pat_id, "role_key": "developer"}]})
    assert res.status_code == 200, res.text
    assert res.json()["added"] == [world.pat_id]
    assert sorted(_targets("steward.members.change")) == [
        "project:cafe group:retail upsert:pat:developer",
        "project:shop group:retail upsert:pat:developer",
    ]
    for owner, pid in ((world.olivia, "shop"), (world.otto, "cafe")):
        (notice,) = owner.get("/api/steward/notices").json()["items"]
        assert notice["project_id"] == pid and notice["actor"] == "sam"
    # an administrator's change leaves only the ordinary journal
    world.admin.put("/api/project-groups/retail/members", json={"remove": [world.pat_id]})
    assert len(_targets("steward.members.change")) == 2


def test_moving_a_project_into_a_group_leaves_the_same_trace(world):
    world.admin.put("/api/project-groups/retail/members",
                    json={"upsert": [{"user_id": world.pat_id, "role_key": "viewer"}]})
    _create(world.admin, "deli", owners=[world.otto_id])
    res = world.sam.put("/api/project-groups/retail/projects", json={"add": ["deli"]})
    assert res.status_code == 200, res.text
    assert _targets("steward.members.change") == ["project:deli group:retail upsert:pat:viewer"]


def test_two_person_stages_a_group_change_until_every_owner_or_a_steward_confirms(world):
    assert world.admin.put("/api/steward/settings",
                           json={"steward_two_person": True}).status_code == 200
    res = world.sam.put("/api/project-groups/retail/members",
                        json={"upsert": [{"user_id": world.pat_id, "role_key": "viewer"}]})
    assert res.status_code == 200, res.text
    (pending_id,) = res.json()["pending"]
    assert _group_members("retail") == set()
    assert _targets("steward.pending.request") == [
        f"group:retail project:cafe project:shop pending:{pending_id} upsert:pat:viewer"]
    # each project's owner is told; owning one project of the group is not enough
    (notice,) = world.olivia.get("/api/steward/notices").json()["items"]
    assert notice["kind"] == "pending" and notice["pending_id"] == pending_id
    assert world.olivia.get("/api/steward/pending").json()["items"] == []
    (item,) = world.sam.get("/api/steward/pending").json()["items"]
    assert item["group_id"] == "retail" and item["mine"] is True
    assert world.olivia.post(f"/api/steward/pending/{pending_id}/confirm").status_code == 404
    res = world.sam.post(f"/api/steward/pending/{pending_id}/confirm")
    assert (res.status_code, res.json()["code"]) == (403, "steward.confirm_self")
    sue = _login(world.app, "sue")
    res = sue.post(f"/api/steward/pending/{pending_id}/confirm")
    assert res.status_code == 200, res.text
    assert res.json()["group_id"] == "retail" and res.json()["added"] == [world.pat_id]
    assert _group_members("retail") == {world.pat_id}
    assert sorted(_targets("steward.members.change")) == [
        "project:cafe group:retail upsert:pat:viewer confirmed_by:sue",
        "project:shop group:retail upsert:pat:viewer confirmed_by:sue",
    ]
    # a rejected one names the group too
    other = world.sam.put("/api/project-groups/retail/members",
                          json={"remove": [world.pat_id]}).json()["pending"][0]
    res = world.sam.post(f"/api/steward/pending/{other}/reject")
    assert res.json() == {"pending_id": other, "project_id": None, "group_id": "retail",
                          "rejected": True}
    assert _group_members("retail") == {world.pat_id}


def test_a_steward_reaches_every_workspace_but_holds_no_grant(world):
    body = world.sam.get("/api/workspaces").json()
    assert body["all_workspaces"] is True and WS in {w["id"] for w in body["workspaces"]}
    res = world.sam.get(f"/api/workspaces/{WS}/grants")
    assert res.status_code == 200, res.text
    res = world.admin.put(f"/api/workspaces/{WS}/grants", json={"grant": [world.sue_id]})
    assert (res.status_code, res.json()["code"]) == (400, "users.steward_no_grants")
    # a project-scoped data route still refuses it
    assert world.sam.get("/api/agents", headers={"X-Project": "shop"}).status_code in (403, 404)


def test_a_steward_reads_only_the_structure_journal(world):
    record_audit_event(workspace_id=WS, actor="olivia", action="release.sign",
                       target="agent:a1", project_id="shop")
    record_audit_event(workspace_id=WS, actor="admin", action="user.update",
                       target="user:pat", project_id=None)
    admin_view = {e["action"] for e in world.admin.get("/api/audit").json()["events"]}
    assert {"release.sign", "user.update", "group.projects.add"} <= admin_view
    res = world.sam.get("/api/audit")
    assert res.status_code == 200, res.text
    seen = {e["action"] for e in res.json()["events"]}
    assert "release.sign" not in seen
    assert {"user.update", "group.projects.add", "project.create"} <= seen
    assert all(audit.steward_visible(a) for a in seen)


def test_a_steward_s_inbox_is_accounts_and_workspaces(world, monkeypatch):
    from app.services import alerts as alert_service

    monkeypatch.setattr(alert_service, "firing_rules",
                        lambda db, ws: [SimpleNamespace(name="r", kind="k", last_detail="x")])
    admin_keys = {i["key"] for i in world.admin.get("/api/inbox").json()["items"]}
    assert "alerts_firing" in admin_keys
    res = world.sam.get("/api/inbox")
    assert res.status_code == 200, res.text
    keys = {i["key"] for i in res.json()["items"]}
    assert keys <= {"accounts_pending", "accounts_expiring", "workspaces_attention"}
    assert res.json()["total"] == sum(i["count"] for i in res.json()["items"])


def test_the_cross_platform_reads_are_structure_for_a_steward(world):
    for path in ("/api/audit", "/api/fleet", "/api/inbox"):
        assert admin_kind("GET", path) == ADMIN
        assert world.sam.get(path).status_code == 200, path
    assert admin_kind("GET", "/api/costs") == ADMIN_DATA


# ── appended route policy ─────────────────────────────────────────────────────


def test_the_appended_route_entries():
    for path in ("/api/knowledge-bases/{kb_id}/migrate-to-project",
                 "/api/knowledge-bases/{kb_id}/migrate-to-project/finish"):
        assert ROUTE_POLICY[("POST", path)] == ADMIN
        assert admin_kind("POST", path) == ADMIN_DATA
    assert {("POST", "/api/gateway-tools/lambda"),
            ("DELETE", "/api/gateway-tools/lambda/{target_id}"),
            ("POST", "/api/identity/gateway-targets")} <= INFRA_LOWERED_WHEN_ISOLATED
