"""Projects phase 1 (design §5.12) for the background tick: a scheduled watch runs —
and writes — as its agent's project and is skipped while that project is archived;
alert rules are evaluated per workspace, a project rule over its own project's agents
only, and an archived project's rules keep the state they last saw."""

from sqlalchemy import select, text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project, project_context
from app.dlc import scheduler
from app.dlc import watch as watch_svc
from app.models.dlc import WatchConfig
from app.models.ledger import Agent, AlertRule
from app.services import alerts as alert_service
from tests.conftest import TEST_PROJECT
from tests.test_project_scope import _project

WS = DEFAULT_WORKSPACE_ID


def _agent(name: str, pid: str) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=WS, name=name, method="harness", status="active",
                        spec={"name": name})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _watch(agent_id: str) -> str:
    db = SessionLocal()
    try:
        row = WatchConfig(workspace_id=WS, agent_id=agent_id, enabled=True, every="daily")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _rule(kind: str, pid: str | None, *, state: str = "ok") -> str:
    db = SessionLocal()
    try:
        with project_context(None):
            row = AlertRule(workspace_id=WS, kind=kind, threshold=0.5, project_id=pid,
                            comparison=alert_service.NATURAL_COMPARISON[kind],
                            state=state)
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _archive(pid: str) -> None:
    db = SessionLocal()
    try:
        db.execute(text("UPDATE projects SET status = 'archived' WHERE id = :p"), {"p": pid})
        db.commit()
    finally:
        db.close()


# ── dlc.watch ─────────────────────────────────────────────────────────────────


def test_a_watch_runs_as_its_agents_project(monkeypatch):
    _project("alpha")
    agent = _agent("watched", "alpha")
    config_id = _watch(agent)
    # a config row left with a stale project (the agent moved): the agent decides
    db = SessionLocal()
    try:
        db.execute(text("UPDATE watch_configs SET project_id = :p WHERE id = :i"),
                   {"p": TEST_PROJECT, "i": config_id})
        db.commit()
    finally:
        db.close()
    ran: list[tuple[str, str | None]] = []
    monkeypatch.setattr(watch_svc, "run_now", lambda db, config, **_: (
        ran.append((config.agent_id, current_project())), "run-1")[1])
    db = SessionLocal()
    try:
        out = scheduler.run_due_watches(db)
    finally:
        db.close()
    assert ran == [(agent, "alpha")]
    assert out["started"] == [{"agent_id": agent, "run_id": "run-1"}]
    assert current_project() is None  # the tick itself stays unscoped


def test_an_archived_projects_watch_is_skipped_and_rescheduled(monkeypatch):
    _project("alpha")
    agent = _agent("frozen", "alpha")
    config_id = _watch(agent)
    _archive("alpha")
    monkeypatch.setattr(watch_svc, "run_now", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("an archived project's watch must not run")))
    db = SessionLocal()
    try:
        out = scheduler.run_due_watches(db)
        config = db.get(WatchConfig, config_id)
        assert (config.last_status, config.last_detail) == ("skipped", "project archived")
        assert config.next_due_at is not None
    finally:
        db.close()
    assert out == {"started": [],
                   "skipped": [{"agent_id": agent, "reason": "project.archived"}]}
    # no longer due: the next tick does not even look at it
    db = SessionLocal()
    try:
        assert [c.id for c in watch_svc.due_configs(db)] == []
    finally:
        db.close()


def test_a_single_project_workspace_watch_runs_as_default(monkeypatch):
    agent = _agent("solo", TEST_PROJECT)
    _watch(agent)
    ran: list[str | None] = []
    monkeypatch.setattr(watch_svc, "run_now", lambda db, config, **_: (
        ran.append(current_project()), "run-2")[1])
    db = SessionLocal()
    try:
        out = scheduler.run_due_watches(db)
    finally:
        db.close()
    assert ran == [TEST_PROJECT] and out["skipped"] == []


# ── dlc.alerts ────────────────────────────────────────────────────────────────


def test_scheduled_alerts_read_each_rule_as_its_own_project(monkeypatch):
    """A project rule's online quality is computed over that project's agents only; a
    workspace rule over every project's."""
    from app.evaluation import online

    _project("alpha")
    mine = _agent("mine", "alpha")
    theirs = _agent("theirs", TEST_PROJECT)
    workspace_rule = _rule("online_quality", None)
    project_rule = _rule("online_quality", "alpha")
    seen: dict[str | None, set[str]] = {}

    def quality(db, workspace, **_):
        seen[current_project()] = set(db.scalars(select(Agent.id)).all())
        return {"mean": 0.9, "scores": 1, "sessions": 1}

    monkeypatch.setattr(online, "online_quality", quality)
    db = SessionLocal()
    try:
        out = scheduler.evaluate_alerts(db)
        states = {r.id: r.state for r in db.scalars(select(AlertRule)).all()}
    finally:
        db.close()
    assert seen == {None: {mine, theirs}, "alpha": {mine}}
    assert out["workspaces"] == [{"workspace_id": WS, "firing": 0}]
    assert states == {workspace_rule: "ok", project_rule: "ok"}


def test_an_archived_projects_rules_keep_their_state(monkeypatch):
    _project("alpha")
    _project("beta")
    frozen = _rule("error_rate", "alpha", state="firing")
    live = _rule("error_rate", "beta")
    _archive("alpha")
    read: list[str | None] = []
    monkeypatch.setattr(alert_service, "_dashboard_value", lambda kind, window, db, ws: (
        read.append(current_project()), (0.9, "9/10 traces failed"))[1])
    posted: list[dict] = []
    monkeypatch.setattr(alert_service, "_post_webhook",
                        lambda url, payload: (posted.append(payload), "HTTP 200")[1])
    db = SessionLocal()
    try:
        with project_context(None):
            db.get(AlertRule, live).webhook_url = "https://hooks.example/x"
            db.commit()
        out = scheduler.evaluate_alerts(db)
        rows = {r.id: r for r in db.scalars(select(AlertRule)).all()}
    finally:
        db.close()
    assert read == ["beta"]
    assert rows[frozen].state == "firing" and rows[frozen].last_checked_at is None
    assert rows[live].state == "firing"
    assert out["workspaces"] == [{"workspace_id": WS, "firing": 2}]
    # the webhook names the rule's project, so a shared channel can route it
    assert [(p["rule_id"], p["project_id"]) for p in posted] == [(live, "beta")]


def test_evaluate_rules_reports_an_archived_projects_rule_as_skipped():
    from app.services.workspace import context_for_workspace

    _project("alpha")
    rule = _rule("error_rate", "alpha")
    _archive("alpha")
    db = SessionLocal()
    try:
        result = alert_service.evaluate_rules(db, context_for_workspace(WS))
    finally:
        db.close()
    assert [(r["id"], r.get("skipped")) for r in result["rules"]] == [
        (rule, "project_archived")]
