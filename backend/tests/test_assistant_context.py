# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""assistant-context C1 (the replay window the console shows) and C5 (the architect's
turn budget, the claim TTL derived from it, and the migration of stored budgets)."""

from datetime import UTC, datetime, timedelta

from app.assistant import service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.assistant import AssistantConversation, AssistantMessage
from app.models.ledger import Agent, AuditEvent, Job, Workspace
from app.system_agents import presets, timeout_migration
from app.system_agents import service as system_service
from app.system_agents.presets import ARCHITECT, PresetEdit

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    BASE,
    _latest,
    _mark_ready,
    _open,
    _turn,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

# ---------------------------------------------------------------------------
# C1: the replay window
# ---------------------------------------------------------------------------


def _seed_turns(cid: str, count: int, *, start: int = 1, size: int = 10) -> None:
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        for turn in range(start, start + count):
            db.add(AssistantMessage(workspace_id=row.workspace_id, conversation_id=cid,
                                    turn=turn, role="user", text=f"q{turn} " + "x" * size))
            db.add(AssistantMessage(workspace_id=row.workspace_id, conversation_id=cid,
                                    turn=turn, role="assistant", text=f"a{turn}"))
        row.turns = start + count - 1
        db.commit()
    finally:
        db.close()


def test_a_fresh_conversation_reports_an_empty_window(client, ready):
    cid = _open(client)
    detail = _latest(client, cid)
    ctx = detail["context"]
    assert (ctx["replayed_turns"], ctx["omitted_turns"], ctx["first_replayed_turn"]) == (0, 0, None)
    assert 0 < ctx["budget_used_ratio"] < 1  # the preamble alone
    listed = client.get(f"{BASE}/conversations").json()["conversations"]
    assert [c["context_trimmed"] for c in listed if c["id"] == cid] == [False]


def test_detail_reports_the_next_turns_window_after_a_reload(client, ready):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 2)
    ctx = _latest(client, cid)["context"]
    assert ctx["replayed_turns"] == service.MAX_REPLAY_TURNS
    assert ctx["omitted_turns"] == 2
    assert ctx["first_replayed_turn"] == 3  # the divider goes before turn 3
    listed = client.get(f"{BASE}/conversations").json()["conversations"]
    assert [c["context_trimmed"] for c in listed if c["id"] == cid] == [True]


def test_the_turn_stream_carries_the_window_it_replays(client, ready, harness):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 1)
    harness.reply("ok")
    meta = _turn(client, cid, "next")[0]
    assert meta[0] == "meta"
    assert meta[1]["omitted_turns"] == 1 and meta[1]["first_replayed_turn"] == 2
    assert meta[1]["context"]["replayed_turns"] == service.MAX_REPLAY_TURNS
    # after that turn the NEXT window starts one turn later
    assert _latest(client, cid)["context"]["first_replayed_turn"] == 3


def test_the_character_budget_also_trims_and_the_list_marker_sees_it(client, ready):
    cid = _open(client)
    _seed_turns(cid, 4, size=service.MAX_REPLAY_CHARS // 3)
    ctx = _latest(client, cid)["context"]
    assert ctx["omitted_turns"] >= 2 and ctx["budget_used_ratio"] <= 1
    listed = client.get(f"{BASE}/conversations").json()["conversations"]
    assert [c["context_trimmed"] for c in listed if c["id"] == cid] == [True]


def test_retried_turns_count_toward_neither_the_window_nor_the_marker(client, ready):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 1)
    db = SessionLocal()
    try:
        db.add(AssistantMessage(workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=cid, turn=1,
                                role="error", name=service.TURN_RETRIED_NAME, text="retried"))
        db.commit()
    finally:
        db.close()
    ctx = _latest(client, cid)["context"]
    assert (ctx["omitted_turns"], ctx["first_replayed_turn"]) == (0, 2)
    listed = client.get(f"{BASE}/conversations").json()["conversations"]
    assert [c["context_trimmed"] for c in listed if c["id"] == cid] == [False]


# ---------------------------------------------------------------------------
# C5: the claim TTL follows the turn budget
# ---------------------------------------------------------------------------


def test_claim_ttl_is_derived_from_the_turn_budget():
    assert service.claim_ttl_s(1500) == 3330
    assert service.claim_ttl_s(3600) == 7530 == service.MAX_TURN_CLAIM_TTL_S


def _set_budget(agent_id: str, seconds: int | None) -> None:
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        spec = dict(agent.spec or {})
        spec.pop("timeout_seconds", None)
        if seconds is not None:
            spec["timeout_seconds"] = seconds
        agent.spec = spec
        db.commit()
    finally:
        db.close()


def _age_claim(cid: str, seconds: float) -> None:
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        row.active_turn, row.turns = row.turns + 1, row.turns + 1
        row.active_turn_token = "orphan"
        row.active_turn_started_at = datetime.now(UTC) - timedelta(seconds=seconds)
        db.commit()
    finally:
        db.close()


def test_a_1500s_turn_with_one_replay_is_never_taken_over(client, ready, harness):
    _set_budget(ready, 1500)
    cid = _open(client)
    # first attempt + transient replay each ran (almost) their whole budget
    _age_claim(cid, 2 * 1500 + service.RETRY_RECORD_WAIT_S + 60)
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "x"})
    assert res.status_code == 409 and res.json()["code"] == "assistant.turn_in_progress"
    db = SessionLocal()
    try:  # past the derived TTL it is an orphan
        db.get(AssistantConversation, cid).active_turn_started_at = (
            datetime.now(UTC) - timedelta(seconds=service.claim_ttl_s(1500) + 5))
        db.commit()
    finally:
        db.close()
    harness.reply("recovered")
    assert _turn(client, cid, "again")[-1][0] == "done"


def test_a_conversation_without_a_readable_budget_falls_back_to_the_old_ttl(client, ready):
    _set_budget(ready, None)
    cid = _open(client)
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        assert service._turn_claim_ttl(db, row) == service.TURN_CLAIM_TTL_S
        _set_budget(ready, 600)
        db.expire_all()
        assert service._turn_claim_ttl(db, db.get(AssistantConversation, cid)) == (
            service.claim_ttl_s(600))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# C5: default + migration of stored budgets
# ---------------------------------------------------------------------------


def test_the_architect_default_budget_is_1500_and_900_is_its_legacy_default():
    assert ARCHITECT.timeout_seconds == 1500
    assert presets.timeout_migration_target(ARCHITECT, {"timeout_seconds": 900}) == 1500
    assert presets.timeout_migration_target(ARCHITECT, {"timeout_seconds": 1200}) is None
    assert presets.timeout_migration_target(ARCHITECT, {"timeout_seconds": 1500}) is None
    assert presets.timeout_migration_target(ARCHITECT, {}) is None
    assert presets.timeout_migration_target(ARCHITECT, None) is None


def _installed_with(timeout: int) -> str:
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        outcome = system_service.install_preset(
            db, row, ARCHITECT, PresetEdit(values={"timeout_seconds": timeout}))
        db.commit()
        agent = db.get(Agent, outcome.agent.id)
        agent.status = "active"  # the install job finished
        db.commit()
        return agent.id
    finally:
        db.close()


def _spec(agent_id: str) -> dict:
    db = SessionLocal()
    try:
        return dict(db.get(Agent, agent_id).spec)
    finally:
        db.close()


def test_migration_moves_only_the_legacy_default_and_is_idempotent(client):
    _mark_ready()
    agent_id = _installed_with(900)
    started: list[str] = []
    jobs = timeout_migration.migrate_preset_timeouts(start=started.append)
    assert len(jobs) == 1 and started == jobs
    spec = _spec(agent_id)
    assert spec["timeout_seconds"] == 1500
    db = SessionLocal()
    try:
        job = db.get(Job, jobs[0])
        assert job.type == "deploy_agent" and db.get(Agent, agent_id).status == "deploying"
        audit = db.query(AuditEvent).filter(AuditEvent.target == agent_id).all()
        assert len(audit) == 1 and audit[0].action.startswith(timeout_migration.AUDIT_ACTION)
        assert "900→1500" in audit[0].action and audit[0].actor == "system"
        db.get(Agent, agent_id).status = "active"  # the re-publish finished
        db.commit()
    finally:
        db.close()
    # a second start finds nothing to do
    assert timeout_migration.migrate_preset_timeouts(start=started.append) == []
    assert started == jobs


def test_migration_keeps_an_administrators_budget(client):
    _mark_ready()
    agent_id = _installed_with(1200)
    assert timeout_migration.migrate_preset_timeouts(start=lambda job_id: None) == []
    assert _spec(agent_id)["timeout_seconds"] == 1200


def test_migration_skips_a_preset_with_a_job_in_flight_and_retries_later(client):
    _mark_ready()
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        outcome = system_service.install_preset(
            db, row, ARCHITECT, PresetEdit(values={"timeout_seconds": 900}))
        db.commit()
        agent_id = outcome.agent.id  # still "deploying": the install job runs
    finally:
        db.close()
    assert timeout_migration.migrate_preset_timeouts(start=lambda job_id: None) == []
    assert _spec(agent_id)["timeout_seconds"] == 900
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).status = "active"
        db.commit()
    finally:
        db.close()
    assert len(timeout_migration.migrate_preset_timeouts(start=lambda job_id: None)) == 1
    assert _spec(agent_id)["timeout_seconds"] == 1500
