"""WIRE-B: the cross-file follow-ups of the B8 nodes — the journal vocabulary
(`project.quotas.update`, `system.update.*`, `steward.*`) and what a steward reads of
it; no department quota caps; quota checks on a key re-enable, a discovery import and
an evaluation estimate; a retired group's pending steward change, the steward's
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
from app.models.projects import Project
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


def test_a_department_caps_no_quota(quota_world):
    """A project's quotas and monthly budget are the platform administrators' alone:
    a department's `settings.quotas` is not a setting any more, and the project's own
    value is its limit."""
    admin = quota_world.admin
    res = admin.patch("/api/departments/retail", json={"settings": {
        "quotas": {"max_agents": 5}}})
    assert (res.status_code, res.json()["code"]) == (400, "department.setting_invalid")
    assert admin.put("/api/projects/alpha/quotas",
                     json={"quotas": {"max_agents": 7}}).status_code == 200
    db = SessionLocal()
    try:
        assert project_quotas.limit_of(db, "alpha", "max_agents") == 7
    finally:
        db.close()
    assert _targets("project.quotas.update") == ["project:alpha quotas:max_agents"]


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
    def import_runtimes(control, db, runtime_ids, *, workspace_id, project_id=None):
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


def test_the_estimate_limit_is_the_workspaces(quota_world):
    """No per-run money limit per project: the monthly budget is its one money limit."""
    from app.dlc import cost as cost_svc

    _set_quotas("alpha", {"monthly_budget_usd": 4})
    db = SessionLocal()
    try:
        workspace = SimpleNamespace(release_policy={"eval_cost_max_usd": 10})
        estimate = cost_svc.estimate(
            db, agent=SimpleNamespace(project_id="alpha", spec={}), workspace=workspace,
            workspace_ctx=None, items=[], evaluators=[])
        assert (estimate["max_usd"], estimate["max_source"]) == (10, "workspace")
        estimate = cost_svc.estimate(db, agent=None, workspace=workspace, workspace_ctx=None,
                                     items=[], evaluators=[])
        assert estimate["max_usd"] == 10
    finally:
        db.close()


# ── the steward ───────────────────────────────────────────────────────────────


@pytest.fixture
def world(app):  # noqa: F811
    """`shop` (owned by olivia) and `cafe` (owned by otto); sam and sue are stewards;
    pat a plain member with no project."""
    admin = _admin(app)
    olivia, otto = _user("olivia", grant=False), _user("otto", grant=False)
    pat = _user("pat", grant=False)
    sam = _user("sam", role=STEWARD, grant=False)
    sue = _user("sue", role=STEWARD, grant=False)
    _create(admin, "shop", owners=[olivia])
    _create(admin, "cafe", owners=[otto])
    return SimpleNamespace(
        app=app, admin=admin, olivia_id=olivia, otto_id=otto, pat_id=pat, sam_id=sam,
        sue_id=sue, sam=_login(app, "sam"), olivia=_login(app, "olivia"),
        otto=_login(app, "otto"),
    )


def test_a_pending_change_of_a_retired_group_can_be_rejected_not_confirmed(world):
    """A two-person change staged for a project group before groups were retired: it is
    still listed and may be rejected, never confirmed (its members are direct now)."""
    import json

    from app.models.platform_settings import PlatformSetting
    from app.services import steward

    doc = {"id": "legacy1", "project_id": None, "group_id": "retail", "group_name": "Retail",
           "project_ids": ["cafe", "shop"], "upsert": [{"user_id": world.pat_id,
                                                         "role_key": "viewer"}],
           "remove": [], "changes": [], "requested_by": "sam",
           "requested_by_user_id": world.sam_id, "created_at": "2026-01-01T00:00:00+00:00"}
    db = SessionLocal()
    try:
        db.add(PlatformSetting(key=f"{steward.PENDING_PREFIX}legacy1", value=json.dumps(doc),
                               updated_by="sam"))
        db.commit()
    finally:
        db.close()
    sue = _login(world.app, "sue")
    res = sue.post("/api/steward/pending/legacy1/confirm")
    assert (res.status_code, res.json()["code"]) == (409, "steward.pending_group_retired")
    res = sue.post("/api/steward/pending/legacy1/reject")
    assert res.json() == {"pending_id": "legacy1", "project_id": None, "group_id": "retail",
                          "rejected": True}
    assert _targets("steward.pending.reject") == [
        "group:retail project:cafe project:shop pending:legacy1 requested_by:sam"]


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
    assert {"release.sign", "user.update", "project.workspaces.bind"} <= admin_view
    res = world.sam.get("/api/audit")
    assert res.status_code == 200, res.text
    seen = {e["action"] for e in res.json()["events"]}
    assert "release.sign" not in seen
    assert {"user.update", "project.workspaces.bind", "project.create"} <= seen
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
