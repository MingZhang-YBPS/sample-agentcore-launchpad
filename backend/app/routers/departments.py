"""Departments API (projects design §2.1, §7.10).

Hub-global, like the rest of project structure. The list is `MEMBER` (department
names label the project switcher's groups, design §2.7); every write is `ADMIN` in
`core/route_policy.py`. A department grants no access, so none of this moves the
access epoch. The logic lives in `services/project_groups.py`.
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
    """Only while no project and no group names it (409 `department.in_use`)."""
    row = svc.get_department(db, department_id)
    svc.delete_department(db, require_identity(request), row)
    db.commit()
