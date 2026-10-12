"""Environment comparison and drift (T32).

Both routes are workspace-scoped (the current workspace is the one drift is measured in
and the one highlighted in the comparison). `compare` additionally reads the *other*
workspaces' ledger rows, so it filters them by the caller's grants exactly as
`GET /api/workspaces` does: an administrator sees every environment, a member only theirs.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.ledger import Workspace
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import environments
from app.services.agentcore.client import control_client
from app.services.workspace import granted_workspace_ids

router = APIRouter(prefix="/api/environments", tags=["environments"])


@router.get("/compare")
def compare(
    request: Request,
    agent: str = Query(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    rows = list(db.scalars(select(Workspace).order_by(Workspace.id)))
    if not identity.is_admin:
        granted = set(granted_workspace_ids(db, identity.user_id or ""))
        rows = [row for row in rows if row.id in granted]
    return environments.compare_agent(db, name=agent, workspaces=rows, current_id=ws.id)


@router.get("/drift")
def drift(
    agent: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Ledger expectation vs what AWS reports, for the current workspace. Read-only."""
    return environments.detect_drift(
        db, workspace_id=ws.id, control=control_client(ws.context), agent_name=agent
    )
