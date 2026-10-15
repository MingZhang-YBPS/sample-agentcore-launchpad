"""Console single sign-on (docs/design sso §2.1): identity providers and their state.

Platform-level and hub-global like `system_updates`: no workspace, no project. Three
tables:

* `identity_providers_console` — one configured IdP (SSO-1: OIDC). The client secret
  is a Fernet ciphertext (`client_secret_enc`) or a Secrets Manager ARN
  (`client_secret_ref`); neither ever leaves the backend in plain text.
* `sso_login_states` — the one-time `state` / `nonce` / PKCE `code_verifier` of a
  sign-in in flight, deleted by the callback that consumes it (10 minute window).
* `sso_memberships` — which global role / project membership / project-group
  membership an IdP's group mapping produced (the design's `source = idp:<id>`), so a
  login only adds and removes what the mapping made and an administrator's manual
  changes win.

SSO-2 (SAML 2.0) adds the `saml_*` columns on the provider (the IdP's entityID, SSO /
SLO URLs and signing certificates parsed from its metadata; the SP signing key, a
Fernet ciphertext like the OIDC client secret) and three short-lived tables:

* `sso_saml_requests` — the ID of an AuthnRequest / LogoutRequest this SP sent, so
  the answer's `InResponseTo` must name one (consumed once, 10 minute window);
* `sso_saml_assertions` — the IDs of assertions already accepted, kept 10 minutes
  (or until the assertion's own `NotOnOrAfter`, if later): a replayed one is refused;
* `sso_saml_sessions` — the NameID / SessionIndex of an account's last SAML sign-in,
  which an SP-initiated LogoutRequest must name.

SSO-3 (SCIM 2.0) adds the provider's SCIM bearer token (a SHA-256 hash; the value is
shown once) and `sso_scim_users` / `sso_scim_groups` / `sso_scim_group_members`: the
accounts and groups the IdP provisioned, whose group membership feeds the same
mappings a login applies.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _now

KIND_OIDC = "oidc"
KIND_SAML = "saml"
KINDS = (KIND_OIDC, KIND_SAML)
POLICY_JIT = "jit"
POLICY_EXISTING_ONLY = "existing_only"
ACCOUNT_POLICIES = (POLICY_JIT, POLICY_EXISTING_ONLY)

# `sso_memberships.kind`
MEMBERSHIP_ROLE = "role"  # target_id "" — the account's global role
MEMBERSHIP_PROJECT = "project"
MEMBERSHIP_GROUP = "group"


class ConsoleIdentityProvider(Base):
    __tablename__ = "identity_providers_console"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default=KIND_OIDC)
    # keycloak | authentik | okta | entra | google | oidc — the attribute template it
    # was created from (display only; the claims below are what is applied)
    preset: Mapped[str] = mapped_column(String(32), default="oidc")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    issuer: Mapped[str] = mapped_column(String(512), default="")
    client_id: Mapped[str] = mapped_column(String(256), default="")
    client_secret_enc: Mapped[str | None] = mapped_column(Text, default=None)
    client_secret_ref: Mapped[str | None] = mapped_column(String(512), default=None)
    scopes: Mapped[str] = mapped_column(String(256), default="openid email profile")
    # {username, email, name, groups}: the claim names read from the id_token
    claims: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    account_policy: Mapped[str] = mapped_column(String(16), default=POLICY_JIT)
    # empty = the console's `auth_allowed_email_domains` / blocklist apply
    allowed_email_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    # the global role of an account no role mapping matched
    default_role: Mapped[str] = mapped_column(String(16), default="member")
    # an SSO account's validity is renewed to `now + valid_days` at every login
    valid_days: Mapped[int] = mapped_column(Integer, default=30)
    # [{group, kind: role|project|group, target_id, role_key}] — evaluated in order
    mappings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # the last connection test: {ok, checked_at, ...}
    last_test: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # --- SAML (SSO-2): the IdP side, from its metadata (URL or uploaded XML) or typed
    saml_metadata_url: Mapped[str] = mapped_column(String(1024), default="")
    saml_idp_entity_id: Mapped[str] = mapped_column(String(512), default="")
    saml_sso_url: Mapped[str] = mapped_column(String(1024), default="")
    saml_slo_url: Mapped[str] = mapped_column(String(1024), default="")
    # PEM bodies (no header lines): more than one while the IdP rotates its key
    saml_idp_certs: Mapped[list[str]] = mapped_column(JSON, default=list)
    saml_name_id_format: Mapped[str] = mapped_column(String(128), default="")
    # the SP side: a self-signed certificate and its key (Fernet ciphertext)
    saml_sp_cert: Mapped[str] = mapped_column(Text, default="")
    saml_sp_key_enc: Mapped[str | None] = mapped_column(Text, default=None)
    saml_sign_requests: Mapped[bool] = mapped_column(Boolean, default=False)
    saml_want_encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    # IdP-initiated sign-in (the Identity Center portal's app tile, sso D-4)
    saml_allow_idp_initiated: Mapped[bool] = mapped_column(Boolean, default=True)
    # --- SCIM (SSO-3): sha256 of the bearer token, never the token itself
    scim_token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    scim_token_hint: Mapped[str | None] = mapped_column(String(8), default=None)
    scim_token_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # which SCIM attribute is the account's subject: the NameID / `sub` it signs in as
    scim_subject: Mapped[str] = mapped_column(String(16), default="userName")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class SsoLoginState(Base):
    __tablename__ = "sso_login_states"

    state: Mapped[str] = mapped_column(String(96), primary_key=True)
    idp_id: Mapped[str] = mapped_column(String(32), index=True)
    nonce: Mapped[str] = mapped_column(String(96))
    code_verifier: Mapped[str] = mapped_column(String(128))
    return_to: Mapped[str] = mapped_column(String(512), default="/")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SsoMembership(Base):
    __tablename__ = "sso_memberships"

    idp_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(32), primary_key=True, default="")
    # the role the mapping set; a live row that no longer carries it was changed by hand
    role_key: Mapped[str] = mapped_column(String(32))
    # role kind only: the account's role differs from what the mapping computes because
    # an administrator set it by hand (sso §2.3: manual wins, shown on the user page)
    conflict: Mapped[str | None] = mapped_column(String(32), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class SamlRequest(Base):
    """An AuthnRequest / LogoutRequest in flight; the answer's InResponseTo names it."""

    __tablename__ = "sso_saml_requests"

    request_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    idp_id: Mapped[str] = mapped_column(String(32), index=True)
    purpose: Mapped[str] = mapped_column(String(8), default="login")  # login | logout
    return_to: Mapped[str] = mapped_column(String(512), default="/")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SamlAssertion(Base):
    """An accepted assertion's ID, kept until `expires_at` so a replay is refused."""

    __tablename__ = "sso_saml_assertions"

    idp_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    assertion_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SamlSession(Base):
    """The NameID and SessionIndex of an account's last SAML sign-in (for SLO)."""

    __tablename__ = "sso_saml_sessions"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    idp_id: Mapped[str] = mapped_column(String(32), index=True)
    name_id: Mapped[str] = mapped_column(String(512))
    name_id_format: Mapped[str | None] = mapped_column(String(128), default=None)
    name_id_nq: Mapped[str | None] = mapped_column(String(256), default=None)
    name_id_spnq: Mapped[str | None] = mapped_column(String(256), default=None)
    session_index: Mapped[str | None] = mapped_column(String(256), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ScimUser(Base):
    """An account the IdP provisioned over SCIM. A DELETE marks it `deleted_at` (the
    account is disabled, not erased: the journal keeps naming it) and the resource
    answers 404 from then on."""

    __tablename__ = "sso_scim_users"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    idp_id: Mapped[str] = mapped_column(String(32), index=True)
    # SCIM `userName` and `externalId` as the IdP sent them; one of them (the
    # provider's `scim_subject`) is also the account's `external_subject`
    user_name: Mapped[str] = mapped_column(String(255), default="")
    external_id: Mapped[str | None] = mapped_column(String(255), default=None)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    # {givenName, familyName, formatted}: echoed back, never interpreted
    name: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ScimGroup(Base):
    """An IdP group pushed over SCIM; a mapping matches its displayName or externalId."""

    __tablename__ = "sso_scim_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    idp_id: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(String(256))
    external_id: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ScimGroupMember(Base):
    __tablename__ = "sso_scim_group_members"

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True, index=True)
