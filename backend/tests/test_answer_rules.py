"""T35 -- curated answers: matching, ordering, off switches, the invoke short-circuit and
its interaction with the T12 PII screen."""

import pytest
from fastapi.testclient import TestClient

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import AuditEvent, ChatMessage
from app.models.selfservice import AnswerRule
from app.services import answer_rules
from app.services import chat as chat_service
from app.services import invoke as invoke_service
from tests.test_guardrail import MASKED, RAW, FakeBedrock, FakeRuntime, _ctx
from tests.test_share_links import make_agent, sse


@pytest.fixture
def client():
    return TestClient(create_app())


def add_rule(client, agent_id, pattern="what is the leave policy", answer="25 days.",
             **extra):
    res = client.post(f"/api/agents/{agent_id}/rules",
                      json={"pattern": pattern, "answer": answer, **extra})
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def no_model(monkeypatch):
    """Any dispatch to the model is a failure: the rule must have answered."""
    def boom(*a, **k):  # pragma: no cover - only reached on a bug
        raise AssertionError("the model was called")

    monkeypatch.setattr(invoke_service, "_dispatch_invoke", boom)


# ── matching ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("match,pattern,question,expected", [
    ("exact", "What is the leave policy?", "what's the LEAVE policy", False),
    ("exact", "What is the leave policy?", "  what is the   leave policy ?? ", True),
    ("exact", "What is the leave policy?", "what is the leave policy for interns", False),
    ("contains", "leave policy", "Tell me the leave policy please", True),
    ("contains", "leave policy", "leave-policy", True),
    ("contains", "leave", "she may leaves early", False),  # whole words for Latin text
    ("contains", "年假", "请问公司的年假政策是什么？", True),  # substring for CJK
    ("exact", "年假政策是什么？", "年假政策是什么?", True),  # full-width vs ASCII mark
    ("exact", "", "anything", False),
])
def test_matching_is_normalised_and_predictable(match, pattern, question, expected):
    assert answer_rules.matches(match, pattern, question) is expected


def test_a_contains_pattern_that_would_match_too_much_is_refused():
    with pytest.raises(AppError) as exc:
        answer_rules.validate_pattern("contains", "hi")
    assert exc.value.code == "rule.pattern_too_short"
    assert answer_rules.validate_pattern("contains", "年假") == "年假"
    with pytest.raises(AppError):
        answer_rules.validate_pattern("fuzzy", "anything at all")


# ── console API: CRUD, ordering, switches ───────────────────────────────────


def test_rules_are_ordered_toggled_and_audited(client):
    agent_id = make_agent()
    first = add_rule(client, agent_id, pattern="leave policy", match="contains", answer="A")
    second = add_rule(client, agent_id, pattern="leave policy", match="contains", answer="B")
    assert [first["position"], second["position"]] == [0, 1]
    assert answer_rules.dry_run(SessionLocal(), "default", agent_id,
                                "the leave policy")["rule"]["id"] == first["id"]

    # reorder: the specific rule now wins
    res = client.put(f"/api/agents/{agent_id}/rules-order",
                     json={"ids": [second["id"], first["id"]]})
    assert [r["id"] for r in res.json()["rules"]] == [second["id"], first["id"]]
    test = client.post(f"/api/agents/{agent_id}/rules/test", json={"question": "leave policy"})
    assert test.json()["rule"]["id"] == second["id"]
    # a partial reorder is refused rather than silently dropping a rule
    assert client.put(f"/api/agents/{agent_id}/rules-order",
                      json={"ids": [second["id"]]}).status_code == 422

    # per-rule off switch: the next enabled rule answers
    client.patch(f"/api/agents/{agent_id}/rules/{second['id']}", json={"enabled": False})
    assert client.post(f"/api/agents/{agent_id}/rules/test",
                       json={"question": "leave policy"}).json()["rule"]["id"] == first["id"]
    # per-agent off switch: nothing matches, but the rules are kept
    body = client.put(f"/api/agents/{agent_id}/rules-enabled", json={"enabled": False}).json()
    assert body["enabled"] is False and len(body["rules"]) == 2
    assert client.post(f"/api/agents/{agent_id}/rules/test",
                       json={"question": "leave policy"}).json()["matched"] is False

    assert client.delete(f"/api/agents/{agent_id}/rules/{first['id']}").json() == {"deleted": True}
    db = SessionLocal()
    try:
        actions = {e.action for e in db.query(AuditEvent)}
        assert {"rule.create", "rule.update", "rule.reorder", "rule.disable",
                "rule.delete"} <= actions
    finally:
        db.close()


def test_rules_are_workspace_and_preset_scoped(client):
    other = make_agent(name="other")
    db = SessionLocal()
    try:
        from app.models.ledger import Agent

        preset = Agent(workspace_id="default", name="preset", method="harness",
                       status="active", system_key="architect", spec={"name": "preset"})
        db.add(preset)
        db.commit()
        preset_id = preset.id
    finally:
        db.close()
    assert client.get(f"/api/agents/{preset_id}/rules").status_code == 409
    assert client.get("/api/agents/nope/rules").status_code == 404
    rule = add_rule(client, other)
    # another agent's rule id is not addressable through this agent
    mine = make_agent(name="mine")
    assert client.patch(f"/api/agents/{mine}/rules/{rule['id']}",
                        json={"enabled": False}).status_code == 404


# ── the invoke chain ────────────────────────────────────────────────────────


def _agent(client, **spec):
    agent_id = make_agent()
    if spec:
        from app.models.ledger import Agent

        db = SessionLocal()
        try:
            row = db.get(Agent, agent_id)
            row.spec = {**row.spec, **spec}
            db.commit()
        finally:
            db.close()
    return agent_id


def _load(agent_id):
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def test_a_match_short_circuits_the_model_and_says_so(client, no_model):
    agent_id = _agent(client)
    rule = add_rule(client, agent_id)
    out = invoke_service.invoke_agent_text(_load(agent_id), "What is the leave policy?")
    assert out["text"] == "25 days." and out["answered_by"] == "rule"
    assert out["rule"]["id"] == rule["id"] and out["session_id"]
    listed = client.get(f"/api/agents/{agent_id}/rules").json()["rules"][0]
    assert listed["hit_count"] == 1 and listed["last_hit_at"]


def test_no_match_a_disabled_rule_or_attachments_fall_through(client, monkeypatch):
    agent_id = _agent(client)
    rule = add_rule(client, agent_id)
    seen = []
    monkeypatch.setattr(invoke_service, "_dispatch_invoke",
                        lambda agent, prompt, **k: seen.append(prompt) or {"text": "model"})
    agent = _load(agent_id)
    assert invoke_service.invoke_agent_text(agent, "something else")["text"] == "model"
    client.patch(f"/api/agents/{agent_id}/rules/{rule['id']}", json={"enabled": False})
    assert invoke_service.invoke_agent_text(agent, "What is the leave policy?")["text"] == "model"
    client.patch(f"/api/agents/{agent_id}/rules/{rule['id']}", json={"enabled": True})

    class Att:  # a turn with a file depends on the file: the rule must not answer
        native = None
        metadata = []

        def prompt(self, text):
            return text + " [file]"

    out = invoke_service.invoke_agent_text(agent, "What is the leave policy?",
                                           attachments=Att())
    assert out["text"] == "model" and len(seen) == 3


def test_a_broken_rule_lookup_never_takes_the_agent_offline(client, monkeypatch):
    agent_id = _agent(client)
    monkeypatch.setattr(answer_rules, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert answer_rules.match_for_agent(_load(agent_id), "anything") is None


def test_block_mode_still_refuses_pii_before_a_rule_can_answer(client, monkeypatch, no_model):
    agent_id = _agent(client, guardrail={"enabled": True, "mode": "block"})
    add_rule(client, agent_id, pattern=RAW, answer="curated")
    ctx = _ctx(FakeBedrock(), FakeRuntime(intervene=True))
    monkeypatch.setattr(invoke_service, "_agent_workspace", lambda agent, ws: ctx)
    with pytest.raises(AppError) as exc:
        invoke_service.invoke_agent_text(_load(agent_id), RAW)
    assert exc.value.code == "guardrail.blocked"


def test_anonymize_matches_the_masked_text_and_does_not_screen_the_answer(
    client, monkeypatch, no_model
):
    agent_id = _agent(client, guardrail={"enabled": True, "mode": "anonymize"})
    # the owner writes the rule against the text the agent will actually see
    add_rule(client, agent_id, pattern=MASKED, answer="Call hr@example.com or 555-0199.")
    runtime = FakeRuntime(intervene=True)
    monkeypatch.setattr(invoke_service, "_agent_workspace",
                        lambda agent, ws: _ctx(FakeBedrock(), runtime))
    out = invoke_service.invoke_agent_text(_load(agent_id), RAW)
    assert out["answered_by"] == "rule"
    # the curated contact details survive: only the INPUT was screened
    assert out["text"] == "Call hr@example.com or 555-0199."
    assert [c["source"] for c in runtime.calls] == ["INPUT"]


def test_a_guardrail_free_agent_is_unaffected_by_other_agents_rules(client, monkeypatch):
    with_rule, without = _agent(client), _agent(client)
    add_rule(client, with_rule)
    monkeypatch.setattr(invoke_service, "_dispatch_invoke",
                        lambda agent, prompt, **k: {"text": "model"})
    assert invoke_service.invoke_agent_text(
        _load(without), "What is the leave policy?")["text"] == "model"


def test_the_stream_marks_a_curated_answer_and_the_ledger_remembers(client, monkeypatch):
    agent_id = _agent(client)
    rule = add_rule(client, agent_id)
    monkeypatch.setattr(chat_service, "invoke_agent_events",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model")))
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "What is the leave policy?"})
    assert res.status_code == 200, res.text
    events = sse(res)
    names = [e for e, _ in events]
    assert names[0] == "meta" and "rule" in names and names[-1] == "done"
    assert next(d for e, d in events if e == "rule")["rule_id"] == rule["id"]
    saved = next(d for e, d in events if e == "saved")
    assert saved["answered_by"] == f"rule:{rule['id']}"
    assert "".join(d["text"] for e, d in events if e == "delta") == "25 days."

    sid = events[0][1]["session_id"]
    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": sid}).json()
    agent_rows = [m for m in history["messages"] if m["role"] == "agent"]
    assert agent_rows[0]["answered_by"] == f"rule:{rule['id']}"
    db = SessionLocal()
    try:
        assert db.query(ChatMessage).filter(ChatMessage.answered_by.isnot(None)).count() == 1
        assert db.query(AnswerRule).one().hit_count == 1
    finally:
        db.close()


def test_the_console_invoke_route_reports_a_curated_answer(client, no_model):
    """Regression (found by the P3/P4 e2e): the console route's response model dropped
    `answered_by`, so a curated answer read as the model's own there while `/v1` said
    "rule". One invoke chain must mean one contract on both entrances."""
    agent_id = _agent(client)
    add_rule(client, agent_id)
    res = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": "What is the leave policy?", "session_id": "s" * 40},
    )
    assert res.status_code == 200, res.text
    assert res.json()["text"] == "25 days."
    assert res.json()["answered_by"] == "rule"
