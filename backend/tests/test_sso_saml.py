"""Console SSO over SAML 2.0 (docs/design sso, SSO-2) and the SSO-3 pieces that ride
on it: SP-initiated SLO and hiding local login.

SAML responses are built here and signed with a test IdP key (`OneLogin_Saml2_Utils.
add_sign`, the library's own signer), so every ACS check — signature, audience, time
window, InResponseTo, replay — is exercised against real XML signatures.
"""

import base64
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from onelogin.saml2.constants import OneLogin_Saml2_Constants
from onelogin.saml2.utils import OneLogin_Saml2_Utils
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.models.ledger import AuditEvent, User
from app.models.sso import ConsoleIdentityProvider, SamlRequest, SamlSession
from app.services import sso_saml
from app.services import users as users_service

BASE = "http://console.test"
IDP_ENTITY = "https://idp.test/saml/metadata"
SSO_URL = "https://idp.test/saml/sso"
SLO_URL = "https://idp.test/saml/slo"
ACS = f"{BASE}/api/auth/saml/ic/acs"
SP_ENTITY = f"{BASE}/api/auth/saml/ic/metadata"
ADMIN = {"username": "operator", "password": "s3cret-pass"}
IDP_CERT, IDP_KEY = sso_saml.generate_keypair("test-idp")
OTHER_CERT, OTHER_KEY = sso_saml.generate_keypair("another-idp")


def _time(offset: int = 0) -> str:
    return datetime.fromtimestamp(time.time() + offset, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attributes(values: dict[str, Any]) -> str:
    out = []
    for name, value in values.items():
        items = value if isinstance(value, list) else [value]
        inner = "".join(f"<saml:AttributeValue>{v}</saml:AttributeValue>" for v in items)
        out.append(f'<saml:Attribute Name="{name}">{inner}</saml:Attribute>')
    return "".join(out)


def build_response(
    *, in_response_to: str | None = None, audience: str = SP_ENTITY,
    destination: str = ACS, lifetime: int = 300, name_id: str = "alice@acme-corp.com",
    attributes: dict[str, Any] | None = None, sign: str | None = "response",
    key: str = IDP_KEY, cert: str = IDP_CERT, assertion_id: str | None = None,
    issuer: str = IDP_ENTITY,
) -> str:
    """A base64 SAMLResponse. `sign`: "response", "assertion" or None."""
    irt = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    aid = assertion_id or f"_a{uuid.uuid4().hex}"
    attrs = _attributes(attributes if attributes is not None else {
        "email": "alice@acme-corp.com", "username": "alice", "name": "Alice",
        "groups": ["engineering"],
    })
    ns = ('xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"')
    assertion = (
        f'<saml:Assertion {ns} ID="{aid}" Version="2.0" IssueInstant="{_time()}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        "<saml:Subject>"
        f'<saml:NameID Format="{OneLogin_Saml2_Constants.NAMEID_EMAIL_ADDRESS}">{name_id}'
        "</saml:NameID>"
        f'<saml:SubjectConfirmation Method="{OneLogin_Saml2_Constants.CM_BEARER}">'
        f'<saml:SubjectConfirmationData NotOnOrAfter="{_time(lifetime)}" '
        f'Recipient="{destination}"{irt}/></saml:SubjectConfirmation></saml:Subject>'
        f'<saml:Conditions NotBefore="{_time(-60)}" NotOnOrAfter="{_time(lifetime)}">'
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>"
        "</saml:AudienceRestriction></saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{_time()}" SessionIndex="_session-1">'
        "<saml:AuthnContext><saml:AuthnContextClassRef>"
        "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
        "</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        f"<saml:AttributeStatement>{attrs}</saml:AttributeStatement>"
        "</saml:Assertion>"
    )
    if sign == "assertion":
        assertion = _signed(assertion, key, cert)
    xml = (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'{ns} ID="_r{uuid.uuid4().hex}" Version="2.0" IssueInstant="{_time()}" '
        f'Destination="{destination}"{irt}>'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>'
        f"</samlp:Status>{assertion}</samlp:Response>"
    )
    if sign == "response":
        xml = _signed(xml, key, cert)
    return base64.b64encode(xml.encode("utf-8")).decode("ascii")


def _signed(xml: str, key: str, cert: str) -> str:
    signed = OneLogin_Saml2_Utils.add_sign(
        xml, key, cert, sign_algorithm=OneLogin_Saml2_Constants.RSA_SHA256,
        digest_algorithm=OneLogin_Saml2_Constants.SHA256)
    text = signed.decode("utf-8") if isinstance(signed, bytes) else signed
    return text.split("?>", 1)[1] if text.startswith("<?xml") else text


IDP_METADATA = f"""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
    xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID="{IDP_ENTITY}">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data><ds:X509Certificate>
      {sso_saml.cert_body(IDP_CERT)}
    </ds:X509Certificate></ds:X509Data></ds:KeyInfo></md:KeyDescriptor>
    <md:SingleLogoutService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="{SLO_URL}"/>
    <md:NameIDFormat>urn:oasis:names:tc:SAML:2.0:nameid-format:persistent</md:NameIDFormat>
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="{SSO_URL}"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""


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
def client(gated):
    with TestClient(gated, client=("127.0.0.1", 4321)) as c:
        yield c


@pytest.fixture
def admin(gated):
    with TestClient(gated, client=("127.0.0.1", 4321)) as c:
        assert c.post("/api/auth/login", json=ADMIN).status_code == 200
        yield c


def _provider(**fields: Any) -> None:
    db = SessionLocal()
    try:
        row = ConsoleIdentityProvider(**{
            "id": "ic", "name": "Identity Center", "kind": "saml",
            "preset": "identity-center", "enabled": True,
            "saml_idp_entity_id": IDP_ENTITY, "saml_sso_url": SSO_URL,
            "saml_slo_url": SLO_URL, "saml_idp_certs": [sso_saml.cert_body(IDP_CERT)],
            "claims": dict(sso_saml.PRESETS["identity-center"]["claims"]),
            "mappings": [], "allowed_email_domains": [], "saml_allow_idp_initiated": True,
            **fields,
        })
        sso_saml.rotate_sp_key(row)
        db.add(row)
        db.commit()
    finally:
        db.close()


def _start(client: TestClient, return_to: str = "/v3/agents") -> str:
    """SP-initiated start; returns the AuthnRequest ID the console recorded."""
    answer = client.get("/api/auth/saml/ic/start", params={"return_to": return_to},
                        follow_redirects=False)
    assert answer.status_code == 302, answer.text
    location = answer.headers["location"]
    assert location.startswith(f"{SSO_URL}?SAMLRequest=")
    assert parse_qs(urlsplit(location).query)["RelayState"] == [return_to]
    db = SessionLocal()
    try:
        return db.scalars(select(SamlRequest.request_id)).one()
    finally:
        db.close()


def _acs(client: TestClient, saml_response: str, relay_state: str | None = None):
    data = {"SAMLResponse": saml_response}
    if relay_state is not None:
        data["RelayState"] = relay_state
    return client.post("/api/auth/saml/ic/acs", data=data, follow_redirects=False)


def _error_of(answer) -> str | None:
    assert answer.status_code in (302, 303), answer.text
    query = parse_qs(urlsplit(answer.headers["location"]).query)
    return query.get("sso_error", [None])[0]


def _user(subject: str = "alice@acme-corp.com") -> User | None:
    db = SessionLocal()
    try:
        return db.scalars(select(User).where(User.external_subject == subject)).first()
    finally:
        db.close()


class TestTheSpSide:
    def test_metadata_names_the_entity_id_acs_and_certificate(self, client):
        _provider(enabled=False)  # imported at the IdP before it is turned on here
        answer = client.get("/api/auth/saml/ic/metadata")
        assert answer.status_code == 200
        assert answer.headers["content-type"].startswith("application/samlmetadata+xml")
        body = answer.text
        assert f'entityID="{SP_ENTITY}"' in body
        assert f'Location="{ACS}"' in body
        assert "/api/auth/saml/ic/sls" in body
        db = SessionLocal()
        try:
            cert = db.get(ConsoleIdentityProvider, "ic").saml_sp_cert
        finally:
            db.close()
        assert sso_saml.cert_body(cert) in body.replace("\n", "")

    def test_an_oidc_provider_has_no_saml_metadata(self, client):
        assert client.get("/api/auth/saml/none/metadata").status_code == 404

    def test_start_redirects_with_an_authn_request(self, client):
        _provider()
        request_id = _start(client)
        assert request_id.startswith("ONELOGIN_") or request_id.startswith("_")


class TestTheAcs:
    def test_sp_initiated_sign_in_creates_the_account_and_a_session(self, client):
        _provider()
        request_id = _start(client)
        answer = _acs(client, build_response(in_response_to=request_id))
        assert answer.status_code == 303, answer.text
        assert answer.headers["location"] == "/v3/agents"
        status = client.get("/api/auth/status").json()
        assert status["authenticated"] and status["username"] == "alice"
        # an SSO session: the top bar hides "change password"
        assert status["account_source"] == "sso"
        user = _user()
        assert user is not None and user.external_idp_id == "ic"
        assert user.email == "alice@acme-corp.com"
        db = SessionLocal()
        try:
            session = db.get(SamlSession, user.id)
            assert session.name_id == "alice@acme-corp.com"
            assert session.session_index == "_session-1"
            # the request is one-time
            assert db.get(SamlRequest, request_id) is None
            event = db.scalars(select(AuditEvent).where(
                AuditEvent.action == "auth.sso.login")).one()
            assert event.outcome == "success" and "idp:ic user:alice" in event.target
        finally:
            db.close()

    def test_idp_initiated_sign_in_lands_on_the_relay_state(self, client):
        _provider()
        answer = _acs(client, build_response(), relay_state="/v3/agents")
        assert answer.status_code == 303 and answer.headers["location"] == "/v3/agents"
        assert client.get("/api/auth/status").json()["authenticated"]

    def test_an_open_redirect_relay_state_lands_on_the_root(self, client):
        _provider()
        answer = _acs(client, build_response(), relay_state="https://evil.test/")
        assert answer.headers["location"] == "/"

    def test_idp_initiated_can_be_turned_off(self, client):
        _provider(saml_allow_idp_initiated=False)
        assert _error_of(_acs(client, build_response())) == "auth.saml.unsolicited"
        assert _user() is None

    def test_a_signed_assertion_alone_is_enough(self, client):
        _provider()
        assert _error_of(_acs(client, build_response(sign="assertion"))) is None
        assert _user() is not None

    def test_the_mappings_apply_like_oidc(self, client):
        _provider(mappings=[{"group": "engineering", "kind": "role", "target_id": "",
                             "role_key": "operator"}])
        _acs(client, build_response())
        assert _user().role == "operator"


class TestTheAcsRefuses:
    @pytest.mark.parametrize(("kwargs", "code"), [
        ({"sign": None}, "auth.saml.unsigned"),
        ({"key": OTHER_KEY, "cert": OTHER_CERT}, "auth.saml.bad_signature"),
        ({"audience": "https://another-sp.test/metadata"}, "auth.saml.bad_audience"),
        ({"lifetime": -600}, "auth.saml.expired"),
        ({"issuer": "https://evil.test/saml"}, "auth.saml.bad_issuer"),
        ({"destination": "https://another-sp.test/acs"}, "auth.saml.bad_destination"),
    ])
    def test_a_bad_response_is_refused(self, client, kwargs, code):
        _provider()
        assert _error_of(_acs(client, build_response(**kwargs))) == code
        assert client.get("/api/auth/status").json()["authenticated"] is False
        assert _user() is None

    def test_a_replayed_assertion_is_refused(self, client):
        _provider()
        response = build_response()
        assert _error_of(_acs(client, response)) is None
        client.cookies.clear()
        assert _error_of(_acs(client, response)) == "auth.saml.replayed"

    def test_an_in_response_to_this_console_never_sent_is_refused(self, client):
        _provider()
        _start(client)
        response = build_response(in_response_to="_forged-request-id")
        assert _error_of(_acs(client, response)) == "auth.saml.in_response_to"
        assert _user() is None

    def test_an_sp_initiated_response_is_one_time(self, client):
        _provider()
        request_id = _start(client)
        assert _error_of(_acs(client, build_response(in_response_to=request_id))) is None
        client.cookies.clear()
        again = build_response(in_response_to=request_id)
        assert _error_of(_acs(client, again)) == "auth.saml.in_response_to"

    def test_another_provider_s_request_does_not_answer(self, client):
        _provider()
        db = SessionLocal()
        try:
            db.add(SamlRequest(request_id="_other", idp_id="kc", purpose="login"))
            db.commit()
        finally:
            db.close()
        response = build_response(in_response_to="_other")
        assert _error_of(_acs(client, response)) == "auth.saml.in_response_to"

    def test_garbage_is_refused(self, client):
        _provider()
        assert _error_of(_acs(client, "bm90IHhtbA==")) == "auth.saml.malformed"
        answer = client.post("/api/auth/saml/ic/acs", data={}, follow_redirects=False)
        assert _error_of(answer) == "auth.saml.no_response"

    def test_a_disabled_provider_is_not_served(self, client):
        _provider(enabled=False)
        assert _error_of(_acs(client, build_response())) == "sso.provider_not_found"

    def test_the_oidc_leg_does_not_serve_a_saml_provider(self, client):
        _provider()
        answer = client.get("/api/auth/oidc/ic/start", follow_redirects=False)
        assert _error_of(answer) == "sso.provider_not_found"


class TestConfiguration:
    def test_create_generates_the_sp_key_and_needs_metadata_to_enable(self, admin):
        created = admin.post("/api/admin/sso/providers",
                             json={"preset": "identity-center", "name": "AWS", "id": "aws"})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["kind"] == "saml" and body["saml"]["sp_key_set"]
        assert body["saml"]["acs_url"] == f"{BASE}/api/auth/saml/aws/acs"
        assert body["callback_url"] == body["saml"]["acs_url"]
        assert body["claims"]["email"] == "email"
        assert "BEGIN CERTIFICATE" in body["saml"]["sp_cert"]
        assert "PRIVATE" not in created.text
        refused = admin.patch("/api/admin/sso/providers/aws", json={"enabled": True})
        assert refused.status_code == 409
        loaded = admin.patch("/api/admin/sso/providers/aws",
                             json={"saml_metadata_xml": IDP_METADATA})
        assert loaded.status_code == 200, loaded.text
        saml = loaded.json()["saml"]
        assert saml["idp_entity_id"] == IDP_ENTITY and saml["sso_url"] == SSO_URL
        assert saml["slo_url"] == SLO_URL and len(saml["idp_certs"]) == 1
        assert saml["idp_certs"][0]["expired"] is False
        assert admin.patch("/api/admin/sso/providers/aws",
                           json={"enabled": True}).status_code == 200

    def test_metadata_by_url_and_the_test(self, admin, monkeypatch):
        fetched: list[str] = []

        def fake(url: str) -> str:
            fetched.append(url)
            return IDP_METADATA

        monkeypatch.setattr(sso_saml, "_http_text", fake)
        admin.post("/api/admin/sso/providers", json={"preset": "saml", "name": "S", "id": "s1"})
        url = "https://idp.test/saml/metadata"
        loaded = admin.post("/api/admin/sso/providers/s1/saml/metadata", json={"url": url})
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["saml"]["metadata_url"] == url
        assert loaded.json()["saml"]["name_id_format"].endswith("persistent")
        result = admin.post("/api/admin/sso/providers/s1/test").json()
        assert result["ok"] and result["metadata_matches"] and result["cert_valid"]
        assert fetched == [url, url]

    def test_bad_metadata_is_refused(self, admin):
        admin.post("/api/admin/sso/providers", json={"preset": "saml", "name": "S", "id": "s1"})
        answer = admin.patch("/api/admin/sso/providers/s1",
                             json={"saml_metadata_xml": "<not-metadata/>"})
        assert answer.status_code == 400
        assert answer.json()["code"] == "sso.saml_metadata_invalid"
        dtd = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
               "<x>&e;</x>")
        assert admin.patch("/api/admin/sso/providers/s1",
                           json={"saml_metadata_xml": dtd}).status_code == 400

    def test_saml_fields_are_refused_on_an_oidc_provider(self, admin):
        admin.post("/api/admin/sso/providers", json={"preset": "keycloak", "name": "K",
                                                     "id": "kc"})
        answer = admin.patch("/api/admin/sso/providers/kc",
                             json={"saml_sso_url": "https://idp.test/sso"})
        assert answer.status_code == 400

    def test_rotating_the_sp_key_changes_the_certificate(self, admin):
        created = admin.post("/api/admin/sso/providers",
                             json={"preset": "saml", "name": "S", "id": "s1"}).json()
        rotated = admin.post("/api/admin/sso/providers/s1/saml/sp-key").json()
        assert rotated["saml"]["sp_cert"] != created["saml"]["sp_cert"]

    def test_the_presets_include_saml(self, admin):
        presets = admin.get("/api/admin/sso/providers").json()["presets"]
        assert presets["identity-center"]["kind"] == "saml"
        assert presets["entra-saml"]["kind"] == "saml"
        assert presets["keycloak"]["kind"] == "oidc"


class TestSingleLogout:
    def _signed_in(self, client: TestClient) -> User:
        assert _error_of(_acs(client, build_response())) is None
        return _user()

    def test_logout_sends_a_logout_request_naming_the_session(self, client):
        _provider()
        user = self._signed_in(client)
        answer = client.post("/api/auth/logout").json()
        url = answer["end_session_url"]
        assert url.startswith(f"{SLO_URL}?SAMLRequest=")
        from onelogin.saml2.logout_request import OneLogin_Saml2_Logout_Request

        raw = parse_qs(urlsplit(url).query)["SAMLRequest"][0]
        xml = OneLogin_Saml2_Utils.decode_base64_and_inflate(raw).decode("utf-8")
        assert OneLogin_Saml2_Logout_Request.get_nameid(xml) == "alice@acme-corp.com"
        assert "_session-1" in xml
        db = SessionLocal()
        try:
            pending = db.scalars(select(SamlRequest)).one()
            assert pending.purpose == "logout"
            assert db.get(SamlSession, user.id) is None
        finally:
            db.close()
        # the IdP answers: a LogoutResponse to that request lands on the console
        response = (
            '<samlp:LogoutResponse xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="_lr{uuid.uuid4().hex}" Version="2.0" IssueInstant="{_time()}" '
            f'Destination="{BASE}/api/auth/saml/ic/sls" InResponseTo="{pending.request_id}">'
            f"<saml:Issuer>{IDP_ENTITY}</saml:Issuer>"
            '<samlp:Status><samlp:StatusCode '
            'Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            "</samlp:LogoutResponse>"
        )
        encoded = OneLogin_Saml2_Utils.deflate_and_base64_encode(response)
        landed = client.get("/api/auth/saml/ic/sls", params={"SAMLResponse": encoded},
                            follow_redirects=False)
        assert landed.status_code == 302 and landed.headers["location"] == "/"
        # and only once
        again = client.get("/api/auth/saml/ic/sls", params={"SAMLResponse": encoded},
                           follow_redirects=False)
        assert _error_of(again) == "auth.saml.in_response_to"

    def test_no_slo_endpoint_means_a_local_logout(self, client):
        _provider(saml_slo_url="")
        self._signed_in(client)
        assert "end_session_url" not in client.post("/api/auth/logout").json()

    def test_an_idp_logout_request_ends_the_account_s_sessions(self, client):
        _provider()
        user = self._signed_in(client)
        request = (
            '<samlp:LogoutRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="_lq{uuid.uuid4().hex}" Version="2.0" IssueInstant="{_time()}" '
            f'Destination="{BASE}/api/auth/saml/ic/sls">'
            f"<saml:Issuer>{IDP_ENTITY}</saml:Issuer>"
            f"<saml:NameID>alice@acme-corp.com</saml:NameID>"
            "</samlp:LogoutRequest>"
        )
        encoded = OneLogin_Saml2_Utils.deflate_and_base64_encode(request)
        answer = client.get("/api/auth/saml/ic/sls", params={"SAMLRequest": encoded},
                            follow_redirects=False)
        assert answer.status_code == 302, answer.text
        assert answer.headers["location"].startswith(f"{SLO_URL}?SAMLResponse=")
        assert client.get("/api/auth/status").json()["authenticated"] is False
        db = SessionLocal()
        try:
            assert db.get(User, user.id).session_epoch == 1
        finally:
            db.close()


class TestHideLocalLogin:
    MEMBER = {"username": "local-user", "email": "local-user@acme-corp.com",
              "password": "sufficient-pass"}

    def _local_member(self) -> None:
        db = SessionLocal()
        try:
            users_service.create_user(db, username=self.MEMBER["username"],
                                      email=self.MEMBER["email"],
                                      password=self.MEMBER["password"], created_by="t")
        finally:
            db.close()

    def test_hiding_needs_an_enabled_provider(self, admin):
        answer = admin.put("/api/admin/sso/settings", json={"hide_local_login": True})
        assert answer.status_code == 409

    def test_hidden_local_login_admits_only_the_built_in_admin(self, admin, gated):
        _provider()
        self._local_member()
        login = {"username": self.MEMBER["username"], "password": self.MEMBER["password"]}
        with TestClient(gated, client=("127.0.0.1", 4321)) as other:
            signed_in = other.post("/api/auth/login", json=login)
            assert signed_in.status_code == 200
            # a password session is a local one: "change password" stays
            assert signed_in.json()["account_source"] == "local"
            assert other.get("/api/auth/status").json()["account_source"] == "local"
        answer = admin.put("/api/admin/sso/settings", json={"hide_local_login": True})
        assert answer.status_code == 200 and answer.json()["local_login_hidden"]
        with TestClient(gated, client=("127.0.0.1", 4321)) as other:
            providers = other.get("/api/auth/providers").json()
            assert providers["hide_local_login"] is True
            assert providers["providers"][0]["kind"] == "saml"
            refused = other.post("/api/auth/login", json=login)
            assert refused.status_code == 403
            assert refused.json()["code"] == "auth.local_login_hidden"
            assert other.get("/api/auth/status").json()["registration_enabled"] is False
            # the break-glass account: always
            assert other.post("/api/auth/login", json=ADMIN).status_code == 200

    def test_the_switch_is_inert_with_every_provider_off(self, admin):
        _provider()
        admin.put("/api/admin/sso/settings", json={"hide_local_login": True})
        admin.patch("/api/admin/sso/providers/ic", json={"enabled": False})
        settings = admin.get("/api/admin/sso/settings").json()
        assert settings == {"hide_local_login": True, "local_login_hidden": False}
