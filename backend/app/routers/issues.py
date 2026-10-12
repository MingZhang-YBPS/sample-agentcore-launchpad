"""The issue box (T36) and the intent view (T33) -- the business self-service loop's
read and bookkeeping API. Fixes are NOT performed here: a curated answer goes through
`/api/agents/{id}/rules`, dataset building through `/api/eval/datasets/from-sessions`,
documents through the knowledge-base routes. `/fixes` only records that one was applied."""

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
from app.services import intents, issues

router = APIRouter(prefix="/api", tags=["issues"])


class OpenIssue(BaseModel):
    agent_id: str = Field(max_length=32)
    session_id: str = Field(min_length=1, max_length=80)
    message_id: int = Field(ge=1)
    kind: Literal["unanswered", "manual"] = "manual"
    note: str | None = Field(default=None, max_length=1000)


class ResolveIssue(BaseModel):
    status: Literal["fixed", "wont_fix"]
    note: str | None = Field(default=None, max_length=1000)


class ReopenIssue(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class RecordFix(BaseModel):
    action: Literal["rule", "kb", "dataset"]
    ref: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=300)


def _actor(request: Request) -> str:
    return require_identity(request).username if auth_enabled() else "river"


@router.get("/intents")
def intent_view(
    agent_id: str | None = Query(default=None, max_length=32),
    days: int = Query(default=7, ge=1, le=30),
    lang: Literal["en", "zh-CN"] = "en",
    refresh: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    if agent_id:
        agent = db.get(Agent, agent_id)
        if agent is None or agent.workspace_id != ws.id:
            raise NotFoundError("agent.not_found", "agent not found")
    return intents.intent_view(db, ws.context, ws.id, agent_id=agent_id, days=days, lang=lang,
                               refresh=refresh)


@router.get("/issues")
def list_issues(
    status: Literal["open", "fixed", "wont_fix"] | None = None,
    agent_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=300),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return issues.list_issues(db, ws.id, status=status, agent_id=agent_id, limit=limit)


@router.post("/issues/sync")
def sync_issues(
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Open an issue for every thumbs-down that has none yet (idempotent)."""
    return {"opened": issues.sync_feedback(db, ws.id)}


@router.post("/issues", status_code=201)
def open_issue(
    req: OpenIssue, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    issue = issues.open_manual(
        db, workspace_id=ws.id, agent_id=req.agent_id, session_id=req.session_id,
        message_id=req.message_id, kind=req.kind, actor=_actor(request), note=req.note,
    )
    return issues.issue_out(issue)


@router.get("/issues/{issue_id}")
def get_issue(
    issue_id: str, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    issue = issues.get_issue(db, ws.id, issue_id)
    agent = db.get(Agent, issue.agent_id)
    return {
        **issues.issue_out(issue, {issue.agent_id: agent.name} if agent else None),
        "transcript": issues.transcript(db, issue),
    }


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(
    issue_id: str, req: ResolveIssue, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    issue = issues.get_issue(db, ws.id, issue_id)
    return issues.issue_out(issues.resolve(db, issue, req.status, _actor(request), req.note))


@router.post("/issues/{issue_id}/reopen")
def reopen_issue(
    issue_id: str, req: ReopenIssue, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    issue = issues.get_issue(db, ws.id, issue_id)
    return issues.issue_out(issues.reopen(db, issue, _actor(request), req.note))


@router.post("/issues/{issue_id}/fixes")
def record_fix(
    issue_id: str, req: RecordFix, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    issue = issues.get_issue(db, ws.id, issue_id)
    return issues.issue_out(
        issues.add_fix(db, issue, action=req.action, ref=req.ref, actor=_actor(request),
                       note=req.note)
    )
