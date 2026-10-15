"""Console single sign-on (docs/design sso §2.6; SSO-1: OIDC, SSO-2: SAML, SSO-3).

Two surfaces, both hub-global:

* **public** — `GET /api/auth/providers` (the login page's buttons, and whether the
  local form is hidden), the OIDC redirect leg `GET /api/auth/oidc/{id}/start` → IdP
  → `/callback`, and the SAML one: `GET /api/auth/saml/{id}/start` (SP-initiated,
  HTTP-Redirect) → IdP → `POST …/acs` (also the landing of an IdP-initiated sign-in),
  `GET …/metadata` (the SP metadata the IdP imports) and `GET …/sls` (single logout).
  The callback / ACS answer with a redirect either way: into the console with the
  session cookie set, or back to the login page with `?sso_error=<code>` — never a
  JSON error page in the middle of a browser redirect.
* **administrators** — `/api/admin/sso/providers*`: configure an IdP (OIDC or SAML),
  test it, load SAML metadata, rotate the SP key or the SCIM token, edit its group
  mappings, read its recent sign-ins; `/api/admin/sso/settings`: hide local login.
  Secrets are write-only: answers carry `client_secret_set` and the Secrets Manager
  ARN when that is the source, never a value; the SCIM token is shown once.

Journal: `auth.sso.login` (outcome success / refused / failed, the reason code in
the target), `auth.sso.config.create|update|delete|test|mappings|metadata|sp_key|
scim_token|settings`.
"""

import logging
import re
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import AuditEvent, User
from app.models.sso import (
    ACCOUNT_POLICIES,
    KIND_OIDC,
    KIND_SAML,
    ConsoleIdentityProvider,
)
from app.routers.auth import Identity, issue_user_session, require_identity
from app.services import sso_mapping, sso_oidc, sso_saml, sso_scim
from app.services import users as users_service
from app.services.audit import OUTCOME_FAILED, OUTCOME_REFUSED, record_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sso"])

LOGIN_ACTION = "auth.sso.login"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
# where the login page is: the console root renders it for a signed-out browser
LOGIN_PATH = "/"
# every preset: OIDC attribute templates (SSO-1) and SAML ones (SSO-2); a preset
# names its channel
PRESETS: dict[str, dict[str, Any]] = {
    **{key: {**value, "kind": KIND_OIDC} for key, value in sso_oidc.PRESETS.items()},
    **sso_saml.PRESETS,
}


def _caller(request: Request) -> Identity:
    return require_identity(request)


def _audit(action: str, target: str, *, actor: str = "anonymous",
           actor_user_id: str | None = None, outcome: str = "success") -> None:
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor=actor[:64], action=action,
                       target=target[:256], actor_user_id=actor_user_id, project_id=None,
                       outcome=outcome)


# --- public ---------------------------------------------------------------------


@router.get("/api/auth/providers")
def providers(db: Session = Depends(get_db)) -> dict[str, Any]:
    # `hide_local_login`: the page shows the IdP buttons only (D-2); `?local=1` still
    # opens the form for the built-in administrator
    return {"providers": sso_oidc.public_providers(db),
            "hide_local_login": sso_mapping.local_login_hidden(db)}


def _login_error(code: str, provider_id: str) -> RedirectResponse:
    query = urlencode({"sso_error": code, "sso_provider": provider_id})
    return RedirectResponse(f"{LOGIN_PATH}?{query}", status_code=302)


@router.get("/api/auth/oidc/{provider_id}/start")
def oidc_start(
    provider_id: str, return_to: str | None = None, db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        row = _enabled(db, provider_id, KIND_OIDC)
        url = sso_oidc.begin_login(db, row, return_to)
    except AppError as err:
        _audit(LOGIN_ACTION, f"idp:{provider_id} reason:{err.code} stage:start",
               outcome=OUTCOME_FAILED)
        return _login_error(err.code, provider_id)
    return RedirectResponse(url, status_code=302)


@router.get("/api/auth/oidc/{provider_id}/callback")
def oidc_callback(
    provider_id: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    settings = get_settings()
    subject = "-"
    try:
        row = _enabled(db, provider_id, KIND_OIDC)
        if error:
            # the IdP refused (access_denied, a consent the user declined…); the state
            # is spent all the same
            sso_oidc.consume_state(db, row.id, state)
            raise AppError("auth.sso.idp_error", f"the identity provider answered {error}",
                           {"error": error[:64]}, status_code=401)
        claims, return_to = sso_oidc.complete_login(db, row, code=code, state=state,
                                                    settings=settings)
        user, created, changes, subject = _sign_in(db, row, claims, settings)
    except AppError as err:
        return _refused(db, err, provider_id, subject)
    return _signed_in(row, user, created, changes, subject, return_to, settings)


def _enabled(db: Session, provider_id: str, kind: str) -> ConsoleIdentityProvider:
    """An enabled provider of this channel; 404 otherwise (an OIDC leg never serves a
    SAML provider, nor the reverse)."""
    row = sso_oidc.enabled_provider(db, provider_id)
    if row.kind != kind:
        raise AppError("sso.provider_not_found", "No such sign-in provider",
                       {"provider_id": provider_id}, status_code=404)
    return row


def _sign_in(
    db: Session, row: ConsoleIdentityProvider, claims: dict[str, Any], settings: Any,
) -> tuple[User, bool, dict[str, Any], str]:
    """Verified claims → the account and its access: the one path every channel
    shares (JIT / existing_only, then the group mappings with the SCIM groups added)."""
    profile = sso_mapping.profile_from_claims(row, claims)
    user, created = sso_mapping.provision(db, row, profile, settings)
    groups = sso_mapping.login_groups(db, row, user, profile.groups)
    changes = sso_mapping.apply(db, row, user, groups, created=created)
    users_service.record_login(db, user)
    return user, created, changes, profile.subject


def _refused(db: Session, err: AppError, provider_id: str, subject: str) -> RedirectResponse:
    db.rollback()  # nothing half-done stays locked while the refusal is journaled
    outcome = OUTCOME_FAILED if err.status_code >= 500 else OUTCOME_REFUSED
    _audit(LOGIN_ACTION, f"idp:{provider_id} reason:{err.code} subject:{subject}",
           outcome=outcome)
    return _login_error(err.code, provider_id)


def _signed_in(
    row: ConsoleIdentityProvider, user: User, created: bool, changes: dict[str, Any],
    subject: str, return_to: str, settings: Any, *, status_code: int = 302,
) -> RedirectResponse:
    summary = " ".join(
        part for part in (
            "jit" if created else "",
            f"role:{changes['role']}" if changes["role"] else "",
            f"conflict:{changes['conflict']}" if changes["conflict"] else "",
            *changes["projects"], *(f"group{g}" for g in changes["groups"]),
        ) if part
    )
    _audit(LOGIN_ACTION,
           f"idp:{row.id} user:{user.username} subject:{subject} {summary}".strip(),
           actor=user.username, actor_user_id=user.id)
    response = RedirectResponse(return_to, status_code=status_code)
    issue_user_session(response, user, settings)
    return response


# --- SAML (SSO-2) -------------------------------------------------------------------


@router.get("/api/auth/saml/{provider_id}/start")
def saml_start(
    provider_id: str, return_to: str | None = None, db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        row = _enabled(db, provider_id, KIND_SAML)
        url = sso_saml.begin_login(db, row, return_to)
    except AppError as err:
        _audit(LOGIN_ACTION, f"idp:{provider_id} reason:{err.code} stage:start",
               outcome=OUTCOME_FAILED)
        return _login_error(err.code, provider_id)
    return RedirectResponse(url, status_code=302)


@router.post("/api/auth/saml/{provider_id}/acs")
async def saml_acs(
    provider_id: str, request: Request, db: Session = Depends(get_db),
) -> RedirectResponse:
    """The Assertion Consumer Service (HTTP-POST binding): SP- and IdP-initiated."""
    settings = get_settings()
    subject = "-"
    try:
        form = await request.form()
        saml_response = form.get("SAMLResponse")
        relay_state = form.get("RelayState")
        row = _enabled(db, provider_id, KIND_SAML)
        claims, return_to, session = sso_saml.complete_login(
            db, row,
            saml_response=saml_response if isinstance(saml_response, str) else None,
            relay_state=relay_state if isinstance(relay_state, str) else None,
            settings=settings,
        )
        subject = str(claims.get("sub") or "-")
        user, created, changes, subject = _sign_in(db, row, claims, settings)
        sso_saml.remember_session(db, user, row, session)
    except AppError as err:
        return _refused(db, err, provider_id, subject)
    # 303: the browser follows a POST's answer with a GET
    return _signed_in(row, user, created, changes, subject, return_to, settings,
                      status_code=303)


@router.get("/api/auth/saml/{provider_id}/metadata")
def saml_metadata(provider_id: str, db: Session = Depends(get_db)) -> Response:
    """The SP metadata for the IdP to import — served while the provider is still
    disabled (it is configured at the IdP before it is turned on here)."""
    row = db.get(ConsoleIdentityProvider, provider_id)
    if row is None or row.kind != KIND_SAML:
        raise NotFoundError("sso.provider_not_found", "No such identity provider",
                            {"provider_id": provider_id})
    return Response(
        sso_saml.sp_metadata(row), media_type="application/samlmetadata+xml",
        headers={"Content-Disposition":
                 f'inline; filename="launchpad-sp-{row.id}.xml"'},
    )


@router.get("/api/auth/saml/{provider_id}/sls")
def saml_sls(
    provider_id: str, request: Request, db: Session = Depends(get_db),
) -> RedirectResponse:
    """Single logout: the IdP's LogoutResponse to our LogoutRequest, or its own
    LogoutRequest (the account's sessions end). Either way back to the console."""
    try:
        row = _enabled(db, provider_id, KIND_SAML)
        target = sso_saml.complete_logout(db, row, dict(request.query_params))
    except AppError as err:
        db.rollback()
        _audit("auth.sso.logout", f"idp:{provider_id} reason:{err.code}",
               outcome=OUTCOME_REFUSED)
        return _login_error(err.code, provider_id)
    return RedirectResponse(target, status_code=302)


# --- administrators ---------------------------------------------------------------


class ClaimNames(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(default="preferred_username", max_length=128)
    email: str = Field(default="email", max_length=128)
    name: str = Field(default="name", max_length=128)
    groups: str = Field(default="groups", max_length=128)


class ProviderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    preset: str | None = None
    enabled: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    issuer: str | None = Field(default=None, max_length=512)
    client_id: str | None = Field(default=None, max_length=256)
    # write-only: a value replaces the secret, "" clears it (a public PKCE client), an
    # `arn:aws:secretsmanager:` value is stored as a reference instead
    client_secret: str | None = Field(default=None, max_length=4096)
    scopes: str | None = Field(default=None, max_length=256)
    claims: ClaimNames | None = None
    account_policy: str | None = None
    allowed_email_domains: list[str] | None = Field(default=None, max_length=50)
    default_role: str | None = None
    valid_days: int | None = Field(default=None, ge=1, le=3650)
    # SAML (SSO-2): typed by hand, or filled by loading the IdP metadata
    # (`saml_metadata_xml` is parsed, not stored; `saml_metadata_url` is fetched)
    saml_metadata_url: str | None = Field(default=None, max_length=1024)
    saml_metadata_xml: str | None = Field(default=None, max_length=1_000_000)
    saml_idp_entity_id: str | None = Field(default=None, max_length=512)
    saml_sso_url: str | None = Field(default=None, max_length=1024)
    saml_slo_url: str | None = Field(default=None, max_length=1024)
    saml_idp_certs: list[str] | None = Field(default=None, max_length=5)
    saml_name_id_format: str | None = Field(default=None, max_length=128)
    saml_sign_requests: bool | None = None
    saml_want_encrypted: bool | None = None
    saml_allow_idp_initiated: bool | None = None
    # SCIM (SSO-3): which SCIM attribute is the account's subject
    scim_subject: str | None = None


class MetadataBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, max_length=1024)
    xml: str | None = Field(default=None, max_length=1_000_000)


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hide_local_login: bool


class MappingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mappings: list[dict[str, Any]] = Field(max_length=sso_mapping.MAX_MAPPINGS)


def _saml_block(row: ConsoleIdentityProvider) -> dict[str, Any] | None:
    if row.kind != KIND_SAML:
        return None
    certs = [{"body": c, **sso_saml.cert_facts(c)} for c in (row.saml_idp_certs or [])]
    return {
        "metadata_url": row.saml_metadata_url,
        "idp_entity_id": row.saml_idp_entity_id,
        "sso_url": row.saml_sso_url,
        "slo_url": row.saml_slo_url,
        "idp_certs": certs,
        "name_id_format": row.saml_name_id_format,
        "sign_requests": bool(row.saml_sign_requests),
        "want_encrypted": bool(row.saml_want_encrypted),
        "allow_idp_initiated": bool(row.saml_allow_idp_initiated),
        # the values the IdP side needs (§2.7 一键复制)
        "sp_entity_id": sso_saml.sp_entity_id(row.id),
        "acs_url": sso_saml.acs_url(row.id),
        "sls_url": sso_saml.sls_url(row.id),
        "metadata_endpoint": sso_saml.sp_entity_id(row.id),
        "sp_cert": row.saml_sp_cert,
        "sp_cert_facts": sso_saml.cert_facts(row.saml_sp_cert) if row.saml_sp_cert else None,
        "sp_key_set": bool(row.saml_sp_key_enc),
    }


def _scim_block(db: Session | None, row: ConsoleIdentityProvider) -> dict[str, Any]:
    created = users_service.as_utc(row.scim_token_created_at)
    return {
        "enabled": bool(row.scim_token_hash),
        "base_url": sso_scim.base_url(),
        "token_hint": row.scim_token_hint,
        "token_created_at": created.isoformat() if created else None,
        "subject": row.scim_subject or "userName",
        **({"counts": sso_scim.stats(db, row)} if db is not None else {}),
    }


def _serialize(row: ConsoleIdentityProvider, linked: int = 0,
               db: Session | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "preset": row.preset,
        "enabled": bool(row.enabled),
        "sort_order": row.sort_order,
        "issuer": row.issuer,
        "client_id": row.client_id,
        "client_secret_set": bool(row.client_secret_enc or row.client_secret_ref),
        "client_secret_source": (
            "secrets_manager" if row.client_secret_ref
            else "encrypted" if row.client_secret_enc else None
        ),
        "client_secret_ref": row.client_secret_ref,
        "scopes": row.scopes,
        "claims": {**PRESETS[_default_preset(row.kind)]["claims"], **(row.claims or {})},
        "account_policy": row.account_policy,
        "allowed_email_domains": list(row.allowed_email_domains or []),
        "default_role": row.default_role,
        "valid_days": row.valid_days,
        "mappings": list(row.mappings or []),
        "last_test": row.last_test,
        # where the IdP sends the browser back: the OIDC callback, or the SAML ACS
        "callback_url": (sso_saml.acs_url(row.id) if row.kind == KIND_SAML
                         else sso_oidc.callback_url(row.id)),
        "saml": _saml_block(row),
        "scim": _scim_block(db, row),
        "linked_accounts": linked,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _default_preset(kind: str) -> str:
    return sso_saml.DEFAULT_PRESET if kind == KIND_SAML else sso_oidc.DEFAULT_PRESET


def _linked_counts(db: Session) -> dict[str, int]:
    return dict(db.execute(
        select(User.external_idp_id, func.count())
        .where(User.external_idp_id.is_not(None))
        .group_by(User.external_idp_id)
    ).all())


def _provider(db: Session, provider_id: str) -> ConsoleIdentityProvider:
    row = db.get(ConsoleIdentityProvider, provider_id)
    if row is None:
        raise NotFoundError("sso.provider_not_found", "No such identity provider",
                            {"provider_id": provider_id})
    return row


def _apply_body(row: ConsoleIdentityProvider, body: ProviderBody, sent: set[str]) -> list[str]:
    """Copy the sent fields onto the row (validated); returns the changed field names
    (the secret as `client_secret`, never its value)."""
    changed: list[str] = []

    def put(name: str, value: Any) -> None:
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed.append(name)

    if "name" in sent and body.name is not None:
        put("name", body.name.strip())
    if "preset" in sent and body.preset is not None:
        if body.preset not in PRESETS or PRESETS[body.preset]["kind"] != row.kind:
            raise AppError("sso.invalid_preset",
                           f"preset must be one of {', '.join(_presets_of(row.kind))}")
        put("preset", body.preset)
    if "enabled" in sent and body.enabled is not None:
        put("enabled", body.enabled)
    if "sort_order" in sent and body.sort_order is not None:
        put("sort_order", body.sort_order)
    if "issuer" in sent and body.issuer is not None:
        put("issuer", sso_oidc.check_url(body.issuer))
    if "client_id" in sent and body.client_id is not None:
        put("client_id", body.client_id.strip())
    if "client_secret" in sent and body.client_secret is not None:
        secret = body.client_secret.strip()
        if sso_oidc.is_secret_ref(secret):
            row.client_secret_ref, row.client_secret_enc = secret, None
        elif secret:
            row.client_secret_ref, row.client_secret_enc = None, sso_oidc.seal_secret(secret)
        else:
            row.client_secret_ref = row.client_secret_enc = None
        changed.append("client_secret")
    if "scopes" in sent and body.scopes is not None:
        scopes = " ".join(dict.fromkeys(body.scopes.split()))
        if "openid" not in scopes.split():
            raise AppError("sso.invalid_scopes", "scopes must include openid")
        put("scopes", scopes)
    if "claims" in sent and body.claims is not None:
        put("claims", {k: v.strip() for k, v in body.claims.model_dump().items()})
    if "account_policy" in sent and body.account_policy is not None:
        if body.account_policy not in ACCOUNT_POLICIES:
            raise AppError("sso.invalid_policy",
                           f"account_policy must be one of {', '.join(ACCOUNT_POLICIES)}")
        put("account_policy", body.account_policy)
    if "allowed_email_domains" in sent and body.allowed_email_domains is not None:
        domains = [d.strip().lower().lstrip("@") for d in body.allowed_email_domains
                   if d.strip()]
        put("allowed_email_domains", list(dict.fromkeys(domains)))
    if "default_role" in sent and body.default_role is not None:
        if body.default_role not in users_service.ROLES:
            raise AppError("sso.invalid_role",
                           f"default_role must be one of {', '.join(users_service.ROLES)}")
        put("default_role", body.default_role)
    if "valid_days" in sent and body.valid_days is not None:
        put("valid_days", body.valid_days)
    if "scim_subject" in sent and body.scim_subject is not None:
        if body.scim_subject not in sso_scim.SUBJECT_ATTRIBUTES:
            raise AppError("sso.invalid_scim_subject", "scim_subject must be one of "
                           f"{', '.join(sso_scim.SUBJECT_ATTRIBUTES)}")
        put("scim_subject", body.scim_subject)
    saml_fields = {f for f in sent if f.startswith("saml_")}
    if saml_fields and row.kind != KIND_SAML:
        raise AppError("sso.invalid_field", "SAML settings apply to a SAML provider only",
                       {"fields": sorted(saml_fields)})
    if saml_fields:
        changed += _apply_saml(row, body, sent, put)
    return changed


def _apply_saml(row: ConsoleIdentityProvider, body: ProviderBody, sent: set[str],
                put: Any) -> list[str]:
    changed: list[str] = []
    if "saml_metadata_xml" in sent and body.saml_metadata_xml:
        changed += sso_saml.apply_idp_metadata(row, sso_saml.parse_idp_metadata(
            body.saml_metadata_xml))
        put("saml_metadata_url", "")
    if "saml_metadata_url" in sent and body.saml_metadata_url is not None:
        url = body.saml_metadata_url.strip()
        if url:
            url = sso_oidc.check_url(url, "saml_metadata_url")
            changed += sso_saml.apply_idp_metadata(row, sso_saml.fetch_idp_metadata(url))
        put("saml_metadata_url", url)
    if "saml_idp_entity_id" in sent and body.saml_idp_entity_id is not None:
        put("saml_idp_entity_id", body.saml_idp_entity_id.strip())
    for field in ("saml_sso_url", "saml_slo_url"):
        value = getattr(body, field)
        if field in sent and value is not None:
            put(field, sso_oidc.check_url(value, field) if value.strip() else "")
    if "saml_idp_certs" in sent and body.saml_idp_certs is not None:
        put("saml_idp_certs", sso_saml.check_certs(body.saml_idp_certs))
    if "saml_name_id_format" in sent and body.saml_name_id_format is not None:
        value = body.saml_name_id_format.strip()
        if value and value not in sso_saml.NAMEID_FORMATS:
            raise AppError("sso.invalid_name_id_format",
                           f"name_id_format must be one of {', '.join(sso_saml.NAMEID_FORMATS)}")
        put("saml_name_id_format", value)
    for field in ("saml_sign_requests", "saml_want_encrypted", "saml_allow_idp_initiated"):
        value = getattr(body, field)
        if field in sent and value is not None:
            put(field, value)
    return changed


def _presets_of(kind: str) -> list[str]:
    return [key for key, value in PRESETS.items() if value["kind"] == kind]


def _check_enable(row: ConsoleIdentityProvider) -> None:
    if row.enabled and row.kind == KIND_SAML:
        if not sso_saml.is_complete(row):
            raise AppError("sso.incomplete",
                           "A SAML provider needs the IdP entity ID, SSO URL and signing "
                           "certificate (load its metadata) before it is enabled",
                           status_code=409)
        return
    if row.enabled and not (row.issuer and row.client_id):
        raise AppError("sso.incomplete",
                       "An identity provider needs an issuer and a client id before it is "
                       "enabled", status_code=409)


@router.get("/api/admin/sso/providers")
def list_providers(db: Session = Depends(get_db)) -> dict[str, Any]:
    linked = _linked_counts(db)
    rows = db.scalars(select(ConsoleIdentityProvider).order_by(
        ConsoleIdentityProvider.sort_order, ConsoleIdentityProvider.name))
    return {
        "items": [_serialize(row, linked.get(row.id, 0), db) for row in rows],
        "presets": PRESETS,
        "callback_url_template": sso_oidc.callback_url("{id}"),
        "acs_url_template": sso_saml.acs_url("{id}"),
        "scim_base_url": sso_scim.base_url(),
        "settings": _settings_answer(db),
    }


@router.post("/api/admin/sso/providers", status_code=201)
def create_provider(
    body: ProviderBody, db: Session = Depends(get_db), caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    sent = set(body.model_fields_set)
    preset = body.preset or sso_oidc.DEFAULT_PRESET
    if preset not in PRESETS:
        raise AppError("sso.invalid_preset",
                       f"preset must be one of {', '.join(PRESETS)}")
    if not body.name:
        raise AppError("sso.invalid_name", "name is required")
    provider_id = (body.id or "").strip().lower() or f"{preset}-{secrets.token_hex(3)}"
    if not _ID_RE.match(provider_id):
        raise AppError("sso.invalid_id",
                       "id must be 2-32 lowercase letters, digits or hyphens")
    if db.get(ConsoleIdentityProvider, provider_id) is not None:
        raise AppError("sso.provider_exists", f"identity provider '{provider_id}' exists",
                       {"provider_id": provider_id}, status_code=409)
    template = PRESETS[preset]
    kind = template["kind"]
    row = ConsoleIdentityProvider(
        id=provider_id, name=body.name, kind=kind, preset=preset, enabled=False,
        scopes=template.get("scopes", "openid email profile"),
        claims=dict(template["claims"]), mappings=[], allowed_email_domains=[],
        created_by=caller.username[:64], saml_idp_certs=[],
        saml_name_id_format=template.get("name_id_format", ""),
        saml_allow_idp_initiated=True, scim_subject="userName",
    )
    if kind == KIND_SAML:
        # the SP's own key pair, generated once (rotatable), sealed like a client secret
        sso_saml.rotate_sp_key(row)
    _apply_body(row, body, sent - {"id", "preset"})
    _check_enable(row)
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit("auth.sso.config.create", f"idp:{row.id} preset:{preset}",
           actor=caller.username, actor_user_id=caller.user_id)
    return _serialize(row, 0, db)


@router.get("/api/admin/sso/providers/{provider_id}")
def get_provider(provider_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = _provider(db, provider_id)
    return _serialize(row, _linked_counts(db).get(row.id, 0), db)


@router.patch("/api/admin/sso/providers/{provider_id}")
def update_provider(
    provider_id: str, body: ProviderBody, db: Session = Depends(get_db),
    caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    row = _provider(db, provider_id)
    if "id" in body.model_fields_set and body.id not in (None, row.id):
        raise AppError("sso.invalid_id", "an identity provider's id cannot change")
    changed = _apply_body(row, body, set(body.model_fields_set) - {"id"})
    _check_enable(row)
    if {"issuer", "client_secret", "client_id"} & set(changed) or any(
            c.startswith("saml_") for c in changed):
        row.last_test = None  # the last test described another configuration
    db.commit()
    db.refresh(row)
    if changed:
        _audit("auth.sso.config.update", f"idp:{row.id} fields:{','.join(changed)}",
               actor=caller.username, actor_user_id=caller.user_id)
    return _serialize(row, _linked_counts(db).get(row.id, 0), db)


@router.delete("/api/admin/sso/providers/{provider_id}")
def delete_provider(
    provider_id: str, db: Session = Depends(get_db), caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    row = _provider(db, provider_id)
    linked = _linked_counts(db).get(row.id, 0)
    if linked:
        # their accounts have no local password: deleting the IdP would strand them
        raise AppError("sso.provider_in_use",
                       f"{linked} account(s) sign in through this provider — disable it "
                       "instead, or delete those accounts first",
                       {"linked_accounts": linked}, status_code=409)
    from sqlalchemy import delete

    from app.models.sso import (
        SamlAssertion,
        SamlRequest,
        SamlSession,
        SsoLoginState,
        SsoMembership,
    )

    db.execute(delete(SsoLoginState).where(SsoLoginState.idp_id == row.id))
    db.execute(delete(SsoMembership).where(SsoMembership.idp_id == row.id))
    for table in (SamlRequest, SamlAssertion, SamlSession):
        db.execute(delete(table).where(table.idp_id == row.id))
    sso_scim.forget_provider(db, row.id)
    db.delete(row)
    db.commit()
    _audit("auth.sso.config.delete", f"idp:{provider_id}",
           actor=caller.username, actor_user_id=caller.user_id)
    return {"ok": True, "id": provider_id}


@router.post("/api/admin/sso/providers/{provider_id}/test")
def test_provider(
    provider_id: str, db: Session = Depends(get_db), caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    row = _provider(db, provider_id)
    if row.kind == KIND_SAML:
        result = sso_saml.test_provider(row)
    else:
        if not row.issuer:
            raise AppError("sso.incomplete", "Set the issuer before testing", status_code=409)
        result = sso_oidc.test_provider(row)
    row.last_test = result
    db.commit()
    _audit("auth.sso.config.test", f"idp:{row.id} ok:{str(result['ok']).lower()}",
           actor=caller.username, actor_user_id=caller.user_id)
    return result


def _saml_provider(db: Session, provider_id: str) -> ConsoleIdentityProvider:
    row = _provider(db, provider_id)
    if row.kind != KIND_SAML:
        raise AppError("sso.not_saml", "This is not a SAML provider", status_code=409)
    return row


@router.post("/api/admin/sso/providers/{provider_id}/saml/metadata")
def load_metadata(
    provider_id: str, body: MetadataBody, db: Session = Depends(get_db),
    caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    """(Re)load the IdP metadata from its URL (the stored one when none is given) or
    an uploaded XML document: entityID, SSO / SLO URLs and certificates."""
    row = _saml_provider(db, provider_id)
    if body.xml:
        found = sso_saml.parse_idp_metadata(body.xml)
        url = ""
    else:
        url = sso_oidc.check_url((body.url or row.saml_metadata_url or "").strip(),
                                 "metadata_url")
        found = sso_saml.fetch_idp_metadata(url)
    changed = sso_saml.apply_idp_metadata(row, found)
    row.saml_metadata_url = url
    if changed:
        row.last_test = None
    db.commit()
    db.refresh(row)
    _audit("auth.sso.config.metadata",
           f"idp:{row.id} source:{'url' if url else 'xml'} fields:{','.join(changed) or '-'}",
           actor=caller.username, actor_user_id=caller.user_id)
    return _serialize(row, _linked_counts(db).get(row.id, 0), db)


@router.post("/api/admin/sso/providers/{provider_id}/saml/sp-key")
def rotate_sp_key(
    provider_id: str, db: Session = Depends(get_db), caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    """A new SP signing key and certificate: the IdP must import the new metadata."""
    row = _saml_provider(db, provider_id)
    sso_saml.rotate_sp_key(row)
    row.last_test = None
    db.commit()
    db.refresh(row)
    _audit("auth.sso.config.sp_key", f"idp:{row.id} rotated",
           actor=caller.username, actor_user_id=caller.user_id)
    return _serialize(row, _linked_counts(db).get(row.id, 0), db)


@router.post("/api/admin/sso/providers/{provider_id}/scim/token")
def rotate_scim_token(
    provider_id: str, db: Session = Depends(get_db), caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    """Mint the provider's SCIM bearer token (replacing any previous one). The value is
    in this answer only — it is stored as a hash."""
    row = _provider(db, provider_id)
    rotated = bool(row.scim_token_hash)
    token = sso_scim.rotate_token(row)
    db.commit()
    db.refresh(row)
    _audit("auth.sso.config.scim_token",
           f"idp:{row.id} {'rotated' if rotated else 'created'} hint:{row.scim_token_hint}",
           actor=caller.username, actor_user_id=caller.user_id)
    return {"token": token, "scim": _scim_block(db, row)}


@router.delete("/api/admin/sso/providers/{provider_id}/scim/token")
def revoke_scim_token(
    provider_id: str, db: Session = Depends(get_db), caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    """Turn SCIM off for the provider: its token stops working at once. What SCIM
    provisioned stays (accounts, groups)."""
    row = _provider(db, provider_id)
    sso_scim.revoke_token(row)
    db.commit()
    db.refresh(row)
    _audit("auth.sso.config.scim_token", f"idp:{row.id} revoked",
           actor=caller.username, actor_user_id=caller.user_id)
    return {"scim": _scim_block(db, row)}


def _settings_answer(db: Session) -> dict[str, Any]:
    return {"hide_local_login": sso_mapping.hide_local_login_setting(db),
            "local_login_hidden": sso_mapping.local_login_hidden(db)}


@router.get("/api/admin/sso/settings")
def get_sso_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _settings_answer(db)


@router.put("/api/admin/sso/settings")
def put_sso_settings(
    body: SettingsBody, db: Session = Depends(get_db), caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    """Hide local login (D-2): only the IdP buttons on the login page, and a password
    login is refused to every account but the built-in administrator (`?local=1`).
    Turning it on needs an enabled provider — else nobody but that one account could
    sign in."""
    if body.hide_local_login:
        enabled = db.scalars(select(ConsoleIdentityProvider.id).where(
            ConsoleIdentityProvider.enabled.is_(True))).first()
        if enabled is None:
            raise AppError("sso.no_enabled_provider",
                           "Enable an identity provider before hiding local login",
                           status_code=409)
    sso_mapping.set_hide_local_login(db, body.hide_local_login, caller.username)
    _audit("auth.sso.config.settings",
           f"hide_local_login:{str(body.hide_local_login).lower()}",
           actor=caller.username, actor_user_id=caller.user_id)
    return _settings_answer(db)


@router.get("/api/admin/sso/providers/{provider_id}/mappings")
def get_mappings(provider_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = _provider(db, provider_id)
    return {"mappings": list(row.mappings or []), "default_role": row.default_role}


@router.put("/api/admin/sso/providers/{provider_id}/mappings")
def put_mappings(
    provider_id: str, body: MappingsBody, db: Session = Depends(get_db),
    caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    row = _provider(db, provider_id)
    row.mappings = sso_mapping.validate_mappings(db, body.mappings)
    db.commit()
    _audit("auth.sso.config.mappings", f"idp:{row.id} count:{len(row.mappings)}",
           actor=caller.username, actor_user_id=caller.user_id)
    return {"mappings": list(row.mappings), "default_role": row.default_role}


_REASON_RE = re.compile(r"reason:(\S+)")
_USER_RE = re.compile(r"user:(\S+)")


@router.get("/api/admin/sso/providers/{provider_id}/logins")
def recent_logins(
    provider_id: str, limit: int = 20, db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The provider's recent sign-ins from the journal, newest first."""
    _provider(db, provider_id)
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.action == LOGIN_ACTION,
               AuditEvent.target.like(f"idp:{provider_id} %"))
        .order_by(AuditEvent.created_at.desc())
        .limit(max(1, min(int(limit), 100)))
    )
    items = []
    for row in rows:
        reason = _REASON_RE.search(row.target or "")
        user = _USER_RE.search(row.target or "")
        items.append({
            "at": row.created_at.isoformat() if row.created_at else None,
            "outcome": row.outcome or "success",
            "username": user.group(1) if user else None,
            "reason": reason.group(1) if reason else None,
            "jit": " jit" in (row.target or ""),
            "target": row.target,
        })
    return {"items": items}


@router.get("/api/admin/sso/users/{user_id}")
def user_sso(user_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The user page's account source (§2.7): local, or the provider, subject and a
    role that differs from what the mapping computes."""
    user = users_service.get_user(db, user_id)
    return {"source": "sso" if users_service.is_sso_account(user) else "local",
            "sso": sso_mapping.account_sso(db, user)}
