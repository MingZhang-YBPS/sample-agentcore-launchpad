"""Calling an agent from your own backend (design invoke-access §0.6).

``GET /api/agents/{agent_id}/invoke-access`` — workspace- and project-scoped like
every agent read (`perm:project.use`): the agent's ARNs, mode, least-privilege policy
and samples. A foreign, hidden or another project's agent answers 404.

``GET /api/projects/{project_id}/invoke-access`` — hub-global under
``/api/projects`` (`pperm:project.use` on the PATH project; a non-member 404): every
callable agent of the project across the workspaces it is bound to, and one merged
policy; ``format=csv|json`` downloads the inventory. Read-only throughout; nothing
here calls AWS (``services/invoke_access``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Agent, Workspace
from app.models.projects import Project, ProjectWorkspace
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access, visibility
from app.services import invoke_access as svc
from app.services import projects as projects_svc
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api", tags=["invoke-access"])

MAX_AGENTS = 500  # a project inventory's cap; narrow with ?agents= beyond it


@router.get("/agents/{agent_id}/invoke-access")
def agent_invoke_access(
    agent_id: str,
    qualifier: str | None = Query(default=None, max_length=48),
    include: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return svc.agent_access(agent, ws.row, qualifier=qualifier or None,
                            include=svc.parse_include(include))


def _project_agents(
    db: Session, request: Request, project_id: str, wanted: list[str],
) -> list[tuple[Agent, Workspace | None]]:
    """The project's agents in every workspace it is bound to, each workspace's
    visibility rule applied to the caller. ``wanted`` ids outside that set: 404."""
    identity = require_identity(request)
    with project_context(None):
        if db.scalar(select(Project.id).where(Project.id == project_id)) is None:
            raise projects_svc.not_found(project_id)  # an administrator's unknown id
        workspace_ids = list(db.scalars(
            select(ProjectWorkspace.workspace_id).where(
                ProjectWorkspace.project_id == project_id)
        ).all())
        workspaces = {w.id: w for w in db.scalars(
            select(Workspace).where(Workspace.id.in_(workspace_ids))).all()}
    project_mode = visibility.project_mode(project_id)
    out: list[tuple[Agent, Workspace | None]] = []
    with project_context(project_id):
        for ws_id in workspace_ids:
            effective = project_mode or visibility.mode(workspaces.get(ws_id))
            viewer = Viewer(user_id=identity.user_id,
                            filters=effective == visibility.ISOLATED and not identity.is_admin,
                            project_id=project_id)
            query = select(Agent).where(
                Agent.workspace_id == ws_id,
                Agent.status != "deleted",
                Agent.system_key.is_(None),
                agent_access.agent_clause(viewer),
            )
            if wanted:
                query = query.where(Agent.id.in_(wanted))
            rows = db.scalars(query.order_by(Agent.name).limit(MAX_AGENTS)).all()
            out.extend((agent, workspaces.get(ws_id)) for agent in rows)
    if wanted:
        missing = sorted(set(wanted) - {agent.id for agent, _ in out})
        if missing:
            raise NotFoundError("agent.not_found", "agent not found", {"agent_ids": missing})
    return out


@router.get("/projects/{project_id}/invoke-access", response_model=None)
def project_invoke_access(
    project_id: str,
    request: Request,
    agents: str | None = Query(default=None, max_length=4096),
    qualifier: str | None = Query(default=None, max_length=48),
    include: str | None = Query(default=None, max_length=128),
    format: str | None = Query(default=None, max_length=8),  # noqa: A002 - query name
    db: Session = Depends(get_db),
) -> dict[str, Any] | Response:
    if format not in (None, "", "json", "csv"):
        raise AppError("invoke_access.unknown_format", f"unknown format {format!r}",
                       {"formats": ["json", "csv"]})
    if qualifier and qualifier not in svc.QUALIFIERS:
        raise AppError("invoke_access.unknown_qualifier", f"unknown qualifier {qualifier!r}",
                       {"qualifier": qualifier, "available": list(svc.QUALIFIERS)})
    wanted = [a.strip() for a in (agents or "").split(",") if a.strip()]
    rows = _project_agents(db, request, project_id, wanted)
    body = svc.project_access(rows, qualifier=qualifier or None,
                              include=svc.parse_include(include))
    body["project_id"] = project_id
    if format == "csv":
        return Response(
            svc.inventory_csv(body["inventory"]), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="{project_id}-agent-arns.csv"'},
        )
    if format == "json":
        return Response(
            svc.dumps(body), media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="{project_id}-agent-arns.json"'},
        )
    return body

