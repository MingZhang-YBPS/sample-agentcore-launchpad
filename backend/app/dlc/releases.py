"""Gate before traffic: `live` / `candidate` endpoints and release records (§6, §4.5).

A migrated agent (`endpoint_mode = "live"`) serves production through the named
endpoint **live**, which only moves when a release is decided. DEFAULT keeps
auto-rolling to every new version but carries no production traffic.

* `release_mode = direct` (workspace release policy, the default): after every
  successful deploy `live` follows the new version — today's behaviour, now through a
  named endpoint so a workspace can switch to gated without touching invoke paths.
* `release_mode = gated`: a deploy points **candidate** at the new version and opens a
  pending release record. Evaluations run against `candidate`; the gate report
  (dlc/gate.py) decides; a signer (≠ requester) releases, which re-points `live`.
  Rollback is re-pointing `live` at the previous live version — no rebuild.

Promotion executions are their own approval flow (promotion.approve + release gates in
the target) and keep `live` in lock-step with the deploy.

Storage (design storage-mounts §8): every open record carries the candidate's mount
diff against live (`attachments[kind=storage]`, also in the gate report); a new
read-write mount, `ro → rw`, or a change of storage / `data_class` under a mount must
be acknowledged when signing. A gate evaluation that writes shared-state storage in
production needs the runner's confirmation, recorded on the record.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.actor import same_actor, user_id_for
from app.core.errors import AppError, NotFoundError
from app.dlc import calibration as calibration_svc
from app.dlc import criteria as criteria_svc
from app.dlc import gate as gate_engine
from app.dlc import golden as golden_svc
from app.evaluation.models import EvalRun
from app.models.dlc import ReleaseRecord, Waiver
from app.models.ledger import Agent, Workspace
from app.services.audit import record_audit_event

logger = logging.getLogger(__name__)

LIVE = "live"
CANDIDATE = "candidate"
GATEABLE_METHODS = ("harness", "zip_runtime", "studio", "container", "byoc")
# Endpoint deletion is asynchronous and a *harness* endpoint can sit in DELETING for
# many minutes, so the in-request wait is deliberately short: long enough for a runtime
# endpoint (seconds), not long enough to hold an HTTP delete open. What is still
# DELETING is finished by `scheduler.sweep_deleted_resources`, which retries the
# resource delete until AWS has let go.
ENDPOINT_DELETE_TIMEOUT_S = 45.0
ENDPOINT_DELETE_POLL_S = 5.0


def _sleep(seconds: float) -> None:
    """Seam: the tests replace this instead of really waiting."""
    _time.sleep(seconds)
MAX_WAIVER_DAYS = 30
OPEN_DECISIONS = ("pending", "evaluating", "blocked", "invalid")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def release_mode(workspace: Workspace | None) -> str:
    return ((workspace.release_policy if workspace else None) or {}).get("release_mode") or (
        "direct"
    )


def gateable(agent: Agent) -> tuple[bool, str | None]:
    if agent.method not in GATEABLE_METHODS:
        return False, f"method '{agent.method}' has no named endpoints the platform manages"
    if (agent.spec or {}).get("protocol") == "a2a":
        return False, "A2A runtimes do not accept an endpoint qualifier"
    if (agent.spec or {}).get("protocol") == "mcp":
        return False, "MCP servers are served to agents through their Gateway target"
    if agent.system_key:
        return False, "system-managed presets are released by the platform"
    if not agent.resource_id or not agent.version:
        return False, "the agent has no deployed version yet"
    return True, None


# ── endpoints ──────────────────────────────────────────────────────────────────


def point_endpoint(control: Any, agent: Agent, name: str, version: str) -> dict[str, Any]:
    """Create or re-point a named endpoint at `version` and wait until it serves it."""
    if agent.method == "harness":
        from app.services.agentcore import harness as hc

        return hc.ensure_harness_endpoint(
            control, agent.resource_id, name, str(version),
            description=f"Launchpad {name}",
        )
    from app.services.agentcore import runtime as rt

    return rt.ensure_runtime_endpoint(
        control, runtime_id=agent.resource_id, endpoint_name=name, version=str(version)
    )


def endpoint_version(control: Any, agent: Agent, name: str) -> str | None:
    try:
        if agent.method == "harness":
            from app.services.agentcore import harness as hc

            detail = hc.get_harness_endpoint(control, agent.resource_id, name)
        else:
            from app.services.agentcore import runtime as rt

            detail = rt.get_runtime_endpoint(
                control, runtime_id=agent.resource_id, endpoint_name=name
            )
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ in ("ResourceNotFoundException", "NotFoundException"):
            return None
        raise
    value = detail.get("liveVersion") or detail.get("targetVersion")
    return str(value) if value is not None else None


def migrate_agent(db: Session, agent: Agent, control: Any, *, actor: str) -> Agent:
    """Create `live` at the current version and switch production traffic onto it."""
    ok, reason = gateable(agent)
    if not ok:
        raise AppError("release.not_gateable", reason or "agent cannot use a live endpoint",
                       status_code=409)
    if agent.status != "active":
        raise AppError("release.not_active", "migrate an active agent", status_code=409)
    point_endpoint(control, agent, LIVE, agent.version)
    agent.endpoint_mode = "live"
    record_audit_event(workspace_id=agent.workspace_id, actor=actor,
                       action="release.migrate_live", target=agent.id, db=db)
    db.flush()
    return agent


def after_deploy(db: Session, agent: Agent, *, note: str | None, actor: str | None,
                 actor_user_id: str | None = None) -> None:
    """Called when a deploy job succeeds. Never raises (the deploy itself succeeded)."""
    if getattr(agent, "endpoint_mode", None) != "live" or not agent.version:
        return
    try:
        from app.services.agentcore.client import control_client
        from app.services.workspace import context_for_workspace

        workspace = db.get(Workspace, agent.workspace_id)
        control = control_client(context_for_workspace(agent.workspace_id))
        promotion_driven = bool(note) and str(note).startswith(
            ("promotion ", "rollback of promotion", "revert after failed promotion")
        )
        if release_mode(workspace) != "gated" or promotion_driven:
            point_endpoint(control, agent, LIVE, agent.version)
            return
        previous = endpoint_version(control, agent, LIVE)
        point_endpoint(control, agent, CANDIDATE, agent.version)
        criteria_set = criteria_svc.agent_set(db, agent.workspace_id, agent.id)
        for stale in db.scalars(
            select(ReleaseRecord).where(
                ReleaseRecord.agent_id == agent.id,
                ReleaseRecord.decision.in_(OPEN_DECISIONS),
            )
        ).all():
            stale.decision = "superseded"
            stale.note = f"superseded by version {agent.version}"
        record = ReleaseRecord(
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            candidate_version=str(agent.version),
            candidate_endpoint=CANDIDATE,
            previous_live_version=previous,
            criteria_set_id=criteria_set.id if criteria_set else None,
            criteria_set_version=criteria_set.version if criteria_set else None,
            decision="pending",
            requested_by=actor or agent.created_by_name or agent.owner or "",
            # with no actor the requester is the agent's creator, whose id is on the row
            requested_by_user_id=(
                user_id_for(actor, actor_user_id) if actor
                else agent.created_by if agent.created_by_name else None
            ),
            note=note or "",
        )
        db.add(record)
        db.flush()
        try:
            refresh_storage(db, record, agent)
            db.flush()
        except Exception as exc:  # noqa: BLE001 — the gate recomputes it
            logger.warning("storage evidence of release %s failed: %s", record.id, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("release bookkeeping after deploy of %s failed: %s", agent.id, exc)


# ── records ────────────────────────────────────────────────────────────────────


def get_record(db: Session, workspace_id: str, record_id: str) -> ReleaseRecord:
    row = db.get(ReleaseRecord, record_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError("release.not_found", "release record not found")
    return row


def records_for(db: Session, workspace_id: str, agent_id: str, limit: int = 50) -> list:
    return list(db.scalars(
        select(ReleaseRecord).where(
            ReleaseRecord.workspace_id == workspace_id, ReleaseRecord.agent_id == agent_id
        ).order_by(ReleaseRecord.created_at.desc()).limit(limit)
    ).all())


def pending_for(db: Session, workspace_id: str, agent_id: str) -> ReleaseRecord | None:
    return db.scalars(
        select(ReleaseRecord).where(
            ReleaseRecord.workspace_id == workspace_id,
            ReleaseRecord.agent_id == agent_id,
            ReleaseRecord.decision.in_(OPEN_DECISIONS),
        ).order_by(ReleaseRecord.created_at.desc())
    ).first()


def latest_decided(db: Session, workspace_id: str, agent_id: str) -> ReleaseRecord | None:
    """The newest release record that carries a gate report, open or already decided.

    The scorecard asks "what did the gate last say", which outlives the open release:
    reading only the pending record made a freshly released agent show no verdict at all.
    """
    for row in db.scalars(
        select(ReleaseRecord).where(
            ReleaseRecord.workspace_id == workspace_id,
            ReleaseRecord.agent_id == agent_id,
        ).order_by(ReleaseRecord.created_at.desc()).limit(20)
    ).all():
        if (row.gate_report or {}).get("verdict"):
            return row
    return None


def record_out(row: ReleaseRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "promotion_id": row.promotion_id,
        "candidate_version": row.candidate_version,
        "candidate_endpoint": row.candidate_endpoint,
        "previous_live_version": row.previous_live_version,
        "criteria_set_id": row.criteria_set_id,
        "criteria_set_version": row.criteria_set_version,
        "golden_versions": row.golden_versions or {},
        "evaluator_set_hash": row.evaluator_set_hash,
        "run_ids": row.run_ids or [],
        "gate_report": row.gate_report or {},
        "decision": row.decision,
        "requested_by": row.requested_by,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "note": row.note,
        "waiver_ids": row.waiver_ids or [],
        "rollback_target": row.rollback_target or {},
        "attachments": row.attachments or [],
        # the mount diff against live (storage-mounts §8.1); None before it is computed
        "storage": _attachment(row, STORAGE_EVIDENCE),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ── storage evidence (storage-mounts §8) ───────────────────────────────────────

STORAGE_EVIDENCE = "storage"
STORAGE_WRITE_CONFIRMATION = "storage_write_confirmation"


def _attachment(row: ReleaseRecord, kind: str) -> dict[str, Any] | None:
    return next((a for a in row.attachments or [] if a.get("kind") == kind), None)


def _set_attachment(row: ReleaseRecord, kind: str, value: dict[str, Any]) -> None:
    # a new list: the JSON column only notices a reassignment
    row.attachments = [a for a in row.attachments or [] if a.get("kind") != kind] + [
        {**value, "kind": kind}
    ]


def version_spec(db: Session, agent: Agent, version: str | None) -> dict[str, Any] | None:
    """The spec `version` was published from: its snapshot (T18), else the agent row
    while the row still is that version. None when nothing records it."""
    if not version:
        return None
    from app.models.ledger import SpecSnapshot

    snap = db.scalars(select(SpecSnapshot).where(
        SpecSnapshot.agent_id == agent.id, SpecSnapshot.aws_version == str(version),
    ).order_by(SpecSnapshot.seq.desc())).first()
    if snap is not None:
        return snap.spec or {}
    if str(agent.version or "") == str(version):
        return agent.spec or {}
    return None


def _live_mounts(db: Session, record: ReleaseRecord, agent: Agent) -> list[dict] | None:
    """The live version's mounts, with the `data_class` each had when that version was
    signed (so a reclassification since then shows as a change). `[]` for a first
    release; None when the live version's spec is unknown."""
    from app.services import storage_dlc

    if not record.previous_live_version:
        return []
    spec = version_spec(db, agent, record.previous_live_version)
    if spec is None:
        return None
    mounts = storage_dlc.with_catalog(db, agent.workspace_id, storage_dlc.mounts_of(spec))
    signed = db.scalars(select(ReleaseRecord).where(
        ReleaseRecord.agent_id == agent.id,
        ReleaseRecord.candidate_version == str(record.previous_live_version),
        ReleaseRecord.decision.in_(("released", "rolled_back")),
    ).order_by(ReleaseRecord.created_at.desc())).first()
    then = _attachment(signed, STORAGE_EVIDENCE) if signed is not None else None
    classes = {m["mount_path"]: m.get("data_class") for m in (then or {}).get("candidate") or []}
    for mount in mounts:
        if classes.get(mount["mount_path"]) and mount.get("storage_id"):
            mount["data_class"] = classes[mount["mount_path"]]
    return mounts


def storage_evidence(db: Session, record: ReleaseRecord, agent: Agent) -> dict[str, Any]:
    """The candidate's mounts against live's, freshly read (§8.1)."""
    from app.services import storage_dlc

    out = storage_dlc.evidence(
        db, agent.workspace_id, version_spec(db, agent, record.candidate_version),
        _live_mounts(db, record, agent),
    )
    confirmed = _attachment(record, STORAGE_WRITE_CONFIRMATION)
    if confirmed:
        out["write_confirmation"] = {k: v for k, v in confirmed.items() if k != "kind"}
    return out


def refresh_storage(db: Session, record: ReleaseRecord, agent: Agent) -> dict[str, Any]:
    """Recompute the evidence and keep it on the record; returned for the report.
    An acknowledgement already given survives only while the evidence is unchanged."""
    out = storage_evidence(db, record, agent)
    previous = _attachment(record, STORAGE_EVIDENCE) or {}
    if previous.get("acknowledged_by") and previous.get("changes") == out["changes"]:
        out["acknowledged_by"] = previous["acknowledged_by"]
        out["acknowledged_at"] = previous.get("acknowledged_at")
    _set_attachment(record, STORAGE_EVIDENCE, out)
    return out


# ── gate evaluation ────────────────────────────────────────────────────────────


def _gate_plan(
    db: Session, record: ReleaseRecord, agent: Agent
) -> tuple[list[Any], list[str], dict[str, Any]]:
    """What a gate evaluation runs: the criteria rows, their evaluators and the golden
    set's splits. Raises the AppError a release that cannot be evaluated answers."""
    if not record.criteria_set_id:
        raise AppError("release.no_criteria",
                       "publish a criteria set for this agent before evaluating a release",
                       status_code=409)
    cset = db.get(criteria_svc.CriteriaSet, record.criteria_set_id)
    rows = criteria_svc.criteria_of(db, record.criteria_set_id)
    evaluators = sorted({
        (r.executor or {}).get("evaluator_id") for r in rows
        if (r.executor or {}).get("kind") == "evaluator" and (r.executor or {}).get("evaluator_id")
    })
    parent = golden_svc.for_criteria(db, agent.workspace_id, cset.lineage_id)
    if parent is None:
        raise AppError("release.no_golden",
                       "create the golden set for these criteria before evaluating a release",
                       status_code=409)
    return rows, evaluators, golden_svc.splits_of(db, parent)


def gate_estimate(
    db: Session, record: ReleaseRecord, agent: Agent, workspace_ctx: Any, *, repeats: int = 1
) -> dict[str, Any]:
    """The cost of `start_evaluation` for this release: both gate splits × `repeats`,
    priced with the criteria's evaluators — the same estimate the gate enforces."""
    from app.dlc import cost as cost_svc

    rows, evaluators, splits = _gate_plan(db, record, agent)
    gate_items = [
        item for name in ("regression", "holdout") if splits.get(name)
        for item in golden_svc.active_items(splits[name])
    ]
    return cost_svc.estimate(
        db, agent=agent, workspace=db.get(Workspace, record.workspace_id),
        workspace_ctx=workspace_ctx, items=gate_items, evaluators=evaluators,
        repeats=repeats, criteria_rows=rows,
    )


def start_evaluation(
    db: Session,
    record: ReleaseRecord,
    agent: Agent,
    workspace_ctx: Any,
    *,
    actor: str,
    repeats: int = 1,
    submit: Any = None,
    confirm_cost: bool = False,
    is_admin: bool = False,
    confirm_storage: bool = False,
) -> list[str]:
    """Queue the regression + holdout runs the gate reads, against `candidate`.

    Priced first: the estimate covers both splits × `repeats`, and the workspace's
    `eval_cost_confirm_usd` / `eval_cost_max_usd` are enforced here, not only shown.
    A candidate that mounts shared-state storage read-write writes into it during the
    replay: 409 `storage.write_confirm_required` unless `confirm_storage` (§8.2, D-3);
    the confirmation is recorded on the record and in the audit journal.
    """
    if record.decision not in ("pending", "invalid", "blocked"):
        raise AppError("release.not_open", "this release is already decided", status_code=409)
    rows, evaluators, splits = _gate_plan(db, record, agent)
    from app.evaluation import agentcore_eval as ac

    if len(evaluators) > ac.MAX_BATCH_EVALUATORS:
        raise AppError("release.too_many_evaluators",
                       f"the criteria use {len(evaluators)} evaluators; a run applies at most "
                       f"{ac.MAX_BATCH_EVALUATORS}", status_code=422)
    from app.dlc import cost as cost_svc

    estimate = gate_estimate(db, record, agent, workspace_ctx, repeats=repeats)
    cost_svc.assert_allowed(estimate, confirmed=confirm_cost, is_admin=is_admin)
    from app.services import storage_dlc

    writes = storage_dlc.require_write_confirmation(
        db, record.workspace_id, version_spec(db, agent, record.candidate_version),
        confirmed=confirm_storage, action="release.evaluate",
    )
    if submit is None:
        from app.evaluation import service as eval_service

        submit = eval_service.submit_run
    run_ids: list[str] = []
    golden_versions: dict[str, Any] = {}
    for name in ("regression", "holdout"):
        split = splits.get(name)
        items = golden_svc.active_items(split) if split else []
        if not items:
            continue
        golden_versions[name] = {"dataset_id": split.id,
                                 "version": golden_svc._version_label(split),
                                 "items": len(items)}
        run = submit(
            agent=agent,
            workspace=workspace_ctx,
            dataset_items=items,
            dataset_id=split.id,
            dataset_name=split.name,
            evaluators=evaluators,
            name=f"release {record.candidate_version} · {name}"[:64],
            description=f"gate evaluation of release {record.id}",
            repeats=repeats,
            qualifier=record.candidate_endpoint or CANDIDATE,
            criteria_set_id=record.criteria_set_id,
            criteria_set_version=record.criteria_set_version,
            split=name,
            agent_version=record.candidate_version,
        )
        run_ids.append(run.id)
    if not run_ids:
        raise AppError("release.empty_golden",
                       "the golden set has no regression or holdout items", status_code=409)
    record.run_ids = run_ids
    record.golden_versions = golden_versions
    record.decision = "evaluating"
    record.gate_report = {}
    from app.evaluation.service import evaluator_set_hash

    record.evaluator_set_hash = evaluator_set_hash(evaluators)
    if writes:
        _set_attachment(record, STORAGE_WRITE_CONFIRMATION,
                        storage_dlc.confirmation(writes, actor))
        record_audit_event(workspace_id=record.workspace_id, actor=actor,
                           action="storage.write_confirmed",
                           target=f"{record.id}:{','.join(m['storage_id'] for m in writes)}",
                           db=db)
    record_audit_event(workspace_id=record.workspace_id, actor=actor,
                       action="release.evaluate", target=record.id, db=db)
    db.flush()
    return run_ids


def provenance(
    db: Session, record: ReleaseRecord, runs: list[EvalRun], workspace: Workspace | None
) -> dict[str, Any]:
    issues: list[str] = []
    cset = db.get(criteria_svc.CriteriaSet, record.criteria_set_id) if record.criteria_set_id \
        else None
    if cset is None:
        issues.append("no criteria set")
    elif not cset.signed_by:
        issues.append(f"criteria v{cset.version} is not signed by the business owner")
    for run in runs:
        if str(run.agent_version or "") != str(record.candidate_version or ""):
            issues.append(f"run {run.id} evaluated version {run.agent_version}, "
                          f"not the candidate {record.candidate_version}")
        if (run.endpoint_qualifier or "") != (record.candidate_endpoint or CANDIDATE):
            issues.append(f"run {run.id} did not invoke the candidate endpoint")
        if run.criteria_set_id != record.criteria_set_id:
            issues.append(f"run {run.id} used another criteria version")
    golden = record.golden_versions or {}
    if "holdout" not in golden:
        issues.append("no holdout split was evaluated")
    return {
        "criteria_set_version": record.criteria_set_version,
        "criteria_signed_by": cset.signed_by if cset else None,
        "golden_versions": golden,
        "evaluator_set_hash": record.evaluator_set_hash,
        "candidate_version": record.candidate_version,
        "previous_live_version": record.previous_live_version,
        "issues": issues,
    }


def evaluate(db: Session, record: ReleaseRecord) -> dict[str, Any]:
    """Compute (or refresh) the gate report once every gate run has completed."""
    runs = [db.get(EvalRun, rid) for rid in record.run_ids or []]
    runs = [r for r in runs if r is not None]
    if not runs:
        raise AppError("release.not_evaluated", "start the gate evaluation first",
                       status_code=409)
    active = [r for r in runs if r.status not in ("completed", "failed", "stopped")]
    if active:
        return {"status": "evaluating", "runs": [{"id": r.id, "status": r.status} for r in runs]}
    failed = [r for r in runs if r.status != "completed"]
    rows = criteria_svc.criteria_of(db, record.criteria_set_id)
    workspace = db.get(Workspace, record.workspace_id)
    policy = calibration_svc.policy_of(workspace.release_policy if workspace else None)
    calibration = calibration_svc.calibrated_keys(db, record.workspace_id, record.agent_id,
                                                  rows, policy)
    waivers = list(db.scalars(select(Waiver).where(
        Waiver.workspace_id == record.workspace_id, Waiver.agent_id == record.agent_id,
    )).all())
    prov = provenance(db, record, runs, workspace)
    if failed:
        prov["issues"].append(f"{len(failed)} gate run(s) did not complete")
    missing_summary = [r.id for r in runs if not (r.criteria_summary or {}).get("criteria")]
    if missing_summary:
        prov["issues"].append("criterion results are missing for run(s) "
                              + ", ".join(missing_summary))
    previous = db.scalars(
        select(ReleaseRecord).where(
            ReleaseRecord.agent_id == record.agent_id, ReleaseRecord.decision == "released",
        ).order_by(ReleaseRecord.decided_at.desc())
    ).first()
    report = gate_engine.decide(
        rows=rows,
        summaries=[r.criteria_summary or {} for r in runs if r.status == "completed"],
        calibration=calibration,
        waivers=waivers,
        provenance=prov,
        previous=(previous.gate_report if previous else None),
    )
    report["runs"] = [{"id": r.id, "split": r.split, "status": r.status,
                       "denominator": r.denominator} for r in runs]
    agent = db.get(Agent, record.agent_id)
    if agent is not None:
        report["storage"] = refresh_storage(db, record, agent)
    record.gate_report = report
    record.waiver_ids = [w["waiver"]["id"] for w in report["criteria"] if w.get("waiver")]
    record.decision = {"PASS": "pending", "BLOCKED": "blocked", "INVALID": "invalid"}[
        report["verdict"]
    ]
    db.flush()
    return {"status": "decided", "report": report}


def sign(db: Session, record: ReleaseRecord, agent: Agent, control: Any, *, actor: str,
         note: str = "", actor_user_id: str | None = None,
         storage_ack: bool = False) -> ReleaseRecord:
    """Release the candidate. A storage change that needs sign-off (§8.1) needs
    `storage_ack`: 409 `release.storage_ack_required` otherwise, with the changes."""
    report = record.gate_report or {}
    if report.get("verdict") != "PASS":
        raise AppError("release.gate_not_passed",
                       "only a release whose gate report is PASS can be released",
                       {"verdict": report.get("verdict")}, status_code=409)
    if record.decision != "pending":
        raise AppError("release.not_open", "this release is already decided", status_code=409)
    actor_user_id = user_id_for(actor, actor_user_id)
    # the same person by account id when both sides carry one (design §3.6), else by name
    if same_actor(record.requested_by, record.requested_by_user_id, actor, actor_user_id):
        raise AppError("release.self_sign", "the requester cannot sign their own release",
                       status_code=409)
    storage = refresh_storage(db, record, agent)
    if storage["requires_ack"]:
        if not storage_ack:
            raise AppError(
                "release.storage_ack_required",
                "this release changes read-write storage mounts — acknowledge the storage "
                "changes to sign it",
                {"changes": [c for c in storage["changes"] if c["requires_ack"]]},
                status_code=409,
            )
        storage["acknowledged_by"], storage["acknowledged_at"] = actor, _now().isoformat()
        _set_attachment(record, STORAGE_EVIDENCE, storage)
    point_endpoint(control, agent, LIVE, record.candidate_version)
    record.decision = "released"
    record.decided_by = actor
    record.decided_by_user_id = actor_user_id
    record.decided_at = _now()
    record.note = note[:2000] or record.note
    record.rollback_target = {"version": record.previous_live_version}
    record_audit_event(workspace_id=record.workspace_id, actor=actor,
                       action="release.sign", target=record.id, db=db)
    if storage["requires_ack"]:
        record_audit_event(workspace_id=record.workspace_id, actor=actor,
                           action="release.storage_ack", target=record.id, db=db)
    db.flush()
    return record


def block(db: Session, record: ReleaseRecord, *, actor: str, note: str,
          actor_user_id: str | None = None) -> ReleaseRecord:
    if record.decision in ("released", "rolled_back", "superseded"):
        raise AppError("release.not_open", "this release is already decided", status_code=409)
    record.decision = "blocked"
    record.decided_by = actor
    record.decided_by_user_id = user_id_for(actor, actor_user_id)
    record.decided_at = _now()
    record.note = note[:2000]
    record_audit_event(workspace_id=record.workspace_id, actor=actor,
                       action="release.block", target=record.id, db=db)
    db.flush()
    return record


def rollback(db: Session, agent: Agent, control: Any, *, actor: str, note: str = "",
             actor_user_id: str | None = None) -> dict:
    """Re-point `live` at the version it served before the last release."""
    if getattr(agent, "endpoint_mode", None) != "live":
        raise AppError("release.not_live", "this agent does not serve through a live endpoint",
                       status_code=409)
    last = db.scalars(
        select(ReleaseRecord).where(
            ReleaseRecord.agent_id == agent.id, ReleaseRecord.decision == "released",
        ).order_by(ReleaseRecord.decided_at.desc())
    ).first()
    if last is None or not last.previous_live_version:
        raise AppError("release.nothing_to_roll_back",
                       "no released version with a previous live version", status_code=409)
    current = endpoint_version(control, agent, LIVE)
    point_endpoint(control, agent, LIVE, last.previous_live_version)
    last.decision = "rolled_back"
    rolled = ReleaseRecord(
        workspace_id=agent.workspace_id, agent_id=agent.id,
        candidate_version=last.previous_live_version, candidate_endpoint=LIVE,
        previous_live_version=current, criteria_set_id=last.criteria_set_id,
        criteria_set_version=last.criteria_set_version, decision="rolled_back",
        requested_by=actor, decided_by=actor, decided_at=_now(),
        requested_by_user_id=user_id_for(actor, actor_user_id),
        decided_by_user_id=user_id_for(actor, actor_user_id),
        note=note[:2000] or f"rollback of release {last.id}",
        rollback_target={"from_release": last.id},
    )
    db.add(rolled)
    record_audit_event(workspace_id=agent.workspace_id, actor=actor,
                       action="release.rollback", target=agent.id, db=db)
    db.flush()
    return {"live_version": last.previous_live_version, "record_id": rolled.id}


def live_state(control: Any, agent: Agent) -> dict[str, Any]:
    state: dict[str, Any] = {"endpoint_mode": agent.endpoint_mode or "default",
                             "ledger_version": agent.version}
    ok, reason = gateable(agent)
    state["gateable"], state["gateable_reason"] = ok, reason
    if agent.endpoint_mode == "live" and control is not None:
        try:
            state["live_version"] = endpoint_version(control, agent, LIVE)
            state["candidate_version"] = endpoint_version(control, agent, CANDIDATE)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return state


# ── waivers ────────────────────────────────────────────────────────────────────


def request_waiver(
    db: Session,
    *,
    workspace_id: str,
    agent_id: str,
    criterion_key: str,
    actual: float | None,
    threshold: float | None,
    reason: str,
    risk_owner: str,
    compensating_control: str,
    expires_on: datetime,
    actor: str,
    actor_user_id: str | None = None,
) -> Waiver:
    cset = criteria_svc.agent_set(db, workspace_id, agent_id)
    if cset is None:
        raise AppError("waiver.no_criteria", "the agent has no published criteria set",
                       status_code=409)
    row = next((c for c in criteria_svc.criteria_of(db, cset.id) if c.key == criterion_key),
               None)
    if row is None:
        raise NotFoundError("waiver.unknown_criterion", "criterion not in the agent's set")
    if row.tier == "redline":
        raise AppError("waiver.redline", "red lines are never waived", status_code=409)
    expires = _aware(expires_on)
    if expires is None or expires <= _now():
        raise AppError("waiver.bad_expiry", "a waiver expires in the future")
    if expires > _now() + timedelta(days=MAX_WAIVER_DAYS):
        raise AppError("waiver.too_long",
                       f"a waiver lasts at most {MAX_WAIVER_DAYS} days")
    if not reason.strip() or not risk_owner.strip():
        raise AppError("waiver.incomplete", "a waiver names its reason and risk owner")
    waiver = Waiver(
        workspace_id=workspace_id, agent_id=agent_id, criterion_key=criterion_key,
        criteria_lineage_id=cset.lineage_id, criteria_set_version=cset.version,
        actual=actual, threshold=threshold, reason=reason[:2000], risk_owner=risk_owner[:64],
        compensating_control=compensating_control[:2000], expires_on=expires,
        status="requested", requested_by=actor,
        requested_by_user_id=user_id_for(actor, actor_user_id),
    )
    db.add(waiver)
    record_audit_event(workspace_id=workspace_id, actor=actor, action="waiver.request",
                       target=f"{agent_id}:{criterion_key}", db=db)
    db.flush()
    return waiver


def decide_waiver(db: Session, waiver: Waiver, *, actor: str, approve: bool,
                  note: str = "", actor_user_id: str | None = None) -> Waiver:
    if waiver.status != "requested":
        raise AppError("waiver.not_open", "this waiver is already decided", status_code=409)
    actor_user_id = user_id_for(actor, actor_user_id)
    if approve and same_actor(waiver.requested_by, waiver.requested_by_user_id,
                              actor, actor_user_id):
        raise AppError("waiver.self_approve", "the requester cannot approve their own waiver",
                       status_code=409)
    waiver.status = "approved" if approve else "rejected"
    waiver.approved_by = actor
    waiver.approved_by_user_id = actor_user_id
    waiver.approved_at = _now()
    waiver.note = note[:2000]
    record_audit_event(workspace_id=waiver.workspace_id, actor=actor,
                       action=f"waiver.{'approve' if approve else 'reject'}",
                       target=waiver.id, db=db)
    db.flush()
    return waiver


def revoke_waiver(db: Session, waiver: Waiver, *, actor: str) -> Waiver:
    if waiver.status not in ("requested", "approved"):
        raise AppError("waiver.not_open", "this waiver is no longer open", status_code=409)
    waiver.status = "revoked"
    record_audit_event(workspace_id=waiver.workspace_id, actor=actor, action="waiver.revoke",
                       target=waiver.id, db=db)
    db.flush()
    return waiver


def waiver_out(w: Waiver, *, history: int | None = None) -> dict[str, Any]:
    expires = _aware(w.expires_on)
    return {
        "id": w.id,
        "agent_id": w.agent_id,
        "criterion_key": w.criterion_key,
        "criteria_set_version": w.criteria_set_version,
        "actual": w.actual,
        "threshold": w.threshold,
        "reason": w.reason,
        "risk_owner": w.risk_owner,
        "compensating_control": w.compensating_control,
        "expires_on": expires.isoformat() if expires else None,
        "expired": bool(expires and expires <= _now()),
        "active": gate_engine.waiver_active(w),
        "status": w.status,
        "requested_by": w.requested_by,
        "approved_by": w.approved_by,
        "approved_at": w.approved_at.isoformat() if w.approved_at else None,
        "note": w.note,
        "times_waived": history,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


def endpoint_status(agent: Agent, control: Any, name: str) -> str | None:
    """`READY` / `DELETING` / … for a named endpoint, or None once AWS forgot it."""
    try:
        if agent.method == "harness":
            from app.services.agentcore import harness as hc

            detail = hc.get_harness_endpoint(control, agent.resource_id, name)
        else:
            from app.services.agentcore import runtime as rt

            detail = rt.get_runtime_endpoint(
                control, runtime_id=agent.resource_id, endpoint_name=name
            )
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ in ("ResourceNotFoundException", "NotFoundException"):
            return None
        raise
    return str((detail or {}).get("status") or "UNKNOWN")


def _endpoint_gone(agent: Agent, control: Any, name: str) -> bool:
    """True once AWS no longer knows this endpoint."""
    return endpoint_status(agent, control, name) is None


def delete_endpoints(
    agent: Agent, control: Any, *, log: Any = None, timeout_s: float = ENDPOINT_DELETE_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Delete the named endpoints a gated agent serves through, and WAIT for them.

    AgentCore will not delete a runtime/harness that still has endpoints, and
    `DeleteHarnessEndpoint` / `DeleteAgentRuntimeEndpoint` are asynchronous — the
    endpoint sits in DELETING for a while. Returning as soon as the calls are issued
    made the agent delete fail with "has endpoints that must be deleted first", which
    is exactly the trap this function exists to avoid. So both deletes are issued
    first (AWS removes them in parallel) and then each is waited out.

    Every failure is reported rather than raised: a stuck endpoint must not make an
    agent undeletable, and the caller's own delete will say so if it still cannot run.
    """
    out: list[dict[str, Any]] = []
    if not agent.resource_id or getattr(agent, "endpoint_mode", None) != "live":
        return out
    issued: list[str] = []
    for name in (CANDIDATE, LIVE):
        status = "deleted"
        try:
            if agent.method == "harness":
                from app.services.agentcore import harness as hc

                if not hc.delete_harness_endpoint(control, agent.resource_id, name):
                    status = "absent"
            else:
                from app.services.agentcore import runtime as rt

                rt.delete_runtime_endpoint(control, runtime_id=agent.resource_id,
                                           endpoint_name=name)
        except Exception as exc:  # noqa: BLE001 — never block the agent's deletion
            kind = type(exc).__name__
            if kind in ("ResourceNotFoundException", "NotFoundException"):
                status = "absent"
            else:
                # already DELETING from an earlier attempt still has to be waited out
                status = "deleting" if kind == "ConflictException" else f"skipped:{kind}"
                if status.startswith("skipped"):
                    logger.warning("agent %s: could not delete endpoint %s: %s: %s",
                                   agent.id, name, kind, exc)
        if status in ("deleted", "deleting"):
            issued.append(name)
        out.append({"endpoint": name, "status": status})
        if log:
            log(f"endpoint {name}: {status}")
    deadline = _time.monotonic() + timeout_s
    for name in issued:
        row = next(r for r in out if r["endpoint"] == name)
        while _time.monotonic() < deadline:
            try:
                if _endpoint_gone(agent, control, name):
                    row["status"] = "deleted"
                    break
            except Exception as exc:  # noqa: BLE001
                row["status"] = f"unknown:{type(exc).__name__}"
                break
            _sleep(ENDPOINT_DELETE_POLL_S)
        else:
            row["status"] = "timeout"
            logger.warning("agent %s: endpoint %s still present after %ss",
                           agent.id, name, timeout_s)
        if log:
            log(f"endpoint {name}: {row['status']}")
    return out
