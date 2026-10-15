"""Project groups and departments (projects design §2.1–§2.2, §7.7, §7.10).

* A **project group** is an access bundle: its members reach every project in it
  with a built-in template role (custom roles are per project). A project is in at
  most one group (D-5, `projects.group_id`). Archiving a group revokes the access it
  gave; the projects keep their `group_id`, so unarchiving gives it back.
* A **department** is the organizational owner of projects and groups. It grants
  no access (phase 3 makes it a unit of delegation), so its writes do not move the
  access epoch.

* **Department administrators** (§11.1, `department_admins`) manage the projects
  and groups of their department and bind its projects only to the workspaces an
  administrator opened to it (`department_workspaces`); optionally (the department's
  `settings.can_create_users`) they invite new `member` accounts into one of its
  projects. Appointing them and opening workspaces stay the platform's.

Like `services/projects.py`: every write that changes who reaches what bumps
`access_epoch` and disables the API keys whose creators lost their project in the
SAME transaction, every write is journalled as a structure event, and commits are
the caller's (the router). Who may call any of this is `core/route_policy.py`
(administrators, and a department's administrators for its groups —
`access.can_manage_structure`); the checks that need the request body (the new
group's department, the projects put into a group) are here.
"""

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.ledger import User, Workspace
from app.models.projects import (
    PROJECT_ID_PATTERN,
    Department,
    DepartmentAdmin,
    DepartmentWorkspace,
    GroupMember,
    Project,
    ProjectGroup,
    ProjectMember,
    ProjectRole,
)
from app.services import access
from app.services import api_keys as api_keys_service
from app.services import projects as projects_service
from app.services import steward as steward_service
from app.services.users import ROLE_STEWARD

# group and department ids are slugs, the same rule as project ids (design §2.2)
SLUG_PATTERN = PROJECT_ID_PATTERN
_SLUG = re.compile(SLUG_PATTERN)
_STATUSES = ("active", "archived", "all")
# `departments.settings` (§11.1) and what an unset key means
DEPARTMENT_SETTING_DEFAULTS: dict[str, Any] = {
    "can_create_users": False, "max_projects": None, "user_validity_max_days": 90,
    # caps on its projects' `settings.quotas` (projects §11.3): each project's
    # effective limit is the smaller of its own and this
    "quotas": None,
}
MAX_VALIDITY_DAYS = 3650


def _now() -> datetime:
    return datetime.now(UTC)


def _journal(db: Session, identity: Any, action: str, target: str) -> None:
    projects_service._journal(db, identity, action, target)


def _check_slug(kind: str, value: str) -> None:
    if not _SLUG.match(value):
        raise AppError(f"{kind}.invalid_id",
                       f"{kind} id must be 2-32 lowercase letters, digits or '-'",
                       {"pattern": SLUG_PATTERN})


def _check_status(status: str | None) -> str:
    status = status or "active"
    if status not in _STATUSES:
        raise AppError("structure.invalid_status", f"status must be one of {list(_STATUSES)}",
                       {"status": status})
    return status


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _forbidden(code: str, message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(code, message, details or {}, status_code=403)


def _require_department(db: Session, identity: Any, department_id: str | None) -> None:
    """A department administrator's new or moved group must be in a department they
    administer (a group with no department is the platform's)."""
    if access.can_manage_structure(identity):
        return
    if not access.can_manage_structure(identity, department_id, db=db):
        raise _forbidden("department.not_managed",
                         "a department administrator works inside their own department",
                         {"department_id": department_id})


# ── departments ───────────────────────────────────────────────────────────────


def department_not_found(department_id: str) -> NotFoundError:
    return NotFoundError("department.not_found", f"department '{department_id}' not found",
                         {"department_id": department_id})


def get_department(db: Session, department_id: str) -> Department:
    row = db.get(Department, department_id)
    if row is None:
        raise department_not_found(department_id)
    return row


def _department_counts(db: Session) -> tuple[dict[str, int], dict[str, int]]:
    projects = dict(db.execute(
        select(Project.department_id, func.count()).where(Project.department_id.is_not(None))
        .group_by(Project.department_id)
    ).all())
    groups = dict(db.execute(
        select(ProjectGroup.department_id, func.count())
        .where(ProjectGroup.department_id.is_not(None))
        .group_by(ProjectGroup.department_id)
    ).all())
    return projects, groups


def department_settings(row: Department) -> dict[str, Any]:
    """The department's settings with the defaults filled in."""
    stored = row.settings if isinstance(row.settings, dict) else {}
    return {k: stored.get(k, v) for k, v in DEPARTMENT_SETTING_DEFAULTS.items()}


def _check_department_settings(value: Any) -> dict[str, Any]:
    """A partial `settings` object; a None entry restores that key's default."""
    if not isinstance(value, dict):
        raise AppError("department.setting_invalid", "settings must be an object")
    unknown = sorted(set(value) - set(DEPARTMENT_SETTING_DEFAULTS))
    if unknown:
        raise AppError("department.setting_invalid", f"unknown department settings: {unknown}",
                       {"keys": unknown})
    out = dict(value)
    for key, item in value.items():
        if item is None:
            continue
        if key == "can_create_users" and not isinstance(item, bool):
            raise AppError("department.setting_invalid", "can_create_users must be true or false",
                           {"key": key})
        if key in ("max_projects", "user_validity_max_days") and (
            isinstance(item, bool) or not isinstance(item, int)
            or not 1 <= item <= (MAX_VALIDITY_DAYS if key == "user_validity_max_days" else 10000)
        ):
            raise AppError("department.setting_invalid", f"{key} must be a positive whole number",
                           {"key": key})
        if key == "quotas":
            try:
                out[key] = projects_service._check_quotas(item)
            except AppError as exc:
                raise AppError("department.setting_invalid", exc.message,
                               {**(exc.detail or {}), "key": key}) from None
    return out


def department_view(
    row: Department, *, projects: int = 0, groups: int = 0,
) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "description": row.description or "",
        "settings": department_settings(row),
        "archived": row.archived_at is not None, "archived_at": _iso(row.archived_at),
        "created_by": row.created_by, "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "project_count": int(projects), "group_count": int(groups),
    }


def list_departments(db: Session, *, status: str | None = None) -> list[dict[str, Any]]:
    status = _check_status(status)
    query = select(Department).order_by(Department.name, Department.id)
    if status == "active":
        query = query.where(Department.archived_at.is_(None))
    elif status == "archived":
        query = query.where(Department.archived_at.is_not(None))
    projects, groups = _department_counts(db)
    return [department_view(r, projects=projects.get(r.id, 0), groups=groups.get(r.id, 0))
            for r in db.scalars(query)]


def one_department_view(db: Session, row: Department) -> dict[str, Any]:
    projects, groups = _department_counts(db)
    return department_view(row, projects=projects.get(row.id, 0), groups=groups.get(row.id, 0))


def create_department(
    db: Session, identity: Any, *, department_id: str, name: str, description: str = "",
) -> Department:
    _check_slug("department", department_id)
    if db.get(Department, department_id) is not None:
        raise AppError("department.exists", f"department '{department_id}' already exists",
                       {"department_id": department_id}, status_code=409)
    row = Department(id=department_id, name=name.strip(), description=description or "",
                     created_by=getattr(identity, "user_id", None))
    db.add(row)
    db.flush()
    _journal(db, identity, "department.create", f"department:{department_id}")
    return row


def update_department(
    db: Session, identity: Any, row: Department, fields: dict[str, Any],
) -> Department:
    changed = []
    for key in ("name", "description"):
        if key in fields and fields[key] is not None and getattr(row, key) != fields[key]:
            setattr(row, key, fields[key].strip() if key == "name" else fields[key])
            changed.append(key)
    if fields.get("settings") is not None:
        clean = _check_department_settings(fields["settings"])
        # `quotas` merges one level deeper, like a project's (one cap set keeps the rest)
        merged = projects_service._merge_settings(row.settings or {}, clean)
        if merged != (row.settings or {}):
            row.settings = merged or None
            changed.append("settings")
    if changed:
        row.updated_at = _now()
        _journal(db, identity, "department.update",
                 f"department:{row.id} {','.join(changed)}")
    return row


def set_department_archived(
    db: Session, identity: Any, row: Department, archived: bool,
) -> Department:
    """Archived: no new project or group may name it; the ones that do keep it."""
    if (row.archived_at is not None) == archived:
        return row
    row.archived_at = _now() if archived else None
    row.updated_at = _now()
    _journal(db, identity, "department.archive" if archived else "department.unarchive",
             f"department:{row.id}")
    return row


def delete_department(db: Session, identity: Any, row: Department) -> None:
    """Only an unused department (no project, no group — archived ones count too)."""
    projects = db.execute(select(func.count()).select_from(Project)
                          .where(Project.department_id == row.id)).scalar_one()
    groups = db.execute(select(func.count()).select_from(ProjectGroup)
                        .where(ProjectGroup.department_id == row.id)).scalar_one()
    if projects or groups:
        raise AppError("department.in_use",
                       f"department '{row.id}' still owns {projects} project(s) and "
                       f"{groups} group(s)",
                       {"department_id": row.id, "projects": int(projects),
                        "groups": int(groups)}, status_code=409)
    for model in (DepartmentAdmin, DepartmentWorkspace):
        for item in db.scalars(select(model).where(model.department_id == row.id)):
            db.delete(item)
    db.delete(row)
    _journal(db, identity, "department.delete", f"department:{row.id}")


# ── department administrators and open workspaces (design §11.1) ──────────────


def department_admins_view(db: Session, row: Department) -> list[dict[str, Any]]:
    admins = list(db.scalars(select(DepartmentAdmin)
                             .where(DepartmentAdmin.department_id == row.id)))
    users = {u.id: u for u in db.scalars(
        select(User).where(User.id.in_([a.user_id for a in admins]))
    )} if admins else {}
    out = []
    for a in admins:
        user = users.get(a.user_id)
        out.append({
            "user_id": a.user_id,
            "username": user.username if user else None,
            "email": user.email if user else None,
            "account_role": user.role if user else None,
            "status": user.status if user else None,
            "added_by": a.added_by, "added_at": _iso(a.added_at),
        })
    out.sort(key=lambda r: ((r["username"] or "").lower(), r["user_id"]))
    return out


def update_department_admins(
    db: Session, identity: Any, row: Department, *, add: list[str], remove: list[str],
) -> dict[str, list[str]]:
    """Appoint / remove department administrators (platform administrators only, in
    `core/route_policy.py`). No access epoch: the route policy reads these rows per
    request, and they grant no data access."""
    if set(add) & set(remove):
        raise AppError("department.admin_conflict", "a user cannot be added and removed "
                       "in one request", {"users": sorted(set(add) & set(remove))})
    result: dict[str, list[str]] = {"added": [], "removed": []}
    for uid in sorted(set(add)):
        user = db.get(User, uid)
        if user is None:
            raise NotFoundError("user.not_found", f"user '{uid}' not found", {"user_id": uid})
        if db.get(DepartmentAdmin, (row.id, uid)) is not None:
            continue
        db.add(DepartmentAdmin(department_id=row.id, user_id=uid,
                               added_by=getattr(identity, "user_id", None)))
        result["added"].append(uid)
        _journal(db, identity, "department.admins.add",
                 f"department:{row.id} user:{user.username}")
    for uid in sorted(set(remove)):
        existing = db.get(DepartmentAdmin, (row.id, uid))
        if existing is None:
            raise NotFoundError("department.admin_not_found",
                                f"user '{uid}' is not an administrator of department '{row.id}'",
                                {"user_id": uid, "department_id": row.id})
        user = db.get(User, uid)
        db.delete(existing)
        result["removed"].append(uid)
        _journal(db, identity, "department.admins.remove",
                 f"department:{row.id} user:{user.username if user else uid}")
    db.flush()
    return result


def department_workspaces_view(db: Session, row: Department) -> list[dict[str, Any]]:
    opened = list(db.scalars(select(DepartmentWorkspace)
                             .where(DepartmentWorkspace.department_id == row.id)
                             .order_by(DepartmentWorkspace.workspace_id)))
    names = {w.id: w for w in db.scalars(select(Workspace))}
    out = []
    for o in opened:
        ws = names.get(o.workspace_id)
        out.append({
            "workspace_id": o.workspace_id,
            "name": ws.name if ws else o.workspace_id,
            "region": ws.region if ws else None,
            "added_by": o.added_by, "added_at": _iso(o.added_at),
        })
    return out


def update_department_workspaces(
    db: Session, identity: Any, row: Department, *, add: list[str], remove: list[str],
) -> dict[str, list[str]]:
    """Open / close workspaces to a department (platform administrators only). Closing
    one leaves the bindings already made: it only stops new ones."""
    if set(add) & set(remove):
        raise AppError("department.workspace_conflict", "a workspace cannot be opened and "
                       "closed in one request", {"workspaces": sorted(set(add) & set(remove))})
    result: dict[str, list[str]] = {"added": [], "removed": []}
    for ws in sorted(set(add)):
        projects_service._workspace(db, ws)
        if db.get(DepartmentWorkspace, (row.id, ws)) is not None:
            continue
        db.add(DepartmentWorkspace(department_id=row.id, workspace_id=ws,
                                   added_by=getattr(identity, "user_id", None)))
        result["added"].append(ws)
        _journal(db, identity, "department.workspaces.add",
                 f"department:{row.id} workspace:{ws}")
    for ws in sorted(set(remove)):
        existing = db.get(DepartmentWorkspace, (row.id, ws))
        if existing is None:
            raise NotFoundError("department.workspace_not_found",
                                f"workspace '{ws}' is not open to department '{row.id}'",
                                {"workspace_id": ws, "department_id": row.id})
        db.delete(existing)
        result["removed"].append(ws)
        _journal(db, identity, "department.workspaces.remove",
                 f"department:{row.id} workspace:{ws}")
    db.flush()
    return result


def invite_user(
    db: Session, identity: Any, row: Department, *, username: str, email: str,
    project_id: str, role_key: str, valid_days: int | None = None,
) -> tuple[User, str | None]:
    """A new `member` account straight into one of the department's projects (§11.1):
    valid at most `user_validity_max_days`, no workspace grant of its own (it reaches
    the project's workspaces through the membership). A department administrator needs
    the department's `can_create_users`. Commits (`users.create_user` does)."""
    from app.services import users as users_service

    settings = department_settings(row)
    if not access.can_manage_structure(identity) and not settings["can_create_users"]:
        raise _forbidden("department.invitations_disabled",
                         f"department '{row.id}' does not let its administrators create "
                         "accounts", {"department_id": row.id})
    if row.archived_at is not None:
        raise AppError("department.archived", f"department '{row.id}' is archived",
                       {"department_id": row.id}, status_code=409)
    project = db.get(Project, project_id)
    if project is None or project.department_id != row.id:
        raise projects_service.not_found(project_id)
    if project.status != projects_service.STATUS_ACTIVE_PROJECT:
        raise AppError("project.archived", f"project '{project_id}' is archived",
                       {"project_id": project_id}, status_code=409)
    if role_key == access.INHERIT_ROLE:
        raise AppError("project.role_unknown", "an invitation names a project role",
                       {"role_key": role_key})
    projects_service._check_role_key(db, project, role_key)
    limit = int(settings["user_validity_max_days"])
    days = limit if valid_days is None else int(valid_days)
    if not 1 <= days <= limit:
        raise AppError("department.validity_too_long",
                       f"an invited account is valid 1-{limit} days in department '{row.id}'",
                       {"max_days": limit}, status_code=422)
    user, password = users_service.create_user(
        db, username=username, email=email, password=None, role=users_service.ROLE_MEMBER,
        workspaces=[], valid_days=days, created_by=identity.username,
        projects=[{"project_id": project_id, "role_key": role_key}], actor=identity,
    )
    _journal(db, identity, "department.invite",
             f"department:{row.id} user:{user.username} project:{project_id} role:{role_key}")
    return user, password


# ── groups: lookup and views ──────────────────────────────────────────────────


def group_not_found(group_id: str) -> NotFoundError:
    return NotFoundError("group.not_found", f"project group '{group_id}' not found",
                         {"group_id": group_id})


def get_group(db: Session, group_id: str) -> ProjectGroup:
    row = db.get(ProjectGroup, group_id)
    if row is None:
        raise group_not_found(group_id)
    return row


def _active(row: ProjectGroup) -> None:
    if row.archived_at is not None:
        raise AppError("group.archived", f"project group '{row.id}' is archived",
                       {"group_id": row.id}, status_code=409)


def _group_projects(db: Session, group_id: str) -> list[Project]:
    return list(db.scalars(
        select(Project).where(Project.group_id == group_id).order_by(Project.name, Project.id)
    ))


def _group_members(db: Session, group_id: str) -> list[GroupMember]:
    return list(db.scalars(select(GroupMember).where(GroupMember.group_id == group_id)))


def group_view(
    row: ProjectGroup, projects: list[Project], members: int,
) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "description": row.description or "",
        "department_id": row.department_id,
        "archived": row.archived_at is not None, "archived_at": _iso(row.archived_at),
        "created_by": row.created_by, "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "projects": [{"id": p.id, "name": p.name, "status": p.status} for p in projects],
        "member_count": int(members),
    }


def list_groups(
    db: Session, *, status: str | None = None, department_id: str | None = None,
) -> list[dict[str, Any]]:
    status = _check_status(status)
    query = select(ProjectGroup).order_by(ProjectGroup.name, ProjectGroup.id)
    if status == "active":
        query = query.where(ProjectGroup.archived_at.is_(None))
    elif status == "archived":
        query = query.where(ProjectGroup.archived_at.is_not(None))
    if department_id:
        query = query.where(ProjectGroup.department_id == department_id)
    rows = list(db.scalars(query))
    ids = [r.id for r in rows]
    if not ids:
        return []
    members = dict(db.execute(
        select(GroupMember.group_id, func.count()).where(GroupMember.group_id.in_(ids))
        .group_by(GroupMember.group_id)
    ).all())
    projects: dict[str, list[Project]] = {}
    for p in db.scalars(select(Project).where(Project.group_id.in_(ids))
                        .order_by(Project.name, Project.id)):
        projects.setdefault(p.group_id or "", []).append(p)
    return [group_view(r, projects.get(r.id, []), members.get(r.id, 0)) for r in rows]


def _member_conflicts(
    db: Session, projects: list[Project], members: list[GroupMember],
) -> dict[str, list[dict[str, Any]]]:
    """Per member: the projects of the group where they combine duties (the group's
    role ∪ their direct row there, design §3.6)."""
    if not projects or not members:
        return {}
    user_ids = sorted({m.user_id for m in members})
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(user_ids)))}
    project_ids = [p.id for p in projects]
    direct = {(m.project_id, m.user_id): m for m in db.scalars(
        select(ProjectMember).where(ProjectMember.project_id.in_(project_ids),
                                    ProjectMember.user_id.in_(user_ids))
    )}
    custom: dict[str, dict[str, frozenset[str]]] = {}
    for r in db.scalars(select(ProjectRole).where(ProjectRole.project_id.in_(project_ids))):
        custom.setdefault(r.project_id, {})[r.key] = (
            frozenset(r.permissions or []) & access.ALL_PROJECT_KEYS
        )
    out: dict[str, list[dict[str, Any]]] = {}
    for m in members:
        for p in projects:
            keys = projects_service.effective_keys(
                users.get(m.user_id), direct.get((p.id, m.user_id)), m, custom.get(p.id, {}),
            )
            pairs = projects_service.sod_conflicts(keys)
            if pairs:
                out.setdefault(m.user_id, []).append({
                    "project_id": p.id, "pairs": pairs,
                    "strict": projects_service.sod_strict(p),
                })
    return out


def group_detail(db: Session, row: ProjectGroup) -> dict[str, Any]:
    projects = _group_projects(db, row.id)
    members = sorted(_group_members(db, row.id), key=lambda m: m.user_id)
    users = {u.id: u for u in db.scalars(
        select(User).where(User.id.in_([m.user_id for m in members]))
    )} if members else {}
    conflicts = _member_conflicts(db, projects, members)
    rows = []
    for m in members:
        user = users.get(m.user_id)
        rows.append({
            "user_id": m.user_id,
            "username": user.username if user else None,
            "email": user.email if user else None,
            "account_role": user.role if user else None,
            "status": user.status if user else None,
            "role_key": m.role_key,
            "added_by": m.added_by, "added_at": _iso(m.added_at),
            # the §3.6 "combined duties" hint, per project of the group
            "sod_conflicts": conflicts.get(m.user_id, []),
        })
    rows.sort(key=lambda r: ((r["username"] or "").lower(), r["user_id"]))
    return {**group_view(row, projects, len(members)), "members": rows}


# ── groups: writes ────────────────────────────────────────────────────────────


def _revoke_effects(db: Session, project_ids: list[str]) -> int:
    """Access moved for the members of these projects: the next request in every
    process re-reads it, and keys whose creators lost the project stop (§5.7)."""
    db.flush()
    access.bump_epoch(db)
    return sum(api_keys_service.disable_offboarded_keys_in_project(db, pid)
               for pid in project_ids)


def create_group(
    db: Session, identity: Any, *, group_id: str, name: str, description: str = "",
    department_id: str | None = None,
) -> ProjectGroup:
    _check_slug("group", group_id)
    if db.get(ProjectGroup, group_id) is not None:
        raise AppError("group.exists", f"project group '{group_id}' already exists",
                       {"group_id": group_id}, status_code=409)
    projects_service._check_org(db, department_id, None)
    _require_department(db, identity, department_id)
    row = ProjectGroup(id=group_id, name=name.strip(), description=description or "",
                       department_id=department_id or None,
                       created_by=getattr(identity, "user_id", None))
    db.add(row)
    db.flush()
    _journal(db, identity, "group.create", f"group:{group_id}")
    return row


def update_group(
    db: Session, identity: Any, row: ProjectGroup, fields: dict[str, Any],
) -> ProjectGroup:
    """Name / description / department — none of it changes who reaches what."""
    if "department_id" in fields and (fields["department_id"] or None) != row.department_id:
        projects_service._check_org(db, fields["department_id"] or None, None)
        _require_department(db, identity, fields["department_id"] or None)
    changed = []
    for key in ("name", "description", "department_id"):
        if key not in fields:
            continue
        value = fields[key]
        if key == "name":
            if value is None:
                continue
            value = value.strip()
        elif key == "description":
            value = value or ""
        else:
            value = value or None
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed.append(key)
    if changed:
        row.updated_at = _now()
        _journal(db, identity, "group.update", f"group:{row.id} {','.join(changed)}")
    return row


def archive_group(db: Session, identity: Any, row: ProjectGroup) -> dict[str, int]:
    """The access the group gave stops now; direct members keep theirs. The projects
    keep `group_id`, so an unarchive gives the same access back."""
    if row.archived_at is not None:
        return {"keys_disabled": 0}
    row.archived_at = row.updated_at = _now()
    keys = _revoke_effects(db, [p.id for p in _group_projects(db, row.id)])
    _journal(db, identity, "group.archive", f"group:{row.id}")
    return {"keys_disabled": keys}


def unarchive_group(db: Session, identity: Any, row: ProjectGroup) -> None:
    if row.archived_at is None:
        return
    projects = _group_projects(db, row.id)
    row.archived_at = None
    row.updated_at = _now()
    db.flush()
    # a strict project must not regain a combined duty through the group
    member_ids = {m.user_id for m in _group_members(db, row.id)}
    for project in projects:
        projects_service.enforce_sod(db, project, member_ids)
    access.bump_epoch(db)
    _journal(db, identity, "group.unarchive", f"group:{row.id}")


def _check_group_role(role_key: str) -> None:
    if role_key not in access.ROLE_TEMPLATES:
        raise AppError("group.role_invalid",
                       f"'{role_key}' is not a built-in template — a group grants built-in "
                       "templates only (custom roles are per project)",
                       {"role_key": role_key, "templates": sorted(access.ROLE_TEMPLATES)})


def _check_member_targets(
    db: Session, identity: Any, row: ProjectGroup, upsert: list[dict[str, Any]],
    remove: list[str],
) -> None:
    """Refuse a change before anything is written (or staged for confirmation). A
    group member reaches every project of the group, so the steward rules of a
    project's members apply (§11.4): a steward never adds itself, and no steward
    account is ever a member."""
    me = getattr(identity, "user_id", None)
    if steward_service.is_steward(identity) and me and me in {u["user_id"] for u in upsert}:
        raise AppError("project.self_membership",
                       "a steward manages structure only and cannot join a group",
                       {"user_id": me}, status_code=403)
    for item in upsert:
        uid = item["user_id"]
        user = db.get(User, uid)
        if user is None:
            raise NotFoundError("user.not_found", f"user '{uid}' not found", {"user_id": uid})
        if user.role == ROLE_STEWARD:
            raise AppError("project.steward_member",
                           f"'{user.username}' is a steward: stewards are never project "
                           "or group members", {"user_id": uid}, status_code=409)
        _check_group_role(item["role_key"])
    for uid in remove:
        if db.get(GroupMember, (row.id, uid)) is None:
            raise NotFoundError("group.member_not_found",
                                f"user '{uid}' is not a member of group '{row.id}'",
                                {"user_id": uid, "group_id": row.id})


def upsert_members(
    db: Session, identity: Any, row: ProjectGroup, upsert: list[dict[str, Any]],
    remove: list[str], *, confirmed_by: Any = None,
) -> dict[str, list[str]]:
    """Add / update / remove group members; each change applies to every project of
    the group at once.

    A steward's change (§11.4) is journaled and noticed to the owners of every
    project of the group; under `steward_two_person` it is staged instead
    (`pending: [id]`, nothing applied) until someone else confirms it —
    `confirmed_by` is that confirmer, applying it."""
    _active(row)
    if {u["user_id"] for u in upsert} & set(remove):
        raise AppError("group.member_conflict", "a user cannot be upserted and removed "
                       "in one request")
    _check_member_targets(db, identity, row, upsert, remove)
    by_steward = steward_service.is_steward(identity)
    if (by_steward and confirmed_by is None and (upsert or remove)
            and steward_service.two_person(db)):
        pending = steward_service.request_group_change(
            db, identity, row, _group_projects(db, row.id), upsert, remove)
        return {"added": [], "updated": [], "removed": [], "pending": [pending]}
    result: dict[str, list[str]] = {"added": [], "updated": [], "removed": []}
    for item in upsert:
        uid, role_key = item["user_id"], item["role_key"]
        user = db.get(User, uid)
        if user is None:
            raise NotFoundError("user.not_found", f"user '{uid}' not found", {"user_id": uid})
        _check_group_role(role_key)
        existing = db.get(GroupMember, (row.id, uid))
        if existing is None:
            db.add(GroupMember(group_id=row.id, user_id=uid, role_key=role_key,
                               added_by=getattr(identity, "user_id", None)))
            result["added"].append(uid)
            _journal(db, identity, "group.members.add",
                     f"group:{row.id} user:{user.username} role:{role_key}")
        elif existing.role_key != role_key:
            existing.role_key = role_key
            result["updated"].append(uid)
            _journal(db, identity, "group.members.update",
                     f"group:{row.id} user:{user.username} role:{role_key}")
    for uid in remove:
        existing = db.get(GroupMember, (row.id, uid))
        if existing is None:
            raise NotFoundError("group.member_not_found",
                                f"user '{uid}' is not a member of group '{row.id}'",
                                {"user_id": uid, "group_id": row.id})
        user = db.get(User, uid)
        db.delete(existing)
        result["removed"].append(uid)
        _journal(db, identity, "group.members.remove",
                 f"group:{row.id} user:{user.username if user else uid}")
    if any(result.values()):
        db.flush()
        projects = _group_projects(db, row.id)
        touched = set(result["added"]) | set(result["updated"])
        for project in projects:
            projects_service.enforce_sod(db, project, touched)
        _revoke_effects(db, [p.id for p in projects])
        if by_steward:  # §11.4 ②: the trace, in each project the group reaches
            for project in projects:
                steward_service.after_member_change(
                    db, identity, project, upsert, result, confirmed_by=confirmed_by,
                    group_id=row.id)
    return result


def update_projects(
    db: Session, identity: Any, row: ProjectGroup, *, add: list[str], remove: list[str],
) -> dict[str, list[str]]:
    """Put projects into the group / take them out (`projects.group_id`, 0..1 — a
    project in another active group must leave it first). Taking a project out
    revokes the access the group gave there; its direct members keep theirs."""
    if set(add) & set(remove):
        raise AppError("group.project_conflict", "a project cannot be added and removed "
                       "in one request", {"projects": sorted(set(add) & set(remove))})
    if add:
        _active(row)
    result: dict[str, list[str]] = {"added": [], "removed": []}
    platform = access.can_manage_structure(identity)
    for pid in sorted(set(add)):
        project = db.get(Project, pid)
        # a department administrator adds the department's own projects only — another
        # department's are invisible to them (404)
        if project is None or not (platform or access.can_manage_structure(
                identity, project.department_id, db=db)):
            raise projects_service.not_found(pid)
        if project.group_id == row.id:
            continue
        if project.group_id:
            other = db.get(ProjectGroup, project.group_id)
            if other is not None and other.archived_at is None:
                raise AppError("group.project_in_other_group",
                               f"project '{pid}' is in group '{project.group_id}' — "
                               "remove it there first (a project is in at most one group)",
                               {"project_id": pid, "group_id": project.group_id},
                               status_code=409)
        project.group_id = row.id
        project.updated_at = _now()
        result["added"].append(pid)
        _journal(db, identity, "group.projects.add", f"group:{row.id} project:{pid}")
    for pid in sorted(set(remove)):
        project = db.get(Project, pid)
        if project is None:
            raise projects_service.not_found(pid)
        if project.group_id != row.id:
            raise NotFoundError("group.project_not_found",
                                f"project '{pid}' is not in group '{row.id}'",
                                {"project_id": pid, "group_id": row.id})
        project.group_id = None
        project.updated_at = _now()
        result["removed"].append(pid)
        _journal(db, identity, "group.projects.remove", f"group:{row.id} project:{pid}")
    if result["added"] or result["removed"]:
        db.flush()
        if row.archived_at is None:
            members = _group_members(db, row.id)
            member_ids = {m.user_id for m in members}
            for pid in result["added"]:
                projects_service.enforce_sod(db, db.get(Project, pid), member_ids)
            if steward_service.is_steward(identity) and members:
                # moving a project into a group hands the group's members access to
                # it: the same trace as adding them one by one (§11.4 ②)
                upsert = [{"user_id": m.user_id, "role_key": m.role_key} for m in members]
                for pid in result["added"]:
                    steward_service.after_member_change(
                        db, identity, db.get(Project, pid), upsert,
                        {"added": sorted(member_ids), "updated": [], "removed": []},
                        group_id=row.id)
        _revoke_effects(db, result["added"] + result["removed"])
    return result
