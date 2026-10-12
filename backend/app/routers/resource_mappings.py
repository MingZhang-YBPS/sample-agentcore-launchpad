"""Logical resource mapping routes (roadmap T23).

Reads and writes act on the workspace the caller has SELECTED (`X-Workspace`) — the
environment the mapped resources live in, i.e. a promotion's target. Reading is `MEMBER`:
a developer needs to see what a bundle will resolve to. Writing needs
`perm:promotion.approve` (operators and administrators): a mapping decides which of the
target environment's resources a promoted agent will be wired to, which is the approver's
call, and a builder who could edit it could redirect a release at any resource in prod.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.models.ledger import ReleaseBundle
from app.routers.auth import Identity, current_identity
from app.routers.workspaces import WorkspaceScope, authorized_workspace, require_workspace
from app.services import resource_mapping as mapping_service
from app.services.audit import record_audit_event

router = APIRouter(prefix="/api", tags=["resource-mappings"])


class MappingBody(BaseModel):
    resource_id: str = Field(min_length=1, max_length=512)
    note: str | None = Field(default=None, max_length=1000)


@router.get("/resource-mappings")
def list_resource_mappings(
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    return {
        "workspace_id": ws.id,
        "kinds": list(mapping_service.KINDS),
        "mappings": [
            mapping_service.mapping_out(r) for r in mapping_service.list_mappings(db, ws.id)
        ],
    }


@router.put("/resource-mappings/{kind}/{name}")
def put_resource_mapping(
    kind: str,
    name: str,
    body: MappingBody,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Create or replace one mapping (`kind:name` -> a resource id in this workspace)."""
    row = mapping_service.upsert_mapping(
        db,
        workspace_id=ws.id,
        kind=kind,
        name=name,
        resource_id=body.resource_id,
        note=body.note,
        updated_by=identity.username,
    )
    record_audit_event(
        workspace_id=ws.id,
        actor=identity.username,
        action="resource_mapping.put",
        target=f"{kind}:{name} -> {row.resource_id}",
        db=db,
    )
    db.commit()
    return mapping_service.mapping_out(row)


@router.delete("/resource-mappings/{kind}/{name}")
def delete_resource_mapping(
    kind: str,
    name: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    if not mapping_service.delete_mapping(db, workspace_id=ws.id, kind=kind, name=name):
        raise AppError("mapping.not_found", "mapping not found", status_code=404)
    record_audit_event(
        workspace_id=ws.id,
        actor=identity.username,
        action="resource_mapping.delete",
        target=f"{kind}:{name}",
        db=db,
    )
    db.commit()
    return {"deleted": True, "key": f"{kind}:{name}"}


@router.get("/release-bundles/{bundle_id}/resolution")
def bundle_resolution(
    bundle_id: str,
    target_workspace_id: str = Query(min_length=1, max_length=32),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """What promoting this bundle into `target_workspace_id` would rewrite, and what is
    still unmapped. Read-only; the rewritten spec is returned for review, never applied."""
    bundle = db.get(ReleaseBundle, bundle_id)
    if bundle is None or bundle.workspace_id != ws.id:
        raise AppError("promotion.bundle_not_found", "release bundle not found", status_code=404)
    # the target's resource map and mappings are the TARGET workspace's data
    target = authorized_workspace(db, identity, target_workspace_id)
    resolution = mapping_service.resolve_spec(
        db,
        bundle.spec or {},
        source_workspace_id=ws.id,
        target_workspace_id=target.id,
        source_resources=ws.row.resources,
        target_resources=target.resources,
    )
    return {
        "target_workspace_id": target.id,
        "complete": resolution.complete,
        "resolved": resolution.resolved,
        "unmapped": resolution.unmapped,
        "spec": resolution.spec,
    }
