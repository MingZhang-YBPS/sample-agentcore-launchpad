"""T34 -- SME review links: the T13 security posture on a second link kind, and a
submission path that lands in the one feedback store."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import SessionLocal
from app.core.route_policy import PROD_PROTECTED, PUBLIC, ROUTE_POLICY, WORKSPACE_EXEMPT
from app.main import create_app
from app.models.ledger import ChatFeedback, ShareLink
from app.models.selfservice import Issue
from app.services import review_links, share_links
from tests.selfservice_util import seed_turn
from tests.test_guardrail import FakeBedrock, FakeRuntime, _ctx
from tests.test_share_links import make_agent


@pytest.fixture(autouse=True)
def fresh_limiters():
    share_links.limiter.reset()
    review_links.review_limiter.reset()
    yield
    review_links.review_limiter.reset()


@pytest.fixture
def client():
    return TestClient(create_app())


def new_review(client, agent_id, **body):
    res = client.post(f"/api/agents/{agent_id}/review-links", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def test_the_token_is_hashed_at_rest_and_listed_apart_from_chat_links(client):
    agent_id = make_agent()
    made = new_review(client, agent_id, label="HR - Wang Fang")
    assert made["token"].startswith("shr_") and made["path"] == f"/r/{made['token']}"
    assert made["kind"] == "review"
    db = SessionLocal()
    try:
        row = db.get(ShareLink, made["id"])
        assert row.kind == "review" and made["token"] not in row.token_hash
        assert row.token_hash == share_links.hash_token(made["token"])
    finally:
        db.close()
    listed = client.get(f"/api/agents/{agent_id}/review-links").json()["links"]
    assert [x["id"] for x in listed] == [made["id"]] and "token" not in listed[0]
    client.post(f"/api/agents/{agent_id}/share-links", json={})
    assert len(client.get(f"/api/agents/{agent_id}/review-links").json()["links"]) == 1


def test_every_bad_state_is_the_same_404_and_kinds_do_not_cross(client):
    agent_id = make_agent()
    good = new_review(client, agent_id)
    revoked = new_review(client, agent_id)
    client.post(f"/api/share-links/{revoked['id']}/revoke")
    expired = new_review(client, agent_id)
    disabled = new_review(client, agent_id)
    db = SessionLocal()
    try:
        db.get(ShareLink, expired["id"]).expires_at = datetime.now(UTC) - timedelta(days=1)
        db.get(ShareLink, disabled["id"]).enabled = False
        db.commit()
    finally:
        db.close()
    chat = client.post(f"/api/agents/{agent_id}/share-links", json={}).json()

    bodies = set()
    for token in (revoked["token"], expired["token"], disabled["token"], "shr_unknown",
                  "not-a-token", chat["token"]):
        res = client.get(f"/share/review/{token}")
        assert res.status_code == 404, token
        bodies.add(res.text)
        assert client.post(f"/share/review/{token}/rate",
                           json={"message_id": 1, "verdict": "up"}).status_code == 404
    assert len(bodies) == 1  # indistinguishable
    assert client.get(f"/share/review/{good['token']}").status_code == 200
    # a review token cannot open the chat surface either
    assert client.get(f"/share/{good['token']}").status_code == 404
    assert client.post(f"/share/{good['token']}/chat",
                       json={"prompt": "hi"}).status_code == 404


def test_the_queue_shows_questions_answers_and_curation(client):
    agent_id = make_agent()
    seed_turn(agent_id, "How many days?", "12 days.")
    seed_turn(agent_id, "Sick leave?", "5 days.", answered_by="rule:abc")
    link = new_review(client, agent_id)
    body = client.get(f"/share/review/{link['token']}").json()
    assert body["agent"]["display_name"] == "Front Desk"
    by_q = {i["question"]: i for i in body["items"]}
    assert by_q["How many days?"]["answer"] == "12 days."
    assert by_q["Sick leave?"]["curated"] is True and by_q["How many days?"]["curated"] is False
    assert all(set(i) == {"message_id", "question", "answer", "curated", "verdict",
                          "comment", "correction"} for i in body["items"])  # no session ids


def test_a_rating_lands_in_the_shared_feedback_store_and_opens_an_issue(client):
    agent_id = make_agent()
    _, mid = seed_turn(agent_id, "How many days?", "12 days.")
    link = new_review(client, agent_id, label="HR")
    res = client.post(f"/share/review/{link['token']}/rate", json={
        "message_id": mid, "verdict": "down", "comment": "wrong",
        "correction": "It is 25 days."})
    assert res.status_code == 200 and res.json() == {"message_id": mid, "verdict": "down"}

    db = SessionLocal()
    try:
        row = db.query(ChatFeedback).one()
        assert (row.source, row.correction, row.comment) == ("review", "It is 25 days.", "wrong")
        assert row.actor == review_links.reviewer_actor(link["id"])
        issue = db.query(Issue).one()
        assert (issue.kind, issue.correction, issue.status) == ("review", "It is 25 days.", "open")
    finally:
        db.close()
    # it is the same store the console reads: one pipeline for every origin
    listed = client.get("/api/feedback", params={"verdict": "down"}).json()
    assert listed["items"][0]["source"] == "review"
    assert listed["items"][0]["correction"] == "It is 25 days."
    # and the reviewer sees their own verdict when they return
    item = client.get(f"/share/review/{link['token']}").json()["items"][0]
    assert (item["verdict"], item["correction"]) == ("down", "It is 25 days.")


def test_a_link_cannot_rate_another_agents_or_workspaces_answers(client):
    mine, theirs = make_agent(name="mine"), make_agent(name="theirs")
    _, other_mid = seed_turn(theirs, "q", "a")
    link = new_review(client, mine)
    res = client.post(f"/share/review/{link['token']}/rate",
                      json={"message_id": other_mid, "verdict": "up"})
    assert res.status_code == 404 and res.json()["code"] == "review.item_not_found"
    assert client.post(f"/share/review/{link['token']}/rate",
                       json={"message_id": 999999, "verdict": "up"}).status_code == 404


def test_the_per_link_rate_limit_and_revoke(client, monkeypatch):
    agent_id = make_agent()
    link = new_review(client, agent_id)
    monkeypatch.setattr(review_links, "review_limiter",
                        review_links.TokenBucket(capacity=2, refill_per_sec=0.001))
    other = new_review(client, agent_id)
    assert client.get(f"/share/review/{link['token']}").status_code == 200
    assert client.get(f"/share/review/{link['token']}").status_code == 200
    limited = client.get(f"/share/review/{link['token']}")
    assert limited.status_code == 429 and "Retry-After" in limited.headers
    assert client.get(f"/share/review/{other['token']}").status_code == 200  # per link
    client.post(f"/api/share-links/{link['id']}/revoke")
    assert client.get(f"/share/review/{link['token']}").status_code == 404


def test_questions_are_masked_for_a_guardrail_agent(client, monkeypatch):
    agent_id = make_agent()
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        row.spec = {**row.spec, "guardrail": {"enabled": True, "mode": "anonymize"}}
        db.commit()
    finally:
        db.close()
    seed_turn(agent_id, "call me at 555-0100", "ok")
    link = new_review(client, agent_id)
    monkeypatch.setattr(review_links, "workspace_context",
                        lambda row: _ctx(FakeBedrock(), FakeRuntime(intervene=True)))
    item = client.get(f"/share/review/{link['token']}").json()["items"][0]
    assert item["question"] == "call me at {PHONE}"


def test_review_routes_are_public_hub_global_and_console_side_is_member():
    review = {k for k in ROUTE_POLICY if k[1].startswith("/share/review/")}
    assert len(review) == 2
    assert all(ROUTE_POLICY[k] == PUBLIC and k in WORKSPACE_EXEMPT for k in review)
    assert not {k for k in PROD_PROTECTED if "review" in k[1]}
