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
  is decided for project data and structure (phase 3's steward changes just these).
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.services.users import AGENT_PERMISSIONS, ROLE_ADMIN, default_permissions

PROJECT_HEADER = "X-Project"

# ── keys and templates ────────────────────────────────────────────────────────

# project-level keys (§3.1). Never part of users.permissions or /api/auth/status.
PROJECT_PERMISSIONS = (
    "project.use",      # participate: chat / invoke, feedback, labels, own sessions
    "project.write",    # ordinary writes inside the project
    "project.members",  # manage this project's members and custom roles
    "project.audit",    # phase 3: the project's audit and cost views
    "project.share",    # phase 3: share this project's resources with another
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
            "release_ops", frozenset({"project.use", "project.write", *_OPERATOR_DEFAULT}),
        ),
        RoleTemplate("sme", frozenset({"project.use", "identity.grant"})),
        RoleTemplate("viewer", frozenset()),
    )
}
BUILTIN_ROLE_KEYS = frozenset((*ROLE_TEMPLATES, INHERIT_ROLE))
# the template a global role suggests when its account joins a new project (§3.7)
SUGGESTED_TEMPLATE = {"member": "developer", "operator": "release_ops"}

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def inherit_keys(role: str, overrides: dict[str, bool] | None) -> frozenset[str]:
    """§3.5: the account's global keys (today's 16-key computation) + use/write."""
    defaults = default_permissions(role)
    granted = {k for k in AGENT_PERMISSIONS if (overrides or {}).get(k, k in defaults)}
    return frozenset(granted | {"project.use", "project.write"})


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
    """May see project data without being a member (phase 1: platform admins)."""
    return getattr(identity, "role", None) == ROLE_ADMIN


def can_manage_structure(identity: Any) -> bool:
    """May create projects, groups, departments and bind workspaces (phase 1: admins)."""
    return getattr(identity, "role", None) == ROLE_ADMIN


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


def workspace_bindings(db: Session, workspace_id: str) -> list[Binding]:
    """The projects bound to one workspace (active or archived), default first."""
    epoch = current_epoch(db)
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
            text("SELECT project_id, key, permissions FROM project_roles"),
        ).fetchall():
            if pid in project_ids:
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


def snapshot_for(db: Session, identity: Any) -> AccessSnapshot:
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
    epoch = current_epoch(db)
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
    bindings = workspace_bindings(db, workspace_id)
    bypass = has_data_bypass(identity)
    snapshot = AccessSnapshot(user_id=None) if bypass else snapshot_for(db, identity)
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
    never get rows (they bypass). Same transaction as the grant write; bumps the epoch.
    """
    if role == ROLE_ADMIN:
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
    if changed:
        bump_epoch(db)


def _now_sql() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
