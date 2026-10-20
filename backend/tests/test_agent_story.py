"""The storyline reads (storyline-ux §2.3): ``GET /api/agents/{id}/story`` and
``GET /api/me/next-steps``.

Asserted: the route classification; every fact the console's storyline needs is
present and read from the ledger (criteria draft / published / signed, golden sealed,
dev run, endpoint mode, candidate, gate report, release live, API key, Cedar mode,
spans, alert rules, issues, golden items from feedback); the 30-second cache; another
workspace's or project's agent answers 404; and "my next steps" filters by the
caller's workshop role and keys — developer, biz-owner, release-ops, sme, admin.
"""

from datetime import UTC, datetime

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.core.route_policy import (
    MEMBER,
    PERM_PROJECT_USE,
    WORKSPACE_EXEMPT,
    required_role,
)
from app.evaluation.models import EvalDataset, EvalRun
from app.models.dlc import AdmissionCandidate, CriteriaSet, ReleaseRecord
from app.models.ledger import Agent, AlertRule, ApiKey, ChatSession
from app.models.selfservice import Issue
from app.services import agent_story as story
from tests.conftest import TEST_PROJECT
from tests.test_project_scope import _login, _member_of, _project, _user, app  # noqa: F401

DEFAULT = {"X-Project": TEST_PROJECT}


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    story.reset_cache_for_tests()
    calls = {"cedar": 0}

    def cedar(_ws):
        calls["cedar"] += 1
        return calls.get("mode")

    monkeypatch.setattr(story, "_read_cedar", cedar)
    yield calls
    story.reset_cache_for_tests()


def _add(*rows, project: str | None = TEST_PROJECT):
    db = SessionLocal()
    try:
        with project_context(project):
            for row in rows:
                db.add(row)
                db.flush()
            db.commit()
            return [r.id for r in rows]
    finally:
        db.close()


def _agent(name="lab", *, project=TEST_PROJECT, workspace_id=DEFAULT_WORKSPACE_ID, **fields) -> str:
    base = {"workspace_id": workspace_id, "name": name, "method": "harness", "status": "active",
            "resource_id": "h-1", "version": "1", "spec": {}}
    return _add(Agent(**{**base, **fields}), project=project)[0]


def _story(client, agent_id, headers=DEFAULT):
    res = client.get(f"/api/agents/{agent_id}/story", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def test_routes_are_classified():
    assert required_role("GET", "/api/agents/{agent_id}/story") == PERM_PROJECT_USE
    assert required_role("GET", "/api/me/next-steps") == MEMBER
    assert ("GET", "/api/me/next-steps") in WORKSPACE_EXEMPT


def test_an_empty_agent_has_every_fact_and_none_done(client):
    body = _story(client, _agent())
    facts = body["facts"]
    for key in ("agent_active", "sessions", "knowledge_bases", "skills", "criteria_draft",
                "criteria_published", "criteria_signed", "golden_sealed", "golden_from_feedback",
                "dev_run_done", "endpoint_mode", "candidate_version", "gate_verdict",
                "release_live", "api_keys", "cedar_mode", "spans_7d", "alert_rules",
                "issues_open", "issues_resolved", "calibrated", "waivers_pending"):
        assert key in facts, key
    assert facts["agent_active"] is True
    assert facts["sessions"] == 0 and facts["criteria_draft"] is False
    assert facts["endpoint_mode"] == "default" and facts["spans_7d"] is None
    assert body["agent"]["kind"] == "agent" and body["cached"] is False
    assert story.current_step(facts).key == "build.chat"


def test_facts_follow_the_ledger(client, fresh_cache):
    fresh_cache["mode"] = "ENFORCE"
    agent_id = _agent(endpoint_mode="live")
    _add(
        ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, session_id="s-1"),
        CriteriaSet(workspace_id=DEFAULT_WORKSPACE_ID, lineage_id="lin-1", kind="agent",
                    agent_id=agent_id, name="c", version=1, status="published",
                    signed_by="biz"),
        CriteriaSet(workspace_id=DEFAULT_WORKSPACE_ID, lineage_id="lin-1", kind="agent",
                    agent_id=agent_id, name="c", version=2, status="draft"),
        EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name="lab",
                status="completed", split="dev"),
        ReleaseRecord(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                      candidate_version="2", decision="pending",
                      gate_report={"verdict": "PASS"}),
        ReleaseRecord(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                      candidate_version="1", decision="released"),
        ApiKey(workspace_id=DEFAULT_WORKSPACE_ID, name="k", prefix="lp_", key_hash="h" * 64,
               agent_ids=[agent_id]),
        ApiKey(workspace_id=DEFAULT_WORKSPACE_ID, name="other", prefix="lp_", key_hash="i" * 64,
               agent_ids=["someone-else"]),
        AlertRule(workspace_id=DEFAULT_WORKSPACE_ID, kind="error_rate", threshold=5.0),
        Issue(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, status="fixed"),
        AdmissionCandidate(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                           source="issue", source_ref="1", status="admitted"),
    )
    parent = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="g", role="golden",
                         criteria_set_id="lin-1")
    pid = _add(parent)[0]
    _add(EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="g·holdout", role="golden",
                     criteria_set_id="lin-1", split_of=pid, split="holdout",
                     items=[{"scenario_id": "h1"}]))
    facts = _story(client, agent_id)["facts"]
    assert facts["sessions"] == 1
    assert facts["criteria_published"] and facts["criteria_signed"]
    assert facts["criteria_version"] == 1 and facts["criteria_draft_version"] == 2
    assert facts["golden_id"] == pid and facts["golden_sealed"] is True
    assert facts["golden_items"] == 1 and facts["golden_from_feedback"] == 1
    assert facts["dev_run_done"] is True
    assert facts["candidate_version"] == "2" and facts["gate_verdict"] == "PASS"
    assert facts["release_live"] is True
    assert facts["api_keys"] == 1  # the other key is scoped to another agent
    assert facts["cedar_mode"] == "ENFORCE"
    assert facts["alert_rules"] == 1
    assert facts["issues_open"] == 0 and facts["issues_resolved"] == 1
    assert story.current_step(facts).key == "observe.traces"


def test_spans_come_from_the_telemetry_probe_cache(client):
    import time

    from app.routers import agent_telemetry

    agent_id = _agent()
    agent_telemetry.reset_cache_for_tests()
    with agent_telemetry._cache_lock:
        agent_telemetry._cache[(DEFAULT_WORKSPACE_ID, agent_id, "h-1", "1", "DEFAULT")] = (
            time.monotonic() + 60, {"status": "receiving", "last_span_within": "24h"})
    try:
        assert _story(client, agent_id)["facts"]["spans_7d"] is True
    finally:
        agent_telemetry.reset_cache_for_tests()


def test_cached_for_thirty_seconds(client, fresh_cache, monkeypatch):
    agent_id = _agent()
    assert _story(client, agent_id)["cached"] is False
    _add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, session_id="s-1"))
    again = _story(client, agent_id)
    assert again["cached"] is True and again["facts"]["sessions"] == 0
    assert fresh_cache["cedar"] == 1
    later = story.time.monotonic() + story.CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(story.time, "monotonic", lambda: later)
    fresh = _story(client, agent_id)
    assert fresh["cached"] is False and fresh["facts"]["sessions"] == 1


def test_another_workspace_or_project_answers_404(client):
    foreign = _agent(workspace_id="acct-other")
    res = client.get(f"/api/agents/{foreign}/story", headers=DEFAULT)
    assert res.status_code == 404 and res.json()["code"] == "agent.not_found"
    _project("alpha")
    theirs = _agent(project="alpha")
    res = client.get(f"/api/agents/{theirs}/story", headers=DEFAULT)
    assert res.status_code == 404 and res.json()["code"] == "agent.not_found"
    alpha = client.get(f"/api/agents/{theirs}/story", headers={"X-Project": "alpha"})
    assert alpha.status_code == 200
    assert client.get("/api/agents/nope/story", headers=DEFAULT).status_code == 404


def test_another_projects_rows_do_not_count(client):
    """A row of project alpha naming default's agent id stays invisible to default."""
    _project("alpha")
    agent_id = _agent()
    _add(Issue(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, status="open",
               project_id="alpha"), project="alpha")
    assert _story(client, agent_id)["facts"]["issues_open"] == 0


def test_mcp_servers_and_presets_are_not_on_the_storyline(client):
    mcp = _agent("mcp", spec={"protocol": "mcp"})
    assert _story(client, mcp)["agent"]["kind"] == "mcp"
    body = client.get("/api/me/next-steps", headers=DEFAULT).json()
    assert all(row["agent"]["id"] != mcp for row in body["mine"] + body["waiting"])


# ── whose turn: the five workshop accounts ─────────────────────────────────────


def _criteria_published(agent_id: str) -> None:
    """build.* done; the current step is evaluate.criteria.sign (biz-owner's)."""
    _add(
        ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, session_id="s"),
        CriteriaSet(workspace_id=DEFAULT_WORKSPACE_ID, lineage_id="lin-1", kind="agent",
                    agent_id=agent_id, name="c", version=1, status="published",
                    published_at=datetime.now(UTC)),
    )


ACCOUNTS = [
    # (username, project role, the agent's step is theirs?)
    ("dev", "developer", False),
    ("biz", "biz_owner", True),
    ("ops", "release_ops", False),
    ("sme", "sme", False),
]


@pytest.mark.parametrize(("username", "role", "theirs"), ACCOUNTS)
def test_next_steps_filter_by_workshop_role(app, username, role, theirs):  # noqa: F811
    agent_id = _agent()
    _criteria_published(agent_id)
    uid = _user(username, grant=False)
    _member_of(TEST_PROJECT, uid, role)
    client = _login(app, username)
    res = client.get("/api/me/next-steps", headers=DEFAULT)
    assert res.status_code == 200, res.text
    body = res.json()
    mine = [r for r in body["mine"] if r["agent"]["id"] == agent_id]
    waiting = [r for r in body["waiting"] if r["agent"]["id"] == agent_id]
    assert bool(mine) is theirs and bool(waiting) is not theirs
    row = (mine or waiting)[0]
    assert row["step"] == "evaluate.criteria.sign" and row["roles"] == ["biz-owner"]
    assert body["project_id"] == TEST_PROJECT


def test_the_developer_owns_the_first_steps(app):  # noqa: F811
    agent_id = _agent()  # no conversation yet: build.chat
    _member_of(TEST_PROJECT, _user("dev", grant=False), "developer")
    body = _login(app, "dev").get("/api/me/next-steps", headers=DEFAULT).json()
    assert [r["step"] for r in body["mine"] if r["agent"]["id"] == agent_id] == ["build.chat"]
    assert body["roles"] == ["developer"]


def test_the_admin_sees_every_current_step_as_theirs(client):
    agent_id = _agent()
    _criteria_published(agent_id)
    body = client.get("/api/me/next-steps", headers=DEFAULT).json()
    assert body["roles"] == ["admin"]
    assert [r["step"] for r in body["mine"]] == ["evaluate.criteria.sign"]
    assert body["waiting"] == []


def test_next_steps_stay_inside_the_callers_project(app):  # noqa: F811
    _project("alpha")
    theirs = _agent("alpha-agent", project="alpha")
    ours = _agent("default-agent")
    _member_of(TEST_PROJECT, _user("dev", grant=False), "developer")
    body = _login(app, "dev").get("/api/me/next-steps", headers=DEFAULT).json()
    ids = {r["agent"]["id"] for r in body["mine"] + body["waiting"]}
    assert ours in ids and theirs not in ids


def test_the_pure_turn_rule():
    step = next(s for s in story.AGENT_STEPS if s.key == "release.sign")
    holds = {"release.sign"}.__contains__
    assert story.is_my_turn(step, roles=frozenset({"release-ops"}), can=holds)
    assert not story.is_my_turn(step, roles=frozenset({"developer"}), can=holds)
    assert not story.is_my_turn(step, roles=frozenset({"release-ops"}), can=lambda _k: False)
    assert story.is_my_turn(step, roles=frozenset(), can=holds)  # custom role: keys alone
    alerts = next(s for s in story.AGENT_STEPS if s.key == "observe.alerts")
    assert not story.is_my_turn(alerts, roles=frozenset({"release-ops"}),
                                can={"project.write"}.__contains__)
