"""Logical resource mapping and spec resolution (roadmap T23).

A bundle's spec carries ids that only exist in the environment it was built in. This
module finds those references, names each one logically, and — given a target workspace —
either rewrites the spec to the target's ids or reports exactly which references have no
mapping there.

**What counts as an environment-specific reference** (kept to what `AgentSpec` actually
carries; a reference the platform does not model cannot be found here):

* `knowledge_bases[].kb_id`         -> kind `kb`
* `memory.memory_id`                -> kind `memory`
* `tools[type=gateway].config.gateway_id` -> kind `gateway`
* `tools[type=gateway].config.record_id`  -> kind `mcp_record` (the approved Registry record)
* `skills[]` entries that are `s3://` prefixes -> kind `skill` (the bucket is per account;
  filesystem-path skills are environment-neutral and left alone)

Free-form `env` values and the code inside a BYOC artifact are NOT scanned: the platform
cannot tell an id from any other string there. That is a stated limit, not an oversight.

**Naming a reference.** The bundle stores the SOURCE id. To look it up in the target we
need a logical name, found in this order: (1) a mapping in the SOURCE workspace whose
`resource_id` equals it — the team named it on purpose; (2) a name derived from what the
spec itself says (the KB's denormalized name, the tool name, the skill directory);
(3) the id itself. Then the target is asked for `<kind>:<name>`.

**Implicit defaults.** A workspace's own shared gateway and shared memory differ per
workspace by construction, and every agent that uses "the default" references them.
Requiring a hand-written mapping for that would be noise, so the source's
`resources.gateway_id` / `resources.memory_id` resolve to the target's own.
"""

import copy
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.resource_mapping import ResourceMapping

KIND_KB = "kb"
KIND_MEMORY = "memory"
KIND_GATEWAY = "gateway"
KIND_MCP_RECORD = "mcp_record"
KIND_SKILL = "skill"
KINDS = (KIND_KB, KIND_MEMORY, KIND_GATEWAY, KIND_MCP_RECORD, KIND_SKILL)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# The same shapes the AgentSpec fields enforce, so a mapping can never produce a spec the
# schema would then refuse.
_KB_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
_MEMORY_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,127}$")
_TOKEN_RE = re.compile(r"^\S{1,512}$")

# resources-map keys whose value is the workspace's own default for a kind
_IMPLICIT_RESOURCE_KEY = {KIND_GATEWAY: "gateway_id", KIND_MEMORY: "memory_id"}

Path = tuple[str | int, ...]


def key_of(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def slug(value: str) -> str:
    """A logical-name-safe form of an arbitrary label."""
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return cleaned[:64]


def validate_mapping(kind: str, name: str, resource_id: str) -> None:
    if kind not in KINDS:
        raise AppError(
            "mapping.invalid_kind", f"kind must be one of {', '.join(KINDS)}", status_code=422
        )
    if not NAME_RE.fullmatch(name):
        raise AppError(
            "mapping.invalid_name",
            "a logical name is 1-64 lowercase letters, digits, '.', '_' or '-'",
            status_code=422,
        )
    shape = {
        KIND_KB: _KB_ID_RE,
        KIND_MEMORY: _MEMORY_ID_RE,
        KIND_GATEWAY: _TOKEN_RE,
        KIND_MCP_RECORD: _TOKEN_RE,
        KIND_SKILL: re.compile(r"^s3://\S{3,500}$"),
    }[kind]
    if not shape.fullmatch(resource_id):
        raise AppError(
            "mapping.invalid_resource_id",
            f"'{resource_id}' is not a valid {kind} id"
            + (" (an s3:// prefix is expected)" if kind == KIND_SKILL else ""),
            status_code=422,
        )


# ── references ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reference:
    kind: str
    source_id: str
    path: Path
    # what the spec itself calls it, used to derive a logical name
    hint: str = ""

    @property
    def path_text(self) -> str:
        out = ""
        for part in self.path:
            out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
        return out


def extract_references(spec: dict[str, Any]) -> list[Reference]:
    """Every environment-specific id in `spec`, in a stable order."""
    refs: list[Reference] = []
    for i, kb in enumerate(spec.get("knowledge_bases") or []):
        if isinstance(kb, dict) and kb.get("kb_id"):
            refs.append(
                Reference(
                    KIND_KB, str(kb["kb_id"]), ("knowledge_bases", i, "kb_id"),
                    str(kb.get("name") or ""),
                )
            )
    memory = spec.get("memory") or {}
    if isinstance(memory, dict) and memory.get("memory_id"):
        refs.append(Reference(KIND_MEMORY, str(memory["memory_id"]), ("memory", "memory_id")))
    for i, tool in enumerate(spec.get("tools") or []):
        if not isinstance(tool, dict) or tool.get("type") != "gateway":
            continue
        config = tool.get("config") or {}
        hint = str(tool.get("name") or "")
        if config.get("gateway_id"):
            refs.append(
                Reference(
                    KIND_GATEWAY, str(config["gateway_id"]),
                    ("tools", i, "config", "gateway_id"), hint,
                )
            )
        if config.get("record_id"):
            refs.append(
                Reference(
                    KIND_MCP_RECORD, str(config["record_id"]),
                    ("tools", i, "config", "record_id"), hint,
                )
            )
    for i, skill in enumerate(spec.get("skills") or []):
        if isinstance(skill, str) and skill.startswith("s3://"):
            tail = skill.removesuffix("SKILL.md").rstrip("/").rsplit("/", 1)[-1]
            refs.append(Reference(KIND_SKILL, skill, ("skills", i), tail))
    return refs


def _set_path(spec: dict[str, Any], path: Path, value: str) -> None:
    node: Any = spec
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value


# ── mappings ─────────────────────────────────────────────────────────────────────


def mapping_out(row: ResourceMapping) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "kind": row.kind,
        "name": row.name,
        "key": key_of(row.kind, row.name),
        "resource_id": row.resource_id,
        "note": row.note,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_mappings(db: Session, workspace_id: str) -> list[ResourceMapping]:
    return list(
        db.scalars(
            select(ResourceMapping)
            .where(ResourceMapping.workspace_id == workspace_id)
            .order_by(ResourceMapping.kind, ResourceMapping.name)
        ).all()
    )


def upsert_mapping(
    db: Session,
    *,
    workspace_id: str,
    kind: str,
    name: str,
    resource_id: str,
    note: str | None,
    updated_by: str | None,
) -> ResourceMapping:
    resource_id = resource_id.strip()
    validate_mapping(kind, name, resource_id)
    row = db.scalars(
        select(ResourceMapping).where(
            ResourceMapping.workspace_id == workspace_id,
            ResourceMapping.kind == kind,
            ResourceMapping.name == name,
        )
    ).first()
    if row is None:
        row = ResourceMapping(workspace_id=workspace_id, kind=kind, name=name, resource_id="")
        db.add(row)
    row.resource_id = resource_id
    row.note = (note or "").strip() or None
    row.updated_by = updated_by
    db.flush()
    return row


def delete_mapping(db: Session, *, workspace_id: str, kind: str, name: str) -> bool:
    row = db.scalars(
        select(ResourceMapping).where(
            ResourceMapping.workspace_id == workspace_id,
            ResourceMapping.kind == kind,
            ResourceMapping.name == name,
        )
    ).first()
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


# ── resolution ───────────────────────────────────────────────────────────────────


@dataclass
class Resolution:
    spec: dict[str, Any]
    resolved: list[dict[str, Any]]
    unmapped: list[dict[str, Any]]

    @property
    def complete(self) -> bool:
        return not self.unmapped


def _logical_name(ref: Reference, source_by_id: dict[tuple[str, str], str]) -> str:
    named = source_by_id.get((ref.kind, ref.source_id))
    if named:
        return named
    derived = slug(ref.hint) or slug(ref.source_id)
    return derived or "unnamed"


def resolve_spec(
    db: Session,
    spec: dict[str, Any],
    *,
    source_workspace_id: str | None,
    target_workspace_id: str,
    source_resources: dict[str, Any] | None = None,
    target_resources: dict[str, Any] | None = None,
) -> Resolution:
    """Rewrite `spec` for the target workspace, or say what is missing.

    Never mutates `spec`. `Resolution.spec` is a deep copy with every resolvable
    reference replaced; unresolvable ones keep their SOURCE value there and are listed in
    `unmapped` (callers must not deploy a spec whose `complete` is False).
    """
    refs = extract_references(spec)
    out = copy.deepcopy(spec)
    if not refs:
        return Resolution(out, [], [])
    source_by_id: dict[tuple[str, str], str] = {}
    if source_workspace_id:
        for row in list_mappings(db, source_workspace_id):
            source_by_id[(row.kind, row.resource_id)] = row.name
    target_by_key = {
        (row.kind, row.name): row for row in list_mappings(db, target_workspace_id)
    }
    src_res, tgt_res = source_resources or {}, target_resources or {}
    resolved: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for ref in refs:
        name = _logical_name(ref, source_by_id)
        entry = {
            "kind": ref.kind,
            "logical": key_of(ref.kind, name),
            "path": ref.path_text,
            "source_id": ref.source_id,
        }
        row = target_by_key.get((ref.kind, name))
        if row is not None:
            _set_path(out, ref.path, row.resource_id)
            resolved.append({**entry, "target_id": row.resource_id, "via": "mapping"})
            continue
        implicit_key = _IMPLICIT_RESOURCE_KEY.get(ref.kind)
        if (
            implicit_key
            and src_res.get(implicit_key) == ref.source_id
            and tgt_res.get(implicit_key)
        ):
            _set_path(out, ref.path, str(tgt_res[implicit_key]))
            resolved.append(
                {**entry, "target_id": str(tgt_res[implicit_key]), "via": "workspace-default"}
            )
            continue
        unmapped.append(
            {
                **entry,
                "reason": (
                    f"workspace '{target_workspace_id}' has no mapping for "
                    f"{key_of(ref.kind, name)} (dev id {ref.source_id})"
                ),
            }
        )
    return Resolution(out, resolved, unmapped)


def resolve_gate(
    db: Session, bundle: Any, target: Any, source: Any | None
) -> dict[str, Any]:
    """The promotion gate entry: `{key, ok, detail}` plus the reference lists."""
    resolution = resolve_spec(
        db,
        bundle.spec or {},
        source_workspace_id=bundle.workspace_id,
        target_workspace_id=target.id,
        source_resources=getattr(source, "resources", None),
        target_resources=getattr(target, "resources", None),
    )
    total = len(resolution.resolved) + len(resolution.unmapped)
    if resolution.complete:
        detail = (
            f"all {total} environment-specific reference(s) map into {target.id}"
            if total
            else "no environment-specific references in the spec"
        )
    else:
        shown = ", ".join(f"{u['logical']} ({u['source_id']})" for u in resolution.unmapped[:3])
        more = len(resolution.unmapped) - 3
        detail = (
            f"{len(resolution.unmapped)} unmapped in {target.id}: {shown}"
            + (f" and {more} more" if more > 0 else "")
        )
    return {
        "key": "resource_mapping",
        "ok": resolution.complete,
        "detail": detail,
        "resolved": resolution.resolved,
        "unmapped": resolution.unmapped,
    }
