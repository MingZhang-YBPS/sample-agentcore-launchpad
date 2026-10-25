"""`GET /api/release-bundles/{id}/export` - the bundle as a Git-committable YAML file
(T31). Kept beside, not inside, `routers/promotions.py`: it only reads a bundle."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.models.ledger import ReleaseBundle
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import release_export

router = APIRouter(prefix="/api", tags=["release-export"])


@router.get("/release-bundles/{bundle_id}/export")
def export_bundle(
    bundle_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> Response:
    bundle = db.get(ReleaseBundle, bundle_id)
    if bundle is None or bundle.workspace_id != ws.id:
        raise AppError("promotion.bundle_not_found", "release bundle not found", status_code=404)
    return Response(
        content=release_export.export_yaml(bundle),
        media_type="application/yaml",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.agent_name}.release.yaml"',
            "X-Bundle-Digest": bundle.digest,
            "Cache-Control": "no-store",
        },
    )
