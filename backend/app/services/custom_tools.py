"""Custom Python tools and sandboxed built-in tools (strands-builder SB-3) — the
ledger half: who approved which version, and what a workspace lets run.

Two records, both small JSON blobs on rows that already exist:

* **approvals** — ``projects.settings["custom_tool_approvals"]``: {code hash →
  {tool name, findings, who, when, note}}. A custom tool whose static check has
  findings (``schemas.custom_tool_code.risk_findings``) deploys only when its exact
  version is approved in the agent's project. The hash covers the code, the
  requirements, the name and where it runs, so any change needs a fresh approval;
  an approval belongs to its project (another project's hash is not found).
* **the sandbox setting** — ``workspaces.settings["strands_sandbox"]``:
  ``{"allowed_tools": [...], "code_interpreter_id": ...}``. The high-risk built-in
  tools (python_repl / shell / file_write / use_aws) are selectable only once an
  administrator allowed them in the workspace; they always run in the Code
  Interpreter (``code_interpreter_id``, default the AWS-managed one).

``assert_deployable`` is the gate: the deploy's generate stage calls it (and the
agent save path should, so the console refuses before a build starts).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.ledger import Workspace
from app.models.projects import Project
from app.schemas.agent import AgentSpec
from app.schemas.custom_tool_code import (
    CustomToolCodeError,
    code_hash,
    risk_findings,
    tool_signature,
)
from app.schemas.strands_builder import (
    DEFAULT_CODE_INTERPRETER_ID,
    SANDBOX_TOOLS,
    CustomTool,
)

APPROVALS_KEY = "custom_tool_approvals"
SANDBOX_SETTING_KEY = "strands_sandbox"
# a project keeps its most recent approvals; the oldest drop off past this
MAX_APPROVALS = 200


# ── the static check ──────────────────────────────────────────────────────────


def check_code(
    name: str, code: str, requirements: list[str], execution: str
) -> dict[str, Any]:
    """The wizard's check: syntax / structure first (an error is final), then the
    input schema and the findings a reviewer must look at."""
    digest = code_hash(name, code, list(requirements), execution)
    try:
        signature = tool_signature(code, name)
        findings = risk_findings(code, list(requirements))
    except CustomToolCodeError as exc:
        return {"ok": False, "error": exc.as_dict(), "findings": [], "code_hash": digest,
                "input_schema": None}
    return {
        "ok": True,
        "error": None,
        "findings": [f.as_dict() for f in findings],
        "code_hash": digest,
        "input_schema": signature.input_schema,
    }


def needs_approval(tool: CustomTool) -> bool:
    return bool(risk_findings(tool.code, tool.requirements))


# ── approvals ─────────────────────────────────────────────────────────────────


def _project(db: Session, project_id: str | None) -> Project:
    row = db.get(Project, project_id) if project_id else None
    if row is None:
        raise NotFoundError("project.not_found", "project not found")
    return row


def approvals(db: Session, project_id: str | None) -> dict[str, dict[str, Any]]:
    if not project_id:
        return {}
    row = db.get(Project, project_id)
    stored = ((row.settings or {}).get(APPROVALS_KEY) if row is not None else None) or {}
    return dict(stored) if isinstance(stored, dict) else {}


def approval_for(db: Session, project_id: str | None, digest: str) -> dict[str, Any] | None:
    return approvals(db, project_id).get(digest)


def approve(
    db: Session,
    project_id: str | None,
    tool: CustomTool,
    *,
    actor: str,
    actor_user_id: str | None,
    note: str = "",
) -> dict[str, Any]:
    """Approve this exact version in the project. Idempotent per hash (the newest
    approval wins); the caller commits."""
    row = _project(db, project_id)
    digest = tool.hash()
    record = {
        "code_hash": digest,
        "tool_name": tool.name,
        "execution": tool.execution,
        "requirements": list(tool.requirements),
        "findings": [f.as_dict() for f in risk_findings(tool.code, tool.requirements)],
        "approved_by": actor,
        "approved_by_user_id": actor_user_id,
        "approved_at": datetime.now(UTC).isoformat(),
        "note": note.strip()[:500],
    }
    current = approvals(db, row.id)
    current.pop(digest, None)
    current[digest] = record
    if len(current) > MAX_APPROVALS:
        oldest = sorted(current, key=lambda k: str(current[k].get("approved_at", "")))
        for key in oldest[: len(current) - MAX_APPROVALS]:
            current.pop(key, None)
    # a new dict, so the JSON column registers the change
    row.settings = {**(row.settings or {}), APPROVALS_KEY: current}
    return record


def revoke(db: Session, project_id: str | None, digest: str) -> dict[str, Any]:
    row = _project(db, project_id)
    current = approvals(db, row.id)
    record = current.pop(digest, None)
    if record is None:
        raise NotFoundError("custom_tool.approval_not_found", "approval not found",
                            {"code_hash": digest})
    settings = {**(row.settings or {}), APPROVALS_KEY: current}
    if not current:
        settings.pop(APPROVALS_KEY)
    row.settings = settings or None
    return record


def unapproved(db: Session, spec: AgentSpec, project_id: str | None) -> list[CustomTool]:
    """The spec's custom tools that have findings and no approval of their version."""
    if not spec.custom_tools:
        return []
    approved = approvals(db, project_id)
    return [t for t in spec.custom_tools if needs_approval(t) and t.hash() not in approved]


# ── the workspace sandbox setting ─────────────────────────────────────────────


def sandbox_setting(row: Workspace | None) -> dict[str, Any]:
    raw = ((row.settings or {}).get(SANDBOX_SETTING_KEY) if row is not None else None) or {}
    allowed = [t for t in raw.get("allowed_tools") or [] if t in SANDBOX_TOOLS]
    interpreter = raw.get("code_interpreter_id") or None
    return {
        "allowed_tools": allowed,
        "code_interpreter_id": interpreter,
        "effective_code_interpreter_id": interpreter or DEFAULT_CODE_INTERPRETER_ID,
        "updated_by": raw.get("updated_by"),
        "updated_at": raw.get("updated_at"),
    }


def set_sandbox_setting(
    row: Workspace,
    *,
    allowed_tools: list[str],
    code_interpreter_id: str | None,
    actor: str,
) -> dict[str, Any]:
    unknown = sorted(set(allowed_tools) - set(SANDBOX_TOOLS))
    if unknown:
        raise AppError("custom_tool.sandbox_tool_unknown",
                       f"not a sandbox tool: {unknown}", {"tools": unknown}, status_code=422)
    value = {
        "allowed_tools": [t for t in SANDBOX_TOOLS if t in allowed_tools],
        "code_interpreter_id": code_interpreter_id or None,
        "updated_by": actor,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    row.settings = {**(row.settings or {}), SANDBOX_SETTING_KEY: value}
    return sandbox_setting(row)


# ── the gate ──────────────────────────────────────────────────────────────────


def assert_deployable(
    db: Session, spec: AgentSpec, *, workspace: Workspace | None, project_id: str | None
) -> AgentSpec:
    """Refuse what may not run; return the spec with the server-owned Code
    Interpreter id stamped from the workspace setting (a client-sent one never
    survives). A spec with no SB-3 field passes through unchanged."""
    if not spec.custom_tools and not spec.sandbox_tools:
        if spec.code_interpreter_id is not None:
            return spec.model_copy(update={"code_interpreter_id": None})
        return spec
    setting = sandbox_setting(workspace)
    refused = [t for t in spec.sandbox_tools if t not in setting["allowed_tools"]]
    if refused:
        raise AppError(
            "custom_tool.sandbox_tool_not_allowed",
            f"{', '.join(refused)} {'is' if len(refused) == 1 else 'are'} not allowed in "
            "this workspace — an administrator opens the sandboxed tools in the "
            "workspace's sandbox setting",
            {"tools": refused},
            status_code=422,
        )
    pending = unapproved(db, spec, project_id)
    if pending:
        names = [t.name for t in pending]
        raise AppError(
            "custom_tool.approval_required",
            f"custom tool{'s' if len(names) > 1 else ''} {', '.join(names)} "
            f"{'need' if len(names) > 1 else 'needs'} an administrator's approval of "
            "this exact version — its static check found imports or calls outside the "
            "allow-list",
            {"tools": names, "code_hashes": [t.hash() for t in pending]},
            status_code=422,
        )
    interpreter = setting["code_interpreter_id"]
    if spec.code_interpreter_id == interpreter:
        return spec
    from app.schemas.strands_builder import uses_code_interpreter

    return spec.model_copy(
        update={"code_interpreter_id": interpreter if uses_code_interpreter(spec) else None}
    )
