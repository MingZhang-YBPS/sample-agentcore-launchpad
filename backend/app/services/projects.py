"""Project administration (design §2–§3, §6.3, §7.8, §7.10, §11.5).

The write side of the access model that ``services.access`` reads: projects, their
workspace bindings, members, custom roles, and moving an agent between projects.

Every write that changes who reaches what — members, roles, bindings, status, the
project's group — bumps ``access_epoch`` in the SAME transaction and journals a
structure event (``audit.record_structure_event``), so the next request in every
process sees it and the journal cannot disagree with the ledger. Commits are the
caller's (the router), like ``services/users.py``.

Ids of projects the caller cannot see answer 404 ``project.not_found`` (design §5.4),
never 403: a project's existence can itself be sensitive.

A department administrator (§11.1) manages the structure of their department's
projects without being a member: they see those projects (not their data), create
them in the workspaces opened to the department, rename / regroup / archive them and
manage their members and roles. ``core/route_policy.py`` lets them reach the routes;
the checks that need the body (department, workspaces, settings) are here.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import _ProjectColumn, project_context
from app.models.ledger import Agent, ApiKey, Job, User, Workspace
from app.models.projects import (
    PROJECT_ID_PATTERN,
    Department,
    GroupMember,
    Project,
    ProjectGroup,
    ProjectMember,
    ProjectRole,
    ProjectWorkspace,
)
from app.schemas.inbound_auth import InboundAuth
from app.services import access
from app.services import api_keys as api_keys_service
from app.services import share_links as share_links_service
from app.services.audit import record_audit_event, record_structure_event
from app.services.users import ROLE_ADMIN, STATUS_ACTIVE

STATUS_ACTIVE_PROJECT = "active"
STATUS_ARCHIVED = "archived"
_PROJECT_ID = re.compile(PROJECT_ID_PATTERN)
ROLE_KEY_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"
_ROLE_KEY = re.compile(ROLE_KEY_PATTERN)
# project settings (design §6.3): an owner (`project.members`) may change these…
OWNER_SETTINGS = frozenset({"resource_visibility"})
# …an administrator also these (sod_strict loosened by the owner would defeat it;
# the /v1 project window and the project's own inbound JWT default, design §10.7, are
# infrastructure decisions like the workspace default they stand in for)
ADMIN_SETTINGS = OWNER_SETTINGS | {"sod_strict", "quotas", "inbound_auth_default"}
# …and a department administrator (§11.1) the owner's plus strictness — the /v1
# window and the inbound JWT default stay platform infrastructure
DEPARTMENT_SETTINGS = OWNER_SETTINGS | {"sod_strict"}
# `settings.quotas` (§11.3): whole-number caps and their bounds…
QUOTA_COUNTS: dict[str, tuple[int, int]] = {
    "max_agents": (0, 10_000),
    "max_api_keys": (0, 10_000),
    # the account's own 5 active batch evaluations still apply (evaluation/queue.py)
    "max_concurrent_eval_runs": (1, 100),
    api_keys_service.PROJECT_RATE_KEY: (1, api_keys_service.MAX_RATE_PER_MINUTE),
}
# …and dollar amounts (≥ 0, a run's estimate and the month's spend compared against them)
QUOTA_USD = frozenset({"eval_run_max_usd", "eval_monthly_usd_cap", "monthly_cost_alert_usd"})
MAX_QUOTA_USD = 10_000_000
QUOTA_KEYS = frozenset(QUOTA_COUNTS) | QUOTA_USD
_VISIBILITY = ("shared", "isolated")
CANDIDATE_LIMIT = 20
TRANSFER_KINDS = ("agent",)
# combined duties (design §3.6): one person holding both sides of a pair in one project
# is flagged in the member views, and refused where the project sets `sod_strict`. The
# object-level checks (no approving / signing your own request) stay the backstop.
SOD_PAIRS: tuple[tuple[str, str], ...] = (
    ("agents.deploy", "release.sign"),
    ("criteria.manage", "criteria.sign"),
)


def _now() -> datetime:
    return datetime.now(UTC)


def not_found(project_id: str) -> NotFoundError:
    return NotFoundError("project.not_found", f"project '{project_id}' not found",
                         {"project_id": project_id})


def _actor(identity: Any) -> dict[str, Any]:
    return {"actor": identity.username, "actor_user_id": getattr(identity, "user_id", None)}


def _journal(db: Session, identity: Any, action: str, target: str) -> None:
    record_structure_event(db=db, action=action, target=target, **_actor(identity))


def mask_email(email: str | None) -> str:
    """`z***@corp.com` — enough to tell two candidates apart, not to harvest."""
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


# ── lookup ────────────────────────────────────────────────────────────────────


def get_project(db: Session, project_id: str) -> Project:
    row = db.get(Project, project_id)
    if row is None:
        raise not_found(project_id)
    return row


def visible_project(db: Session, identity: Any, project_id: str) -> Project:
    """The project, when the caller may see it: an administrator always, a department
    administrator their department's (§11.1), anyone else only as a member reaching
    it in some workspace (else 404)."""
    if not access.has_data_bypass(identity):
        snapshot = access.snapshot_for(db, identity)
        if project_id not in snapshot.memberships:
            row = db.get(Project, project_id)
            if row is None or not access.can_manage_structure(
                    identity, row.department_id, db=db):
                raise not_found(project_id)
            return row
    return get_project(db, project_id)


def can_manage(db: Session, identity: Any, row: Project) -> bool:
    """May manage this project's structure: an administrator, or an administrator of
    the project's department (§11.1)."""
    return access.can_manage_structure(identity, row.department_id, db=db)


def caller_permissions(db: Session, identity: Any, project_id: str) -> list[str]:
    if access.has_data_bypass(identity):
        return sorted(access.ALL_PROJECT_KEYS)
    membership = access.snapshot_for(db, identity).memberships.get(project_id)
    return sorted(membership.permissions) if membership else []


def _bindings(db: Session, project_id: str) -> list[ProjectWorkspace]:
    return list(db.scalars(
        select(ProjectWorkspace).where(ProjectWorkspace.project_id == project_id)
        .order_by(ProjectWorkspace.workspace_id)
    ))


def bindings_view(db: Session, project_id: str) -> list[dict[str, Any]]:
    names = {w.id: w for w in db.scalars(select(Workspace))}
    out = []
    for b in _bindings(db, project_id):
        ws = names.get(b.workspace_id)
        out.append({
            "workspace_id": b.workspace_id,
            "name": ws.name if ws else b.workspace_id,
            "region": ws.region if ws else None,
            "tier": getattr(ws, "tier", None) if ws else None,
            "is_workspace_default": bool(b.is_workspace_default),
            "enabled_by": b.enabled_by,
            "enabled_at": b.enabled_at.isoformat() if b.enabled_at else None,
        })
    return out


def _counts(db: Session, project_ids: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    if not project_ids:
        return {}, {}
    members = dict(db.execute(
        select(ProjectMember.project_id, func.count())
        .where(ProjectMember.project_id.in_(project_ids)).group_by(ProjectMember.project_id)
    ).all())
    with project_context(None):
        agents = dict(db.execute(
            select(Agent.project_id, func.count())
            .where(Agent.project_id.in_(project_ids), Agent.status != "deleted")
            .group_by(Agent.project_id)
        ).all())
    return members, agents


def project_view(
    db: Session, row: Project, *, members: int | None = None, agents: int | None = None,
    workspaces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if members is None or agents is None:
        m, a = _counts(db, [row.id])
        members, agents = m.get(row.id, 0), a.get(row.id, 0)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "department_id": row.department_id,
        "group_id": row.group_id,
        "status": row.status,
        "is_default": bool(row.is_default),
        "settings": dict(row.settings or {}),
        "created_by": row.created_by,
        "created_by_name": row.created_by_name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
        "workspaces": workspaces if workspaces is not None else bindings_view(db, row.id),
        "member_count": members,
        "agent_count": agents,
    }


def list_projects(
    db: Session, identity: Any, *, department_id: str | None = None,
    group_id: str | None = None, status: str | None = None,
    workspace_id: str | None = None, q: str | None = None,
) -> list[dict[str, Any]]:
    """Administrators: every project (filterable); anyone else: their own."""
    query = select(Project).order_by(Project.is_default.desc(), Project.name)
    if not access.has_data_bypass(identity):
        mine = sorted(access.snapshot_for(db, identity).memberships)
        managed = sorted(access.managed_departments(db, identity))
        # a department administrator also lists their department's projects (§11.1)
        query = query.where(or_(Project.id.in_(mine), Project.department_id.in_(managed))
                            if managed else Project.id.in_(mine))
    if department_id:
        query = query.where(Project.department_id == department_id)
    if group_id:
        query = query.where(Project.group_id == group_id)
    if status and status != "all":
        query = query.where(Project.status == status)
    elif not status:
        query = query.where(Project.status != "deleted")  # tombstones only when asked
    if workspace_id:
        query = query.where(Project.id.in_(
            select(ProjectWorkspace.project_id).where(ProjectWorkspace.workspace_id == workspace_id)
        ))
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.where(or_(func.lower(Project.id).like(like),
                                func.lower(Project.name).like(like)))
    rows = list(db.scalars(query))
    members, agents = _counts(db, [r.id for r in rows])
    return [
        project_view(db, r, members=members.get(r.id, 0), agents=agents.get(r.id, 0))
        for r in rows
    ]


# ── create / update / archive ─────────────────────────────────────────────────


def _check_org(db: Session, department_id: str | None, group_id: str | None) -> None:
    if department_id:
        department = db.get(Department, department_id)
        if department is None:
            raise NotFoundError("department.not_found",
                                f"department '{department_id}' not found",
                                {"department_id": department_id})
        if department.archived_at is not None:
            raise AppError("department.archived", f"department '{department_id}' is archived",
                           {"department_id": department_id}, status_code=409)
    if group_id:
        group = db.get(ProjectGroup, group_id)
        if group is None or group.archived_at is not None:
            raise NotFoundError("group.not_found", f"project group '{group_id}' not found",
                                {"group_id": group_id})


def _check_settings(settings: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    unknown = sorted(set(settings) - allowed)
    if unknown:
        raise AppError("project.setting_forbidden",
                       f"these project settings cannot be changed here: {unknown}",
                       {"keys": unknown}, status_code=403 if set(unknown) <= ADMIN_SETTINGS
                       else 400)
    out: dict[str, Any] = {}
    for key, value in settings.items():
        if key == "resource_visibility":
            if value is not None and value not in _VISIBILITY:
                raise AppError("project.setting_invalid",
                               f"resource_visibility must be one of {list(_VISIBILITY)}",
                               {"key": key})
        elif key == "sod_strict" and not isinstance(value, bool):
            raise AppError("project.setting_invalid", "sod_strict must be true or false",
                           {"key": key})
        elif key == "quotas" and value is not None:
            value = _check_quotas(value)
        elif key == "inbound_auth_default" and value is not None:
            value = _check_inbound_auth(value)
        out[key] = value
    return out


def _check_quotas(value: Any) -> dict[str, Any]:
    """`settings.quotas`: a partial object; a None entry clears that quota."""
    if not isinstance(value, dict):
        raise AppError("project.setting_invalid", "quotas must be an object", {"key": "quotas"})
    unknown = sorted(set(value) - QUOTA_KEYS)
    if unknown:
        raise AppError("project.setting_invalid", f"unknown quotas: {unknown}",
                       {"key": "quotas", "quotas": unknown})
    out: dict[str, Any] = {}
    for key, item in value.items():
        if item is not None:
            item = check_quota_value(key, item)
        out[key] = item
    return out


def check_quota_value(key: str, value: Any) -> int | float:
    """One quota's value, normalized (a whole-number dollar amount stays an int)."""
    if key in QUOTA_COUNTS:
        low, high = QUOTA_COUNTS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise AppError("project.setting_invalid",
                           f"quotas.{key} must be a whole number from {low} to {high}",
                           {"key": "quotas", "quota": key})
        return value
    if (isinstance(value, bool) or not isinstance(value, int | float)
            or not 0 <= value <= MAX_QUOTA_USD or value != value):
        raise AppError("project.setting_invalid",
                       f"quotas.{key} must be an amount from 0 to {MAX_QUOTA_USD} USD",
                       {"key": "quotas", "quota": key})
    return value


def _check_inbound_auth(value: Any) -> dict[str, Any]:
    """`settings.inbound_auth_default`: the workspace default's shape (`InboundAuth`),
    stored normalized. Shape only: the discovery document is probed where an agent's
    own inbound auth is saved, and a redeploy is what applies it (§10.7)."""
    try:
        return InboundAuth.model_validate(value).model_dump()
    except ValidationError as exc:
        reason = "; ".join(str(err.get("msg") or "") for err in exc.errors()[:3])
        raise AppError("project.setting_invalid",
                       f"inbound_auth_default is not a valid inbound auth config: {reason}",
                       {"key": "inbound_auth_default"}) from exc


def _merge_settings(current: dict[str, Any], clean: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge, None removing a key — except `quotas`, merged one level deeper
    so setting one quota keeps the others (an emptied `quotas` is removed)."""
    merged = {**current, **clean}
    if isinstance(clean.get("quotas"), dict):
        quotas = {**(current.get("quotas") or {}), **clean["quotas"]}
        merged["quotas"] = {k: v for k, v in quotas.items() if v is not None} or None
    return {k: v for k, v in merged.items() if v is not None}


def _workspace(db: Session, workspace_id: str) -> Workspace:
    row = db.get(Workspace, workspace_id)
    if row is None:
        raise NotFoundError("workspace.not_found", f"workspace '{workspace_id}' does not exist",
                            {"workspace_id": workspace_id})
    return row


def _check_department_scope(
    db: Session, identity: Any, department_id: str | None, group_id: str | None,
) -> None:
    """A department administrator's project stays in a department they administer,
    and its group (if any) is one of that department's (§11.1)."""
    if not access.can_manage_structure(identity, department_id, db=db):
        raise AppError("department.not_managed",
                       "a department administrator works inside their own department — "
                       "name one you administer",
                       {"department_id": department_id}, status_code=403)
    if group_id:
        group = db.get(ProjectGroup, group_id)
        if group is None or group.department_id != department_id:
            raise NotFoundError("group.not_found", f"project group '{group_id}' not found",
                                {"group_id": group_id})


def _check_open_workspaces(db: Session, department_id: str | None, workspaces: list[str]) -> None:
    """§11.1: a department administrator binds only to the workspaces an administrator
    opened to the department."""
    closed = sorted(set(workspaces) - access.open_workspaces(db, department_id))
    if closed:
        raise AppError("department.workspace_not_open",
                       f"workspaces {closed} are not open to department '{department_id}'",
                       {"department_id": department_id, "workspaces": closed}, status_code=403)


def _check_project_limit(db: Session, department_id: str | None) -> None:
    """The department's `settings.max_projects` (§11.1), counting every project that
    is not a tombstone."""
    from app.services.project_groups import department_settings

    department = db.get(Department, department_id) if department_id else None
    limit = department_settings(department)["max_projects"] if department else None
    if limit is None:
        return
    count = db.execute(select(func.count()).select_from(Project).where(
        Project.department_id == department_id, Project.status != "deleted",
    )).scalar_one()
    if count >= int(limit):
        raise AppError("department.project_limit",
                       f"department '{department_id}' already has {count} of its "
                       f"{limit} projects", {"department_id": department_id, "limit": limit},
                       status_code=409)


def create_project(
    db: Session, identity: Any, *, project_id: str, name: str, description: str = "",
    department_id: str | None = None, group_id: str | None = None,
    workspaces: list[str], owners: list[str] | None = None,
    settings: dict[str, Any] | None = None,
) -> Project:
    """A new project bound to `workspaces`, with `owners` as its project owners."""
    if not _PROJECT_ID.match(project_id):
        raise AppError("project.invalid_id",
                       "project id must be 2-32 lowercase letters, digits or '-'",
                       {"pattern": PROJECT_ID_PATTERN})
    if db.get(Project, project_id) is not None:
        raise AppError("project.exists", f"project '{project_id}' already exists",
                       {"project_id": project_id}, status_code=409)
    if not workspaces:
        raise AppError("project.workspaces_required", "bind the project to at least one workspace")
    _check_org(db, department_id, group_id)
    platform = access.can_manage_structure(identity)
    if not platform:
        _check_department_scope(db, identity, department_id or None, group_id or None)
        _check_open_workspaces(db, department_id, workspaces)
        _check_project_limit(db, department_id)
    clean = _check_settings(settings or {}, ADMIN_SETTINGS if platform else DEPARTMENT_SETTINGS)
    for ws in workspaces:
        _workspace(db, ws)
    row = Project(
        id=project_id, name=name.strip(), description=description or "",
        department_id=department_id or None, group_id=group_id or None,
        status=STATUS_ACTIVE_PROJECT, is_default=False,
        settings=_merge_settings({}, clean) or None,
        created_by=getattr(identity, "user_id", None), created_by_name=identity.username,
    )
    db.add(row)
    for ws in sorted(set(workspaces)):
        db.add(ProjectWorkspace(project_id=project_id, workspace_id=ws,
                                is_workspace_default=False,
                                enabled_by=getattr(identity, "user_id", None)))
    db.flush()
    _journal(db, identity, "project.create", f"project:{project_id}")
    for ws in sorted(set(workspaces)):
        _journal(db, identity, "project.workspaces.bind", f"project:{project_id} workspace:{ws}")
    if owners:
        upsert_members(db, identity, row,
                       [{"user_id": uid, "role_key": "project_owner"} for uid in owners], [],
                       check_self=False)
    access.bump_epoch(db)
    return row


def update_project(
    db: Session, identity: Any, row: Project, fields: dict[str, Any],
) -> Project:
    """Name / department / group are structure (administrators, and the department's
    administrators inside their department); description and the owner-editable
    settings are the project's own (`project.members`)."""
    structure = access.can_manage_structure(identity)
    department = not structure and can_manage(db, identity, row)
    admin_fields = {"name", "department_id", "group_id"} & set(fields)
    if admin_fields and not (structure or department):
        raise AppError("auth.forbidden",
                       f"only an administrator may change {sorted(admin_fields)}",
                       {"fields": sorted(admin_fields)}, status_code=403)
    if "department_id" in fields or "group_id" in fields:
        _check_org(db, fields.get("department_id"), fields.get("group_id"))
        if department:
            _check_department_scope(
                db, identity, fields.get("department_id", row.department_id) or None,
                fields.get("group_id") or None,  # a group named now: the department's
            )
    changed: list[str] = []
    for key in ("name", "description", "department_id"):
        if key in fields and getattr(row, key) != fields[key]:
            value = fields[key]
            setattr(row, key, (value.strip() if key == "name" else value) or
                    ("" if key == "description" else None))
            changed.append(key)
    was_strict = sod_strict(row)
    group_moved = "group_id" in fields and (fields["group_id"] or None) != row.group_id
    if group_moved:
        row.group_id = fields["group_id"] or None
        changed.append("group_id")
    if "settings" in fields and fields["settings"] is not None:
        clean = _check_settings(fields["settings"], ADMIN_SETTINGS if structure
                                else DEPARTMENT_SETTINGS if department else OWNER_SETTINGS)
        merged = _merge_settings(row.settings or {}, clean)
        if merged != (row.settings or {}):
            row.settings = merged or None
            changed.append("settings")
    if not changed:
        return row
    row.updated_at = _now()
    if group_moved or (sod_strict(row) and not was_strict):
        # a new group's roles, or strictness switched on, must not leave a combined duty
        db.flush()
        enforce_sod(db, row)
    if group_moved:
        # the old group's members lose the project, the new group's gain it
        access.bump_epoch(db)
        api_keys_service.disable_offboarded_keys_in_project(db, row.id)
    _journal(db, identity, "project.update", f"project:{row.id} {','.join(changed)}")
    return row


def archive_project(db: Session, identity: Any, row: Project) -> dict[str, int]:
    """Read-only from now on (design §11.5): links revoked, keys disabled."""
    if row.is_default:
        raise AppError("project.default_locked", "the default project cannot be archived",
                       {"project_id": row.id}, status_code=409)
    if row.status == STATUS_ARCHIVED:
        return {"links_revoked": 0, "keys_disabled": 0}
    _refuse_past_archived(row)
    row.status = STATUS_ARCHIVED
    row.archived_at = row.updated_at = _now()
    db.flush()
    links = share_links_service.revoke_project_links(db, row.id)
    keys = api_keys_service.disable_offboarded_keys_in_project(db, row.id)
    access.bump_epoch(db)
    _journal(db, identity, "project.archive", f"project:{row.id}")
    return {"links_revoked": links, "keys_disabled": keys}


def unarchive_project(db: Session, identity: Any, row: Project) -> None:
    """Writable again. Revoked links and disabled keys stay so (design §11.5)."""
    if row.status == STATUS_ACTIVE_PROJECT:
        return
    _refuse_past_archived(row)
    if _deletion(row):
        raise AppError("project.deletion_pending",
                       f"project '{row.id}' is scheduled for deletion — cancel that first",
                       {"project_id": row.id}, status_code=409)
    row.status = STATUS_ACTIVE_PROJECT
    row.archived_at = None
    row.updated_at = _now()
    access.bump_epoch(db)
    _journal(db, identity, "project.unarchive", f"project:{row.id}")


# ── deletion (design §11.5): request → cooling-off → purge → tombstone ────────


def _deletion(row: Project) -> dict[str, Any] | None:
    from app.services import project_purge

    return project_purge.lifecycle(row).get("deletion") or None


def _refuse_past_archived(row: Project) -> None:
    """`deleting` / `deleted` accept no lifecycle change but the purge's own."""
    from app.services import project_purge

    if row.status == project_purge.STATUS_DELETED:
        raise AppError("project.deleted", f"project '{row.id}' is deleted",
                       {"project_id": row.id}, status_code=409)
    if row.status == project_purge.STATUS_DELETING:
        raise AppError("project.deleting", f"project '{row.id}' is being deleted",
                       {"project_id": row.id}, status_code=409)


def lifecycle_view(db: Session, row: Project, *, now: datetime | None = None) -> dict[str, Any]:
    """The project's place in the lifecycle: the deletion request and its cooling-off,
    the export bundles, the latest purge job."""
    from app.services import project_purge

    now = now or _now()
    state = project_purge.lifecycle(row)
    deletion = state.get("deletion")
    if deletion:
        purge_after = datetime.fromisoformat(deletion["purge_after"])
        deletion = {**deletion, "purge_ready": now >= purge_after,
                    "seconds_left": max(0, int((purge_after - now).total_seconds()))}
    return {
        "project_id": row.id,
        "status": row.status,
        "is_default": bool(row.is_default),
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
        "cooling_off_days": project_purge.cooling_off_days(db),
        "deletion": deletion,
        "deleted": state.get("deleted"),
        "exports": list(state.get("exports") or []),
        "purge_job": project_purge.job_view(project_purge.latest_job(db, row.id)),
    }


def request_deletion(db: Session, identity: Any, row: Project, *,
                     now: datetime | None = None) -> dict[str, Any]:
    """Ask for an archived project's deletion: writes the export bundle, then starts the
    cooling-off (`project_deletion_cooling_off_days`, default 7) — cancellable until the
    purge starts. The default project and a workspace's default project cannot go."""
    from app.services import project_purge

    now = now or _now()
    if row.is_default:
        raise AppError("project.default_locked", "the default project cannot be deleted",
                       {"project_id": row.id}, status_code=409)
    _refuse_past_archived(row)
    if row.status != STATUS_ARCHIVED:
        raise AppError("project.not_archived",
                       f"archive project '{row.id}' before requesting its deletion",
                       {"project_id": row.id}, status_code=409)
    if _deletion(row):
        raise AppError("project.deletion_pending",
                       f"the deletion of project '{row.id}' is already requested",
                       {"project_id": row.id}, status_code=409)
    defaults = sorted(b.workspace_id for b in _bindings(db, row.id) if b.is_workspace_default)
    if defaults:
        raise AppError("project.workspace_default",
                       f"project '{row.id}' is the default project of {defaults} — "
                       "make another project the default first",
                       {"project_id": row.id, "workspaces": defaults}, status_code=409)
    export = project_purge.build_export(db, identity, row)
    days = project_purge.cooling_off_days(db)
    state = project_purge.lifecycle(row)
    state["deletion"] = {
        "requested_at": now.isoformat(),
        "requested_by": identity.username,
        "requested_by_user_id": getattr(identity, "user_id", None),
        "cooling_off_days": days,
        "purge_after": (now + timedelta(days=days)).isoformat(),
        "export_id": export["id"],
        "job_id": None,
    }
    project_purge.set_lifecycle(row, state)
    row.updated_at = now
    project_purge.journal(db, identity, "project.delete_request", row.id)
    return lifecycle_view(db, row, now=now)


def cancel_deletion(db: Session, identity: Any, row: Project) -> dict[str, Any]:
    """Withdraw a deletion request during its cooling-off. A failed purge leaves the
    project archived with its request kept, so that can be withdrawn too (the agents
    the failed job already deleted stay deleted)."""
    from app.services import project_purge

    _refuse_past_archived(row)
    if not _deletion(row):
        raise AppError("project.deletion_not_requested",
                       f"no deletion of project '{row.id}' is requested",
                       {"project_id": row.id}, status_code=409)
    state = project_purge.lifecycle(row)
    state.pop("deletion", None)
    project_purge.set_lifecycle(row, state)
    row.updated_at = _now()
    project_purge.journal(db, identity, "project.delete_cancel", row.id)
    return lifecycle_view(db, row)


# ── workspace bindings ────────────────────────────────────────────────────────


def _project_tables() -> list[Any]:
    """Every project-carrying table (owned or shared) — what an unbind and a transfer
    have to consider. Read from the mappers so a new table is covered by itself."""
    return [
        m.class_ for m in Base.registry.mappers
        if issubclass(m.class_, _ProjectColumn)
    ]


def rows_in_workspace(db: Session, project_id: str, workspace_id: str) -> dict[str, int]:
    """How many of the project's rows each table holds in one workspace."""
    out: dict[str, int] = {}
    with project_context(None):
        for model in _project_tables():
            table = model.__table__
            if "workspace_id" not in table.c:
                continue
            count = db.execute(
                select(func.count()).select_from(table).where(
                    table.c.project_id == project_id, table.c.workspace_id == workspace_id,
                )
            ).scalar_one()
            if count:
                out[table.name] = int(count)
    return out


def update_bindings(
    db: Session, identity: Any, row: Project, *, bind: list[str], unbind: list[str],
    make_default: list[str],
) -> list[str]:
    """Bind / unbind / make the project its workspaces' default. Returns the workspace
    ids newly bound (the router seeds their Skill Lab samples after commit).

    An unbind is refused while the workspace still holds any of the project's rows
    (agents, runs, keys…) — the same protection a workspace detach has — and for the
    workspace-default binding (another project must become the default first)."""
    _refuse_past_archived(row)
    if not access.can_manage_structure(identity):
        # a department administrator (§11.1): only the department's open workspaces,
        # and never a workspace's default project — that is the whole workspace's
        new = [ws for ws in bind if ws not in {b.workspace_id for b in _bindings(db, row.id)}]
        _check_open_workspaces(db, row.department_id, new)
        if make_default:
            raise AppError("auth.forbidden", "only an administrator may change a workspace's "
                           "default project", {"workspaces": sorted(set(make_default))},
                           status_code=403)
    if set(bind) & set(unbind):
        raise AppError("project.binding_conflict", "a workspace cannot be bound and unbound "
                       "in one request", {"workspaces": sorted(set(bind) & set(unbind))})
    current = {b.workspace_id: b for b in _bindings(db, row.id)}
    added: list[str] = []
    for ws in sorted(set(bind)):
        _workspace(db, ws)
        if ws in current:
            continue
        binding = ProjectWorkspace(project_id=row.id, workspace_id=ws,
                                   is_workspace_default=False,
                                   enabled_by=getattr(identity, "user_id", None))
        db.add(binding)
        current[ws] = binding
        added.append(ws)
        _journal(db, identity, "project.workspaces.bind", f"project:{row.id} workspace:{ws}")
    removed: list[str] = []
    for ws in sorted(set(unbind)):
        binding = current.get(ws)
        if binding is None:
            continue
        if row.is_default:
            raise AppError("project.default_locked",
                           "the default project cannot be unbound from its workspaces",
                           {"project_id": row.id, "workspace_id": ws}, status_code=409)
        if binding.is_workspace_default:
            raise AppError("project.workspace_default",
                           f"project '{row.id}' is workspace '{ws}''s default project — "
                           "make another project the default first",
                           {"project_id": row.id, "workspace_id": ws}, status_code=409)
        held = rows_in_workspace(db, row.id, ws)
        if held:
            raise AppError("project.workspace_in_use",
                           f"workspace '{ws}' still holds rows of project '{row.id}' — "
                           "move or delete them first",
                           {"project_id": row.id, "workspace_id": ws, "rows": held},
                           status_code=409)
        db.delete(binding)
        current.pop(ws)
        removed.append(ws)
        _journal(db, identity, "project.workspaces.unbind", f"project:{row.id} workspace:{ws}")
    db.flush()
    for ws in sorted(set(make_default)):
        if ws not in current:
            raise AppError("project.not_bound",
                           f"project '{row.id}' is not bound to workspace '{ws}'",
                           {"project_id": row.id, "workspace_id": ws}, status_code=409)
        if row.status != STATUS_ACTIVE_PROJECT:
            raise AppError("project.archived", f"project '{row.id}' is archived",
                           status_code=409)
        if current[ws].is_workspace_default:
            continue
        db.execute(update(ProjectWorkspace).where(
            ProjectWorkspace.workspace_id == ws, ProjectWorkspace.is_workspace_default.is_(True),
        ).values(is_workspace_default=False))
        db.flush()  # the partial unique index allows one default per workspace
        current[ws].is_workspace_default = True
        db.flush()
        _journal(db, identity, "project.update", f"project:{row.id} workspace-default:{ws}")
    if added or removed or make_default:
        access.bump_epoch(db)
    if removed:
        api_keys_service.disable_offboarded_keys_in_project(db, row.id)
    return added


# ── members (design §3.4, §7.8) ───────────────────────────────────────────────


def custom_roles(db: Session, project_id: str) -> dict[str, ProjectRole]:
    return {r.key: r for r in db.scalars(
        select(ProjectRole).where(ProjectRole.project_id == project_id).order_by(ProjectRole.key)
    )}


def _check_role_key(db: Session, row: Project, role_key: str, *, group: bool = False) -> None:
    if role_key in access.ROLE_TEMPLATES:
        return
    if not group and role_key == access.INHERIT_ROLE and row.is_default:
        return  # the compatibility role exists in the default project only (§3.5)
    if not group and role_key in custom_roles(db, row.id):
        return
    raise AppError("project.role_unknown", f"'{role_key}' is not a role of project '{row.id}'",
                   {"role_key": role_key, "project_id": row.id})


def _check_overrides(overrides: dict[str, Any] | None) -> dict[str, bool] | None:
    if not overrides:
        return None
    unknown = sorted(set(overrides) - access.ALL_PROJECT_KEYS)
    if unknown:
        raise AppError("project.override_unknown", f"unknown permission keys: {unknown}",
                       {"keys": unknown})
    return {k: bool(v) for k, v in sorted(overrides.items())}


def _group_rows(db: Session, row: Project) -> dict[str, GroupMember]:
    if not row.group_id:
        return {}
    group = db.get(ProjectGroup, row.group_id)
    if group is None or group.archived_at is not None:
        return {}
    return {g.user_id: g for g in db.scalars(
        select(GroupMember).where(GroupMember.group_id == row.group_id)
    )}


def effective_keys(
    user: User | None, direct: ProjectMember | None, group: GroupMember | None,
    roles: dict[str, frozenset[str]],
) -> frozenset[str]:
    """§3.4 for one member: direct ∪ group, then the direct row's overrides."""
    role = user.role if user else "member"
    overrides = user.permissions if user else None
    base: frozenset[str] = frozenset()
    for role_key in (direct.role_key if direct else None, group.role_key if group else None):
        if role_key:
            base |= access.template_keys(role_key, roles, account_role=role,
                                         account_overrides=overrides)
    return access.apply_overrides(base, direct.overrides if direct else None)


def members_view(db: Session, row: Project, *, full_email: bool) -> list[dict[str, Any]]:
    """Direct and group members with their sources and effective keys."""
    direct = {m.user_id: m for m in db.scalars(
        select(ProjectMember).where(ProjectMember.project_id == row.id)
    )}
    groups = _group_rows(db, row)
    ids = sorted(set(direct) | set(groups))
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids)))} if ids else {}
    roles = {k: frozenset(r.permissions or []) & access.ALL_PROJECT_KEYS
             for k, r in custom_roles(db, row.id).items()}
    out = []
    for uid in ids:
        user, d, g = users.get(uid), direct.get(uid), groups.get(uid)
        sources = []
        if d:
            sources.append({"source": "direct", "role_key": d.role_key})
        if g:
            sources.append({"source": f"group:{g.group_id}", "role_key": g.role_key})
        keys = effective_keys(user, d, g, roles)
        out.append({
            "user_id": uid,
            "username": user.username if user else None,
            "email": (user.email if full_email else mask_email(user.email)) if user else None,
            "account_role": user.role if user else None,
            "status": user.status if user else None,
            "direct": {
                "role_key": d.role_key, "overrides": d.overrides or {},
                "added_by": d.added_by,
                "added_at": d.added_at.isoformat() if d.added_at else None,
            } if d else None,
            "group": {"group_id": g.group_id, "role_key": g.role_key} if g else None,
            "sources": sources,
            # an administrator holds every key anyway; the row is kept for a demotion
            "bypass": bool(user and user.role == ROLE_ADMIN),
            "permissions": sorted(keys),
            # the §3.6 "combined duties" hint (a refusal instead under sod_strict)
            "sod_conflicts": sod_conflicts(keys),
        })
    return out


# ── combined duties (design §3.6, D-22) ───────────────────────────────────────


def sod_conflicts(keys: frozenset[str] | set[str]) -> list[list[str]]:
    """The SoD pairs `keys` holds both sides of."""
    return [list(pair) for pair in SOD_PAIRS if set(pair) <= keys]


def sod_strict(row: Project) -> bool:
    return bool((row.settings or {}).get("sod_strict"))


def project_conflicts(
    db: Session, row: Project, user_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Members of `row` (all, or just `user_ids`) whose effective keys combine duties,
    read from the session — so a write path sees its own pending rows."""
    direct = {m.user_id: m for m in db.scalars(
        select(ProjectMember).where(ProjectMember.project_id == row.id)
    )}
    groups = _group_rows(db, row)
    ids = set(direct) | set(groups)
    if user_ids is not None:
        ids &= user_ids
    if not ids:
        return []
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(sorted(ids))))}
    roles = {k: frozenset(r.permissions or []) & access.ALL_PROJECT_KEYS
             for k, r in custom_roles(db, row.id).items()}
    out = []
    for uid in sorted(ids):
        user = users.get(uid)
        pairs = sod_conflicts(effective_keys(user, direct.get(uid), groups.get(uid), roles))
        if pairs:
            out.append({"user_id": uid, "username": user.username if user else None,
                        "pairs": pairs})
    return out


def enforce_sod(db: Session, row: Project, user_ids: set[str] | None = None) -> None:
    """Under `settings.sod_strict`, refuse a write that leaves someone holding both
    sides of an SoD pair in the project (409 `project.sod_conflict`). The caller
    has flushed its change and has not committed: raising rolls all of it back."""
    if not sod_strict(row):
        return
    conflicts = project_conflicts(db, row, user_ids)
    if conflicts:
        names = ", ".join(str(c["username"] or c["user_id"]) for c in conflicts)
        raise AppError(
            "project.sod_conflict",
            f"project '{row.id}' separates duties (sod_strict): {names} would hold both "
            "sides of " + "; ".join(" + ".join(p) for p in conflicts[0]["pairs"]),
            {"project_id": row.id, "conflicts": conflicts}, status_code=409,
        )


def _refuse_self(db: Session, identity: Any, row: Project, user_ids: set[str]) -> bool:
    """Design §3.6 / D-22: nobody but an administrator edits their own membership —
    an owner could otherwise hand themselves the sign / approve keys. A department
    administrator may (§11.1: that is how they get data access); True tells the caller
    to journal it as such."""
    me = getattr(identity, "user_id", None)
    if not me or me not in user_ids or access.can_manage_structure(identity):
        return False
    if can_manage(db, identity, row):
        return True
    raise AppError("project.self_membership", "you cannot change your own membership",
                   {"user_id": me}, status_code=403)


def upsert_members(
    db: Session, identity: Any, row: Project, upsert: list[dict[str, Any]],
    remove: list[str], *, check_self: bool = True,
) -> dict[str, list[str]]:
    """Add / update direct members and remove direct rows. Group-sourced access is
    managed on the group; removing someone with no direct row is a 404."""
    targets = {u["user_id"] for u in upsert} | set(remove)
    self_edit = _refuse_self(db, identity, row, targets) if check_self else False
    if {u["user_id"] for u in upsert} & set(remove):
        raise AppError("project.member_conflict", "a user cannot be upserted and removed "
                       "in one request")
    result: dict[str, list[str]] = {"added": [], "updated": [], "removed": []}
    for item in upsert:
        uid, role_key = item["user_id"], item["role_key"]
        user = db.get(User, uid)
        if user is None:
            raise NotFoundError("user.not_found", f"user '{uid}' not found", {"user_id": uid})
        _check_role_key(db, row, role_key)
        overrides = _check_overrides(item.get("overrides"))
        existing = db.get(ProjectMember, (row.id, uid))
        if existing is None:
            db.add(ProjectMember(project_id=row.id, user_id=uid, role_key=role_key,
                                 overrides=overrides,
                                 added_by=getattr(identity, "user_id", None)))
            result["added"].append(uid)
            _journal(db, identity, "project.members.add",
                     f"project:{row.id} user:{user.username} role:{role_key}")
        elif existing.role_key != role_key or (existing.overrides or None) != overrides:
            existing.role_key, existing.overrides = role_key, overrides
            result["updated"].append(uid)
            _journal(db, identity, "project.members.update",
                     f"project:{row.id} user:{user.username} role:{role_key}")
    for uid in remove:
        existing = db.get(ProjectMember, (row.id, uid))
        if existing is None:
            if uid in _group_rows(db, row):
                raise AppError("project.member_from_group",
                               "this access comes from the project's group — manage it there",
                               {"user_id": uid, "group_id": row.group_id}, status_code=409)
            raise NotFoundError("project.member_not_found",
                                f"user '{uid}' is not a direct member", {"user_id": uid})
        user = db.get(User, uid)
        db.delete(existing)
        result["removed"].append(uid)
        _journal(db, identity, "project.members.remove",
                 f"project:{row.id} user:{user.username if user else uid}")
    if self_edit and any(getattr(identity, "user_id", None) in ids for ids in result.values()):
        # a department administrator giving themselves data access: its own journal
        # line, so the project's owners can see it in the audit view (§11.1)
        role = next((u["role_key"] for u in upsert if u["user_id"] == identity.user_id), "-")
        _journal(db, identity, "department.admins.self_membership",
                 f"department:{row.department_id} project:{row.id} user:{identity.username}"
                 f" role:{role}")
    if any(result.values()):
        db.flush()
        enforce_sod(db, row, set(result["added"]) | set(result["updated"]))
        access.bump_epoch(db)
        # a removed (or narrowed) creator's keys stop with their membership (§5.7)
        api_keys_service.disable_offboarded_keys_in_project(db, row.id)
    return result


def candidates(db: Session, row: Project, q: str) -> list[dict[str, Any]]:
    """Active accounts matching `q` — username and masked email only (§7.8)."""
    needle = (q or "").strip().lower()
    query = select(User).where(User.status == STATUS_ACTIVE, User.role != ROLE_ADMIN)
    if needle:
        like = f"%{needle}%"
        query = query.where(or_(User.username_key.like(like), User.email.like(like)))
    users = list(db.scalars(query.order_by(User.username_key).limit(CANDIDATE_LIMIT)))
    members = set(db.scalars(
        select(ProjectMember.user_id).where(ProjectMember.project_id == row.id)
    ))
    return [
        {"user_id": u.id, "username": u.username, "email": mask_email(u.email),
         "is_member": u.id in members}
        for u in users
    ]


# ── roles (design §3.2, §3.3) ─────────────────────────────────────────────────


def role_templates_view() -> dict[str, Any]:
    return {
        "templates": [
            {"key": t.key, "permissions": sorted(t.keys)} for t in access.ROLE_TEMPLATES.values()
        ],
        "inherit": access.INHERIT_ROLE,
        "agent_keys": sorted(access.ALL_PROJECT_KEYS - set(access.PROJECT_PERMISSIONS)),
        "project_keys": list(access.PROJECT_PERMISSIONS),
    }


def roles_view(db: Session, row: Project) -> dict[str, Any]:
    used = dict(db.execute(
        select(ProjectMember.role_key, func.count()).where(ProjectMember.project_id == row.id)
        .group_by(ProjectMember.role_key)
    ).all())
    return {
        **role_templates_view(),
        "inherit_available": bool(row.is_default),
        "custom": [
            {"key": r.key, "name": r.name, "permissions": sorted(r.permissions or []),
             "based_on": r.based_on, "members": int(used.get(r.key, 0)),
             "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in custom_roles(db, row.id).values()
        ],
    }


def upsert_role(
    db: Session, identity: Any, row: Project, role_key: str, *, name: str,
    permissions: list[str], based_on: str | None = None,
) -> ProjectRole:
    if not _ROLE_KEY.match(role_key) or role_key in access.BUILTIN_ROLE_KEYS:
        raise AppError("project.role_invalid_key",
                       "a custom role key is 2-32 of a-z, 0-9, '_' and not a built-in role",
                       {"role_key": role_key, "pattern": ROLE_KEY_PATTERN})
    if based_on and based_on not in access.ROLE_TEMPLATES:
        raise AppError("project.role_unknown", f"'{based_on}' is not a built-in template",
                       {"role_key": based_on})
    unknown = sorted(set(permissions) - access.ALL_PROJECT_KEYS)
    if unknown:
        raise AppError("project.override_unknown", f"unknown permission keys: {unknown}",
                       {"keys": unknown})
    keys = sorted(set(permissions))
    existing = db.get(ProjectRole, (row.id, role_key))
    if existing is None:
        existing = ProjectRole(project_id=row.id, key=role_key, name=name.strip(),
                               permissions=keys, based_on=based_on,
                               created_by=getattr(identity, "user_id", None))
        db.add(existing)
    else:
        existing.name, existing.permissions = name.strip(), keys
        existing.based_on = based_on
        existing.updated_at = _now()
    db.flush()
    enforce_sod(db, row)  # everyone holding the role gets its new keys
    access.bump_epoch(db)
    api_keys_service.disable_offboarded_keys_in_project(db, row.id)
    _journal(db, identity, "project.roles.upsert", f"project:{row.id} role:{role_key}")
    return existing


def delete_role(db: Session, identity: Any, row: Project, role_key: str) -> None:
    existing = db.get(ProjectRole, (row.id, role_key))
    if existing is None:
        raise NotFoundError("project.role_not_found", f"role '{role_key}' not found",
                            {"role_key": role_key})
    held = db.execute(select(func.count()).select_from(ProjectMember).where(
        ProjectMember.project_id == row.id, ProjectMember.role_key == role_key,
    )).scalar_one()
    if held:
        raise AppError("project.role_in_use", f"{held} member(s) still hold role '{role_key}'",
                       {"role_key": role_key, "members": int(held)}, status_code=409)
    db.delete(existing)
    access.bump_epoch(db)
    _journal(db, identity, "project.roles.delete", f"project:{row.id} role:{role_key}")


# ── moving an agent to another project (design §7.10, §11.5) ──────────────────


def _row_key(table: Any) -> Any:
    """The row's single-column key (`id` on most tables, `agent_id` on per-agent ones)."""
    return table.c.id if "id" in table.c else next(iter(table.primary_key.columns))


def _derived_ids(db: Session, agent_id: str, source: str) -> dict[Any, set[str]]:
    """The agent and every row derived from it, transitively along each model's
    `__project_parent__` (eval run → criterion results, annotation task → labels and
    links…), restricted to the source project."""
    tables = [m for m in _project_tables() if m.__project_parent__ is not None]
    moved: dict[str, set[str]] = {"agents": {agent_id}}
    models: dict[str, Any] = {"agents": Agent}
    changed = True
    while changed:
        changed = False
        for model in tables:
            attr, parents = model.__project_parent__
            parent_ids = set().union(*(moved.get(p, set()) for p in parents))
            if not parent_ids:
                continue
            table = model.__table__
            found = set(db.execute(select(_row_key(table)).where(
                table.c[attr].in_(sorted(parent_ids)), table.c.project_id == source,
            )).scalars())
            fresh = found - moved.get(table.name, set())
            if fresh:
                moved.setdefault(table.name, set()).update(fresh)
                models[table.name] = model
                changed = True
    return {models[name]: ids for name, ids in moved.items()}


def transfer_agent(
    db: Session, identity: Any, source: Project, agent_id: str, to_project_id: str,
) -> dict[str, Any]:
    """Phase 1: ledger only, one transaction. Phase 2 re-tags and re-bounds after."""
    with project_context(None):
        agent = db.get(Agent, agent_id)
        if agent is None or agent.project_id != source.id or agent.status == "deleted":
            raise NotFoundError("agent.not_found", f"agent '{agent_id}' not found",
                                {"agent_id": agent_id})
        if agent.system_key:
            raise AppError("project.transfer_platform",
                           "a system preset belongs to the platform, not a project",
                           {"agent_id": agent_id}, status_code=409)
        if to_project_id == source.id:
            raise AppError("project.transfer_same", "the agent is already in that project")
        target = db.get(Project, to_project_id)
        if target is None:
            raise not_found(to_project_id)
        if target.status != STATUS_ACTIVE_PROJECT or source.status != STATUS_ACTIVE_PROJECT:
            raise AppError("project.archived", "both projects must be active",
                           status_code=409)
        if db.get(ProjectWorkspace, (to_project_id, agent.workspace_id)) is None:
            raise AppError("project.not_bound",
                           f"project '{to_project_id}' is not bound to workspace "
                           f"'{agent.workspace_id}'",
                           {"project_id": to_project_id, "workspace_id": agent.workspace_id},
                           status_code=409)
        jobs = [j for j in db.scalars(select(Job).where(
            Job.workspace_id == agent.workspace_id, Job.project_id == source.id,
        )) if (j.payload or {}).get("agent_id") == agent_id]
        if any(j.status in ("queued", "running") for j in jobs):
            raise AppError("project.transfer_busy", "a job of this agent is still running",
                           {"agent_id": agent_id}, status_code=409)
        from app.optimization.models import RuntimeCanary

        linked = db.scalars(select(RuntimeCanary.id).where(
            RuntimeCanary.status == "running",
            or_(RuntimeCanary.champion_agent_id == agent_id,
                RuntimeCanary.challenger_agent_id == agent_id),
            RuntimeCanary.champion_agent_id != RuntimeCanary.challenger_agent_id,
        )).first()
        if linked is not None:
            raise AppError("project.transfer_linked",
                           "a running canary pairs this agent with another one",
                           {"canary_id": linked}, status_code=409)
        moved = _derived_ids(db, agent_id, source.id)
        if jobs:
            moved[Job] = {j.id for j in jobs}
        counts: dict[str, int] = {}
        for model, ids in moved.items():
            table = model.__table__
            result = db.execute(update(table).where(
                _row_key(table).in_(sorted(ids)), table.c.project_id == source.id,
            ).values(project_id=to_project_id))
            counts[table.name] = int(result.rowcount or 0)
        # keys of the source project scoped to the agent lose it; a key left with no
        # agent would mean "every agent of its project" — disable it instead
        for key in db.scalars(select(ApiKey).where(ApiKey.project_id == source.id)):
            if key.agent_ids and agent_id in key.agent_ids:
                key.agent_ids = [a for a in key.agent_ids if a != agent_id]
                if not key.agent_ids:
                    key.enabled = False
        db.flush()
        db.expire_all()  # the Core updates bypassed the identity map
        record_audit_event(
            workspace_id=agent.workspace_id or "default", actor=identity.username,
            actor_user_id=getattr(identity, "user_id", None), action="project.transfer",
            target=f"agent:{agent_id} project:{source.id}->project:{to_project_id}",
            db=db, project_id=to_project_id,
        )
    return {"agent_id": agent_id, "from_project_id": source.id,
            "to_project_id": to_project_id, "rows": counts}


# ── the caller's access (GET /api/me/access) ──────────────────────────────────


def _project_entry(
    *, project_id: str, name: str, status: str, is_default: bool, workspace_default: bool,
    permissions: frozenset[str], sources: tuple[tuple[str, str], ...], bypass: bool,
) -> dict[str, Any]:
    return {
        "id": project_id, "name": name, "status": status, "is_default": is_default,
        "is_workspace_default": workspace_default,
        "permissions": sorted(permissions),
        "role_sources": [{"source": s, "role_key": r} for s, r in sources],
        "bypass": bypass,
    }


def access_view(db: Session, identity: Any) -> dict[str, Any]:
    """Reachable workspaces → projects → effective keys and their sources: the data
    source of the project switcher and of the console's `can()` (design §2.7)."""
    from app.services.workspace import granted_workspace_ids

    bypass = access.has_data_bypass(identity)
    snapshot = access.snapshot_for(db, identity)
    workspaces = {w.id: w for w in db.scalars(select(Workspace).order_by(Workspace.id))}
    if bypass:
        reach = sorted(workspaces)
    else:
        reach = sorted(
            set(granted_workspace_ids(db, identity.user_id) if identity.user_id else [])
            | {ws for m in snapshot.memberships.values() for ws in m.workspaces}
        )
    departments = {
        pid: (did, dname) for pid, did, dname in db.execute(text(
            "SELECT p.id, d.id, d.name FROM projects p"
            " JOIN departments d ON d.id = p.department_id"
        )).fetchall()
    }
    out = []
    for ws in reach:
        projects: list[dict[str, Any]] = []
        default_id: str | None = None
        if bypass:
            for b in access.workspace_bindings(db, ws):
                member = snapshot.memberships.get(b.project_id)
                projects.append(_project_entry(
                    project_id=b.project_id, name=b.name, status=b.status,
                    is_default=b.is_default, workspace_default=b.is_workspace_default,
                    permissions=access.ALL_PROJECT_KEYS,
                    sources=member.role_sources if member else (), bypass=member is None,
                ))
                if b.is_workspace_default:
                    default_id = b.project_id
            if default_id is None and projects:
                default_id = projects[0]["id"]
        else:
            reachable = sorted(snapshot.reachable(ws), key=lambda m: (not m.is_default, m.name))
            for m in reachable:
                projects.append(_project_entry(
                    project_id=m.project_id, name=m.name, status=m.status,
                    is_default=m.is_default, workspace_default=ws in m.workspace_defaults,
                    permissions=m.permissions, sources=m.role_sources, bypass=False,
                ))
            # the §2.6 header-less fallback, so the console starts where curl would
            if len(reachable) == 1:
                default_id = reachable[0].project_id
            else:
                default_id = next(
                    (m.project_id for m in reachable if ws in m.workspace_defaults), None,
                )
        for entry in projects:  # the switcher groups by department (§6.1)
            dept = departments.get(entry["id"])
            entry["department_id"], entry["department_name"] = dept or (None, None)
        row = workspaces.get(ws)
        out.append({
            # how many active projects the workspace binds: the read-only project chip
            # shows when the caller reaches one of several (§2.7)
            "bound_projects": sum(
                1 for b in access.workspace_bindings(db, ws) if b.status == "active"
            ),
            "workspace_id": ws, "name": row.name if row else ws,
            "default_project_id": default_id, "projects": projects,
        })
    return {
        "username": identity.username, "user_id": getattr(identity, "user_id", None),
        "role": identity.role, "is_admin": bypass,
        "can_manage_structure": access.can_manage_structure(identity),
        # §11.1: what the console shows a department administrator (their department's
        # projects and groups, and where they may bind)
        "managed_departments": _managed_view(db, identity),
        "workspaces": out,
    }


def _managed_view(db: Session, identity: Any) -> list[dict[str, Any]]:
    ids = sorted(access.managed_departments(db, identity))
    rows = {d.id: d for d in db.scalars(select(Department).where(Department.id.in_(ids)))}
    return [
        {"id": did, "name": rows[did].name if did in rows else did,
         "workspaces": sorted(access.open_workspaces(db, did))}
        for did in ids
    ]
