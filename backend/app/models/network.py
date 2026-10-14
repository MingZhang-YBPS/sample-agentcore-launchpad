"""VPC network profiles (design projects §12.3): the subnets + security groups an
agent's runtime may be placed in, kept by administrators per workspace.

An agent spec names a profile (`network: {"profile_id": ...}`) instead of carrying
subnet ids; the deployer resolves it to `networkModeConfig`
(`services/network_profiles.resolve_vpc`). A project only sees — and only picks —
the profiles in its allowed set (`project_network_profiles`); any other one reads as
missing (design §5.4).

`network_profiles` and `project_network_profiles` are workspace-scoped tables
(`core/db.WORKSPACE_SCOPED_TABLES`), so a workspace purge takes them along. Neither
carries `project_id`: a profile is workspace infrastructure shared by the projects
allowed to use it. No database-level foreign keys, for the same reason as
`models/projects.py`.
"""

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _now

PURPOSES = ("agents", "tools", "both")
INTERNET_MODES = ("none", "nat")
# unchecked until the first preflight (V12-check); blocked profiles cannot be chosen,
# warn ones can (the wizard shows the warnings)
STATUSES = ("unchecked", "ok", "warn", "blocked")
TOOL_KINDS = ("code_interpreter", "browser")
LEGACY_PREFIX = "legacy-"


def legacy_profile_id(workspace_id: str, subnets: list[str], security_groups: list[str]) -> str:
    """`legacy-<hash8>` for one (workspace, subnet set, SG set) (design §12.10): order-
    and duplicate-insensitive, so every inline spec naming the same network shares one
    profile, and re-running the migration finds the profile it made."""
    key = "|".join((
        workspace_id, ",".join(sorted(set(subnets))), ",".join(sorted(set(security_groups))),
    ))
    return LEGACY_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:8]


class NetworkProfile(Base):
    __tablename__ = "network_profiles"
    # (workspace_id, name) uniqueness among live rows lives in the service: the
    # ledger-migration tests drop `workspace_id` to model an older database, and
    # SQLite cannot drop a column a constraint uses (same as resource_mappings)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    # NULL only on a `legacy-<hash8>` profile made from an inline spec: the old spec
    # never named its VPC, and the migration makes no AWS call to find it
    vpc_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # ≥ 2 in supported AZs (checked by preflight); legacy profiles may hold 1
    subnet_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 1–5, the same cap as schemas.agent.VpcNetwork
    security_group_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    purpose: Mapped[str] = mapped_column(String(16), default="agents")
    # declared internet access, which preflight checks the routes against
    internet: Mapped[str] = mapped_column(String(16), default="none")
    # [{cidr|host, port, note}]: declared private targets (probe + documentation only)
    reachable: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    # the last preflight's per-check results (V12-check)
    check_results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(String(16), default="unchecked")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ProjectNetworkProfile(Base):
    """A project may use this profile in the profile's workspace.

    Keyed on (project, profile): a profile lives in exactly one workspace, so the
    design's (project, workspace, profile) key collapses to these two, and
    `workspace_id` stays an ordinary column the workspace purge can match."""

    __tablename__ = "project_network_profiles"

    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # the wizard's preselection; at most one per (project, workspace), kept by the service
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class NetworkProfileTool(Base):
    """A custom code interpreter / browser created in a profile's network (VPC mode),
    on first use by an agent whose built-in tools follow its network (§12.6)."""

    __tablename__ = "network_profile_tools"

    profile_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)  # TOOL_KINDS
    resource_id: Mapped[str | None] = mapped_column(String(128), default=None)
    arn: Mapped[str | None] = mapped_column(String(256), default=None)
    status: Mapped[str] = mapped_column(String(16), default="creating")
