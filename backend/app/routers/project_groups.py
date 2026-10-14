"""Project groups API (projects design §7.7, §7.10).

Hub-global: a group bundles projects across workspaces, so nothing here reads
`X-Workspace` or `X-Project`. Every route is `ADMIN` in `core/route_policy.py`
(phase 1: structure is the platform administrator's, `access.can_manage_structure`).
The logic lives in `services/project_groups.py`; this module is request shapes only.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.services import project_groups as svc

router = APIRouter(prefix="/api/project-groups", tags=["projects"])


class GroupCreate(BaseModel):
    id: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    department_id: str | None = Field(default=None, max_length=32)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    department_id: str | None = Field(default=None, max_length=32)


class GroupMemberUpsert(BaseModel):
    user_id: str = Field(min_length=1, max_length=32)
    role_key: str = Field(min_length=1, max_length=32)  # a built-in template


class GroupMembersPut(BaseModel):
    upsert: list[GroupMemberUpsert] = Field(default_factory=list, max_length=500)
    remove: list[str] = Field(default_factory=list, max_length=500)


class GroupProjectsPut(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=200)
    remove: list[str] = Field(default_factory=list, max_length=200)


@router.get("")
def list_groups(
    status: str | None = Query(default=None, max_length=16),
    department_id: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return {"groups": svc.list_groups(db, status=status, department_id=department_id)}


@router.post("", status_code=201)
def create_group(
    body: GroupCreate, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.create_group(
        db, require_identity(request), group_id=body.id, name=body.name,
        description=body.description, department_id=body.department_id,
    )
    db.commit()
    return svc.group_detail(db, row)


@router.get("/{group_id}")
def get_group(group_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return svc.group_detail(db, svc.get_group(db, group_id))


@router.patch("/{group_id}")
def patch_group(
    group_id: str, body: GroupPatch, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_group(db, group_id)
    svc.update_group(db, require_identity(request), row, body.model_dump(exclude_unset=True))
    db.commit()
    return svc.group_detail(db, row)


@router.delete("/{group_id}")
def archive_group(
    group_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Archives (design §7.10's delete): the rows stay for the journal and an undo."""
    row = svc.get_group(db, group_id)
    effects = svc.archive_group(db, require_identity(request), row)
    db.commit()
    return {**svc.group_detail(db, row), **effects}


@router.post("/{group_id}/unarchive")
def unarchive_group(
    group_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_group(db, group_id)
    svc.unarchive_group(db, require_identity(request), row)
    db.commit()
    return svc.group_detail(db, row)


@router.put("/{group_id}/members")
def put_members(
    group_id: str, body: GroupMembersPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_group(db, group_id)
    result = svc.upsert_members(
        db, require_identity(request), row, [m.model_dump() for m in body.upsert], body.remove,
    )
    db.commit()
    return {**result, **svc.group_detail(db, row)}


@router.put("/{group_id}/projects")
def put_projects(
    group_id: str, body: GroupProjectsPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_group(db, group_id)
    result = svc.update_projects(db, require_identity(request), row, add=body.add,
                                 remove=body.remove)
    db.commit()
    return {**result, **svc.group_detail(db, row)}
