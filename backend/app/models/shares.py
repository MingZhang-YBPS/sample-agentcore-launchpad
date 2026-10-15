"""Cross-project sharing grants (projects design §11.2, G11-share).

A project's owner (`project.share`) lends one resource to another project of the same
workspace, read-only (`read`) or for its agents and evaluations to use (`use`). A share
never grants a write and is never passed on: the grantee sees the resource, its own
project stays the only one that changes it.

Workspace-scoped (a share never crosses workspaces), not project-owned: the row has two
projects, and both sides list it (`services/resource_shares.py`). No database-level
foreign keys, like `models/projects.py`.

The models whose rows a share makes readable are registered here with
`project_scope.register_shareable`, at import time, so the automatic criteria know them
before the first query compiles.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.project_scope import register_shareable

# `resource_shares.kind` — what can be shared (§11.2 table). Memory, sessions,
# feedback, issues and audit are business records: never shareable.
KIND_KNOWLEDGE_BASE = "knowledge_base"
KIND_EVALUATOR = "evaluator"
KIND_EVAL_DATASET = "eval_dataset"  # a golden set (its three splits come along) or a dataset
KIND_CRITERIA_SET = "criteria_set"  # a template lineage (every version, its criteria)
KIND_GATEWAY_TARGET = "gateway_target"  # a console Lambda target or an adopted MCP target
SHARE_KINDS = (
    KIND_KNOWLEDGE_BASE, KIND_EVALUATOR, KIND_EVAL_DATASET, KIND_CRITERIA_SET,
    KIND_GATEWAY_TARGET,
)
ACCESS_READ, ACCESS_USE = "read", "use"
ACCESS_LEVELS = (ACCESS_READ, ACCESS_USE)
# requested: a cross-department share waiting for the grantee department's approval
STATUS_REQUESTED, STATUS_ACTIVE = "requested", "active"
STATUS_REVOKED, STATUS_EXPIRED, STATUS_REJECTED = "revoked", "expired", "rejected"
OPEN_STATUSES = (STATUS_REQUESTED, STATUS_ACTIVE)


def _now() -> datetime:
    return datetime.now(UTC)


class ResourceShare(Base):
    __tablename__ = "resource_shares"
    __table_args__ = (
        Index("ix_resource_shares_grantee", "grantee_project_id", "status"),
        Index("ix_resource_shares_owner", "owner_project_id", "status"),
        # one open grant per (resource, grantee): a second asks the first to be revoked
        Index(
            "uq_resource_shares_open", "kind", "resource_id", "grantee_project_id",
            unique=True,
            sqlite_where=text("status IN ('requested', 'active')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    resource_id: Mapped[str] = mapped_column(String(128))
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    owner_project_id: Mapped[str] = mapped_column(String(32))
    grantee_project_id: Mapped[str] = mapped_column(String(32))
    access: Mapped[str] = mapped_column(String(8), default=ACCESS_READ)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE)
    # display name of the resource at grant time (AWS-only kinds have no ledger name)
    resource_name: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    granted_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    approved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_by: Mapped[str | None] = mapped_column(String(64), default=None)
    revoked_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


def _register() -> None:
    from app.evaluation.models import EvalDataset
    from app.models.dlc import CriteriaSet, Criterion
    from app.models.lambda_tools import ExternalMcpTarget, LambdaToolTarget
    from app.models.ledger import ResourceCreator

    register_shareable(
        EvalDataset, CriteriaSet, Criterion, LambdaToolTarget, ExternalMcpTarget,
        # the ownership rows of shared KBs / evaluators (creator display)
        ResourceCreator,
    )


_register()
