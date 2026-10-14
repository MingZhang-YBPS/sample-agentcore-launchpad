"""Network profiles (design projects §12.3, §12.10, §12.11): workspace VPC settings an
agent spec names by id, and the per-project allowed set that decides who may name one.

Visibility follows design §5.4: a profile outside the current project's allowed set
reads as missing (`network_profile.not_found`), exactly like an id that does not exist.
Administrators see every profile of the workspace (the "configure → network" page).

Nothing here calls AWS: preflight (`POST …/check`, V12-check) fills `check_results` /
`status`; until then a profile is `unchecked`.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Agent
from app.models.network import (
    LEGACY_PREFIX,
    NetworkProfile,
    ProjectNetworkProfile,
    legacy_profile_id,
)

# edits to these move the profile back to `unchecked`: the last preflight no longer
# describes it
_NETWORK_FIELDS = ("vpc_id", "subnet_ids", "security_group_ids", "internet")
_EDITABLE = ("name", "vpc_id", "subnet_ids", "security_group_ids", "purpose", "internet",
             "reachable")


def _now() -> datetime:
    return datetime.now(UTC)


def new_profile_id() -> str:
    return f"np-{uuid.uuid4().hex[:12]}"


def _not_found(profile_id: str) -> NotFoundError:
    return NotFoundError("network_profile.not_found",
                         f"network profile '{profile_id}' not found",
                         {"profile_id": profile_id})


# ── reads ─────────────────────────────────────────────────────────────────────


def _live(db: Session, workspace_id: str, profile_id: str) -> NetworkProfile | None:
    row = db.get(NetworkProfile, profile_id)
    if row is None or row.workspace_id != workspace_id or row.archived_at is not None:
        return None
    return row


def allowed_bindings(
    db: Session, workspace_id: str, project_id: str,
) -> dict[str, ProjectNetworkProfile]:
    """The project's allowed set in this workspace: profile id → binding row."""
    rows = db.scalars(
        select(ProjectNetworkProfile).where(
            ProjectNetworkProfile.workspace_id == workspace_id,
            ProjectNetworkProfile.project_id == project_id,
        )
    )
    return {row.profile_id: row for row in rows}


def _bindings_by_profile(db: Session, workspace_id: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    rows = db.scalars(
        select(ProjectNetworkProfile)
        .where(ProjectNetworkProfile.workspace_id == workspace_id)
        .order_by(ProjectNetworkProfile.project_id)
    )
    for row in rows:
        out.setdefault(row.profile_id, []).append(
            {"project_id": row.project_id, "is_default": bool(row.is_default)}
        )
    return out


def network_profile_id(network: Any) -> str | None:
    """The profile a stored spec's `network` names (None: PUBLIC or legacy inline)."""
    if isinstance(network, dict):
        value = network.get("profile_id")
        return value if isinstance(value, str) else None
    return getattr(network, "profile_id", None)


def agents_using(db: Session, workspace_id: str, profile_id: str | None = None) -> dict[str, int]:
    """Live agents per profile id they name, across every project of the workspace
    (a profile is shared infrastructure; its usage count is not project data)."""
    counts: dict[str, int] = {}
    with project_context(None):
        specs = db.execute(
            select(Agent.spec).where(Agent.workspace_id == workspace_id,
                                     Agent.status != "deleted")
        ).scalars()
        for spec in specs:
            pid = network_profile_id((spec or {}).get("network"))
            if pid and (profile_id is None or pid == profile_id):
                counts[pid] = counts.get(pid, 0) + 1
    return counts


def to_dict(row: NetworkProfile, *, binding: ProjectNetworkProfile | None = None,
            projects: list[dict[str, Any]] | None = None,
            agents: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "vpc_id": row.vpc_id,
        "subnet_ids": list(row.subnet_ids or []),
        "security_group_ids": list(row.security_group_ids or []),
        "purpose": row.purpose,
        "internet": row.internet,
        "reachable": row.reachable or [],
        "status": row.status,
        "check_results": row.check_results,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "legacy": row.id.startswith(LEGACY_PREFIX),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
        # relative to the request's project: may it pick this profile, preselected?
        "allowed": binding is not None,
        "is_default": bool(binding.is_default) if binding is not None else False,
    }
    # administrator view only: who else uses it ("shared by several projects")
    if projects is not None:
        out["projects"] = projects
    if agents is not None:
        out["agents"] = agents
    return out


def list_profiles(
    db: Session, workspace_id: str, project_id: str | None, *, see_all: bool,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Members: the current project's allowed set. Administrators: the workspace's
    profiles, each with its projects and agent count."""
    allowed = allowed_bindings(db, workspace_id, project_id) if project_id else {}
    query = select(NetworkProfile).where(NetworkProfile.workspace_id == workspace_id)
    if not (see_all and include_archived):
        query = query.where(NetworkProfile.archived_at.is_(None))
    if not see_all:
        if not allowed:
            return []
        query = query.where(NetworkProfile.id.in_(sorted(allowed)))
    rows = list(db.scalars(query.order_by(NetworkProfile.name, NetworkProfile.id)))
    if not see_all:
        return [to_dict(row, binding=allowed.get(row.id)) for row in rows]
    projects = _bindings_by_profile(db, workspace_id)
    usage = agents_using(db, workspace_id)
    return [
        to_dict(row, binding=allowed.get(row.id), projects=projects.get(row.id, []),
                agents=usage.get(row.id, 0))
        for row in rows
    ]


def get_profile(
    db: Session, workspace_id: str, profile_id: str, project_id: str | None, *, see_all: bool,
) -> dict[str, Any]:
    row = _live(db, workspace_id, profile_id)
    binding = (
        db.get(ProjectNetworkProfile, (project_id, profile_id)) if project_id else None
    )
    if row is None or (not see_all and binding is None):
        raise _not_found(profile_id)
    if not see_all:
        return to_dict(row, binding=binding)
    return to_dict(row, binding=binding,
                   projects=_bindings_by_profile(db, workspace_id).get(row.id, []),
                   agents=agents_using(db, workspace_id, row.id).get(row.id, 0))


# ── writes (administrators) ───────────────────────────────────────────────────


def _assert_name_free(db: Session, workspace_id: str, name: str, *, except_id: str = "") -> None:
    taken = db.scalar(
        select(NetworkProfile.id).where(
            NetworkProfile.workspace_id == workspace_id,
            NetworkProfile.name == name,
            NetworkProfile.archived_at.is_(None),
            NetworkProfile.id != except_id,
        )
    )
    if taken is not None:
        raise AppError("network_profile.name_exists",
                       f"a network profile named '{name}' already exists in this workspace",
                       {"profile_id": taken}, status_code=409)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def create_profile(db: Session, workspace_id: str, fields: dict[str, Any], actor: str) -> dict:
    _assert_name_free(db, workspace_id, fields["name"])
    row = NetworkProfile(
        id=new_profile_id(),
        workspace_id=workspace_id,
        name=fields["name"],
        vpc_id=fields["vpc_id"],
        subnet_ids=_dedupe(fields["subnet_ids"]),
        security_group_ids=_dedupe(fields["security_group_ids"]),
        purpose=fields.get("purpose") or "agents",
        internet=fields.get("internet") or "none",
        reachable=fields.get("reachable") or None,
        status="unchecked",
        created_by=actor,
    )
    db.add(row)
    db.commit()
    return to_dict(row, projects=[], agents=0)


def update_profile(
    db: Session, workspace_id: str, profile_id: str, fields: dict[str, Any],
) -> dict[str, Any]:
    row = _live(db, workspace_id, profile_id)
    if row is None:
        raise _not_found(profile_id)
    changes = {k: v for k, v in fields.items() if k in _EDITABLE and v is not None}
    if "name" in changes and changes["name"] != row.name:
        _assert_name_free(db, workspace_id, changes["name"], except_id=row.id)
    for key in ("subnet_ids", "security_group_ids"):
        if key in changes:
            changes[key] = _dedupe(changes[key])
    reshaped = any(
        key in changes and changes[key] != getattr(row, key) for key in _NETWORK_FIELDS
    )
    for key, value in changes.items():
        setattr(row, key, (value or None) if key == "reachable" else value)
    if reshaped:
        row.status = "unchecked"
        row.check_results = None
        row.checked_at = None
    row.updated_at = _now()
    db.commit()
    return get_profile(db, workspace_id, profile_id, None, see_all=True)


def archive_profile(db: Session, workspace_id: str, profile_id: str) -> None:
    """Archive (the ledger keeps the row: snapshots may still name it). Refused while a
    live agent names it — its runtime's ENIs sit in these subnets (design §12.9)."""
    row = _live(db, workspace_id, profile_id)
    if row is None:
        raise _not_found(profile_id)
    used = agents_using(db, workspace_id, profile_id).get(profile_id, 0)
    if used:
        raise AppError("network_profile.in_use",
                       f"{used} agent(s) still use network profile '{row.name}' — move "
                       "them to another network first",
                       {"profile_id": profile_id, "agents": used}, status_code=409)
    row.archived_at = _now()
    row.updated_at = row.archived_at
    for binding in allowed_bindings_of_profile(db, profile_id):
        db.delete(binding)
    db.commit()


def allowed_bindings_of_profile(db: Session, profile_id: str) -> list[ProjectNetworkProfile]:
    return list(db.scalars(
        select(ProjectNetworkProfile).where(ProjectNetworkProfile.profile_id == profile_id)
    ))


# ── the per-project allowed set ───────────────────────────────────────────────


def assert_project_in_workspace(db: Session, workspace_id: str, project_id: str) -> None:
    from app.models.projects import ProjectWorkspace

    if db.get(ProjectWorkspace, (project_id, workspace_id)) is None:
        raise NotFoundError("project.not_found", f"project '{project_id}' not found",
                            {"project_id": project_id})


def project_profiles(db: Session, workspace_id: str, project_id: str) -> dict[str, Any]:
    assert_project_in_workspace(db, workspace_id, project_id)
    allowed = allowed_bindings(db, workspace_id, project_id)
    rows = db.scalars(
        select(NetworkProfile).where(NetworkProfile.id.in_(sorted(allowed)),
                                     NetworkProfile.archived_at.is_(None))
        .order_by(NetworkProfile.name)
    ) if allowed else []
    profiles = [to_dict(row, binding=allowed[row.id]) for row in rows]
    default = next((p["id"] for p in profiles if p["is_default"]), None)
    return {"project_id": project_id, "workspace_id": workspace_id,
            "profiles": profiles, "default_profile_id": default}


def set_project_profiles(
    db: Session, workspace_id: str, project_id: str, profile_ids: list[str],
    default_profile_id: str | None,
) -> dict[str, Any]:
    """Replace the project's allowed set in this workspace (and its default)."""
    assert_project_in_workspace(db, workspace_id, project_id)
    wanted = _dedupe(profile_ids)
    for pid in wanted:
        if _live(db, workspace_id, pid) is None:
            raise _not_found(pid)
    if default_profile_id is not None and default_profile_id not in wanted:
        raise AppError("network_profile.default_not_allowed",
                       "the default network profile must be one of the allowed profiles",
                       {"profile_id": default_profile_id}, status_code=400)
    current = allowed_bindings(db, workspace_id, project_id)
    for pid, binding in current.items():
        if pid not in wanted:
            db.delete(binding)
    for pid in wanted:
        binding = current.get(pid)
        if binding is None:
            binding = ProjectNetworkProfile(project_id=project_id, profile_id=pid,
                                            workspace_id=workspace_id)
            db.add(binding)
        binding.is_default = pid == default_profile_id
    db.commit()
    return project_profiles(db, workspace_id, project_id)


# ── agent specs (the deploy path: V12-deploy wires these in) ──────────────────


def usable_profile(db: Session, workspace_id: str, project_id: str | None,
                   profile_id: str) -> NetworkProfile:
    """The profile a spec in `project_id` may name: allowed (else 404, design §12.11)
    and not blocked by its last preflight (409)."""
    row = _live(db, workspace_id, profile_id)
    if row is None or (
        project_id is not None and db.get(ProjectNetworkProfile, (project_id, profile_id)) is None
    ):
        raise _not_found(profile_id)
    if row.status == "blocked":
        raise AppError("network_profile.blocked",
                       f"network profile '{row.name}' failed its preflight — fix it, re-run "
                       "the check, or choose another network",
                       {"profile_id": profile_id}, status_code=409)
    return row


def resolve_vpc(db: Session, workspace_id: str, network: Any) -> dict[str, list[str]] | None:
    """`{subnets, security_groups}` for a spec's network (None = PUBLIC) — what the
    deployers pass to `runtime._network_configuration`. Reads the profile whatever its
    status: a redeploy of an existing agent keeps its network (preflight gates new
    choices, `usable_profile`)."""
    if network is None:
        return None
    pid = network_profile_id(network)
    if pid is None:
        data = network if isinstance(network, dict) else network.model_dump()
        return {"subnets": list(data["subnets"]), "security_groups": list(data["security_groups"])}
    row = db.get(NetworkProfile, pid)
    if row is None or row.workspace_id != workspace_id:
        raise _not_found(pid)
    return {"subnets": list(row.subnet_ids or []),
            "security_groups": list(row.security_group_ids or [])}


def normalize_spec_network(
    db: Session, workspace_id: str, project_id: str | None, spec: dict[str, Any], *,
    is_admin: bool, actor: str,
) -> dict[str, Any]:
    """An incoming spec's network in its stored form (design §12.10-2).

    `{profile_id}` must be usable by the project. Inline subnets / SGs from an older
    client: an administrator's are matched to (or create) the `legacy-<hash8>` profile
    and added to the project's allowed set; anyone else is asked to pick a profile."""
    network = spec.get("network")
    if not network:
        return spec
    pid = network_profile_id(network)
    if pid is not None:
        usable_profile(db, workspace_id, project_id, pid)
        return spec
    if not is_admin:
        raise AppError("network.inline_not_allowed",
                       "choose a network profile — inline subnets and security groups are "
                       "accepted from administrators only", status_code=400)
    subnets = list(network.get("subnets") or [])
    groups = list(network.get("security_groups") or [])
    pid = legacy_profile_id(workspace_id, subnets, groups)
    row = db.get(NetworkProfile, pid)
    if row is None:
        db.add(NetworkProfile(id=pid, workspace_id=workspace_id, name=pid, vpc_id=None,
                              subnet_ids=_dedupe(subnets),
                              security_group_ids=_dedupe(groups), purpose="agents",
                              internet="none", status="unchecked", created_by=actor))
    elif row.archived_at is not None:
        row.archived_at = None
        row.updated_at = _now()
    if project_id is not None and db.get(ProjectNetworkProfile, (project_id, pid)) is None:
        db.add(ProjectNetworkProfile(project_id=project_id, profile_id=pid,
                                     workspace_id=workspace_id, is_default=False))
    db.commit()
    return {**spec, "network": {"profile_id": pid}}
