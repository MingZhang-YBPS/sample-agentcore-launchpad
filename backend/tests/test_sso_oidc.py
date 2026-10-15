"""Console SSO over OIDC (docs/design sso, SSO-1) against a local fake IdP.

The fake answers discovery, the JWKS and the token endpoint through
`sso_oidc._http_json` (the module's one HTTP seam) and signs id_tokens with a test
RSA key, so every check of the callback — signature, iss, aud, exp, nonce, the
one-time state — is exercised without a network.
"""

import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.models.ledger import AuditEvent, User
from app.models.projects import GroupMember, Project, ProjectGroup, ProjectMember
from app.models.sso import ConsoleIdentityProvider, SsoLoginState
from app.services import sso_oidc
from app.services import users as users_service

ISSUER = "https://idp.test/realms/acme"
CLIENT_ID = "launchpad"
ADMIN = {"username": "operator", "password": "s3cret-pass"}


class FakeIdP:
    """Discovery + JWKS + token endpoint. `claims` shape the next id_token."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})
        self.sign_key = self.key
        self.claims: dict[str, Any] = {}
        self.token_requests: list[dict[str, Any]] = []
        self.discovery = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
            "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
            "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
            "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
            "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
            "code_challenge_methods_supported": ["plain", "S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }
        self.nonce = ""

    def id_token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER, "aud": CLIENT_ID, "sub": "subject-1", "iat": now,
            "exp": now + 300, "nonce": self.nonce, "preferred_username": "alice",
            "email": "alice@acme-corp.com", "name": "Alice", "groups": [],
            **self.claims, **overrides,
        }
        return jwt.encode({"alg": "RS256", "kid": self.sign_key.kid}, claims, self.sign_key)

    def http(self, url: str, *, data=None, headers=None) -> dict[str, Any]:
        if url.endswith("/.well-known/openid-configuration"):
            return dict(self.discovery)
        if url == self.discovery["jwks_uri"]:
            return {"keys": [self.key.as_dict(private=False)]}
        if url == self.discovery["token_endpoint"]:
            self.token_requests.append({"data": data, "headers": headers})
            return {"access_token": "at", "token_type": "Bearer", "id_token": self.id_token()}
        if url == self.discovery["userinfo_endpoint"]:
            return {"sub": "subject-1"}
        raise AssertionError(f"unexpected IdP call {url}")


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(sso_oidc, "_http_json", fake.http)
    sso_oidc.reset_caches()
    yield fake
    sso_oidc.reset_caches()


@pytest.fixture
def gated(monkeypatch, tmp_path):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setenv("LAUNCHPAD_PUBLIC_BASE_URL", "http://console.test")
    monkeypatch.delenv("LAUNCHPAD_SSO_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(gated):
    with TestClient(gated, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


def _provider(**fields: Any) -> None:
    db = SessionLocal()
    try:
        values: dict[str, Any] = {
            "id": "kc", "name": "Acme SSO", "preset": "keycloak", "enabled": True,
            "issuer": ISSUER, "client_id": CLIENT_ID,
            "client_secret_enc": sso_oidc.seal_secret("shh"),
            "claims": dict(sso_oidc.PRESETS["keycloak"]["claims"]), "mappings": [],
            "allowed_email_domains": [], **fields,
        }
        db.add(ConsoleIdentityProvider(**values))
        db.commit()
    finally:
        db.close()


def _start(client: TestClient, idp: FakeIdP, return_to: str = "/v3/agents") -> dict[str, str]:
    answer = client.get("/api/auth/oidc/kc/start", params={"return_to": return_to},
                        follow_redirects=False)
    assert answer.status_code == 302, answer.text
    location = answer.headers["location"]
    assert location.startswith(idp.discovery["authorization_endpoint"])
    query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    idp.nonce = query["nonce"]
    return query


def _callback(client: TestClient, state: str, **params: str):
    return client.get("/api/auth/oidc/kc/callback",
                      params={"code": "the-code", "state": state, **params},
                      follow_redirects=False)


def _sign_in(client: TestClient, idp: FakeIdP, **claims: Any):
    idp.claims = claims
    query = _start(client, idp)
    return _callback(client, query["state"])


def _error_of(answer) -> str | None:
    assert answer.status_code == 302, answer.text
    query = parse_qs(urlsplit(answer.headers["location"]).query)
    return query.get("sso_error", [None])[0]


def _user(subject: str = "subject-1") -> User | None:
    db = SessionLocal()
    try:
        return db.scalars(select(User).where(User.external_subject == subject)).first()
    finally:
        db.close()


def _epoch() -> int:
    db = SessionLocal()
    try:
        return int(db.execute(text("SELECT value FROM access_epoch WHERE id = 1")).scalar() or 0)
    finally:
        db.close()


class TestTheFlow:
    def test_start_redirects_with_pkce_and_stores_a_one_time_state(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            query = _start(client, idp)
        assert query["code_challenge_method"] == "S256"
        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == "http://console.test/api/auth/oidc/kc/callback"
        assert query["scope"] == "openid email profile"
        db = SessionLocal()
        try:
            state = db.get(SsoLoginState, query["state"])
            assert state is not None and state.nonce == query["nonce"]
            assert state.return_to == "/v3/agents"
            from authlib.oauth2.rfc7636 import create_s256_code_challenge

            assert create_s256_code_challenge(state.code_verifier) == query["code_challenge"]
        finally:
            db.close()

    def test_a_first_login_creates_the_account_and_a_session(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            answer = _sign_in(client, idp)
            assert answer.status_code == 302 and answer.headers["location"] == "/v3/agents"
            status = client.get("/api/auth/status").json()
            assert status["authenticated"] and status["username"] == "alice"
        user = _user()
        assert user is not None and user.external_idp_id == "kc"
        assert user.email == "alice@acme-corp.com" and user.created_by == "sso:kc"
        assert not users_service.verify_password("anything", user.password_hash)
        # the code exchange sent the verifier and the secret (client_secret_basic)
        sent = idp.token_requests[0]
        assert sent["data"]["code_verifier"] and sent["data"]["code"] == "the-code"
        assert sent["headers"]["Authorization"].startswith("Basic ")
        db = SessionLocal()
        try:
            row = db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.sso.login")).one()
            assert row.outcome == "success" and "user:alice" in row.target and "jit" in row.target
        finally:
            db.close()

    def test_a_second_login_finds_the_same_account_by_subject(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp)
            # a changed username claim does not create another account
            _sign_in(client, idp, preferred_username="alice2")
        db = SessionLocal()
        try:
            assert db.scalars(select(User).where(User.external_idp_id == "kc")).all().__len__() == 1
        finally:
            db.close()

    def test_the_state_is_one_time(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            query = _start(client, idp)
            assert _error_of(_callback(client, query["state"])) is None
            assert _error_of(_callback(client, query["state"])) == "auth.sso.state_invalid"
            assert _error_of(_callback(client, "forged")) == "auth.sso.state_invalid"

    def test_an_idp_error_is_explained_and_spends_the_state(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            query = _start(client, idp)
            answer = client.get("/api/auth/oidc/kc/callback",
                                params={"state": query["state"], "error": "access_denied"},
                                follow_redirects=False)
            assert _error_of(answer) == "auth.sso.idp_error"
            assert _error_of(_callback(client, query["state"])) == "auth.sso.state_invalid"

    def test_a_disabled_provider_is_not_served(self, gated, idp):
        _provider(enabled=False)
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            answer = client.get("/api/auth/oidc/kc/start", follow_redirects=False)
            assert _error_of(answer) == "sso.provider_not_found"


class TestTheIdTokenIsVerified:
    @pytest.mark.parametrize(("claims", "code"), [
        ({"iss": "https://evil.test"}, "auth.sso.bad_issuer"),
        ({"aud": "another-client"}, "auth.sso.bad_audience"),
        ({"aud": [CLIENT_ID, "other"], "azp": "other"}, "auth.sso.bad_audience"),
        ({"exp": int(time.time()) - 600}, "auth.sso.expired"),
        ({"nonce": "replayed-nonce"}, "auth.sso.bad_nonce"),
    ])
    def test_a_bad_claim_is_refused(self, gated, idp, claims, code):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            assert _error_of(_sign_in(client, idp, **claims)) == code
            assert client.get("/api/auth/status").json()["authenticated"] is False
        assert _user() is None

    def test_a_token_signed_by_another_key_is_refused(self, gated, idp):
        _provider()
        idp.sign_key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            assert _error_of(_sign_in(client, idp)) == "auth.sso.bad_signature"

    def test_a_symmetric_token_is_refused(self, idp):
        from joserfc.jwk import OctKey

        row = ConsoleIdentityProvider(id="kc", issuer=ISSUER, client_id=CLIENT_ID)
        now = int(time.time())
        forged = jwt.encode({"alg": "HS256"}, {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x",
                                               "exp": now + 60, "nonce": "n"},
                            OctKey.import_key("shh-its-the-client-secret-0123456789"))
        from app.core.errors import AppError

        with pytest.raises(AppError) as caught:
            sso_oidc.validate_id_token(row, idp.discovery, forged, "n")
        assert caught.value.code == "auth.sso.bad_signature"

    def test_clock_skew_is_tolerated(self, idp):
        row = ConsoleIdentityProvider(id="kc", issuer=ISSUER, client_id=CLIENT_ID)
        idp.nonce = "n"
        token = idp.id_token(exp=int(time.time()) - 60)
        assert sso_oidc.validate_id_token(row, idp.discovery, token, "n")["sub"] == "subject-1"


class TestAccountPolicy:
    def test_existing_only_refuses_a_new_subject(self, gated, idp):
        _provider(account_policy="existing_only")
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            assert _error_of(_sign_in(client, idp)) == "auth.sso.not_provisioned"
        assert _user() is None

    def test_the_provider_s_email_domains_apply(self, gated, idp):
        _provider(allowed_email_domains=["example.org"])
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            assert _error_of(_sign_in(client, idp)) == "auth.sso.email_domain_blocked"

    def test_the_console_s_blocklist_applies_without_a_list(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            assert _error_of(_sign_in(client, idp, email="a@gmail.com")) == \
                "auth.sso.email_domain_blocked"

    def test_an_email_held_by_a_local_account_is_never_linked(self, gated, idp):
        _provider()
        db = SessionLocal()
        try:
            users_service.create_user(db, username="alice-local", email="alice@acme-corp.com",
                                      password="long-enough-pass", created_by="t")
        finally:
            db.close()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            assert _error_of(_sign_in(client, idp)) == "auth.sso.email_taken"

    def test_a_disabled_account_cannot_sign_in(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp)
            db = SessionLocal()
            try:
                user = db.scalars(select(User).where(User.external_subject == "subject-1")).one()
                user.status = "disabled"
                db.commit()
            finally:
                db.close()
            assert _error_of(_sign_in(client, idp)) == "auth.account_disabled"

    def test_a_username_collision_gets_a_suffix(self, gated, idp):
        _provider()
        db = SessionLocal()
        try:
            users_service.create_user(db, username="alice", email="alice@other-corp.com",
                                      password="long-enough-pass", created_by="t")
        finally:
            db.close()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp)
        assert _user().username == "alice-2"


class TestNoLocalPassword:
    def test_password_routes_refuse_an_sso_account(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp)
            answer = client.post("/api/auth/password",
                                 json={"current_password": "x", "new_password": "y" * 10})
            assert answer.status_code == 409
            assert answer.json()["code"] == "auth.sso_account_no_password"
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            answer = client.post("/api/auth/login",
                                 json={"username": "alice", "password": "!sso"})
            assert answer.status_code == 401
        db = SessionLocal()
        try:
            user = db.scalars(select(User).where(User.external_subject == "subject-1")).one()
            from app.core.errors import AppError

            with pytest.raises(AppError) as caught:
                users_service.apply_patch(db, user, {"password": "new-password-123"})
            assert caught.value.code == "users.sso_account_password"
            assert users_service.serialize(user)["source"] == "sso"
        finally:
            db.close()

    def test_a_forced_logout_ends_the_sso_session(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp)
            db = SessionLocal()
            try:
                user = db.scalars(select(User).where(User.external_subject == "subject-1")).one()
                users_service.revoke_sessions(db, user)
            finally:
                db.close()
            assert client.get("/api/auth/status").json()["authenticated"] is False

    def test_logout_answers_the_rp_initiated_logout_url(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp)
            answer = client.post("/api/auth/logout").json()
        url = answer["end_session_url"]
        assert url.startswith(idp.discovery["end_session_endpoint"])
        query = parse_qs(urlsplit(url).query)
        assert query["client_id"] == [CLIENT_ID]
        assert query["post_logout_redirect_uri"] == ["http://console.test/"]


class TestMappings:
    def _seed(self) -> None:
        db = SessionLocal()
        try:
            db.add(Project(id="alpha", name="Alpha", status="active"))
            db.add(Project(id="beta", name="Beta", status="active"))
            db.add(ProjectGroup(id="g1", name="Group one"))
            db.commit()
        finally:
            db.close()
        _provider(mappings=[
            {"group": "ops", "kind": "role", "target_id": "", "role_key": "operator"},
            {"group": "dev", "kind": "project", "target_id": "alpha", "role_key": "developer"},
            {"group": "dev", "kind": "group", "target_id": "g1", "role_key": "viewer"},
        ])

    def test_groups_map_to_role_projects_and_groups_each_login(self, gated, idp):
        self._seed()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            before = _epoch()
            _sign_in(client, idp, groups=["/ops", "dev"])
            assert _epoch() > before
            user = _user()
            assert user.role == "operator"
            db = SessionLocal()
            try:
                assert db.get(ProjectMember, ("alpha", user.id)).role_key == "developer"
                assert db.get(GroupMember, ("g1", user.id)).role_key == "viewer"
                # an administrator's own membership in another project
                db.add(ProjectMember(project_id="beta", user_id=user.id, role_key="viewer"))
                db.commit()
            finally:
                db.close()
            # the IdP drops the groups: only what the mappings made goes
            _sign_in(client, idp, groups=[])
            user = _user()
            assert user.role == "member"
            db = SessionLocal()
            try:
                assert db.get(ProjectMember, ("alpha", user.id)) is None
                assert db.get(GroupMember, ("g1", user.id)) is None
                assert db.get(ProjectMember, ("beta", user.id)) is not None
            finally:
                db.close()

    def test_a_manual_role_wins_and_is_marked(self, gated, idp, admin):
        self._seed()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp, groups=["ops"])
        user = _user()
        db = SessionLocal()
        try:
            row = db.get(User, user.id)
            row.role = "member"  # an administrator's change
            db.commit()
        finally:
            db.close()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp, groups=["ops"])
        assert _user().role == "member"
        detail = admin.get(f"/api/admin/sso/users/{user.id}").json()
        assert detail["source"] == "sso"
        assert detail["sso"]["role_conflict"] == "operator"
        assert detail["sso"]["provider_name"] == "Acme SSO"

    def test_a_manual_project_role_is_left_alone(self, gated, idp):
        self._seed()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp, groups=["dev"])
            user = _user()
            db = SessionLocal()
            try:
                db.get(ProjectMember, ("alpha", user.id)).role_key = "viewer"
                db.commit()
            finally:
                db.close()
            _sign_in(client, idp, groups=[])
        db = SessionLocal()
        try:
            assert db.get(ProjectMember, ("alpha", user.id)).role_key == "viewer"
        finally:
            db.close()


class TestAdministration:
    def test_the_secret_is_write_only_and_encrypted(self, admin, idp):
        created = admin.post("/api/admin/sso/providers", json={
            "id": "kc", "name": "Acme SSO", "preset": "keycloak", "issuer": ISSUER,
            "client_id": CLIENT_ID, "client_secret": "super-secret-value",
        })
        assert created.status_code == 201, created.text
        body = created.json()
        assert "super-secret-value" not in created.text
        assert body["client_secret_set"] and body["client_secret_source"] == "encrypted"
        assert body["callback_url"] == "http://console.test/api/auth/oidc/kc/callback"
        assert body["claims"]["groups"] == "groups" and body["enabled"] is False
        db = SessionLocal()
        try:
            row = db.get(ConsoleIdentityProvider, "kc")
            assert "super-secret-value" not in (row.client_secret_enc or "")
            assert sso_oidc.client_secret(row) == "super-secret-value"
        finally:
            db.close()
        listed = admin.get("/api/admin/sso/providers")
        assert "super-secret-value" not in listed.text
        assert set(listed.json()["presets"]) >= {"keycloak", "authentik", "oidc", "okta",
                                                 "entra", "google"}
        patched = admin.patch("/api/admin/sso/providers/kc", json={
            "client_secret": "arn:aws:secretsmanager:us-east-1:111122223333:secret:sso",
            "enabled": True,
        }).json()
        assert patched["client_secret_source"] == "secrets_manager" and patched["enabled"]

    def test_issuer_must_be_https(self, admin):
        answer = admin.post("/api/admin/sso/providers", json={
            "name": "x", "issuer": "http://idp.example.com", "client_id": "c"})
        assert answer.status_code == 400 and answer.json()["code"] == "sso.invalid_url"

    def test_test_reads_discovery_and_jwks(self, admin, idp):
        _provider()
        result = admin.post("/api/admin/sso/providers/kc/test").json()
        assert result["ok"] and result["jwks_keys"] == 1 and result["pkce_s256"] is True
        idp.discovery["issuer"] = "https://other.test"
        failed = admin.post("/api/admin/sso/providers/kc/test").json()
        assert not failed["ok"] and failed["error"]["code"] == "sso.issuer_mismatch"
        assert admin.get("/api/admin/sso/providers/kc").json()["last_test"]["ok"] is False

    def test_mappings_are_validated(self, admin):
        _provider()
        bad = admin.put("/api/admin/sso/providers/kc/mappings", json={"mappings": [
            {"group": "dev", "kind": "project", "target_id": "nope", "role_key": "developer"}]})
        assert bad.status_code == 404
        bad_role = admin.put("/api/admin/sso/providers/kc/mappings", json={"mappings": [
            {"group": "dev", "kind": "role", "role_key": "root"}]})
        assert bad_role.status_code == 400
        good = admin.put("/api/admin/sso/providers/kc/mappings", json={"mappings": [
            {"group": "dev", "kind": "project", "target_id": "default", "role_key": "viewer"}]})
        assert good.status_code == 200, good.text

    def test_a_provider_with_accounts_cannot_be_deleted(self, gated, admin, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp)
        answer = admin.delete("/api/admin/sso/providers/kc")
        assert answer.status_code == 409 and answer.json()["code"] == "sso.provider_in_use"
        logins = admin.get("/api/admin/sso/providers/kc/logins").json()["items"]
        assert logins[0]["username"] == "alice" and logins[0]["jit"]

    def test_a_member_is_refused(self, gated, idp):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            _sign_in(client, idp)
            assert client.get("/api/admin/sso/providers").status_code == 403


class TestThePublicSurface:
    def test_the_login_page_lists_enabled_providers_only(self, gated):
        _provider()
        db = SessionLocal()
        try:
            db.add(ConsoleIdentityProvider(id="off", name="Off", enabled=False, issuer=ISSUER,
                                           client_id="c", claims={}, mappings=[],
                                           allowed_email_domains=[]))
            db.commit()
        finally:
            db.close()
        with TestClient(gated, client=("127.0.0.1", 4321)) as client:
            listed = client.get("/api/auth/providers")
            assert listed.status_code == 200
            assert listed.json()["providers"] == [
                {"id": "kc", "name": "Acme SSO", "kind": "oidc", "preset": "keycloak"}]
            assert client.get("/api/auth/status").json()["sso_providers"][0]["id"] == "kc"

    @pytest.mark.parametrize(("value", "expected"), [
        ("/v3/agents?x=1", "/v3/agents?x=1"),
        ("//evil.test", "/"),
        ("/\\evil.test", "/"),
        ("https://evil.test", "/"),
        ("/api/auth/logout", "/"),
        (None, "/"),
    ])
    def test_return_to_never_leaves_the_console(self, value, expected):
        assert sso_oidc.safe_return_to(value) == expected
