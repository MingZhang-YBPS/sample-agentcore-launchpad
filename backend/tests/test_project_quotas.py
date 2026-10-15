"""Project quotas and budgets (projects design §11.3, G11-quota): validation of every
quota key, the department cap (smaller limit wins; a project value may not exceed it),
GET / PUT `/api/projects/{id}/quotas` (members read with usage, administrators and the
department administrator write, non-members 404), and each enforcement point at its
boundary — agent creation, API key creation, the per-project evaluation slots of the
run queue (queued, not refused), the evaluation cost caps, the advisory release gate,
and the project budget alert rule. A project without quotas meets none of them."""

import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.core.route_policy import DEPARTMENT_ADMIN_ROUTES, ROUTE_POLICY, WORKSPACE_EXEMPT
from app.evaluation.models import EvalRun
from app.evaluation.queue import EvalRunQueue
from app.models.ledger import Agent, AlertRule, ApiKey, AuditEvent
from app.models.projects import Department, Project
from app.services import alerts, project_quotas, release_gates
from app.services.workspace import WorkspaceContext
from tests.test_project_scope import _agent, _login, _member_of, _project, _user, app  # noqa: F401
from tests.test_projects_api import _admin, _create

WS = DEFAULT_WORKSPACE_ID
ALPHA = {"X-Project": "alpha"}


def _set(pid: str, quotas: dict | None, *, department: str | None = None) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(Project, pid)
            row.settings = {"quotas": quotas} if quotas else None
            if department is not None:
                row.department_id = department
            db.commit()
    finally:
        db.close()


def _department(did: str, quotas: dict | None) -> None:
    db = SessionLocal()
    try:
        db.add(Department(id=did, name=did, settings={"quotas": quotas} if quotas else None))
        db.commit()
    finally:
        db.close()


def _run(pid: str, status: str = "completed", *, actual=None, estimate=None,
         created_at: datetime | None = None) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(EvalRun(workspace_id=WS, agent_id="a" * 32, agent_name="a", status=status,
                           cost_actual=actual, cost_estimate=estimate,
                           created_at=created_at or datetime.now(UTC)))
            db.commit()
    finally:
        db.close()


@pytest.fixture
def world(app):  # noqa: F811
    """alpha (dev a developer, out no member) in department retail (dana administers it)."""
    admin = _admin(app)
    dana, dev, out = (_user(n, grant=False) for n in ("dana", "dev", "out"))
    assert admin.post("/api/departments", json={"id": "retail", "name": "Retail"}
                      ).status_code == 201
    _create(admin, "alpha", department_id="retail")
    _create(admin, "beta")
    _member_of("alpha", dev)
    assert admin.put("/api/departments/retail/admins", json={"add": [dana]}).status_code == 200
    return SimpleNamespace(admin=admin, app=app, dana=_login(app, "dana"),
                           dev=_login(app, "dev"), out=_login(app, "out"))


# ── routes ────────────────────────────────────────────────────────────────────


def test_routes_are_classified():
    assert ROUTE_POLICY[("GET", "/api/projects/{project_id}/quotas")] == "member"
    assert ROUTE_POLICY[("PUT", "/api/projects/{project_id}/quotas")] == "admin"
    assert DEPARTMENT_ADMIN_ROUTES[("PUT", "/api/projects/{project_id}/quotas")] == "project"
    assert ("GET", "/api/projects/{project_id}/quotas") in WORKSPACE_EXEMPT


def test_read_is_members_write_is_admins_and_the_departments(world):
    view = world.dev.get("/api/projects/alpha/quotas")
    assert view.status_code == 200, view.text
    items = {i["key"]: i for i in view.json()["items"]}
    assert set(items) == set(project_quotas.ENFORCEMENT)
    assert items["max_agents"] == {
        "key": "max_agents", "unit": "count", "enforcement": "block", "project_value": None,
        "department_cap": None, "limit": None, "used": 0}
    # a non-member: the project does not exist; another department's admin likewise
    assert world.out.get("/api/projects/alpha/quotas").status_code == 404
    assert world.dana.get("/api/projects/beta/quotas").status_code == 404
    assert world.dana.put("/api/projects/beta/quotas",
                          json={"quotas": {"max_agents": 3}}).status_code == 404
    # a member cannot write
    assert world.dev.put("/api/projects/alpha/quotas",
                         json={"quotas": {"max_agents": 3}}).status_code == 403
    # the department administrator can, inside the department
    res = world.dana.put("/api/projects/alpha/quotas", json={"quotas": {"max_agents": 3}})
    assert res.status_code == 200, res.text
    assert res.json()["quotas"] == {"max_agents": 3}
    # an administrator; quotas merge, a null clears one
    res = world.admin.put("/api/projects/alpha/quotas", json={"quotas": {
        "max_api_keys": 2, "eval_run_max_usd": 1.5, "max_agents": None}})
    assert res.json()["quotas"] == {"max_api_keys": 2, "eval_run_max_usd": 1.5}
    db = SessionLocal()
    try:
        targets = list(db.scalars(select(AuditEvent.target).where(
            AuditEvent.action == "project.quotas.update",
            AuditEvent.target.like("% quotas:%")).order_by(AuditEvent.created_at)))
    finally:
        db.close()
    assert targets == ["project:alpha quotas:max_agents",
                       "project:alpha quotas:eval_run_max_usd,max_agents,max_api_keys"]


@pytest.mark.parametrize("quotas", [
    {"max_agents": -1}, {"max_agents": 1.5}, {"max_agents": True}, {"max_agents": "3"},
    {"max_concurrent_eval_runs": 0}, {"eval_run_max_usd": -0.01},
    {"monthly_cost_alert_usd": "100"}, {"widgets": 1},
])
def test_invalid_values_are_refused(world, quotas):
    res = world.admin.put("/api/projects/alpha/quotas", json={"quotas": quotas})
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "project.setting_invalid"


def test_the_department_cap_bounds_the_project(world):
    db = SessionLocal()
    try:
        db.get(Department, "retail").settings = {"quotas": {"max_agents": 5,
                                                            "v1_rate_per_minute": 100}}
        db.commit()
    finally:
        db.close()
    over = world.dana.put("/api/projects/alpha/quotas", json={"quotas": {"max_agents": 6}})
    assert over.status_code == 422 and over.json()["code"] == "project.quota_above_department"
    # administrators too: raise the department's cap first
    assert world.admin.put("/api/projects/alpha/quotas",
                           json={"quotas": {"max_agents": 6}}).status_code == 422
    # unset, the department's cap applies; set lower, the project's
    items = {i["key"]: i for i in world.dev.get("/api/projects/alpha/quotas").json()["items"]}
    assert (items["max_agents"]["limit"], items["max_agents"]["department_cap"]) == (5, 5)
    assert world.dana.put("/api/projects/alpha/quotas",
                          json={"quotas": {"max_agents": 2}}).status_code == 200
    items = {i["key"]: i for i in world.dev.get("/api/projects/alpha/quotas").json()["items"]}
    assert items["max_agents"]["limit"] == 2
    # the /v1 window: the department's administrator only under a cap the platform set
    assert world.dana.put("/api/projects/alpha/quotas",
                          json={"quotas": {"v1_rate_per_minute": 50}}).status_code == 200


def test_the_v1_window_needs_a_department_cap_for_a_department_admin(world):
    res = world.dana.put("/api/projects/alpha/quotas", json={"quotas": {"v1_rate_per_minute": 5}})
    assert res.status_code == 403 and res.json()["code"] == "project.setting_forbidden"
    assert world.admin.put("/api/projects/alpha/quotas",
                           json={"quotas": {"v1_rate_per_minute": 5}}).status_code == 200


def test_an_archived_project_keeps_its_quotas(world):
    assert world.admin.post("/api/projects/alpha/archive").status_code == 200
    res = world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"max_agents": 1}})
    assert res.status_code == 409 and res.json()["code"] == "project.archived"


# ── agents ────────────────────────────────────────────────────────────────────


def test_agent_creation_stops_at_the_quota(world):
    _agent("one", "alpha")
    _agent("gone", "alpha")
    db = SessionLocal()
    try:
        with project_context(None):
            db.execute(Agent.__table__.update().where(Agent.name == "gone")
                       .values(status="deleted"))
            db.commit()
    finally:
        db.close()
    _set("alpha", {"max_agents": 1}, department="retail")
    res = world.admin.post("/api/agents", headers=ALPHA, json={
        "name": "two", "method": "harness", "system_prompt": "p"})
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "project.quota_exceeded"
    assert res.json()["detail"] == {"quota": "max_agents", "used": 1, "limit": 1}
    # another project, and the default project without quotas, are not affected
    assert project_quotas.assert_capacity(SessionLocal(), "default", "max_agents") is None


def test_a_concurrent_creator_of_the_last_slot_loses_on_the_recount(world):
    _set("alpha", {"max_agents": 1})
    db = SessionLocal()
    try:
        with project_context("alpha"):
            project_quotas.assert_capacity(db, "alpha", "max_agents")  # 0 of 1: passes
            _agent("raced", "alpha")  # another request commits the last slot meanwhile
            mine = Agent(workspace_id=WS, name="mine", method="harness", status="deploying",
                         spec={"name": "mine"})
            db.add(mine)
            db.flush()
            with pytest.raises(AppError) as exc:
                project_quotas.recheck_after_insert(db, mine.project_id, "max_agents")
        assert exc.value.code == "project.quota_exceeded"
        with project_context(None):
            assert db.scalar(select(Agent.id).where(Agent.name == "mine")) is None  # rolled back
    finally:
        db.close()


# ── API keys ──────────────────────────────────────────────────────────────────


def test_api_key_creation_stops_at_the_quota_and_a_disabled_key_frees_its_slot(world):
    _set("alpha", {"max_api_keys": 1})
    first = world.admin.post("/api/apikeys", headers=ALPHA, json={"name": "k1"})
    assert first.status_code == 201, first.text
    second = world.admin.post("/api/apikeys", headers=ALPHA, json={"name": "k2"})
    assert second.status_code == 409
    assert second.json()["detail"] == {"quota": "max_api_keys", "used": 1, "limit": 1}
    # the default project has no quota
    assert world.admin.post("/api/apikeys", headers={"X-Project": "default"},
                            json={"name": "k"}).status_code == 201
    assert world.admin.post(f"/api/apikeys/{first.json()['id']}/disable",
                            headers=ALPHA).status_code == 200
    assert world.admin.post("/api/apikeys", headers=ALPHA, json={"name": "k3"}).status_code == 201
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.scalar(select(ApiKey.id).where(ApiKey.name == "k2")) is None
    finally:
        db.close()


# ── evaluation ────────────────────────────────────────────────────────────────


def _wait(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_the_queue_holds_a_full_projects_run_and_lets_others_through():
    limits = {"alpha": 1}
    q = EvalRunQueue(max_concurrency=3, project_limit=limits.get)
    release = threading.Event()
    ran: list[str] = []

    def job(name):
        def fn():
            ran.append(name)
            if name == "a1":
                release.wait(5)
        return fn

    q.submit("a1", job("a1"), project_id="alpha")
    assert _wait(lambda: "a1" in ran)
    q.submit("a2", job("a2"), project_id="alpha")
    q.submit("b1", job("b1"), project_id="beta")
    assert _wait(lambda: "b1" in ran)
    assert _wait(lambda: q.state()["project_waiting"] == ["a2"])
    assert q.project_full("a2") and q.position("a2") == 1 and "a2" not in ran
    release.set()
    assert _wait(lambda: "a2" in ran)
    q._queue.join()  # every task closed, held ones included
    assert q.state()["project_waiting"] == [] and q.state()["queued"] == []


def test_a_held_run_can_be_cancelled():
    q = EvalRunQueue(max_concurrency=2, project_limit=lambda pid: 1)
    release = threading.Event()
    ran: list[str] = []
    q.submit("a1", lambda: (ran.append("a1"), release.wait(5)), project_id="alpha")
    assert _wait(lambda: "a1" in ran)
    q.submit("a2", lambda: ran.append("a2"), project_id="alpha")
    assert _wait(lambda: q.project_full("a2"))
    assert q.cancel("a2") is True
    release.set()
    q._queue.join()
    assert ran == ["a1"]


def test_without_a_project_limit_the_queue_is_unchanged():
    q = EvalRunQueue(max_concurrency=2, project_limit=lambda pid: None)
    done = threading.Event()
    q.submit("x", done.set, project_id="alpha")
    assert done.wait(5)
    q._queue.join()


def test_eval_cost_caps(app):  # noqa: F811
    _project("alpha")
    db = SessionLocal()
    try:
        project_quotas.assert_eval_cost(db, "alpha", 100.0)  # no quotas: nothing to check
        _set("alpha", {"eval_run_max_usd": 5, "eval_monthly_usd_cap": 10})
        project_quotas.assert_eval_cost(db, "alpha", 5.0)  # at the boundary: allowed
        with pytest.raises(AppError) as exc:
            project_quotas.assert_eval_cost(db, "alpha", 5.01)
        assert exc.value.detail["quota"] == "eval_run_max_usd"
        # this month: an actual 4, an estimate 3 still running; failed and last month free
        _run("alpha", actual={"agent_usd": 4.0}, estimate={"total_usd": 9})
        _run("alpha", status="evaluating", estimate={"total_usd": 3})
        _run("alpha", status="failed", estimate={"total_usd": 50})
        _run("alpha", actual={"agent_usd": 99.0},
             created_at=datetime.now(UTC).replace(day=1) - timedelta(days=3))
        assert project_quotas.eval_month_usd(db, "alpha") == 7.0
        project_quotas.assert_eval_cost(db, "alpha", 3.0)  # 7 + 3 = 10: allowed
        with pytest.raises(AppError) as exc:
            project_quotas.assert_eval_cost(db, "alpha", 3.5)
        assert exc.value.status_code == 409
        assert exc.value.detail == {"quota": "eval_monthly_usd_cap", "used": 7.0,
                                    "limit": 10, "estimate": 3.5}
        # the release-gate helper reads the estimate's total
        with pytest.raises(AppError):
            release_gates.assert_eval_budget(db, "alpha", {"total_usd": 3.5})
        release_gates.assert_eval_budget(db, "alpha", {"total_usd": None})
    finally:
        db.close()


def test_the_per_run_limit_is_the_smaller_of_workspace_and_project(app):  # noqa: F811
    _project("alpha")
    _set("alpha", {"eval_run_max_usd": 4})
    db = SessionLocal()
    try:
        policy = SimpleNamespace(release_policy={"eval_cost_max_usd": 10})
        assert release_gates.eval_cost_max_usd(db, policy, "alpha") == 4
        assert release_gates.eval_cost_max_usd(db, policy, "default") == 10
        assert release_gates.eval_cost_max_usd(db, None, None) is None
    finally:
        db.close()


def test_the_budget_gate_is_advisory_and_absent_without_quotas(app):  # noqa: F811
    _project("alpha")
    db = SessionLocal()
    try:
        assert release_gates.project_budget_gate(db, "alpha") is None
        assert "project_budget" in release_gates.ADVISORY_GATES
        _set("alpha", {"eval_monthly_usd_cap": 5, "monthly_cost_alert_usd": 100})
        _run("alpha", actual={"agent_usd": 6.0})
        gate = release_gates.project_budget_gate(db, "alpha")
        assert gate["ok"] is False and gate["eval_spent_usd"] == 6.0
        assert "spend not measured yet" in gate["detail"]
    finally:
        db.close()


# ── the budget alert ──────────────────────────────────────────────────────────


def _budget_rules() -> list[AlertRule]:
    db = SessionLocal()
    try:
        with project_context(None):
            return list(db.scalars(select(AlertRule).where(
                AlertRule.name == alerts.BUDGET_RULE_NAME)))
    finally:
        db.close()


def test_the_budget_quota_keeps_one_alert_rule(world):
    res = world.admin.put("/api/projects/alpha/quotas",
                          json={"quotas": {"monthly_cost_alert_usd": 200}})
    assert res.status_code == 200, res.text
    [rule] = _budget_rules()
    assert (rule.project_id, rule.workspace_id, rule.kind, rule.threshold) == (
        "alpha", WS, "cost_mtd_usd", 200.0)
    world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"monthly_cost_alert_usd": 50}})
    [rule] = _budget_rules()
    assert rule.threshold == 50.0
    world.admin.put("/api/projects/alpha/quotas",
                    json={"quotas": {"monthly_cost_alert_usd": None}})
    assert _budget_rules() == []


def test_the_budget_rule_fires_on_the_projects_spend(world, monkeypatch):
    _set("alpha", {"monthly_cost_alert_usd": 10})
    seen: list[str | None] = []

    def spend(db, workspace, *, force=False, project=None):
        seen.append(project)
        return 12.5

    monkeypatch.setattr(alerts.costs, "month_to_date_usd", spend)
    ctx = WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})
    db = SessionLocal()
    try:
        result = alerts.evaluate_rules(db, ctx, notify_transitions=False)  # creates the rule
    finally:
        db.close()
    [row] = [r for r in result["rules"] if r["budget"]]
    assert (row["state"], row["last_value"], row["project_id"]) == ("firing", 12.5, "alpha")
    assert seen == ["alpha"]  # two projects share the workspace: alpha's spend only
    items = {i["key"]: i for i in world.dev.get("/api/projects/alpha/quotas").json()["items"]}
    assert (items["monthly_cost_alert_usd"]["used"], items["monthly_cost_alert_usd"]["state"]
            ) == (12.5, "firing")
    # the quota cleared: the next pass removes the rule
    _set("alpha", None)
    db = SessionLocal()
    try:
        alerts.evaluate_rules(db, ctx, notify_transitions=False)
    finally:
        db.close()
    assert _budget_rules() == []
