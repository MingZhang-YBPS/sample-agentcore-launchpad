"""T01 — TTFA (Time to First Agent): first-login stamp, owner stamp, admin endpoint."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, inspect, text

from app.core import db as db_mod
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, Deployment, User, Workspace
from app.services import users as users_service
from app.services.ttfa import compute_ttfa
from tests.test_users_api import (  # noqa: F401 — fixtures
    ADMIN,
    MEMBER,
    admin,
    anon,
    app,
    member_session,
    stored,
)

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def _user(username: str, *, first_login=None, created=T0, role="member", status="active"):
    db = SessionLocal()
    try:
        user = User(
            username=username, username_key=username.lower(),
            email=f"{username.lower()}@acme-corp.com", password_hash="x", role=role,
            status=status, first_login_at=first_login, created_at=created,
        )
        db.add(user)
        db.flush()
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
    finally:
        db.close()


def _deploy(owner: str, ended: datetime, status="succeeded", workspace=DEFAULT_WORKSPACE_ID,
            *, created_by_name: str | None = None):
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=workspace, name=f"a-{owner}-{ended.timestamp():.0f}-{status}",
            method="harness", status="active", spec={}, owner=owner,
            created_by_name=created_by_name,
        )
        db.add(agent)
        db.flush()
        db.add(Deployment(
            workspace_id=workspace, agent_id=agent.id, status=status,
            started_at=ended - timedelta(minutes=2), ended_at=ended,
        ))
        db.commit()
    finally:
        db.close()


def test_first_login_is_stamped_once(admin, app):  # noqa: F811
    client = member_session(app)
    first = stored().first_login_at
    assert first is not None
    assert stored().login_count == 1
    again = client.post(
        "/api/auth/login",
        json={"username": MEMBER["username"], "password": MEMBER["password"]},
    )
    assert again.status_code == 200
    user = stored()
    assert user.login_count == 2
    assert user.first_login_at == first
    listed = admin.get("/api/users").json()["items"]
    assert listed[0]["first_login_at"] is not None


def test_prior_login_without_stamp_stays_null():
    db = SessionLocal()
    try:
        user = User(username="old", username_key="old", email="old@acme-corp.com",
                    password_hash="x", login_count=3)
        db.add(user)
        db.commit()
        users_service.record_login(db, user)
        assert user.first_login_at is None  # unknown → TTFA falls back to created_at
        assert user.login_count == 4
    finally:
        db.close()


def test_compute_ttfa_median_fallback_and_scoping():
    _user("Alice", first_login=T0)
    _user("bob", first_login=None, created=T0)  # fallback to created_at
    _user("carol", first_login=T0)  # never shipped
    _user("dave", status="pending")  # excluded
    _deploy("alice", T0 + timedelta(minutes=10))  # case-insensitive owner match
    _deploy("Alice", T0 + timedelta(minutes=5), status="failed")  # ignored
    _deploy("Alice", T0 + timedelta(minutes=30))  # later success ignored
    _deploy("bob", T0 + timedelta(minutes=20))
    db = SessionLocal()
    try:
        db.add(Workspace(id="ws-other", name="other", account_id="1", region="us-west-2"))
        db.commit()
    finally:
        db.close()
    _deploy("carol", T0 + timedelta(minutes=1), workspace="ws-other")  # other workspace

    db = SessionLocal()
    try:
        body = compute_ttfa(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    by_name = {u["username"]: u for u in body["users"]}
    assert set(by_name) == {"Alice", "bob", "carol"}
    assert by_name["Alice"]["ttfa_seconds"] == 600.0
    assert by_name["bob"]["ttfa_seconds"] == 1200.0
    assert by_name["bob"]["first_login_at"] is None
    assert by_name["carol"]["ttfa_seconds"] is None
    assert by_name["carol"]["first_agent_at"] is None
    assert body["samples"] == 2
    assert body["median_seconds"] == 900.0


def test_compute_ttfa_prefers_the_recorded_creator_over_owner():
    """A row stamped by `services.creators` is attributed by `created_by_name`; `owner`
    (a provenance label such as "river") counts only for pre-creator rows."""
    _user("erin", first_login=T0)
    _deploy("river", T0 + timedelta(minutes=3), created_by_name="Erin")
    db = SessionLocal()
    try:
        body = compute_ttfa(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    by_name = {u["username"]: u for u in body["users"]}
    assert by_name["erin"]["ttfa_seconds"] == 180.0


def test_ttfa_endpoint_is_admin_only(admin, app):  # noqa: F811
    member = member_session(app)
    assert member.get("/api/overview/ttfa").status_code == 403
    res = admin.get("/api/overview/ttfa")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["samples"] == 0 and body["median_seconds"] is None
    assert [u["username"] for u in body["users"]] == [MEMBER["username"]]


def test_create_agent_stamps_creator_name(admin, monkeypatch):  # noqa: F811
    """TTFA attributes an agent by its creator's username (`created_by_name`, stamped
    by `services.creators`); `owner` stays the provenance label."""
    import app.routers.agents as agents_mod

    monkeypatch.setattr(agents_mod, "start_deploy_async", lambda _job_id: None)
    res = admin.post("/api/agents", json={
        "name": "ttfa-owner", "method": "harness", "system_prompt": "be brief",
    })
    assert res.status_code == 202, res.text
    assert res.json()["agent"]["created_by_name"] == ADMIN["username"]
    assert res.json()["agent"]["owner"] == "river"


def test_first_login_migration_is_additive(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id VARCHAR(32) PRIMARY KEY)"))
    db_mod._migrate(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    assert "first_login_at" in cols
    db_mod._migrate(engine)  # idempotent
