"""Departments API (projects design §2.1, §7.10, §11.1).

Hub-global, like the rest of project structure. The list is `MEMBER` (department
names label the project switcher's groups, design §2.7); every write is `ADMIN` in
`core/route_policy.py` — appointing department administrators and opening workspaces
to a department included — except the invitation, which the department's own
administrators may send when its `settings.can_create_users` allows. A department
grants no access, so none of this moves the access epoch. The logic lives in
`services/project_groups.py` (departments only; the module name is historical).
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.services import project_groups as svc

router = APIRouter(prefix="/api/departments", tags=["projects"])


class DepartmentCreate(BaseModel):
    id: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)


class DepartmentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    # partial: can_create_users, max_projects, user_validity_max_days (None = default)
    settings: dict[str, Any] | None = None


class IdsPut(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=200)
    remove: list[str] = Field(default_factory=list, max_length=200)


class InvitationCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=254)
    project_id: str = Field(min_length=2, max_length=32)
    role_key: str = Field(default="developer", min_length=1, max_length=32)
    valid_days: int | None = Field(default=None, ge=1, le=3650)


class DepartmentBudgetPut(BaseModel):
    # partial; typed loosely so the service answers `department.budget_invalid`
    monthly_budget_usd: Any = None
    allow_overrun: Any = None


# ── the department budget and its cost page (projects §11.3) ─────────────────


@router.get("/{department_id}/budget")
def get_budget(department_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The department cost page: platform administrators and the department's own
    administrators (`dadmin` overlay, `core/route_policy.py`)."""
    from app.services import department_budgets

    return department_budgets.department_view(db, svc.get_department(db, department_id))


@router.put("/{department_id}/budget")
def put_budget(
    department_id: str, body: DepartmentBudgetPut, request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The department's monthly budget (an allocation ceiling, never a block) and its
    overrun switch — platform administrators only."""
    from app.services import department_budgets

    row = svc.get_department(db, department_id)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("allow_overrun", False) is None:
        fields.pop("allow_overrun")
    department_budgets.update_department_budget(db, require_identity(request), row, fields)
    db.commit()
    return department_budgets.department_view(db, row)


@router.get("")
def list_departments(
    status: str | None = Query(default=None, max_length=16), db: Session = Depends(get_db),
) -> dict[str, Any]:
    return {"departments": svc.list_departments(db, status=status)}


@router.post("", status_code=201)
def create_department(
    body: DepartmentCreate, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.create_department(db, require_identity(request), department_id=body.id,
                                name=body.name, description=body.description)
    db.commit()
    return svc.one_department_view(db, row)


@router.patch("/{department_id}")
def patch_department(
    department_id: str, body: DepartmentPatch, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_department(db, department_id)
    svc.update_department(db, require_identity(request), row,
                          body.model_dump(exclude_unset=True))
    db.commit()
    return svc.one_department_view(db, row)


@router.post("/{department_id}/archive")
def archive_department(
    department_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_department(db, department_id)
    svc.set_department_archived(db, require_identity(request), row, True)
    db.commit()
    return svc.one_department_view(db, row)


@router.post("/{department_id}/unarchive")
def unarchive_department(
    department_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_department(db, department_id)
    svc.set_department_archived(db, require_identity(request), row, False)
    db.commit()
    return svc.one_department_view(db, row)


@router.delete("/{department_id}", status_code=204)
def delete_department(
    department_id: str, request: Request, db: Session = Depends(get_db),
) -> None:
    """Only while no project names it (409 `department.in_use`)."""
    row = svc.get_department(db, department_id)
    svc.delete_department(db, require_identity(request), row)
    db.commit()


# ── department administrators, open workspaces, invitations (design §11.1) ───


@router.get("/{department_id}/admins")
def list_admins(department_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = svc.get_department(db, department_id)
    return {"admins": svc.department_admins_view(db, row)}


@router.put("/{department_id}/admins")
def put_admins(
    department_id: str, body: IdsPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_department(db, department_id)
    result = svc.update_department_admins(db, require_identity(request), row,
                                          add=body.add, remove=body.remove)
    db.commit()
    return {**result, "admins": svc.department_admins_view(db, row)}


@router.get("/{department_id}/workspaces")
def list_workspaces(department_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = svc.get_department(db, department_id)
    return {"workspaces": svc.department_workspaces_view(db, row)}


@router.put("/{department_id}/workspaces")
def put_workspaces(
    department_id: str, body: IdsPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_department(db, department_id)
    result = svc.update_department_workspaces(db, require_identity(request), row,
                                              add=body.add, remove=body.remove)
    db.commit()
    return {**result, "workspaces": svc.department_workspaces_view(db, row)}


@router.post("/{department_id}/invitations", status_code=201)
def invite(
    department_id: str, body: InvitationCreate, request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A new member account in one of the department's projects; the generated
    password is shown once, like `POST /api/users`."""
    row = svc.get_department(db, department_id)
    user, password = svc.invite_user(
        db, require_identity(request), row, username=body.username, email=body.email,
        project_id=body.project_id, role_key=body.role_key, valid_days=body.valid_days,
    )
    db.commit()
    return {
        "user_id": user.id, "username": user.username, "email": user.email,
        "expires_at": user.expires_at.isoformat() if user.expires_at else None,
        "project_id": body.project_id, "role_key": body.role_key,
        "generated_password": password,
    }
