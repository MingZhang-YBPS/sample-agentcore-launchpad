"""Projects phase 1 (design §5.5) for chat, feedback, issues and links: a preset chat
turn is filed in the caller's project from inside the stream, a preset session id is
never continued across projects, a channel turn runs as its link's project, the intent
cache never serves one project's view to another, and Studio local-debug conversations
stay inside the project that opened them."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.chat as chat_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project, project_context
from app.main import create_app
from app.models.conversation import CreateConversationRequest
from app.models.ledger import Agent, ChatMessage, ChatSession, ShareLink
from app.services import channels, intents
from app.services.channels.base import Inbound
from app.services.conversation_service import ConversationService
from tests.test_project_scope import _project

SESSION = "p" * 40


def _active_agent(name: str, pid: str | None, *, system_key: str | None = None) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime",
                        status="active", arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
                        spec={"name": name}, system_key=system_key)
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


# ── console chat on a system preset ───────────────────────────────────────────


@pytest.fixture
def fake_stream(monkeypatch):
    seen: list[str | None] = []

    def stream(agent, prompt, **kwargs):
        seen.append(current_project())  # what the generator thread sees
        yield {"event": "meta", "data": {"session_id": SESSION, "agent": agent.name,
                                         "mode": "buffered"}}
        yield {"event": "delta", "data": {"text": "hi"}}
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(chat_router, "chat_stream", stream)
    return seen


def test_a_preset_chat_turn_is_filed_in_the_callers_project(fake_stream):
    _project("alpha")
    preset = _active_agent("preset", None, system_key="aws-agent-solution-architect")
    client = TestClient(create_app())
    res = client.post(f"/api/chat/{preset}", json={"prompt": "hello"},
                      headers={"X-Project": "alpha"})
    assert res.status_code == 200, res.text
    assert fake_stream == ["alpha"]
    db = SessionLocal()
    try:
        with project_context(None):
            assert set(db.scalars(select(ChatSession.project_id))) == {"alpha"}
            assert set(db.scalars(select(ChatMessage.project_id))) == {"alpha"}
    finally:
        db.close()


def test_a_preset_session_is_not_continued_from_another_project(fake_stream):
    _project("alpha")
    preset = _active_agent("preset", None, system_key="aws-agent-solution-architect")
    client = TestClient(create_app())
    first = client.post(f"/api/chat/{preset}", json={"prompt": "hello"},
                        headers={"X-Project": "alpha"})
    assert first.status_code == 200, first.text
    again = client.post(f"/api/chat/{preset}",
                        json={"prompt": "more", "session_id": SESSION},
                        headers={"X-Project": "default"})
    assert again.status_code == 404
    assert again.json()["code"] == "chat.session_not_found"
    # the owning project continues it as before
    same = client.post(f"/api/chat/{preset}", json={"prompt": "more", "session_id": SESSION},
                       headers={"X-Project": "alpha"})
    assert same.status_code == 200, same.text


def test_a_single_project_workspace_continues_its_sessions(fake_stream):
    preset = _active_agent("preset", None, system_key="aws-agent-solution-architect")
    client = TestClient(create_app())
    for prompt in ("hello", "again"):
        res = client.post(f"/api/chat/{preset}", json={"prompt": prompt, "session_id": SESSION})
        assert res.status_code == 200, res.text


# ── channel turns ─────────────────────────────────────────────────────────────


def test_a_channel_turn_runs_as_its_links_project(monkeypatch):
    _project("alpha")
    agent_id = _active_agent("chan", "alpha")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            link = ShareLink(workspace_id=DEFAULT_WORKSPACE_ID, kind="slack",
                             target_id=agent_id, token_hash="h" * 64)
            db.add(link)
            db.commit()
            link_id = link.id
    finally:
        db.close()
    seen: dict[str, str | None] = {}

    def fake_invoke(agent, prompt, **kwargs):
        seen["invoke"] = current_project()
        return {"text": "echo"}

    class Adapter:
        @staticmethod
        def reply(link, message, answer):
            seen["reply"] = current_project()

    monkeypatch.setattr(channels, "invoke_agent_text", fake_invoke)
    monkeypatch.setattr(channels, "adapter_for", lambda kind: Adapter)
    assert current_project() is None  # a background job: nothing is current
    channels.handle_message(link_id, Inbound(event_id="e1", text="hi", conversation_key="c"))
    assert seen == {"invoke": "alpha", "reply": "alpha"}
    assert current_project() is None
    db = SessionLocal()
    try:
        with project_context(None):
            assert set(db.scalars(select(ChatMessage.project_id))) == {"alpha"}
    finally:
        db.close()


# ── the intent view's cache ───────────────────────────────────────────────────


def test_the_intent_cache_is_keyed_by_project(monkeypatch):
    intents.reset_cache()
    turn = intents.Turn(session_id="s1", agent_id="a1", message_id=1, question="q",
                        answer="a", errored=False, by_rule=False)
    calls: list[str | None] = []
    monkeypatch.setattr(intents, "collect_turns", lambda *a, **k: [turn])

    def model(_ws, turns, _lang):
        calls.append(current_project())
        return None  # fall back: grouping is not under test

    monkeypatch.setattr(intents, "_model_groups", model)
    try:
        for pid in ("alpha", "default", "alpha"):
            with project_context(pid):
                intents.intent_view(None, None, DEFAULT_WORKSPACE_ID, agent_id=None, days=7)
        assert calls == ["alpha", "default"]  # the third call is alpha's cache hit
    finally:
        intents.reset_cache()


# ── Studio local-debug conversations ──────────────────────────────────────────


def test_studio_conversations_stay_in_the_project_that_opened_them(monkeypatch):
    service = ConversationService()
    monkeypatch.setattr(service, "_initialize_agent", lambda *a: None)
    run = asyncio.run
    with project_context("alpha"):
        mine = run(service.create_session(CreateConversationRequest(generated_code="x")))
    with project_context("default"):
        theirs = run(service.create_session(CreateConversationRequest(generated_code="x")))
        listed = [s.session_id for s in run(service.get_sessions()).sessions]
        assert listed == [theirs.session_id]
        for call in (service.get_session_history, service.get_session_messages,
                     service.delete_session):
            with pytest.raises(ValueError, match="not found"):
                run(call(mine.session_id))
        with pytest.raises(ValueError, match="not found"):
            run(service.send_message(mine.session_id, "hi"))
        with pytest.raises(ValueError, match="not found"):
            run(service.update_session_code(mine.session_id, "y"))
    with project_context("alpha"):
        assert run(service.get_session_history(mine.session_id)).session.session_id \
            == mine.session_id
    with project_context(None):  # unscoped housekeeping sees every session
        assert len(run(service.get_sessions()).sessions) == 2
