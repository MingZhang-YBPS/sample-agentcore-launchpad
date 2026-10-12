"""Release bundles and promotion requests (roadmap T20/T21).

The hand-off this closes: a developer verifies an agent in a dev or staging workspace
and today has no way to give it to whoever runs production except by describing it.
`prod` refuses member mutations (T05), so the only honest path in is a *promotion*.

Two objects, both append-only:

* a **release bundle** freezes one publish — the spec, the artifact coordinates that
  publish actually used, the evaluation evidence pinned to it, the policy posture — and
  reduces them to a `digest`. The digest is the point: promotion ships the bundle, so
  "the thing that goes live is the thing that was tested" becomes checkable rather than
  asserted. Re-bundling the same publish yields the same digest.
* a **promotion** is one request to release a bundle into a target workspace, carrying
  the change note and the rollback plan, and recording who approved it and which gates
  passed *at that moment* (evidence, not a live read).

Execution — dependency mapping, artifact copy, gates, canary ramp — is P3 (T23–T27).
This module deliberately stops at an approved request: approving something that cannot
yet be executed is still useful (it is the audit trail), and a half-built executor that
deploys into production would not be.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import (
    Agent,
    Deployment,
    Promotion,
    ReleaseBundle,
    SpecSnapshot,
    Workspace,
)

# A promotion's lifecycle. Only `pending` is mutable by review; execution states are
# written by the executor (P3).
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
# Closed by the system, not by a reviewer: the target workspace was removed, so the
# request can never be reviewed or executed (review and execute 404 on a missing target).
STATUS_CANCELLED = "cancelled"
OPEN_STATUSES = (STATUS_PENDING,)
TERMINAL_REVIEW = (STATUS_APPROVED, STATUS_REJECTED)

# Which specs can be promoted at all. A converted agent bakes its prompt and model into
# exported code, and a system preset is server-owned — both are refused for the same
# reason the redeploy route refuses them.
SYSTEM_MANAGED_MESSAGE = "a system-managed preset is not promotable"


def _canonical(value: Any) -> str:
    """Stable JSON for digesting: sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def bundle_digest(*, agent_name: str, method: str, spec: dict, artifact: dict) -> str:
    """The bundle's identity.

    Covers what determines the running agent: its name, method, spec and the artifact
    coordinates. Deliberately NOT the evaluation or policy blobs — evidence about a
    bundle must not change what the bundle *is*, or re-running evaluation would mint a
    different digest for identical code.
    """
    return hashlib.sha256(
        _canonical(
            {"agent_name": agent_name, "method": method, "spec": spec, "artifact": artifact}
        ).encode()
    ).hexdigest()


def _artifact_of(db: Session, agent: Agent, snapshot: SpecSnapshot | None) -> dict[str, Any]:
    """The coordinates the publish landed on, read back from its Deployment row.

    An image digest or a staged upload id is what makes a promotion a *copy* rather than
    a rebuild; when the snapshot predates deployment bookkeeping the bundle still forms,
    with the fields it could establish, and the gate that cares (P3) can refuse it.
    """
    deployment: Deployment | None = None
    if snapshot is not None and snapshot.deployment_id:
        deployment = db.get(Deployment, snapshot.deployment_id)
    if deployment is None:
        deployment = db.scalars(
            select(Deployment)
            .where(Deployment.agent_id == agent.id, Deployment.status == "succeeded")
            .order_by(Deployment.started_at.desc())
        ).first()
    spec = agent.spec or {}
    artifact: dict[str, Any] = {
        "aws_version": snapshot.aws_version if snapshot else agent.version,
        "source_arn": agent.arn,
    }
    if deployment is not None and deployment.image_digest:
        artifact["image_digest"] = deployment.image_digest
    byoc = spec.get("byoc") or {}
    for key in ("upload_id", "image_uri", "artifact_kind"):
        if byoc.get(key):
            artifact[key] = byoc[key]
    return artifact


def _evaluation_of(db: Session, agent: Agent) -> dict[str, Any]:
    """The most recent completed evaluation run for this agent, as evidence.

    Imported inside the function: the evaluation models register their own tables, and a
    module-scope import would make the promotion surface a dependency of theirs.
    """
    from app.evaluation.models import EvalRun
    run = db.scalars(
        select(EvalRun)
        # the evaluation service ends a run as `completed`; `succeeded` is kept so a row
        # written by an older build (or a test) still counts
        .where(
            EvalRun.agent_id == agent.id,
            EvalRun.status.in_(("completed", "succeeded")),
            EvalRun.mode == "evaluators",
        )
        .order_by(EvalRun.created_at.desc())
    ).first()
    if run is None:
        return {}
    return {
        "run_id": run.id,
        "dataset_id": run.dataset_id,
        "dataset_version": run.dataset_version,
        "scores": run.scores or [],
        "finished_at": (run.updated_at or run.created_at).isoformat()
        if (run.updated_at or run.created_at)
        else None,
    }


def _policy_of(agent: Agent) -> dict[str, Any]:
    spec = agent.spec or {}
    return {
        "guardrail": spec.get("guardrail") or {"enabled": False},
        "gateways": [ref.get("id") for ref in (spec.get("tools") or []) if isinstance(ref, dict)],
        "registry_record_id": agent.registry_record_id,
    }


def build_bundle(
    db: Session,
    agent: Agent,
    *,
    workspace_id: str,
    created_by: str | None,
    note: str | None = None,
    snapshot_seq: int | None = None,
) -> ReleaseBundle:
    """Freeze a publish into a bundle. Idempotent per digest.

    Re-bundling an unchanged publish returns the existing row rather than a second
    identical one, so a member clicking twice does not fork the audit trail.
    """
    if agent.system_key:
        raise AppError("promotion.system_managed", SYSTEM_MANAGED_MESSAGE, status_code=409)
    if agent.status != "active":
        raise AppError(
            "promotion.agent_not_active",
            f"only an active agent can be bundled (this one is {agent.status})",
            status_code=409,
        )
    query = select(SpecSnapshot).where(SpecSnapshot.agent_id == agent.id)
    if snapshot_seq is not None:
        query = query.where(SpecSnapshot.seq == snapshot_seq)
    snapshot = db.scalars(query.order_by(SpecSnapshot.seq.desc())).first()
    if snapshot_seq is not None and snapshot is None:
        raise AppError(
            "promotion.snapshot_not_found",
            f"agent has no publish #{snapshot_seq}",
            status_code=404,
        )
    # An agent published before snapshots existed still bundles, from its live spec.
    spec = dict(snapshot.spec if snapshot else (agent.spec or {}))
    artifact = _artifact_of(db, agent, snapshot)
    digest = bundle_digest(
        agent_name=agent.name, method=agent.method, spec=spec, artifact=artifact
    )
    existing = db.scalars(
        select(ReleaseBundle).where(
            ReleaseBundle.agent_id == agent.id, ReleaseBundle.digest == digest
        )
    ).first()
    if existing is not None:
        return existing
    bundle = ReleaseBundle(
        workspace_id=workspace_id,
        agent_id=agent.id,
        snapshot_id=snapshot.id if snapshot else None,
        snapshot_seq=snapshot.seq if snapshot else None,
        agent_name=agent.name,
        method=agent.method,
        spec=spec,
        artifact=artifact,
        evaluation=_evaluation_of(db, agent),
        policy=_policy_of(agent),
        digest=digest,
        created_by=created_by,
        note=note,
    )
    db.add(bundle)
    db.flush()
    return bundle


def bundle_out(bundle: ReleaseBundle) -> dict[str, Any]:
    return {
        "id": bundle.id,
        "agent_id": bundle.agent_id,
        "agent_name": bundle.agent_name,
        "display_name": (bundle.spec or {}).get("display_name"),
        "method": bundle.method,
        "snapshot_seq": bundle.snapshot_seq,
        "digest": bundle.digest,
        "artifact": bundle.artifact or {},
        "evaluation": bundle.evaluation or {},
        "policy": bundle.policy or {},
        "workspace_id": bundle.workspace_id,
        "created_by": bundle.created_by,
        "note": bundle.note,
        "created_at": bundle.created_at.isoformat() if bundle.created_at else None,
    }


# ── gates ────────────────────────────────────────────────────────────────────────


def evaluate_gates(db: Session, bundle: ReleaseBundle, target: Workspace) -> dict[str, Any]:
    """The checks an approver sees, each with a verdict and a reason.

    Advisory at this stage: an approver may accept a bundle with a failing gate, and the
    record says they did. Gates become blocking when execution exists (T26), which is
    why every entry already carries the shape the executor will read.
    """
    checks: list[dict[str, Any]] = []

    evaluation = bundle.evaluation or {}
    checks.append(
        {
            "key": "evaluation",
            "ok": bool(evaluation.get("run_id")),
            "detail": (
                f"pinned run {evaluation['run_id']}"
                if evaluation.get("run_id")
                else "no completed evaluation run for this agent"
            ),
        }
    )

    artifact = bundle.artifact or {}
    reusable = any(
        artifact.get(key) for key in ("image_digest", "upload_id", "image_uri")
    )
    checks.append(
        {
            "key": "artifact",
            # a harness has no artifact by design — build-once does not apply to it
            "ok": reusable or bundle.method == "harness",
            "detail": (
                "harness — no build artifact"
                if bundle.method == "harness" and not reusable
                else "artifact coordinates recorded"
                if reusable
                else "no image digest or staged upload: the target would have to rebuild"
            ),
        }
    )

    guardrail = (bundle.policy or {}).get("guardrail") or {}
    checks.append(
        {
            "key": "guardrail",
            "ok": True,  # advisory: not every agent handles personal data
            "detail": (
                f"PII protection {guardrail.get('mode')}"
                if guardrail.get("enabled")
                else "PII protection off"
            ),
        }
    )

    checks.append(
        {
            "key": "target_ready",
            "ok": target.bootstrap_status == "ready",
            "detail": f"target workspace is {target.bootstrap_status}",
        }
    )

    name_clash = db.scalars(
        select(Agent).where(
            Agent.workspace_id == target.id,
            Agent.name == bundle.agent_name,
            Agent.status != "deleted",
        )
    ).first()
    checks.append(
        {
            "key": "target_agent",
            "ok": True,
            "detail": (
                f"replaces the existing '{bundle.agent_name}' ({name_clash.status})"
                if name_clash
                else f"creates '{bundle.agent_name}' in {target.id}"
            ),
        }
    )
    # T23: every environment-specific id in the spec must map into the target. The entry
    # carries `resolved` / `unmapped` so the console can say exactly what is missing.
    from app.services import resource_mapping

    source = db.get(Workspace, bundle.workspace_id) if bundle.workspace_id else None
    checks.append(resource_mapping.resolve_gate(db, bundle, target, source))
    return {
        "checks": checks,
        "blocking_failures": [c["key"] for c in checks if not c["ok"]],
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


# ── promotions ───────────────────────────────────────────────────────────────────


def request_promotion(
    db: Session,
    *,
    bundle: ReleaseBundle,
    source_workspace_id: str,
    target: Workspace,
    requested_by: str | None,
    change_note: str,
    rollback_note: str,
) -> Promotion:
    if target.id == source_workspace_id:
        raise AppError(
            "promotion.same_workspace",
            "a promotion must target a different workspace",
            status_code=400,
        )
    open_request = db.scalars(
        select(Promotion).where(
            Promotion.bundle_id == bundle.id,
            Promotion.target_workspace_id == target.id,
            Promotion.status.in_(OPEN_STATUSES),
        )
    ).first()
    if open_request is not None:
        raise AppError(
            "promotion.already_open",
            f"this bundle already has a pending promotion into {target.id}",
            {"promotion_id": open_request.id},
            status_code=409,
        )
    promotion = Promotion(
        workspace_id=source_workspace_id,
        bundle_id=bundle.id,
        target_workspace_id=target.id,
        status=STATUS_PENDING,
        requested_by=requested_by,
        change_note=change_note.strip(),
        rollback_note=rollback_note.strip(),
        gates=evaluate_gates(db, bundle, target),
    )
    db.add(promotion)
    db.flush()
    return promotion


def review_promotion(
    db: Session,
    promotion: Promotion,
    *,
    approve: bool,
    reviewer: str | None,
    note: str | None,
    target: Workspace,
    bundle: ReleaseBundle,
) -> Promotion:
    """Approve or reject a pending request.

    Refuses a self-approval: the whole point of the hand-off is that a second person
    accepts the release. An administrator is not exempt — they can grant themselves the
    permission, but not the second pair of eyes.
    """
    if promotion.status != STATUS_PENDING:
        raise AppError(
            "promotion.not_pending",
            f"this promotion is already {promotion.status}",
            status_code=409,
        )
    if approve and reviewer and promotion.requested_by == reviewer:
        raise AppError(
            "promotion.self_approval",
            "a promotion must be approved by someone other than the requester",
            status_code=409,
        )
    promotion.gates = evaluate_gates(db, bundle, target)
    promotion.status = STATUS_APPROVED if approve else STATUS_REJECTED
    promotion.reviewed_by = reviewer
    promotion.review_note = (note or "").strip() or None
    promotion.reviewed_at = datetime.now(UTC)
    db.flush()
    return promotion


# Spec fields whose VALUES never appear in a diff, only the fact that they changed:
# `env` is where credentials end up, and inline code / BYOC coordinates are code. A
# reviewer needs to know they differ, not to read them in a review panel — and a diff is
# exactly the kind of response that gets screenshotted, logged and pasted into tickets.
REDACTED_DIFF_FIELDS = frozenset({"env", "code", "code_bundle", "byoc"})
REDACTED = "‹hidden›"


def cancel_for_removed_target(db: Session, target_workspace_id: str) -> int:
    """Close every not-yet-executed request aimed at a workspace that is going away.

    Called by the workspace detach and purge paths in the same transaction as the
    removal. Without it, a pending request into a removed target stayed in the inbox
    forever — nobody can review it (its target answers 404) — which the e2e suite found
    as four orphaned rows after a few runs. Executing or finished promotions are history
    and are left untouched.
    """
    rows = db.scalars(
        select(Promotion).where(
            Promotion.target_workspace_id == target_workspace_id,
            Promotion.status.in_((STATUS_PENDING, STATUS_APPROVED)),
        )
    ).all()
    for row in rows:
        row.status = STATUS_CANCELLED
        row.error = f"target workspace '{target_workspace_id}' was removed"
    return len(rows)


def spec_diff(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level differences between two specs, in a shape the console renders.

    Shallow on purpose: a member reviewing a release wants "the prompt changed, the
    model changed, one tool was added", not a recursive JSON delta. Values are stringified
    and long ones truncated, since this feeds a review panel rather than a patch.
    `REDACTED_DIFF_FIELDS` report the change with both values hidden.
    """

    def show(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, str):
            return value if len(value) <= 600 else f"{value[:600]}…"
        return _canonical(value)[:600]

    def shown(key: str, value: Any) -> str:
        if key in REDACTED_DIFF_FIELDS and value not in (None, {}, [], ""):
            return REDACTED
        return show(value)

    keys = sorted(set(before or {}) | set(after or {}))
    rows = []
    for key in keys:
        old, new = (before or {}).get(key), (after or {}).get(key)
        if old == new:
            continue
        rows.append(
            {
                "field": key,
                "before": shown(key, old),
                "after": shown(key, new),
                "redacted": key in REDACTED_DIFF_FIELDS,
                "kind": "added" if key not in (before or {})
                else "removed" if key not in (after or {})
                else "changed",
            }
        )
    return rows


def promotion_out(
    promotion: Promotion, bundle: ReleaseBundle | None = None
) -> dict[str, Any]:
    return {
        "id": promotion.id,
        "bundle_id": promotion.bundle_id,
        "bundle": bundle_out(bundle) if bundle is not None else None,
        "source_workspace_id": promotion.workspace_id,
        "target_workspace_id": promotion.target_workspace_id,
        "status": promotion.status,
        "requested_by": promotion.requested_by,
        "change_note": promotion.change_note,
        "rollback_note": promotion.rollback_note,
        "reviewed_by": promotion.reviewed_by,
        "review_note": promotion.review_note,
        "reviewed_at": promotion.reviewed_at.isoformat() if promotion.reviewed_at else None,
        "gates": promotion.gates or {},
        "job_id": promotion.job_id,
        "error": promotion.error,
        # T27: execution progress (one entry per stage), what a rollback would restore,
        # and which stage a failed run stopped at
        "stages": promotion.stages or [],
        "previous_bundle_id": promotion.previous_bundle_id,
        "started_at": promotion.started_at.isoformat() if promotion.started_at else None,
        "finished_at": promotion.finished_at.isoformat() if promotion.finished_at else None,
        "failed_stage": next(
            (s["name"] for s in (promotion.stages or []) if s.get("status") == "failed"), None
        ),
        "action": (promotion.execution or {}).get("action"),
        "created_at": promotion.created_at.isoformat() if promotion.created_at else None,
        "updated_at": promotion.updated_at.isoformat() if promotion.updated_at else None,
    }
