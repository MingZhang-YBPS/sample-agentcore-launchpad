"""Console single sign-on over SAML 2.0 (docs/design sso §2.1-§2.2, §2.5; SSO-2).

The service provider's half, on `python3-saml` (D-6: no hand-written XML signature
checks — the library also refuses signature-wrapping layouts):

* **SP-initiated** — `begin_login` builds an AuthnRequest (HTTP-Redirect binding,
  signed when the provider asks for it) and records its ID in `sso_saml_requests`;
* **ACS** — `complete_login` reads the posted `SAMLResponse`. A response that names an
  `InResponseTo` must name a request this SP sent to this IdP within 10 minutes (the
  row is consumed first, so it is one-time); one without is IdP-initiated (the
  Identity Center portal's tile, D-4) and accepted only while the provider allows it.
  The library then checks, in strict mode: the XML schema, the signature (response or
  assertion, against the IdP certificates — an unsigned response is refused), the
  issuer, the audience (our entityID), Destination / Recipient (our ACS URL), the
  `NotBefore` / `NotOnOrAfter` window with 2 minutes of clock skew, and the
  SubjectConfirmation. Last, the assertion ID is recorded for 10 minutes: a replay of
  the same assertion is refused;
* **metadata** — `sp_metadata` is the SP's own (entityID
  `<console>/api/auth/saml/<id>/metadata`, ACS `…/acs`, SLS `…/sls`, its certificate);
  `parse_idp_metadata` / `fetch_idp_metadata` read an IdP's into entityID, SSO / SLO
  URLs and signing certificates;
* **SLO** — `end_session_url` (the logout answer's next stop) builds an SP-initiated
  LogoutRequest naming the NameID / SessionIndex of the account's last SAML sign-in
  (and hands OIDC accounts to `sso_oidc.end_session_url`); `complete_logout` checks the
  IdP's LogoutResponse — or answers an IdP-initiated LogoutRequest, ending the
  account's sessions through `session_epoch`.

The SP signing key is generated per provider (RSA 2048, self-signed certificate, ten
years) and stored like the OIDC client secret: a Fernet ciphertext keyed outside the
ledger (`sso_oidc.seal_secret`). Assertions are verified in memory and never stored.

HTTP (an IdP metadata URL) goes through `_http_text`, the seam tests replace.
"""

import base64
import hashlib
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.constants import OneLogin_Saml2_Constants
from onelogin.saml2.errors import OneLogin_Saml2_ValidationError
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
from onelogin.saml2.logout_request import OneLogin_Saml2_Logout_Request
from onelogin.saml2.logout_response import OneLogin_Saml2_Logout_Response
from onelogin.saml2.response import OneLogin_Saml2_Response
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models.ledger import User
from app.models.sso import (
    KIND_SAML,
    ConsoleIdentityProvider,
    SamlAssertion,
    SamlRequest,
    SamlSession,
)
from app.services import sso_oidc

logger = logging.getLogger(__name__)

REQUEST_TTL_SECONDS = 600
REPLAY_WINDOW_SECONDS = 600
CLOCK_SKEW_SECONDS = 120
# the library's Conditions window tolerance is a module constant (default 300 s);
# nothing else in the process uses python3-saml, so the design's 2 minutes applies
OneLogin_Saml2_Constants.ALLOWED_CLOCK_DRIFT = CLOCK_SKEW_SECONDS
_MAX_METADATA_BYTES = 1_000_000
_HTTP_TIMEOUT = 10

NAMEID_UNSPECIFIED = OneLogin_Saml2_Constants.NAMEID_UNSPECIFIED
NAMEID_EMAIL = OneLogin_Saml2_Constants.NAMEID_EMAIL_ADDRESS
NAMEID_PERSISTENT = OneLogin_Saml2_Constants.NAMEID_PERSISTENT
NAMEID_FORMATS = (NAMEID_UNSPECIFIED, NAMEID_EMAIL, NAMEID_PERSISTENT,
                  OneLogin_Saml2_Constants.NAMEID_TRANSIENT)

_ENTRA_CLAIMS = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims"
# Attribute templates (§2.1 预设模板): `claims` are SAML attribute names, read the way
# OIDC claims are; `metadata_hint` is the form's placeholder for the metadata URL.
PRESETS: dict[str, dict[str, Any]] = {
    "identity-center": {
        "kind": KIND_SAML,
        "metadata_hint": "https://portal.sso.<region>.amazonaws.com/saml/metadata/<id>",
        "name_id_format": NAMEID_PERSISTENT,
        "claims": {"username": "username", "email": "email", "name": "name",
                   "groups": "groups"},
    },
    "entra-saml": {
        "kind": KIND_SAML,
        "metadata_hint": ("https://login.microsoftonline.com/<tenant-id>/federationmetadata/"
                          "2007-06/federationmetadata.xml?appid=<app-id>"),
        "name_id_format": NAMEID_UNSPECIFIED,
        "claims": {"username": f"{_ENTRA_CLAIMS}/name",
                   "email": f"{_ENTRA_CLAIMS}/emailaddress",
                   "name": "http://schemas.microsoft.com/identity/claims/displayname",
                   "groups": "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"},
    },
    "saml": {
        "kind": KIND_SAML,
        "metadata_hint": "https://idp.example.com/saml/metadata",
        "name_id_format": NAMEID_UNSPECIFIED,
        "claims": {"username": "username", "email": "email", "name": "name",
                   "groups": "groups"},
    },
}
DEFAULT_PRESET = "saml"


def _error(code: str, message: str, status_code: int = 400, **detail: Any) -> AppError:
    return AppError(code, message, detail or None, status_code=status_code)


# --- URLs -----------------------------------------------------------------------------


def _base(settings: Settings | None = None) -> str:
    return (settings or get_settings()).public_base_url.rstrip("/")


def sp_entity_id(idp_id: str, settings: Settings | None = None) -> str:
    return f"{_base(settings)}/api/auth/saml/{idp_id}/metadata"


def acs_url(idp_id: str, settings: Settings | None = None) -> str:
    return f"{_base(settings)}/api/auth/saml/{idp_id}/acs"


def sls_url(idp_id: str, settings: Settings | None = None) -> str:
    return f"{_base(settings)}/api/auth/saml/{idp_id}/sls"


def start_url(idp_id: str, settings: Settings | None = None) -> str:
    return f"{_base(settings)}/api/auth/saml/{idp_id}/start"


def _request_data(
    url: str, *, post: dict[str, str] | None = None, get: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The library's view of "this request", built from the configured public URL —
    never from Host / X-Forwarded-* headers — so Destination and Recipient are checked
    against the URL the IdP was told to use."""
    parts = urllib.parse.urlsplit(url)
    return {
        "https": "on" if parts.scheme == "https" else "off",
        "http_host": parts.netloc.rpartition("@")[2] or "localhost",
        "script_name": parts.path,
        "get_data": dict(get or {}),
        "post_data": dict(post or {}),
    }


# --- keys and certificates ---------------------------------------------------------------


def generate_keypair(common_name: str, days: int = 3650) -> tuple[str, str]:
    """A self-signed RSA 2048 certificate and its key, PEM: `(cert, key)`."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64] or "sp")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


def rotate_sp_key(row: ConsoleIdentityProvider, settings: Settings | None = None) -> None:
    """A new SP signing key and certificate (the IdP must import the new metadata)."""
    cert, key = generate_keypair(f"launchpad-sso-{row.id}")
    row.saml_sp_cert = cert
    row.saml_sp_key_enc = sso_oidc.seal_secret(key, settings)


def _sp_key(row: ConsoleIdentityProvider) -> str:
    if not row.saml_sp_key_enc:
        return ""
    try:
        return sso_oidc._unseal(row.saml_sp_key_enc)
    except AppError as err:
        raise _error("sso.saml_key_unreadable",
                     "the SP signing key cannot be decrypted (the SSO secret key changed) "
                     "— rotate the SP key and import the new metadata at the IdP",
                     409) from err


def cert_body(pem: str) -> str:
    """A certificate's base64 body, whitespace and PEM header lines removed."""
    lines = [line.strip() for line in (pem or "").strip().splitlines()]
    return "".join(line for line in lines if line and not line.startswith("-----"))


def cert_facts(pem: str) -> dict[str, Any]:
    """`{subject, not_after, sha256, expired}` of a certificate, or `{error}`."""
    try:
        cert = x509.load_der_x509_certificate(base64.b64decode(cert_body(pem), validate=True))
    except (ValueError, TypeError) as exc:
        return {"error": f"not a certificate: {exc}"}
    not_after = cert.not_valid_after_utc
    return {
        "subject": cert.subject.rfc4514_string(),
        "not_after": not_after.isoformat(),
        "sha256": hashlib.sha256(cert.public_bytes(serialization.Encoding.DER))
        .hexdigest(),
        "expired": not_after <= datetime.now(UTC),
    }


def check_certs(values: list[str]) -> list[str]:
    """Certificates an administrator typed or uploaded: each must parse."""
    bodies: list[str] = []
    for value in values:
        body = cert_body(value)
        if not body:
            continue
        facts = cert_facts(body)
        if "error" in facts:
            raise _error("sso.saml_cert_invalid", "an IdP certificate does not parse",
                         reason=facts["error"])
        bodies.append(body)
    return list(dict.fromkeys(bodies))


# --- IdP metadata --------------------------------------------------------------------------


def _http_text(url: str) -> str:
    """GET the IdP metadata document (https, or http to this machine only)."""
    sso_oidc.check_url(url, "metadata_url")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url, headers={"Accept": "application/samlmetadata+xml, application/xml, text/xml"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # noqa: S310
            raw = response.read(_MAX_METADATA_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise _error("sso.idp_http_error", f"the identity provider answered HTTP {exc.code}",
                     502, url=url, status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _error("sso.idp_unreachable", f"the identity provider is unreachable: {exc}",
                     502, url=url) from exc
    if len(raw) > _MAX_METADATA_BYTES:
        raise _error("sso.saml_metadata_invalid", "the metadata document is over 1 MB", 502)
    return raw.decode("utf-8", "replace")


def parse_idp_metadata(xml: str) -> dict[str, Any]:
    """`{entity_id, sso_url, slo_url, certs, name_id_format}` from IdP metadata XML.
    400 `sso.saml_metadata_invalid` when it is not metadata or lacks a redirect-binding
    SSO endpoint or a signing certificate. (The parser refuses DTDs: no XXE.)"""
    text = (xml or "").strip()
    if not text or len(text.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise _error("sso.saml_metadata_invalid", "the metadata is empty or over 1 MB")
    try:
        data = OneLogin_Saml2_IdPMetadataParser.parse(text)
    except Exception as exc:  # noqa: BLE001 — lxml / parser errors of every kind
        raise _error("sso.saml_metadata_invalid", f"the metadata does not parse: {exc}") from exc
    idp = data.get("idp") or {}
    multi = (idp.get("x509certMulti") or {}).get("signing") or []
    certs = [c for c in (multi or [idp.get("x509cert")]) if c]
    found = {
        "entity_id": idp.get("entityId") or "",
        "sso_url": (idp.get("singleSignOnService") or {}).get("url") or "",
        "slo_url": (idp.get("singleLogoutService") or {}).get("url") or "",
        "certs": [cert_body(c) for c in certs],
        "name_id_format": (data.get("sp") or {}).get("NameIDFormat") or "",
    }
    missing = [k for k in ("entity_id", "sso_url", "certs") if not found[k]]
    if missing:
        raise _error("sso.saml_metadata_invalid",
                     f"the metadata has no {', '.join(missing)} (an HTTP-Redirect SSO "
                     "endpoint and a signing certificate are required)", missing=missing)
    return found


def fetch_idp_metadata(url: str) -> dict[str, Any]:
    return parse_idp_metadata(_http_text(url))


def apply_idp_metadata(row: ConsoleIdentityProvider, found: dict[str, Any]) -> list[str]:
    """Copy parsed metadata onto the provider; returns the changed field names."""
    changed: list[str] = []
    for field, column in (("entity_id", "saml_idp_entity_id"), ("sso_url", "saml_sso_url"),
                          ("slo_url", "saml_slo_url"), ("certs", "saml_idp_certs")):
        if getattr(row, column) != found[field]:
            setattr(row, column, found[field])
            changed.append(column)
    # the IdP's NameID format, unless the provider already asks for a specific one
    fmt = found.get("name_id_format") or ""
    if fmt in NAMEID_FORMATS and row.saml_name_id_format in ("", NAMEID_UNSPECIFIED) and (
            fmt != row.saml_name_id_format):
        row.saml_name_id_format = fmt
        changed.append("saml_name_id_format")
    return changed


# --- the library settings ------------------------------------------------------------------


def _settings(
    row: ConsoleIdentityProvider, settings: Settings | None = None, *, sp_only: bool = False,
) -> OneLogin_Saml2_Settings:
    current = settings or get_settings()
    sign = bool(row.saml_sign_requests)
    key = _sp_key(row) if (sign or row.saml_want_encrypted or not sp_only) else ""
    sp: dict[str, Any] = {
        "entityId": sp_entity_id(row.id, current),
        "assertionConsumerService": {"url": acs_url(row.id, current),
                                     "binding": OneLogin_Saml2_Constants.BINDING_HTTP_POST},
        "singleLogoutService": {"url": sls_url(row.id, current),
                                "binding": OneLogin_Saml2_Constants.BINDING_HTTP_REDIRECT},
        "NameIDFormat": row.saml_name_id_format or NAMEID_UNSPECIFIED,
        "x509cert": row.saml_sp_cert or "",
        "privateKey": key,
    }
    certs = [c for c in (row.saml_idp_certs or []) if c]
    idp: dict[str, Any] = {
        "entityId": row.saml_idp_entity_id or "",
        "singleSignOnService": {"url": row.saml_sso_url or "",
                                "binding": OneLogin_Saml2_Constants.BINDING_HTTP_REDIRECT},
    }
    if row.saml_slo_url:
        idp["singleLogoutService"] = {"url": row.saml_slo_url,
                                      "binding": OneLogin_Saml2_Constants.BINDING_HTTP_REDIRECT}
    if len(certs) == 1:
        idp["x509cert"] = certs[0]
    elif certs:
        idp["x509certMulti"] = {"signing": certs, "encryption": certs[:1]}
    security = {
        "authnRequestsSigned": sign,
        "logoutRequestSigned": sign,
        "logoutResponseSigned": sign,
        # either the response or the assertion must carry a valid signature (the
        # library refuses one with neither); requiring a specific one would refuse
        # IdPs that sign only the other
        "wantMessagesSigned": False,
        "wantAssertionsSigned": False,
        "wantAssertionsEncrypted": bool(row.saml_want_encrypted),
        "wantNameId": True,
        "wantAttributeStatement": False,
        "requestedAuthnContext": False,
        "rejectDeprecatedAlgorithm": True,
        "signatureAlgorithm": OneLogin_Saml2_Constants.RSA_SHA256,
        "digestAlgorithm": OneLogin_Saml2_Constants.SHA256,
        "metadataValidUntil": None,
        "metadataCacheDuration": None,
    }
    try:
        return OneLogin_Saml2_Settings(
            {"strict": True, "debug": False, "sp": sp, "idp": idp, "security": security},
            sp_validation_only=sp_only,
        )
    except Exception as exc:  # noqa: BLE001 — OneLogin_Saml2_Error lists what is missing
        raise _error("sso.incomplete", f"the SAML configuration is incomplete: {exc}",
                     409) from exc


def sp_metadata(row: ConsoleIdentityProvider, settings: Settings | None = None) -> str:
    """The SP metadata XML the IdP imports (§2.6 `GET …/metadata`)."""
    xml = _settings(row, settings, sp_only=True).get_sp_metadata()
    return xml.decode("utf-8") if isinstance(xml, bytes) else xml


def is_complete(row: ConsoleIdentityProvider) -> bool:
    return bool(row.saml_idp_entity_id and row.saml_sso_url and (row.saml_idp_certs or [])
                and row.saml_sp_key_enc)


# --- the flow ---------------------------------------------------------------------------


def _purge(db: Session) -> None:
    now = datetime.now(UTC)
    db.execute(delete(SamlRequest).where(
        SamlRequest.created_at < now - timedelta(seconds=REQUEST_TTL_SECONDS)))
    db.execute(delete(SamlAssertion).where(SamlAssertion.expires_at < now))


def begin_login(
    db: Session, row: ConsoleIdentityProvider, return_to: str | None,
    settings: Settings | None = None,
) -> str:
    """Record an AuthnRequest and return the IdP URL to redirect to."""
    current = settings or get_settings()
    target = sso_oidc.safe_return_to(return_to)
    auth = OneLogin_Saml2_Auth(_request_data(start_url(row.id, current)),
                               old_settings=_settings(row, current))
    url = auth.login(return_to=target)
    _purge(db)
    db.add(SamlRequest(request_id=auth.get_last_request_id(), idp_id=row.id,
                       purpose="login", return_to=target))
    db.commit()
    return url


# library validation codes → the login page's reason codes
_VALIDATION_CODES = {
    OneLogin_Saml2_ValidationError.NO_SIGNATURE_FOUND: "auth.saml.unsigned",
    OneLogin_Saml2_ValidationError.NO_SIGNED_ASSERTION: "auth.saml.unsigned",
    OneLogin_Saml2_ValidationError.NO_SIGNED_MESSAGE: "auth.saml.unsigned",
    OneLogin_Saml2_ValidationError.INVALID_SIGNATURE: "auth.saml.bad_signature",
    OneLogin_Saml2_ValidationError.WRONG_SIGNED_ELEMENT: "auth.saml.bad_signature",
    OneLogin_Saml2_ValidationError.UNEXPECTED_SIGNED_ELEMENTS: "auth.saml.bad_signature",
    OneLogin_Saml2_ValidationError.WRONG_NUMBER_OF_SIGNATURES: "auth.saml.bad_signature",
    OneLogin_Saml2_ValidationError.WRONG_NUMBER_OF_SIGNATURES_IN_RESPONSE:
        "auth.saml.bad_signature",
    OneLogin_Saml2_ValidationError.WRONG_NUMBER_OF_SIGNATURES_IN_ASSERTION:
        "auth.saml.bad_signature",
    OneLogin_Saml2_ValidationError.DEPRECATED_SIGNATURE_METHOD: "auth.saml.bad_signature",
    OneLogin_Saml2_ValidationError.DEPRECATED_DIGEST_METHOD: "auth.saml.bad_signature",
    OneLogin_Saml2_ValidationError.WRONG_AUDIENCE: "auth.saml.bad_audience",
    OneLogin_Saml2_ValidationError.WRONG_ISSUER: "auth.saml.bad_issuer",
    OneLogin_Saml2_ValidationError.ASSERTION_EXPIRED: "auth.saml.expired",
    OneLogin_Saml2_ValidationError.ASSERTION_TOO_EARLY: "auth.saml.expired",
    OneLogin_Saml2_ValidationError.SESSION_EXPIRED: "auth.saml.expired",
    OneLogin_Saml2_ValidationError.RESPONSE_EXPIRED: "auth.saml.expired",
    OneLogin_Saml2_ValidationError.WRONG_SUBJECTCONFIRMATION: "auth.saml.bad_subject",
    OneLogin_Saml2_ValidationError.WRONG_INRESPONSETO: "auth.saml.in_response_to",
    OneLogin_Saml2_ValidationError.WRONG_DESTINATION: "auth.saml.bad_destination",
    OneLogin_Saml2_ValidationError.EMPTY_DESTINATION: "auth.saml.bad_destination",
    OneLogin_Saml2_ValidationError.STATUS_CODE_IS_NOT_SUCCESS: "auth.saml.idp_error",
    OneLogin_Saml2_ValidationError.NO_ENCRYPTED_ASSERTION: "auth.saml.not_encrypted",
    OneLogin_Saml2_ValidationError.NO_NAMEID: "auth.saml.no_subject",
    OneLogin_Saml2_ValidationError.EMPTY_NAMEID: "auth.saml.no_subject",
}


def _consume_request(
    db: Session, row: ConsoleIdentityProvider, request_id: str, purpose: str,
) -> SamlRequest:
    pending = db.get(SamlRequest, request_id)
    if pending is None or pending.idp_id != row.id or pending.purpose != purpose:
        raise _error("auth.saml.in_response_to",
                     "This sign-in answers a request this console did not send — start again",
                     401)
    db.delete(pending)
    db.commit()
    created = pending.created_at if pending.created_at.tzinfo else (
        pending.created_at.replace(tzinfo=UTC))
    if (datetime.now(UTC) - created).total_seconds() > REQUEST_TTL_SECONDS:
        raise _error("auth.sso.state_expired", "This sign-in took too long — start again")
    return pending


def _flatten(attributes: dict[str, list[Any]]) -> dict[str, Any]:
    """SAML attributes are multi-valued: a single value reads as a string (the way an
    OIDC claim does), several as a list (groups)."""
    flat: dict[str, Any] = {}
    for name, values in (attributes or {}).items():
        items = [str(v) for v in (values or []) if v is not None]
        flat[name] = items[0] if len(items) == 1 else items
    return flat


def complete_login(
    db: Session, row: ConsoleIdentityProvider, *, saml_response: str | None,
    relay_state: str | None, settings: Settings | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """The ACS. Returns `(claims, return_to, session)`: the claims carry the NameID as
    `sub` and the attributes by name (`sso_mapping.profile_from_claims` reads them);
    the session is what an SP-initiated logout must name."""
    current = settings or get_settings()
    if not saml_response:
        raise _error("auth.saml.no_response", "the identity provider posted no SAMLResponse")
    library_settings = _settings(row, current)
    try:
        response = OneLogin_Saml2_Response(library_settings, saml_response)
    except Exception as exc:  # noqa: BLE001 — base64, XML or decryption errors
        raise _error("auth.saml.malformed", f"the SAMLResponse does not parse: {exc}",
                     401) from exc
    _purge(db)
    db.commit()
    in_response_to = response.get_in_response_to()
    if in_response_to:
        return_to = _consume_request(db, row, in_response_to, "login").return_to
    else:
        if not row.saml_allow_idp_initiated:
            raise _error("auth.saml.unsolicited",
                         "Sign-in from the identity provider's portal is turned off — "
                         "start from the console", 403)
        return_to = sso_oidc.safe_return_to(relay_state)
    data = _request_data(acs_url(row.id, current), post={"SAMLResponse": saml_response})
    try:
        response.is_valid(data, request_id=in_response_to, raise_exceptions=True)
    except OneLogin_Saml2_ValidationError as exc:
        code = _VALIDATION_CODES.get(exc.code, "auth.saml.invalid")
        raise _error(code, f"the SAML response was refused: {exc}", 401) from exc
    except Exception as exc:  # noqa: BLE001 — the library raises plain errors too
        raise _error("auth.saml.invalid", f"the SAML response was refused: {exc}",
                     401) from exc
    name_id = (response.get_nameid() or "").strip()
    if not name_id:
        raise _error("auth.saml.no_subject", "the assertion has no NameID", 401)
    _remember_assertion(db, row, response)
    claims = {**_flatten(response.get_attributes()), "sub": name_id}
    email_attr = (row.claims or {}).get("email") or "email"
    if not claims.get(email_attr) and "@" in name_id:
        claims[email_attr] = name_id  # an emailAddress NameID is the address
    session = {
        "name_id": name_id,
        "name_id_format": response.get_nameid_format(),
        "name_id_nq": response.get_nameid_nq(),
        "name_id_spnq": response.get_nameid_spnq(),
        "session_index": response.get_session_index(),
    }
    return claims, return_to, session


def _remember_assertion(
    db: Session, row: ConsoleIdentityProvider, response: OneLogin_Saml2_Response,
) -> None:
    """Record the assertion ID; a second sight of it is a replay (§2.2)."""
    assertion_id = response.get_assertion_id() or response.get_id()
    if not assertion_id:
        raise _error("auth.saml.invalid", "the assertion has no ID", 401)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=REPLAY_WINDOW_SECONDS)
    not_on_or_after = response.get_assertion_not_on_or_after()
    if isinstance(not_on_or_after, int | float):
        until = datetime.fromtimestamp(not_on_or_after, UTC) + timedelta(
            seconds=CLOCK_SKEW_SECONDS)
        # an assertion valid for longer stays remembered for as long (capped at a day)
        expires = min(max(expires, until), now + timedelta(days=1))
    db.add(SamlAssertion(idp_id=row.id, assertion_id=str(assertion_id)[:128],
                         expires_at=expires))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error("auth.saml.replayed", "This sign-in response was already used", 401) from exc


def remember_session(db: Session, user: User, row: ConsoleIdentityProvider,
                     session: dict[str, Any]) -> None:
    """The NameID / SessionIndex an SP-initiated LogoutRequest names (one per account:
    the last SAML sign-in)."""
    record = db.get(SamlSession, user.id)
    if record is None:
        record = SamlSession(user_id=user.id, idp_id=row.id, name_id="")
        db.add(record)
    record.idp_id = row.id
    record.name_id = str(session.get("name_id") or "")[:512]
    record.name_id_format = (session.get("name_id_format") or None)
    record.name_id_nq = (session.get("name_id_nq") or None)
    record.name_id_spnq = (session.get("name_id_spnq") or None)
    record.session_index = (session.get("session_index") or None)
    db.commit()


# --- logout -----------------------------------------------------------------------------


def end_session_url(db: Session, user: User, settings: Settings | None = None) -> str | None:
    """Where the browser goes after the console session ends (D-5): the IdP's SLO with
    a LogoutRequest for a SAML account, the OIDC `end_session_endpoint` for an OIDC
    one, None when there is no such page. Never raises: logging out must not fail."""
    row = db.get(ConsoleIdentityProvider, user.external_idp_id)
    if row is None or not row.enabled:
        return None
    if row.kind != KIND_SAML:
        return sso_oidc.end_session_url(db, user, settings)
    session = db.get(SamlSession, user.id)
    if not row.saml_slo_url or session is None or session.idp_id != row.id:
        return None
    current = settings or get_settings()
    try:
        auth = OneLogin_Saml2_Auth(_request_data(sls_url(row.id, current)),
                                   old_settings=_settings(row, current))
        url = auth.logout(
            return_to=f"{_base(current)}/", name_id=session.name_id,
            session_index=session.session_index, nq=session.name_id_nq,
            name_id_format=session.name_id_format, spnq=session.name_id_spnq,
        )
        db.add(SamlRequest(request_id=auth.get_last_request_id(), idp_id=row.id,
                           purpose="logout", return_to="/"))
        db.delete(session)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — a broken IdP config ends the local session only
        db.rollback()
        logger.warning("sso %s: SAML logout request not built: %s", row.id, exc)
        return None
    return url


def complete_logout(
    db: Session, row: ConsoleIdentityProvider, query: dict[str, str],
    settings: Settings | None = None,
) -> str:
    """`GET …/sls`: the IdP's LogoutResponse to our request (checked: signature when
    the IdP signs, InResponseTo, issuer, destination) — or an IdP-initiated
    LogoutRequest, which ends every session of the account it names (`session_epoch`)
    and is answered with a LogoutResponse. Returns where to redirect."""
    current = settings or get_settings()
    library_settings = _settings(row, current)
    data = _request_data(sls_url(row.id, current), get=query)
    auth = OneLogin_Saml2_Auth(data, old_settings=library_settings)
    if "SAMLResponse" in query:
        try:
            in_response_to = OneLogin_Saml2_Logout_Response(
                library_settings, query["SAMLResponse"]).get_in_response_to()
        except Exception as exc:  # noqa: BLE001
            raise _error("auth.saml.malformed", f"the LogoutResponse does not parse: {exc}",
                         401) from exc
        _consume_request(db, row, in_response_to or "", "logout")
        auth.process_slo(keep_local_session=True, request_id=in_response_to)
        if auth.get_errors():
            raise _error("auth.saml.logout_invalid",
                         f"the LogoutResponse was refused: {auth.get_last_error_reason()}", 401)
        return "/"
    if "SAMLRequest" in query:
        name_id = ""
        try:
            raw = OneLogin_Saml2_Logout_Request(library_settings, query["SAMLRequest"])
            name_id = OneLogin_Saml2_Logout_Request.get_nameid(
                raw.get_xml(), _sp_key(row) or None)
        except Exception as exc:  # noqa: BLE001
            raise _error("auth.saml.malformed", f"the LogoutRequest does not parse: {exc}",
                         401) from exc
        url = auth.process_slo(keep_local_session=True)
        if auth.get_errors():
            raise _error("auth.saml.logout_invalid",
                         f"the LogoutRequest was refused: {auth.get_last_error_reason()}", 401)
        _end_sessions(db, row, name_id)
        return url or "/"
    raise _error("auth.saml.no_response", "no SAML logout message")


def _end_sessions(db: Session, row: ConsoleIdentityProvider, name_id: str) -> None:
    from sqlalchemy import select

    for record in db.scalars(select(SamlSession).where(
            SamlSession.idp_id == row.id, SamlSession.name_id == name_id)):
        user = db.get(User, record.user_id)
        if user is not None:
            user.session_epoch = int(user.session_epoch or 0) + 1
        db.delete(record)
    db.commit()


# --- the connection test ---------------------------------------------------------------------


def test_provider(row: ConsoleIdentityProvider) -> dict[str, Any]:
    """§2.1 测试连接 for SAML: re-read the metadata URL (when there is one) and check
    the IdP certificates' validity and the SP key. Never raises for an IdP-side
    problem — that is the answer."""
    result: dict[str, Any] = {"ok": False, "checked_at": datetime.now(UTC).isoformat(),
                              "kind": KIND_SAML}
    try:
        if row.saml_metadata_url:
            found = fetch_idp_metadata(row.saml_metadata_url)
            result["metadata_fetched"] = True
            result["metadata_matches"] = (
                found["entity_id"] == row.saml_idp_entity_id
                and found["sso_url"] == row.saml_sso_url
                and set(found["certs"]) == set(row.saml_idp_certs or [])
            )
        result["entity_id"] = row.saml_idp_entity_id or None
        result["sso_url"] = row.saml_sso_url or None
        result["slo_url"] = row.saml_slo_url or None
        certs = [cert_facts(c) for c in (row.saml_idp_certs or [])]
        result["certs"] = certs
        valid = [c for c in certs if "error" not in c and not c["expired"]]
        result["cert_valid"] = bool(valid)
        sp = cert_facts(row.saml_sp_cert) if row.saml_sp_cert else {"error": "none"}
        result["sp_cert"] = sp
        result["sp_key"] = bool(row.saml_sp_key_enc) and "error" not in sp
        if row.saml_sp_key_enc:
            _sp_key(row)
        result["ok"] = bool(result["entity_id"] and result["sso_url"] and valid
                            and result["sp_key"]
                            and result.get("metadata_matches", True))
        if not valid:
            result["error"] = {"code": "sso.saml_cert_invalid",
                               "message": "no valid IdP signing certificate"}
        elif result.get("metadata_matches") is False:
            result["error"] = {"code": "sso.saml_metadata_changed",
                               "message": "the IdP metadata changed — load it again"}
    except AppError as err:
        result["error"] = {"code": err.code, "message": err.message}
    return result

