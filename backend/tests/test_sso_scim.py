"""SCIM 2.0 provisioning (docs/design sso §2.4, SSO-3): the bearer token, Users and
Groups CRUD, deactivation ending sessions, and group membership driving the same
mappings a login applies."""

from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.models.ledger import AuditEvent, User
from app.models.sso import ConsoleIdentityProvider
from app.routers.auth import COOKIE_NAME, issue_user_session
from app.services import sso_mapping, sso_saml, sso_scim

BASE = "http://console.test"
SCIM = "/api/scim/v2"
ADMIN = {"username": "operator", "password": "s3cret-pass"}
ROLE_MAPPING = [{"group": "Launchpad Operators", "kind": "role", "target_id": "",
                 "role_key": "operator"}]


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setenv("LAUNCHPAD_PUBLIC_BASE_URL", BASE)
    monkeypatch.delenv("LAUNCHPAD_SSO_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(gated):
    with TestClient(gated, client=("127.0.0.1", 4321)) as c:
        assert c.post("/api/auth/login", json=ADMIN).status_code == 200
        yield c


def _provider(provider_id: str = "entra", **fields: Any) -> None:
    db = SessionLocal()
    try:
        row = ConsoleIdentityProvider(**{
            "id": provider_id, "name": "Entra", "kind": "saml", "preset": "entra-saml",
            "enabled": True, "claims": dict(sso_saml.PRESETS["saml"]["claims"]),
            "mappings": [], "allowed_email_domains": [], "saml_idp_certs": [],
            "scim_subject": "userName", **fields,
        })
        db.add(row)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def scim(admin, gated):
    """A SCIM client holding the `entra` provider's freshly minted token."""
    _provider(mappings=ROLE_MAPPING)
    minted = admin.post("/api/admin/sso/providers/entra/scim/token")
    assert minted.status_code == 200, minted.text
    token = minted.json()["token"]
    assert minted.json()["scim"]["enabled"] and token.endswith(minted.json()["scim"]["token_hint"])
    with TestClient(gated, client=("10.0.0.9", 4321)) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def _alice(**overrides: Any) -> dict[str, Any]:
    return {
        "schemas": [sso_scim.SCHEMA_USER], "userName": "alice@acme-corp.com",
        "externalId": "00u-alice", "name": {"givenName": "Alice", "familyName": "Liddell"},
        "displayName": "Alice Liddell", "active": True,
        "emails": [{"value": "alice@acme-corp.com", "type": "work", "primary": True}],
        **overrides,
    }


def _user(user_id: str) -> User:
    db = SessionLocal()
    try:
        return db.get(User, user_id)
    finally:
        db.close()


def _session_for(gated, user_id: str) -> TestClient:
    """A browser holding a live console session of the account."""
    response = Response()
    issue_user_session(response, _user(user_id))
    cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    browser = TestClient(gated, client=("127.0.0.1", 4321))
    browser.cookies.set(COOKIE_NAME, cookie)
    return browser


class TestAuth:
    def test_no_token_is_a_scim_401(self, gated):
        _provider()
        with TestClient(gated, client=("10.0.0.9", 4321)) as c:
            answer = c.get(f"{SCIM}/Users")
        assert answer.status_code == 401
        assert answer.headers["content-type"].startswith("application/scim+json")
        assert answer.json()["schemas"] == [sso_scim.SCHEMA_ERROR]

    def test_a_wrong_token_is_refused(self, scim):
        scim.headers["Authorization"] = "Bearer lpscim_not-the-token"
        assert scim.get(f"{SCIM}/Users").status_code == 401

    def test_rotating_ends_the_old_token_and_revoking_ends_scim(self, scim, admin):
        assert scim.get(f"{SCIM}/Users").status_code == 200
        fresh = admin.post("/api/admin/sso/providers/entra/scim/token").json()["token"]
        assert scim.get(f"{SCIM}/Users").status_code == 401
        scim.headers["Authorization"] = f"Bearer {fresh}"
        assert scim.get(f"{SCIM}/ServiceProviderConfig").json()["patch"]["supported"]
        revoked = admin.delete("/api/admin/sso/providers/entra/scim/token").json()
        assert revoked["scim"]["enabled"] is False
        assert scim.get(f"{SCIM}/Users").status_code == 401

    def test_the_token_is_stored_as_a_hash_only(self, scim):
        db = SessionLocal()
        try:
            row = db.get(ConsoleIdentityProvider, "entra")
            token = scim.headers["Authorization"].split()[1]
            assert row.scim_token_hash == sso_scim.token_hash(token)
            assert token not in (row.scim_token_hash or "")
        finally:
            db.close()

    def test_minting_a_token_is_for_administrators(self, gated):
        _provider()
        with TestClient(gated, client=("127.0.0.1", 4321)) as anon:
            assert anon.post("/api/admin/sso/providers/entra/scim/token").status_code == 401


class TestUsers:
    def test_create_get_list_and_filter(self, scim):
        created = scim.post(f"{SCIM}/Users", json=_alice())
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["userName"] == "alice@acme-corp.com" and body["active"] is True
        assert body["externalId"] == "00u-alice"
        assert body["meta"]["location"] == f"{BASE}{SCIM}/Users/{body['id']}"
        user = _user(body["id"])
        assert user.external_idp_id == "entra"
        assert user.external_subject == "alice@acme-corp.com"
        assert user.email == "alice@acme-corp.com" and user.status == "active"
        assert scim.get(f"{SCIM}/Users/{body['id']}").json()["displayName"] == "Alice Liddell"
        found = scim.get(f"{SCIM}/Users",
                         params={"filter": 'userName eq "ALICE@acme-corp.com"'}).json()
        assert found["totalResults"] == 1 and found["Resources"][0]["id"] == body["id"]
        none = scim.get(f"{SCIM}/Users", params={"filter": 'userName eq "bob"'}).json()
        assert none["totalResults"] == 0 and none["Resources"] == []
        bad = scim.get(f"{SCIM}/Users", params={"filter": 'title sw "x"'})
        assert bad.status_code == 400 and bad.json()["scimType"] == "invalidFilter"

    def test_a_duplicate_is_a_uniqueness_conflict(self, scim):
        scim.post(f"{SCIM}/Users", json=_alice())
        again = scim.post(f"{SCIM}/Users", json=_alice())
        assert again.status_code == 409 and again.json()["scimType"] == "uniqueness"

    def test_an_email_held_by_a_local_account_is_refused(self, scim):
        db = SessionLocal()
        try:
            from app.services import users as users_service

            users_service.create_user(db, username="alice-local", email="alice@acme-corp.com",
                                      password="long-enough-pass", created_by="t")
        finally:
            db.close()
        answer = scim.post(f"{SCIM}/Users", json=_alice())
        assert answer.status_code == 409

    def test_a_jit_account_is_adopted_not_duplicated(self, scim):
        db = SessionLocal()
        try:
            row = db.get(ConsoleIdentityProvider, "entra")
            profile = sso_mapping.ExternalProfile(
                subject="alice@acme-corp.com", username="alice", email="alice@acme-corp.com",
                name="Alice")
            jit, created = sso_mapping.provision(db, row, profile)
            assert created
            jit_id = jit.id
        finally:
            db.close()
        adopted = scim.post(f"{SCIM}/Users", json=_alice())
        assert adopted.status_code == 201 and adopted.json()["id"] == jit_id

    def test_entra_style_deactivation_ends_sessions(self, scim, gated):
        user_id = scim.post(f"{SCIM}/Users", json=_alice()).json()["id"]
        with _session_for(gated, user_id) as browser:
            assert browser.get("/api/auth/status").json()["authenticated"] is True
            patched = scim.patch(f"{SCIM}/Users/{user_id}", json={
                "schemas": [sso_scim.SCHEMA_PATCH],
                "Operations": [{"op": "Replace", "path": "active", "value": "False"}],
            })
            assert patched.status_code == 200, patched.text
            assert patched.json()["active"] is False
            assert browser.get("/api/auth/status").json()["authenticated"] is False
        user = _user(user_id)
        assert user.status == "disabled" and user.session_epoch == 1
        # reactivated: the account works again, the old cookie stays dead
        back = scim.patch(f"{SCIM}/Users/{user_id}", json={
            "schemas": [sso_scim.SCHEMA_PATCH],
            "Operations": [{"op": "replace", "value": {"active": True}}],
        })
        assert back.json()["active"] is True and _user(user_id).status == "active"
        db = SessionLocal()
        try:
            actions = set(db.scalars(select(AuditEvent.action).where(
                AuditEvent.actor == "scim:entra")))
        finally:
            db.close()
        assert {"auth.scim.user.create", "auth.scim.user.update"} <= actions

    def test_patch_paths_and_replace(self, scim):
        user_id = scim.post(f"{SCIM}/Users", json=_alice()).json()["id"]
        patched = scim.patch(f"{SCIM}/Users/{user_id}", json={
            "schemas": [sso_scim.SCHEMA_PATCH],
            "Operations": [
                {"op": "replace", "path": "name.givenName", "value": "Al"},
                {"op": "replace", "path": 'emails[type eq "work"].value',
                 "value": "al@acme-corp.com"},
                {"op": "add", "path": "displayName", "value": "Al L."},
            ],
        }).json()
        assert patched["name"]["givenName"] == "Al" and patched["displayName"] == "Al L."
        assert _user(user_id).email == "al@acme-corp.com"
        replaced = scim.put(f"{SCIM}/Users/{user_id}",
                            json=_alice(displayName="Alice Again", active=False))
        assert replaced.status_code == 200 and replaced.json()["displayName"] == "Alice Again"
        assert _user(user_id).status == "disabled"

    def test_delete_disables_and_hides_the_account(self, scim, gated):
        user_id = scim.post(f"{SCIM}/Users", json=_alice()).json()["id"]
        with _session_for(gated, user_id) as browser:
            assert scim.delete(f"{SCIM}/Users/{user_id}").status_code == 204
            assert browser.get("/api/auth/status").json()["authenticated"] is False
        assert scim.get(f"{SCIM}/Users/{user_id}").status_code == 404
        assert scim.get(f"{SCIM}/Users").json()["totalResults"] == 0
        assert _user(user_id).status == "disabled"  # the row stays for the journal

    def test_another_provider_s_token_does_not_see_the_account(self, scim, admin, gated):
        user_id = scim.post(f"{SCIM}/Users", json=_alice()).json()["id"]
        _provider("okta", name="Okta")
        token = admin.post("/api/admin/sso/providers/okta/scim/token").json()["token"]
        with TestClient(gated, client=("10.0.0.9", 4321)) as other:
            other.headers["Authorization"] = f"Bearer {token}"
            assert other.get(f"{SCIM}/Users/{user_id}").status_code == 404
            assert other.get(f"{SCIM}/Users").json()["totalResults"] == 0
            assert other.delete(f"{SCIM}/Users/{user_id}").status_code == 404

    def test_external_id_as_the_subject(self, scim):
        db = SessionLocal()
        try:
            db.get(ConsoleIdentityProvider, "entra").scim_subject = "externalId"
            db.commit()
        finally:
            db.close()
        user_id = scim.post(f"{SCIM}/Users", json=_alice()).json()["id"]
        assert _user(user_id).external_subject == "00u-alice"
        missing = scim.post(f"{SCIM}/Users", json=_alice(userName="bob@acme-corp.com",
                                                          externalId=None,
                                                          emails=[]))
        assert missing.status_code == 400


class TestGroups:
    def test_membership_drives_the_mappings(self, scim):
        user_id = scim.post(f"{SCIM}/Users", json=_alice()).json()["id"]
        assert _user(user_id).role == "member"
        group = scim.post(f"{SCIM}/Groups", json={
            "schemas": [sso_scim.SCHEMA_GROUP], "displayName": "Launchpad Operators",
            "members": [{"value": user_id}, {"value": "not-a-scim-user"}],
        })
        assert group.status_code == 201, group.text
        body = group.json()
        assert [m["value"] for m in body["members"]] == [user_id]
        assert _user(user_id).role == "operator"
        user = scim.get(f"{SCIM}/Users/{user_id}").json()
        assert user["groups"] == [{"value": body["id"], "display": "Launchpad Operators"}]
        # Entra removes a member with a value filter
        removed = scim.patch(f"{SCIM}/Groups/{body['id']}", json={
            "schemas": [sso_scim.SCHEMA_PATCH],
            "Operations": [{"op": "Remove", "path": f'members[value eq "{user_id}"]'}],
        })
        assert removed.status_code == 200 and removed.json()["members"] == []
        assert _user(user_id).role == "member"
        added = scim.patch(f"{SCIM}/Groups/{body['id']}", json={
            "schemas": [sso_scim.SCHEMA_PATCH],
            "Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}],
        })
        assert len(added.json()["members"]) == 1 and _user(user_id).role == "operator"
        assert scim.delete(f"{SCIM}/Groups/{body['id']}").status_code == 204
        assert _user(user_id).role == "member"
        assert scim.get(f"{SCIM}/Groups/{body['id']}").status_code == 404

    def test_list_filter_rename_and_conflict(self, scim):
        group_id = scim.post(f"{SCIM}/Groups", json={"displayName": "Eng"}).json()["id"]
        clash = scim.post(f"{SCIM}/Groups", json={"displayName": "eng"})
        assert clash.status_code == 409
        listed = scim.get(f"{SCIM}/Groups", params={"filter": 'displayName eq "Eng"',
                                                    "excludedAttributes": "members"}).json()
        assert listed["totalResults"] == 1 and "members" not in listed["Resources"][0]
        renamed = scim.patch(f"{SCIM}/Groups/{group_id}", json={
            "schemas": [sso_scim.SCHEMA_PATCH],
            "Operations": [{"op": "replace", "value": {"displayName": "Engineering"}}],
        })
        assert renamed.json()["displayName"] == "Engineering"
        replaced = scim.put(f"{SCIM}/Groups/{group_id}",
                            json={"displayName": "Eng 2", "members": []})
        assert replaced.json()["displayName"] == "Eng 2"

    def test_a_malformed_body_is_a_scim_error(self, scim):
        answer = scim.post(f"{SCIM}/Groups", content=b"{not json",
                           headers={"Content-Type": "application/scim+json"})
        assert answer.status_code == 400 and answer.json()["scimType"] == "invalidSyntax"
        bad_op = scim.patch(f"{SCIM}/Groups/x", json={"Operations": [{"op": "move"}]})
        assert bad_op.status_code == 404  # the group is checked first

    def test_a_login_keeps_the_scim_groups(self, scim):
        """A provider whose assertion carries no groups: the SCIM-pushed ones still
        map at sign-in."""
        user_id = scim.post(f"{SCIM}/Users", json=_alice()).json()["id"]
        scim.post(f"{SCIM}/Groups", json={"displayName": "Launchpad Operators",
                                          "members": [{"value": user_id}]})
        db = SessionLocal()
        try:
            row = db.get(ConsoleIdentityProvider, "entra")
            user = db.get(User, user_id)
            groups = sso_mapping.login_groups(db, row, user, [])
            assert groups == ["Launchpad Operators"]
            assert sso_mapping.apply(db, row, user, groups)["role"] is None  # already set
            assert db.get(User, user_id).role == "operator"
        finally:
            db.close()

    def test_the_admin_page_shows_the_counts(self, scim, admin):
        scim.post(f"{SCIM}/Users", json=_alice())
        scim.post(f"{SCIM}/Groups", json={"displayName": "Eng"})
        provider = admin.get("/api/admin/sso/providers/entra").json()
        assert provider["scim"]["counts"] == {"users": 1, "groups": 1}
        assert provider["scim"]["base_url"] == f"{BASE}{SCIM}"
