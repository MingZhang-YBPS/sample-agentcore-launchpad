"""Per-workspace logical resource mapping (roadmap T23).

A release bundle's spec names environment-specific ids: a knowledge base id, a memory id,
a gateway id, an S3 skill prefix. Those ids mean nothing in another account. A mapping row
says "in THIS workspace, the resource the team calls `kb:hr-policy` is `ABCDE12345`", so a
promotion can rewrite the spec instead of carrying dev ids into production.

`workspace_id` is the workspace the resource LIVES in (the promotion target when it is
read); it is scoped like every other environment table, so a purge takes it with it.
Registered from `core/db.init_db` so `create_all` sees it.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now


class ResourceMapping(Base):
    __tablename__ = "resource_mappings"
    # Uniqueness of (workspace_id, kind, name) is enforced by `upsert_mapping`, not by a
    # constraint: the ledger-migration tests drop `workspace_id` from every scoped table
    # to simulate an older database, and SQLite cannot drop a column a constraint uses.

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # kb | memory | gateway | mcp_record | skill  (services.resource_mapping.KINDS)
    kind: Mapped[str] = mapped_column(String(16))
    # the logical name, e.g. `hr-policy` — the mapping key is `<kind>:<name>`
    name: Mapped[str] = mapped_column(String(64))
    # the real id (or, for a skill, the s3:// prefix) in this workspace
    resource_id: Mapped[str] = mapped_column(String(512))
    note: Mapped[str | None] = mapped_column(Text, default=None)
    updated_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
