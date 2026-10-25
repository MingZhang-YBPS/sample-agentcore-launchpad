"""Who may write inline S3 Files / EFS mounts into a spec (design storage-mounts D-2).

Until the storage catalog (§4, S2) exists, a BYO mount is an inline access point
ARN in ``spec.filesystem.s3_files`` / ``efs``. An access point names a file system
the agent's role will be granted ``ClientMount`` + ``ClientWrite`` on, so — like
inline subnets / security groups (`network_profiles.normalize_spec_network`) —
inline mounts are accepted from administrators only. A member may still re-publish
an agent whose administrator-set mounts stay as they are (same kind, ARN and path),
or remove mounts; adding or changing one is refused with
``storage.inline_not_allowed``.
"""

from typing import Any

from app.core.errors import AppError
from app.schemas.agent import AgentSpec


def _mounts(spec: AgentSpec | dict[str, Any] | None) -> set[tuple[str, str, str]]:
    """``{(kind, access_point_arn, mount_path)}`` of a spec (or stored spec dict)."""
    if spec is None:
        return set()
    if isinstance(spec, AgentSpec):
        return {(kind, m.access_point_arn, m.mount_path.rstrip("/"))
                for kind, m in spec.filesystem.byo_mounts()}
    fs = spec.get("filesystem") or {}
    out: set[tuple[str, str, str]] = set()
    for key, kind in (("s3_files", "s3files"), ("efs", "efs")):
        for m in fs.get(key) or []:
            m = m or {}
            out.add((kind, str(m.get("access_point_arn") or ""),
                     str(m.get("mount_path") or "").rstrip("/")))
    return out


def new_inline_mounts(
    spec: AgentSpec, previous: AgentSpec | dict[str, Any] | None = None,
) -> list[str]:
    """The mount paths ``spec`` adds or changes relative to ``previous``."""
    before = _mounts(previous)
    return sorted({path for (_kind, _arn, path) in _mounts(spec) - before})


def assert_inline_mounts_allowed(
    spec: AgentSpec, previous: AgentSpec | dict[str, Any] | None = None, *,
    is_admin: bool,
) -> None:
    """Refuse a non-administrator's new or changed inline mount (400)."""
    if is_admin:
        return
    added = new_inline_mounts(spec, previous)
    if added:
        raise AppError(
            "storage.inline_not_allowed",
            "S3 Files / EFS mounts are set by administrators — ask one to add "
            f"{', '.join(added)}, or remove the mount",
            {"mount_paths": added},
            status_code=400,
        )
