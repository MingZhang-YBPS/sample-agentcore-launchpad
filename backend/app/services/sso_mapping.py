"""SSO accounts and group mappings (docs/design sso §2.1 账号策略 / 组映射, §2.3).

After `sso_oidc` has verified an id_token, this module turns its claims into a
console account and its access:

* **the account** is keyed by `(idp_id, subject)` — `users.external_idp_id` /
  `external_subject` — never by email, so re-binding an address at the IdP cannot
  take over another account. `jit` creates it on first login (active, no local
  password, the default workspace grant an administrator-created account gets);
  `existing_only` refuses a subject that has no account yet. The email must pass the
  provider's domain list, else the console's registration rule.
* **the mappings** (`[{group, kind, target_id, role_key}]`, in order) compute a
  global role, project memberships (a role template or custom role) and project-group
  memberships, re-applied at every login (D-3). Only what a mapping produced — rows in
  `sso_memberships`, the design's `source = idp:<id>` — is updated or removed; a
  membership an administrator added by hand is left alone, and so is a role or a
  membership role an administrator changed (manual wins; a role conflict is
  recorded for the user page). Any change bumps `access_epoch`, so it applies on the
  next request.
"""

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import DEFAULT_WORKSPACE_ID
from app.core.errors import AppError
from app.models.ledger import User, UserWorkspace
from app.models.projects import GroupMember, Project, ProjectGroup, ProjectMember
from app.models.sso import (
    MEMBERSHIP_GROUP,
    MEMBERSHIP_PROJECT,
    MEMBERSHIP_ROLE,
    POLICY_EXISTING_ONLY,
    ConsoleIdentityProvider,
    SsoMembership,
)
from app.services import users as users_service

logger = logging.getLogger(__name__)

MAPPING_KINDS = (MEMBERSHIP_ROLE, MEMBERSHIP_PROJECT, MEMBERSHIP_GROUP)
MAX_MAPPINGS = 200


@dataclass(frozen=True)
class SsoActor:
    """The journal's actor for what a login's mappings change."""

    username: str
    user_id: str | None = None
    role: str = users_service.ROLE_ADMIN


@dataclass
class ExternalProfile:
    subject: str
    username: str
    email: str
    name: str
    groups: list[str] = field(default_factory=list)


def _claim(claims: dict[str, Any], name: str | None) -> Any:
    """A claim by name; a dotted name reads a nested object (`realm_access.roles`)."""
    if not name:
        return None
    value: Any = claims
    for part in name.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def profile_from_claims(row: ConsoleIdentityProvider, claims: dict[str, Any]) -> ExternalProfile:
    names = row.claims or {}
    groups_raw = _claim(claims, names.get("groups"))
    if isinstance(groups_raw, str):
        groups = [groups_raw]
    elif isinstance(groups_raw, list):
        groups = [str(g) for g in groups_raw if isinstance(g, str | int)]
    else:
        groups = []
    email = _claim(claims, names.get("email") or "email")
    username = _claim(claims, names.get("username") or "preferred_username")
    name = _claim(claims, names.get("name") or "name")
    return ExternalProfile(
        subject=str(claims["sub"]),
        username=username if isinstance(username, str) else "",
        email=email.strip().lower() if isinstance(email, str) else "",
        name=name if isinstance(name, str) else "",
        # Keycloak's full-path groups ("/team/dev") match a mapping written either way
        groups=sorted({g.strip() for g in groups if g.strip()}),
    )


# --- the account ---------------------------------------------------------------


def check_email(row: ConsoleIdentityProvider, email: str, settings: Settings) -> str:
    """The provider's domain list when it has one, else the console's registration
    rule (`auth_allowed_email_domains`, the public-mailbox blocklist)."""
    if not email:
        raise AppError("auth.sso.email_missing",
                       "The identity provider sent no email address for this account",
                       status_code=403)
    allowed = [d for d in (row.allowed_email_domains or []) if str(d).strip()]
    if allowed:
        domain = email.rpartition("@")[2]
        if not any(users_service._domain_matches(domain, str(entry)) for entry in allowed):
            raise AppError("auth.sso.email_domain_blocked",
                           "Your email domain is not allowed to sign in to this console",
                           status_code=403)
        return email
    try:
        return users_service.validate_email(email, settings)
    except AppError as err:
        code = ("auth.sso.email_domain_blocked" if err.code == "auth.email_domain_blocked"
                else "auth.sso.email_invalid")
        raise AppError(code, err.message, status_code=403) from err


_USERNAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _candidate_username(profile: ExternalProfile) -> str:
    raw = profile.username or profile.email.partition("@")[0]
    if "@" in raw:
        raw = raw.partition("@")[0]
    name = _USERNAME_CHARS.sub("-", raw).strip("._-")[:32]
    if len(name) < 3:
        digest = hashlib.sha256(profile.subject.encode("utf-8")).hexdigest()[:8]
        name = f"sso-{digest}"
    return name


def _free_username(db: Session, base: str, settings: Settings) -> str:
    reserved = settings.auth_username.strip().lower()
    for index in range(1, 100):
        suffix = "" if index == 1 else f"-{index}"
        name = f"{base[:32 - len(suffix)]}{suffix}"
        if name.lower() != reserved and users_service.find_by_username(db, name) is None:
            return name
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"sso-{digest}"


def find_account(db: Session, idp_id: str, subject: str) -> User | None:
    return db.scalars(
        select(User).where(User.external_idp_id == idp_id, User.external_subject == subject)
    ).first()


def provision(
    db: Session, row: ConsoleIdentityProvider, profile: ExternalProfile,
    settings: Settings | None = None,
) -> tuple[User, bool]:
    """The account for this subject — found, or created under `jit`. Returns
    `(user, created)`. Refusals carry `auth.sso.*` codes the login page explains."""
    current = settings or get_settings()
    email = check_email(row, profile.email, current)
    user = find_account(db, row.id, profile.subject)
    now = datetime.now(UTC)
    if user is not None:
        if user.status == users_service.STATUS_PENDING:
            raise AppError("auth.account_pending",
                           "This account is waiting for administrator approval",
                           status_code=403)
        if user.status != users_service.STATUS_ACTIVE:
            raise AppError("auth.account_disabled", "This account is disabled",
                           status_code=403)
        # the address follows the IdP unless another account holds it
        if email != user.email:
            holder = users_service.find_by_email(db, email)
            if holder is None:
                user.email = email
        _renew(user, row, now)
        db.commit()
        return user, False
    if row.account_policy == POLICY_EXISTING_ONLY:
        raise AppError("auth.sso.not_provisioned",
                       "No console account exists for you yet — ask an administrator",
                       status_code=403)
    if users_service.find_by_email(db, email) is not None:
        # an existing local (or other-IdP) account holds the address: never link by
        # email (§2.2), an administrator decides
        raise AppError("auth.sso.email_taken",
                       "Another console account already uses your email address — ask an "
                       "administrator", status_code=409)
    name = _free_username(db, _candidate_username(profile), current)
    user = User(
        id=uuid.uuid4().hex,
        username=name,
        username_key=name.lower(),
        email=email,
        password_hash=users_service.SSO_PASSWORD_HASH,
        role=users_service.ROLE_MEMBER,
        status=users_service.STATUS_ACTIVE,
        expires_at=now + timedelta(days=max(1, int(row.valid_days or 30))),
        created_by=f"sso:{row.id}"[:64],
        external_idp_id=row.id,
        external_subject=profile.subject,
    )
    db.add(user)
    db.add(UserWorkspace(user_id=user.id, workspace_id=DEFAULT_WORKSPACE_ID))
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AppError("auth.sso.account_conflict",
                       "Your account is being created by another sign-in — try again",
                       status_code=409) from exc
    from app.services.access import sync_default_membership

    sync_default_membership(db, user.id, {DEFAULT_WORKSPACE_ID}, previous=set(),
                            role=user.role)
    db.commit()
    db.refresh(user)
    return user, True


def _renew(user: User, row: ConsoleIdentityProvider, now: datetime) -> None:
    """Validity follows the last SSO login (§2.2): `now + valid_days`, never
    shortened; an account an administrator made non-expiring stays so."""
    if user.expires_at is None:
        return
    renewed = now + timedelta(days=max(1, int(row.valid_days or 30)))
    current = users_service.as_utc(user.expires_at)
    if current is None or current < renewed:
        user.expires_at = renewed


# --- the mappings --------------------------------------------------------------


def validate_mappings(db: Session, value: Any) -> list[dict[str, Any]]:
    """`[{group, kind, target_id, role_key}]`: the target exists and the role is one
    it takes. 400 `sso.invalid_mappings` / 404 for an unknown target."""
    from app.services.access import ROLE_TEMPLATES
    from app.services.projects import custom_roles

    if not isinstance(value, list) or len(value) > MAX_MAPPINGS:
        raise AppError("sso.invalid_mappings",
                       f"mappings must be a list of at most {MAX_MAPPINGS} items")
    items: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise AppError("sso.invalid_mappings", "every mapping must be an object")
        group = str(raw.get("group") or "").strip()
        kind = raw.get("kind")
        role_key = str(raw.get("role_key") or "").strip()
        target = str(raw.get("target_id") or "").strip()
        if not group or len(group) > 256:
            raise AppError("sso.invalid_mappings", "every mapping needs an IdP group name")
        if kind not in MAPPING_KINDS:
            raise AppError("sso.invalid_mappings",
                           f"kind must be one of {', '.join(MAPPING_KINDS)}", {"group": group})
        if kind == MEMBERSHIP_ROLE:
            if role_key not in users_service.ROLES:
                raise AppError("sso.invalid_mappings",
                               f"role must be one of {', '.join(users_service.ROLES)}",
                               {"group": group})
            target = ""
        elif kind == MEMBERSHIP_PROJECT:
            project = db.get(Project, target) if target else None
            if project is None:
                raise AppError("project.not_found", f"project '{target}' not found",
                               {"project_id": target}, status_code=404)
            if role_key not in ROLE_TEMPLATES and role_key not in custom_roles(db, target):
                raise AppError("project.role_unknown",
                               f"'{role_key}' is not a role of project '{target}'",
                               {"role_key": role_key, "project_id": target})
        else:
            if (db.get(ProjectGroup, target) if target else None) is None:
                raise AppError("group.not_found", f"project group '{target}' not found",
                               {"group_id": target}, status_code=404)
            if role_key not in ROLE_TEMPLATES:
                raise AppError("group.role_unknown",
                               f"'{role_key}' is not a built-in role (groups take templates)",
                               {"role_key": role_key, "group_id": target})
        items.append({"group": group, "kind": kind, "target_id": target, "role_key": role_key})
    return items


def _group_matches(mapping_group: str, groups: set[str]) -> bool:
    name = mapping_group.strip()
    return name in groups or f"/{name.lstrip('/')}" in groups or name.lstrip("/") in groups


def compute(row: ConsoleIdentityProvider, groups: list[str]) -> dict[str, Any]:
    """`{role, projects: {id: role_key}, groups: {id: role_key}}` from the mappings
    the caller's groups match; the first match wins for each target."""
    held = set(groups)
    role: str | None = None
    projects: dict[str, str] = {}
    project_groups: dict[str, str] = {}
    for item in row.mappings or []:
        if not _group_matches(str(item.get("group", "")), held):
            continue
        kind, target, role_key = item.get("kind"), item.get("target_id"), item.get("role_key")
        if kind == MEMBERSHIP_ROLE and role is None:
            role = role_key
        elif kind == MEMBERSHIP_PROJECT and target:
            projects.setdefault(target, role_key)
        elif kind == MEMBERSHIP_GROUP and target:
            project_groups.setdefault(target, role_key)
    default = row.default_role if row.default_role in users_service.ROLES else "member"
    return {"role": role or default, "projects": projects, "groups": project_groups}


def _records(db: Session, idp_id: str, user_id: str, kind: str) -> dict[str, SsoMembership]:
    return {
        rec.target_id: rec
        for rec in db.scalars(select(SsoMembership).where(
            SsoMembership.idp_id == idp_id, SsoMembership.user_id == user_id,
            SsoMembership.kind == kind,
        ))
    }


def apply(
    db: Session, row: ConsoleIdentityProvider, user: User, groups: list[str],
    *, created: bool = False,
) -> dict[str, Any]:
    """Apply the mappings to the account (one transaction, committed). Returns what
    changed, for the login's journal line."""
    from app.services.access import bump_epoch

    wanted = compute(row, groups)
    actor = SsoActor(username=f"sso:{row.id}"[:64])
    changes: dict[str, Any] = {"role": None, "projects": [], "groups": [], "conflict": None}
    changes["conflict"] = _apply_role(db, row, user, wanted["role"], actor, changes,
                                      created=created)
    if user.role not in users_service.PLATFORM_ROLES:
        # administrators reach every project by role and stewards none (§11.4): neither
        # holds membership rows, so a mapping writes none for them
        _apply_projects(db, row, user, wanted["projects"], actor, changes)
        _apply_groups(db, row, user, wanted["groups"], actor, changes)
    if changes["role"] or changes["projects"] or changes["groups"]:
        bump_epoch(db)
    db.commit()
    db.refresh(user)
    return changes


def _role_record(db: Session, row: ConsoleIdentityProvider, user: User) -> SsoMembership:
    record = _records(db, row.id, user.id, MEMBERSHIP_ROLE).get("")
    if record is None:
        record = SsoMembership(idp_id=row.id, user_id=user.id, kind=MEMBERSHIP_ROLE,
                               target_id="", role_key=user.role)
        db.add(record)
    return record


def _apply_role(
    db: Session, row: ConsoleIdentityProvider, user: User, role: str, actor: SsoActor,
    changes: dict[str, Any], *, created: bool,
) -> str | None:
    """Set the mapped global role unless an administrator changed it by hand.
    Returns the conflicting mapped role when manual wins (§2.3), else None."""
    known = _records(db, row.id, user.id, MEMBERSHIP_ROLE).get("") is not None
    record = _role_record(db, row, user)
    if (known and user.role != record.role_key) or (not known and not created):
        # changed by hand since the mapping last set it — or an account whose role no
        # mapping ever set: the administrator's value stands
        record.conflict = role if role != user.role else None
        return record.conflict
    record.conflict = None
    if user.role == role:
        return None
    try:
        # its own transaction (it commits, and rolls back what it refuses)
        users_service.apply_patch(db, user, {"role": role}, actor=actor)
    except AppError as err:
        # e.g. `users.last_admin`: the change is refused, the login is not
        logger.warning("sso %s: role %s for %s refused: %s", row.id, role, user.id, err.code)
        db.rollback()
        record = _role_record(db, row, user)
        record.conflict = role
        return role
    record = _role_record(db, row, user)
    record.role_key, record.conflict = role, None
    changes["role"] = role
    return None


def _apply_projects(
    db: Session, row: ConsoleIdentityProvider, user: User, wanted: dict[str, str],
    actor: SsoActor, changes: dict[str, Any],
) -> None:
    from app.services import projects as projects_service

    records = _records(db, row.id, user.id, MEMBERSHIP_PROJECT)
    for project_id, role_key in sorted(wanted.items()):
        project = db.get(Project, project_id)
        if project is None or project.status != "active":
            continue
        existing = db.get(ProjectMember, (project_id, user.id))
        record = records.get(project_id)
        if existing is not None and (record is None or existing.role_key != record.role_key):
            # an administrator's row (or role): left alone, and no longer the mapping's
            if record is not None:
                db.delete(record)
            continue
        if existing is not None and existing.role_key == role_key:
            continue
        try:
            with db.begin_nested():
                projects_service.upsert_members(
                    db, actor, project,
                    [{"user_id": user.id, "role_key": role_key,
                      "overrides": existing.overrides if existing is not None else None}],
                    [], check_self=False,
                )
        except AppError as err:
            logger.warning("sso %s: project %s for %s refused: %s",
                           row.id, project_id, user.id, err.code)
            continue
        if record is None:
            db.add(SsoMembership(idp_id=row.id, user_id=user.id, kind=MEMBERSHIP_PROJECT,
                                 target_id=project_id, role_key=role_key))
        else:
            record.role_key = role_key
        changes["projects"].append(f"+{project_id}:{role_key}")
    for project_id, record in sorted(records.items()):
        if project_id in wanted:
            continue
        existing = db.get(ProjectMember, (project_id, user.id))
        db.delete(record)
        project = db.get(Project, project_id)
        if existing is None or existing.role_key != record.role_key or project is None:
            continue  # gone already, or changed by hand: not the mapping's any more
        try:
            with db.begin_nested():
                projects_service.upsert_members(db, actor, project, [], [user.id],
                                                check_self=False)
        except AppError as err:
            logger.warning("sso %s: removing project %s for %s refused: %s",
                           row.id, project_id, user.id, err.code)
            continue
        changes["projects"].append(f"-{project_id}")


def _apply_groups(
    db: Session, row: ConsoleIdentityProvider, user: User, wanted: dict[str, str],
    actor: SsoActor, changes: dict[str, Any],
) -> None:
    from app.services.audit import record_structure_event

    records = _records(db, row.id, user.id, MEMBERSHIP_GROUP)

    def journal(action: str, target: str) -> None:
        record_structure_event(db=db, actor=actor.username, action=action, target=target,
                               actor_user_id=None)

    for group_id, role_key in sorted(wanted.items()):
        group = db.get(ProjectGroup, group_id)
        if group is None or group.archived_at is not None:
            continue
        existing = db.get(GroupMember, (group_id, user.id))
        record = records.get(group_id)
        if existing is not None and (record is None or existing.role_key != record.role_key):
            if record is not None:
                db.delete(record)
            continue
        if existing is None:
            db.add(GroupMember(group_id=group_id, user_id=user.id, role_key=role_key,
                               added_by=None))
            journal("group.members.add",
                    f"group:{group_id} user:{user.username} role:{role_key}")
        elif existing.role_key != role_key:
            existing.role_key = role_key
            journal("group.members.update",
                    f"group:{group_id} user:{user.username} role:{role_key}")
        else:
            continue
        if record is None:
            db.add(SsoMembership(idp_id=row.id, user_id=user.id, kind=MEMBERSHIP_GROUP,
                                 target_id=group_id, role_key=role_key))
        else:
            record.role_key = role_key
        changes["groups"].append(f"+{group_id}:{role_key}")
    for group_id, record in sorted(records.items()):
        if group_id in wanted:
            continue
        existing = db.get(GroupMember, (group_id, user.id))
        db.delete(record)
        if existing is None or existing.role_key != record.role_key:
            continue
        db.delete(existing)
        journal("group.members.remove", f"group:{group_id} user:{user.username}")
        changes["groups"].append(f"-{group_id}")
    db.flush()


def account_sso(db: Session, user: User) -> dict[str, Any] | None:
    """The user page's SSO facts: the provider, the subject and a role conflict."""
    if not users_service.is_sso_account(user):
        return None
    row = db.get(ConsoleIdentityProvider, user.external_idp_id)
    record = _records(db, user.external_idp_id or "", user.id, MEMBERSHIP_ROLE).get("")
    last = users_service.as_utc(user.last_login_at)
    return {
        "provider_id": user.external_idp_id,
        "provider_name": row.name if row is not None else None,
        "subject": user.external_subject,
        "last_login_at": last.isoformat() if last else None,
        "role_conflict": record.conflict if record is not None else None,
    }
