"""Project quotas and the monthly budget (projects design §11.3, G11-quota): validation
of every quota key, GET / PUT `/api/projects/{id}/quotas` (members and the department
administrator read with usage and the budget's spend / remaining / reset date; only a
platform administrator writes; non-members 404), the startup migration of the retired
money quotas, the budget gate (blocks at / over the budget, allows under, resets on
the 1st UTC, fail-open, exempt without a project), the count quotas at their
boundaries, the advisory release gate, and the 80 % / 100 % budget alert rules."""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.core import billing_policy
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, engine
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.core.route_policy import DEPARTMENT_ADMIN_ROUTES, ROUTE_POLICY, WORKSPACE_EXEMPT
from app.evaluation.models import EvalRun
from app.evaluation.queue import EvalRunQueue
from app.models.ledger import Agent, AlertRule, ApiKey, AuditEvent
from app.models.projects import Department, Project
from app.services import alerts, project_quotas, release_gates
from app.services.workspace import WorkspaceContext
from tests.conftest import TEST_PROJECT
from tests.test_project_scope import _agent, _login, _member_of, _project, _user, app  # noqa: F401
from tests.test_projects_api import _admin, _create

WS = DEFAULT_WORKSPACE_ID
ALPHA = {"X-Project": "alpha"}
QUOTAS = "/api/projects/{project_id}/quotas"
BASIS = "/api/projects/{project_id}/budget-basis"


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
    project_quotas.clear_spend_cache()


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


def _spend(monkeypatch, usd):
    """The project's month-to-date spend, by project (a callable may read the clock)."""
    calls: list[str] = []

    def fake(db, project_id, *, workspace=None):
        calls.append(project_id)
        value = usd(project_id) if callable(usd) else usd
        return value, "test spend"

    monkeypatch.setattr(project_quotas, "project_spend", fake)
    project_quotas.clear_spend_cache()
    return calls


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


def test_routes_are_classified_admin_only():
    assert ROUTE_POLICY[("GET", QUOTAS)] == "member"
    assert ROUTE_POLICY[("PUT", QUOTAS)] == "admin"
    assert ROUTE_POLICY[("PUT", BASIS)] == "admin"
    # no department-administrator overlay on either write
    assert ("PUT", QUOTAS) not in DEPARTMENT_ADMIN_ROUTES
    assert ("PUT", BASIS) not in DEPARTMENT_ADMIN_ROUTES
    assert ("GET", QUOTAS) in WORKSPACE_EXEMPT


def test_read_is_members_and_the_departments_write_is_platform_admins(world, monkeypatch):
    view = world.dev.get("/api/projects/alpha/quotas")
    assert view.status_code == 200, view.text
    items = {i["key"]: i for i in view.json()["items"]}
    assert set(items) == set(project_quotas.ENFORCEMENT)
    assert items["max_agents"] == {
        "key": "max_agents", "unit": "count", "enforcement": "block", "project_value": None,
        "limit": None, "used": 0}
    assert view.json()["budget"] is None
    # a non-member: the project does not exist; another department's admin likewise
    assert world.out.get("/api/projects/alpha/quotas").status_code == 404
    assert world.dana.get("/api/projects/beta/quotas").status_code == 404
    # a member cannot write; nor can the department's administrator (no overlay)
    for client in (world.dev, world.dana):
        res = client.put("/api/projects/alpha/quotas", json={"quotas": {"max_agents": 3}})
        assert res.status_code == 403, res.text
        res = client.put("/api/projects/alpha/budget-basis", json={"basis": "billing"})
        assert res.status_code == 403, res.text
    # a platform administrator; quotas merge, a null clears one
    res = world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"max_agents": 3}})
    assert res.status_code == 200, res.text
    _spend(monkeypatch, 4.0)
    res = world.admin.put("/api/projects/alpha/quotas", json={"quotas": {
        "max_api_keys": 2, "monthly_budget_usd": 10, "max_agents": None}})
    assert res.json()["quotas"] == {"max_api_keys": 2, "monthly_budget_usd": 10}
    assert world.admin.put("/api/projects/alpha/budget-basis",
                           json={"basis": "billing"}).status_code == 200
    # the department administrator and a member read the budget, spend and reset date
    for client in (world.dev, world.dana):
        body = client.get("/api/projects/alpha/quotas").json()
        budget = body["budget"]
        assert (budget["budget_usd"], budget["spent_usd"], budget["remaining_usd"],
                budget["fraction"], budget["exhausted"]) == (10.0, 4.0, 6.0, 0.4, False)
        assert budget["reset_at"] == body["reset_at"]
        assert budget["reset_at"].startswith(
            project_quotas._next_month_start().strftime("%Y-%m-01T00:00:00"))
        assert budget["basis"] == "billing"
        item = {i["key"]: i for i in body["items"]}["monthly_budget_usd"]
        assert (item["unit"], item["enforcement"], item["limit"], item["used"]) == (
            "usd", "budget", 10, 4.0)
    db = SessionLocal()
    try:
        targets = list(db.scalars(select(AuditEvent.target).where(
            AuditEvent.action == "project.quotas.update").order_by(AuditEvent.created_at)))
    finally:
        db.close()
    assert targets == ["project:alpha quotas:max_agents",
                       "project:alpha quotas:max_agents,max_api_keys,monthly_budget_usd",
                       "project:alpha budget_basis:billing"]


@pytest.mark.parametrize("quotas", [
    {"max_agents": -1}, {"max_agents": 1.5}, {"max_agents": True}, {"max_agents": "3"},
    {"max_concurrent_eval_runs": 0}, {"monthly_budget_usd": -0.01},
    {"monthly_budget_usd": "100"}, {"widgets": 1},
    # the retired money keys are unknown now
    {"monthly_cost_alert_usd": 10}, {"eval_run_max_usd": 1}, {"eval_monthly_usd_cap": 5},
])
def test_invalid_values_are_refused(world, quotas):
    res = world.admin.put("/api/projects/alpha/quotas", json={"quotas": quotas})
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "project.setting_invalid"


def test_a_department_has_no_quota_caps(world):
    res = world.admin.patch("/api/departments/retail",
                            json={"settings": {"quotas": {"max_agents": 5}}})
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "department.setting_invalid"
    body = world.admin.get("/api/projects/alpha/quotas").json()
    assert "department_caps" not in body
    assert all("department_cap" not in item for item in body["items"])


def test_an_archived_project_keeps_its_quotas(world):
    assert world.admin.post("/api/projects/alpha/archive").status_code == 200
    res = world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"max_agents": 1}})
    assert res.status_code == 409 and res.json()["code"] == "project.archived"


# ── the migration ─────────────────────────────────────────────────────────────


def _raw_settings(table: str, row_id: str):
    with engine.begin() as conn:
        raw = conn.execute(text(f"SELECT settings FROM {table} WHERE id = :id"),
                           {"id": row_id}).scalar_one()
    return json.loads(raw) if isinstance(raw, str) else raw


def test_the_retired_money_quotas_migrate_idempotently(app):  # noqa: F811
    for pid in ("p-alert", "p-cap", "p-kept", "p-runonly", "p-none"):
        _project(pid)
    rows = {
        "p-alert": {"quotas": {"monthly_cost_alert_usd": 200, "eval_monthly_usd_cap": 50,
                               "eval_run_max_usd": 5, "max_agents": 3},
                    "budget_basis": "billing"},
        "p-cap": {"quotas": {"eval_monthly_usd_cap": 50}},
        "p-kept": {"quotas": {"monthly_budget_usd": 70, "monthly_cost_alert_usd": 200}},
        "p-runonly": {"quotas": {"eval_run_max_usd": 5}, "sod_strict": True},
        "p-none": {"quotas": {"max_api_keys": 1}},
    }
    db = SessionLocal()
    try:
        db.add(Department(id="dept", name="Dept", settings={
            "quotas": {"max_agents": 5}, "can_create_users": True}))
        db.commit()
    finally:
        db.close()
    with engine.begin() as conn:
        for pid, settings in rows.items():
            conn.execute(text("UPDATE projects SET settings = :s WHERE id = :id"),
                         {"s": json.dumps(settings), "id": pid})
    first = project_quotas.migrate_budget_quotas(engine)
    assert first == {"projects": 4, "departments": 1}
    assert _raw_settings("projects", "p-alert") == {
        "quotas": {"max_agents": 3, "monthly_budget_usd": 200}, "budget_basis": "billing"}
    assert _raw_settings("projects", "p-cap") == {"quotas": {"monthly_budget_usd": 50}}
    assert _raw_settings("projects", "p-kept") == {"quotas": {"monthly_budget_usd": 70}}
    assert _raw_settings("projects", "p-runonly") == {"sod_strict": True}
    assert _raw_settings("projects", "p-none") == {"quotas": {"max_api_keys": 1}}
    assert _raw_settings("departments", "dept") == {"can_create_users": True}
    # a re-run changes nothing
    assert project_quotas.migrate_budget_quotas(engine) == {"projects": 0, "departments": 0}
    assert _raw_settings("projects", "p-alert")["quotas"]["monthly_budget_usd"] == 200


# ── the budget gate ───────────────────────────────────────────────────────────


def test_the_gate_blocks_at_and_over_the_budget_and_allows_under(app, monkeypatch):  # noqa: F811
    _project("alpha")
    spent = {"alpha": 9.99}
    _spend(monkeypatch, lambda pid: spent[pid])
    db = SessionLocal()
    try:
        project_quotas.assert_budget(db, "alpha")  # no budget: nothing to check
        _set("alpha", {"monthly_budget_usd": 10})
        project_quotas.assert_budget(db, "alpha")  # under
        for value in (10.0, 12.5):  # at, over
            spent["alpha"] = value
            project_quotas.clear_spend_cache()
            with pytest.raises(AppError) as exc:
                project_quotas.assert_budget(db, "alpha")
            err = exc.value
            assert (err.code, err.status_code) == ("project.budget_exhausted", 409)
            assert err.detail["budget_usd"] == 10.0 and err.detail["spent_usd"] == value
            assert err.detail["reset_at"] == project_quotas._next_month_start().isoformat()
            assert err.next == [{"action": "project.settings",
                                 "params": {"project_id": "alpha"}}]
        # no project (platform / system work) and an exempt block are never refused
        project_quotas.assert_budget(db, None)
        with project_quotas.budget_exempt():
            project_quotas.assert_budget(db, "alpha")
        # a zero budget freezes billable work
        _set("alpha", {"monthly_budget_usd": 0})
        spent["alpha"] = 0.0
        with pytest.raises(AppError):
            project_quotas.assert_budget(db, "alpha")
    finally:
        db.close()


def test_an_unreadable_spend_fails_open(app, monkeypatch):  # noqa: F811
    _project("alpha")
    _set("alpha", {"monthly_budget_usd": 1})
    _spend(monkeypatch, None)
    db = SessionLocal()
    try:
        project_quotas.assert_budget(db, "alpha")
        assert project_quotas.budget_status(db, "alpha")["spent_usd"] is None
    finally:
        db.close()


def test_the_spend_is_cached_briefly(app, monkeypatch):  # noqa: F811
    _project("alpha")
    _set("alpha", {"monthly_budget_usd": 100})
    calls = _spend(monkeypatch, 1.0)
    db = SessionLocal()
    try:
        for _ in range(5):
            project_quotas.assert_budget(db, "alpha")
        assert calls == ["alpha"]
        monkeypatch.setattr(project_quotas, "SPEND_CACHE_SECONDS", -1.0)
        project_quotas.clear_spend_cache()
        project_quotas.assert_budget(db, "alpha")
        project_quotas.assert_budget(db, "alpha")
        assert len(calls) == 3
    finally:
        db.close()


def test_the_budget_resets_on_the_first_of_the_month_utc(app, monkeypatch):  # noqa: F811
    _project("alpha")
    _set("alpha", {"monthly_budget_usd": 10})
    clock = {"now": datetime(2026, 10, 31, 23, 59, tzinfo=UTC)}
    monkeypatch.setattr(project_quotas, "_now", lambda: clock["now"])
    # month-to-date: October spent 12; November starts from nothing
    _spend(monkeypatch, lambda pid: 12.0 if clock["now"].month == 10 else 0.5)
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            project_quotas.assert_budget(db, "alpha")
        assert exc.value.detail["reset_at"] == "2026-11-01T00:00:00+00:00"
        assert exc.value.detail["month_start"] == "2026-10-01T00:00:00+00:00"
        clock["now"] = datetime(2026, 11, 1, 0, 1, tzinfo=UTC)
        # the cache is per month: October's figure is not reused
        project_quotas.assert_budget(db, "alpha")
        status = project_quotas.budget_status(db, "alpha")
        assert (status["spent_usd"], status["reset_at"]) == (0.5, "2026-12-01T00:00:00+00:00")
        clock["now"] = datetime(2026, 12, 31, 12, tzinfo=UTC)
        assert project_quotas._next_month_start() == datetime(2027, 1, 1, tzinfo=UTC)
    finally:
        db.close()


def test_a_billable_route_is_refused_and_a_read_is_not(world, monkeypatch):
    _set("alpha", {"monthly_budget_usd": 5})
    _spend(monkeypatch, 5.0)
    res = world.admin.post("/api/chat/" + "a" * 32, headers=ALPHA, json={"prompt": "hi"})
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "project.budget_exhausted"
    assert body["detail"]["budget_usd"] == 5.0 and body["detail"]["spent_usd"] == 5.0
    assert body["next"][0]["action"] == "project.settings"
    res = world.admin.post("/api/eval/runs", headers=ALPHA, json={})
    assert res.json()["code"] == "project.budget_exhausted"
    # a FREE write and a read of the same project go through the gate untouched
    assert world.admin.get("/api/agents", headers=ALPHA).status_code == 200
    assert world.admin.get("/api/projects/alpha/quotas").status_code == 200
    res = world.admin.post("/api/eval/runs/estimate", headers=ALPHA, json={})
    assert res.status_code != 409
    # another project is not affected
    assert world.admin.post("/api/chat/" + "a" * 32, headers={"X-Project": "beta"},
                            json={"prompt": "hi"}).status_code != 409


def test_the_invoke_chain_gates_every_entrance(app, monkeypatch):  # noqa: F811
    from app.services import chat, invoke

    _project("alpha")
    agent_id = _agent("bot", "alpha")
    _set("alpha", {"monthly_budget_usd": 1})
    _spend(monkeypatch, 3.0)
    db = SessionLocal()
    try:
        with project_context(None):
            agent = db.get(Agent, agent_id)
        with pytest.raises(AppError) as exc:
            invoke.invoke_agent_text(agent, "hi", session_id="s" * 40)
        assert exc.value.code == "project.budget_exhausted"
        ctx = WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})
        events = list(chat.chat_stream(agent, "hi", session_id="s" * 40, workspace=ctx))
        assert events[0]["event"] == "meta"
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["code"] == "project.budget_exhausted"
    finally:
        db.close()


def test_an_action_dispatcher_gates_only_its_billable_actions(app, monkeypatch):  # noqa: F811
    _project("alpha")
    _set("alpha", {"monthly_budget_usd": 1})
    _spend(monkeypatch, 3.0)
    for action in ("cleanup", "verdict", "accept"):
        billing_policy.gate_action("alpha", action, billing_policy.EXPERIMENT_BILLABLE_ACTIONS)
    with pytest.raises(AppError):
        billing_policy.gate_action("alpha", "traffic",
                                   billing_policy.EXPERIMENT_BILLABLE_ACTIONS)
    with pytest.raises(AppError):
        billing_policy.gate_action("alpha", "setup", billing_policy.CANARY_BILLABLE_ACTIONS)
    billing_policy.gate_action("alpha", "rollback", billing_policy.CANARY_BILLABLE_ACTIONS)


def test_the_spend_adds_this_months_evaluation_judges(app, monkeypatch):  # noqa: F811
    _project("alpha")
    monkeypatch.setattr(project_quotas, "budget_spend",
                        lambda db, ctx, pid, several: (2.0, "estimate"))
    _run("alpha", actual={"final": True, "agent_usd": 4.0, "judge_usd": 1.0,
                          "managed_evaluations_usd": 0.5, "total_usd": 5.5})
    _run("alpha", status="evaluating", estimate={"judge_usd": 0.25, "total_usd": 9})
    _run("alpha", status="failed", estimate={"judge_usd": 50})
    _run("alpha", actual={"final": True, "judge_usd": 99.0},
         created_at=datetime.now(UTC).replace(day=1) - timedelta(days=3))
    db = SessionLocal()
    try:
        assert project_quotas.eval_judges_month_usd(db, "alpha") == 1.75
        usd, _ = project_quotas.project_spend(db, "alpha")
        assert usd == 3.75
    finally:
        db.close()


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
    assert project_quotas.assert_capacity(SessionLocal(), TEST_PROJECT, "max_agents") is None


def test_agent_creation_meets_the_budget(world, monkeypatch):
    _set("alpha", {"monthly_budget_usd": 2})
    _spend(monkeypatch, 2.0)
    res = world.admin.post("/api/agents", headers=ALPHA, json={
        "name": "two", "method": "harness", "system_prompt": "p"})
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "project.budget_exhausted"


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
    assert world.admin.post("/api/apikeys", headers={"X-Project": TEST_PROJECT},
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


def test_an_evaluation_meets_the_budget_not_a_per_run_cap(app, monkeypatch):  # noqa: F811
    _project("alpha")
    _set("alpha", {"monthly_budget_usd": 10})
    _spend(monkeypatch, 9.0)
    db = SessionLocal()
    try:
        # under the budget: a big estimate is not refused (no per-run money limit)
        release_gates.assert_eval_budget(db, "alpha", {"total_usd": 500})
        project_quotas.clear_spend_cache()
        _spend(monkeypatch, 10.0)
        with pytest.raises(AppError) as exc:
            release_gates.assert_eval_budget(db, "alpha", {"total_usd": None})
        assert exc.value.code == "project.budget_exhausted"
    finally:
        db.close()


def test_the_per_run_limit_is_the_workspaces_alone(app):  # noqa: F811
    from app.dlc import cost as cost_svc

    _project("alpha")
    db = SessionLocal()
    try:
        policy = SimpleNamespace(release_policy={"eval_cost_max_usd": 10})
        assert release_gates.eval_cost_limit(db, policy, "alpha") == (10.0, "workspace")
        assert release_gates.eval_cost_max_usd(db, policy, "alpha") == 10.0
        assert release_gates.eval_cost_limit(db, None, None) == (None, None)
    finally:
        db.close()
    over = {"over_limit": True, "total_usd": 12.0, "max_usd": 10.0, "max_source": "workspace"}
    with pytest.raises(AppError) as exc:
        cost_svc.assert_allowed(over, confirmed=True, is_admin=False)
    assert "workspace's limit" in exc.value.message


def test_the_budget_gate_is_advisory_and_absent_without_a_budget(app, monkeypatch):  # noqa: F811
    _project("alpha")
    db = SessionLocal()
    try:
        assert release_gates.project_budget_gate(db, "alpha") is None
        assert "project_budget" in release_gates.ADVISORY_GATES
        _set("alpha", {"monthly_budget_usd": 5})
        _spend(monkeypatch, None)
        assert "spend not measured yet" in release_gates.project_budget_gate(db, "alpha")[
            "detail"]
        _spend(monkeypatch, 6.0)
        gate = release_gates.project_budget_gate(db, "alpha")
        assert gate["ok"] is False and gate["spent_usd"] == 6.0 and gate["budget_usd"] == 5.0
    finally:
        db.close()


# ── the budget alert ──────────────────────────────────────────────────────────


def _budget_rules() -> dict[str, AlertRule]:
    db = SessionLocal()
    try:
        with project_context(None):
            return {r.name: r for r in db.scalars(select(AlertRule).where(
                AlertRule.name.in_(list(alerts.BUDGET_RULES))))}
    finally:
        db.close()


def test_the_budget_keeps_an_80_and_a_100_percent_rule(world):
    res = world.admin.put("/api/projects/alpha/quotas",
                          json={"quotas": {"monthly_budget_usd": 200}})
    assert res.status_code == 200, res.text
    rules = _budget_rules()
    assert {name: (r.project_id, r.workspace_id, r.kind, r.threshold)
            for name, r in rules.items()} == {
        alerts.BUDGET_WARN_RULE_NAME: ("alpha", WS, "cost_mtd_usd", 160.0),
        alerts.BUDGET_RULE_NAME: ("alpha", WS, "cost_mtd_usd", 200.0)}
    world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"monthly_budget_usd": 50}})
    assert {n: r.threshold for n, r in _budget_rules().items()} == {
        alerts.BUDGET_WARN_RULE_NAME: 40.0, alerts.BUDGET_RULE_NAME: 50.0}
    world.admin.put("/api/projects/alpha/quotas", json={"quotas": {"monthly_budget_usd": None}})
    assert _budget_rules() == {}


@pytest.mark.parametrize(("spent", "warn", "full"), [
    (7.9, "ok", "ok"), (8.0, "firing", "ok"), (10.0, "firing", "firing")])
def test_the_budget_rules_fire_at_80_and_100_percent(world, monkeypatch, spent, warn, full):
    _set("alpha", {"monthly_budget_usd": 10})
    seen: list[str | None] = []

    def spend(db, workspace, *, force=False, project=None):
        seen.append(project)
        return spent

    monkeypatch.setattr(alerts.costs, "month_to_date_usd", spend)
    ctx = WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})
    db = SessionLocal()
    try:
        result = alerts.evaluate_rules(db, ctx, notify_transitions=False)  # creates the rules
    finally:
        db.close()
    states = {r["name"]: (r["state"], r["last_value"]) for r in result["rules"] if r["budget"]}
    assert states == {alerts.BUDGET_WARN_RULE_NAME: (warn, spent),
                      alerts.BUDGET_RULE_NAME: (full, spent)}
    assert seen == ["alpha"]  # two projects share the workspace; both rules, one read
    items = {i["key"]: i for i in world.dev.get("/api/projects/alpha/quotas").json()["items"]}
    assert (items["monthly_budget_usd"]["used"], items["monthly_budget_usd"]["state"]) == (
        spent, full)
    # the budget cleared: the next pass removes the rules
    _set("alpha", None)
    db = SessionLocal()
    try:
        alerts.evaluate_rules(db, ctx, notify_transitions=False)
    finally:
        db.close()
    assert _budget_rules() == {}
