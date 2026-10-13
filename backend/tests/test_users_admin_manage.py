"""Admin-created accounts, workspace edits, password changes and the built-in admin.

Covers `POST /api/users`, `PATCH /api/users/{id}` workspaces (taking effect on the
member's next request, without a re-login), `POST /api/auth/password`, the built-in
admin's console-set password override (precedence, break-glass, session end), its
`builtin` row in the users list, and the last-admin guard.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import AuditEvent, UserWorkspace, Workspace
from app.models.platform_settings import PlatformSetting
from app.services import admin_credential
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PROBE = "/api/agents"  # a member-reachable, workspace-scoped read
NEW = {"username": "ws-attendee-01", "email": "ws-attendee-01@acme-corp.com"}


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


def create(admin: TestClient, **overrides):
    return admin.post("/api/users", json={**NEW, **overrides})


def add_workspace(workspace_id: str = "team-b") -> None:
    db = SessionLocal()
    try:
        db.add(
            Workspace(
                id=workspace_id, name=workspace_id, account_id="123456789012",
                region="us-west-2", bootstrap_status="ready",
            )
        )
        db.commit()
    finally:
        db.close()


def grants(user_id: str) -> list[str]:
    db = SessionLocal()
    try:
        return sorted(
            db.scalars(
                select(UserWorkspace.workspace_id).where(UserWorkspace.user_id == user_id)
            )
        )
    finally:
        db.close()


def audit_rows(action: str | None = None) -> list[AuditEvent]:
    db = SessionLocal()
    try:
        stmt = select(AuditEvent)
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        rows = list(db.scalars(stmt))
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def member_client(app, admin: TestClient, **overrides) -> tuple[TestClient, dict]:
    body = create(admin, password="member-pass-1", **overrides).json()
    return login(app, body["username"], "member-pass-1"), body


# ── POST /api/users ─────────────────────────────────────────────────────────


class TestCreate:
    def test_generated_password_is_returned_once_and_signs_in(self, app, admin, caplog):
        caplog.set_level(logging.DEBUG)
        response = create(admin)
        assert response.status_code == 201, response.text
        body = response.json()
        password = body["generated_password"]
        assert len(password) >= users_service.MIN_PASSWORD_LENGTH
        assert body["state"] == "active" and body["status"] == "active"
        assert body["role"] == "member"
        assert body["workspaces"] == [DEFAULT_WORKSPACE_ID]
        assert body["created_by"] == ADMIN["username"]
        # same default validity an approval starts
        assert body["days_remaining"] in (6, 7)
        # an approved registration's permission set: the member role defaults
        assert body["permissions"] == {
            key: key in users_service.default_permissions("member")
            for key in users_service.AGENT_PERMISSIONS
        }
        # never stored, logged or listed in clear
        db = SessionLocal()
        try:
            stored = users_service.find_by_username(db, NEW["username"])
            assert stored is not None and password not in stored.password_hash
            assert stored.permissions is None
        finally:
            db.close()
        assert password not in caplog.text
        listed = admin.get("/api/users", params={"q": NEW["username"]}).json()["items"]
        assert "generated_password" not in listed[0]
        # the created account is usable at once — no approval step
        member = login(app, NEW["username"], password)
        assert member.get(PROBE).status_code == 200

    def test_provided_password_is_not_echoed(self, app, admin):
        response = create(admin, password="chosen-pass-1")
        assert response.status_code == 201
        assert "generated_password" not in response.json()
        assert login(app, NEW["username"], "chosen-pass-1").get(PROBE).status_code == 200

    def test_password_policy_applies(self, admin):
        response = create(admin, password="short")
        assert response.status_code == 400
        assert response.json()["code"] == "auth.weak_password"

    def test_duplicate_username_and_email_are_refused_case_insensitively(self, admin):
        assert create(admin).status_code == 201
        dup_name = create(admin, username=NEW["username"].upper(), email="other@acme-corp.com")
        assert dup_name.status_code == 409
        assert dup_name.json()["code"] == "auth.username_taken"
        dup_mail = create(admin, username="someone-else", email=NEW["email"].upper())
        assert dup_mail.status_code == 409
        assert dup_mail.json()["code"] == "auth.email_taken"

    def test_duplicate_of_a_self_registration(self, admin, app):
        anon = TestClient(app)
        assert anon.post(
            "/api/auth/register", json={**NEW, "password": "reg-pass-123"}
        ).status_code == 201
        assert create(admin).status_code == 409

    @pytest.mark.parametrize("name", ["operator", "OPERATOR", " Operator "])
    def test_reserved_builtin_name(self, admin, name):
        response = create(admin, username=name)
        assert response.status_code == 409
        assert response.json()["code"] == "auth.username_taken"

    def test_username_and_email_rules_match_registration(self, admin):
        assert create(admin, username="a").json()["code"] == "auth.invalid_username"
        assert create(admin, email="not-an-email").json()["code"] == "auth.invalid_email"
        blocked = create(admin, email="someone@gmail.com")
        assert blocked.json()["code"] == "auth.email_domain_blocked"

    def test_non_admin_is_refused(self, app, admin):
        member, _ = member_client(app, admin)
        response = member.post("/api/users", json={**NEW, "username": "x-user-2",
                                                   "email": "x2@acme-corp.com"})
        assert response.status_code == 403
        assert response.json()["code"] == "auth.forbidden"
        # only an admin can grant admin
        response = member.post("/api/users", json={"username": "x-admin", "role": "admin",
                                                   "email": "xa@acme-corp.com"})
        assert response.status_code == 403
        assert TestClient(app).post("/api/users", json=NEW).status_code == 401

    def test_unknown_workspace_creates_nothing(self, admin):
        response = create(admin, workspaces=["default", "nope"])
        assert response.status_code == 400
        assert response.json()["code"] == "users.invalid_workspaces"
        assert admin.get("/api/users", params={"q": NEW["username"]}).json()["total"] == 0

    def test_explicit_workspaces(self, admin):
        add_workspace()
        body = create(admin, workspaces=["team-b"]).json()
        assert body["workspaces"] == ["team-b"]

    def test_expiry_variants(self, admin):
        never = create(admin, never_expires=True).json()
        assert never["expires_at"] is None and never["days_remaining"] is None
        days = create(admin, username="u-thirty", email="u30@acme-corp.com", valid_days=30)
        assert days.json()["days_remaining"] in (29, 30)
        moment = (datetime.now(UTC) + timedelta(days=3, hours=1)).isoformat()
        at = create(admin, username="u-at", email="uat@acme-corp.com", expires_at=moment)
        assert at.json()["days_remaining"] == 3
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        refused = create(admin, username="u-past", email="up@acme-corp.com", expires_at=past)
        assert refused.status_code == 422
        assert refused.json()["code"] == "users.invalid_expiry"
        both = create(admin, username="u-both", email="ub@acme-corp.com",
                      valid_days=3, never_expires=True)
        assert both.status_code == 422
        assert create(admin, username="u-zero", email="uz@acme-corp.com",
                      valid_days=0).status_code == 422

    def test_roles(self, app, admin):
        operator = create(admin, role="operator").json()
        assert operator["role"] == "operator"
        assert operator["permissions"]["promotion.approve"] is True
        assert operator["permissions"]["agents.deploy"] is False
        bogus = create(admin, username="u-bogus", email="ub@acme-corp.com", role="root")
        assert bogus.json()["code"] == "users.invalid_role"
        # a new administrator: no grants (every workspace by role), can manage users
        made = create(admin, username="second-admin", email="sa@acme-corp.com",
                      role="admin", password="admin-pass-2", workspaces=[])
        assert made.status_code == 201 and made.json()["workspaces"] == []
        assert login(app, "second-admin", "admin-pass-2").get("/api/users").status_code == 200
        granted = create(admin, username="third-admin", email="ta@acme-corp.com",
                         role="admin", workspaces=["default"])
        assert granted.status_code == 400
        assert granted.json()["code"] == "users.admin_no_grants"

    def test_permission_overrides(self, admin):
        body = create(admin, permissions={"agents.delete": False}).json()
        assert body["permissions"]["agents.delete"] is False
        assert body["permissions"]["agents.deploy"] is True

    def test_creation_is_audited_without_the_password(self, admin):
        password = create(admin).json()["generated_password"]
        rows = audit_rows("user.create")
        assert len(rows) == 1
        assert rows[0].actor == ADMIN["username"]
        assert rows[0].workspace_id is None
        assert NEW["username"] in rows[0].target and "generated" in rows[0].target
        assert all(password not in row.target for row in audit_rows())


# ── workspaces on PATCH ─────────────────────────────────────────────────────


class TestEditWorkspaces:
    def test_add_and_remove_take_effect_without_relogin(self, app, admin):
        add_workspace()
        member, body = member_client(app, admin)
        assert member.get(PROBE, headers={"X-Workspace": "team-b"}).status_code == 403

        added = admin.patch(f"/api/users/{body['id']}", json={"workspaces": ["default", "team-b"]})
        assert added.status_code == 200 and added.json()["workspaces"] == ["default", "team-b"]
        # the same session, no re-login: the grant is read per request
        assert member.get(PROBE, headers={"X-Workspace": "team-b"}).status_code == 200

        removed = admin.patch(f"/api/users/{body['id']}", json={"workspaces": ["team-b"]})
        assert removed.json()["workspaces"] == ["team-b"]
        refused = member.get(PROBE, headers={"X-Workspace": DEFAULT_WORKSPACE_ID})
        assert refused.status_code == 403 and refused.json()["code"] == "workspace.forbidden"
        # a single grant is also the fallback when no header is sent
        assert member.get(PROBE).status_code == 200
        assert grants(body["id"]) == ["team-b"]
        audited = [row.target for row in audit_rows("user.update")]
        assert any("workspaces=team-b" in target for target in audited)

    def test_zero_workspaces_is_the_backend_rule_and_locks_every_environment(self, app, admin):
        member, body = member_client(app, admin)
        cleared = admin.patch(f"/api/users/{body['id']}", json={"workspaces": []})
        assert cleared.status_code == 200 and cleared.json()["workspaces"] == []
        refused = member.get(PROBE)
        assert refused.status_code == 403 and refused.json()["code"] == "workspace.forbidden"

    def test_unknown_workspace_changes_nothing(self, app, admin):
        _, body = member_client(app, admin)
        response = admin.patch(f"/api/users/{body['id']}", json={"workspaces": ["ghost"]})
        assert response.status_code == 400
        assert response.json()["code"] == "users.invalid_workspaces"
        assert grants(body["id"]) == [DEFAULT_WORKSPACE_ID]

    def test_non_admin_is_refused(self, app, admin):
        member, body = member_client(app, admin)
        response = member.patch(f"/api/users/{body['id']}", json={"workspaces": ["default"]})
        assert response.status_code == 403


# ── the built-in admin and password changes ─────────────────────────────────


def override_row() -> PlatformSetting | None:
    db = SessionLocal()
    try:
        row = db.get(PlatformSetting, admin_credential.OVERRIDE_KEY)
        if row is not None:
            db.expunge(row)
        return row
    finally:
        db.close()


def change_builtin_password(admin: TestClient, new: str = "brand-new-1") -> None:
    response = admin.post(
        "/api/auth/password", json={"current_password": ADMIN["password"], "new_password": new}
    )
    assert response.status_code == 200, response.text


class TestBuiltinAdmin:
    def test_listed_as_builtin_outside_the_page_total(self, admin):
        listing = admin.get("/api/users").json()
        assert listing["total"] == 0
        builtin = listing["builtin"]
        assert builtin["id"] == admin_credential.BUILTIN_ADMIN_ID
        assert builtin["username"] == ADMIN["username"] and builtin["builtin"] is True
        assert builtin["role"] == "admin" and builtin["password_source"] == "config"
        assert admin.get("/api/users", params={"q": "zzz"}).json()["builtin"] is None
        assert admin.get("/api/users", params={"status": "pending"}).json()["builtin"] is None

    def test_change_own_password_overrides_config_and_ends_other_sessions(self, app, admin):
        other = login(app, **ADMIN)
        wrong = admin.post("/api/auth/password",
                           json={"current_password": "nope-nope", "new_password": "brand-new-1"})
        assert wrong.status_code == 400
        assert wrong.json()["code"] == "auth.invalid_current_password"
        weak = admin.post("/api/auth/password",
                          json={"current_password": ADMIN["password"], "new_password": "short"})
        assert weak.json()["code"] == "auth.weak_password"

        changed = admin.post("/api/auth/password", json={
            "current_password": ADMIN["password"], "new_password": "brand-new-1"})
        assert changed.status_code == 200, changed.text
        # this session continues (re-issued cookie); the other one is over
        assert admin.get("/api/users").status_code == 200
        assert other.get("/api/users").status_code == 401
        # the configured password stops working; the new one signs in
        refused = TestClient(app).post("/api/auth/login", json=ADMIN)
        assert refused.status_code == 401
        login(app, ADMIN["username"], "brand-new-1")
        row = override_row()
        assert row is not None and "brand-new-1" not in row.value
        assert admin.get("/api/users").json()["builtin"]["password_source"] == "console"
        rows = audit_rows("auth.password_change")
        assert len(rows) == 1 and "builtin" in rows[0].target
        assert all("brand-new-1" not in r.target for r in audit_rows())

    def test_break_glass_clear_restores_the_configured_password(self, app, admin):
        change_builtin_password(admin)
        db = SessionLocal()
        try:
            assert admin_credential.clear(db) is True
        finally:
            db.close()
        # the override's sessions end with it, and the config password works again
        assert admin.get("/api/users").status_code == 401
        login(app, **ADMIN)
        assert TestClient(app).post("/api/auth/login", json={
            "username": ADMIN["username"], "password": "brand-new-1"}).status_code == 401

    def test_break_glass_flag_ignores_the_override(self, app, admin, monkeypatch):
        change_builtin_password(admin)
        monkeypatch.setenv("LAUNCHPAD_AUTH_ADMIN_PASSWORD_OVERRIDE_DISABLED", "true")
        get_settings.cache_clear()
        login(app, **ADMIN)

    def test_a_malformed_row_cannot_lock_the_console(self, app):
        db = SessionLocal()
        try:
            db.add(PlatformSetting(key=admin_credential.OVERRIDE_KEY, value="garbage"))
            db.commit()
        finally:
            db.close()
        login(app, **ADMIN)

    def test_another_admin_resets_the_builtin_password(self, app, admin):
        create(admin, username="second-admin", email="sa@acme-corp.com",
               role="admin", password="admin-pass-2", workspaces=[])
        second = login(app, "second-admin", "admin-pass-2")
        reset = second.patch(f"/api/users/{admin_credential.BUILTIN_ADMIN_ID}",
                             json={"password": None})
        assert reset.status_code == 200, reset.text
        generated = reset.json()["generated_password"]
        assert reset.json()["password_source"] == "console"
        assert admin.get("/api/users").status_code == 401  # its old session ended
        login(app, ADMIN["username"], generated)
        rows = audit_rows("admin.password_change")
        assert rows and rows[0].actor == "second-admin" and generated not in rows[0].target

    def test_builtin_is_fixed_except_its_password(self, admin):
        target = f"/api/users/{admin_credential.BUILTIN_ADMIN_ID}"
        for body in ({"role": "member"}, {"status": "disabled"}, {"expires_at": None},
                     {"workspaces": []}):
            response = admin.patch(target, json=body)
            assert response.status_code == 400
            assert response.json()["code"] == "users.builtin_immutable"
        assert admin.delete(target).json()["code"] == "users.builtin_immutable"
        weak = admin.patch(target, json={"password": "short"})
        assert weak.json()["code"] == "auth.weak_password"

    def test_registered_account_changes_its_own_password(self, app, admin):
        member, body = member_client(app, admin)
        other = login(app, body["username"], "member-pass-1")
        changed = member.post("/api/auth/password", json={
            "current_password": "member-pass-1", "new_password": "member-pass-2"})
        assert changed.status_code == 200
        assert member.get(PROBE).status_code == 200
        assert other.get(PROBE).status_code == 401
        login(app, body["username"], "member-pass-2")
        assert TestClient(app).post("/api/auth/login", json={
            "username": body["username"], "password": "member-pass-1"}).status_code == 401

    def test_change_password_needs_a_session(self, app):
        response = TestClient(app).post("/api/auth/password", json={
            "current_password": "x", "new_password": "yyyyyyyy"})
        assert response.status_code == 401


def test_change_password_without_a_gate(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_AUTH_PASSWORD", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        response = client.post("/api/auth/password", json={
            "current_password": "x", "new_password": "yyyyyyyy"})
        assert response.status_code == 400
        assert response.json()["code"] == "auth.not_enabled"
        assert client.get("/api/users").json()["builtin"] is None
    get_settings.cache_clear()


# ── administrators ──────────────────────────────────────────────────────────


class TestAdministrators:
    def test_promote_and_demote_a_member(self, app, admin):
        _, body = member_client(app, admin)
        promoted = admin.patch(f"/api/users/{body['id']}", json={"role": "admin"})
        assert promoted.json()["role"] == "admin"
        # the built-in admin still counts, so a registered admin can be demoted
        assert admin.patch(f"/api/users/{body['id']}", json={"role": "member"}).status_code == 200

    def test_last_admin_guard(self, app, admin, monkeypatch):
        create(admin, username="solo-admin", email="solo@acme-corp.com",
               role="admin", password="admin-pass-2", workspaces=[])
        solo = login(app, "solo-admin", "admin-pass-2")
        me = solo.get("/api/users", params={"q": "solo-admin"}).json()["items"][0]
        # as if no built-in admin could sign in: solo-admin is the last one
        monkeypatch.setattr(users_service, "builtin_admin_enabled", lambda settings=None: False)
        for body in ({"role": "member"}, {"status": "disabled"},
                     {"expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}):
            refused = solo.patch(f"/api/users/{me['id']}", json=body)
            assert refused.status_code == 409, body
            assert refused.json()["code"] == "users.last_admin"
        assert solo.delete(f"/api/users/{me['id']}").json()["code"] == "users.last_admin"
        still = solo.get("/api/users", params={"q": "solo-admin"}).json()["items"][0]
        assert still["role"] == "admin" and still["state"] == "active"
        # with a second admin the same demotion goes through
        create(solo, username="other-admin", email="oa@acme-corp.com", role="admin",
               workspaces=[])
        assert solo.patch(f"/api/users/{me['id']}", json={"role": "member"}).status_code == 200

    def test_with_the_builtin_admin_a_registered_admin_may_step_down(self, app, admin):
        create(admin, username="solo-admin", email="solo@acme-corp.com",
               role="admin", password="admin-pass-2", workspaces=[])
        solo = login(app, "solo-admin", "admin-pass-2")
        me = solo.get("/api/users", params={"q": "solo-admin"}).json()["items"][0]
        assert solo.patch(f"/api/users/{me['id']}", json={"role": "member"}).status_code == 200


def test_break_glass_script(app, admin, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "reset_admin_password.py"
    spec = importlib.util.spec_from_file_location("reset_admin_password", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    change_builtin_password(admin)
    monkeypatch.setattr("sys.argv", ["reset_admin_password.py", "--status"])
    assert module.main() == 0
    assert "console" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["reset_admin_password.py"])
    assert module.main() == 0
    out = capsys.readouterr().out
    assert out.startswith("cleared") and "brand-new-1" not in out
    login(app, **ADMIN)
    assert audit_rows("admin.password_reset_to_config")
    assert module.main() == 0
    assert capsys.readouterr().out.startswith("nothing to clear")


def test_status_marks_the_builtin_admin(app, admin):
    assert admin.get("/api/auth/status").json()["builtin"] is True
    member, _ = member_client(app, admin)
    assert member.get("/api/auth/status").json()["builtin"] is False
