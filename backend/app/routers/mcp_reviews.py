"""Tool reviews of hosted MCP servers (design projects §15.6, §15.13).

Reads are ``perm:project.use`` inside the request's project; deciding is
``perm:mcp.approve`` and never the deployer's own (409 ``mcp_review.self_decide``);
re-checking the live tool list for drift is ``perm:project.write``. A server of
another project (or one the viewer may not see) answers 404 like a missing one.

An approval re-creates the withheld Gateway target in the background
(``mcp_servers.start_publish``); the review's ``publish_status`` reports how that went.

Server operations (§15.7, §15.8): ``POST …/health`` runs one health check now
(``perm:project.write``); ``POST …/rollback`` returns the target to an earlier approved
version (``perm:release.sign`` for a gated agent's live endpoint, ``perm:agents.deploy``
otherwise — checked here, the table only demands participation); ``POST
…/policies/scaffold`` writes the Cedar forbid / permit pair (``perm:mcp.approve``), and a
mode switch in the same request is shared infrastructure (``infra+perm:mcp.approve``).
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.actor import user_id_for
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent
from app.models.mcp_review import McpToolReview
from app.routers.auth import require_identity, require_permission
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import (
    access,
    agent_access,
    governance,
    governance_evidence,
    mcp_reviews,
    mcp_servers,
)
from app.services.agentcore.client import control_client
from app.services.observability import cw_client
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


class DecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    note: str = Field(default="", max_length=mcp_reviews.MAX_NOTE)


def _server(db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer) -> Agent:
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    if (agent.spec or {}).get("protocol") != "mcp":
        raise AppError(
            "agent.not_mcp_server", "this agent is not an MCP server", status_code=409
        )
    return agent


def _review(db: Session, agent: Agent, review_id: str) -> McpToolReview:
    row = db.get(McpToolReview, review_id)
    if row is None or row.agent_id != agent.id:
        raise NotFoundError("mcp_review.not_found", "tool review not found")
    return row


def _detail(db: Session, review: McpToolReview) -> dict[str, Any]:
    out = mcp_reviews.review_out(review)
    base = db.get(McpToolReview, review.base_review_id) if review.base_review_id else None
    # the approved list it is compared with, for a side-by-side reading
    out["base_tools"] = (base.tools or []) if base is not None else None
    return out


@router.get("/{agent_id}/reviews")
def list_reviews(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = _server(db, ws, agent_id, viewer)
    return {
        "agent_id": agent.id,
        **mcp_reviews.state(db, agent.id),
        "reviews": [
            mcp_reviews.review_out(r, full=False) for r in mcp_reviews.list_reviews(db, agent.id)
        ],
    }


@router.get("/{agent_id}/reviews/{review_id}")
def get_review(
    agent_id: str,
    review_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = _server(db, ws, agent_id, viewer)
    return _detail(db, _review(db, agent, review_id))


def _decide(
    request: Request, db: Session, ws: WorkspaceScope, viewer: Viewer, agent_id: str,
    review_id: str, body: DecisionIn, *, approve: bool,
) -> dict[str, Any]:
    agent = _server(db, ws, agent_id, viewer)
    review = mcp_reviews.decide(
        db, _review(db, agent, review_id), actor=require_identity(request).username,
        approve=approve, note=body.note,
    )
    db.commit()
    if approve:
        mcp_servers.start_publish(ws.context, agent, review.id)
    return _detail(db, review)


@router.post("/{agent_id}/reviews/{review_id}/approve")
def approve_review(
    agent_id: str,
    review_id: str,
    body: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Approve the tool list; the withheld target is re-created in the background."""
    return _decide(request, db, ws, viewer, agent_id, review_id, body, approve=True)


@router.post("/{agent_id}/reviews/{review_id}/reject")
def reject_review(
    agent_id: str,
    review_id: str,
    body: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Reject the tool list; the target stays withheld until a new deploy is approved."""
    return _decide(request, db, ws, viewer, agent_id, review_id, body, approve=False)


@router.post("/{agent_id}/reviews/check")
def check_reviews(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Re-sync the live target and compare its tools with the approved list (drift)."""
    agent = _server(db, ws, agent_id, viewer)
    found = mcp_servers.check_drift(ws.context, agent)
    db.expire_all()
    return {**found, **mcp_reviews.state(db, agent.id)}


# ── server operations (§15.7, §15.8) ──────────────────────────────────────────


def _state_out(db: Session, agent: Agent) -> dict[str, Any]:
    row = mcp_servers.load_state(db, agent)
    db.commit()
    return {"qualifier": mcp_servers.current_qualifier(agent), "live_version": row.live_version,
            "health": row.health or {}, "operation": row.operation or {}}


@router.post("/{agent_id}/health")
def check_health(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """One health check now: follow the live endpoint, re-sync, compare with the
    approved list (a drift withholds the target), record the outcome."""
    agent = _server(db, ws, agent_id, viewer)
    health = mcp_servers.check_health(ws.context, agent)
    db.expire_all()
    return {"health": health, **mcp_reviews.state(db, agent.id)}


class RollbackIn(BaseModel):
    model_config = {"extra": "forbid"}

    # the runtime version to return to; default: the newest other approved one
    version: str | None = Field(default=None, min_length=1, max_length=16)


@router.post("/{agent_id}/rollback", status_code=202)
def rollback(
    agent_id: str,
    body: RollbackIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Point the target at an earlier version's endpoint and restore that version's
    approved tool list (in the background; progress in ``operation``)."""
    agent = _server(db, ws, agent_id, viewer)
    # moving a gated agent's live endpoint is a release decision; DEFAULT is a deploy's
    require_permission(
        request, "release.sign" if agent.endpoint_mode == mcp_servers.LIVE else "agents.deploy"
    )
    review = mcp_servers.plan_rollback(db, agent, body.version)
    actor = require_identity(request).username
    mcp_servers.start_rollback(ws.context, agent, review.id, actor=actor,
                               actor_user_id=user_id_for(actor))
    db.expire_all()
    return {"version": review.version, "review_id": review.id, **_state_out(db, agent)}


class ScaffoldIn(BaseModel):
    model_config = {"extra": "forbid"}

    dry_run: bool = False
    # optional mode switch after the policies (shared infrastructure, §15.7)
    mode: Literal["LOG_ONLY", "ENFORCE"] | None = None
    confirmation_name: str | None = Field(default=None, max_length=100)
    override_reason: str | None = Field(default=None, max_length=1000)
    evidence_range: Literal["1h", "6h", "24h", "7d"] = "24h"
    acknowledged_gateway_ids: list[str] = Field(default_factory=list, max_length=100)


@router.post("/{agent_id}/policies/scaffold")
def scaffold_policies(
    agent_id: str,
    body: ScaffoldIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> Any:
    """Plan (``dry_run``) or apply the server's Cedar forbid / permit pair."""
    from app.core.route_policy import require_infra

    agent = _server(db, ws, agent_id, viewer)
    inputs = mcp_servers.scaffold_inputs(db, agent.id)
    target_name = str(((agent.spec or {}).get("mcp") or {}).get("target_name") or "")
    gateway_id = str(ws.context.resources.get("gateway_id") or "")
    if not gateway_id:
        raise AppError("gateway.not_bootstrapped",
                       "gateway_id missing from this workspace's resource map — run its bootstrap",
                       status_code=503)
    active = [b for b in access.workspace_bindings(db, ws.id) if b.status == "active"]
    plan = governance.mcp_scaffold_plan(
        control_client(ws.context), gateway_id, target_name=target_name,
        approved_tools=inputs["approved_tools"], open_tools=inputs["open_tools"],
        # one project: every principal is that project's — no condition (today's shape)
        project_id=agent.project_id if len(active) > 1 else None,
    )
    plan.update(approved_review_id=inputs["approved_review_id"],
                open_review_id=inputs["open_review_id"])
    if body.mode is not None:
        require_infra(request, "infra+perm:mcp.approve")
    if body.dry_run:
        return {"plan": plan}
    evidence = 0
    if body.mode == "ENFORCE":
        evidence = governance_evidence.evidence_count(cw_client(ws.context), gateway_id,
                                                      body.evidence_range)
    mode = None if body.mode is None else {
        "mode": body.mode, "confirmation_name": body.confirmation_name,
        "override_reason": body.override_reason, "evidence_range": body.evidence_range,
    }
    mcp_servers.start_scaffold(
        ws.context, agent, plan, target_name=target_name,
        acknowledged_gateway_ids=body.acknowledged_gateway_ids, mode=mode,
        evidence_count=evidence, actor=require_identity(request).username,
    )
    db.expire_all()
    return JSONResponse({"plan": plan, **_state_out(db, agent)}, status_code=202)
