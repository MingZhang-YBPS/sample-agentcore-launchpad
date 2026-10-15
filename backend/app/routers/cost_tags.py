"""Cost allocation tags and the billing-basis cost view (cost-tags design §1.4, §1.6, CT-2).

Three surfaces, the logic in `services/billing_costs.py`:

* `/api/cost-tags/*` — the workspace's tag set and its cost allocation activation
  state, and the one-click activation (administrators; an organization member
  account gets 409 with the management account to ask). Workspace-scoped, no
  project: activation is the account's billing configuration.
* `/api/cost-tags/backfill` — the workspace-wide back-fill run (CT-1's resumable job
  `cost_tags_backfill`, §1.3): start one, read the latest with its coverage
  (administrators, data: it lists every project's resources).
* `/api/costs/billing` — what AWS billed per `launchpad:project`, next to the
  estimate of `/api/costs` and classified like it (administrators, cost detail).
* `/api/projects/{project_id}/cost-tags` and `/budget-basis` — a project's custom
  cost tags (its owner, `project.members`) and its budget's basis (administrators
  and its department administrator, like the quota it qualifies). Hub-global like
  the rest of `/api/projects/{project_id}`; a non-member gets 404.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.alerts import _bound_project
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import billing_costs as svc
from app.services import cost_tags, project_quotas
from app.services import projects as projects_svc

router = APIRouter(prefix="/api", tags=["cost-tags"])


class ActivateBody(BaseModel):
    # omitted: every key of the set that is inactive
    keys: list[str] | None = Field(default=None, max_length=50)


class CostTagsPut(BaseModel):
    cost_tags: dict[str, Any] = Field(default_factory=dict)


class BudgetBasisPut(BaseModel):
    basis: Literal["estimate", "billing"]


@router.get("/cost-tags")
def get_cost_tags(
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The tag set, each key's activation state, and what activation needs here."""
    return {
        "tag_set": list(svc.TAG_SET),
        "shared_value": svc.SHARED_VALUE,
        "custom_max": svc.MAX_CUSTOM_TAGS,
        "reserved_prefixes": list(svc.RESERVED_PREFIXES),
        "activation": svc.activation_status(db, ws.context, force=force),
    }


@router.post("/cost-tags/activate")
def activate_cost_tags(
    body: ActivateBody,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    from app.services.audit import record_audit_event

    result = svc.activate(db, ws.context, body.keys)
    if result["activated"]:
        record_audit_event(workspace_id=ws.id, actor=require_identity(request).username,
                           action="cost_tags.activate",
                           target=",".join(result["activated"]), db=db)
        db.commit()
    return result


@router.get("/costs/billing")
def get_billing_costs(
    granularity: str = Query(default=svc.DEFAULT_GRANULARITY),
    project: str | None = Query(default=None, max_length=32),
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Billed cost per project and service, daily or monthly, cached 6 hours.

    `project` narrows it to one project bound here (another answers 404), with its
    share of the shared infrastructure row; the activation state rides along so the
    page can say why everything is untagged."""
    if project is not None:
        _bound_project(db, ws, project)
    report = svc.billing_report(db, ws.context, granularity=granularity, force=force,
                                project=project)
    try:
        status = svc.activation_status(db, ws.context)
        report["project_tag_active"] = status["project_tag_active"]
        report["activation_checked"] = status["error"] is None
    except Exception:  # noqa: BLE001 - the billed rows stand without it
        report["project_tag_active"] = None
        report["activation_checked"] = False
    return report


# ── back-fill (cost-tags §1.3, CT-1's resumable job `cost_tags_backfill`) ─────


@router.post("/cost-tags/backfill")
def start_cost_tag_backfill(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Run the workspace-wide back-fill: every ledger agent's resources and the
    workspace infrastructure get the standard tag set. An identical run still queued
    is reused; the view of the run is answered at once."""
    from app.services.audit import record_audit_event

    identity = require_identity(request)
    job = cost_tags.start_backfill(db, identity, ws.id)
    record_audit_event(workspace_id=ws.id, actor=identity.username,
                       action="cost_tags.backfill", target=job.id, db=db)
    db.commit()
    cost_tags.start_backfill_async(job.id)
    return cost_tags.run_view(job)


@router.get("/cost-tags/backfill")
def get_cost_tag_backfill(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The tag set and the workspace's latest workspace-wide run with its coverage
    and the resources not tagged (`run` is null before the first)."""
    return cost_tags.backfill_view(db, ws.id)


# ── per project ───────────────────────────────────────────────────────────────


def _project_view(row: Any) -> dict[str, Any]:
    return {
        "project_id": row.id,
        "cost_tags": svc.custom_tags(row),
        "max": svc.MAX_CUSTOM_TAGS,
        "reserved_prefixes": list(svc.RESERVED_PREFIXES),
        "budget_basis": project_quotas.budget_basis(row),
        "budget_bases": list(project_quotas.BUDGET_BASES),
    }


@router.get("/projects/{project_id}/cost-tags")
def get_project_cost_tags(
    project_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = projects_svc.visible_project(db, require_identity(request), project_id)
    return _project_view(row)


@router.put("/projects/{project_id}/cost-tags")
def put_project_cost_tags(
    project_id: str, body: CostTagsPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = projects_svc.get_project(db, project_id)
    svc.update_custom_tags(db, require_identity(request), row, body.cost_tags)
    db.commit()
    cost_tags.start_queued(db)
    return _project_view(row)


@router.put("/projects/{project_id}/budget-basis")
def put_budget_basis(
    project_id: str, body: BudgetBasisPut, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = projects_svc.get_project(db, project_id)
    project_quotas.update_budget_basis(db, require_identity(request), row, body.basis)
    db.commit()
    return _project_view(row)
