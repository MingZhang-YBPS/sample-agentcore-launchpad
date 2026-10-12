"""Share links (T13/T14): hashed bearer token, one 404 for every bad state, a
per-token rate limit, the workspace named by the row, and no dependence on a
console session."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.routers.share as share_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.route_policy import (
    PROD_PROTECTED,
    PUBLIC,
    ROUTE_POLICY,
    WORKSPACE_EXEMPT,
)
from app.main import create_app
from app.models.ledger import Agent, ChatMessage, ChatSession, ShareLink, Workspace
from app.services import share_links

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}


def make_agent(workspace_id=DEFAULT_WORKSPACE_ID, name="shared-agent", **fields) -> str:
    db = SessionLocal()
    agent = Agent(
        workspace_id=workspace_id, name=name, method="zip_runtime", status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
        spec={"name": name, "display_name": "Front Desk"}, **fields,
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


@pytest.fixture(autouse=True)
def fresh_limiter():
    share_links.limiter.reset()
    yield
    share_links.limiter.reset()


@pytest.fixture
def seen(monkeypatch):
    """Replace the invoke chain with a scripted stream; record what it was given."""
    class Seen(list):
        script: dict

    calls = Seen()
    script = {
        "events": [
            {"event": "tool", "data": {"name": "internal_lookup"}},
            {"event": "delta", "data": {"text": "hello "}},
            {"event": "delta", "data": {"text": "there"}},
        ],
    }

    def fake_stream(agent, prompt, session_id=None, actor_id="river", **kwargs):
        calls.append({"agent": agent.id, "session_id": session_id, "actor_id": actor_id,
                      "workspace": kwargs.get("workspace"), "kwargs": kwargs})
        yield {"event": "meta", "data": {"session_id": session_id, "agent": agent.name,
                                         "mode": "stream"}}
        yield from script["events"]
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(share_router, "chat_stream", fake_stream)
    calls.script = script
    return calls


@pytest.fixture
def client():
    return TestClient(create_app())


def new_link(client, agent_id, **body):
    res = client.post(f"/api/agents/{agent_id}/share-links", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def sse(res) -> list[tuple[str, dict]]:
    out = []
    for frame in res.text.split("\n\n"):
        if not frame.strip() or frame.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in frame.split("\n") if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


# ── the primitive ──────────────────────────────────────────────────────────


def test_only_the_sha256_of_the_token_is_stored(client):
    agent_id = make_agent()
    made = new_link(client, agent_id, label="partner demo")
    raw = made["token"]
    assert raw.startswith("shr_") and made["path"] == f"/s/{raw}"

    db = SessionLocal()
    try:
        row = db.get(ShareLink, made["id"])
        assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest()
        stored = " ".join(str(getattr(row, c.name)) for c in ShareLink.__table__.columns)
        assert raw not in stored
        assert row.workspace_id == DEFAULT_WORKSPACE_ID and row.target_id == agent_id
    finally:
        db.close()

    listed = client.get(f"/api/agents/{agent_id}/share-links").json()["links"]
    assert [x["id"] for x in listed] == [made["id"]]
    assert "token" not in listed[0] and raw not in json.dumps(listed)
    assert listed[0]["state"] == "active" and listed[0]["use_count"] == 0


def test_a_system_preset_cannot_be_shared(client):
    agent_id = make_agent(system_key="aws-agent-solution-architect")
    res = client.post(f"/api/agents/{agent_id}/share-links", json={})
    assert res.status_code == 409 and res.json()["code"] == "share.agent_not_shareable"


def test_info_shows_the_display_name_and_nothing_internal(client):
    made = new_link(client, make_agent(), label="demo")
    body = client.get(f"/share/{made['token']}").json()
    assert body["agent"] == {"display_name": "Front Desk"}
    assert body["label"] == "demo"
    assert "arn" not in json.dumps(body).lower()


# ── 404 for every bad state, indistinguishably ─────────────────────────────


def _mutate(link_id, **fields):
    db = SessionLocal()
    try:
        row = db.get(ShareLink, link_id)
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def test_every_unusable_state_is_the_same_404(client):
    agent_id = make_agent()
    bad = {}
    bad["unknown"] = "shr_" + "x" * 43
    bad["no-prefix"] = "plain-token"
    bad["oversized"] = "shr_" + "y" * 500

    disabled = new_link(client, agent_id)
    _mutate(disabled["id"], enabled=False)
    bad["disabled"] = disabled["token"]

    revoked = new_link(client, agent_id)
    assert client.post(f"/api/share-links/{revoked['id']}/revoke").status_code == 200
    bad["revoked"] = revoked["token"]

    expired = new_link(client, agent_id, expires_in_days=1)
    _mutate(expired["id"], expires_at=datetime.now(UTC) - timedelta(seconds=1))
    bad["expired"] = expired["token"]

    orphan_agent = make_agent(name="soon-gone")
    orphaned = new_link(client, orphan_agent)
    db = SessionLocal()
    db.get(Agent, orphan_agent).status = "deleted"
    db.commit()
    db.close()
    bad["agent-deleted"] = orphaned["token"]

    for state, token in bad.items():
        for method, suffix, body in (
            ("get", "", None),
            ("post", "/chat", {"prompt": "hi"}),
            ("post", "/feedback", {"session_id": "s", "message_id": 1, "verdict": "up"}),
        ):
            kwargs = {"json": body} if body else {}
            res = getattr(client, method)(f"/share/{token}{suffix}", **kwargs)
            assert res.status_code == 404, (state, suffix, res.text)
            assert res.json() == {
                "code": "share.not_found", "message": "share link not found", "detail": None,
            }, (state, suffix)


def test_expiry_is_enforced_at_use_time(client, seen):
    made = new_link(client, make_agent(), expires_in_days=7)
    assert client.get(f"/share/{made['token']}").status_code == 200
    _mutate(made["id"], expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert client.get(f"/share/{made['token']}").status_code == 404
    assert client.post(f"/share/{made['token']}/chat", json={"prompt": "hi"}).status_code == 404
    assert seen == []  # nothing reached the invoke chain


def test_revoke_takes_effect_at_once_and_is_idempotent(client, seen):
    agent_id = make_agent()
    made = new_link(client, agent_id)
    assert client.post(f"/share/{made['token']}/chat", json={"prompt": "hi"}).status_code == 200
    first = client.post(f"/api/share-links/{made['id']}/revoke").json()
    again = client.post(f"/api/share-links/{made['id']}/revoke").json()
    assert first["state"] == "revoked" and first["revoked_at"] == again["revoked_at"]
    assert client.post(f"/share/{made['token']}/chat", json={"prompt": "hi"}).status_code == 404
    listed = client.get(f"/api/agents/{agent_id}/share-links").json()["links"]
    assert listed[0]["state"] == "revoked"


# ── chat over the shared invoke chain ──────────────────────────────────────


def test_chat_streams_persists_and_keeps_bookkeeping(client, seen):
    made = new_link(client, make_agent())
    res = client.post(f"/share/{made['token']}/chat", json={"prompt": "hello"})
    assert res.status_code == 200
    events = sse(res)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert "tool" not in kinds  # tool names stay server-side
    assert "".join(d["text"] for e, d in events if e == "delta") == "hello there"
    assert list(events[0][1]) == ["session_id"]  # no runtime mode / agent name
    saved = [d["message_id"] for e, d in events if e == "saved"]
    assert len(saved) == 1

    db = SessionLocal()
    try:
        link = db.get(ShareLink, made["id"])
        assert link.use_count == 1 and link.last_used_at is not None
        session_id = events[0][1]["session_id"]
        row = db.query(ChatSession).filter_by(session_id=session_id).one()
        assert row.workspace_id == DEFAULT_WORKSPACE_ID
        assert db.get(ChatMessage, saved[0]).role == "agent"
    finally:
        db.close()


def test_a_share_visitor_gets_a_distinct_per_session_memory_actor(client, seen):
    agent_id = make_agent()
    made = new_link(client, agent_id)
    client.post(f"/share/{made['token']}/chat", json={"prompt": "one"})
    client.post(f"/share/{made['token']}/chat", json={"prompt": "two"})
    first, second = seen
    for call in seen:
        assert call["actor_id"].startswith(f"{agent_id}__share_{made['id']}_")
    assert first["actor_id"] != second["actor_id"]  # strangers do not share memory
    assert first["session_id"] != second["session_id"]
    assert len(first["session_id"]) >= 33


def test_a_visitor_can_continue_only_their_own_session(client, seen):
    agent_id = make_agent()
    made = new_link(client, agent_id)
    other = new_link(client, agent_id)
    sid = sse(client.post(f"/share/{made['token']}/chat", json={"prompt": "a"}))[0][1]["session_id"]

    again = client.post(f"/share/{made['token']}/chat", json={"prompt": "b", "session_id": sid})
    assert again.status_code == 200 and seen[-1]["session_id"] == sid

    # another link, and a console user's own session, must read as missing
    console_sid = "c" * 40
    db = SessionLocal()
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       session_id=console_sid, actor_id="river"))
    db.commit()
    db.close()
    for stolen in (sid, console_sid):
        token = other["token"] if stolen == sid else made["token"]
        res = client.post(f"/share/{token}/chat", json={"prompt": "x", "session_id": stolen})
        assert res.status_code == 404 and res.json()["code"] == "share.session_not_found"


def test_errors_are_generic_for_visitors_but_recorded(client, seen):
    seen.script["events"] = [
        {"event": "error", "data": {"message": "ClientError: arn:aws:iam::1:role/secret"}},
    ]
    made = new_link(client, make_agent())
    res = client.post(f"/share/{made['token']}/chat", json={"prompt": "hi"})
    assert "arn:aws" not in res.text and "secret" not in res.text
    assert [e for e, _ in sse(res)][-2:] == ["error", "done"]
    db = SessionLocal()
    try:
        assert db.query(ChatMessage).filter_by(role="error").one().text.startswith("ClientError")
    finally:
        db.close()


def test_prompt_is_bounded(client, seen):
    made = new_link(client, make_agent())
    res = client.post(f"/share/{made['token']}/chat", json={"prompt": "x" * 9000})
    assert res.status_code == 422 and seen == []


# ── rate limiting ──────────────────────────────────────────────────────────


def test_rate_limit_is_per_token(client, seen, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        share_links, "limiter", share_links.TokenBucket(2, 0.1, clock=lambda: now[0])
    )
    agent_id = make_agent()
    a, b = new_link(client, agent_id), new_link(client, agent_id)
    post = lambda t: client.post(f"/share/{t['token']}/chat", json={"prompt": "hi"})  # noqa: E731
    assert post(a).status_code == 200 and post(a).status_code == 200
    limited = post(a)
    assert limited.status_code == 429
    assert limited.json()["code"] == "share.rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
    assert post(b).status_code == 200  # another link is unaffected
    now[0] += 10  # one token refilled
    assert post(a).status_code == 200


def test_token_bucket_refills_and_reports_the_wait():
    now = [0.0]
    bucket = share_links.TokenBucket(1, 0.5, clock=lambda: now[0])
    assert bucket.take("k") == 0.0
    assert bucket.take("k") == pytest.approx(2.0)
    now[0] += 2
    assert bucket.take("k") == 0.0


# ── the row decides the workspace ──────────────────────────────────────────


def test_the_link_row_names_the_workspace_not_the_header(client, seen):
    db = SessionLocal()
    db.add(Workspace(id="lab", name="lab", account_id="222233334444", region="us-east-2",
                     bootstrap_status="ready", resources={"artifacts_bucket": "lab-bucket"}))
    db.commit()
    db.close()
    lab_agent = make_agent(workspace_id="lab", name="lab-agent")
    made = client.post(
        f"/api/agents/{lab_agent}/share-links", json={}, headers={"X-Workspace": "lab"}
    )
    assert made.status_code == 201
    token = made.json()["token"]

    # the visitor names another (even nonexistent) workspace: ignored
    for header in ("default", "does-not-exist"):
        res = client.post(
            f"/share/{token}/chat", json={"prompt": "hi"}, headers={"X-Workspace": header}
        )
        assert res.status_code == 200, res.text
    assert seen[-1]["workspace"].id == "lab"
    assert seen[-1]["workspace"].region == "us-east-2"
    db = SessionLocal()
    try:
        assert {r.workspace_id for r in db.query(ChatSession)} == {"lab"}
    finally:
        db.close()


def test_a_link_cannot_be_pointed_at_another_workspaces_agent(client):
    db = SessionLocal()
    db.add(Workspace(id="lab", name="lab", account_id="222233334444", region="us-east-2",
                     bootstrap_status="ready"))
    db.commit()
    db.close()
    lab_agent = make_agent(workspace_id="lab")
    # created while the console addresses `default`: the agent reads as missing
    res = client.post(f"/api/agents/{lab_agent}/share-links", json={})
    assert res.status_code == 404


# ── classification + independence from the console session ─────────────────


def test_share_routes_are_public_hub_global_and_unprotected():
    # the SME review routes (T34) are asserted in tests/test_review_links.py
    # the IM webhooks (T30) are asserted in tests/test_channels.py
    # the annotation links (Agent-DLC) are asserted in tests/test_dlc_annotation_links.py
    share = {k for k in ROUTE_POLICY
             if k[1].startswith("/share/") and not k[1].startswith("/share/review/")
             and not k[1].startswith("/share/channels/")
             and not k[1].startswith("/share/annotate/")}
    assert len(share) == 3
    assert all(ROUTE_POLICY[k] == PUBLIC and k in WORKSPACE_EXEMPT for k in share)
    # a prod workspace must still allow handing out a link (not an agent mutation)
    assert not {k for k in PROD_PROTECTED if "share" in k[1] or "feedback" in k[1]}


@pytest.fixture
def gated():
    import os

    os.environ["LAUNCHPAD_AUTH_USERNAME"] = ADMIN_CREDS["username"]
    os.environ["LAUNCHPAD_AUTH_PASSWORD"] = ADMIN_CREDS["password"]
    get_settings.cache_clear()
    try:
        yield create_app()
    finally:
        del os.environ["LAUNCHPAD_AUTH_USERNAME"]
        del os.environ["LAUNCHPAD_AUTH_PASSWORD"]
        get_settings.cache_clear()


def test_share_routes_need_no_console_session(gated, seen):
    with TestClient(gated, client=("127.0.0.1", 4321)) as admin:
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        made = admin.post(f"/api/agents/{make_agent()}/share-links", json={}).json()
    # a fresh client: no cookie at all
    with TestClient(gated, client=("203.0.113.9", 4321)) as outsider:
        assert outsider.get("/api/agents").status_code == 401  # console stays closed
        assert outsider.get(f"/share/{made['token']}").status_code == 200
        chat = outsider.post(f"/share/{made['token']}/chat", json={"prompt": "hi"})
        assert chat.status_code == 200
        # and the console-only surfaces do not open up through the share token
        assert outsider.post(f"/api/share-links/{made['id']}/revoke").status_code == 401


def test_a_console_session_alone_opens_no_share_link(gated):
    with TestClient(gated, client=("127.0.0.1", 4321)) as admin:
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        agent_id = make_agent()
        assert admin.get(f"/share/{agent_id}").status_code == 404
        assert admin.get("/share/shr_" + "z" * 43).status_code == 404
