"""Custom Python tools and sandboxed built-in tools (strands-builder SB-3).

``POST /api/custom-tools/check`` — the wizard's code check (`perm:project.use`):
syntax / structure, the model-facing input schema, the static findings and whether
this exact version is approved in the request's project. Nothing is run.

``GET /api/custom-tools/approvals`` (`perm:project.use`), ``POST`` / ``DELETE
/api/custom-tools/approvals[/{code_hash}]`` (administrators — approving lets code
outside the allow-list run with an agent's role: data, a steward does not). Approvals
belong to the request's project; another project's hash answers 404.

``GET`` / ``PUT /api/custom-tools/sandbox`` — the workspace's sandbox setting: which
high-risk built-in tools members may select, and the Code Interpreter they run in.
Workspace configuration, not project data (project-exempt); reading it is every
member's (the wizard greys out what is closed), changing it is an administrator's.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.project_scope import current_project
from app.models.ledger import Workspace
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.strands_builder import (
    CODE_INTERPRETER_ID_RE,
    MAX_CUSTOM_TOOL_REQUIREMENTS,
    CustomTool,
    SandboxTool,
)
from app.services import custom_tools as svc
from app.services.audit import record_audit_event

router = APIRouter(prefix="/api/custom-tools", tags=["custom-tools"])


class ToolCode(BaseModel):
    """One custom tool's version — what the hash covers (the description does not:
    rewording when to call it changes no code)."""

    name: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=20000)
    requirements: list[str] = Field(default_factory=list, max_length=MAX_CUSTOM_TOOL_REQUIREMENTS)
    execution: Literal["runtime", "code_interpreter"] = "runtime"


class ApproveRequest(ToolCode):
    description: str = Field(default="(approval)", min_length=1, max_length=1000)
    note: str = Field(default="", max_length=500)


class SandboxSettingRequest(BaseModel):
    allowed_tools: list[SandboxTool] = Field(default_factory=list, max_length=4)
    code_interpreter_id: str | None = Field(default=None, pattern=CODE_INTERPRETER_ID_RE)


def _tool(req: ApproveRequest) -> CustomTool:
    try:
        return CustomTool(
            name=req.name, description=req.description, code=req.code,
            requirements=req.requirements, execution=req.execution,
        )
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        raise AppError(
            "custom_tool.invalid",
            str(first.get("msg") or "the tool's code is not a valid custom tool"),
            status_code=422,
        ) from None


@router.post("/check")
def check(
    req: ToolCode,
    db: Session = Depends(get_db),
    _ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    requirements = list(dict.fromkeys(r.strip() for r in req.requirements if r.strip()))
    out = svc.check_code(req.name, req.code, requirements, req.execution)
    approval = svc.approval_for(db, current_project(), out["code_hash"])
    return {
        **out,
        "needs_approval": bool(out["ok"] and out["findings"]),
        "approval": approval,
    }


@router.get("/approvals")
def list_approvals(
    db: Session = Depends(get_db),
    _ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    records = svc.approvals(db, current_project())
    items = sorted(records.values(), key=lambda r: str(r.get("approved_at", "")), reverse=True)
    return {"approvals": items}


@router.post("/approvals", status_code=201)
def approve(
    req: ApproveRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    tool = _tool(req)
    record = svc.approve(
        db, current_project(), tool, actor=identity.username,
        actor_user_id=identity.user_id, note=req.note,
    )
    record_audit_event(
        workspace_id=ws.id, actor=identity.username, action="custom_tool.approve",
        target=f"{tool.name}@{record['code_hash'][:12]}", db=db,
    )
    db.commit()
    return record


@router.delete("/approvals/{code_hash}")
def revoke(
    code_hash: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    record = svc.revoke(db, current_project(), code_hash)
    record_audit_event(
        workspace_id=ws.id, actor=identity.username, action="custom_tool.revoke",
        target=f"{record.get('tool_name', '')}@{code_hash[:12]}", db=db,
    )
    db.commit()
    return {"revoked": code_hash}


@router.get("/sandbox")
def get_sandbox(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    return svc.sandbox_setting(ws.row)


@router.put("/sandbox")
def put_sandbox(
    req: SandboxSettingRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = db.get(Workspace, ws.id)
    if row is None:  # resolved a moment ago; gone only under a concurrent delete
        raise AppError("workspace.not_found", "workspace not found", status_code=404)
    out = svc.set_sandbox_setting(
        row, allowed_tools=list(req.allowed_tools),
        code_interpreter_id=req.code_interpreter_id, actor=identity.username,
    )
    record_audit_event(
        workspace_id=ws.id, actor=identity.username, action="custom_tool.sandbox_setting",
        target=",".join(out["allowed_tools"]) or "none", db=db, project_id=None,
    )
    db.commit()
    return out
