"""Departments (projects design §2.1–§2.2, §7.10).

The organisation is exactly three levels: department → project → member. (This
module once also held project groups; they were retired and folded into direct
project memberships — `core/db.flatten_project_groups` — so only the department
logic is left under the historical module name.)

* A **department** is the organizational owner of projects; every new project names
  one. It grants no access (phase 3 makes it a unit of delegation), so its writes do
  not move the access epoch.

* **Department administrators** (§11.1, `department_admins`) manage the projects
  of their department and bind its projects only to the workspaces an
  administrator opened to it (`department_workspaces`); optionally (the department's
  `settings.can_create_users`) they invite new `member` accounts into one of its
  projects. Appointing them and opening workspaces stay the platform's.

Like `services/projects.py`: every write is journalled as a structure event, and
commits are the caller's (the router). Who may call any of this is
`core/route_policy.py` (administrators, and a department's administrators for its
invitations — `access.can_manage_structure`).
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
    Project,
)
from app.services import access
from app.services import projects as projects_service

# department ids are slugs, the same rule as project ids (design §2.2)
SLUG_PATTERN = PROJECT_ID_PATTERN
_SLUG = re.compile(SLUG_PATTERN)
_STATUSES = ("active", "archived", "all")
# `departments.settings` (§11.1) and what an unset key means
DEPARTMENT_SETTING_DEFAULTS: dict[str, Any] = {
    "can_create_users": False, "max_projects": None, "user_validity_max_days": 90,
    # no `quotas`: a department caps nothing — a project's quotas and monthly budget
    # are set by platform administrators alone (projects §11.3)
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


# ── departments ───────────────────────────────────────────────────────────────


def department_not_found(department_id: str) -> NotFoundError:
    return NotFoundError("department.not_found", f"department '{department_id}' not found",
                         {"department_id": department_id})


def get_department(db: Session, department_id: str) -> Department:
    row = db.get(Department, department_id)
    if row is None:
        raise department_not_found(department_id)
    return row


def _department_counts(db: Session) -> dict[str, int]:
    return dict(db.execute(
        select(Project.department_id, func.count()).where(Project.department_id.is_not(None))
        .group_by(Project.department_id)
    ).all())


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
    return out


def department_view(row: Department, *, projects: int = 0) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "description": row.description or "",
        "settings": department_settings(row),
        "archived": row.archived_at is not None, "archived_at": _iso(row.archived_at),
        "created_by": row.created_by, "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "project_count": int(projects),
    }


def list_departments(db: Session, *, status: str | None = None) -> list[dict[str, Any]]:
    status = _check_status(status)
    query = select(Department).order_by(Department.name, Department.id)
    if status == "active":
        query = query.where(Department.archived_at.is_(None))
    elif status == "archived":
        query = query.where(Department.archived_at.is_not(None))
    projects = _department_counts(db)
    return [department_view(r, projects=projects.get(r.id, 0)) for r in db.scalars(query)]


def one_department_view(db: Session, row: Department) -> dict[str, Any]:
    return department_view(row, projects=_department_counts(db).get(row.id, 0))


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
    """Archived: no new project may name it; the ones that do keep it."""
    if (row.archived_at is not None) == archived:
        return row
    row.archived_at = _now() if archived else None
    row.updated_at = _now()
    _journal(db, identity, "department.archive" if archived else "department.unarchive",
             f"department:{row.id}")
    return row


def delete_department(db: Session, identity: Any, row: Department) -> None:
    """Only an unused department (no project — archived ones count too)."""
    projects = db.execute(select(func.count()).select_from(Project)
                          .where(Project.department_id == row.id)).scalar_one()
    if projects:
        raise AppError("department.in_use",
                       f"department '{row.id}' still owns {projects} project(s)",
                       {"department_id": row.id, "projects": int(projects)}, status_code=409)
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
