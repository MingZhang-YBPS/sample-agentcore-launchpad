"""SME review (T34): console side manages review links; the public `/share/review` side is
what a domain expert with no account uses. See `services/review_links`.

The public routes live under `/share` beside the chat ones (PUBLIC + hub-global in
`route_policy`), so no console session and no `X-Workspace` header is ever read.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotFoundError
from app.models.ledger import Agent, ShareLink
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import display_name_of
from app.services import agent_access, review_links, share_links
from app.services import feedback as feedback_service

REVIEW_PAGE_PATH = "/r"

console_router = APIRouter(
    prefix="/api",
    tags=["review-links"],
    # workspace isolation: a hidden `{agent_id}` / `{run_id}` reads as missing
    dependencies=[Depends(agent_access.require_path_ids_visible)],
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _agent_in(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != ws.id:
        raise NotFoundError("agent.not_found", "agent not found")
    return agent


def _link_out(link: ShareLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "kind": link.kind,
        "agent_id": link.target_id,
        "label": link.label,
        "prefix": link.prefix,
        "state": share_links.link_state(link),
        "created_by": link.created_by,
        "expires_at": _iso(link.expires_at),
        "revoked_at": _iso(link.revoked_at),
        "last_used_at": _iso(link.last_used_at),
        "use_count": link.use_count or 0,
        "created_at": _iso(link.created_at),
    }


class CreateReviewLink(BaseModel):
    label: str = Field(default="", max_length=64)  # the reviewer, e.g. "HR — Wang Fang"
    expires_in_days: int | None = Field(default=None, ge=1, le=share_links.MAX_EXPIRY_DAYS)


@console_router.get("/agents/{agent_id}/review-links")
def list_review_links(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent_in(db, ws, agent_id)
    rows = (
        db.query(ShareLink)
        .filter(ShareLink.workspace_id == ws.id, ShareLink.target_id == agent_id,
                ShareLink.kind == review_links.KIND_REVIEW)
        .order_by(ShareLink.created_at.desc())
        .all()
    )
    return {"links": [_link_out(r) for r in rows]}


@console_router.post("/agents/{agent_id}/review-links", status_code=201)
def create_review_link(
    agent_id: str,
    req: CreateReviewLink,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent_in(db, ws, agent_id)
    identity = require_identity(request)
    link, raw = review_links.create(
        db, workspace_id=ws.id, agent=agent, label=req.label,
        created_by=identity.username if auth_enabled() else "river",
        expires_in_days=req.expires_in_days,
    )
    path = f"{REVIEW_PAGE_PATH}/{raw}"
    origin = (request.headers.get("origin") or "").rstrip("/")
    # like a chat link: the token is shown once; revoke with POST /api/share-links/{id}/revoke
    return {**_link_out(link), "token": raw, "path": path, "url": f"{origin}{path}"}


# ── public ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/share/review", tags=["share-review"])


class ReviewRating(BaseModel):
    message_id: int = Field(ge=1)
    verdict: Literal["up", "down", "none"]
    comment: str | None = Field(default=None, max_length=feedback_service.MAX_COMMENT)
    correction: str | None = Field(default=None, max_length=feedback_service.MAX_CORRECTION)


@router.get("/{token}", summary="The review queue behind a review link")
def review_queue(token: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    link, agent = review_links.resolve(db, token)
    review_links.enforce_rate_limit(link.id)
    share_links.record_use(link.id)
    return {
        "kind": link.kind,
        "label": link.label,
        "agent": {"display_name": display_name_of(agent.spec) or agent.name},
        "expires_at": _iso(link.expires_at),
        "items": review_links.queue(db, link, agent),
    }


@router.post("/{token}/rate", summary="Rate one answer, optionally with a correction")
def review_rate(token: str, req: ReviewRating, db: Session = Depends(get_db)) -> dict[str, Any]:
    link, agent = review_links.resolve(db, token)
    review_links.enforce_rate_limit(link.id)
    session_id = review_links.answer_session(db, link, agent, req.message_id)
    return feedback_service.record_feedback(
        db,
        workspace_id=link.workspace_id or "",
        agent_id=agent.id,
        session_id=session_id,
        message_id=req.message_id,
        verdict=req.verdict,
        comment=req.comment,
        correction=req.correction,
        actor=review_links.reviewer_actor(link.id),
        source="review",
    )
