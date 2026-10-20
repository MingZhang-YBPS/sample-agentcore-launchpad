"""A project's quotas and monthly budget (projects design §11.3, G11-quota).

Hub-global like the rest of `/api/projects/{project_id}`: the project is the path's.
The read is every member's (and the project's department administrator's), with
usage and the budget's month-to-date spend, remaining and reset date — a non-member
gets 404 here (`visible_project`). The write is a platform administrator's only (no
department overlay, `core/route_policy.py`). The logic is in
`services/project_quotas.py`.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.services import project_quotas as svc
from app.services import projects as projects_svc

router = APIRouter(prefix="/api/projects", tags=["projects"])


class QuotasPut(BaseModel):
    # partial: a key set to null clears that quota; omitted keys stay
    quotas: dict[str, Any] = Field(default_factory=dict)


@router.get("/{project_id}/quotas")
def get_quotas(project_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = projects_svc.visible_project(db, require_identity(request), project_id)
    return svc.quotas_view(db, row)


@router.put("/{project_id}/quotas")
def put_quotas(
    project_id: str, body: QuotasPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = projects_svc.get_project(db, project_id)
    svc.update_quotas(db, identity, row, body.quotas)
    db.commit()
    return svc.quotas_view(db, row)
