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


class BudgetPut(BaseModel):
    # partial: an omitted field stays; `monthly_budget_usd: null` clears the budget.
    # Typed loosely on purpose: the service refuses a bool / string amount with
    # `project.budget_invalid` (pydantic would coerce them).
    monthly_budget_usd: Any = None
    allow_overrun: Any = None


@router.put("/{project_id}/budget")
def put_budget(
    project_id: str, body: BudgetPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The project's monthly budget and overrun switch: a platform administrator or
    an administrator of the project's department (`dadmin` overlay), allocated from
    the department's budget (`services/department_budgets.py`)."""
    from app.services import department_budgets

    identity = require_identity(request)
    row = projects_svc.get_project(db, project_id)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("allow_overrun", False) is None:
        fields.pop("allow_overrun")
    department_budgets.update_project_budget(db, identity, row, fields)
    db.commit()
    return svc.quotas_view(db, row)
