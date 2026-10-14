"""Tool reviews of hosted MCP servers (design projects §15.3, §15.6).

A tool's ``description`` and ``inputSchema`` reach every calling model verbatim, so
they are a prompt-injection surface. Each tool list a server presents (its first
deploy, a re-publish, a drift the console notices) is snapshotted here with its hash,
its diff against the last approved list and static hints; the server's Gateway target
stays withheld until a project member holding ``mcp.approve`` — never the deployer —
approves it (`services/mcp_servers.py` is the gate).

Project-owned through the server's agent row (``__project_parent__``); workspace
scoped like every per-environment table. No database-level foreign keys, for the same
reason as `models/projects.py`.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.project_scope import ProjectOwned
from app.models.ledger import _id, _now

# reviewing → approved | rejected; an open review a newer tool list replaces → superseded
STATUSES = ("reviewing", "approved", "rejected", "superseded")
# deploy: a deploy / re-publish listed the tools; drift: a later read found them
# changed without one; baseline: a server deployed before tool review existed,
# recorded as approved on its first re-sync so its live target is not pulled
# rollback: an earlier version's approved list restored as the current one (§15.8)
SOURCES = ("deploy", "drift", "baseline", "rollback")
# after an approval the target is re-created in the background (`publish_approved`)
PUBLISH_STATUSES = ("publishing", "published", "failed")


class McpToolReview(ProjectOwned, Base):
    __tablename__ = "mcp_tool_reviews"
    __project_parent__ = ("agent_id", ("agents",))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    # the runtime version that served this list (None when unknown)
    version: Mapped[str | None] = mapped_column(String(16), default=None)
    target_name: Mapped[str] = mapped_column(String(100), default="")
    # [{name, description, input_schema}] sorted by name, the target prefix stripped
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tools_hash: Mapped[str] = mapped_column(String(64), index=True)
    # the approved review this one is compared with (None: the server's first list)
    base_review_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # {added: [name], removed: [name], description_changed: [name], schema_changed: [name]}
    diff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # [{tool, kind, detail}] — hints only, never an automatic rejection
    hints: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="reviewing")
    source: Mapped[str] = mapped_column(String(16), default="deploy")
    # SoD (design §3.6): the deployer, by name and `users.id`; NULL id = compare by name
    requested_by: Mapped[str] = mapped_column(String(64), default="")
    requested_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    decided_by: Mapped[str | None] = mapped_column(String(64), default=None)
    decided_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str] = mapped_column(Text, default="")
    publish_status: Mapped[str | None] = mapped_column(String(16), default=None)
    publish_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
