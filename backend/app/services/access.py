"""Who reaches which project, with which keys (design §2.4, §3, §5.1, §5.11).

The one place project access is decided:

* ``ROLE_TEMPLATES`` — built-in project roles, code constants (not rows), so a new
  permission key is granted by code the way ``users.DEFAULT_BY_ROLE`` grants one.
* ``project_permissions`` — §3.4: direct role ∪ group role, then the direct row's
  overrides; ``inherit`` reads the account's global keys.
* ``AccessSnapshot`` — one query per user (all their memberships), cached in-process
  and dropped whenever ``access_epoch`` moves (bumped in the same transaction as
  every access-affecting write), so a revoked membership is gone on the next request.
* ``resolve_project`` — the request's project from ``X-Project`` (or the §2.6
  fallback); anything the caller cannot reach answers 404 ``project.not_found``.
* ``has_data_bypass`` / ``can_manage_structure`` — the only places "an admin can"
  is decided for project data and structure (phase 3's steward changes just these);
  ``can_manage_structure(identity, department_id, db=db)`` adds the department
  administrators of §11.1 for that department's projects and groups. A steward
  (§11.4) is the platform structure manager without data: ``has_data_bypass`` False,
  ``can_manage_structure`` True, and its snapshot always empty (no membership counts).
* ``shared_for`` / ``shared_rows`` — what other projects shared with a project
  (§11.2, ``resource_shares``): per kind the resource ids and their access level, and
  per shareable table the row ids its reads may see. Cached per project like the
  snapshot (epoch + TTL), and dropped early once the first of its shares expires.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.services.users import (
    AGENT_PERMISSIONS,
    PLATFORM_ROLES,
    ROLE_ADMIN,
    ROLE_STEWARD,
    default_permissions,
)

PROJECT_HEADER = "X-Project"

# ── keys and templates ────────────────────────────────────────────────────────

# project-level keys (§3.1). Never part of users.permissions or /api/auth/status.
PROJECT_PERMISSIONS = (
    "project.use",      # participate: chat / invoke, feedback, labels, own sessions
    "project.write",    # ordinary writes inside the project
    "project.members",  # manage this project's members and custom roles
    "project.audit",    # phase 3: the project's audit and cost views
    "project.share",    # phase 3: share this project's resources with another
    # §15.6/§15.9: approve a hosted MCP server's tool list (never the deployer's own).
    # A project key, not an AGENT_PERMISSIONS one, so /api/auth/status keeps its list.
    "mcp.approve",
)
ALL_PROJECT_KEYS = frozenset((*AGENT_PERMISSIONS, *PROJECT_PERMISSIONS))
INHERIT_ROLE = "inherit"

_MEMBER_DEFAULT = (
    "agents.deploy", "agents.import", "agents.delete", "agents.convert", "eval.run",
    "promotion.request", "identity.manage", "identity.grant", "memory.manage",
    "criteria.manage",
)
_OPERATOR_DEFAULT = (
    "eval.run", "promotion.request", "promotion.approve", "identity.grant",
    "criteria.manage", "waiver.approve", "release.sign",
)


@dataclass(frozen=True)
class RoleTemplate:
    key: str
    keys: frozenset[str]


ROLE_TEMPLATES: dict[str, RoleTemplate] = {
    template.key: template
    for template in (
        # manages people + every build key; deliberately NOT the approve / sign keys
        RoleTemplate("project_owner", frozenset({
            "project.members", "project.audit", "project.share", "project.use", "project.write",
            "agents.deploy", "agents.import", "agents.delete", "agents.convert", "eval.run",
            "promotion.request", "identity.manage", "identity.grant", "memory.manage",
            "criteria.manage",
        })),
        RoleTemplate("developer", frozenset({"project.use", "project.write", *_MEMBER_DEFAULT})),
        RoleTemplate("biz_owner", frozenset({
            "project.use", "project.write", "eval.run", "criteria.manage", "criteria.sign",
            "golden.admit", "judge.calibrate", "identity.grant",
        })),
        RoleTemplate(
            "release_ops",
            frozenset({"project.use", "project.write", "mcp.approve", *_OPERATOR_DEFAULT}),
        ),
        RoleTemplate("sme", frozenset({"project.use", "identity.grant"})),
        # §15.9: reviews MCP servers' tool lists (and nothing it could approve for itself)
        RoleTemplate("tool_steward", frozenset({"project.use", "mcp.approve"})),
        RoleTemplate("viewer", frozenset()),
    )
}
BUILTIN_ROLE_KEYS = frozenset((*ROLE_TEMPLATES, INHERIT_ROLE))
# the template a global role suggests when its account joins a new project (§3.7)
SUGGESTED_TEMPLATE = {"member": "developer", "operator": "release_ops"}

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# §15.9: the global key an `inherit` member must hold to approve MCP tool lists — the
# approver's key (operators by default), mirroring `release_ops` holding mcp.approve
_INHERIT_MCP_APPROVE_FROM = "promotion.approve"


def inherit_keys(role: str, overrides: dict[str, bool] | None) -> frozenset[str]:
    """§3.5: the account's global keys (today's 16-key computation) + use/write, and
    mcp.approve for an account holding the approver key."""
    defaults = default_permissions(role)
    granted = {k for k in AGENT_PERMISSIONS if (overrides or {}).get(k, k in defaults)}
    extra = {"mcp.approve"} if _INHERIT_MCP_APPROVE_FROM in granted else set()
    return frozenset(granted | extra | {"project.use", "project.write"})


def template_keys(
    role_key: str,
    custom_roles: dict[str, frozenset[str]],
    *,
    account_role: str,
    account_overrides: dict[str, bool] | None,
) -> frozenset[str]:
    if role_key == INHERIT_ROLE:
        return inherit_keys(account_role, account_overrides)
    builtin = ROLE_TEMPLATES.get(role_key)
    if builtin is not None:
        return builtin.keys
    return custom_roles.get(role_key, frozenset())


def apply_overrides(base: frozenset[str], overrides: dict[str, bool] | None) -> frozenset[str]:
    keys = set(base)
    for key, granted in (overrides or {}).items():
        if key not in ALL_PROJECT_KEYS:
            continue
        if granted:
            keys.add(key)
        else:
            keys.discard(key)
    return frozenset(keys)


# ── who: the identity's bypass ────────────────────────────────────────────────


def has_data_bypass(identity: Any) -> bool:
    """May see project data without being a member: platform admins only — never a
    steward (§11.4)."""
    return getattr(identity, "role", None) == ROLE_ADMIN


_ANY_DEPARTMENT: Any = object()  # `department_id` omitted: the whole platform's structure


def can_manage_structure(
    identity: Any, department_id: Any = _ANY_DEPARTMENT, *, db: Session | None = None,
) -> bool:
    """May manage structure. Without `department_id`: the platform's — departments,
    workspaces, every project and group (platform administrators). With one (§11.1):
    that department's projects and groups — an administrator, or one of the
    department's administrators (`department_admins`; needs `db`). A project or group
    with no department is the platform's only. Stewards (§11.4) manage all of it."""
    if getattr(identity, "role", None) in PLATFORM_ROLES:
        return True
    if department_id is _ANY_DEPARTMENT or not department_id or db is None:
        return False
    return department_id in managed_departments(db, identity)


def require_structure(request: Request) -> Any:
    """Route dependency: the caller manages the platform's structure — an administrator
    or a steward (§11.4). 403 `auth.forbidden` like `require_admin` otherwise."""
    from app.routers.auth import require_identity

    identity = require_identity(request)
    if not can_manage_structure(identity):
        raise AppError("auth.forbidden", "This action requires an administrator account",
                       status_code=403)
    return identity


def managed_departments(db: Session, identity: Any) -> frozenset[str]:
    """The active departments the caller administers (§11.1) — rows only, so empty
    for a platform administrator, who manages every department by role anyway. Read
    per call (one indexed query), not from the access snapshot: it is authorization
    for structure writes, never data access."""
    user_id = getattr(identity, "user_id", None)
    if not user_id:
        return frozenset()
    return frozenset(r[0] for r in db.execute(
        text("SELECT da.department_id FROM department_admins da"
             " JOIN departments d ON d.id = da.department_id"
             " WHERE da.user_id = :uid AND d.archived_at IS NULL"),
        {"uid": user_id},
    ).fetchall())


def open_workspaces(db: Session, department_id: str | None) -> frozenset[str]:
    """The workspaces opened to a department (§11.1): where its administrators may
    bind its projects."""
    if not department_id:
        return frozenset()
    return frozenset(r[0] for r in db.execute(
        text("SELECT workspace_id FROM department_workspaces WHERE department_id = :d"),
        {"d": department_id},
    ).fetchall())


# ── the snapshot ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Membership:
    project_id: str
    name: str
    status: str
    is_default: bool
    workspaces: frozenset[str]
    workspace_defaults: frozenset[str]
    direct_role: str | None
    group_role: str | None
    group_id: str | None
    overrides: dict[str, bool] | None
    permissions: frozenset[str]

    @property
    def role_sources(self) -> tuple[tuple[str, str], ...]:
        out: list[tuple[str, str]] = []
        if self.direct_role:
            out.append(("direct", self.direct_role))
        if self.group_role and self.group_id:
            out.append((f"group:{self.group_id}", self.group_role))
        return tuple(out)


@dataclass(frozen=True)
class Binding:
    project_id: str
    name: str
    status: str
    is_default: bool
    workspace_id: str
    is_workspace_default: bool


@dataclass
class AccessSnapshot:
    user_id: str | None
    memberships: dict[str, Membership] = field(default_factory=dict)

    def reachable(self, workspace_id: str) -> list[Membership]:
        return [m for m in self.memberships.values() if workspace_id in m.workspaces]


_cache: OrderedDict[str, tuple[int, float, AccessSnapshot]] = OrderedDict()
_bindings_cache: dict[str, tuple[int, float, list[Binding]]] = {}
_lock = threading.Lock()
_TTL_S = 60.0
_MAX_USERS = 5000


def current_epoch(db: Session) -> int:
    row = db.execute(text("SELECT value FROM access_epoch WHERE id = 1")).first()
    return int(row[0]) if row else 0


def bump_epoch(db: Session) -> None:
    """Call in the SAME transaction as any write that changes who reaches what."""
    db.execute(text("UPDATE access_epoch SET value = value + 1 WHERE id = 1"))


def reset_cache_for_tests() -> None:
    with _lock:
        _cache.clear()
        _bindings_cache.clear()
        _shared_cache.clear()


def workspace_bindings(
    db: Session, workspace_id: str, *, epoch: int | None = None,
) -> list[Binding]:
    """The projects bound to one workspace (active or archived), default first."""
    epoch = current_epoch(db) if epoch is None else epoch
    now = time.monotonic()
    with _lock:
        hit = _bindings_cache.get(workspace_id)
        if hit and hit[0] == epoch and now - hit[1] < _TTL_S:
            return hit[2]
    rows = db.execute(
        text(
            "SELECT p.id, p.name, p.status, p.is_default, pw.workspace_id, pw.is_workspace_default"
            " FROM project_workspaces pw JOIN projects p ON p.id = pw.project_id"
            " WHERE pw.workspace_id = :ws AND p.status IN ('active', 'archived')"
            " ORDER BY pw.is_workspace_default DESC, p.name"
        ),
        {"ws": workspace_id},
    ).fetchall()
    bindings = [Binding(r[0], r[1], r[2], bool(r[3]), r[4], bool(r[5])) for r in rows]
    with _lock:
        _bindings_cache[workspace_id] = (epoch, now, bindings)
    return bindings


def _load_snapshot(
    db: Session, user_id: str, role: str, overrides: dict[str, bool] | None,
) -> AccessSnapshot:
    if role == ROLE_STEWARD:
        # §11.4: whatever rows linger, a steward reaches no project's data
        return AccessSnapshot(user_id=user_id)
    rows = db.execute(
        text(
            "SELECT p.id, p.name, p.status, p.is_default, p.group_id, pw.workspace_id,"
            " pw.is_workspace_default, pm.role_key, pm.overrides, gm.role_key"
            " FROM projects p"
            " JOIN project_workspaces pw ON pw.project_id = p.id"
            " LEFT JOIN project_members pm ON pm.project_id = p.id AND pm.user_id = :uid"
            " LEFT JOIN project_groups g ON g.id = p.group_id AND g.archived_at IS NULL"
            " LEFT JOIN group_members gm ON gm.group_id = g.id AND gm.user_id = :uid"
            " WHERE p.status IN ('active', 'archived')"
            " AND (pm.user_id IS NOT NULL OR gm.user_id IS NOT NULL)"
        ),
        {"uid": user_id},
    ).fetchall()
    custom: dict[str, dict[str, frozenset[str]]] = {}
    project_ids = {r[0] for r in rows}
    if project_ids:
        for pid, key, perms in db.execute(
            text("SELECT project_id, key, permissions FROM project_roles"
                 " WHERE project_id IN :ids").bindparams(bindparam("ids", expanding=True)),
            {"ids": sorted(project_ids)},
        ).fetchall():
            keys = perms if isinstance(perms, list) else _json_list(perms)
            custom.setdefault(pid, {})[key] = frozenset(keys) & ALL_PROJECT_KEYS
    grouped: dict[str, dict[str, Any]] = {}
    for pid, name, status, is_default, group_id, ws, ws_default, direct, ovr, group_role in rows:
        entry = grouped.setdefault(pid, {
            "name": name, "status": status, "is_default": bool(is_default), "group_id": group_id,
            "workspaces": set(), "defaults": set(), "direct": direct, "group": group_role,
            "overrides": ovr if isinstance(ovr, dict) else _json_dict(ovr),
        })
        entry["workspaces"].add(ws)
        if ws_default:
            entry["defaults"].add(ws)
    # an `inherit` membership (the default project's compatibility row) reaches only
    # the workspaces the account itself is granted — see granted_workspace_ids
    direct_grants = {
        r[0] for r in db.execute(
            text("SELECT workspace_id FROM user_workspaces WHERE user_id = :uid"), {"uid": user_id},
        ).fetchall()
    }
    snapshot = AccessSnapshot(user_id=user_id)
    for pid, e in grouped.items():
        if e["direct"] == INHERIT_ROLE and not e["group"]:
            e["workspaces"] &= direct_grants
            e["defaults"] &= direct_grants
            if not e["workspaces"]:
                continue
        roles = custom.get(pid, {})
        base: frozenset[str] = frozenset()
        if e["direct"]:
            base |= template_keys(
                e["direct"], roles, account_role=role, account_overrides=overrides,
            )
        if e["group"]:
            base |= template_keys(
                e["group"], roles, account_role=role, account_overrides=overrides,
            )
        snapshot.memberships[pid] = Membership(
            project_id=pid, name=e["name"], status=e["status"], is_default=e["is_default"],
            workspaces=frozenset(e["workspaces"]), workspace_defaults=frozenset(e["defaults"]),
            direct_role=e["direct"], group_role=e["group"], group_id=e["group_id"],
            overrides=e["overrides"], permissions=apply_overrides(base, e["overrides"]),
        )
    return snapshot


def load_snapshot(
    db: Session, user_id: str, role: str, overrides: dict[str, bool] | None,
) -> AccessSnapshot:
    """An uncached snapshot — for checks inside a write transaction that must see
    its own not-yet-committed membership changes."""
    return _load_snapshot(db, user_id, role, overrides)


def _json_list(value: Any) -> list[str]:
    import json

    try:
        out = json.loads(value) if isinstance(value, str) else []
    except ValueError:
        return []
    return [str(v) for v in out] if isinstance(out, list) else []


def _json_dict(value: Any) -> dict[str, bool] | None:
    import json

    if value is None:
        return None
    try:
        out = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return {str(k): bool(v) for k, v in out.items()} if isinstance(out, dict) else None


def snapshot_for(db: Session, identity: Any, *, epoch: int | None = None) -> AccessSnapshot:
    """The caller's memberships, from the epoch-checked cache."""
    user_id = getattr(identity, "user_id", None)
    if not user_id:
        return AccessSnapshot(user_id=None)
    role = getattr(identity, "role", "member")
    overrides = getattr(identity, "permission_overrides", None)
    if overrides is None:
        found = db.execute(
            text("SELECT permissions FROM users WHERE id = :id"), {"id": user_id},
        ).first()
        raw = found[0] if found else None
        overrides = raw if isinstance(raw, dict) else _json_dict(raw)
    epoch = current_epoch(db) if epoch is None else epoch
    now = time.monotonic()
    key = f"{user_id}:{role}"
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == epoch and now - hit[1] < _TTL_S:
            _cache.move_to_end(key)
            return hit[2]
    snapshot = _load_snapshot(db, user_id, role, overrides)
    with _lock:
        _cache[key] = (epoch, now, snapshot)
        _cache.move_to_end(key)
        while len(_cache) > _MAX_USERS:
            _cache.popitem(last=False)
    return snapshot


def reachable_workspace_ids(db: Session, identity: Any) -> set[str]:
    """Workspaces the caller reaches through a project membership (design D-6)."""
    snapshot = snapshot_for(db, identity)
    out: set[str] = set()
    for membership in snapshot.memberships.values():
        out |= membership.workspaces
    return out


# ── what other projects shared with this one (§11.2) ──────────────────────────


@dataclass(frozen=True)
class SharedResources:
    """The active, unexpired shares one project receives."""

    # kind -> {resource_id: "read" | "use"}
    by_kind: dict[str, dict[str, str]] = field(default_factory=dict)
    # kind -> {resource_id: owner project}
    owners: dict[str, dict[str, str]] = field(default_factory=dict)
    # shareable table -> the row ids the project's reads may see
    rows: dict[str, frozenset[str]] = field(default_factory=dict)
    # the earliest expiry among them: past it the cached value is stale
    valid_until: Any = None

    def ids(self, kind: str, *, use: bool = False) -> frozenset[str]:
        granted = self.by_kind.get(kind) or {}
        return frozenset(rid for rid, level in granted.items() if not use or level == "use")


_NO_SHARES = SharedResources()
_shared_cache: dict[str, tuple[int, float, SharedResources]] = {}
_MAX_SHARED_PROJECTS = 5000


def shared_for(db: Session, project_id: str | None, *, epoch: int | None = None
               ) -> SharedResources:
    """The shares ``project_id`` receives, from the epoch-checked cache (every share
    write bumps the epoch; the scheduler's expiry sweep too)."""
    if not project_id:
        return _NO_SHARES
    from datetime import UTC, datetime

    epoch = current_epoch(db) if epoch is None else epoch
    now = time.monotonic()
    with _lock:
        hit = _shared_cache.get(project_id)
    fresh = hit and hit[0] == epoch and now - hit[1] < _TTL_S
    if fresh and not _expired(hit[2], datetime.now(UTC)):
        return hit[2]
    from app.services import resource_shares

    value = resource_shares.load_received(db, project_id)
    with _lock:
        if len(_shared_cache) >= _MAX_SHARED_PROJECTS:
            _shared_cache.clear()
        _shared_cache[project_id] = (epoch, now, value)
    return value


def _expired(value: SharedResources, now: Any) -> bool:
    until = value.valid_until
    if until is None:
        return False
    if until.tzinfo is None:
        from datetime import UTC

        until = until.replace(tzinfo=UTC)
    return until <= now


def shared_rows(db: Session, project_id: str) -> dict[str, frozenset[str]]:
    """``{table: row ids}`` for the ORM criteria (``core/project_scope.py``)."""
    return shared_for(db, project_id).rows


# ── the request's project ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectScope:
    id: str
    name: str
    status: str
    is_default: bool
    workspace_id: str
    permissions: frozenset[str]
    role_sources: tuple[tuple[str, str], ...]
    # an administrator reaching a project they are not a member of
    bypass: bool

    def can(self, key: str) -> bool:
        return self.bypass or key in self.permissions


def _not_found(project_id: str) -> NotFoundError:
    return NotFoundError("project.not_found", f"project '{project_id}' not found",
                         {"project_id": project_id})


def resolve_project(db: Session, identity: Any, workspace_id: str, requested: str,
                    method: str) -> ProjectScope:
    """§5.1.1: the project a workspace-scoped request runs in."""
    epoch = current_epoch(db)  # one PK read per request (design §5.11)
    bindings = workspace_bindings(db, workspace_id, epoch=epoch)
    bypass = has_data_bypass(identity)
    snapshot = (
        AccessSnapshot(user_id=None) if bypass else snapshot_for(db, identity, epoch=epoch)
    )
    reachable = {m.project_id: m for m in snapshot.reachable(workspace_id)}
    bound = {b.project_id: b for b in bindings}
    pid = requested
    if not pid:
        if bypass:
            default = next((b for b in bindings if b.is_workspace_default), None) or (
                bindings[0] if bindings else None
            )
            if default is None:
                raise AppError("project.unbound",
                               f"workspace '{workspace_id}' has no project", status_code=409)
            pid = default.project_id
        elif len(reachable) == 1:
            pid = next(iter(reachable))
        elif not reachable:
            raise AppError("project.none", "this account is not a member of any project in "
                           f"workspace '{workspace_id}'", status_code=403)
        else:
            default = next(
                (m for m in reachable.values() if workspace_id in m.workspace_defaults), None,
            )
            if default is None:
                raise AppError(
                    "project.header_required",
                    f"send {PROJECT_HEADER} — this account has several projects here",
                    {"available": sorted(reachable)}, status_code=400,
                )
            pid = default.project_id
    if pid not in bound:
        raise _not_found(pid)
    if bypass:
        binding = bound[pid]
        scope = ProjectScope(
            id=pid, name=binding.name, status=binding.status, is_default=binding.is_default,
            workspace_id=workspace_id, permissions=ALL_PROJECT_KEYS, role_sources=(), bypass=True,
        )
    else:
        membership = reachable.get(pid)
        if membership is None:
            raise _not_found(pid)
        scope = ProjectScope(
            id=pid, name=membership.name, status=membership.status,
            is_default=membership.is_default, workspace_id=workspace_id,
            permissions=membership.permissions, role_sources=membership.role_sources,
            bypass=False,
        )
    if method not in READ_METHODS and scope.status != "active":
        raise AppError("project.archived", f"project '{pid}' is archived", status_code=409)
    return scope


# ── keeping the default project in step with workspace grants (§8.5) ─────────


def sync_default_membership(db: Session, user_id: str, workspace_ids: set[str] | None,
                            *, previous: set[str] | None = None, role: str = "member") -> None:
    """Mirror a user's workspace grants onto each workspace's default project.

    A new grant adds an ``inherit`` membership of that workspace's default project; a
    revoked one removes the ``inherit`` row (an explicit role is left alone). Admins
    never get rows (they bypass), nor do stewards (they reach no data). Same transaction
    as the grant write; bumps the epoch.
    """
    if role in PLATFORM_ROLES:
        return
    granted = set(workspace_ids or set())
    before = set(previous or set())
    changed = False
    for ws in sorted(granted - before):
        row = db.execute(
            text("SELECT project_id FROM project_workspaces"
                 " WHERE workspace_id = :ws AND is_workspace_default = 1"),
            {"ws": ws},
        ).first()
        if row is None:
            continue
        exists = db.execute(
            text("SELECT 1 FROM project_members WHERE project_id = :p AND user_id = :u"),
            {"p": row[0], "u": user_id},
        ).first()
        if exists is None:
            db.execute(
                text("INSERT INTO project_members (project_id, user_id, role_key, overrides,"
                     " added_by, added_at) VALUES (:p, :u, 'inherit', NULL, NULL, :now)"),
                {"p": row[0], "u": user_id, "now": _now_sql()},
            )
            changed = True
    for ws in sorted(before - granted):
        row = db.execute(
            text("SELECT project_id FROM project_workspaces"
                 " WHERE workspace_id = :ws AND is_workspace_default = 1"),
            {"ws": ws},
        ).first()
        if row is None:
            continue
        # only when no OTHER still-granted workspace has the same default project
        still = any(
            db.execute(
                text("SELECT 1 FROM project_workspaces WHERE workspace_id = :ws"
                     " AND project_id = :p AND is_workspace_default = 1"),
                {"ws": other, "p": row[0]},
            ).first() is not None
            for other in granted
        )
        if still:
            continue
        result = db.execute(
            text("DELETE FROM project_members WHERE project_id = :p AND user_id = :u"
                 " AND role_key = 'inherit'"),
            {"p": row[0], "u": user_id},
        )
        changed = changed or bool(result.rowcount)
    # any grant change moves reach (an inherit row's workspaces are the account's
    # grants), even when no membership row was added or removed
    if changed or granted != before:
        bump_epoch(db)


def _now_sql() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
