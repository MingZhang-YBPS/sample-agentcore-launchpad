"""Fleet overview, governance health and the template marketplace (T37–T39).

Three surfaces that all answer "across the platform, what should I look at" rather than the
per-workspace question the rest of the console answers.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.models.ledger import Agent, SharedTemplate
from app.routers.auth import Identity, current_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import fleet as fleet_service
from app.services import marketplace as marketplace_service

router = APIRouter(prefix="/api", tags=["fleet"])


class PublishRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=2000)


# ── T37: fleet overview ──────────────────────────────────────────────────────────


@router.get("/fleet")
def get_fleet(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every workspace in one table — agents, failures, pending releases, firing alerts.

    A ledger read by design: fanning Logs Insights across every account would be slow,
    billed per scan, and would fail the whole page when one spoke's role lapsed. A
    workspace that is not usable reports `readable: false` rather than zeroes.
    """
    return fleet_service.fleet_overview(db)


# ── T39: governance health ───────────────────────────────────────────────────────


@router.get("/governance/health")
def get_governance_health(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Findings for this workspace, each with the link that fixes it, plus a score."""
    return fleet_service.governance_health(db, ws.id)


# ── T38: the template marketplace ────────────────────────────────────────────────


@router.get("/marketplace/templates")
def list_templates(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Every published template, from any workspace — that is the point of publishing."""
    return {"templates": marketplace_service.catalogue(db, own_workspace=ws.id)}


@router.post("/marketplace/templates", status_code=201)
def publish_template(
    req: PublishRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Publish one of this workspace's active agents as a reusable template."""
    agent = db.get(Agent, req.agent_id)
    if agent is None or agent.status == "deleted" or agent.workspace_id != ws.id:
        raise AppError("agent.not_found", "agent not found", status_code=404)
    row = marketplace_service.publish(
        db,
        agent,
        workspace_id=ws.id,
        title=req.title,
        summary=req.summary,
        published_by=identity.username,
    )
    db.commit()
    return marketplace_service.entry_out(row, own_workspace=ws.id)


def _entry(db: Session, template_id: str) -> SharedTemplate:
    row = db.get(SharedTemplate, template_id)
    if row is None:
        raise AppError("marketplace.not_found", "template not found", status_code=404)
    return row


@router.post("/marketplace/templates/{template_id}/use")
def use_template(
    template_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Take a copy: returns the wizard defaults and what the consumer must still supply.

    Deliberately does **not** create an agent. The consumer still goes through the wizard,
    sees every field, and supplies their own knowledge bases and tools — a template that
    silently deployed someone else's shape into your account would be the wrong tradeoff.
    """
    row = _entry(db, template_id)
    marketplace_service.record_use(db, row)
    db.commit()
    return marketplace_service.entry_out(row, own_workspace=ws.id)


@router.delete("/marketplace/templates/{template_id}")
def unpublish_template(
    template_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Only the publishing workspace may withdraw its own template (admins may always)."""
    row = _entry(db, template_id)
    if row.source_workspace_id != ws.id and not identity.is_admin:
        raise AppError(
            "marketplace.not_yours",
            "only the workspace that published a template can withdraw it",
            status_code=403,
        )
    marketplace_service.unpublish(db, row)
    db.commit()
    return {"deleted": template_id}
