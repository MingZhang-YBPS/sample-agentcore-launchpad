"""Department budgets (projects §11.3, DBUD): the allocation rules, the sum constraint,
the permission matrix (platform administrator / this department's administrator /
another department's administrator / a project member), the 80/100 and 80/105/110
thresholds, the department alerts, and the three cost views."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.core.route_policy import (
    ADMIN_DATA,
    ADMIN_SPLIT,
    DEPARTMENT_ADMIN_ROUTES,
    ROUTE_POLICY,
    WORKSPACE_EXEMPT,
    admin_kind,
)
from app.models.ledger import AlertRule, AuditEvent
from app.models.projects import Department, Project
from app.services import alerts, project_quotas
from app.services.workspace import WorkspaceContext
from tests.test_project_scope import _login, _member_of, _user, app  # noqa: F401
from tests.test_projects_api import _admin, _create

WS = DEFAULT_WORKSPACE_ID
DEPT_BUDGET = "/api/departments/{department_id}/budget"
PROJECT_BUDGET = "/api/projects/{project_id}/budget"


def _spend(monkeypatch, usd):
    def fake(db, project_id, *, workspace=None):
        value = usd(project_id) if callable(usd) else usd
        return value, "test spend"

    monkeypatch.setattr(project_quotas, "project_spend", fake)
    project_quotas.clear_spend_cache()


@pytest.fixture
def world(app):  # noqa: F811
    """retail (dana administers it): alpha (dev a developer), alpha2; ops (olga
    administers it): gamma. The built-in `default` project has no department. Both
    department administrators hold `budget.manage` (granted, never by role)."""
    admin = _admin(app)
    dana, olga, dev = (_user(n, grant=False) for n in ("dana", "olga", "dev"))
    for dept in ("retail", "ops"):
        assert admin.post("/api/departments", json={"id": dept, "name": dept.title()}
                          ).status_code == 201
    _create(admin, "alpha", department_id="retail")
    _create(admin, "alpha2", department_id="retail")
    _create(admin, "gamma", department_id="ops")
    _member_of("alpha", dev)
    assert admin.put("/api/departments/retail/admins", json={"add": [dana]}).status_code == 200
    assert admin.put("/api/departments/ops/admins", json={"add": [olga]}).status_code == 200
    for uid in (dana, olga):
        _grant_budget(admin, uid)
    project_quotas.clear_spend_cache()
    return SimpleNamespace(admin=admin, dana=_login(app, "dana"), olga=_login(app, "olga"),
                           dev=_login(app, "dev"), dev_id=dev, dana_id=dana)


def _grant_budget(admin, user_id: str, granted: bool = True) -> None:
    res = admin.patch(f"/api/users/{user_id}", json={"permissions": {"budget.manage": granted}})
    assert res.status_code == 200, res.text
    assert res.json()["permissions"]["budget.manage"] is granted


def _journal(action: str) -> list[str]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(AuditEvent.target).where(
            AuditEvent.action == action).order_by(AuditEvent.created_at)))
    finally:
        db.close()


# ── routes ────────────────────────────────────────────────────────────────────


def test_the_routes_are_classified():
    assert ROUTE_POLICY[("PUT", PROJECT_BUDGET)] == "admin"
    assert DEPARTMENT_ADMIN_ROUTES[("PUT", PROJECT_BUDGET)] == "project"
    assert ROUTE_POLICY[("PUT", DEPT_BUDGET)] == "admin"
    assert ("PUT", DEPT_BUDGET) not in DEPARTMENT_ADMIN_ROUTES  # platform only
    assert ROUTE_POLICY[("GET", DEPT_BUDGET)] == "admin"
    assert DEPARTMENT_ADMIN_ROUTES[("GET", DEPT_BUDGET)] == "department"
    assert ROUTE_POLICY[("GET", "/api/budgets/platform")] == "admin"
    assert admin_kind("GET", "/api/budgets/platform") == ADMIN_DATA
    assert ADMIN_SPLIT[("PUT", DEPT_BUDGET)] == "admin"  # structure: a steward may
    assert ROUTE_POLICY[("GET", "/api/budgets/me")] == "member"
    for key in (("PUT", PROJECT_BUDGET), ("PUT", DEPT_BUDGET), ("GET", DEPT_BUDGET),
                ("GET", "/api/budgets/platform"), ("GET", "/api/budgets/me")):
        assert key in WORKSPACE_EXEMPT


# ── the department budget: platform administrators only ───────────────────────


def test_only_a_platform_admin_sets_a_department_budget(world):
    for client in (world.dana, world.olga, world.dev):
        res = client.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
        assert res.status_code == 403, res.text
    res = world.admin.put("/api/departments/retail/budget",
                          json={"monthly_budget_usd": 100, "allow_overrun": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["monthly_budget_usd"], body["allow_overrun"], body["allocated_usd"],
            body["unallocated_usd"]) == (100, True, 0.0, 100.0)
    assert _journal("department.budget.update") == [
        "department:retail monthly_budget_usd=100 allow_overrun=on"]
    # invalid values
    for bad in ({"monthly_budget_usd": -1}, {"monthly_budget_usd": "5"},
                {"monthly_budget_usd": True}, {"allow_overrun": "yes"}):
        assert world.admin.put("/api/departments/retail/budget", json=bad).status_code == 400
    # the generic settings PATCH cannot set it around the checks
    res = world.admin.patch("/api/departments/retail",
                            json={"settings": {"budget": {"monthly_budget_usd": 1}}})
    assert res.status_code == 400


# ── the permission matrix of the project budget ──────────────────────────────


def test_the_project_budget_permission_matrix(world):
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    body = {"monthly_budget_usd": 30}
    # a project member, and another department's administrator: no
    assert world.dev.put("/api/projects/alpha/budget", json=body).status_code == 403
    assert world.olga.put("/api/projects/alpha/budget", json=body).status_code == 404
    # this department's administrator: yes, and the read shows it
    res = world.dana.put("/api/projects/alpha/budget", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["quotas"] == {"monthly_budget_usd": 30}
    assert res.json()["department_budget"]["allocated_usd"] == 30.0
    # a platform administrator: yes
    assert world.admin.put("/api/projects/alpha2/budget",
                           json={"monthly_budget_usd": 20}).status_code == 200
    # the general quotas stay the platform's alone
    assert world.dana.put("/api/projects/alpha/quotas",
                          json={"quotas": {"max_agents": 3}}).status_code == 403
    # a project with no department: platform administrators only (to a department
    # administrator it does not exist, like another department's project)
    assert world.dana.put("/api/projects/default/budget", json=body).status_code == 404
    assert world.admin.put("/api/projects/default/budget", json=body).status_code == 200
    assert _journal("project.budget.update") == [
        "project:alpha monthly_budget_usd=30", "project:alpha2 monthly_budget_usd=20",
        "project:default monthly_budget_usd=30"]


# ── allocation and the sum constraint ─────────────────────────────────────────


def test_the_sum_of_project_budgets_stays_within_the_department_budget(world):
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    assert world.dana.put("/api/projects/alpha/budget",
                          json={"monthly_budget_usd": 60}).status_code == 200
    res = world.dana.put("/api/projects/alpha2/budget", json={"monthly_budget_usd": 41})
    assert res.status_code == 409
    assert res.json()["code"] == "department.budget_overallocated"
    assert res.json()["detail"] == {
        "department_id": "retail", "budget_usd": 100.0, "allocated_usd": 60.0,
        "requested_usd": 41.0, "remaining_usd": 40.0}
    # a platform administrator meets the same ceiling, on either route
    assert world.admin.put("/api/projects/alpha2/budget",
                           json={"monthly_budget_usd": 41}).status_code == 409
    res = world.admin.put("/api/projects/alpha2/quotas",
                          json={"quotas": {"monthly_budget_usd": 41}})
    assert res.status_code == 409 and res.json()["code"] == "department.budget_overallocated"
    res = world.admin.patch("/api/projects/alpha2",
                            json={"settings": {"quotas": {"monthly_budget_usd": 41}}})
    assert res.status_code == 409
    # exactly the remainder fits
    assert world.dana.put("/api/projects/alpha2/budget",
                          json={"monthly_budget_usd": 40}).status_code == 200
    # raising its own budget counts only the others
    assert world.dana.put("/api/projects/alpha/budget",
                          json={"monthly_budget_usd": 60}).status_code == 200
    # lowering the department budget below what is allocated
    res = world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 99})
    assert res.status_code == 409 and res.json()["code"] == "department.budget_below_allocated"
    assert res.json()["detail"]["allocated_usd"] == 100.0
    assert world.admin.put("/api/departments/retail/budget",
                           json={"monthly_budget_usd": 100}).status_code == 200


def test_moving_a_project_needs_room_in_the_new_department(world):
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 50})
    world.admin.put("/api/departments/ops/budget", json={"monthly_budget_usd": 100})
    assert world.olga.put("/api/projects/gamma/budget",
                          json={"monthly_budget_usd": 60}).status_code == 200
    res = world.admin.patch("/api/projects/gamma", json={"department_id": "retail"})
    assert res.status_code == 409
    assert res.json()["code"] == "department.budget_overallocated"
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.get(Project, "gamma").department_id == "ops"  # nothing moved
    finally:
        db.close()
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 60})
    assert world.admin.patch("/api/projects/gamma",
                             json={"department_id": "retail"}).status_code == 200


def test_a_department_without_a_budget_is_unlimited(world):
    # no department budget: the department administrator allocates without a ceiling
    assert world.dana.put("/api/projects/alpha/budget",
                          json={"monthly_budget_usd": 10_000}).status_code == 200
    assert world.dana.put("/api/projects/alpha2/budget",
                          json={"monthly_budget_usd": 50_000}).status_code == 200
    assert world.admin.put("/api/projects/gamma/budget",
                           json={"monthly_budget_usd": 7}).status_code == 200
    body = world.dana.get("/api/departments/retail/budget").json()
    assert (body["monthly_budget_usd"], body["allocated_usd"], body["unallocated_usd"]) == (
        None, 60_000.0, None)
    # a ceiling set later may not go below what is allocated
    res = world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    assert res.status_code == 409 and res.json()["code"] == "department.budget_below_allocated"


# ── budget.manage: one key, the identity decides the level ────────────────────


def test_budget_writes_need_budget_manage(app, world):  # noqa: F811
    # a department administrator without the key: refused, on every budget path
    _grant_budget(world.admin, world.dana_id, granted=False)
    dana = _login(app, "dana")
    res = dana.put("/api/projects/alpha/budget", json={"monthly_budget_usd": 10})
    assert res.status_code == 403 and res.json()["code"] == "budget.forbidden"
    assert res.json()["detail"]["permission"] == "budget.manage"
    assert dana.get("/api/budgets/me").json()["budget_scope"] is None
    # ...yet still reads the department page (viewing is not a budget operation)
    assert dana.get("/api/departments/retail/budget").status_code == 200
    # the key without a department to administer reaches nothing either
    _grant_budget(world.admin, world.dev_id)
    dev = _login(app, "dev")
    assert dev.put("/api/projects/alpha/budget",
                   json={"monthly_budget_usd": 10}).status_code == 403
    me = dev.get("/api/budgets/me").json()
    assert (me["budget_scope"], me["budget_departments"]) == (None, [])


def test_the_identity_decides_the_level_of_budget_manage(app, world):  # noqa: F811
    from tests.test_project_scope import _user as make_user

    # a department administrator: their department's projects, never its ceiling
    me = world.dana.get("/api/budgets/me").json()
    assert (me["budget_scope"], me["budget_departments"]) == ("department", ["retail"])
    assert world.dana.put("/api/departments/retail/budget",
                          json={"monthly_budget_usd": 100}).status_code == 403
    assert world.dana.put("/api/projects/gamma/budget",
                          json={"monthly_budget_usd": 1}).status_code == 404
    # an organization administrator (steward): no budget authority until granted...
    steward_id = make_user("sam", role="steward", grant=False)
    sam = _login(app, "sam")
    assert sam.get("/api/budgets/me").json()["budget_scope"] is None
    res = sam.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    assert res.status_code == 403 and res.json()["code"] == "budget.forbidden"
    assert sam.put("/api/projects/default/budget",
                   json={"monthly_budget_usd": 5}).status_code == 403
    res = sam.put("/api/projects/alpha/quotas", json={"quotas": {"monthly_budget_usd": 5}})
    assert res.status_code == 403 and res.json()["code"] == "budget.forbidden"
    # ...the other quotas stay structure, open to a steward without the key
    assert sam.put("/api/projects/alpha/quotas",
                   json={"quotas": {"max_agents": 3}}).status_code == 200
    # ...then the organization level: every department and every project
    _grant_budget(world.admin, steward_id)
    sam = _login(app, "sam")
    assert sam.get("/api/budgets/me").json()["budget_scope"] == "platform"
    assert sam.put("/api/departments/retail/budget",
                   json={"monthly_budget_usd": 100}).status_code == 200
    for pid in ("alpha", "gamma", "default"):
        assert sam.put(f"/api/projects/{pid}/budget",
                       json={"monthly_budget_usd": 5}).status_code == 200, pid
    # a platform administrator holds the key by role
    assert world.admin.get("/api/budgets/me").json()["budget_scope"] == "platform"


def test_an_over_allocated_legacy_department_can_be_brought_back_under(world):
    world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"monthly_budget_usd": 80}})
    world.admin.put("/api/projects/alpha2/quotas", json={"quotas": {"monthly_budget_usd": 80}})
    # the department budget cannot be set below what is already allocated
    res = world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    assert res.status_code == 409
    db = SessionLocal()
    try:  # a legacy state: the ceiling set behind the rules' back
        with project_context(None):
            db.get(Department, "retail").settings = {"budget": {"monthly_budget_usd": 100}}
            db.commit()
    finally:
        db.close()
    # lowering always passes, raising does not
    assert world.dana.put("/api/projects/alpha/budget",
                          json={"monthly_budget_usd": 50}).status_code == 200
    assert world.dana.put("/api/projects/alpha/budget",
                          json={"monthly_budget_usd": 51}).status_code == 409


# ── the overrun switches ──────────────────────────────────────────────────────


def test_the_overrun_switch_follows_the_department(world):
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    res = world.dana.put("/api/projects/alpha/budget", json={"allow_overrun": True})
    assert res.status_code == 409 and res.json()["code"] == "department.overrun_disabled"
    # only a platform administrator turns the department's on
    assert world.dana.put("/api/departments/retail/budget",
                          json={"allow_overrun": True}).status_code == 403
    assert world.admin.put("/api/departments/retail/budget",
                           json={"allow_overrun": True}).status_code == 200
    res = world.dana.put("/api/projects/alpha/budget", json={"allow_overrun": True})
    assert res.status_code == 200 and res.json()["allow_overrun"] is True
    # the department's switch cannot go off under a project that uses it
    res = world.admin.put("/api/departments/retail/budget", json={"allow_overrun": False})
    assert res.status_code == 409
    assert res.json()["code"] == "department.overrun_in_use"
    assert res.json()["detail"]["projects"] == ["alpha"]
    assert world.dana.put("/api/projects/alpha/budget",
                          json={"allow_overrun": False}).status_code == 200
    assert world.admin.put("/api/departments/retail/budget",
                           json={"allow_overrun": False}).status_code == 200
    # a project with its switch on cannot move into a department without one
    world.admin.put("/api/departments/ops/budget", json={"allow_overrun": True})
    world.olga.put("/api/projects/gamma/budget", json={"allow_overrun": True})
    res = world.admin.patch("/api/projects/gamma", json={"department_id": "retail"})
    assert res.status_code == 409 and res.json()["code"] == "department.overrun_disabled"
    assert _journal("project.budget.update") == [
        "project:alpha allow_overrun=on", "project:alpha allow_overrun=off",
        "project:gamma allow_overrun=on"]


# ── thresholds: 80/100 off, 80/105/110 on ─────────────────────────────────────


def _rules(pid: str) -> dict[str, float]:
    db = SessionLocal()
    try:
        with project_context(None):
            return {r.name: r.threshold for r in db.scalars(select(AlertRule).where(
                AlertRule.project_id == pid, AlertRule.created_by == "quota"))}
    finally:
        db.close()


def test_the_alert_rules_follow_the_switch(world):
    world.admin.put("/api/departments/retail/budget",
                    json={"monthly_budget_usd": 1000, "allow_overrun": True})
    world.dana.put("/api/projects/alpha/budget", json={"monthly_budget_usd": 100})
    assert _rules("alpha") == {alerts.BUDGET_WARN_RULE_NAME: 80.0,
                               alerts.BUDGET_RULE_NAME: 100.0}
    world.dana.put("/api/projects/alpha/budget", json={"allow_overrun": True})
    assert _rules("alpha") == {alerts.BUDGET_WARN_RULE_NAME: 80.0,
                               alerts.BUDGET_OVER_RULE_NAME: 105.0}
    world.dana.put("/api/projects/alpha/budget", json={"allow_overrun": False})
    assert _rules("alpha") == {alerts.BUDGET_WARN_RULE_NAME: 80.0,
                               alerts.BUDGET_RULE_NAME: 100.0}


@pytest.mark.parametrize(("overrun", "spent", "blocked"), [
    (False, 99.99, False), (False, 100.0, True),
    (True, 100.0, False), (True, 109.99, False), (True, 110.0, True)])
def test_the_gate_blocks_at_100_or_110_percent(world, monkeypatch, overrun, spent, blocked):
    world.admin.put("/api/departments/retail/budget",
                    json={"monthly_budget_usd": 1000, "allow_overrun": True})
    world.dana.put("/api/projects/alpha/budget",
                   json={"monthly_budget_usd": 100, "allow_overrun": overrun})
    _spend(monkeypatch, spent)
    db = SessionLocal()
    try:
        if blocked:
            with pytest.raises(Exception) as error:
                project_quotas.assert_budget(db, "alpha")
            assert error.value.code == "project.budget_exhausted"
            assert error.value.detail["allow_overrun"] is overrun
            assert error.value.detail["block_at_usd"] == (110.0 if overrun else 100.0)
        else:
            project_quotas.assert_budget(db, "alpha")
        status = project_quotas.budget_status(db, "alpha")
    finally:
        db.close()
    assert status["exhausted"] is blocked
    assert [(line["kind"], line["fraction"]) for line in status["lines"]] == (
        [("warn", 0.8), ("alert", 1.05), ("block", 1.1)] if overrun
        else [("warn", 0.8), ("alert", 1.0), ("block", 1.0)])


@pytest.mark.parametrize(("overrun", "spent", "states"), [
    (False, 85.0, {"80": "firing", "100": "ok"}),
    (False, 100.0, {"80": "firing", "100": "firing"}),
    (True, 104.0, {"80": "firing", "105": "ok"}),
    (True, 105.0, {"80": "firing", "105": "firing"})])
def test_the_project_alerts_fire_at_their_lines(world, monkeypatch, overrun, spent, states):
    world.admin.put("/api/departments/retail/budget",
                    json={"monthly_budget_usd": 1000, "allow_overrun": True})
    world.dana.put("/api/projects/alpha/budget",
                   json={"monthly_budget_usd": 100, "allow_overrun": overrun})
    _spend(monkeypatch, lambda pid: spent if pid == "alpha" else 0.0)
    ctx = WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})
    db = SessionLocal()
    try:
        result = alerts.evaluate_rules(db, ctx, notify_transitions=False)
    finally:
        db.close()
    names = {alerts.BUDGET_WARN_RULE_NAME: "80", alerts.BUDGET_RULE_NAME: "100",
             alerts.BUDGET_OVER_RULE_NAME: "105"}
    seen = {names[r["name"]]: r["state"] for r in result["rules"]
            if r["budget"] and r["project_id"] == "alpha"}
    assert seen == states


# ── department alerts (notify only) ───────────────────────────────────────────


def _evaluate() -> dict:
    ctx = WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})
    db = SessionLocal()
    try:
        return alerts.evaluate_rules(db, ctx, notify_transitions=False)
    finally:
        db.close()


def test_department_alerts_fire_once_at_80_and_100_percent(world, monkeypatch):
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    spend = {"alpha": 50.0, "alpha2": 20.0, "gamma": 999.0}
    _spend(monkeypatch, lambda pid: spend.get(pid, 0.0))
    assert [d["fired"] for d in _evaluate()["departments"]] == [[]]  # 70 %: nothing
    spend["alpha2"] = 30.0
    project_quotas.clear_spend_cache()
    first = _evaluate()["departments"]
    assert [(d["department_id"], d["spent_usd"], d["newly"]) for d in first] == [
        ("retail", 80.0, ["80"])]
    # notify only: nothing is blocked (no project of retail has a budget)
    db = SessionLocal()
    try:
        project_quotas.assert_budget(db, "alpha")
    finally:
        db.close()
    assert _evaluate()["departments"][0]["newly"] == []  # once per line
    spend["alpha"] = 70.0
    project_quotas.clear_spend_cache()
    assert _evaluate()["departments"][0]["newly"] == ["100"]
    assert _journal("department.budget.alert") == [
        "department:retail level:80% spent:80 budget:100",
        "department:retail level:100% spent:100 budget:100"]
    # the department's administrator and the platform's see them; others do not
    for client in (world.dana, world.admin):
        mine = client.get("/api/budgets/me").json()
        assert [(a["department_id"], a["level"]) for a in mine["alerts"]] == [
            ("retail", 80), ("retail", 100)]
    assert world.olga.get("/api/budgets/me").json()["alerts"] == []
    assert world.dev.get("/api/budgets/me").json() == {
        "platform": False, "departments": [], "alerts": [], "budget_scope": None,
        "budget_departments": []}


def test_a_new_month_re_arms_the_department_lines(world, monkeypatch):
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    _spend(monkeypatch, 90.0)
    assert _evaluate()["departments"][0]["newly"] == ["80", "100"]
    monkeypatch.setattr(project_quotas, "_now", lambda: datetime(2031, 2, 1, 1, tzinfo=UTC))
    project_quotas.clear_spend_cache()
    assert _evaluate()["departments"][0]["newly"] == ["80", "100"]


# ── the cost views ────────────────────────────────────────────────────────────


def test_the_department_view_and_who_may_open_it(world, monkeypatch):
    world.admin.put("/api/departments/retail/budget",
                    json={"monthly_budget_usd": 100, "allow_overrun": True})
    world.dana.put("/api/projects/alpha/budget",
                   json={"monthly_budget_usd": 40, "allow_overrun": True})
    world.dana.put("/api/projects/alpha2/budget", json={"monthly_budget_usd": 25})
    _spend(monkeypatch, lambda pid: {"alpha": 30.0, "alpha2": 5.0}.get(pid, 0.0))
    assert world.olga.get("/api/departments/retail/budget").status_code == 403
    assert world.dev.get("/api/departments/retail/budget").status_code == 403
    for client in (world.dana, world.admin):
        res = client.get("/api/departments/retail/budget")
        assert res.status_code == 200, res.text
        body = res.json()
        assert (body["monthly_budget_usd"], body["allocated_usd"], body["unallocated_usd"],
                body["spent_usd"], body["fraction"]) == (100, 65.0, 35.0, 35.0, 0.35)
        rows = {r["project_id"]: r for r in body["projects"]}
        assert (rows["alpha"]["budget_usd"], rows["alpha"]["spent_usd"],
                rows["alpha"]["fraction"], rows["alpha"]["allow_overrun"],
                rows["alpha"]["block_at_usd"]) == (40.0, 30.0, 0.75, True, 44.0)
        assert rows["alpha2"]["block_at_usd"] == 25.0
    # what the caller can open
    assert world.dana.get("/api/budgets/me").json()["departments"] == [
        {"id": "retail", "name": "Retail"}]
    me = world.admin.get("/api/budgets/me").json()
    assert me["platform"] is True and {d["id"] for d in me["departments"]} >= {"retail", "ops"}


def test_the_platform_view_is_platform_admins_only(world, monkeypatch):
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    world.dana.put("/api/projects/alpha/budget", json={"monthly_budget_usd": 40})
    _spend(monkeypatch, lambda pid: {"alpha": 30.0, "gamma": 7.0}.get(pid, 0.0))
    from app.services import costs

    monkeypatch.setattr(costs, "month_to_date_breakdown", lambda db, ctx, force=False: {
        "usd": 50.0, "by_project": {"alpha": 30.0, "gamma": 7.0},
        "unattributed_usd": 13.0, "hours": 1})
    for client in (world.dana, world.olga, world.dev):
        assert client.get("/api/budgets/platform").status_code == 403
    res = world.admin.get("/api/budgets/platform")
    assert res.status_code == 200, res.text
    body = res.json()
    depts = {d["department_id"]: d for d in body["departments"]}
    assert (depts["retail"]["monthly_budget_usd"], depts["retail"]["allocated_usd"],
            depts["retail"]["spent_usd"]) == (100, 40.0, 30.0)
    assert depts["ops"]["spent_usd"] == 7.0
    assert any(p["project_id"] == "default" for p in body["unassigned_projects"])
    # several projects share the workspace: the unattributed part is the platform line
    assert body["platform"]["spent_usd"] == 13.0
    assert body["platform"]["workspaces"][0]["workspace_id"] == WS
    assert body["total_usd"] == round(body["projects_spent_usd"] + 13.0, 4)


def test_budget_writes_are_never_billable(world, monkeypatch):
    """A used-up project can always be given more budget: the budget routes are
    outside the billable prefixes, so the budget gate never sees them."""
    world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"monthly_budget_usd": 1}})
    world.admin.put("/api/departments/retail/budget", json={"monthly_budget_usd": 100})
    _spend(monkeypatch, 5.0)
    assert world.dana.put("/api/projects/alpha/budget",
                          json={"monthly_budget_usd": 10}).status_code == 200


# ── COA calls meet the agent's project's gate (direct and gateway mode) ───────


@pytest.mark.parametrize("via", ["direct", "gateway"])
def test_a_coa_turn_meets_the_agents_project_budget_first(world, monkeypatch, via):
    """A turn's COA tool calls run inside the agent's invoke, so an exhausted project
    is refused before the gateway preflight, a COA token or any COA call."""
    from app.core.errors import AppError
    from app.services import context_layer_gateway, context_layer_tokens, invoke

    world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"monthly_budget_usd": 1}})
    _spend(monkeypatch, 2.0)

    def never(*_a, **_k):
        raise AssertionError("COA reached before the budget gate")

    monkeypatch.setattr(context_layer_gateway, "gateway_turn_auth", never)
    for name in dir(context_layer_tokens):
        if name.startswith(("grant", "member_token", "fetch")):
            monkeypatch.setattr(context_layer_tokens, name, never)
    agent = SimpleNamespace(
        id="a" * 32, project_id="alpha", method="harness", system_key=None,
        spec={"context_layer": {"via": via}}, workspace_id=WS)
    with pytest.raises(AppError) as error:
        invoke.invoke_agent_text(agent, "hello", workspace=None)
    assert error.value.code == "project.budget_exhausted"


def test_the_managed_coa_gateway_is_tagged_as_shared_infrastructure(app):  # noqa: F811
    """Its fees land on the billing view's shared row (the platform line), not
    untagged: the back-fill plans it with no project."""
    from app.models.ledger import ContextLayer
    from app.services import cost_tags

    ctx = WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})
    db = SessionLocal()
    try:
        assert cost_tags.context_layer_infra(db, ctx) == []
        db.add(ContextLayer(workspace_id=ctx.id, prefix="coa", coa_region="us-east-1",
                            connection="conn", console_workload="wl",
                            resources={"gateway": {"gateway_arn": "arn:gw/coa"}}))
        db.commit()
        [item] = cost_tags.context_layer_infra(db, ctx)
        assert (item["kind"], item["arn"], item["project_id"]) == (
            cost_tags.GATEWAY, "arn:gw/coa", None)
        assert cost_tags.tags_for(item["project_id"], workspace_id=ctx.id)[
            "launchpad:project"] == "shared"
    finally:
        db.close()
