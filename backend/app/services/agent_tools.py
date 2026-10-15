"""Agents-as-tools (strands-builder SB-2 §6): which deployed agents a Strands agent
may call as tools, and the server-owned facts its spec carries about each.

A member picks agents by id; everything the generated code and the execution role
use — the runtime ARN, its protocol, the endpoint production traffic uses, whether
it needs ``runtimeUserId`` — is read here from the ledger, never taken from the
client. ``resolve_agent_tools`` runs on save (``routers/agents.py``) and again in
the deploy's generate stage (``deployer/zip_runtime._stage_generate``), which writes
the resolved refs back before the provision stage derives the role, so a hand-made
``runtime_arn`` never reaches an IAM grant.

Eligible: an agent of the caller's project — or one another project lent it at
``use`` level (§11.2, share kind ``agent``) — in the same workspace, deployed as an
AgentCore Runtime (``http`` or ``a2a`` protocol), whose inbound auth is IAM (the
caller signs with its execution role; a JWT runtime would refuse it). An id the
project cannot see answers 404, as every other foreign id does.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project, project_context
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import invoke_access
from app.templates import gateway_support

# the share kind §11.2 names for agents (A2A cards / use = callable)
SHARE_KIND_AGENT = "agent"


def _shared_agent_ids(db: Session, project_id: str | None) -> frozenset[str]:
    if not project_id:
        return frozenset()
    from app.services.resource_shares import received_ids

    return received_ids(db, SHARE_KIND_AGENT, use=True, project_id=project_id)


def _load(db: Session, agent_id: str, project_id: str | None) -> tuple[Agent, str | None]:
    """(row, lending project) — 404 for an id the project neither owns nor receives."""
    row = db.execute(select(Agent).where(Agent.id == agent_id)).scalar_one_or_none()
    if row is not None and row.status != "deleted":
        return row, None
    if agent_id in _shared_agent_ids(db, project_id):
        with project_context(None):
            row = db.execute(select(Agent).where(Agent.id == agent_id)).scalar_one_or_none()
        if row is not None and row.status != "deleted":
            return row, getattr(row, "project_id", None)
    raise NotFoundError(
        "agent_tools.not_found", f"agent {agent_id!r} not found", {"agent_id": agent_id}
    )


def _ineligible(row: Agent, reason: str, message: str) -> AppError:
    return AppError(
        f"agent_tools.{reason}",
        f"agent {row.name!r} cannot be offered as a tool: {message}",
        {"agent_id": row.id, "reason": reason},
        status_code=422,
    )


def eligibility(row: Agent) -> str | None:
    """Why ``row`` cannot be called as a tool (a reason code), or None."""
    if invoke_access.resource_kind(row) != "runtime":
        return "not_runtime"
    if row.status != "active" or not row.arn:
        return "not_deployed"
    protocol = (row.spec or {}).get("protocol") or "http"
    if protocol not in ("http", "a2a"):
        return "protocol"
    if invoke_access.mode_of(row) == "jwt":
        return "jwt_inbound"
    return None


_MESSAGES = {
    "not_runtime": "only AgentCore Runtime agents can be called (a managed Harness cannot yet)",
    "not_deployed": "it is not deployed",
    "protocol": "an MCP server is a tool server, not an agent — attach it as a tool instead",
    "jwt_inbound": "its runtime accepts JWT bearer tokens only, and this agent signs with IAM",
}


def resolve_agent_tools(
    db: Session, spec: AgentSpec, *, agent_id: str | None, workspace_id: str | None
) -> list[dict[str, Any]]:
    """The spec's ``agent_tools`` with every server-owned field read from the ledger.

    Raises 404 for an id the current project cannot see, 422 for an ineligible or
    self-referencing agent. Returns the refs as stored-spec dicts, in order."""
    project_id = current_project()
    out: list[dict[str, Any]] = []
    for ref in spec.agent_tools:
        if agent_id and ref.agent_id == agent_id:
            raise AppError(
                "agent_tools.self",
                "an agent cannot be offered to itself as a tool",
                {"agent_id": ref.agent_id},
                status_code=422,
            )
        row, lender = _load(db, ref.agent_id, project_id)
        if workspace_id and row.workspace_id and row.workspace_id != workspace_id:
            # shares never cross workspaces; another workspace's agent is not ours to see
            raise NotFoundError(
                "agent_tools.not_found", f"agent {ref.agent_id!r} not found",
                {"agent_id": ref.agent_id},
            )
        reason = eligibility(row)
        if reason:
            raise _ineligible(row, reason, _MESSAGES[reason])
        out.append({
            **ref.model_dump(),
            "runtime_arn": row.arn,
            "protocol": (row.spec or {}).get("protocol") or "http",
            "qualifier": invoke_access.production_qualifier(row),
            "runtime_user": gateway_support.runtime_user_id(row.spec) is not None,
            "shared_from": lender,
        })
    return out


def resolved_spec(
    db: Session, spec: AgentSpec, *, agent_id: str | None, workspace_id: str | None
) -> AgentSpec:
    """``spec`` with its agent tools resolved (the same object when it has none)."""
    if not spec.agent_tools:
        return spec
    refs = resolve_agent_tools(db, spec, agent_id=agent_id, workspace_id=workspace_id)
    return spec.model_copy(update={"agent_tools": AgentSpec(
        **{**spec.model_dump(), "agent_tools": refs}).agent_tools})
