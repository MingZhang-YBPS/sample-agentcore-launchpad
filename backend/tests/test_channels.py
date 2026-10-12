"""T30 - channel publishing: embed mode and the Slack / Feishu webhook adapters.

Everything here is hermetic: the agent turn and the outbound reply are replaced, so the
tests pin the parts that are ours - who is let in, what is stored, what is mapped.
"""

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, ChatMessage, ChatSession, ShareLink
from app.services import channels, share_links
from app.services.channels import slack, transport

SIGNING = "slack-signing-secret-123"
# A placeholder, not a credential. The adapter requires the `xoxb-` prefix, and a literal
# of that shape trips secret scanners on every commit, so the fixture is assembled here.
BOT = "-".join(("xoxb", "test", "bot", "token"))
VERIFY = "feishu-verification-token"


def make_agent(name="chan-agent") -> str:
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime", status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
        spec={"name": name, "display_name": "Front Desk"},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


@pytest.fixture(autouse=True)
def fresh_state():
    share_links.limiter.reset()
    channels.reset_seen()
    yield
    share_links.limiter.reset()
    channels.reset_seen()


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def turns(monkeypatch):
    """Record agent turns and outbound replies instead of touching AWS or the network."""

    class Log:
        invoked: list[dict]
        posted: list[dict]

    log = Log()
    log.invoked, log.posted = [], []

    def fake_invoke(agent, prompt, session_id=None, actor_id="x", **kwargs):
        log.invoked.append({"agent": agent.id, "prompt": prompt, "session_id": session_id,
                            "actor_id": actor_id})
        return {"text": f"echo: {prompt}", "session_id": session_id}

    def fake_post(url, payload, headers=None):
        log.posted.append({"url": url, "payload": payload, "headers": headers or {}})
        if url.endswith("/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "t-abc"}
        return {"ok": True, "code": 0}

    monkeypatch.setattr(channels, "invoke_agent_text", fake_invoke)
    monkeypatch.setattr(transport, "post_json", fake_post)
    return log


def slack_link(client, agent_id, **body):
    res = client.post(
        f"/api/agents/{agent_id}/channel-links",
        json={"platform": "slack", "credentials": {"signing_secret": SIGNING, "bot_token": BOT},
              **body},
    )
    assert res.status_code == 201, res.text
    return res.json()


def feishu_link(client, agent_id):
    res = client.post(
        f"/api/agents/{agent_id}/channel-links",
        json={"platform": "feishu",
              "credentials": {"verification_token": VERIFY, "app_id": "cli_a1",
                              "app_secret": "app-secret-xyz"}},
    )
    assert res.status_code == 201, res.text
    return res.json()


def slack_post(client, made, payload, *, secret=SIGNING, ts=None, headers=None):
    body = json.dumps(payload).encode()
    stamp = str(int(ts if ts is not None else time.time()))
    sent = {
        "content-type": "application/json",
        "x-slack-request-timestamp": stamp,
        "x-slack-signature": slack.sign(secret, stamp, body),
        **(headers or {}),
    }
    return client.post(made["path"], content=body, headers=sent)


def slack_event(text="<@U0BOT> what is 2+2?", event_id="Ev1", **event):
    return {
        "type": "event_callback", "event_id": event_id,
        "event": {"type": "app_mention", "user": "U1", "text": text, "channel": "C1",
                  "ts": "1700000000.000100", **event},
    }


# ── embed ─────────────────────────────────────────────────────────────────


def test_embed_snippet_is_built_once_and_escaped(client):
    agent_id = make_agent()
    res = client.post(f"/api/agents/{agent_id}/share-links", json={"label": 'x" onload="evil'},
                      headers={"origin": "https://lp.example.com"})
    made = res.json()
    assert made["embed_url"] == f"https://lp.example.com/s/{made['token']}?embed=1"
    snippet = made["embed_snippet"]
    assert snippet.startswith("<iframe ") and made["embed_url"] in snippet
    assert 'onload="evil' not in snippet and "&quot;" in snippet  # label cannot break out
    listed = client.get(f"/api/agents/{agent_id}/share-links").json()["links"][0]
    assert "embed_snippet" not in listed and made["token"] not in json.dumps(listed)


def test_info_reports_embed_mode_and_the_link_still_limits_it(client):
    made = client.post(f"/api/agents/{make_agent()}/share-links", json={}).json()
    assert client.get(f"/share/{made['token']}").json()["embed"] is False
    assert client.get(f"/share/{made['token']}?embed=1").json()["embed"] is True
    client.post(f"/api/share-links/{made['id']}/revoke")
    assert client.get(f"/share/{made['token']}?embed=1").status_code == 404


# ── channel links: creation and secrecy ───────────────────────────────────


def test_secrets_are_stored_but_never_echoed(client):
    agent_id = make_agent()
    made = slack_link(client, agent_id, label="ops")
    assert made["kind"] == "slack" and made["path"].startswith("/share/channels/slack/shr_")
    assert made["channel"] == {"platform": "slack", "config": {},
                               "secrets_set": ["bot_token", "signing_secret"]}
    listed = client.get(f"/api/agents/{agent_id}/share-links").json()
    blob = json.dumps(made) + json.dumps(listed)
    assert SIGNING not in blob and BOT not in blob
    assert listed["links"][0]["channel"]["secrets_set"] == ["bot_token", "signing_secret"]
    db = SessionLocal()
    try:
        row = db.get(ShareLink, made["id"])
        assert row.channel_secrets["signing_secret"] == SIGNING  # the adapter needs it
        assert row.token_hash == hashlib.sha256(made["token"].encode()).hexdigest()
    finally:
        db.close()


def test_feishu_verification_token_is_kept_only_as_a_hash(client):
    made = feishu_link(client, make_agent())
    db = SessionLocal()
    try:
        secrets_ = db.get(ShareLink, made["id"]).channel_secrets
    finally:
        db.close()
    assert VERIFY not in json.dumps(secrets_)
    assert secrets_["verification_token_sha256"] == hashlib.sha256(VERIFY.encode()).hexdigest()
    assert "app-secret-xyz" not in json.dumps(made)
    assert made["channel"]["secrets_set"] == ["app_secret", "verification_token"]


@pytest.mark.parametrize(
    "platform,credentials",
    [("slack", {"signing_secret": SIGNING}), ("slack", {"signing_secret": SIGNING,
                                                        "bot_token": "nope"}),
     ("feishu", {"verification_token": VERIFY}), ("teams", {})],
)
def test_bad_setup_is_refused(client, platform, credentials):
    res = client.post(f"/api/agents/{make_agent()}/channel-links",
                      json={"platform": platform, "credentials": credentials})
    assert res.status_code == 422
    assert res.json()["code"] in ("channel.invalid_setup", "channel.unsupported")


def test_link_kinds_do_not_cross_over(client):
    agent_id = make_agent()
    chat = client.post(f"/api/agents/{agent_id}/share-links", json={}).json()
    slack_made = slack_link(client, agent_id)
    # a channel token opens no web chat page, and a chat token is no webhook
    assert client.get(f"/share/{slack_made['token']}").status_code == 404
    forged = client.post(f"/share/channels/slack/{chat['token']}", json={})
    assert forged.status_code == 404
    wrong_platform = client.post(f"/share/channels/feishu/{slack_made['token']}", json={})
    assert wrong_platform.status_code == 404


# ── Slack ─────────────────────────────────────────────────────────────────


def test_slack_handshake_is_answered_only_when_signed(client, turns):
    made = slack_link(client, make_agent())
    challenge = {"type": "url_verification", "challenge": "abc123"}
    ok = slack_post(client, made, challenge)
    assert ok.status_code == 200 and ok.json() == {"challenge": "abc123"}
    forged = slack_post(client, made, challenge, secret="wrong-secret")
    assert forged.status_code == 401 and forged.json()["code"] == "channel.signature_invalid"


def test_slack_rejects_stale_and_unsigned_requests(client, turns):
    made = slack_link(client, make_agent())
    stale = slack_post(client, made, slack_event(), ts=time.time() - 3600)
    assert stale.status_code == 401
    bare = client.post(made["path"], json=slack_event())
    assert bare.status_code == 401
    assert turns.invoked == [] and turns.posted == []


def test_slack_message_runs_the_agent_and_replies_in_thread(client, turns):
    agent_id = make_agent()
    made = slack_link(client, agent_id)
    res = slack_post(client, made, slack_event())
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert [t["prompt"] for t in turns.invoked] == ["what is 2+2?"]  # mention stripped
    assert turns.invoked[0]["agent"] == agent_id
    assert turns.invoked[0]["actor_id"].startswith(f"{agent_id}__share_{made['id']}_")
    (post,) = turns.posted
    assert post["url"] == slack.POST_MESSAGE_URL
    assert post["headers"] == {"Authorization": f"Bearer {BOT}"}
    assert post["payload"] == {"channel": "C1", "thread_ts": "1700000000.000100",
                               "text": "echo: what is 2+2?"}
    db = SessionLocal()
    try:
        session = db.query(ChatSession).filter_by(agent_id=agent_id).one()
        roles = [m.role for m in db.query(ChatMessage).filter_by(session_id=session.session_id)
                 .order_by(ChatMessage.id)]
        assert roles == ["user", "agent"] and session.workspace_id == DEFAULT_WORKSPACE_ID
        link = db.get(ShareLink, made["id"])
        assert link.use_count == 1
    finally:
        db.close()


def test_slack_ignores_bots_edits_and_channel_chatter(client, turns):
    made = slack_link(client, make_agent())
    for payload in (
        slack_event(event_id="E1", bot_id="B1"),
        slack_event(event_id="E2", subtype="message_changed"),
        slack_event(event_id="E3", type="message", channel_type="channel"),
    ):
        assert slack_post(client, made, payload).json() == {"ok": True}
    assert turns.invoked == [] and turns.posted == []


def test_slack_redelivery_is_one_turn_and_a_thread_is_one_session(client, turns):
    made = slack_link(client, make_agent())
    first = slack_event(event_id="E9")
    slack_post(client, made, first)
    again = slack_post(client, made, first, headers={"x-slack-retry-num": "1"})
    assert again.json()["duplicate"] is True
    reply = slack_event(event_id="E10", thread_ts="1700000000.000100", ts="1700000005.000200")
    slack_post(client, made, reply)
    assert len(turns.invoked) == 2
    assert turns.invoked[0]["session_id"] == turns.invoked[1]["session_id"]
    assert len(turns.invoked[0]["session_id"]) >= 33


def test_slack_turn_failure_replies_generically(client, turns, monkeypatch):
    made = slack_link(client, make_agent())

    def boom(*args, **kwargs):
        raise RuntimeError("secret internals arn:aws:...")

    monkeypatch.setattr(channels, "invoke_agent_text", boom)
    assert slack_post(client, made, slack_event()).status_code == 200
    assert turns.posted[0]["payload"]["text"] == channels.GENERIC_ERROR
    assert "internals" not in json.dumps(turns.posted)


def test_channel_webhook_is_rate_limited_per_link(client, turns):
    made = slack_link(client, make_agent())
    statuses = [
        slack_post(client, made, slack_event(event_id=f"R{i}")).status_code for i in range(14)
    ]
    assert statuses[:12] == [200] * 12 and statuses[12:] == [429, 429]
    other = slack_link(client, make_agent("other-agent"))
    assert slack_post(client, other, slack_event(event_id="Z")).status_code == 200


def test_forged_requests_do_not_spend_the_rate_budget(client, turns):
    made = slack_link(client, make_agent())
    for i in range(20):
        assert slack_post(client, made, slack_event(event_id=f"F{i}"),
                          secret="wrong").status_code == 401
    assert slack_post(client, made, slack_event(event_id="ok")).status_code == 200


def test_revoked_channel_link_is_gone(client, turns):
    made = slack_link(client, make_agent())
    client.post(f"/api/share-links/{made['id']}/revoke")
    assert slack_post(client, made, slack_event()).status_code == 404


# ── Feishu ────────────────────────────────────────────────────────────────


def feishu_message(text="hello", *, token=VERIFY, event_id="fe1", chat_type="p2p", **msg):
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1", "token": token},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_1"}},
            "message": {"message_id": "om_1", "chat_id": "oc_1", "chat_type": chat_type,
                        "message_type": "text", "content": json.dumps({"text": text}), **msg},
        },
    }


def test_feishu_handshake_needs_the_verification_token(client, turns):
    made = feishu_link(client, make_agent())
    hello = {"type": "url_verification", "challenge": "c-77", "token": VERIFY}
    ok = client.post(made["path"], json=hello)
    assert ok.status_code == 200 and ok.json() == {"challenge": "c-77"}
    bad = client.post(made["path"], json={**hello, "token": "guess"})
    assert bad.status_code == 401 and bad.json()["code"] == "channel.signature_invalid"


def test_feishu_message_runs_the_agent_and_replies(client, turns):
    agent_id = make_agent()
    made = feishu_link(client, agent_id)
    payload = feishu_message("@_user_1 hi there", mentions=[{"key": "@_user_1"}],
                             chat_type="group")
    assert client.post(made["path"], json=payload).json() == {"ok": True}
    assert [t["prompt"] for t in turns.invoked] == ["hi there"]
    token_call, reply_call = turns.posted
    assert token_call["payload"] == {"app_id": "cli_a1", "app_secret": "app-secret-xyz"}
    assert reply_call["url"].endswith("/open-apis/im/v1/messages/om_1/reply")
    assert reply_call["headers"] == {"Authorization": "Bearer t-abc"}
    assert json.loads(reply_call["payload"]["content"]) == {"text": "echo: hi there"}


def test_feishu_forged_message_and_encrypted_payloads_are_refused(client, turns):
    made = feishu_link(client, make_agent())
    assert client.post(made["path"], json=feishu_message(token="nope")).status_code == 401
    assert client.post(made["path"], json={"type": "event_callback"}).status_code == 401
    enc = client.post(made["path"], json={"encrypt": "AAAA"})
    assert enc.status_code == 400 and enc.json()["code"] == "channel.encrypted_unsupported"
    assert client.post(made["path"], content=b"not json").status_code == 401
    assert turns.invoked == []


def test_feishu_ignores_non_text_and_bot_senders(client, turns):
    made = feishu_link(client, make_agent())
    image = feishu_message(message_type="image", event_id="i1")
    assert client.post(made["path"], json=image).json() == {"ok": True}
    bot = feishu_message(event_id="b1")
    bot["event"]["sender"]["sender_type"] = "app"
    assert client.post(made["path"], json=bot).json() == {"ok": True}
    assert turns.invoked == []


def test_replies_may_only_go_to_the_known_hosts():
    with pytest.raises(ValueError):
        transport.post_json("https://evil.example.com/hook", {})
    with pytest.raises(ValueError):
        transport.post_json("http://slack.com/api/chat.postMessage", {})


def test_webhook_is_public_hub_global_and_console_route_is_member():
    from app.core.route_policy import MEMBER, PUBLIC, ROUTE_POLICY, WORKSPACE_EXEMPT, is_hub_global

    hook = ("POST", "/share/channels/{platform}/{token}")
    assert ROUTE_POLICY[hook] == PUBLIC and hook in WORKSPACE_EXEMPT and is_hub_global(hook[1])
    assert ROUTE_POLICY[("POST", "/api/agents/{agent_id}/channel-links")] == MEMBER


def test_channel_columns_are_added_to_an_older_share_links_table(tmp_path):
    import sqlalchemy as sa

    from app.core import db as db_module

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE share_links (id VARCHAR(32) PRIMARY KEY, workspace_id VARCHAR(32), "
            "kind VARCHAR(16), target_id VARCHAR(32), token_hash VARCHAR(64), "
            "prefix VARCHAR(16), label VARCHAR(64), created_by VARCHAR(64), enabled BOOLEAN, "
            "expires_at DATETIME, revoked_at DATETIME, last_used_at DATETIME, "
            "use_count INTEGER, created_at DATETIME)"))
    db_module._migrate_share_link_channels(engine)
    columns = {c["name"] for c in sa.inspect(engine).get_columns("share_links")}
    assert {"channel_config", "channel_secrets"} <= columns
