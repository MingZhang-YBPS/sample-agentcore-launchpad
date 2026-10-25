"""Trusted console identity -> Cognito JWT bridge for Gateway Policy.

AgentCore Gateway builds an ``OAuthUser`` principal from the JWT it receives.
``runtimeUserId`` does not become a JWT claim: the existing M2M exchange always
returns the same client principal. Chat therefore needs a real user access token
when Cedar rules must evaluate ``username`` or ``cognito:groups``.

The browser never supplies this token or chooses its subject. Callers pass the
``Identity`` fields already resolved from the signed console session.

Besides the role groups, the user is put in the ``lp-project-<pid>`` group of every
isolated project they belong to in the workspace (projects §10.6-2), so the Cedar
rule that keeps a project's tools to the project admits its members' own calls.
A shared project has no such group; a single-project workspace sees no change.

**Role groups.** Every console user's token carries ``lp-role-<role>`` for their
platform role — one of the four ``users.ROLES`` (admin, operator, member, steward).
``admin`` and ``member`` ALSO keep their legacy group (``platform-admin`` /
``hr-analyst``), which the infra stack, workspace bootstrap, the workshop and every
existing Cedar policy name; ``operator`` and ``steward`` get no legacy group (least
privilege: a policy written for one of the legacy groups never admits them). A
missing ``lp-role-*`` group is created on first use (an older pool predates them);
``workspace_bootstrap`` creates them with the rest.
"""

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings, load_yaml_config
from app.core.db import DEFAULT_WORKSPACE_ID
from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

# the legacy group each role kept (existing Cedar policies and infra name these)
ROLE_GROUP = {"admin": "platform-admin", "member": "hr-analyst"}
# one group per platform role (`users.ROLES`), on every console user's token
ROLE_GROUP_PREFIX = "lp-role-"
PLATFORM_ROLES = ("admin", "operator", "member", "steward")
POLICY_GROUPS = frozenset(ROLE_GROUP.values()) | frozenset(
    f"{ROLE_GROUP_PREFIX}{role}" for role in PLATFORM_ROLES)
# `resource_names(pid).cognito_group` — reconciled like the role groups
PROJECT_GROUP_PREFIX = "lp-project-"
SHADOW_MARKER = "launchpad-policy-shadow-v1"


def role_groups(role: str) -> frozenset[str]:
    """The groups a console role puts on the token: `lp-role-<role>`, plus the legacy
    group of admin / member. Empty for an unknown role."""
    if role not in PLATFORM_ROLES:
        return frozenset()
    legacy = ROLE_GROUP.get(role)
    return frozenset({f"{ROLE_GROUP_PREFIX}{role}", *([legacy] if legacy else [])})

_token_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
_lock = threading.Lock()


def _demo_password(workspace: WorkspaceContext, username: str) -> str | None:
    """The bootstrap-owned password for a demo user, or None.

    `demo_users.passwords` in `config/launchpad.yaml` belongs to the **hub's**
    user pool: only `make bootstrap` provisions those users, and a
    console-registered workspace deliberately has none (see
    `workspace_bootstrap`, which creates the groups but no lab users). Taking the
    demo path against another pool would look up a user that cannot exist there
    and answer 503; that caller gets a Launchpad-owned shadow user instead, like
    every other console account.
    """
    if workspace.id != DEFAULT_WORKSPACE_ID:
        return None
    passwords = (load_yaml_config().get("demo_users") or {}).get("passwords") or {}
    value = passwords.get(username)
    return str(value) if value else None


def _shadow_password(username: str) -> str:
    settings = get_settings()
    secret = settings.auth_password.get_secret_value() if settings.auth_password else ""
    if not secret:
        raise AppError(
            "policy.identity_unavailable",
            "Authenticated policy identity requires console authentication",
            status_code=503,
        )
    digest = hmac.new(
        secret.encode("utf-8"),
        f"launchpad-policy-shadow:{username}".encode(),
        hashlib.sha256,
    ).digest()
    # Cognito's pool requires lower/upper/digit. The suffix makes those
    # properties explicit while the HMAC supplies the entropy.
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=") + "aA1"


def _attributes(response: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("Name")): str(item.get("Value"))
        for item in (response.get("UserAttributes") or response.get("Attributes") or [])
        if item.get("Name") and item.get("Value") is not None
    }


def _ensure_user(
    cognito: Any,
    *,
    pool_id: str,
    username: str,
    email: str | None,
    password: str,
    demo_user: bool,
) -> None:
    try:
        user = cognito.admin_get_user(UserPoolId=pool_id, Username=username)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "UserNotFoundException":
            raise
        if demo_user:
            raise AppError(
                "policy.identity_unavailable",
                f"Bootstrapped Cognito user '{username}' is missing; run make bootstrap",
                status_code=503,
            ) from exc
        user = cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=username,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email or f"{username}@launchpad.local"},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "preferred_username", "Value": SHADOW_MARKER},
            ],
        ).get("User") or {}

    attrs = _attributes(user)
    if not demo_user and attrs.get("preferred_username") != SHADOW_MARKER:
        raise AppError(
            "policy.identity_conflict",
            f"Cognito username '{username}' exists but is not owned by Launchpad",
            status_code=409,
        )
    cognito.admin_set_user_password(
        UserPoolId=pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )


def _user_groups(cognito: Any, *, pool_id: str, username: str) -> set[str]:
    out: set[str] = set()
    token = None
    while True:
        kwargs: dict[str, Any] = {"UserPoolId": pool_id, "Username": username, "Limit": 60}
        if token:
            kwargs["NextToken"] = token
        page = cognito.admin_list_groups_for_user(**kwargs)
        out |= {str(g.get("GroupName")) for g in page.get("Groups", []) if g.get("GroupName")}
        token = page.get("NextToken")
        if not token:
            return out


def _add_to_group(cognito: Any, *, pool_id: str, username: str, group: str) -> None:
    """Add the user; a missing `lp-role-*` group (a pool older than them) is created
    first, idempotently. Any other missing group is the bootstrap's to make."""
    try:
        cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=group)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "ResourceNotFoundException" or not group.startswith(ROLE_GROUP_PREFIX):
            raise
        try:
            cognito.create_group(UserPoolId=pool_id, GroupName=group,
                                 Description=f"Launchpad role: {group}")
        except ClientError as create_exc:
            if create_exc.response.get("Error", {}).get("Code") != "GroupExistsException":
                raise
        cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=group)


def _reconcile_group(
    cognito: Any, *, pool_id: str, username: str, role: str,
    project_groups: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Make the user's managed groups exactly the role's (`role_groups`) and the
    project groups; returns the role's groups. Stale managed groups — a legacy or
    `lp-role-*` group of another role, an `lp-project-*` group no longer theirs — are
    removed; groups Launchpad does not manage are left alone."""
    expected = role_groups(role)
    if not expected:
        raise AppError(
            "policy.identity_role_invalid",
            f"Console role '{role}' cannot be mapped to a policy principal",
            status_code=500,
        )
    current = _user_groups(cognito, pool_id=pool_id, username=username)
    managed = {g for g in current if g in POLICY_GROUPS or g.startswith(
        (ROLE_GROUP_PREFIX, PROJECT_GROUP_PREFIX))}
    wanted = {*expected, *project_groups}
    for group in sorted(managed - wanted):
        cognito.admin_remove_user_from_group(
            UserPoolId=pool_id,
            Username=username,
            GroupName=group,
        )
    for group in sorted(wanted - current):
        _add_to_group(cognito, pool_id=pool_id, username=username, group=group)
    return expected


def _project_groups(workspace: WorkspaceContext, username: str, role: str) -> frozenset[str]:
    """The isolated projects' groups the user's token carries (`project_gateway`)."""
    from app.core.db import SessionLocal
    from app.services import project_gateway

    db = SessionLocal()
    try:
        return frozenset(project_gateway.project_groups_for(db, username, role, workspace.id))
    except SQLAlchemyError:
        # fewer groups only narrows what the token may call — never widens it
        return frozenset()
    finally:
        db.close()


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("access token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
    claims = json.loads(decoded)
    if not isinstance(claims, dict):
        raise ValueError("access token payload is not an object")
    return claims


def gateway_user_token(
    workspace: WorkspaceContext,
    username: str,
    role: str,
    email: str | None = None,
    *,
    min_valid_s: float = 60,
) -> str | None:
    """Return a verified Cognito user JWT for an authenticated Chat caller.

    ``None`` is deliberate only while the console auth gate is disabled, where
    there is no authenticated human and the established M2M path remains the
    correct identity.

    A cached token is reused while it stays valid for ``min_valid_s`` more
    seconds. A caller whose token must still be the cached one later (the managed
    COA gateway's consent, completed with the very JWT it was asked with) asks for
    a longer margin, so the cache cannot roll over in between.
    """

    settings = get_settings()
    if not settings.auth_password or not settings.auth_password.get_secret_value():
        return None
    pool_id = str(workspace.resources.get("user_pool_id") or "")
    client_id = str(workspace.resources.get("user_pool_client_id") or "")
    if not (pool_id and client_id):
        raise AppError(
            "policy.identity_unavailable",
            "Cognito policy identity is not bootstrapped",
            status_code=503,
        )

    # Keyed on the workspace too: each environment has its own user pool, so a
    # token minted against one is rejected by another's gateway. And on the project
    # groups: a membership change must mint a token that carries it.
    project_groups = _project_groups(workspace, username, role)
    key = (workspace.account_id, workspace.region, username, role, tuple(sorted(project_groups)))
    cached = _token_cache.get(key)
    if cached and float(cached["expires_at"]) > time.time() + min_valid_s:
        return str(cached["token"])

    with _lock:
        cached = _token_cache.get(key)
        if cached and float(cached["expires_at"]) > time.time() + min_valid_s:
            return str(cached["token"])
        demo_password = _demo_password(workspace, username)
        password = demo_password or _shadow_password(username)
        cognito = workspace.client("cognito-idp")
        try:
            _ensure_user(
                cognito,
                pool_id=pool_id,
                username=username,
                email=email,
                password=password,
                demo_user=demo_password is not None,
            )
            expected_groups = _reconcile_group(
                cognito,
                pool_id=pool_id,
                username=username,
                role=role,
                project_groups=project_groups,
            )
            auth = cognito.initiate_auth(
                ClientId=client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": username, "PASSWORD": password},
            )["AuthenticationResult"]
        except AppError:
            raise
        except (BotoCoreError, ClientError, KeyError) as exc:
            raise AppError(
                "policy.identity_unavailable",
                "Could not obtain the authenticated user's Gateway policy identity",
                status_code=503,
            ) from exc

        token = str(auth.get("AccessToken") or "")
        try:
            claims = _jwt_claims(token)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AppError(
                "policy.identity_invalid",
                "Cognito returned an invalid policy identity token",
                status_code=502,
            ) from exc
        groups = claims.get("cognito:groups") or []
        if (claims.get("username") != username or not expected_groups <= set(groups)
                or not project_groups <= set(groups)):
            raise AppError(
                "policy.identity_invalid",
                "Cognito policy identity does not match the authenticated console user",
                status_code=502,
            )
        expires_at = min(
            float(claims.get("exp") or (time.time() + auth.get("ExpiresIn", 3600))),
            time.time() + float(auth.get("ExpiresIn", 3600)),
        )
        _token_cache[key] = {"token": token, "expires_at": expires_at}
        return token
