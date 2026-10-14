"""Administrator management (design projects §7.3, §7.4, §7.10).

Promotion / demotion through `PATCH /api/users/{id} {role}` with the `admin.grant` /
`admin.revoke` structure events, the self-demotion and last-admin guards, the
forced logout `POST /api/users/{id}/sessions/revoke` (session epoch), the
`GET /api/admins` list, and `credential_stamp`'s epoch argument.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.models.ledger import AuditEvent, User
from app.routers import auth
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(app) -> TestClient:
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


def login(app, username: str, password: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def create(admin: TestClient, username: str, role: str = "member", **extra) -> dict:
    body = {
        "username": username, "email": f"{username}@acme-corp.com",
        "password": f"{username}-pass", "role": role, **extra,
    }
    if role == "admin":
        body.setdefault("workspaces", [])
    response = admin.post("/api/users", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def events(action: str) -> list[AuditEvent]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))
    finally:
        db.close()


def epoch(user_id: str) -> int:
    db = SessionLocal()
    try:
        return db.get(User, user_id).session_epoch
    finally:
        db.close()


# ── credential stamp ────────────────────────────────────────────────────────────


def test_credential_stamp_epoch_zero_is_the_hash_alone():
    stamp = users_service.credential_stamp("pbkdf2$1$abc$def")
    assert users_service.credential_stamp("pbkdf2$1$abc$def", 0) == stamp
    assert users_service.credential_stamp("pbkdf2$1$abc$def", -1) == stamp
    one = users_service.credential_stamp("pbkdf2$1$abc$def", 1)
    assert one != stamp
    assert users_service.credential_stamp("pbkdf2$1$abc$def", 2) not in (stamp, one)


def test_account_stamp_delegates_to_credential_stamp():
    class Row:
        password_hash = "pbkdf2$1$abc$def"
        session_epoch = 3

    assert auth.account_stamp(Row()) == users_service.credential_stamp(Row.password_hash, 3)
    Row.session_epoch = None
    assert auth.account_stamp(Row()) == users_service.credential_stamp(Row.password_hash)


# ── promote / demote ───────────────────────────────────────────────────────────


class TestPromoteDemote:
    def test_promotion_and_demotion_journal_structure_events(self, app, admin):
        peer = create(admin, "li-si")
        promoted = admin.patch(f"/api/users/{peer['id']}", json={"role": "admin"})
        assert promoted.status_code == 200 and promoted.json()["role"] == "admin"
        [grant] = events("admin.grant")
        assert grant.project_id is None and grant.actor == ADMIN["username"]
        assert grant.actor_user_id is None  # the built-in admin has no row
        assert "user:li-si" in grant.target

        # a registered admin demotes them: their own users.id is recorded
        create(admin, "wang-wu", role="admin")
        other = login(app, "wang-wu", "wang-wu-pass")
        demoted = other.patch(f"/api/users/{peer['id']}", json={"role": "member"})
        assert demoted.status_code == 200 and demoted.json()["role"] == "member"
        [revoke] = events("admin.revoke")
        wang = admin.get("/api/users", params={"q": "wang-wu"}).json()["items"][0]
        assert revoke.actor == "wang-wu" and revoke.actor_user_id == wang["id"]
        assert "role=member" in revoke.target
        # the account journal still gets its user.update row beside them
        assert len(events("user.update")) == 2

    def test_a_patch_without_a_role_change_writes_no_admin_event(self, admin):
        peer = create(admin, "li-si", role="admin")
        assert admin.patch(f"/api/users/{peer['id']}", json={"role": "admin"}).status_code == 200
        assert admin.patch(f"/api/users/{peer['id']}", json={"extend_days": 5}).status_code == 200
        member = create(admin, "zhang-san")
        assert admin.patch(
            f"/api/users/{member['id']}", json={"role": "operator"}
        ).status_code == 200
        assert events("admin.grant") == [] and events("admin.revoke") == []

    def test_nobody_demotes_themselves(self, app, admin):
        me = create(admin, "li-si", role="admin")
        solo = login(app, "li-si", "li-si-pass")
        refused = solo.patch(f"/api/users/{me['id']}", json={"role": "member"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "users.self_demote"
        assert admin.get("/api/admins").json()["items"][1]["role"] == "admin"
        assert events("admin.revoke") == []
        # another admin may
        assert admin.patch(f"/api/users/{me['id']}", json={"role": "member"}).status_code == 200

    def test_the_last_admin_answer_wins_over_self_demotion(self, app, admin, monkeypatch):
        me = create(admin, "li-si", role="admin")
        solo = login(app, "li-si", "li-si-pass")
        monkeypatch.setattr(users_service, "builtin_admin_enabled", lambda settings=None: False)
        refused = solo.patch(f"/api/users/{me['id']}", json={"role": "member"})
        assert refused.status_code == 409 and refused.json()["code"] == "users.last_admin"

    def test_demoting_the_last_admin_is_refused(self, app, admin, monkeypatch):
        target = create(admin, "li-si", role="admin")
        actor = create(admin, "wang-wu", role="admin")
        other = login(app, "wang-wu", "wang-wu-pass")
        monkeypatch.setattr(users_service, "builtin_admin_enabled", lambda settings=None: False)
        # two signable admins: demoting one leaves the other
        assert other.patch(f"/api/users/{target['id']}", json={"role": "member"}).status_code == 200
        # wang-wu is now the last one; the only admin left to demote them is themselves
        refused = other.patch(f"/api/users/{actor['id']}", json={"role": "member"})
        assert refused.json()["code"] == "users.last_admin"

    def test_demotion_restores_default_memberships(self, admin):
        from app.models.projects import ProjectMember

        peer = create(admin, "li-si")
        assert admin.patch(f"/api/users/{peer['id']}", json={"role": "admin"}).status_code == 200
        assert admin.patch(f"/api/users/{peer['id']}", json={"role": "member"}).status_code == 200
        db = SessionLocal()
        try:
            rows = list(db.scalars(
                select(ProjectMember).where(ProjectMember.user_id == peer["id"])
            ))
        finally:
            db.close()
        assert rows, "the default workspace grant implies a default-project membership"


# ── forced logout ──────────────────────────────────────────────────────────────


class TestSessionRevoke:
    def test_revoke_ends_every_open_session(self, app, admin):
        peer = create(admin, "zhang-san")
        first = login(app, "zhang-san", "zhang-san-pass")
        second = login(app, "zhang-san", "zhang-san-pass")
        assert first.get("/api/auth/status").json()["authenticated"] is True
        assert epoch(peer["id"]) == 0

        response = admin.post(f"/api/users/{peer['id']}/sessions/revoke")
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "user_id": peer["id"], "session_epoch": 1}
        assert epoch(peer["id"]) == 1
        for client in (first, second):
            assert client.get("/api/auth/status").json()["authenticated"] is False
            assert client.get("/api/agents").status_code == 401

        # a fresh login carries the new epoch and works; a second revoke ends it too
        third = login(app, "zhang-san", "zhang-san-pass")
        assert third.get("/api/auth/status").json()["authenticated"] is True
        assert admin.post(f"/api/users/{peer['id']}/sessions/revoke").json()["session_epoch"] == 2
        assert third.get("/api/auth/status").json()["authenticated"] is False

        journal = events("user.sessions.revoke")
        assert len(journal) == 2 and all(row.project_id is None for row in journal)
        assert "user:zhang-san" in journal[0].target

    def test_revoke_leaves_other_accounts_alone(self, app, admin):
        peer = create(admin, "zhang-san")
        create(admin, "li-si")
        bystander = login(app, "li-si", "li-si-pass")
        assert admin.post(f"/api/users/{peer['id']}/sessions/revoke").status_code == 200
        assert bystander.get("/api/auth/status").json()["authenticated"] is True
        # the admin's own (built-in) session too
        assert admin.get("/api/auth/status").json()["authenticated"] is True

    def test_revoke_records_the_registered_actor(self, app, admin):
        create(admin, "wang-wu", role="admin")
        actor = login(app, "wang-wu", "wang-wu-pass")
        peer = create(admin, "zhang-san")
        assert actor.post(f"/api/users/{peer['id']}/sessions/revoke").status_code == 200
        [row] = events("user.sessions.revoke")
        wang = admin.get("/api/users", params={"q": "wang-wu"}).json()["items"][0]
        assert row.actor == "wang-wu" and row.actor_user_id == wang["id"]

    def test_unknown_and_builtin_targets(self, admin):
        assert admin.post("/api/users/nope/sessions/revoke").status_code == 404
        builtin = admin.post("/api/users/builtin-admin/sessions/revoke")
        assert builtin.status_code == 400
        assert builtin.json()["code"] == "users.builtin_immutable"
        assert events("user.sessions.revoke") == []

    def test_only_admins_may_revoke(self, app, admin):
        peer = create(admin, "zhang-san")
        create(admin, "li-si")
        member = login(app, "li-si", "li-si-pass")
        assert member.post(f"/api/users/{peer['id']}/sessions/revoke").status_code == 403
        assert member.get("/api/admins").status_code == 403
        assert TestClient(app).get("/api/admins").status_code == 401


# ── admin list ─────────────────────────────────────────────────────────────────


class TestListAdmins:
    def test_builtin_first_then_admin_accounts(self, app, admin):
        create(admin, "li-si", role="admin")
        create(admin, "wang-wu", role="admin")
        create(admin, "zhang-san")  # a member: not listed
        body = admin.get("/api/admins").json()
        names = [row["username"] for row in body["items"]]
        assert names == [ADMIN["username"], "li-si", "wang-wu"]
        assert body["total"] == 3 and body["can_sign_in"] == 3
        builtin, li, wang = body["items"]
        assert builtin["builtin"] is True and builtin["can_demote"] is False
        assert "password_source" in builtin
        assert li["builtin"] is False and li["can_demote"] is True and li["can_sign_in"] is True
        assert "password_hash" not in li

        # seen by li-si: they cannot demote themselves
        own = login(app, "li-si", "li-si-pass").get("/api/admins").json()["items"]
        assert [row["can_demote"] for row in own] == [False, False, True]
        assert wang["can_demote"] is True

    def test_the_last_signable_admin_cannot_be_demoted(self, app, admin, monkeypatch):
        li = create(admin, "li-si", role="admin")
        wang = create(admin, "wang-wu", role="admin")
        disabled = admin.patch(f"/api/users/{wang['id']}", json={"status": "disabled"})
        assert disabled.status_code == 200
        monkeypatch.setattr(users_service, "builtin_admin_enabled", lambda settings=None: False)
        body = admin.get("/api/admins").json()
        rows = {row["username"]: row for row in body["items"]}
        assert ADMIN["username"] not in rows  # gate off ⇒ no built-in admin
        assert body["can_sign_in"] == 1
        assert rows["li-si"]["last_signable"] is True and rows["li-si"]["can_demote"] is False
        # a disabled admin cannot sign in, so it is not "last" and may be demoted
        assert rows["wang-wu"]["can_sign_in"] is False
        assert rows["wang-wu"]["can_demote"] is True
        assert li["id"] == rows["li-si"]["id"]
