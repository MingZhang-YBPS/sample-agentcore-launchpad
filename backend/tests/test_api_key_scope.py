"""T16 — scoped API keys: migration defaults, scope, expiry, rate limit, usage."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import app.routers.public_api as public_api
from app.core import db as db_module
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ApiKey, User
from app.services import api_keys as key_limits


@pytest.fixture(autouse=True)
def stub_invoke(monkeypatch):
    key_limits.reset_rate_limits()
    monkeypatch.setattr(
        public_api,
        "invoke_agent_text",
        lambda agent, prompt, **_kw: {"text": f"hi from {agent.name}", "session_id": "s" * 40},
    )
    yield
    key_limits.reset_rate_limits()


def _agent(name: str) -> str:
    db = SessionLocal()
    try:
        row = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="harness", status="active",
            arn=f"arn:aws:bedrock-agentcore:us-west-2:111:harness/{name}",
            spec={"name": name, "method": "harness", "system_prompt": "x"},
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _mint(client, **body) -> dict:
    res = client.post("/api/apikeys", json={"name": "k", **body})
    assert res.status_code == 201, res.text
    return res.json()


def _invoke(client, key: str, agent_id: str):
    return client.post(
        f"/v1/agents/{agent_id}/invoke", headers={"X-Api-Key": key}, json={"prompt": "hi"}
    )


def test_migration_adds_columns_with_legacy_defaults(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.init_db(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO api_keys (id, workspace_id, name, prefix, key_hash, enabled,"
            " created_at) VALUES ('k1', 'default', 'old', 'lp_live_ab12', 'h', 1,"
            " '2026-01-01 00:00:00')"
        ))
        # `created_by` / `created_by_name` are the lifecycle migration's (indexed), see
        # test_t16_username_creator_is_reinterpreted_once below
        for col in ("agent_ids", "expires_at", "rate_per_minute", "last_used_at",
                    "use_count"):
            conn.execute(sa.text(f"ALTER TABLE api_keys DROP COLUMN {col}"))
    assert db_module.schema_drift(engine)["api_keys"]
    db_module.init_db(engine)
    assert db_module.schema_drift(engine) == {}
    with engine.begin() as conn:
        row = conn.execute(sa.text(
            "SELECT agent_ids, expires_at, rate_per_minute, last_used_at, use_count"
            " FROM api_keys")).one()
    assert tuple(row) == (None, None, None, None, 0)


def test_t16_username_creator_is_reinterpreted_once(tmp_path):
    """A ledger written by the T16 line stored the minting *username* in
    `api_keys.created_by` and had no `created_by_name`. The merge with the upstream
    key lifecycle reinterprets it once: the name moves to `created_by_name`, and
    `created_by` becomes the `users.id` (NULL with no `users` row — the config admin —
    so that key is never treated as offboarded)."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.init_db(engine)
    with Session(engine) as db:
        db.add(User(id="u-ada", username="ada", username_key="ada",
                    email="ada@example.com", password_hash="x"))
        db.commit()
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_api_keys_created_by"))
        conn.execute(sa.text("ALTER TABLE api_keys DROP COLUMN created_by_name"))
        for kid, creator in (("k-ada", "ada"), ("k-admin", "admin"), ("k-legacy", None)):
            conn.execute(sa.text(
                "INSERT INTO api_keys (id, workspace_id, name, prefix, key_hash, enabled,"
                " created_at, use_count, created_by) VALUES (:id, 'default', :id,"
                " 'lp_live_ab12', :id, 1, '2026-01-01 00:00:00', 0, :by)"
            ), {"id": kid, "by": creator})
    db_module.init_db(engine)
    db_module.init_db(engine)  # idempotent: the second pass must not touch the ids again
    assert db_module.schema_drift(engine) == {}
    with engine.begin() as conn:
        rows = {
            r.id: (r.created_by, r.created_by_name)
            for r in conn.execute(sa.text(
                "SELECT id, created_by, created_by_name FROM api_keys"))
        }
    assert rows == {
        "k-ada": ("u-ada", "ada"),
        "k-admin": (None, "admin"),
        "k-legacy": (None, None),
    }


def test_unscoped_key_behaves_as_before(client):
    a, b = _agent("agent-aaa"), _agent("agent-bbb")
    key = _mint(client)
    assert key["agent_ids"] == [] and key["expires_at"] is None
    assert key["rate_per_minute"] is None and key["use_count"] == 0
    assert _invoke(client, key["key"], a).status_code == 200
    assert _invoke(client, key["key"], b).status_code == 200
    listed = client.get("/v1/agents", headers={"X-Api-Key": key["key"]}).json()["agents"]
    assert {x["id"] for x in listed} == {a, b}


def test_scoped_key_reads_out_of_scope_agent_as_missing(client):
    a, b = _agent("agent-aaa"), _agent("agent-bbb")
    key = _mint(client, agent_ids=[a])
    assert _invoke(client, key["key"], a).status_code == 200
    denied = _invoke(client, key["key"], b)
    missing = _invoke(client, key["key"], "does-not-exist")
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()  # indistinguishable
    stream = client.post(
        f"/v1/agents/{b}/invoke-stream",
        headers={"X-Api-Key": key["key"]}, json={"prompt": "hi"},
    )
    assert stream.status_code == 404
    listed = client.get("/v1/agents", headers={"X-Api-Key": key["key"]}).json()["agents"]
    assert [x["id"] for x in listed] == [a]


def test_scope_must_name_live_agents_of_this_workspace(client):
    res = client.post("/api/apikeys", json={"name": "k", "agent_ids": ["nope"]})
    assert res.status_code == 422 and res.json()["code"] == "apikey.unknown_agent"


def test_expired_key_is_401_and_past_expiry_is_refused_at_mint(client):
    a = _agent("agent-aaa")
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    res = client.post("/api/apikeys", json={"name": "k", "expires_at": past})
    assert res.status_code == 422 and res.json()["code"] == "apikey.expiry_in_past"

    key = _mint(client, expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    assert _invoke(client, key["key"], a).status_code == 200
    db = SessionLocal()
    row = db.get(ApiKey, key["id"])
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    db.close()
    expired = _invoke(client, key["key"], a)
    assert expired.status_code == 401
    assert expired.json()["code"] == "auth.expired_api_key"
    assert client.get("/api/apikeys").json()["keys"][0]["expired"] is True


def test_rate_limit_429_with_retry_after_and_window_slides(client):
    a = _agent("agent-aaa")
    key = _mint(client, rate_per_minute=2)
    assert _invoke(client, key["key"], a).status_code == 200
    assert _invoke(client, key["key"], a).status_code == 200
    limited = _invoke(client, key["key"], a)
    assert limited.status_code == 429
    assert limited.json()["code"] == "auth.rate_limited"
    assert 1 <= int(limited.headers["Retry-After"]) <= 61
    # rejected calls are not counted as usage
    assert client.get(f"/api/apikeys/{key['id']}/usage").json()["total"] == 2
    # a fresh window admits again
    row = ApiKey(id=key["id"], rate_per_minute=2)
    key_limits.check_rate(row, now=10_000_000.0)


def test_usage_is_recorded_per_day(client):
    a = _agent("agent-aaa")
    key = _mint(client)
    for _ in range(3):
        _invoke(client, key["key"], a)
    usage = client.get(f"/api/apikeys/{key['id']}/usage").json()
    assert usage["total"] == 3 and usage["last_used_at"]
    assert len(usage["days"]) == 14 and usage["days"][-1]["count"] == 3
    listed = client.get("/api/apikeys").json()["keys"][0]
    assert listed["use_count"] == 3 and listed["last_used_at"]


def test_patch_updates_and_clears_fields(client):
    a = _agent("agent-aaa")
    key = _mint(client, agent_ids=[a], rate_per_minute=5)
    patched = client.patch(
        f"/api/apikeys/{key['id']}", json={"agent_ids": None, "rate_per_minute": 9}
    ).json()
    assert patched["agent_ids"] == [] and patched["rate_per_minute"] == 9
    cleared = client.patch(f"/api/apikeys/{key['id']}", json={"rate_per_minute": None}).json()
    assert cleared["rate_per_minute"] is None
    assert client.patch("/api/apikeys/nope", json={}).status_code == 404
