"""Phase 2 runtime isolation: the per-(project, workspace) AWS resources (design §10.2).

`project_resources` records what `project_provision` made for one project in one
workspace — its permissions-boundary policy, memory, KB service role, M2M client and
token-vault provider, ECR repository, S3 prefix, and later one application inference
profile per model. One row per (project, workspace, kind): a stage re-run upserts its
row, so a resumed or retried job adopts what an earlier run recorded.

Hub-global like the other project tables (`models/projects.py`): no project mixin —
the key already names the project, and every reader filters on it explicitly
(`services/project_isolation.py`). No database-level foreign keys, for the same
reason as there.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# kinds (design §10.2); `inference_profile:<model id>` is one row per model
KIND_BOUNDARY_POLICY = "boundary_policy"
KIND_MEMORY = "memory"
KIND_KB_ROLE = "kb_role"
KIND_M2M_CLIENT = "m2m_client"
KIND_M2M_PROVIDER = "m2m_provider"
KIND_CEDAR_POLICY = "cedar_policy"
# only under `settings.isolation.gateway = dedicated` (§10.6-7)
KIND_GATEWAY = "gateway"
KIND_ECR_REPO = "ecr_repo"
KIND_S3_PREFIX = "s3_prefix"
KIND_INFERENCE_PROFILE_PREFIX = "inference_profile:"
KINDS = frozenset({
    KIND_BOUNDARY_POLICY, KIND_MEMORY, KIND_KB_ROLE, KIND_M2M_CLIENT, KIND_M2M_PROVIDER,
    KIND_CEDAR_POLICY, KIND_GATEWAY, KIND_ECR_REPO, KIND_S3_PREFIX,
})

# provisioning: recorded by a stage of a run that has not finished; ready: the run
# finalized; failed: the stage that recorded it failed (`error` says why); retiring:
# scheduled for removal (§10.11 retire, R10-migrate)
STATUS_PROVISIONING = "provisioning"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_RETIRING = "retiring"


def valid_kind(kind: str) -> bool:
    if kind in KINDS:
        return True
    model = kind.removeprefix(KIND_INFERENCE_PROFILE_PREFIX)
    return kind.startswith(KIND_INFERENCE_PROFILE_PREFIX) and bool(model)


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectResource(Base):
    """One runtime resource of a project in a workspace."""

    __tablename__ = "project_resources"
    __table_args__ = (Index("ix_project_resources_workspace", "workspace_id"),)

    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(96), primary_key=True)
    # the service's own id (memory id, role name, client id, repository name, prefix)
    resource_id: Mapped[str | None] = mapped_column(String(256), default=None)
    arn: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_PROVISIONING)
    # kind-specific extras (a boundary's version, a provider's callback, …); no secrets
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # the last failure of the stage that owns this row; cleared when it next succeeds
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # the project_provision job that last wrote the row
    job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
