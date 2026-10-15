"""Console single sign-on (docs/design sso §2.6; SSO-1: OIDC).

Two surfaces, both hub-global:

* **public** — `GET /api/auth/providers` (the login page's buttons) and the OIDC
  redirect leg `GET /api/auth/oidc/{id}/start` → IdP → `/callback`. The callback
  answers with a redirect either way: into the console with the session cookie set,
  or back to the login page with `?sso_error=<code>` — never a JSON error page in
  the middle of a browser redirect.
* **administrators** — `/api/admin/sso/providers*`: configure an IdP, test its
  discovery / JWKS, edit its group mappings, read its recent sign-ins. The client
  secret is write-only: answers carry `client_secret_set` and the Secrets Manager
  ARN when that is the source, never the value.

Journal: `auth.sso.login` (outcome success / refused / failed, the reason code in
the target), `auth.sso.config.create|update|delete|test|mappings`.
"""

import logging
import re
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import AuditEvent, User
from app.models.sso import ACCOUNT_POLICIES, KIND_OIDC, ConsoleIdentityProvider
from app.routers.auth import Identity, issue_user_session, require_identity
from app.services import sso_mapping, sso_oidc
from app.services import users as users_service
from app.services.audit import OUTCOME_FAILED, OUTCOME_REFUSED, record_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sso"])

LOGIN_ACTION = "auth.sso.login"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
# where the login page is: the console root renders it for a signed-out browser
LOGIN_PATH = "/"


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
    return {"providers": sso_oidc.public_providers(db)}


def _login_error(code: str, provider_id: str) -> RedirectResponse:
    query = urlencode({"sso_error": code, "sso_provider": provider_id})
    return RedirectResponse(f"{LOGIN_PATH}?{query}", status_code=302)


@router.get("/api/auth/oidc/{provider_id}/start")
def oidc_start(
    provider_id: str, return_to: str | None = None, db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        row = sso_oidc.enabled_provider(db, provider_id)
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
        row = sso_oidc.enabled_provider(db, provider_id)
        if error:
            # the IdP refused (access_denied, a consent the user declined…); the state
            # is spent all the same
            sso_oidc.consume_state(db, row.id, state)
            raise AppError("auth.sso.idp_error", f"the identity provider answered {error}",
                           {"error": error[:64]}, status_code=401)
        claims, return_to = sso_oidc.complete_login(db, row, code=code, state=state,
                                                    settings=settings)
        profile = sso_mapping.profile_from_claims(row, claims)
        subject = profile.subject
        user, created = sso_mapping.provision(db, row, profile, settings)
        changes = sso_mapping.apply(db, row, user, profile.groups, created=created)
        users_service.record_login(db, user)
    except AppError as err:
        outcome = OUTCOME_FAILED if err.status_code >= 500 else OUTCOME_REFUSED
        _audit(LOGIN_ACTION, f"idp:{provider_id} reason:{err.code} subject:{subject}",
               outcome=outcome)
        return _login_error(err.code, provider_id)
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
    response = RedirectResponse(return_to, status_code=302)
    issue_user_session(response, user, settings)
    return response


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


class MappingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mappings: list[dict[str, Any]] = Field(max_length=sso_mapping.MAX_MAPPINGS)


def _serialize(row: ConsoleIdentityProvider, linked: int = 0) -> dict[str, Any]:
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
        "claims": {**sso_oidc.PRESETS[sso_oidc.DEFAULT_PRESET]["claims"], **(row.claims or {})},
        "account_policy": row.account_policy,
        "allowed_email_domains": list(row.allowed_email_domains or []),
        "default_role": row.default_role,
        "valid_days": row.valid_days,
        "mappings": list(row.mappings or []),
        "last_test": row.last_test,
        "callback_url": sso_oidc.callback_url(row.id),
        "linked_accounts": linked,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


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
        if body.preset not in sso_oidc.PRESETS:
            raise AppError("sso.invalid_preset",
                           f"preset must be one of {', '.join(sso_oidc.PRESETS)}")
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
    return changed


def _check_enable(row: ConsoleIdentityProvider) -> None:
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
        "items": [_serialize(row, linked.get(row.id, 0)) for row in rows],
        "presets": sso_oidc.PRESETS,
        "callback_url_template": sso_oidc.callback_url("{id}"),
    }


@router.post("/api/admin/sso/providers", status_code=201)
def create_provider(
    body: ProviderBody, db: Session = Depends(get_db), caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    sent = set(body.model_fields_set)
    preset = body.preset or sso_oidc.DEFAULT_PRESET
    if preset not in sso_oidc.PRESETS:
        raise AppError("sso.invalid_preset",
                       f"preset must be one of {', '.join(sso_oidc.PRESETS)}")
    if not body.name:
        raise AppError("sso.invalid_name", "name is required")
    provider_id = (body.id or "").strip().lower() or f"{preset}-{secrets.token_hex(3)}"
    if not _ID_RE.match(provider_id):
        raise AppError("sso.invalid_id",
                       "id must be 2-32 lowercase letters, digits or hyphens")
    if db.get(ConsoleIdentityProvider, provider_id) is not None:
        raise AppError("sso.provider_exists", f"identity provider '{provider_id}' exists",
                       {"provider_id": provider_id}, status_code=409)
    template = sso_oidc.PRESETS[preset]
    row = ConsoleIdentityProvider(
        id=provider_id, name=body.name, kind=KIND_OIDC, preset=preset, enabled=False,
        scopes=template["scopes"], claims=dict(template["claims"]), mappings=[],
        allowed_email_domains=[], created_by=caller.username[:64],
    )
    _apply_body(row, body, sent - {"id", "preset"})
    _check_enable(row)
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit("auth.sso.config.create", f"idp:{row.id} preset:{preset}",
           actor=caller.username, actor_user_id=caller.user_id)
    return _serialize(row)


@router.get("/api/admin/sso/providers/{provider_id}")
def get_provider(provider_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = _provider(db, provider_id)
    return _serialize(row, _linked_counts(db).get(row.id, 0))


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
    if "issuer" in changed or "client_secret" in changed or "client_id" in changed:
        row.last_test = None  # the last test described another configuration
    db.commit()
    db.refresh(row)
    if changed:
        _audit("auth.sso.config.update", f"idp:{row.id} fields:{','.join(changed)}",
               actor=caller.username, actor_user_id=caller.user_id)
    return _serialize(row, _linked_counts(db).get(row.id, 0))


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

    from app.models.sso import SsoLoginState, SsoMembership

    db.execute(delete(SsoLoginState).where(SsoLoginState.idp_id == row.id))
    db.execute(delete(SsoMembership).where(SsoMembership.idp_id == row.id))
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
    if not row.issuer:
        raise AppError("sso.incomplete", "Set the issuer before testing", status_code=409)
    result = sso_oidc.test_provider(row)
    row.last_test = result
    db.commit()
    _audit("auth.sso.config.test", f"idp:{row.id} ok:{str(result['ok']).lower()}",
           actor=caller.username, actor_user_id=caller.user_id)
    return result


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
