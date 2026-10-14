"""Tool reviews of hosted MCP servers (design projects §15.6, §15.13).

Reads are ``perm:project.use`` inside the request's project; deciding is
``perm:mcp.approve`` and never the deployer's own (409 ``mcp_review.self_decide``);
re-checking the live tool list for drift is ``perm:project.write``. A server of
another project (or one the viewer may not see) answers 404 like a missing one.

An approval re-creates the withheld Gateway target in the background
(``mcp_servers.start_publish``); the review's ``publish_status`` reports how that went.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent
from app.models.mcp_review import McpToolReview
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access, mcp_reviews, mcp_servers
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
