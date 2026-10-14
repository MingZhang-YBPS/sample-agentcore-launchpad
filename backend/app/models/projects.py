"""Projects inside a workspace: the data and permission boundary (design §2.2).

All of these tables are hub-global (no `workspace_id`): a project spans the
workspaces it is bound to (`project_workspaces`). Business rows carry
`project_id` through `app.core.project_scope.ProjectOwned` / `ProjectShared`.

No database-level foreign keys, on purpose: SQLite does not enforce them and the
migration tests drop columns to model an old ledger (same reason as
`models/resource_mapping.py`). Integrity lives in `services/projects.py`.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

DEFAULT_PROJECT_ID = "default"
# Ids are slugs (design §9.5): phase 2 folds them into IAM role names, tag values
# and memory names, so the charset must survive all of those.
PROJECT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"


def _now() -> datetime:
    return datetime.now(UTC)


class Department(Base):
    """Organizational owner of projects (and, in phase 3, a unit of delegation)."""

    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # §11.1: {can_create_users, max_projects, user_validity_max_days}; None = defaults
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)


class DepartmentAdmin(Base):
    """A department administrator (design §11.1): manages the structure of the
    department's projects and groups. Grants no data access by itself."""

    __tablename__ = "department_admins"
    __table_args__ = (Index("ix_department_admins_user", "user_id"),)

    department_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    added_by: Mapped[str | None] = mapped_column(String(32), default=None)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DepartmentWorkspace(Base):
    """A workspace a platform administrator opened to a department: its department
    administrators may bind the department's projects there, and nowhere else."""

    __tablename__ = "department_workspaces"

    department_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    added_by: Mapped[str | None] = mapped_column(String(32), default=None)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Project(Base):
    """The data + permission boundary: an agent and all its business data belong to one."""

    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_department", "department_id"),
        Index("ix_projects_group", "group_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    department_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 0..1 project group (design D-5)
    group_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # active | archived (phase 1); deleting arrives with phase 3's lifecycle
    status: Mapped[str] = mapped_column(String(16), default="active")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # P1: resource_visibility; P2: isolation{...}, inbound_auth_default (isolated
    # projects); P3: quotas{...} (incl. quotas.v1_rate_per_minute), sod_strict
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ProjectWorkspace(Base):
    """An administrator allowed project P to run in workspace W (many-to-many)."""

    __tablename__ = "project_workspaces"
    __table_args__ = (
        Index("ix_project_workspaces_workspace", "workspace_id"),
        # one default project per workspace — the one a header-less request means
        Index(
            "uq_project_workspaces_default",
            "workspace_id",
            unique=True,
            sqlite_where=text("is_workspace_default = 1"),
        ),
    )

    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    is_workspace_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_by: Mapped[str | None] = mapped_column(String(32), default=None)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProjectRole(Base):
    """A per-project custom role: a built-in template copied and edited."""

    __tablename__ = "project_roles"

    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # a list of keys, ⊆ services.access.ALL_PROJECT_KEYS
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    based_on: Mapped[str | None] = mapped_column(String(32), default=None)
    created_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProjectMember(Base):
    """A user's direct role in one project."""

    __tablename__ = "project_members"
    __table_args__ = (Index("ix_project_members_user", "user_id"),)

    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # a built-in template key | a project_roles.key | "inherit"
    role_key: Mapped[str] = mapped_column(String(32))
    # {key: bool}: same semantics as users.permissions (explicit entries only)
    overrides: Mapped[dict[str, bool] | None] = mapped_column(JSON, default=None)
    added_by: Mapped[str | None] = mapped_column(String(32), default=None)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProjectGroup(Base):
    """An access bundle: puts its members into every project in the group at once."""

    __tablename__ = "project_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    department_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class GroupMember(Base):
    """A user's role in a project group — applies to every project in it."""

    __tablename__ = "group_members"
    __table_args__ = (Index("ix_group_members_user", "user_id"),)

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # built-in template keys only: custom roles are per project
    role_key: Mapped[str] = mapped_column(String(32))
    added_by: Mapped[str | None] = mapped_column(String(32), default=None)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AccessEpoch(Base):
    """One-row counter bumped by every access-affecting write (design §5.11).

    Every request reads it (a PK read); a changed value drops the process-local
    access-snapshot cache, so a revoked membership is gone on the next request in
    every process."""

    __tablename__ = "access_epoch"
    __table_args__ = (CheckConstraint("id = 1", name="ck_access_epoch_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    value: Mapped[int] = mapped_column(Integer, default=0)


def _bind_default_project(_mapper, connection, target) -> None:
    """A newly registered workspace starts with the `default` project bound as its
    workspace default (design D-9): every workspace always resolves a project, and a
    DEV → PROD release keeps its project because both sides share `default`."""
    now = _now().strftime("%Y-%m-%d %H:%M:%S.%f")
    connection.execute(
        text(
            "INSERT INTO projects (id, name, description, status, is_default, settings,"
            " created_by, created_by_name, created_at, updated_at)"
            " SELECT 'default', 'Default', '', 'active', 1, NULL, NULL, NULL, :now, :now"
            " WHERE NOT EXISTS (SELECT 1 FROM projects WHERE id = 'default')"
        ),
        {"now": now},
    )
    connection.execute(
        text(
            "INSERT INTO project_workspaces (project_id, workspace_id, is_workspace_default,"
            " enabled_by, enabled_at)"
            " SELECT 'default', :ws, 1, NULL, :now"
            " WHERE NOT EXISTS (SELECT 1 FROM project_workspaces WHERE workspace_id = :ws)"
        ),
        {"ws": target.id, "now": now},
    )
    connection.execute(text("UPDATE access_epoch SET value = value + 1 WHERE id = 1"))


_ACCESS_USER_FIELDS = ("role", "permissions", "status", "expires_at")


def _bump_on_user_access_change(_mapper, connection, target) -> None:
    """An account's role / keys / status feed its `inherit` project keys and its reach:
    any change moves the epoch so every process re-reads the account (design §5.11)."""
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(target)
    if any(state.attrs[name].history.has_changes() for name in _ACCESS_USER_FIELDS):
        connection.execute(text("UPDATE access_epoch SET value = value + 1 WHERE id = 1"))


def _bump_on_user_delete(_mapper, connection, _target) -> None:
    connection.execute(text("UPDATE access_epoch SET value = value + 1 WHERE id = 1"))


def register_workspace_binding() -> None:
    from sqlalchemy import event

    from app.models.ledger import User, Workspace

    for model, name, fn in (
        (Workspace, "after_insert", _bind_default_project),
        (User, "after_update", _bump_on_user_access_change),
        (User, "after_delete", _bump_on_user_delete),
    ):
        if not event.contains(model, name, fn):
            event.listen(model, name, fn)


register_workspace_binding()
