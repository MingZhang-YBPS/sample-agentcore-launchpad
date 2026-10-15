"""Console single sign-on over OpenID Connect (docs/design sso §2.1-§2.2, §2.5; SSO-1).

The authorization-code flow with PKCE, the relying party's half:

* **start** — a one-time `state`, `nonce` and PKCE `code_verifier` are written to
  `sso_login_states` (server side, never in the browser) and the browser is sent to
  the IdP's `authorization_endpoint` with the S256 challenge;
* **callback** — the state row is consumed (deleted before anything else is checked,
  so a replayed callback finds nothing), the code is exchanged at the
  `token_endpoint` with the verifier, and the `id_token` is verified: signature
  against the IdP's JWKS (asymmetric algorithms only), `iss`, `aud` / `azp`, `exp` /
  `iat` with 2 minutes of clock skew, and the stored `nonce`.

Tokens are verified in memory and never stored (§2.5). The client secret is a Fernet
ciphertext keyed outside the ledger (`LAUNCHPAD_SSO_SECRET_KEY`, else a 0600 key file
beside the SQLite ledger) or a Secrets Manager ARN read with the hub's credentials;
the API never returns it. PKCE / state helpers come from `authlib`, JOSE from
`joserfc` (authlib's JOSE implementation) — no hand-written signature checks.

HTTP goes through `_http_json`, the one seam the tests replace with a local fake IdP.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from cryptography.fernet import Fernet, InvalidToken
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import DATA_DIR, Settings, get_settings
from app.core.errors import AppError
from app.models.ledger import User
from app.models.sso import ConsoleIdentityProvider, SsoLoginState

STATE_TTL_SECONDS = 600
CLOCK_SKEW_SECONDS = 120
_CACHE_SECONDS = 600
_HTTP_TIMEOUT = 10
# asymmetric only: an HS* token would be "signed" with the client secret, and `none`
# is not a signature
ID_TOKEN_ALGORITHMS = (
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512",
    "EdDSA",
)

# Attribute templates (§2.1 预设模板). `issuer_hint` is a placeholder shown in the
# form; `claims` are the id_token claim names; `groups_scope` is the extra scope some
# IdPs need before they emit a groups claim.
PRESETS: dict[str, dict[str, Any]] = {
    "keycloak": {
        "issuer_hint": "https://keycloak.example.com/realms/<realm>",
        "scopes": "openid email profile",
        "claims": {"username": "preferred_username", "email": "email", "name": "name",
                   "groups": "groups"},
    },
    "authentik": {
        "issuer_hint": "https://authentik.example.com/application/o/<slug>/",
        "scopes": "openid email profile",
        "claims": {"username": "preferred_username", "email": "email", "name": "name",
                   "groups": "groups"},
    },
    "okta": {
        "issuer_hint": "https://<org>.okta.com/oauth2/default",
        "scopes": "openid email profile groups",
        "claims": {"username": "preferred_username", "email": "email", "name": "name",
                   "groups": "groups"},
    },
    "entra": {
        "issuer_hint": "https://login.microsoftonline.com/<tenant-id>/v2.0",
        "scopes": "openid email profile",
        "claims": {"username": "preferred_username", "email": "email", "name": "name",
                   "groups": "groups"},
    },
    "google": {
        "issuer_hint": "https://accounts.google.com",
        "scopes": "openid email profile",
        "claims": {"username": "email", "email": "email", "name": "name", "groups": ""},
    },
    "oidc": {
        "issuer_hint": "https://idp.example.com",
        "scopes": "openid email profile",
        "claims": {"username": "preferred_username", "email": "email", "name": "name",
                   "groups": "groups"},
    },
}
DEFAULT_PRESET = "oidc"

_lock = threading.Lock()
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_secret_cache: dict[str, tuple[float, str]] = {}


def reset_caches() -> None:
    with _lock:
        _discovery_cache.clear()
        _jwks_cache.clear()
        _secret_cache.clear()


def _error(code: str, message: str, status_code: int = 400, **detail: Any) -> AppError:
    return AppError(code, message, detail or None, status_code=status_code)


# --- HTTP --------------------------------------------------------------------


def check_url(url: str, field: str = "issuer") -> str:
    """An IdP URL: https, or http to this machine only (a local Keycloak in dev)."""
    value = (url or "").strip()
    parts = urllib.parse.urlsplit(value)
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parts.hostname or not (
        parts.scheme == "https" or (parts.scheme == "http" and local)
    ):
        raise _error(
            "sso.invalid_url", f"{field} must be an https URL (http only for localhost)",
            field=field,
        )
    if parts.username or parts.password or parts.fragment:
        raise _error("sso.invalid_url", f"{field} must not carry credentials or a fragment",
                     field=field)
    return value


def _http_json(
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET (or form POST with `data`) a JSON document from the IdP."""
    check_url(url, "url")
    body = urllib.parse.urlencode(data).encode("ascii") if data is not None else None
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url, data=body, method="POST" if body is not None else "GET",
        headers={"Accept": "application/json", **(headers or {})},
    )
    if body is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # noqa: S310
            raw = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace")
        raise _error("sso.idp_http_error", f"the identity provider answered HTTP {exc.code}",
                     502, url=url, status=exc.code, body=detail[:500]) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _error("sso.idp_unreachable", f"the identity provider is unreachable: {exc}",
                     502, url=url) from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise _error("sso.idp_bad_response", "the identity provider did not answer JSON",
                     502, url=url) from exc
    if not isinstance(doc, dict):
        raise _error("sso.idp_bad_response", "the identity provider did not answer an object",
                     502, url=url)
    return doc


def discover(issuer: str, *, fresh: bool = False) -> dict[str, Any]:
    """The issuer's `.well-known/openid-configuration`, cached for 10 minutes.

    The document must name the configured issuer exactly (OIDC Discovery §4.3), else
    an attacker-influenced document could redirect validation to another issuer."""
    key = issuer.strip()
    now = time.monotonic()
    with _lock:
        cached = _discovery_cache.get(key)
    if cached and not fresh and cached[0] > now:
        return cached[1]
    doc = _http_json(f"{key.rstrip('/')}/.well-known/openid-configuration")
    if doc.get("issuer") != key:
        raise _error("sso.issuer_mismatch",
                     f"discovery names issuer '{doc.get('issuer')}', configured '{key}'",
                     502, expected=key, actual=doc.get("issuer"))
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(doc.get(field), str):
            raise _error("sso.discovery_incomplete", f"discovery has no {field}", 502,
                         field=field)
    with _lock:
        _discovery_cache[key] = (now + _CACHE_SECONDS, doc)
    return doc


def _jwks(uri: str, *, fresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        cached = _jwks_cache.get(uri)
    if cached and not fresh and cached[0] > now:
        return cached[1]
    doc = _http_json(uri)
    if not isinstance(doc.get("keys"), list):
        raise _error("sso.jwks_invalid", "the JWKS document has no keys", 502)
    with _lock:
        _jwks_cache[uri] = (now + _CACHE_SECONDS, doc)
    return doc


# --- the client secret ---------------------------------------------------------


def _key_path(settings: Settings) -> Path:
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        return Path(url.database).resolve().with_name("sso-secret.key")
    return DATA_DIR / "sso-secret.key"


def _fernet(settings: Settings | None = None) -> Fernet:
    """`LAUNCHPAD_SSO_SECRET_KEY` (a Fernet key), else a key file created once with
    mode 0600 beside the ledger: the ciphertext and its key never share a store."""
    configured = os.environ.get("LAUNCHPAD_SSO_SECRET_KEY", "").strip()
    if configured:
        return Fernet(configured.encode("ascii"))
    path = _key_path(settings or get_settings())
    with _lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
        key = path.read_bytes().strip()
    return Fernet(key)


def seal_secret(secret: str, settings: Settings | None = None) -> str:
    return _fernet(settings).encrypt(secret.encode("utf-8")).decode("ascii")


def _unseal(token: str, settings: Settings | None = None) -> str:
    try:
        return _fernet(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise _error("sso.secret_unreadable",
                     "the stored client secret cannot be decrypted (the SSO secret key "
                     "changed) — enter the client secret again", 409) from exc


def is_secret_ref(value: str) -> bool:
    return value.startswith("arn:aws:secretsmanager:")


def _read_secret_ref(arn: str) -> str:
    now = time.monotonic()
    with _lock:
        cached = _secret_cache.get(arn)
    if cached and cached[0] > now:
        return cached[1]
    from botocore.exceptions import BotoCoreError, ClientError

    from app.services.aws_clients import get_session

    parts = arn.split(":")
    try:
        client = get_session(parts[4], parts[3]).client("secretsmanager")
        value = client.get_secret_value(SecretId=arn).get("SecretString") or ""
    except (ClientError, BotoCoreError, IndexError) as exc:
        raise _error("sso.secret_unreadable",
                     f"the client secret could not be read from Secrets Manager: {exc}",
                     502) from exc
    with _lock:
        _secret_cache[arn] = (now + 300, value)
    return value


def client_secret(row: ConsoleIdentityProvider) -> str | None:
    """The plain client secret, or None for a public (PKCE-only) client."""
    if row.client_secret_ref:
        return _read_secret_ref(row.client_secret_ref)
    if row.client_secret_enc:
        return _unseal(row.client_secret_enc)
    return None


# --- providers ----------------------------------------------------------------


def callback_url(idp_id: str, settings: Settings | None = None) -> str:
    base = (settings or get_settings()).public_base_url.rstrip("/")
    return f"{base}/api/auth/oidc/{idp_id}/callback"


def public_providers(db: Session) -> list[dict[str, Any]]:
    """The login page's buttons: only id, name, kind and preset (§2.6)."""
    rows = db.scalars(
        select(ConsoleIdentityProvider)
        .where(ConsoleIdentityProvider.enabled.is_(True))
        .order_by(ConsoleIdentityProvider.sort_order, ConsoleIdentityProvider.name)
    )
    return [{"id": r.id, "name": r.name, "kind": r.kind, "preset": r.preset} for r in rows]


def enabled_provider(db: Session, idp_id: str) -> ConsoleIdentityProvider:
    """The provider a start / callback names; 404 unless it exists and is enabled —
    the callback only accepts configured IdPs (§2.5)."""
    row = db.get(ConsoleIdentityProvider, idp_id)
    if row is None or not row.enabled:
        raise AppError("sso.provider_not_found", "No such sign-in provider",
                       {"provider_id": idp_id}, status_code=404)
    return row


def test_provider(row: ConsoleIdentityProvider) -> dict[str, Any]:
    """Read discovery and the JWKS afresh (§2.1 测试连接); the result is the row's
    `last_test`. Never raises for an IdP-side problem — that is the answer."""
    result: dict[str, Any] = {"ok": False, "checked_at": datetime.now(UTC).isoformat()}
    try:
        doc = discover(row.issuer, fresh=True)
        result.update({
            "issuer": doc["issuer"],
            "authorization_endpoint": doc["authorization_endpoint"],
            "token_endpoint": doc["token_endpoint"],
            "userinfo_endpoint": doc.get("userinfo_endpoint"),
            "end_session_endpoint": doc.get("end_session_endpoint"),
            "scopes_supported": doc.get("scopes_supported"),
            # absent from discovery means "unknown", not "unsupported"
            "pkce_s256": (
                None if "code_challenge_methods_supported" not in doc
                else "S256" in (doc.get("code_challenge_methods_supported") or [])
            ),
        })
        keys = _jwks(doc["jwks_uri"], fresh=True)["keys"]
        KeySet.import_key_set({"keys": keys})
        result["jwks_keys"] = len(keys)
        result["secret_configured"] = bool(row.client_secret_enc or row.client_secret_ref)
        result["ok"] = result["pkce_s256"] is not False and len(keys) > 0
        if result["pkce_s256"] is False:
            result["error"] = {"code": "sso.pkce_unsupported",
                               "message": "the IdP does not advertise PKCE S256"}
    except AppError as err:
        result["error"] = {"code": err.code, "message": err.message}
    except (JoseError, ValueError, TypeError) as err:
        result["error"] = {"code": "sso.jwks_invalid", "message": str(err)}
    return result


# --- the flow -----------------------------------------------------------------


def safe_return_to(value: str | None) -> str:
    """A same-origin path to land on after sign-in; anything else is `/` (no open
    redirect: `//host`, `/\\host` and absolute URLs are refused)."""
    path = (value or "").strip()
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(ch in path for ch in "\r\n\t")
        or path.startswith("/api/")
        or len(path) > 500
    ):
        return "/"
    return path


def begin_login(
    db: Session, row: ConsoleIdentityProvider, return_to: str | None,
    settings: Settings | None = None,
) -> str:
    """Store a one-time state and return the IdP authorization URL to redirect to."""
    current = settings or get_settings()
    doc = discover(row.issuer)
    cutoff = datetime.now(UTC) - timedelta(seconds=STATE_TTL_SECONDS)
    db.execute(delete(SsoLoginState).where(SsoLoginState.created_at < cutoff))
    state = generate_token(43)
    nonce = generate_token(43)
    verifier = generate_token(64)  # RFC 7636: 43-128 unreserved characters
    db.add(SsoLoginState(state=state, idp_id=row.id, nonce=nonce, code_verifier=verifier,
                         return_to=safe_return_to(return_to)))
    db.commit()
    params = {
        "response_type": "code",
        "client_id": row.client_id,
        "redirect_uri": callback_url(row.id, current),
        "scope": row.scopes or "openid",
        "state": state,
        "nonce": nonce,
        "code_challenge": create_s256_code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    endpoint = doc["authorization_endpoint"]
    joiner = "&" if "?" in endpoint else "?"
    return f"{endpoint}{joiner}{urllib.parse.urlencode(params)}"


def consume_state(db: Session, idp_id: str, state: str | None) -> SsoLoginState:
    """Take the state row out of the store — deleted first, so it is one-time even
    when the rest of the callback fails."""
    row = db.get(SsoLoginState, state) if state else None
    if row is None or row.idp_id != idp_id:
        raise _error("auth.sso.state_invalid", "This sign-in link is not valid — start again")
    db.delete(row)
    db.commit()
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    if (datetime.now(UTC) - created).total_seconds() > STATE_TTL_SECONDS:
        raise _error("auth.sso.state_expired", "This sign-in took too long — start again")
    return row


def _exchange_code(
    row: ConsoleIdentityProvider, doc: dict[str, Any], code: str, verifier: str,
    settings: Settings,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url(row.id, settings),
        "code_verifier": verifier,
    }
    headers: dict[str, str] = {}
    secret = client_secret(row)
    methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if secret and "client_secret_basic" in methods:
        quote = urllib.parse.quote
        pair = f"{quote(row.client_id, safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    else:
        data["client_id"] = row.client_id
        if secret:
            data["client_secret"] = secret
    tokens = _http_json(doc["token_endpoint"], data=data, headers=headers)
    if not isinstance(tokens.get("id_token"), str):
        raise _error("auth.sso.no_id_token", "the identity provider returned no id_token", 502)
    return tokens


def validate_id_token(
    row: ConsoleIdentityProvider, doc: dict[str, Any], id_token: str, nonce: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify signature, `iss`, `aud` / `azp`, `exp` / `iat` and `nonce`."""
    def decode(fresh: bool) -> Any:
        keys = KeySet.import_key_set(_jwks(doc["jwks_uri"], fresh=fresh))
        return jwt.decode(id_token, keys, algorithms=list(ID_TOKEN_ALGORITHMS))

    try:
        try:
            token = decode(False)
        except (JoseError, ValueError):
            # the IdP may have rotated its signing key: one fresh JWKS read, then the
            # answer stands
            token = decode(True)
    except (JoseError, ValueError, TypeError) as exc:
        raise _error("auth.sso.bad_signature",
                     f"the id_token signature does not verify: {exc}", 401) from exc
    claims = dict(token.claims)
    moment = time.time() if now is None else now
    if claims.get("iss") != row.issuer:
        raise _error("auth.sso.bad_issuer", "the id_token was issued by another issuer", 401)
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if row.client_id not in audiences:
        raise _error("auth.sso.bad_audience", "the id_token is for another client", 401)
    if len(audiences) > 1 and claims.get("azp") != row.client_id:
        raise _error("auth.sso.bad_audience", "the id_token's azp is another client", 401)
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or exp + CLOCK_SKEW_SECONDS < moment:
        raise _error("auth.sso.expired", "the id_token has expired", 401)
    iat = claims.get("iat")
    if isinstance(iat, int | float) and iat - CLOCK_SKEW_SECONDS > moment:
        raise _error("auth.sso.expired", "the id_token was issued in the future", 401)
    if not isinstance(claims.get("nonce"), str) or claims["nonce"] != nonce:
        raise _error("auth.sso.bad_nonce", "the id_token nonce does not match", 401)
    if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
        raise _error("auth.sso.no_subject", "the id_token has no subject", 401)
    return claims


def complete_login(
    db: Session, row: ConsoleIdentityProvider, *, code: str | None, state: str | None,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], str]:
    """The callback: consume the state, exchange the code, verify the id_token.
    Returns `(claims, return_to)`."""
    current = settings or get_settings()
    pending = consume_state(db, row.id, state)
    if not code:
        raise _error("auth.sso.no_code", "the identity provider returned no code")
    doc = discover(row.issuer)
    tokens = _exchange_code(row, doc, code, pending.code_verifier, current)
    claims = validate_id_token(row, doc, tokens["id_token"], pending.nonce)
    groups_claim = (row.claims or {}).get("groups") or ""
    access_token = tokens.get("access_token")
    if (groups_claim and groups_claim not in claims and doc.get("userinfo_endpoint")
            and isinstance(access_token, str)):
        # some IdPs put groups in userinfo only; it must describe the same subject
        info = _http_json(doc["userinfo_endpoint"],
                          headers={"Authorization": f"Bearer {access_token}"})
        if info.get("sub") == claims["sub"]:
            claims = {**info, **claims}
    return claims, pending.return_to


def end_session_url(db: Session, user: User, settings: Settings | None = None) -> str | None:
    """OIDC RP-initiated logout (D-5): the IdP's `end_session_endpoint` with this
    client and the console as the post-logout landing, or None when it has none."""
    row = db.get(ConsoleIdentityProvider, user.external_idp_id)
    if row is None or not row.enabled:
        return None
    try:
        endpoint = discover(row.issuer).get("end_session_endpoint")
    except AppError:
        return None
    if not isinstance(endpoint, str) or not endpoint:
        return None
    base = (settings or get_settings()).public_base_url.rstrip("/")
    params = {"client_id": row.client_id, "post_logout_redirect_uri": f"{base}/"}
    joiner = "&" if "?" in endpoint else "?"
    return f"{endpoint}{joiner}{urllib.parse.urlencode(params)}"
