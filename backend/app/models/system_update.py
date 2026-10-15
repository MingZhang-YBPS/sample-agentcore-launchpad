"""Online console updates (docs/design online-update §2.2): one row per prepared commit.

Platform-level and hub-global like `announcements`: no workspace, no project. The
row is also the prepare job's checkpoint (`progress`), so a backend restart resumes
a build from its first unfinished step instead of a `jobs` row — a job row must name
a workspace, and an update belongs to none.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now

# the state machine of §2.2; `superseded` = a ready stage a newer prepare replaced
PREPARING, PREPARE_FAILED, READY = "preparing", "prepare_failed", "ready"
SCHEDULED, APPLYING = "scheduled", "applying"
DONE, ROLLED_BACK, FAILED, SUPERSEDED = "done", "rolled_back", "failed", "superseded"
ACTIVE_STATUSES = (PREPARING, READY, SCHEDULED, APPLYING)
# at most one of these at a time; a `ready` row is replaced by a newer prepare
BUSY_STATUSES = (PREPARING, SCHEDULED, APPLYING)
TERMINAL_STATUSES = (PREPARE_FAILED, DONE, ROLLED_BACK, FAILED, SUPERSEDED)

KIND_UPDATE, KIND_ROLLBACK = "update", "rollback"


class SystemUpdate(Base):
    __tablename__ = "system_updates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # update = fast-forward to target_sha; rollback = back to an ancestor of the running one
    kind: Mapped[str] = mapped_column(String(16), default=KIND_UPDATE)
    target_sha: Mapped[str] = mapped_column(String(40), index=True)
    target_time: Mapped[str | None] = mapped_column(String(40), default=None)  # commit time
    # the commit the console ran when the row was created (the fast-forward base)
    base_sha: Mapped[str | None] = mapped_column(String(40), default=None)
    behind: Mapped[int] = mapped_column(Integer, default=0)
    # [{sha, title, author, time}], newest first, at most 50
    commits: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default=PREPARING, index=True)
    staged_dir: Mapped[str | None] = mapped_column(String(512), default=None)
    # prepare checkpoints: {step: "done"}; a resumed job skips what is done
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    requested_by: Mapped[str] = mapped_column(String(64), default="")
    requested_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # the announcement the schedule published (unpublished once the update ends)
    announcement_id: Mapped[str | None] = mapped_column(String(32), default=None)
    log: Mapped[str] = mapped_column(Text, default="")  # JSONL, one event per line
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # the commit the updater replaced (from apply.result); a rollback's target
    previous_sha: Mapped[str | None] = mapped_column(String(40), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
