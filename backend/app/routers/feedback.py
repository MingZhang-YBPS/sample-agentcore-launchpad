"""Console thumbs feedback (T15): rate an answer, list the feedback, feed bad cases
to the evaluation flow.

`GET /api/feedback?verdict=down` returns `down_session_ids`, which the existing
"add to dataset" flow (`POST /api/eval/datasets/from-sessions`) takes as-is.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotFoundError
from app.models.ledger import Agent
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access
from app.services import feedback as feedback_service

router = APIRouter(
    prefix="/api",
    tags=["feedback"],
    # workspace isolation: a hidden `{agent_id}` / `{run_id}` reads as missing
    dependencies=[Depends(agent_access.require_path_ids_visible)],
)


class FeedbackRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=80)
    message_id: int = Field(ge=1)
    verdict: Literal["up", "down", "none"]  # "none" withdraws the caller's verdict
    comment: str | None = Field(default=None, max_length=feedback_service.MAX_COMMENT)


@router.post("/chat/{agent_id}/feedback")
def rate_answer(
    agent_id: str,
    req: FeedbackRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != ws.id:
        raise NotFoundError("agent.not_found", "agent not found")
    identity = require_identity(request)
    return feedback_service.record_feedback(
        db,
        workspace_id=ws.id,
        agent_id=agent.id,
        session_id=req.session_id,
        message_id=req.message_id,
        verdict=req.verdict,
        comment=req.comment,
        actor=identity.username if auth_enabled() else "river",
        source="console",
    )


@router.get("/feedback")
def list_feedback(
    verdict: Literal["up", "down"] | None = None,
    agent_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return feedback_service.list_feedback(
        db, ws.id, verdict=verdict, agent_id=agent_id, limit=limit
    )
