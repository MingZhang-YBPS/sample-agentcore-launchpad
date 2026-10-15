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
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _now

KIND_OIDC = "oidc"
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
