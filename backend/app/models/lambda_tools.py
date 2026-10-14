"""Gateway tool targets the console tracks besides hosted MCP servers (design projects
§15.3, §15.8, §15.12, §15.15).

* ``mcp_servers`` — the platform-side state of a hosted MCP server that does not belong
  in its agent row's spec: which runtime endpoint its Gateway target calls (``DEFAULT``,
  or ``live`` pinned at a version after a rollback), the last scheduled health check, and
  the last background operation (rollback, Cedar scaffold). 1:1 with the agent row,
  derived from it (``__project_parent__``).
* ``lambda_tool_targets`` — a Lambda function a member put on the Gateway from the
  console (``POST /api/gateway-tools/lambda``). Its tool schema is reviewed like a hosted
  server's tool list before the target exists (§15.6); the review rows use this row's
  id as their subject (``mcp_tool_reviews.agent_id``).
* ``external_mcp_targets`` — an MCP-server target made by hand through Connections (the
  pre-§15 path, §15.11) that a member brought under tool review: an approved baseline of
  what it lists today, then drift checks. Nothing about the target itself is managed —
  no versions, no rollback.

All three are project-owned and workspace-scoped; no database-level foreign keys, for
the same reason as `models/projects.py`.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.project_scope import ProjectOwned
from app.models.ledger import _id, _now

# the runtime endpoint a hosted server's Gateway target calls
QUALIFIERS = ("DEFAULT", "live")
# reviewing → publishing → live | failed; reviewing → rejected
LAMBDA_STATUSES = ("reviewing", "publishing", "live", "rejected", "failed")


class McpServerState(ProjectOwned, Base):
    __tablename__ = "mcp_servers"
    __project_parent__ = ("agent_id", ("agents",))

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    qualifier: Mapped[str] = mapped_column(String(32), default="DEFAULT")
    # the version `live` serves while the target is pinned to it (None: follows DEFAULT)
    live_version: Mapped[str | None] = mapped_column(String(16), default=None)
    # {status: healthy|drift|failing|withheld, checked_at, tool_count, error,
    #  consecutive_failures, review_id}
    health: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # the last background operation: {kind: rollback|scaffold, status, …}
    operation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class LambdaToolTarget(ProjectOwned, Base):
    __tablename__ = "lambda_tool_targets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    gateway_id: Mapped[str] = mapped_column(String(64), default="")
    # set once the approved schema is published as a Gateway target
    target_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    target_name: Mapped[str] = mapped_column(String(100), index=True)
    lambda_arn: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    # MCP tool definitions as the Gateway takes them: [{name, description, inputSchema}]
    tool_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="reviewing")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ExternalMcpTarget(ProjectOwned, Base):
    __tablename__ = "external_mcp_targets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    gateway_id: Mapped[str] = mapped_column(String(64), default="")
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    target_name: Mapped[str] = mapped_column(String(100))
    endpoint: Mapped[str] = mapped_column(String(1024), default="")
    adopted_by: Mapped[str] = mapped_column(String(64), default="")
    adopted_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # the last drift check (same shape as McpServerState.health)
    health: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
