"""Network profiles (design projects §12.11): workspace VPC settings, kept by
administrators, that a project's agents may run in.

Reads are member routes inside the request's project — a member sees the project's
allowed set only, an administrator the whole workspace. Writes and the per-project
allowed set are administrator routes, project-exempt: a profile is workspace
infrastructure, and `PUT /api/network-profiles/projects/{project_id}` names its
project in the path. (Not under `/api/projects`: that prefix is hub-global, while an
allowed set belongs to one workspace.)

Preflight (`POST …/{profile_id}/check`) and the probe arrive with V12-check.
"""

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import network_profiles as service
from app.services.audit import record_audit_event

router = APIRouter(prefix="/api/network-profiles", tags=["network-profiles"])

VPC_ID = r"^vpc-[0-9a-f]{8,17}$"
SUBNET_ID = r"^subnet-[0-9a-f]{8,17}$"
SG_ID = r"^sg-[0-9a-f]{8,17}$"
MAX_REACHABLE = 50


class Reachable(BaseModel):
    """A declared private target: a CIDR or a host name, and a port (§12.7)."""

    model_config = {"extra": "forbid"}

    cidr: str | None = Field(default=None, max_length=64)
    host: str | None = Field(default=None, max_length=253)
    port: int = Field(ge=1, le=65535)
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _one_target(self) -> "Reachable":
        if bool(self.cidr) == bool(self.host):
            raise ValueError("a reachable target names either cidr or host")
        return self


class ProfileBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=64)
    vpc_id: str = Field(pattern=VPC_ID)
    # ≥ 2 (AgentCore wants two AZs; preflight checks the AZs themselves)
    subnet_ids: list[str] = Field(min_length=2, max_length=8)
    security_group_ids: list[str] = Field(min_length=1, max_length=5)
    purpose: Literal["agents", "tools", "both"] = "agents"
    internet: Literal["none", "nat"] = "none"
    reachable: list[Reachable] = Field(default_factory=list, max_length=MAX_REACHABLE)

    @model_validator(mode="after")
    def _ids(self) -> "ProfileBody":
        _check_ids(self.subnet_ids, self.security_group_ids)
        return self


class ProfilePatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=64)
    vpc_id: str | None = Field(default=None, pattern=VPC_ID)
    subnet_ids: list[str] | None = Field(default=None, min_length=2, max_length=8)
    security_group_ids: list[str] | None = Field(default=None, min_length=1, max_length=5)
    purpose: Literal["agents", "tools", "both"] | None = None
    internet: Literal["none", "nat"] | None = None
    reachable: list[Reachable] | None = Field(default=None, max_length=MAX_REACHABLE)

    @model_validator(mode="after")
    def _ids(self) -> "ProfilePatch":
        _check_ids(self.subnet_ids or [], self.security_group_ids or [])
        return self


class ProjectProfilesBody(BaseModel):
    model_config = {"extra": "forbid"}

    profile_ids: list[str] = Field(default_factory=list, max_length=50)
    default_profile_id: str | None = None


def _check_ids(subnets: list[str], groups: list[str]) -> None:
    bad = [s for s in subnets if not re.match(SUBNET_ID, s)]
    bad += [g for g in groups if not re.match(SG_ID, g)]
    if bad:
        raise ValueError(f"not a subnet / security group id: {', '.join(bad)}")


def _fields(body: BaseModel) -> dict[str, Any]:
    out = body.model_dump(exclude_unset=True)
    if "reachable" in out and out["reachable"] is not None:
        out["reachable"] = [
            {k: v for k, v in item.items() if v not in (None, "")} for item in out["reachable"]
        ]
    return out


def _actor(request: Request) -> str:
    return require_identity(request).username if auth_enabled() else "river"


def _see_all(request: Request) -> bool:
    return require_identity(request).is_admin


def _project_id(request: Request) -> str | None:
    scope = getattr(request.state, "project", None)
    return scope.id if scope is not None else None


def _audit(ws: WorkspaceScope, request: Request, action: str, target: str) -> None:
    # workspace infrastructure: a workspace-level event, not one project's
    record_audit_event(workspace_id=ws.id, actor=_actor(request), action=action,
                       target=target, project_id=None)


@router.get("")
def list_profiles(
    request: Request, include_archived: bool = False, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    profiles = service.list_profiles(db, ws.id, _project_id(request), see_all=_see_all(request),
                                     include_archived=include_archived)
    return {"profiles": profiles}


@router.post("", status_code=201)
def create_profile(
    body: ProfileBody, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    created = service.create_profile(db, ws.id, _fields(body), _actor(request))
    _audit(ws, request, "network_profile.create", f"network_profile:{created['id']}")
    return created


@router.get("/projects/{project_id}")
def get_project_profiles(
    project_id: str, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return service.project_profiles(db, ws.id, project_id)


@router.put("/projects/{project_id}")
def put_project_profiles(
    project_id: str, body: ProjectProfilesBody, request: Request,
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    result = service.set_project_profiles(db, ws.id, project_id, body.profile_ids,
                                          body.default_profile_id)
    _audit(ws, request, "network_profile.project_set", f"project:{project_id}")
    return result


@router.get("/{profile_id}")
def get_profile(
    profile_id: str, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return service.get_profile(db, ws.id, profile_id, _project_id(request),
                               see_all=_see_all(request))


@router.patch("/{profile_id}")
def update_profile(
    profile_id: str, body: ProfilePatch, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    updated = service.update_profile(db, ws.id, profile_id, _fields(body))
    _audit(ws, request, "network_profile.update", f"network_profile:{profile_id}")
    return updated


@router.delete("/{profile_id}", status_code=204)
def archive_profile(
    profile_id: str, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> None:
    service.archive_profile(db, ws.id, profile_id)
    _audit(ws, request, "network_profile.archive", f"network_profile:{profile_id}")
