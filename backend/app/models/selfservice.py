"""Business self-service tables (roadmap T35/T36).

`AnswerRule` is a curated answer a business owner attached to one agent; `AnswerRuleSet`
holds the per-agent master switch; `Issue` is one problem worked end to end in the issue
box. All three are workspace-scoped like every other environment table (see
`WORKSPACE_SCOPED_TABLES`) and are registered from `core/db.init_db` so `create_all`
sees them.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now


class AnswerRule(Base):
    """One curated answer for one agent. Rules are evaluated in `position` order and
    the first enabled match wins, so a specific rule sits above a general one."""

    __tablename__ = "answer_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    position: Mapped[int] = mapped_column(default=0)
    name: Mapped[str] = mapped_column(String(64), default="")
    match: Mapped[str] = mapped_column(String(16), default="exact")  # exact | contains
    pattern: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    # the issue-box item this rule was written to fix, if any
    source_issue_id: Mapped[str | None] = mapped_column(String(32), default=None)
    hit_count: Mapped[int] = mapped_column(default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class AnswerRuleSet(Base):
    """The per-agent off switch. Absent row = rules on (they only exist if someone
    wrote them); the row is created the first time an owner flips the switch."""

    __tablename__ = "answer_rule_sets"
    __table_args__ = (UniqueConstraint("agent_id", name="uq_rule_set_agent"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Issue(Base):
    """A problem the agent caused, worked from discovery to close (T36).

    `history` is the append-only who/when trail (`open`, `fixed`, `wont_fix`, reopen);
    `fixes` records the fixes attempted (curated answer, knowledge-base documents,
    evaluation dataset) without performing them — those go through their own endpoints.
    `created_at` -> `resolved_at` is the close time the business measures.
    """

    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("agent_id", "message_id", name="uq_issue_answer"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    # thumbs_down | unanswered | review | manual
    kind: Mapped[str] = mapped_column(String(16), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True, default="")
    message_id: Mapped[int | None] = mapped_column(default=None)  # the answer at fault
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    correction: Mapped[str | None] = mapped_column(Text, default=None)
    opened_by: Mapped[str] = mapped_column(String(64), default="")
    resolved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    resolution_note: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    fixes: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    history: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
