"""Ledger spec snapshots per publish, and their human-readable diff (T18).

A snapshot is written by `deployer.pipeline.create_deployment` — the one place every
create / redeploy / rollback / promotion passes through — so no publish path can
forget it. The AWS version string is only known once the deploy stage ran, so
`_finish` fills it in through `stamp_version`. Rollback never touches AWS-side
versions: it re-publishes a stored spec as an ordinary new snapshot.
"""

import copy
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.ledger import Agent, SpecSnapshot

# Top-level spec field -> the group the console labels it with.
_GROUPS = {
    "system_prompt": "prompt",
    "model_id": "model",
    "model_source": "model",
    "max_tokens": "model",
    "reasoning_effort": "model",
    "tools": "tools",
    "toolkits": "tools",
    "native_tools": "tools",
    "allowed_tools": "tools",
    "tool_description_overrides": "tools",
    "skills": "skills",
    "knowledge_bases": "knowledge_bases",
    "memory": "memory",
    "guardrail": "guardrail",
}
# Fields whose recursion would produce noise (whole documents / large blobs).
_ATOMIC = {"code", "code_bundle", "studio_flow", "system_prompt"}


def record_snapshot(
    db: Session,
    agent: Agent,
    *,
    deployment_id: str | None = None,
    created_by: str | None = None,
    note: str | None = None,
) -> SpecSnapshot:
    last = (
        db.query(func.max(SpecSnapshot.seq)).filter(SpecSnapshot.agent_id == agent.id).scalar()
    )
    snap = SpecSnapshot(
        workspace_id=agent.workspace_id,
        agent_id=agent.id,
        seq=(last or 0) + 1,
        spec=copy.deepcopy(agent.spec or {}),
        deployment_id=deployment_id,
        created_by=created_by or agent.created_by_name or agent.owner,
        note=note,
    )
    db.add(snap)
    db.flush()
    return snap


def stamp_version(db: Session, deployment_id: str, version: str | None) -> None:
    """Record the AWS version string on the snapshot its deployment published."""
    if not version:
        return
    snap = db.query(SpecSnapshot).filter(SpecSnapshot.deployment_id == deployment_id).first()
    if snap is not None:
        snap.aws_version = str(version)


def snapshot_out(snap: SpecSnapshot, *, with_spec: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "seq": snap.seq,
        "agent_id": snap.agent_id,
        "aws_version": snap.aws_version,
        "deployment_id": snap.deployment_id,
        "created_by": snap.created_by,
        "note": snap.note,
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
    }
    if with_spec:
        out["spec"] = snap.spec
    return out


def list_snapshots(db: Session, agent_id: str) -> list[SpecSnapshot]:
    return (
        db.query(SpecSnapshot)
        .filter(SpecSnapshot.agent_id == agent_id)
        .order_by(SpecSnapshot.seq.desc())
        .all()
    )


def get_snapshot(db: Session, agent_id: str, seq: int) -> SpecSnapshot:
    snap = (
        db.query(SpecSnapshot)
        .filter(SpecSnapshot.agent_id == agent_id, SpecSnapshot.seq == seq)
        .first()
    )
    if snap is None:
        raise NotFoundError("snapshot.not_found", "snapshot not found")
    return snap


def _kind(before: Any, after: Any, missing: object) -> str:
    if before is missing:
        return "added"
    if after is missing:
        return "removed"
    return "changed"


def _walk(path: str, before: Any, after: Any, out: list[dict[str, Any]], missing: object) -> None:
    if before == after:
        return
    top = path.split(".", 1)[0]
    if (
        isinstance(before, dict)
        and isinstance(after, dict)
        and top not in _ATOMIC
    ):
        for key in sorted(set(before) | set(after)):
            _walk(f"{path}.{key}", before.get(key, missing), after.get(key, missing), out, missing)
        return
    row: dict[str, Any] = {
        "field": path,
        "group": _GROUPS.get(top, "other"),
        "kind": _kind(before, after, missing),
        "before": None if before is missing else before,
        "after": None if after is missing else after,
    }
    if isinstance(before, list) and isinstance(after, list):
        # Order rarely matters for tools/skills/KBs: report membership changes, and keep
        # before/after for the pure re-order case.
        row["added"] = [x for x in after if x not in before]
        row["removed"] = [x for x in before if x not in after]
    out.append(row)


def diff_specs(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level differences, dotted paths (`memory.long_term`), stable order.

    Lists (tools, skills, knowledge bases…) carry `added` / `removed`; long text
    (`system_prompt`, `code`) is compared whole. Unchanged fields are omitted.
    """
    missing = object()
    out: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        _walk(key, before.get(key, missing), after.get(key, missing), out, missing)
    order = ["prompt", "model", "tools", "skills", "knowledge_bases", "memory", "guardrail"]
    out.sort(key=lambda r: (order.index(r["group"]) if r["group"] in order else len(order)))
    return out
