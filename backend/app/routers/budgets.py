"""The budget dashboards above one project (projects §11.3).

Hub-global: budgets and spend span workspaces. `GET /api/budgets/platform` is the
platform cost page (every department and project, and the platform / unattributed
line) — platform administrators only, cost detail like `/api/costs` (a steward is
refused). `GET /api/budgets/me` tells any signed-in account which cost views it can
open and lists the department budget alerts firing for it; the rail uses it to show
only the entries the caller can open. The department page is
`GET /api/departments/{id}/budget`; the project page reads
`GET /api/projects/{id}/quotas`. The logic is `services/department_budgets.py`.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.services import department_budgets

router = APIRouter(prefix="/api/budgets", tags=["projects"])


@router.get("/platform")
def platform(db: Session = Depends(get_db)) -> dict[str, Any]:
    return department_budgets.platform_view(db)


@router.get("/me")
def mine(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    return department_budgets.me_view(db, require_identity(request))
