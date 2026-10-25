"""A project's cross-project shares (projects design §11.2, G11-share).

Hub-global like the rest of `/api/projects/{project_id}`: the project is the path's,
the workspace a body / query field the project must be bound to. Listing is every
member's (a non-member gets 404 here, `visible_project`) and shows both directions —
what the project lends (`outgoing`) and what it receives (`incoming`). Granting and
revoking are the owner side's `project.share` (`pperm:`); approving or rejecting a
cross-department request is the grantee side's department administrator's (or an
administrator's). The logic is in `services/resource_shares.py`.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.shares import ResourceShare
from app.routers.auth import require_identity
from app.services import projects as projects_svc
from app.services import resource_shares as svc

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ShareCreate(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=32)
    kind: str = Field(min_length=1, max_length=24)
    resource_id: str = Field(min_length=1, max_length=128)
    grantee_project_id: str = Field(min_length=1, max_length=32)
    access: str = "read"
    expires_in_days: int | None = None
    resource_name: str = Field(default="", max_length=128)
    note: str = Field(default="", max_length=2000)


def _one(db: Session, row: ResourceShare, **extra: Any) -> dict[str, Any]:
    names = svc.project_names(db, {row.owner_project_id, row.grantee_project_id})
    return svc.view(row, names, extra=extra or None)


@router.get("/{project_id}/shares")
def list_shares(
    project_id: str,
    request: Request,
    direction: str = Query("all"),
    include_closed: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    projects_svc.visible_project(db, require_identity(request), project_id)
    return svc.list_for(db, project_id, direction, include_closed=include_closed)


@router.get("/{project_id}/shares/options")
def share_options(
    project_id: str, workspace_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return svc.options(db, projects_svc.get_project(db, project_id), workspace_id)


@router.post("/{project_id}/shares", status_code=201)
def create_share(
    project_id: str, body: ShareCreate, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.grant(
        db, identity, projects_svc.get_project(db, project_id),
        workspace_id=body.workspace_id, kind=body.kind, resource_id=body.resource_id,
        grantee_project_id=body.grantee_project_id, access_level=body.access,
        expires_in_days=body.expires_in_days, resource_name=body.resource_name,
        note=body.note,
    )
    db.commit()
    return _one(db, row)


@router.delete("/{project_id}/shares/{share_id}")
def revoke_share(
    project_id: str, share_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.revoke(db, require_identity(request), project_id, share_id)
    pinned = svc.dependents(db, row)
    db.commit()
    return _one(db, row, pinned_by=pinned)


@router.post("/{project_id}/shares/{share_id}/approve")
def approve_share(
    project_id: str, share_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.decide(db, require_identity(request), project_id, share_id, approve=True)
    db.commit()
    return _one(db, row)


@router.post("/{project_id}/shares/{share_id}/reject")
def reject_share(
    project_id: str, share_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.decide(db, require_identity(request), project_id, share_id, approve=False)
    db.commit()
    return _one(db, row)
