"""Project runtime isolation API (projects design §10.13, R10-model).

Hub-global like the rest of `/api/projects` (a project spans workspaces): the
project is the path's, a workspace is a query / body field and must be one the
project is bound to. Reads are `MEMBER` in `core/route_policy.py` and answer 404 here
to non-members (`visible_project`), and a member only sees the workspaces they reach
the project in; switching isolation and retrying a run are `ADMIN`. The logic lives
in `services/project_isolation.py`.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.services import access
from app.services import project_isolation as svc
from app.services import projects as projects_svc

router = APIRouter(prefix="/api/projects", tags=["projects"])


class IsolationPut(BaseModel):
    # either the mode or the enabled switch (`enabled: true` = isolated)
    mode: str | None = Field(default=None, max_length=16)
    enabled: bool | None = None
    gateway: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _one_switch(self) -> "IsolationPut":
        if self.mode is None and self.enabled is None:
            raise ValueError("give mode or enabled")
        if self.mode is not None and self.enabled is not None and (
            (self.mode == svc.MODE_ISOLATED) != self.enabled
        ):
            raise ValueError("mode and enabled disagree")
        return self

    def resolved_mode(self) -> str:
        if self.mode is not None:
            return self.mode
        return svc.MODE_ISOLATED if self.enabled else svc.MODE_SHARED


class ProvisionPost(BaseModel):
    workspace: str = Field(min_length=1, max_length=32)


def _visible(db: Session, identity: Any, project_id: str):
    """The project and the workspaces the caller sees it in (None = all: an admin)."""
    row = projects_svc.visible_project(db, identity, project_id)
    if access.has_data_bypass(identity):
        return row, None
    membership = access.snapshot_for(db, identity).memberships.get(project_id)
    return row, set(membership.workspaces) if membership else set()


@router.get("/{project_id}/isolation")
def get_isolation(project_id: str, request: Request,
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    row, visible = _visible(db, require_identity(request), project_id)
    return svc.isolation_view(db, row, visible)


@router.put("/{project_id}/isolation")
def put_isolation(project_id: str, body: IsolationPut, request: Request,
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    identity = require_identity(request)
    row = projects_svc.get_project(db, project_id)
    queued = svc.set_isolation(db, identity, row, mode=body.resolved_mode(),
                               gateway=body.gateway)
    svc.start_jobs(queued)
    return {**svc.isolation_view(db, row, None), "queued": queued}


@router.get("/{project_id}/resources")
def get_resources(
    project_id: str, request: Request,
    workspace: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row, visible = _visible(db, require_identity(request), project_id)
    if workspace is not None:
        svc.require_bound(db, row.id, workspace, visible)
    return {"resources": svc.resources_list(db, row.id, workspace, visible)}


@router.get("/{project_id}/provisioning")
def get_provisioning(
    project_id: str, request: Request,
    workspace: str = Query(min_length=1, max_length=32),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row, visible = _visible(db, require_identity(request), project_id)
    svc.require_bound(db, row.id, workspace, visible)
    return svc.provisioning_view(db, row.id, workspace)


@router.post("/{project_id}/provision", status_code=202)
def post_provision(project_id: str, body: ProvisionPost, request: Request,
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    identity = require_identity(request)
    row = projects_svc.get_project(db, project_id)
    job, created = svc.retry_provision(db, identity, row, body.workspace)
    if created:
        svc.start_provision_async(job.id)
    return {**svc.provisioning_view(db, row.id, body.workspace), "created": created}
