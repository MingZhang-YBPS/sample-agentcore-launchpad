"""Project runtime isolation API (projects design §10.13, R10-model).

Hub-global like the rest of `/api/projects` (a project spans workspaces): the
project is the path's, a workspace is a query / body field and must be one the
project is bound to. Reads are `MEMBER` in `core/route_policy.py` and answer 404 here
to non-members (`visible_project`), and a member only sees the workspaces they reach
the project in; switching isolation and retrying a run are `ADMIN`. The logic lives
in `services/project_isolation.py`; §10.11's migration of existing agents (migrate,
its status, retry, retire, an agent's boundary) in `services/project_migration.py`.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Agent
from app.routers.auth import require_identity
from app.services import access
from app.services import project_isolation as svc
from app.services import project_migration as migration
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


class MigratePost(BaseModel):
    # isolate (onto the project's resources) or revert (back to the shared ones)
    direction: str = Field(default=migration.DIRECTION_ISOLATE, max_length=16)
    # one bound workspace, or every one
    workspace: str | None = Field(default=None, min_length=1, max_length=32)


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


# ── §10.11 migration of existing agents (R10-migrate) ─────────────────────────


@router.post("/{project_id}/isolation/migrate", status_code=202)
def post_migrate(project_id: str, body: MigratePost, request: Request,
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    identity = require_identity(request)
    row = projects_svc.get_project(db, project_id)
    queued = migration.migrate(db, identity, row, direction=body.direction,
                               workspace_id=body.workspace)
    migration.start_jobs(queued)
    db.refresh(row)
    return {**migration.migration_view(db, row, None), "queued": queued}


@router.get("/{project_id}/isolation/migration")
def get_migration(
    project_id: str, request: Request,
    workspace: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row, visible = _visible(db, require_identity(request), project_id)
    if workspace is not None:
        svc.require_bound(db, row.id, workspace, visible)
    return migration.migration_view(db, row, visible, workspace)


@router.post("/{project_id}/isolation/migration/{run_id}/retry", status_code=202)
def post_migration_retry(project_id: str, run_id: str, request: Request,
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    identity = require_identity(request)
    row = projects_svc.get_project(db, project_id)
    job = migration.retry(db, identity, row, run_id)
    migration.start_migration_async(job.id)
    return {"run": migration.run_view(job)}


@router.post("/{project_id}/isolation/retire", status_code=202)
def post_retire(project_id: str, request: Request,
                db: Session = Depends(get_db)) -> dict[str, Any]:
    identity = require_identity(request)
    row = projects_svc.get_project(db, project_id)
    queued = migration.retire(db, identity, row)
    migration.start_jobs(queued)
    db.refresh(row)
    return {**migration.migration_view(db, row, None), "queued": queued}


@router.get("/{project_id}/isolation/agents/{agent_id}")
def get_agent_boundary(project_id: str, agent_id: str, request: Request,
                       db: Session = Depends(get_db)) -> dict[str, Any]:
    """The agent detail's boundary line (§10.14). Another project's agent, or one in a
    workspace the caller does not reach the project in, is 404."""
    row, visible = _visible(db, require_identity(request), project_id)
    with project_context(None):
        agent = db.get(Agent, agent_id)
    if (agent is None or agent.status == "deleted" or agent.project_id != row.id
            or (visible is not None and agent.workspace_id not in visible)):
        raise NotFoundError("agent.not_found", "agent not found")
    return migration.agent_boundary_view(db, row, agent)
