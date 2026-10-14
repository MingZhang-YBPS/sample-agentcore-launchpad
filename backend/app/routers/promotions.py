"""Release bundles, promotion requests and the administrator inbox (T20–T22).

The dev → ops hand-off. A member bundles a verified publish and asks for a release; an
operator (or an administrator) approves or rejects it. Execution into the target
environment is P3 — an approved promotion is the durable, auditable record that the
release was accepted, and the inbox is where the work waiting on a human shows up.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.models.ledger import (
    Agent,
    Job,
    Promotion,
    ReleaseBundle,
    User,
    Workspace,
)
from app.routers.auth import Identity, current_identity
from app.routers.workspaces import WorkspaceScope, authorized_workspace, require_workspace
from app.services import agent_access, promotion_exec, release_gates
from app.services import alerts as alert_service
from app.services import promotion as promotion_service
from app.services.audit import record_audit_event

router = APIRouter(
    prefix="/api",
    tags=["promotions"],
    # workspace isolation: a hidden `{agent_id}` / `{run_id}` reads as missing
    dependencies=[Depends(agent_access.require_path_ids_visible)],
)


class BundleRequest(BaseModel):
    """Which publish to freeze. Omitting `snapshot_seq` takes the latest."""

    snapshot_seq: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=2000)


class PromotionRequest(BaseModel):
    bundle_id: str = Field(min_length=1, max_length=32)
    target_workspace_id: str = Field(min_length=1, max_length=32)
    # A release nobody described cannot be reviewed, and one without a way back should
    # not be approved — both are required rather than optional-with-a-default.
    change_note: str = Field(min_length=1, max_length=4000)
    rollback_note: str = Field(min_length=1, max_length=4000)


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=4000)


def _agent(db: Session, agent_id: str, workspace_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.status == "deleted" or agent.workspace_id != workspace_id:
        raise AppError("agent.not_found", "agent not found", status_code=404)
    return agent


def _bundle(db: Session, bundle_id: str, workspace_id: str) -> ReleaseBundle:
    bundle = db.get(ReleaseBundle, bundle_id)
    if bundle is None or bundle.workspace_id != workspace_id:
        raise AppError("promotion.bundle_not_found", "release bundle not found", status_code=404)
    return bundle


def _granted_on(db: Session, identity: Identity, workspace_id: str, request: Request) -> bool:
    """Whether the caller may see the target's data (used to trim a read, not to refuse)."""
    try:
        _target(db, workspace_id, identity, request)
    except AppError:
        return False
    return True


def _for_reader(payload: dict[str, Any], visible: bool) -> dict[str, Any]:
    """Trim what a source-workspace reader without a target grant may not see.

    The gates were computed by someone granted on the target, and the resource-mapping
    gate lists target resource ids (`resolved` / `unmapped`). The verdicts stay — whether
    a release is blocked is the requester's business — but the target's ids do not.
    """
    payload["target_visible"] = visible
    if visible:
        return payload
    gates = dict(payload.get("gates") or {})
    gates["checks"] = [
        {key: value for key, value in check.items() if key not in ("resolved", "unmapped")}
        for check in gates.get("checks") or []
    ]
    payload["gates"] = gates
    return payload


def _target(db: Session, workspace_id: str, identity: Identity, request: Request) -> Workspace:
    """The target workspace — existing, and granted to the caller (admins by role).

    The route policy authorizes only the SOURCE workspace (`X-Workspace`); every route
    here that reads or acts on the target must hold the caller to that one too — and,
    inside a project, the target must be bound to that project and reached through it
    (design §5.6; 404 `project.not_found` otherwise).
    """
    target = authorized_workspace(db, identity, workspace_id)
    promotion_service.assert_project_reaches(
        db, identity, workspace_id, getattr(request.state, "project", None), request.method
    )
    return target


# ── release bundles ──────────────────────────────────────────────────────────────


@router.post("/agents/{agent_id}/release-bundles", status_code=201)
def create_bundle(
    agent_id: str,
    req: BundleRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Freeze this agent's publish into an immutable, digest-identified bundle."""
    agent = _agent(db, agent_id, ws.id)
    bundle = promotion_service.build_bundle(
        db,
        agent,
        workspace_id=ws.id,
        created_by=identity.username,
        note=req.note,
        snapshot_seq=req.snapshot_seq,
    )
    db.commit()
    return promotion_service.bundle_out(bundle)


@router.get("/agents/{agent_id}/release-bundles")
def list_agent_bundles(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent(db, agent_id, ws.id)
    rows = db.scalars(
        select(ReleaseBundle)
        .where(ReleaseBundle.agent_id == agent_id, ReleaseBundle.workspace_id == ws.id)
        .order_by(ReleaseBundle.created_at.desc())
    ).all()
    return {"bundles": [promotion_service.bundle_out(row) for row in rows]}


@router.get("/release-bundles/{bundle_id}")
def get_bundle(
    bundle_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return promotion_service.bundle_out(_bundle(db, bundle_id, ws.id))


# ── promotions ───────────────────────────────────────────────────────────────────


@router.post("/promotions", status_code=201)
def create_promotion(
    request: Request,
    req: PromotionRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Ask for a bundle to be released into another environment."""
    bundle = _bundle(db, req.bundle_id, ws.id)
    target = _target(db, req.target_workspace_id, identity, request)
    row = promotion_service.request_promotion(
        db,
        bundle=bundle,
        source_workspace_id=ws.id,
        target=target,
        requested_by=identity.username,
        change_note=req.change_note,
        rollback_note=req.rollback_note,
    )
    db.commit()
    return promotion_service.promotion_out(row, bundle)


@router.get("/promotions")
def list_promotions(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
    status: str | None = Query(default=None, max_length=16),
    target: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    """Requests raised in this workspace, newest first.

    Scoped by source (`workspace_id`) like every other ledger read, so an operator
    reviewing what is heading into prod selects the *source* workspace — the release is
    the source environment's artifact, and the target is a field on it.
    """
    query = select(Promotion).where(Promotion.workspace_id == ws.id)
    if status:
        query = query.where(Promotion.status == status)
    if target:
        query = query.where(Promotion.target_workspace_id == target)
    rows = db.scalars(query.order_by(Promotion.created_at.desc())).all()
    bundles = {
        bundle.id: bundle
        for bundle in db.scalars(
            select(ReleaseBundle).where(
                ReleaseBundle.id.in_([row.bundle_id for row in rows] or [""])
            )
        ).all()
    }
    return {
        "promotions": [
            _for_reader(
                promotion_service.promotion_out(row, bundles.get(row.bundle_id)),
                _granted_on(db, identity, row.target_workspace_id, request),
            )
            for row in rows
        ]
    }


def _promotion(db: Session, promotion_id: str, workspace_id: str) -> Promotion:
    row = db.get(Promotion, promotion_id)
    if row is None or row.workspace_id != workspace_id:
        raise AppError("promotion.not_found", "promotion not found", status_code=404)
    return row


@router.get("/promotions/{promotion_id}")
def get_promotion(
    request: Request,
    promotion_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """The request, its bundle, its gates, and the diff against the target.

    The diff compares the bundle's spec with whatever agent of that name currently runs
    in the target workspace — the question an approver actually has ("what changes over
    there?"), not "what changed since the last publish here".
    """
    row = _promotion(db, promotion_id, ws.id)
    bundle = db.get(ReleaseBundle, row.bundle_id)
    # The target agent is the TARGET workspace's data: only a caller granted there may
    # see it. Anyone else in the source workspace still sees the request itself.
    payload = _for_reader(
        promotion_service.promotion_out(row, bundle),
        _granted_on(db, identity, row.target_workspace_id, request),
    )
    if not payload["target_visible"]:
        payload["target_agent"] = None
        payload["diff"] = []
        return payload
    # the name is workspace-wide: another project's holder is flagged, never shown
    current, foreign = promotion_service.target_holder(
        db, row.target_workspace_id, bundle.agent_name if bundle else "", row.project_id
    )
    payload["target_agent"] = (
        {"id": current.id, "status": current.status, "version": current.version}
        if current
        else None
    )
    payload["target_name_taken"] = foreign
    payload["diff"] = promotion_service.spec_diff(
        current.spec if current else None, (bundle.spec if bundle else {}) or {}
    )
    return payload


@router.post("/promotions/{promotion_id}/review")
def review(
    request: Request,
    promotion_id: str,
    req: ReviewRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Approve or reject a pending request (needs `promotion.approve`).

    Journaled in `audit_events` either way: who accepted a release into which
    environment is exactly the kind of thing an audit asks about later.
    """
    row = _promotion(db, promotion_id, ws.id)
    # the approver must be granted on the environment the release goes INTO
    target = _target(db, row.target_workspace_id, identity, request)
    bundle = _bundle(db, row.bundle_id, ws.id)
    promotion_service.review_promotion(
        db,
        row,
        approve=req.decision == "approve",
        reviewer=identity.username,
        note=req.note,
        target=target,
        bundle=bundle,
    )
    record_audit_event(
        workspace_id=ws.id,
        actor=identity.username,
        action=f"promotion.{req.decision}",
        target=f"{bundle.agent_name}→{target.id} ({bundle.digest[:12]})",
        db=db,
    )
    db.commit()
    return promotion_service.promotion_out(row, bundle)


# ── release policy, plan preview and execution (T26/T27) ────────────────────────


class ReleasePolicyRequest(BaseModel):
    """An administrator's policy for releases INTO a workspace. Unset keys use tier defaults."""

    min_eval_score: float | None = Field(default=None, ge=0, le=1)
    require_policy_enforce: bool | None = None
    observe_seconds: int | None = Field(default=None, ge=0)
    smoke_prompts: list[str] | None = None
    window: dict[str, Any] | None = None
    freezes: list[dict[str, Any]] | None = None
    # Agent-DLC (docs/agent-dlc-design.md §6, §10): whether a deploy waits on the gate,
    # how long a judge calibration holds, and the evaluation spend guard. Validated
    # by `release_gates.normalize_policy`, so they are typed loosely here.
    release_mode: str | None = None
    calibration: dict[str, Any] | None = None
    eval_cost_confirm_usd: float | None = Field(default=None, ge=0)
    eval_cost_max_usd: float | None = Field(default=None, ge=0)


@router.get("/release-policies/{workspace_id}")
def get_release_policy(
    request: Request,
    workspace_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    target = _target(db, workspace_id, identity, request)
    return {
        "workspace_id": target.id,
        "tier": target.tier,
        "policy": target.release_policy or {},
        "effective": release_gates.effective_policy(target),
    }


@router.put("/release-policies/{workspace_id}")
def put_release_policy(
    request: Request,
    workspace_id: str,
    req: ReleasePolicyRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Replace the policy wholesale (administrator). Journaled: it decides what may ship."""
    target = _target(db, workspace_id, identity, request)
    target.release_policy = release_gates.normalize_policy(req.model_dump())
    record_audit_event(
        workspace_id=ws.id,
        actor=identity.username,
        action="release_policy.update",
        target=target.id,
        db=db,
    )
    db.commit()
    return {
        "workspace_id": target.id,
        "tier": target.tier,
        "policy": target.release_policy,
        "effective": release_gates.effective_policy(target),
    }


@router.get("/promotions/{promotion_id}/plan")
def promotion_plan(
    request: Request,
    promotion_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """What executing this promotion would create or change. Read-only."""
    row = _promotion(db, promotion_id, ws.id)
    bundle = _bundle(db, row.bundle_id, ws.id)
    target = _target(db, row.target_workspace_id, identity, request)
    return release_gates.build_plan(db, row, bundle, target, db.get(Workspace, ws.id))


@router.get("/promotions/{promotion_id}/execution")
def promotion_execution(
    request: Request,
    promotion_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Live progress: the promotion with its per-stage state and the job log.

    The job belongs to the TARGET workspace, so `/api/jobs/{id}` (scoped to the caller's
    workspace) cannot serve it; this reads it through the promotion the caller can see —
    and, because that log is the target's, only for a caller granted on the target.
    """
    row = _promotion(db, promotion_id, ws.id)
    _target(db, row.target_workspace_id, identity, request)
    bundle = db.get(ReleaseBundle, row.bundle_id)
    return {
        "promotion": promotion_service.promotion_out(row, bundle),
        "log": promotion_exec.job_log(db, row.job_id),
    }


def _start(job_id: str) -> None:
    promotion_exec.start_async(job_id)


@router.post("/promotions/{promotion_id}/execute", status_code=202)
def execute(
    request: Request,
    promotion_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Deliver an approved promotion into its target (needs `promotion.approve`).

    Refuses while a blocking gate fails, with a code naming it
    (`promotion.gate_failed.<gate>`). A `failed` run may be retried; the job restarts from
    the first stage, and stages already done are cheap to re-verify.
    """
    row = _promotion(db, promotion_id, ws.id)
    # authorize before revealing anything about the request's state
    target = _target(db, row.target_workspace_id, identity, request)
    if row.status == promotion_exec.STATUS_EXECUTING:
        raise AppError(
            "promotion.already_executing", "this promotion is already executing", status_code=409
        )
    if row.status not in (promotion_service.STATUS_APPROVED, promotion_exec.STATUS_FAILED) or (
        # a rollback that failed leaves the target in an unknown state: retry the rollback
        row.status == promotion_exec.STATUS_FAILED
        and (row.execution or {}).get("action") == promotion_exec.ACTION_ROLLBACK
    ):
        raise AppError(
            "promotion.not_executable",
            f"only an approved promotion can be executed (this one is {row.status})",
            status_code=409,
        )
    bundle = _bundle(db, row.bundle_id, ws.id)
    gates = release_gates.execution_gates(db, bundle, target)
    release_gates.assert_executable(gates)
    promotion_exec.assert_target_free(db, row, bundle)
    job = promotion_exec.admit(
        db,
        row,
        action=promotion_exec.ACTION_EXECUTE,
        bundle_id=bundle.id,
        previous_id=promotion_exec.previous_bundle_id(db, row, bundle),
        gates=gates,
    )
    record_audit_event(
        workspace_id=ws.id,
        actor=identity.username,
        action="promotion.execute",
        target=f"{bundle.agent_name}→{target.id} ({bundle.digest[:12]})",
        db=db,
    )
    db.commit()
    _start(job.id)
    return {"promotion": promotion_service.promotion_out(row, bundle), "job_id": job.id}


@router.post("/promotions/{promotion_id}/rollback", status_code=202)
def rollback(
    request: Request,
    promotion_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    """Re-deploy the bundle the target ran before this release (needs `promotion.approve`).

    A new publish in the target through the ordinary deploy path — never an AWS-side
    version revert. Refused when the target had no earlier release to return to, and not
    gated by the deploy window: a rollback is the way out of a bad release.
    """
    row = _promotion(db, promotion_id, ws.id)
    _target(db, row.target_workspace_id, identity, request)
    if row.status == promotion_exec.STATUS_EXECUTING:
        raise AppError(
            "promotion.already_executing", "this promotion is still executing", status_code=409
        )
    touched = bool((row.execution or {}).get("target_touched")) or (
        (row.execution or {}).get("action") == promotion_exec.ACTION_ROLLBACK
    )
    if row.status != promotion_exec.STATUS_SUCCEEDED and not (
        row.status == promotion_exec.STATUS_FAILED and touched
    ):
        raise AppError(
            "promotion.not_rollbackable",
            f"only a released promotion can be rolled back (this one is {row.status})",
            status_code=409,
        )
    if not row.previous_bundle_id:
        raise AppError(
            "promotion.no_previous_bundle",
            "the target ran no earlier release of this agent, so there is nothing to "
            "roll back to",
            status_code=409,
        )
    previous = db.get(ReleaseBundle, row.previous_bundle_id)
    bundle = _bundle(db, row.bundle_id, ws.id)
    if previous is None:
        raise AppError(
            "promotion.no_previous_bundle", "the previous release bundle is gone", status_code=409
        )
    promotion_exec.assert_target_free(db, row, bundle)
    job = promotion_exec.admit(
        db,
        row,
        action=promotion_exec.ACTION_ROLLBACK,
        bundle_id=previous.id,
        previous_id=row.previous_bundle_id,
    )
    record_audit_event(
        workspace_id=ws.id,
        actor=identity.username,
        action="promotion.rollback",
        target=f"{bundle.agent_name}→{row.target_workspace_id} (to {previous.digest[:12]})",
        db=db,
    )
    db.commit()
    _start(job.id)
    return {"promotion": promotion_service.promotion_out(row, bundle), "job_id": job.id}


# ── the administrator inbox (T22) ────────────────────────────────────────────────


def _projects_of(db: Session, rows: list[Any]) -> list[dict[str, Any] | None]:
    """Each sample's project, aligned with `sample`: the inbox is cross-project (route
    policy PROJECT_EXEMPT) while each `to` opens a project-scoped page, so the console
    switches project first. None for a workspace-level row (a bootstrap job)."""
    from app.models.projects import Project

    ids = {row.project_id for row in rows if row.project_id}
    names = {
        p.id: p.name for p in db.scalars(select(Project).where(Project.id.in_(ids or [""])))
    }
    return [
        {"id": row.project_id, "name": names.get(row.project_id, row.project_id)}
        if row.project_id
        else None
        for row in rows
    ]


@router.get("/inbox")
def inbox(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Everything waiting on a human, in one place.

    Mixes hub-global items (accounts, workspaces) with items from the **selected**
    workspace (failed jobs, promotions), because that is how an administrator works: one
    environment in focus, the tenancy always visible. Every entry carries a `to` the
    console turns into a link, so the inbox never becomes a dead end.
    """
    now = datetime.now(UTC)
    soon = now + timedelta(days=7)
    items: list[dict[str, Any]] = []

    pending_accounts = db.scalars(select(User).where(User.status == "pending")).all()
    if pending_accounts:
        items.append(
            {
                "key": "accounts_pending",
                "severity": "action",
                "count": len(pending_accounts),
                "to": "/v2/users?state=pending",
                "sample": [user.username for user in pending_accounts[:5]],
            }
        )

    expiring = db.scalars(
        select(User).where(
            User.status == "active", User.expires_at.is_not(None), User.expires_at <= soon
        )
    ).all()
    if expiring:
        items.append(
            {
                "key": "accounts_expiring",
                "severity": "warn",
                "count": len(expiring),
                "to": "/v2/users",
                "sample": [user.username for user in expiring[:5]],
            }
        )

    # `registered` counts as needing attention, not just `failed`/`bootstrapping`: a
    # registration nobody finished cannot be used, refuses mutating calls with 409, and
    # holds its (account, region) slot until someone bootstraps or purges it.
    stuck_workspaces = db.scalars(
        select(Workspace).where(
            Workspace.bootstrap_status.in_(("registered", "failed", "bootstrapping"))
        )
    ).all()
    if stuck_workspaces:
        items.append(
            {
                "key": "workspaces_attention",
                "severity": "warn",
                "count": len(stuck_workspaces),
                "to": "/v2/workspaces",
                "sample": [f"{row.id} ({row.bootstrap_status})" for row in stuck_workspaces[:5]],
            }
        )

    failed_jobs = db.scalars(
        select(Job)
        .where(Job.workspace_id == ws.id, Job.status == "failed")
        .order_by(Job.updated_at.desc())
        .limit(20)
    ).all()
    if failed_jobs:
        items.append(
            {
                "key": "jobs_failed",
                "severity": "warn",
                "count": len(failed_jobs),
                "to": "/v2/agents",
                "sample": [f"{job.type}: {(job.error or '')[:80]}" for job in failed_jobs[:5]],
                "sample_projects": _projects_of(db, failed_jobs[:5]),
            }
        )

    pending_promotions = db.scalars(
        select(Promotion).where(
            Promotion.workspace_id == ws.id, Promotion.status == promotion_service.STATUS_PENDING
        )
    ).all()
    if pending_promotions:
        items.append(
            {
                "key": "promotions_pending",
                "severity": "action",
                "count": len(pending_promotions),
                "to": "/v2/promotions",
                "sample": [
                    f"→ {row.target_workspace_id}: {row.change_note[:60]}"
                    for row in pending_promotions[:5]
                ],
                "sample_projects": _projects_of(db, pending_promotions[:5]),
            }
        )

    # T29: whatever the last alert evaluation saw. Read off the rows, so the inbox costs
    # no billed query — a stale `firing` is still the truth as of the last check, and the
    # Alerts page is where a fresh read is triggered.
    firing = alert_service.firing_rules(db, ws.id)
    if firing:
        items.append(
            {
                "key": "alerts_firing",
                "severity": "action",
                "count": len(firing),
                "to": "/v2/costs?view=alerts",
                "sample": [
                    f"{rule.name or rule.kind}: {rule.last_detail or '—'}" for rule in firing[:5]
                ],
            }
        )

    # Agents nobody has ever evaluated: the quality gap an operator should know about
    # before a release, counted rather than listed to keep the query cheap.
    from app.evaluation.models import EvalRun

    evaluated = select(EvalRun.agent_id).where(EvalRun.workspace_id == ws.id).distinct()
    unevaluated = db.scalar(
        select(func.count())
        .select_from(Agent)
        .where(
            Agent.workspace_id == ws.id,
            Agent.status == "active",
            Agent.system_key.is_(None),
            Agent.id.not_in(evaluated),
        )
    )
    if unevaluated:
        items.append(
            {
                "key": "agents_unevaluated",
                "severity": "info",
                "count": int(unevaluated),
                "to": "/v2/eval/tasks?view=new",
                "sample": [],
            }
        )

    order = {"action": 0, "warn": 1, "info": 2}
    items.sort(key=lambda item: (order.get(item["severity"], 3), -item["count"]))
    return {
        "workspace_id": ws.id,
        "generated_at": now.isoformat(),
        "total": sum(item["count"] for item in items),
        "items": items,
    }
