"""Thumbs feedback (T15): write, upsert, withdraw, scope, and the bad-case query
the evaluation flow consumes."""

import json

import pytest
from fastapi.testclient import TestClient

import app.routers.chat as chat_router
import app.routers.share as share_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import ChatFeedback, ChatMessage
from app.services import share_links
from tests.test_share_links import make_agent, sse


@pytest.fixture(autouse=True)
def fresh_limiter():
    share_links.limiter.reset()


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def scripted(monkeypatch):
    def fake_stream(agent, prompt, session_id=None, actor_id="river", **kwargs):
        sid = session_id or ("s" * 40)
        yield {"event": "meta", "data": {"session_id": sid, "agent": agent.name}}
        yield {"event": "delta", "data": {"text": f"answer to {prompt}"}}
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(chat_router, "chat_stream", fake_stream)
    monkeypatch.setattr(share_router, "chat_stream", fake_stream)


def console_turn(client, agent_id, prompt, session_id=None):
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": prompt, "session_id": session_id})
    assert res.status_code == 200, res.text
    events = sse(res)
    sid = events[0][1]["session_id"]
    message_id = next(d["message_id"] for e, d in events if e == "saved")
    return sid, message_id


def test_the_console_stream_announces_the_saved_message_id(client, scripted):
    agent_id = make_agent()
    sid, message_id = console_turn(client, agent_id, "hi")
    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": sid}).json()
    agent_rows = [m for m in history["messages"] if m["role"] == "agent"]
    assert [m["id"] for m in agent_rows] == [message_id]
    assert agent_rows[0]["verdict"] is None


def test_thumbs_down_is_stored_and_shows_up_as_a_bad_case(client, scripted):
    agent_id = make_agent()
    sid, message_id = console_turn(client, agent_id, "what is 2+2")
    res = client.post(
        f"/api/chat/{agent_id}/feedback",
        json={"session_id": sid, "message_id": message_id, "verdict": "down",
              "comment": "  wrong  "},
    )
    assert res.status_code == 200 and res.json() == {"message_id": message_id, "verdict": "down"}

    db = SessionLocal()
    try:
        row = db.query(ChatFeedback).one()
        assert (row.workspace_id, row.agent_id, row.session_id, row.message_id) == (
            DEFAULT_WORKSPACE_ID, agent_id, sid, message_id)
        assert (row.verdict, row.comment, row.source) == ("down", "wrong", "console")
        assert row.actor  # the console caller
    finally:
        db.close()

    body = client.get("/api/feedback", params={"verdict": "down"}).json()
    assert body["counts"] == {"up": 0, "down": 1}
    assert body["down_session_ids"] == [sid]  # what from-sessions takes as-is
    item = body["items"][0]
    assert item["question"] == "what is 2+2" and item["answer"] == "answer to what is 2+2"
    assert item["comment"] == "wrong" and item["agent_name"] == "shared-agent"

    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": sid}).json()
    assert [m["verdict"] for m in history["messages"] if m["role"] == "agent"] == ["down"]


def test_revoting_updates_and_none_withdraws(client, scripted):
    agent_id = make_agent()
    sid, message_id = console_turn(client, agent_id, "q")

    def vote(verdict):
        return client.post(
            f"/api/chat/{agent_id}/feedback",
            json={"session_id": sid, "message_id": message_id, "verdict": verdict},
        )

    vote("down")
    vote("up")
    assert client.get("/api/feedback").json()["counts"] == {"up": 1, "down": 0}
    assert client.get("/api/feedback", params={"verdict": "down"}).json()["down_session_ids"] == []
    assert vote("none").json()["verdict"] is None
    assert client.get("/api/feedback").json()["counts"] == {"up": 0, "down": 0}
    assert vote("none").status_code == 200  # withdrawing nothing is fine


def test_only_an_answer_of_that_agent_and_session_can_be_rated(client, scripted):
    agent_id = make_agent()
    other_agent = make_agent(name="other")
    sid, message_id = console_turn(client, agent_id, "q")
    db = SessionLocal()
    user_msg = db.query(ChatMessage).filter_by(role="user").one().id
    db.close()

    def vote(agent, session, message):
        return client.post(
            f"/api/chat/{agent}/feedback",
            json={"session_id": session, "message_id": message, "verdict": "down"},
        )

    assert vote(agent_id, sid, user_msg).status_code == 404  # a question, not an answer
    assert vote(other_agent, sid, message_id).status_code == 404  # wrong agent
    assert vote(agent_id, "z" * 40, message_id).status_code == 404  # wrong session
    assert vote(agent_id, sid, 99999).status_code == 404
    assert vote("nope", sid, message_id).status_code == 404
    assert client.post(
        f"/api/chat/{agent_id}/feedback",
        json={"session_id": sid, "message_id": message_id, "verdict": "meh"},
    ).status_code == 422


def test_feedback_is_workspace_scoped(client, scripted):
    agent_id = make_agent()
    sid, message_id = console_turn(client, agent_id, "q")
    client.post(f"/api/chat/{agent_id}/feedback",
                json={"session_id": sid, "message_id": message_id, "verdict": "down"})
    db = SessionLocal()
    from app.models.ledger import Workspace

    db.add(Workspace(id="lab", name="lab", account_id="222233334444", region="us-east-2",
                     bootstrap_status="ready"))
    db.commit()
    db.close()
    other = client.get("/api/feedback", headers={"X-Workspace": "lab"}).json()
    assert other["counts"] == {"up": 0, "down": 0} and other["items"] == []
    # nor can it be rated through another workspace's console
    res = client.post(
        f"/api/chat/{agent_id}/feedback", headers={"X-Workspace": "lab"},
        json={"session_id": sid, "message_id": message_id, "verdict": "down"},
    )
    assert res.status_code == 404


def test_share_visitors_rate_only_their_own_conversation(client, scripted):
    agent_id = make_agent()
    link = client.post(f"/api/agents/{agent_id}/share-links", json={}).json()
    turn = sse(client.post(f"/share/{link['token']}/chat", json={"prompt": "hello"}))
    sid = turn[0][1]["session_id"]
    message_id = next(d["message_id"] for e, d in turn if e == "saved")

    ok = client.post(
        f"/share/{link['token']}/feedback",
        json={"session_id": sid, "message_id": message_id, "verdict": "down", "comment": "meh"},
    )
    assert ok.status_code == 200
    items = client.get("/api/feedback", params={"verdict": "down"}).json()["items"]
    assert [(i["source"], i["session_id"], i["comment"]) for i in items] == [
        ("share", sid, "meh")]
    assert items[0]["actor"].startswith("share_")

    # someone else's session id, or a console session, is not theirs to rate
    console_sid, console_msg = console_turn(client, agent_id, "mine")
    res = client.post(
        f"/share/{link['token']}/feedback",
        json={"session_id": console_sid, "message_id": console_msg, "verdict": "down"},
    )
    assert res.status_code == 404 and res.json()["code"] == "share.session_not_found"


def test_down_sessions_are_distinct_newest_first_and_capped(client, scripted):
    agent_id = make_agent()
    made = []
    for n in range(3):
        sid = f"session-{n}".ljust(40, "x")
        _, mid = console_turn(client, agent_id, f"q{n}", session_id=sid)
        made.append((sid, mid))
    for sid, mid in made:
        client.post(f"/api/chat/{agent_id}/feedback",
                    json={"session_id": sid, "message_id": mid, "verdict": "down"})
    ids = client.get("/api/feedback", params={"verdict": "down"}).json()["down_session_ids"]
    assert ids == [sid for sid, _ in reversed(made)]
    assert json.dumps(ids)  # plain strings
