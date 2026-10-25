"""Storyline reads (storyline-ux §2.3): one agent's facts, and whose turn it is.

``GET /api/agents/{agent_id}/story`` returns what the console's storyline ticks its
steps from (``services/agent_story.compute_facts``), cached 30 seconds. A foreign,
hidden or another project's agent answers 404 like a missing one.

``GET /api/me/next-steps`` lists, for every agent of the caller's project, the
current step when it is the caller's turn (their keys and workshop role) and the
others' turns they are waiting on. It sits under the hub-global ``/api/me`` prefix,
so the route policy resolves no workspace for it: the handler resolves the
``X-Workspace`` / ``X-Project`` pair itself with the same rules and reads inside
that project only. Read-only throughout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db
from app.core.errors import NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Agent
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace, resolve_workspace
from app.services import access, agent_access
from app.services import agent_story as story_svc
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api", tags=["storyline"])

# the home page lists at most this many agents' turns
MAX_AGENTS = 100


@router.get("/agents/{agent_id}/story")
def get_agent_story(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    project = getattr(request.state, "project", None)
    facts, cached = story_svc.agent_facts(db, ws.id, project.id if project else None, agent)
    return {
        "agent": story_svc.agent_out(agent),
        "facts": facts,
        "cached": cached,
        "ttl_seconds": int(story_svc.CACHE_TTL_SECONDS),
    }


def _request_project(request: Request, workspace_id: str) -> access.ProjectScope:
    identity = require_identity(request)
    requested = (request.headers.get(access.PROJECT_HEADER) or "").strip()
    db = SessionLocal()
    try:
        with project_context(None):
            project = access.resolve_project(db, identity, workspace_id, requested, "GET")
    finally:
        db.close()
    request.state.project = project
    return project


@router.get("/me/next-steps")
def my_next_steps(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    ws = resolve_workspace(request)
    project = _request_project(request, ws.id)
    viewer = Viewer.of_request(request)
    roles = story_svc.viewer_roles(project)
    mine: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    with project_context(project.id):
        agents = db.scalars(
            select(Agent).where(
                Agent.workspace_id == ws.id,
                Agent.status != "deleted",
                agent_access.agent_clause(viewer),
            ).order_by(Agent.created_at.desc()).limit(MAX_AGENTS)
        ).all()
        for agent in agents:
            if story_svc.story_kind(agent) != "agent":
                continue
            facts, _cached = story_svc.agent_facts(db, ws.id, project.id, agent)
            step = story_svc.current_step(facts)
            if step is None:
                continue
            row = {
                "agent": story_svc.agent_out(agent),
                "step": step.key,
                "chapter": step.chapter,
                "perm": step.perm,
                "roles": list(step.roles),
            }
            if story_svc.is_my_turn(step, roles=roles, can=project.can):
                mine.append(row)
            else:
                waiting.append(row)
    return {
        "workspace_id": ws.id,
        "project_id": project.id,
        "roles": sorted(roles),
        "mine": mine,
        "waiting": waiting,
        "computed_at": datetime.now(UTC).isoformat(),
    }
