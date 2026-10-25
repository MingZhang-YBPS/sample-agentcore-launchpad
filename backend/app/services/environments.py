"""Environment comparison and drift detection (roadmap T32).

Two different questions, both answered from evidence rather than assumption:

* **compare** - for one agent *name*, what does each workspace the caller can see run?
  (ledger only; no AWS). Where a digest of the spec is needed this imports the release
  bundle's own `bundle_digest`, so a "spec digest" here is the very hash a promotion
  would carry - not a second definition of identity. The artifact is left out on purpose:
  an artifact's coordinates (`source_arn`, AWS version, image URI) are environment
  specific by nature, so digesting them would make two identical specs look different in
  every pair of workspaces. The image digest is reported beside it, not folded in.
* **drift** - for the current workspace, does AWS still match what the ledger believes?
  Reads each active agent's Runtime / Harness back through the `agentcore` wrappers.
  The rule that matters is **fail soft, never fail green**: an answer we could not read,
  or one that cannot be judged (a resource mid-update, a canary that legitimately moved
  the version), is `unknown`, never `in_sync`.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ledger import Agent, Deployment, Workspace
from app.services.agent_versions import resource_kind
from app.services.agentcore import harness as harness_api
from app.services.agentcore import runtime as runtime_api
from app.services.promotion import bundle_digest

logger = logging.getLogger(__name__)

IN_SYNC = "in_sync"
DRIFT = "drift"
UNKNOWN = "unknown"

# Order environments the way a release flows; the highest present tier is the reference
# the others are compared with.
TIER_ORDER = {"dev": 0, "staging": 1, "prod": 2}
DRIFT_AGENT_LIMIT = 100
_READ_WORKERS = 8

_HEALTHY = {"READY"}
_TRANSIENT = {"CREATING", "UPDATING"}


# ── compare ─────────────────────────────────────────────────────────────────────


def spec_digest(agent: Agent) -> str:
    """The bundle helper's digest over (name, method, spec) with no artifact."""
    return bundle_digest(
        agent_name=agent.name, method=agent.method, spec=agent.spec or {}, artifact={}
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _last_deploy(db: Session, agent: Agent) -> dict[str, Any] | None:
    deployment = db.scalars(
        select(Deployment)
        .where(Deployment.agent_id == agent.id)
        .order_by(Deployment.started_at.desc())
    ).first()
    if deployment is None:
        return None
    return {
        "status": deployment.status,
        "started_at": _iso(deployment.started_at),
        "ended_at": _iso(deployment.ended_at),
        "image_digest": deployment.image_digest,
    }


def _environment_row(db: Session, workspace: Workspace, name: str, current_id: str) -> dict:
    agent = db.scalars(
        select(Agent)
        .where(
            Agent.workspace_id == workspace.id, Agent.name == name, Agent.status != "deleted"
        )
        .order_by(Agent.created_at.desc())
    ).first()
    row: dict[str, Any] = {
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "tier": workspace.tier or "dev",
            "region": workspace.region,
            "status": workspace.bootstrap_status,
        },
        "current": workspace.id == current_id,
        "present": agent is not None,
        "agent": None,
    }
    if agent is not None:
        last = _last_deploy(db, agent)
        row["agent"] = {
            "id": agent.id,
            "status": agent.status,
            "method": agent.method,
            "version": agent.version,
            "spec_digest": spec_digest(agent),
            "image_digest": (last or {}).get("image_digest"),
            "last_deploy": last,
            "updated_at": _iso(agent.updated_at),
        }
    return row


def compare_agent(
    db: Session, *, name: str, workspaces: list[Workspace], current_id: str
) -> dict[str, Any]:
    ordered = sorted(
        workspaces, key=lambda w: (TIER_ORDER.get(w.tier or "dev", 0), w.id)
    )
    rows = [_environment_row(db, w, name, current_id) for w in ordered]
    present = [r for r in rows if r["present"]]
    reference = present[-1] if present else None  # highest tier present
    for row in rows:
        if not row["present"]:
            row["vs_reference"] = "absent"
        elif row is reference:
            row["vs_reference"] = "reference"
        else:
            same_spec = row["agent"]["spec_digest"] == reference["agent"]["spec_digest"]
            ours, theirs = row["agent"]["image_digest"], reference["agent"]["image_digest"]
            same_image = ours is None or theirs is None or ours == theirs
            row["vs_reference"] = "same" if same_spec and same_image else "differs"
    distinct_specs = {r["agent"]["spec_digest"] for r in present}
    return {
        "agent": name,
        "environments": rows,
        "reference_workspace": reference["workspace"]["id"] if reference else None,
        "summary": {
            "present": len(present),
            "distinct_spec_digests": len(distinct_specs),
            "aligned": len(distinct_specs) <= 1
            and all(r["vs_reference"] in ("same", "reference") for r in present),
        },
    }


# ── drift ───────────────────────────────────────────────────────────────────────


def _is_not_found(exc: Exception) -> bool:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") in (
            "ResourceNotFoundException",
            "NotFoundException",
        )
    return type(exc).__name__ == "ResourceNotFoundException"


def _observe(control: Any, agent: Agent) -> tuple[str | None, str | None]:
    """(status, version) as AWS reports them right now."""
    kind = resource_kind(agent)
    if kind == "harness":
        raw = harness_api.get_harness(control, agent.resource_id or "")
        return raw.get("status"), _text(raw.get("harnessVersion"))
    raw = runtime_api.get_runtime(control, agent.resource_id or "")
    return raw.get("status"), _text(raw.get("agentRuntimeVersion"))


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _canary_agent_ids(db: Session, workspace_id: str) -> set[str]:
    from app.optimization.models import RuntimeCanary

    rows = db.execute(
        select(RuntimeCanary.champion_agent_id, RuntimeCanary.challenger_agent_id).where(
            RuntimeCanary.workspace_id == workspace_id, RuntimeCanary.status == "running"
        )
    )
    return {agent_id for pair in rows for agent_id in pair}


def check_agent(control: Any, agent: Agent, *, canary: bool) -> dict[str, Any]:
    """One agent's ledger-vs-AWS verdict. Never raises."""
    result: dict[str, Any] = {
        "agent_id": agent.id,
        "name": agent.name,
        "method": agent.method,
        "state": UNKNOWN,
        "findings": [],
        "reason": None,
    }
    try:
        status, version = _observe(control, agent)
    except Exception as exc:
        if _is_not_found(exc):
            result["state"] = DRIFT
            result["findings"].append(
                {"code": "resource_missing", "expected": agent.resource_id, "observed": None}
            )
            return result
        logger.warning("drift read failed for agent %s: %s", agent.id, type(exc).__name__)
        result["reason"] = "unreadable"
        return result
    if not status:
        result["reason"] = "no_status_reported"  # an empty answer is not a healthy one
        return result
    findings: list[dict[str, Any]] = result["findings"]
    if status in _TRANSIENT:
        result["reason"] = "transitioning"
        return result
    if status not in _HEALTHY:
        findings.append({"code": "unhealthy", "expected": "READY", "observed": status})
    expected = _text(agent.version)
    if expected is None or version is None:
        if not findings:
            result["reason"] = "version_not_comparable"
            return result
    elif expected != version:
        if canary:
            # a running canary mints a candidate version on purpose; the ledger's version
            # is the champion's, so a difference cannot be called drift
            if not findings:
                result["reason"] = "canary_active"
                return result
        else:
            findings.append({"code": "version_changed", "expected": expected, "observed": version})
    result["state"] = DRIFT if findings else IN_SYNC
    return result


def detect_drift(
    db: Session,
    *,
    workspace_id: str,
    control: Any,
    agent_name: str | None = None,
    limit: int = DRIFT_AGENT_LIMIT,
    reader: Callable[[Any, Agent], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    query = select(Agent).where(
        Agent.workspace_id == workspace_id, Agent.status == "active", Agent.resource_id.is_not(None)
    )
    if agent_name:
        query = query.where(Agent.name == agent_name)
    agents = [a for a in db.scalars(query.order_by(Agent.name)) if resource_kind(a)]
    truncated = len(agents) > limit
    agents = agents[:limit]
    canaries = _canary_agent_ids(db, workspace_id)
    # Detach what the workers read; the session is not shared across threads.
    check = reader or (lambda c, a: check_agent(c, a, canary=a.id in canaries))
    if agents:
        db.expunge_all()
        with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
            results = list(pool.map(lambda a: check(control, a), agents))
    else:
        results = []
    counts = {IN_SYNC: 0, DRIFT: 0, UNKNOWN: 0}
    for item in results:
        counts[item["state"]] += 1
    overall = DRIFT if counts[DRIFT] else UNKNOWN if counts[UNKNOWN] else IN_SYNC
    return {
        "workspace_id": workspace_id,
        "state": overall,
        "checked": len(results),
        "counts": counts,
        "truncated": truncated,
        "agents": results,
    }
