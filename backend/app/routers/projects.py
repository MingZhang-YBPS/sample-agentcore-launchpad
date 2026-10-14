"""Project administration API (projects design §7.10).

Hub-global: a project spans workspaces, so nothing here reads `X-Workspace` or
`X-Project` — the project is the one named in the path. Who may do what is in
`core/route_policy.py` (`ADMIN` for structure, `pperm:project.members` for the
project's own members and roles, checked against the path's project); the
membership-checked reads answer 404 to non-members here (`visible_project`).
The logic lives in `services/projects.py`; this module is request shapes only.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.routers.auth import require_identity
from app.services import access
from app.services import projects as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])
templates_router = APIRouter(prefix="/api/role-templates", tags=["projects"])
me_router = APIRouter(prefix="/api/me", tags=["projects"])


class ProjectCreate(BaseModel):
    id: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    department_id: str | None = Field(default=None, max_length=32)
    group_id: str | None = Field(default=None, max_length=32)
    workspaces: list[str] = Field(min_length=1, max_length=50)
    owners: list[str] = Field(default_factory=list, max_length=50)  # users.id
    settings: dict[str, Any] | None = None


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    department_id: str | None = Field(default=None, max_length=32)
    group_id: str | None = Field(default=None, max_length=32)
    settings: dict[str, Any] | None = None


class BindingsPut(BaseModel):
    bind: list[str] = Field(default_factory=list, max_length=50)
    unbind: list[str] = Field(default_factory=list, max_length=50)
    # make this project the workspace default (what a header-less request means)
    make_default: list[str] = Field(default_factory=list, max_length=50)


class MemberUpsert(BaseModel):
    user_id: str = Field(min_length=1, max_length=32)
    role_key: str = Field(min_length=1, max_length=32)
    overrides: dict[str, bool] | None = None


class MembersPut(BaseModel):
    upsert: list[MemberUpsert] = Field(default_factory=list, max_length=500)
    remove: list[str] = Field(default_factory=list, max_length=500)


class RolePut(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    permissions: list[str] = Field(default_factory=list, max_length=64)
    based_on: str | None = Field(default=None, max_length=32)


class TransferBody(BaseModel):
    kind: str = Field(default="agent", max_length=16)
    resource_id: str = Field(min_length=1, max_length=64)
    to_project_id: str = Field(min_length=2, max_length=32)


def _detail(db: Session, identity: Any, row: Any) -> dict[str, Any]:
    return {**svc.project_view(db, row),
            "my_permissions": svc.caller_permissions(db, identity, row.id)}


# ── the caller's access, templates ────────────────────────────────────────────


@me_router.get("/access")
def my_access(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    return svc.access_view(db, require_identity(request))


@templates_router.get("")
def role_templates() -> dict[str, Any]:
    return svc.role_templates_view()


# ── projects ──────────────────────────────────────────────────────────────────


@router.get("")
def list_projects(
    request: Request,
    department_id: str | None = Query(default=None, max_length=32),
    group_id: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=16),
    workspace_id: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    return {"projects": svc.list_projects(
        db, identity, department_id=department_id, group_id=group_id, status=status,
        workspace_id=workspace_id, q=q,
    )}


@router.post("", status_code=201)
def create_project(
    body: ProjectCreate, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.create_project(
        db, identity, project_id=body.id, name=body.name, description=body.description,
        department_id=body.department_id, group_id=body.group_id,
        workspaces=body.workspaces, owners=body.owners, settings=body.settings,
    )
    db.commit()
    for ws in sorted(set(body.workspaces)):
        _seed_samples(db, ws, row.id)
    return _detail(db, identity, row)


@router.get("/{project_id}")
def get_project(project_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    identity = require_identity(request)
    return _detail(db, identity, svc.visible_project(db, identity, project_id))


@router.patch("/{project_id}")
def patch_project(
    project_id: str, body: ProjectPatch, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    svc.update_project(db, identity, row, body.model_dump(exclude_unset=True))
    db.commit()
    return _detail(db, identity, row)


@router.post("/{project_id}/archive")
def archive_project(
    project_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    effects = svc.archive_project(db, identity, row)
    db.commit()
    return {**_detail(db, identity, row), **effects}


@router.post("/{project_id}/unarchive")
def unarchive_project(
    project_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    svc.unarchive_project(db, identity, row)
    db.commit()
    return _detail(db, identity, row)


# ── workspace bindings ────────────────────────────────────────────────────────


@router.get("/{project_id}/workspaces")
def list_bindings(
    project_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    svc.visible_project(db, require_identity(request), project_id)
    return {"workspaces": svc.bindings_view(db, project_id)}


@router.put("/{project_id}/workspaces")
def put_bindings(
    project_id: str, body: BindingsPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    added = svc.update_bindings(db, identity, row, bind=body.bind, unbind=body.unbind,
                                make_default=body.make_default)
    db.commit()
    for ws in added:
        _seed_samples(db, ws, project_id)
    return {"workspaces": svc.bindings_view(db, project_id), "bound": added}


def _seed_samples(db: Session, workspace_id: str, project_id: str) -> None:
    """A newly bound project gets its own Skill Lab sample task sets (best effort —
    the binding itself is already committed)."""
    from app.skill_lab import samples

    try:
        samples.ensure_project_samples(db, workspace_id, project_id)
    except Exception:  # noqa: BLE001 — seeding must never fail a committed binding
        db.rollback()
        logger.warning("skill lab samples for project %s in %s skipped", project_id,
                       workspace_id, exc_info=True)


# ── members ───────────────────────────────────────────────────────────────────


@router.get("/{project_id}/members")
def list_members(
    project_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.visible_project(db, identity, project_id)
    return {"members": svc.members_view(
        db, row, full_email=access.can_manage_structure(identity),
    )}


@router.put("/{project_id}/members")
def put_members(
    project_id: str, body: MembersPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    result = svc.upsert_members(
        db, identity, row, [m.model_dump() for m in body.upsert], body.remove,
    )
    db.commit()
    return {**result, "members": svc.members_view(
        db, row, full_email=access.can_manage_structure(identity),
    )}


@router.delete("/{project_id}/members/{user_id}", status_code=204)
def delete_member(
    project_id: str, user_id: str, request: Request, db: Session = Depends(get_db),
) -> None:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    svc.upsert_members(db, identity, row, [], [user_id])
    db.commit()


@router.get("/{project_id}/candidates")
def member_candidates(
    project_id: str, q: str = Query(default="", max_length=64), db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_project(db, project_id)
    return {"candidates": svc.candidates(db, row, q)}


# ── custom roles ──────────────────────────────────────────────────────────────


@router.get("/{project_id}/roles")
def list_roles(project_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = svc.visible_project(db, require_identity(request), project_id)
    return svc.roles_view(db, row)


@router.put("/{project_id}/roles/{role_key}")
def put_role(
    project_id: str, role_key: str, body: RolePut, request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    svc.upsert_role(db, identity, row, role_key, name=body.name,
                    permissions=body.permissions, based_on=body.based_on)
    db.commit()
    return svc.roles_view(db, row)


@router.delete("/{project_id}/roles/{role_key}", status_code=204)
def delete_role(
    project_id: str, role_key: str, request: Request, db: Session = Depends(get_db),
) -> None:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    svc.delete_role(db, identity, row, role_key)
    db.commit()


# ── moving resources between projects ─────────────────────────────────────────


@router.post("/{project_id}/transfer")
def transfer(
    project_id: str, body: TransferBody, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = svc.get_project(db, project_id)
    if body.kind not in svc.TRANSFER_KINDS:
        raise AppError("project.transfer_kind",
                       f"phase 1 moves {list(svc.TRANSFER_KINDS)} only",
                       {"kind": body.kind, "supported": list(svc.TRANSFER_KINDS)})
    result = svc.transfer_agent(db, identity, row, body.resource_id, body.to_project_id)
    db.commit()
    return result
