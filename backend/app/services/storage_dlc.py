"""Storage mounts in the Agent-DLC (design storage-mounts §8, S4).

Three consumers, one reading of a spec's mounts:

* **Release gate evidence** (§8.1, `dlc/releases`): the candidate version's mounts
  against the live version's — added, removed, `ro → rw`, another storage at the same
  path, a `data_class` that changed since live was signed. A new `rw` mount, `ro → rw`,
  and a change of storage or `data_class` under a mount are marked "sign-off needed":
  the signer must tick the acknowledgement, `releases.sign` refuses otherwise.
* **Evaluation write isolation** (§8.2): a version is evaluated with the mounts it
  will ship with, so a `rw` mount of `shared_state` storage is written by the
  regression replay. In a `gated` workspace the gate evaluation, an evaluation replay
  and a canary of such an agent need the runner's explicit confirmation (D-3: allowed
  once confirmed, and recorded); swapping the disk for the evaluation is deliberately
  not offered — it would evaluate another version than the one that ships.
* **Architect proposals** (§8.3, `assistant/proposal`) read the same catalog facts.

A mount is keyed by its path: the same path carrying another storage is a change of
storage, not a removal plus an addition. Inline (legacy) mounts are read-write, as
they have always been, and carry no `data_class`.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.services.storage_mounts import spec_mounts

_INLINE = (("s3_files", "s3files"), ("efs", "efs"))


def mounts_of(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """`[{storage_id, mount_path, mode, kind, access_point_arn}]` of a stored spec dict,
    catalog references first (best effort, no validation)."""
    out: list[dict[str, Any]] = []
    for mount in spec_mounts(spec):
        sid = str(mount["storage_id"])
        out.append({
            "storage_id": sid,
            "mount_path": str(mount.get("mount_path") or f"/mnt/{sid}").rstrip("/"),
            "mode": mount.get("mode") or "ro",
            "kind": mount.get("kind"),
            "access_point_arn": mount.get("access_point_arn"),
        })
    fs = (spec or {}).get("filesystem") or {}
    for key, kind in _INLINE:
        for mount in fs.get(key) or []:
            path = str((mount or {}).get("mount_path") or "").rstrip("/")
            if path:
                out.append({"storage_id": None, "mount_path": path, "mode": "rw",
                            "kind": kind, "access_point_arn": mount.get("access_point_arn")})
    return out


def with_catalog(db: Session, workspace_id: str,
                 mounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each mount plus its catalog entry's `name` and `data_class` (an archived entry
    still answers: a version may name storage archived since). Unknown → None."""
    from app.services.storage_mounts import _row

    out = []
    for mount in mounts:
        row = None
        if mount.get("storage_id"):
            row = _row(db, workspace_id, mount["storage_id"], archived=True)
        out.append({**mount, "name": row.name if row else None,
                    "data_class": row.data_class if row else None})
    return out


def _identity(mount: dict[str, Any]) -> str:
    return mount.get("storage_id") or f"arn:{mount.get('access_point_arn') or ''}"


def diff(live: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The mount changes from `live` to `candidate`, one per path whose mount differs,
    each saying whether the signer must acknowledge it."""
    before = {m["mount_path"]: m for m in live}
    after = {m["mount_path"]: m for m in candidate}
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old is None:
            change = {"change": "added", "requires_ack": new["mode"] == "rw"}
        elif new is None:
            change = {"change": "removed", "requires_ack": False}
        elif _identity(old) != _identity(new):
            change = {"change": "storage", "requires_ack": new["mode"] == "rw"
                      or old.get("data_class") != new.get("data_class")}
        elif old["mode"] != new["mode"]:
            change = {"change": "mode", "requires_ack": new["mode"] == "rw"}
        elif (old.get("data_class") and new.get("data_class")
              and old["data_class"] != new["data_class"]):
            change = {"change": "data_class", "requires_ack": True}
        else:
            continue
        current = new or old
        change.update({
            "mount_path": path,
            "storage_id": current.get("storage_id"),
            "name": current.get("name"),
            "mode": new["mode"] if new else None,
            "data_class": new.get("data_class") if new else None,
            "from_storage_id": old.get("storage_id") if old else None,
            "from_mode": old["mode"] if old else None,
            "from_data_class": old.get("data_class") if old else None,
        })
        changes.append(change)
    return changes


def evidence(db: Session, workspace_id: str, candidate_spec: dict[str, Any] | None,
             live_mounts: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The gate's storage evidence. `live_mounts` None = the live version's spec is not
    known (no snapshot): every candidate mount then reads as added, so nothing
    read-write slips through unacknowledged."""
    candidate = with_catalog(db, workspace_id, mounts_of(candidate_spec))
    changes = diff(live_mounts or [], candidate)
    return {
        "candidate": candidate,
        "live": live_mounts,
        "live_known": live_mounts is not None,
        "changes": changes,
        "requires_ack": any(c["requires_ack"] for c in changes),
        "shared_state_writes": [m for m in candidate
                                if m["mode"] == "rw" and m.get("data_class") == "shared_state"],
        "computed_at": datetime.now(UTC).isoformat(),
    }


# ── evaluation write isolation (§8.2) ─────────────────────────────────────────


def shared_state_writes(db: Session, workspace_id: str,
                        spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The spec's read-write mounts of `shared_state` storage."""
    return [m for m in with_catalog(db, workspace_id, mounts_of(spec))
            if m["mode"] == "rw" and m.get("data_class") == "shared_state"]


def gated(db: Session, workspace_id: str) -> bool:
    from app.services.storage_mounts import _gated

    return _gated(db, workspace_id)


def require_write_confirmation(db: Session, workspace_id: str, spec: dict[str, Any] | None,
                               *, confirmed: bool, action: str) -> list[dict[str, Any]]:
    """The shared-state writes a run of `spec` would make in a gated workspace; 409
    `storage.write_confirm_required` (with the mounts) unless the runner confirmed.
    `[]` outside gated workspaces and for specs without such a mount."""
    if not gated(db, workspace_id):
        return []
    writes = shared_state_writes(db, workspace_id, spec)
    if writes and not confirmed:
        names = ", ".join(m.get("name") or m["storage_id"] for m in writes)
        raise AppError(
            "storage.write_confirm_required",
            f"this run writes into production shared storage ({names}) — confirm, or "
            "evaluate in a DEV workspace that registers the same storage on staging data",
            {"action": action, "storage": [
                {"storage_id": m["storage_id"], "name": m.get("name"),
                 "mount_path": m["mount_path"]} for m in writes]},
            status_code=409,
        )
    return writes


def confirmation(writes: list[dict[str, Any]], actor: str | None) -> dict[str, Any]:
    """What a confirmed shared-state write records (release attachment, audit target)."""
    return {"confirmed_by": actor or "", "confirmed_at": datetime.now(UTC).isoformat(),
            "storage": sorted({m["storage_id"] for m in writes if m.get("storage_id")})}


def confirm_run_writes(workspace_id: str, spec: dict[str, Any] | None, *, confirmed: bool,
                       action: str, target: str, actor: str | None) -> list[dict[str, Any]]:
    """`require_write_confirmation` on its own session, for callers outside a request
    transaction (an evaluation replay, a canary); a confirmed write is journaled
    (`storage.write_confirmed`, target `<target>:<storage ids>`)."""
    from app.core.db import SessionLocal
    from app.services.audit import record_audit_event

    db = SessionLocal()
    try:
        writes = require_write_confirmation(db, workspace_id, spec, confirmed=confirmed,
                                            action=action)
    finally:
        db.close()
    if writes:
        record_audit_event(
            workspace_id=workspace_id, actor=actor or "", action="storage.write_confirmed",
            target=f"{target}:{','.join(m['storage_id'] for m in writes)}",
        )
    return writes
