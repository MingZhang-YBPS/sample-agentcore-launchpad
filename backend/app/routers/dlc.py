"""Agent-DLC console API: criteria, golden sets, calibration, releases, admission, watch.

Routes do auth, workspace scoping and AWS clients; every decision lives in `app/dlc/*`
so it can be tested without a request. Permissions are declared in
`core/route_policy.py` — the three that decide what "good" means (`criteria.sign`,
`golden.admit`, `judge.calibrate`) are granted to named people, not roles.
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import owned_get
from app.dlc import admission as admission_svc
from app.dlc import calibration as cal_svc
from app.dlc import compare as compare_svc
from app.dlc import cost as cost_svc
from app.dlc import criteria as criteria_svc
from app.dlc import engine as engine_svc
from app.dlc import golden as golden_svc
from app.dlc import releases as release_svc
from app.dlc import watch as watch_svc
from app.evaluation.models import EvalDataset, EvalRun
from app.models.dlc import CriteriaSet, Waiver
from app.models.ledger import Agent, AuditEvent
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import access, agent_access, visibility
from app.services.agentcore.client import control_client
from app.services.visibility import Viewer, require_viewer

router = APIRouter(
    prefix="/api",
    tags=["agent-dlc"],
    # workspace isolation: a hidden `{agent_id}` / `{run_id}` reads as missing
    dependencies=[Depends(agent_access.require_path_ids_visible)],
)


# ── helpers ────────────────────────────────────────────────────────────────────


def _actor(request: Request) -> str:
    return require_identity(request).username


def _bypasses(request: Request) -> bool:
    """The separation-of-duties and cost-guard exemption (design §3.6): phase 1, admins."""
    return access.has_data_bypass(require_identity(request))


def _can(request: Request, key: str) -> bool:
    """A permission as `require_permission` reads it: the project's keys inside one."""
    project = getattr(request.state, "project", None)
    return project.can(key) if project is not None else require_identity(request).can(key)


def _sees_verdicts(request: Request) -> bool:
    """Blind labelling: only a calibrator (or an administrator) sees the judge's verdicts."""
    return _bypasses(request) or _can(request, "judge.calibrate")


def _writable(request: Request) -> bool:
    """Whether a GET may persist what it computes (a refresh, a backfill). An archived
    project serves reads only, so those writes are skipped there."""
    project = getattr(request.state, "project", None)
    return project is None or project.status == "active"


def _agent(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != ws.id or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    return agent


def _evaluator_kind_resolver(ws: WorkspaceScope):
    """evaluator id → code | trajectory | judge, one GetEvaluator per unknown id."""
    cache: dict[str, str] = {}

    def resolve(evaluator_id: str) -> str:
        static = criteria_svc.static_evaluator_kind(evaluator_id)
        if static:
            return static
        if evaluator_id in cache:
            return cache[evaluator_id]
        kind = "judge"
        try:
            from app.evaluation import agentcore_eval as ac

            detail = ac.get_evaluator(control_client(ws.context), evaluator_id=evaluator_id)
            config = (detail.get("evaluator") or detail).get("evaluatorConfig") or {}
            if config.get("codeBased"):
                kind = "code"
            elif config.get("derived"):
                kind = "judge"
        except Exception:  # noqa: BLE001 - an unreadable evaluator is treated as a judge,
            kind = "judge"  # the safer assumption (a judge cannot gate until calibrated)
        cache[evaluator_id] = kind
        return kind

    return resolve


def _calibration(db: Session, ws: WorkspaceScope, agent_id: str | None, rows: list):
    policy = cal_svc.policy_of(ws.row.release_policy)
    return cal_svc.calibrated_keys(db, ws.id, agent_id, rows, policy), policy


def _set_payload(db: Session, ws: WorkspaceScope, row: CriteriaSet) -> dict[str, Any]:
    rows = criteria_svc.criteria_of(db, row.id)
    calibration, policy = _calibration(db, ws, row.agent_id, rows)
    dicts = [{"key": r.key, "tier": r.tier, "threshold": r.threshold, "executor": r.executor,
              "dimension": r.dimension} for r in rows]
    return {
        "set": criteria_svc.set_out(row),
        "criteria": [
            criterion_out
            for criterion_out in (
                {**criteria_svc.criterion_out(
                    r, calibrated=bool((calibration.get(r.key) or {}).get("calibrated"))),
                 "calibration": calibration.get(r.key)}
                for r in rows
            )
        ],
        "summary": criteria_svc.summary(
            dicts, {k for k, v in calibration.items() if v.get("calibrated")}
        ),
        "findings": criteria_svc.validate(
            [{"key": r.key, **{f: getattr(r, f) for f in (
                "text", "dimension", "tier", "threshold", "metric_rule", "level", "executor",
                "denominator", "expected_type", "pass_k", "notes", "examples")}}
             for r in rows],
            removals=row.removals or [],
            for_publish=row.kind == "agent",
        ),
        "calibration_policy": policy,
        "newer_template_version": criteria_svc.newer_template_version(db, row),
        "versions": [
            {"version": v.version, "status": v.status, "signed_by": v.signed_by,
             "published_at": v.published_at.isoformat() if v.published_at else None}
            for v in criteria_svc.versions_of(db, ws.id, row.lineage_id)
        ],
    }


# ── criteria sets ──────────────────────────────────────────────────────────────


class CriterionIn(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=2000)
    dimension: Literal["cognition", "quality", "responsibility", "cost", "performance"]
    tier: Literal["redline", "gate", "observe"]
    threshold: float | None = Field(default=None, ge=0, le=1)
    metric_rule: dict[str, Any] | None = None
    level: Literal["session", "trace", "tool_call"] = "session"
    executor: dict[str, Any] = Field(default_factory=dict)
    denominator: Literal["sessions", "turns", "fields"] = "sessions"
    expected_type: Literal[
        "deterministic", "redline", "trajectory", "compliance", "soft", "efficiency"
    ] = "deterministic"
    pass_k: dict[str, Any] | None = None
    attribution_layer: str | None = Field(default=None, max_length=4)
    owner: str = Field(default="", max_length=64)
    examples: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    notes: str = Field(default="", max_length=4000)


class CriteriaSetCreate(BaseModel):
    kind: Literal["template", "agent"] = "agent"
    name: str = Field(min_length=1, max_length=96)
    agent_id: str | None = Field(default=None, max_length=32)
    description: str = Field(default="", max_length=4000)
    scenario: str = Field(default="", max_length=96)
    template_lineage_id: str | None = Field(default=None, max_length=32)
    template_version: int | None = Field(default=None, ge=1)
    from_evaluation_plan: str | None = Field(default=None, max_length=32)


class CriteriaSetSave(BaseModel):
    criteria: list[CriterionIn] = Field(max_length=200)
    removals: list[dict[str, Any]] | None = None
    name: str | None = Field(default=None, max_length=96)
    description: str | None = Field(default=None, max_length=4000)
    scenario: str | None = Field(default=None, max_length=96)


class SignIn(BaseModel):
    note: str = Field(default="", max_length=2000)


@router.get("/criteria-sets")
def list_criteria_sets(
    kind: Literal["template", "agent"] | None = None,
    agent_id: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    rows = criteria_svc.list_lineages(db, ws.id, kind=kind, agent_id=agent_id)
    return {"sets": [criteria_svc.set_out(r) for r in rows]}


@router.post("/criteria-sets", status_code=201)
def create_criteria_set(
    req: CriteriaSetCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    actor = _actor(request)
    if req.agent_id:
        _agent(db, ws, req.agent_id)
    rows = None
    source = "manual"
    if req.from_evaluation_plan:
        from app.models.assistant import AssistantEvaluationPlan

        plan = db.get(AssistantEvaluationPlan, req.from_evaluation_plan)
        if plan is None or plan.workspace_id != ws.id:
            raise NotFoundError("criteria.plan_not_found", "evaluation plan not found")
        rows = criteria_svc.rows_from_evaluation_plan(plan.content or {})
        source = "assistant_plan"
    row = criteria_svc.create_set(
        db, ws.id, kind=req.kind, name=req.name, actor=actor, agent_id=req.agent_id,
        description=req.description, scenario=req.scenario,
        template_id=req.template_lineage_id, template_version=req.template_version,
        source=source,
        rows=[criteria_svc.normalize_criterion(r, _evaluator_kind_resolver(ws)) for r in rows]
        if rows else None,
    )
    db.commit()
    return _set_payload(db, ws, row)


@router.get("/criteria-sets/{lineage_id}")
def get_criteria_set(
    lineage_id: str,
    version: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _set_payload(db, ws, criteria_svc.get_version(db, ws.id, lineage_id, version))


@router.put("/criteria-sets/{lineage_id}")
def save_criteria_set(
    lineage_id: str,
    req: CriteriaSetSave,
    request: Request,
    version: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = criteria_svc.get_version(db, ws.id, lineage_id, version)
    criteria_svc.save_draft(
        db, row, [r.model_dump() for r in req.criteria], actor=_actor(request),
        kind_of=_evaluator_kind_resolver(ws), removals=req.removals, name=req.name,
        description=req.description, scenario=req.scenario,
    )
    db.commit()
    return _set_payload(db, ws, row)


@router.post("/criteria-sets/{lineage_id}/versions", status_code=201)
def new_criteria_version(
    lineage_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = criteria_svc.new_version(db, ws.id, lineage_id, actor=_actor(request))
    db.commit()
    return _set_payload(db, ws, row)


@router.post("/criteria-sets/{lineage_id}/publish")
def publish_criteria_set(
    lineage_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = criteria_svc.get_version(db, ws.id, lineage_id)
    criteria_svc.publish(db, row, actor=_actor(request))
    db.commit()
    return _set_payload(db, ws, row)


@router.post("/criteria-sets/{lineage_id}/sign")
def sign_criteria_set(
    lineage_id: str,
    req: SignIn,
    request: Request,
    version: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = criteria_svc.get_version(db, ws.id, lineage_id, version)
    criteria_svc.sign(db, row, actor=_actor(request), note=req.note,
                      is_admin=_bypasses(request))
    from app.services.audit import record_audit_event

    record_audit_event(workspace_id=ws.id, actor=_actor(request), action="criteria.sign",
                       target=f"{lineage_id}:v{row.version}", db=db)
    db.commit()
    return _set_payload(db, ws, row)


@router.post("/criteria-sets/{lineage_id}/adopt-template", status_code=201)
def adopt_template(
    lineage_id: str,
    request: Request,
    template_version: int = Query(ge=1),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = criteria_svc.adopt_template(db, ws.id, lineage_id,
                                      template_version=template_version,
                                      actor=_actor(request))
    db.commit()
    return _set_payload(db, ws, row)


@router.delete("/criteria-sets/{lineage_id}")
def discard_criteria_draft(
    lineage_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = criteria_svc.get_version(db, ws.id, lineage_id)
    version = row.version
    criteria_svc.discard_draft(db, row)
    db.commit()
    return {"discarded": version}


@router.get("/criteria-sets/{lineage_id}/diff")
def diff_criteria_versions(
    lineage_id: str,
    a: int = Query(ge=1),
    b: int = Query(ge=1),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    left = criteria_svc.get_version(db, ws.id, lineage_id, a)
    right = criteria_svc.get_version(db, ws.id, lineage_id, b)
    return {
        "from": criteria_svc.set_out(left),
        "to": criteria_svc.set_out(right),
        "changes": criteria_svc.diff(criteria_svc.criteria_of(db, left.id),
                                     criteria_svc.criteria_of(db, right.id)),
    }


# ── golden sets ────────────────────────────────────────────────────────────────


class GoldenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    criteria_lineage_id: str | None = Field(default=None, max_length=32)
    description: str = Field(default="", max_length=4000)


class GoldenItemsIn(BaseModel):
    split: Literal["dev", "regression"] = "dev"
    items: list[dict[str, Any]] = Field(min_length=1, max_length=200)


class GoldenSeedIn(BaseModel):
    """Initial curation. Each item may carry `split`; the rest are stratified."""

    items: list[dict[str, Any]] = Field(min_length=1, max_length=600)
    shares: tuple[float, float, float] = (0.5, 0.3, 0.2)


class GoldenMoveIn(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=128)
    to: Literal["dev", "regression"]


class GoldenRetireIn(BaseModel):
    split: Literal["dev", "regression", "holdout"]
    scenario_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


def _golden_payload(db: Session, ws: WorkspaceScope, parent: EvalDataset) -> dict[str, Any]:
    out = golden_svc.parent_out(db, parent)
    keys: list[str] = []
    if parent.criteria_set_id:
        published = criteria_svc.latest_published(db, ws.id, parent.criteria_set_id)
        if published:
            keys = [r.key for r in criteria_svc.criteria_of(db, published.id)]
    out["coverage"] = golden_svc.coverage(keys, golden_svc.splits_of(db, parent))
    return out


@router.get("/golden-sets")
def list_golden_sets(
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    return {"golden_sets": [golden_svc.parent_out(db, p)
                            for p in golden_svc.list_parents(db, ws.id)]}


@router.post("/golden-sets", status_code=201)
def create_golden_set(
    req: GoldenCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    parent = golden_svc.create(db, ws.id, name=req.name,
                               criteria_lineage_id=req.criteria_lineage_id,
                               actor=_actor(request), description=req.description)
    db.commit()
    return _golden_payload(db, ws, parent)


@router.get("/golden-sets/{dataset_id}")
def get_golden_set(
    dataset_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _golden_payload(db, ws, golden_svc.get_parent(db, ws.id, dataset_id))


@router.post("/golden-sets/{dataset_id}/items", status_code=201)
def add_golden_items(
    dataset_id: str,
    req: GoldenItemsIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    parent = golden_svc.get_parent(db, ws.id, dataset_id)
    split = golden_svc.splits_of(db, parent).get(req.split)
    if split is None:
        raise NotFoundError("golden.split_not_found", "split not found")
    added = golden_svc.add_items(db, split, req.items, actor=_actor(request))
    db.commit()
    return {"added": len(added), "golden_set": _golden_payload(db, ws, parent)}


@router.post("/golden-sets/{dataset_id}/seed", status_code=201)
def seed_golden_set(
    dataset_id: str,
    req: GoldenSeedIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Curate all three splits once — the only path that writes the holdout."""
    parent = golden_svc.get_parent(db, ws.id, dataset_id)
    from app.services.audit import record_audit_event

    counts = golden_svc.seed(db, parent, req.items, actor=_actor(request),
                             shares=req.shares)
    record_audit_event(workspace_id=ws.id, actor=_actor(request), action="golden.seed",
                       target=parent.id, db=db)
    db.commit()
    return {"counts": counts, "golden_set": _golden_payload(db, ws, parent)}


@router.post("/golden-sets/{dataset_id}/move")
def move_golden_item(
    dataset_id: str,
    req: GoldenMoveIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    parent = golden_svc.get_parent(db, ws.id, dataset_id)
    golden_svc.move_item(db, parent, req.scenario_id, to=req.to, actor=_actor(request))
    db.commit()
    return _golden_payload(db, ws, parent)


@router.post("/golden-sets/{dataset_id}/retire")
def retire_golden_item(
    dataset_id: str,
    req: GoldenRetireIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    parent = golden_svc.get_parent(db, ws.id, dataset_id)
    split = golden_svc.splits_of(db, parent).get(req.split)
    if split is None:
        raise NotFoundError("golden.split_not_found", "split not found")
    golden_svc.retire_item(db, split, req.scenario_id, actor=_actor(request),
                           reason=req.reason)
    db.commit()
    return _golden_payload(db, ws, parent)


# ── annotation + calibration ───────────────────────────────────────────────────


class TaskCreate(BaseModel):
    agent_id: str | None = Field(default=None, max_length=32)
    criteria_lineage_id: str | None = Field(default=None, max_length=32)
    criterion_key: str = Field(min_length=1, max_length=32)
    purpose: Literal["judge_calibration", "golden_answer", "admission"] = "judge_calibration"
    annotators: list[str] = Field(min_length=2, max_length=10)
    adjudicator: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=32)
    dataset_id: str | None = Field(default=None, max_length=32)
    items: list[dict[str, Any]] | None = Field(default=None, max_length=200)


class LabelIn(BaseModel):
    item_ref: str = Field(min_length=1, max_length=160)
    label: str = Field(default="", max_length=32)
    answer: str = Field(default="", max_length=16000)
    rationale: str = Field(default="", max_length=4000)


class CalibrationDecision(BaseModel):
    verdict: Literal["aligned", "not_aligned"]
    note: str = Field(default="", max_length=2000)


def _scenario_texts(db: Session, run: EvalRun) -> dict[str, tuple[str, str]]:
    """scenario_id -> (first user input, expected answer) from the run's dataset."""
    dataset = db.get(EvalDataset, run.dataset_id) if run.dataset_id else None
    if dataset is None or dataset.workspace_id != run.workspace_id:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for item in dataset.items or []:
        turns = item.get("turns") or []
        question = turns[0].get("input") if turns else None
        expected = next((t.get("expected_response") for t in reversed(turns)
                         if t.get("expected_response")), None)
        out[str(item.get("scenario_id") or "")] = (
            question if isinstance(question, str) else "", expected or "")
    return out


def _agent_reply(db: Session, ws: WorkspaceScope, run: EvalRun, session_id: str) -> str:
    """What the agent actually said in that session, from the memory transcript."""
    from app.services import observability

    try:
        turns = observability.get_session_transcript(
            session_id, db, ws.context, run.agent_id or None,
        )["transcript"].get("turns") or []
    except Exception:  # noqa: BLE001 - a missing transcript must not block the task
        return ""
    return "\n".join(t.get("text") or "" for t in turns
                     if str(t.get("role")).upper() == "ASSISTANT").strip()


def _items_from_run(
    db: Session, ws: WorkspaceScope, run: EvalRun, criterion_key: str,
) -> list[dict[str, Any]]:
    """Calibration items: the run's own sessions with the judge's verdict attached.

    `input` / `expected` / `answer` are what an annotator needs to label blind: the
    user's question, the reference answer and the agent's actual reply. `answer`
    used to carry the judge's explanation, which showed annotators the judge's
    reasoning during blind labelling and left them without the reply itself; the
    explanation now stays in `judge_explanation`, hidden until the task closes.
    """
    rows = engine_svc.results_for(db, run.id, criterion_key)
    texts = _scenario_texts(db, run)
    # One item per replayed session (scenario + attempt): a trace-level judge scores
    # every trace, and a session can hold more than one (live 2026-10-13: a replay
    # the runtime answered twice gave kg-… two S2 verdicts, two items with one ref,
    # and the task was refused with "every item needs a unique ref"). The session's
    # verdict collapses its traces the way the gate does (`engine.collapse`).
    grouped: dict[tuple[str, int], list[Any]] = {}
    for row in rows:
        grouped.setdefault((row.scenario_id, row.attempt), []).append(row)
    replies: dict[str, str] = {}
    out = []
    for (scenario_id, attempt), group in grouped.items():
        first = group[0]
        question, expected = texts.get(scenario_id, ("", ""))
        if first.session_id and first.session_id not in replies:
            replies[first.session_id] = _agent_reply(db, ws, run, first.session_id)
        verdict = engine_svc.collapse([r.verdict for r in group])
        explanation = "\n\n".join(dict.fromkeys(r.explanation for r in group if r.explanation))
        out.append({
            "ref": f"{scenario_id}#{attempt}",
            "session_id": first.session_id,
            "scenario_id": scenario_id,
            "input": question or scenario_id,
            "expected": expected[:2000],
            "answer": replies.get(first.session_id or "", "")[:4000],
            "judge_label": verdict if verdict in ("pass", "fail") else "inconclusive",
            "judge_explanation": explanation[:2000],
        })
    return out


@router.get("/annotation-tasks")
def list_annotation_tasks(
    request: Request,
    agent_id: str | None = Query(default=None, max_length=32),
    status: str | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    from app.models.dlc import AnnotationTask

    query = select(AnnotationTask).where(AnnotationTask.workspace_id == ws.id)
    if agent_id:
        query = query.where(AnnotationTask.agent_id == agent_id)
    if status:
        query = query.where(AnnotationTask.status == status)
    rows = db.scalars(query.order_by(AnnotationTask.created_at.desc()).limit(100)).all()
    identity = require_identity(request)
    # the same test as the detail route: hardcoding `privileged` here handed every
    # annotator the judge's verdicts in the list, which is exactly what blind labelling
    # exists to withhold — an annotator who sees them first makes κ meaningless
    privileged = _sees_verdicts(request)
    return {"tasks": [
        cal_svc.task_view(db, t, viewer=identity.username, privileged=privileged)
        for t in rows
    ]}


@router.post("/annotation-tasks", status_code=201)
def create_annotation_task(
    req: TaskCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    # the task is stamped from its agent: an unchecked id would file it into another
    # project — written where its creator cannot see it
    if req.agent_id:
        _agent(db, ws, req.agent_id)
    if req.dataset_id:
        dataset = owned_get(db, EvalDataset, req.dataset_id)
        if dataset is None or dataset.workspace_id != ws.id:
            raise NotFoundError("dataset.not_found", "dataset not found")
    items = req.items
    if items is None:
        if not req.run_id:
            raise AppError("annotation.no_items",
                           "supply items, or a run_id to take them from")
        run = db.get(EvalRun, req.run_id)
        if run is None or run.workspace_id != ws.id:
            raise NotFoundError("run.not_found", "run not found")
        if req.agent_id and run.agent_id != req.agent_id:
            raise AppError("annotation.run_mismatch",
                           "that run evaluated a different agent", status_code=422)
        items = _items_from_run(db, ws, run, req.criterion_key)
        if not items:
            raise AppError("annotation.no_items",
                           "that run has no verdicts for this criterion")
    cset = (
        criteria_svc.latest_published(db, ws.id, req.criteria_lineage_id)
        if req.criteria_lineage_id else None
    )
    task = cal_svc.create_task(
        db, workspace_id=ws.id, agent_id=req.agent_id,
        criteria_set_id=cset.id if cset else None, criterion_key=req.criterion_key,
        purpose=req.purpose, items=items, annotators=req.annotators,
        adjudicator=req.adjudicator, actor=_actor(request), run_id=req.run_id,
        dataset_id=req.dataset_id,
    )
    db.commit()
    return cal_svc.task_view(db, task, viewer=_actor(request), privileged=True)


@router.get("/annotation-tasks/{task_id}")
def get_annotation_task(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    task = cal_svc.get_task(db, ws.id, task_id)
    if _writable(request):
        _backfill_answers(db, ws, task)
    identity = require_identity(request)
    privileged = _sees_verdicts(request)
    return cal_svc.task_view(db, task, viewer=identity.username, privileged=privileged)


def _backfill_answers(db: Session, ws: WorkspaceScope, task: Any) -> None:
    """Fill in the agent's reply on items taken from a run before the reader could find it.

    Tasks created while the transcript reader still looked under the bare "default"
    actor froze an empty `answer` for every per-session-actor replay. The reply is a
    fact about the session, not a label, so re-reading it is safe at any status; each
    item is tried once (`answer_checked`) so a session with no transcript does not
    cost a memory read on every view."""
    if not task.run_id:
        return
    pending = [item for item in task.items or []
               if item.get("session_id") and not item.get("answer")
               and not item.get("answer_checked")]
    if not pending:
        return
    run = db.get(EvalRun, task.run_id)
    if run is None or run.workspace_id != ws.id:
        return
    items = []
    for item in task.items or []:
        if item in pending:
            item = {**item, "answer": _agent_reply(db, ws, run, item["session_id"])[:4000],
                    "answer_checked": True}
        items.append(item)
    task.items = items
    db.commit()


@router.post("/annotation-tasks/{task_id}/labels", status_code=201)
def record_annotation(
    task_id: str,
    req: LabelIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    task = cal_svc.get_task(db, ws.id, task_id)
    cal_svc.record_label(db, task, annotator=_actor(request), item_ref=req.item_ref,
                         label=req.label, rationale=req.rationale, answer=req.answer)
    db.commit()
    return cal_svc.task_view(db, task, viewer=_actor(request), privileged=False)


@router.post("/annotation-tasks/{task_id}/adjudicate")
def start_adjudication(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    task = cal_svc.get_task(db, ws.id, task_id)
    cal_svc.start_adjudication(db, task)
    db.commit()
    return cal_svc.task_view(db, task, viewer=_actor(request), privileged=True)


@router.get("/annotation-tasks/{task_id}/agreement")
def task_agreement(
    task_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    task = cal_svc.get_task(db, ws.id, task_id)
    measured = cal_svc.agreement(db, task)
    policy = cal_svc.policy_of(ws.row.release_policy)
    return {
        "task_id": task.id,
        "criterion_key": task.criterion_key,
        **measured,
        "policy": policy,
        "suggested_verdict": cal_svc.suggested_verdict(measured, policy),
    }


@router.post("/annotation-tasks/{task_id}/decide")
def decide_calibration(
    task_id: str,
    req: CalibrationDecision,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    task = cal_svc.get_task(db, ws.id, task_id)
    evaluator_id = ""
    lineage_id, version = None, None
    if task.criteria_set_id:
        cset = db.get(CriteriaSet, task.criteria_set_id)
        if cset is not None:
            lineage_id, version = cset.lineage_id, cset.version
            row = next((c for c in criteria_svc.criteria_of(db, cset.id)
                        if c.key == task.criterion_key), None)
            evaluator_id = ((row.executor or {}) if row else {}).get("evaluator_id") or ""
    record = cal_svc.decide(
        db, task, verdict=req.verdict, actor=_actor(request),
        policy=cal_svc.policy_of(ws.row.release_policy), evaluator_id=evaluator_id,
        evaluator_updated_at=None, criteria_lineage_id=lineage_id,
        criteria_set_version=version, note=req.note,
    )
    from app.services.audit import record_audit_event

    record_audit_event(workspace_id=ws.id, actor=_actor(request),
                       action=f"calibration.{record.verdict}", target=task.id, db=db)
    db.commit()
    return cal_svc.record_out(record)


@router.get("/calibration/{criterion_key}")
def calibration_history(
    criterion_key: str,
    agent_id: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    rows = cal_svc.history(db, ws.id, agent_id, criterion_key)
    policy = cal_svc.policy_of(ws.row.release_policy)
    return {
        "criterion_key": criterion_key,
        "records": [cal_svc.record_out(r) for r in rows],
        "status": cal_svc.status_of(rows[0] if rows else None, policy),
        "policy": policy,
    }


# ── releases ───────────────────────────────────────────────────────────────────


class ReleaseEvaluateIn(BaseModel):
    repeats: int = Field(default=1, ge=1, le=10)
    confirm_cost: bool = False


class ReleaseDecisionIn(BaseModel):
    note: str = Field(default="", max_length=2000)


class WaiverIn(BaseModel):
    criterion_key: str = Field(min_length=1, max_length=32)
    actual: float | None = None
    threshold: float | None = None
    reason: str = Field(min_length=1, max_length=2000)
    risk_owner: str = Field(min_length=1, max_length=64)
    compensating_control: str = Field(default="", max_length=2000)
    expires_on: datetime


class WaiverDecisionIn(BaseModel):
    note: str = Field(default="", max_length=2000)


def _release_payload(db: Session, ws: WorkspaceScope, agent: Agent) -> dict[str, Any]:
    control = None
    if agent.endpoint_mode == "live":
        try:
            control = control_client(ws.context)
        except Exception:  # noqa: BLE001 - the state view degrades, it never 500s
            control = None
    pending = release_svc.pending_for(db, ws.id, agent.id)
    waivers = db.scalars(
        select(Waiver).where(Waiver.workspace_id == ws.id, Waiver.agent_id == agent.id)
        .order_by(Waiver.created_at.desc())
    ).all()
    counts: dict[str, int] = {}
    for w in waivers:
        counts[w.criterion_key] = counts.get(w.criterion_key, 0) + 1
    cset = criteria_svc.agent_set(db, ws.id, agent.id)
    return {
        "agent_id": agent.id,
        "release_mode": release_svc.release_mode(ws.row),
        "state": release_svc.live_state(control, agent),
        "criteria_set": criteria_svc.set_out(cset) if cset else None,
        "pending": release_svc.record_out(pending) if pending else None,
        "records": [release_svc.record_out(r)
                    for r in release_svc.records_for(db, ws.id, agent.id)],
        "waivers": [release_svc.waiver_out(w, history=counts.get(w.criterion_key))
                    for w in waivers],
    }


@router.get("/agents/{agent_id}/release")
def get_release(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _release_payload(db, ws, _agent(db, ws, agent_id))


@router.post("/agents/{agent_id}/release/migrate")
def migrate_to_live(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent(db, ws, agent_id)
    release_svc.migrate_agent(db, agent, control_client(ws.context), actor=_actor(request))
    db.commit()
    return _release_payload(db, ws, agent)


@router.post("/agents/{agent_id}/release/evaluate", status_code=202)
def evaluate_release(
    agent_id: str,
    req: ReleaseEvaluateIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent(db, ws, agent_id)
    record = release_svc.pending_for(db, ws.id, agent.id)
    if record is None:
        raise AppError("release.nothing_pending",
                       "no candidate version is waiting for a gate decision",
                       status_code=409)
    release_svc.start_evaluation(db, record, agent, ws.context, actor=_actor(request),
                                 repeats=req.repeats, confirm_cost=req.confirm_cost,
                                 is_admin=_bypasses(request))
    db.commit()
    return _release_payload(db, ws, agent)


@router.get("/agents/{agent_id}/release/gate")
def release_gate(
    request: Request,
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent(db, ws, agent_id)
    record = release_svc.pending_for(db, ws.id, agent.id)
    if record is None:
        raise NotFoundError("release.nothing_pending", "no release is open for this agent")
    out = release_svc.evaluate(db, record)
    if _writable(request):
        db.commit()
    else:
        db.rollback()  # the report is still answered; an archived project keeps its row
    return {**out, "record": release_svc.record_out(record)}


@router.post("/agents/{agent_id}/release/sign")
def sign_release(
    agent_id: str,
    req: ReleaseDecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent(db, ws, agent_id)
    record = release_svc.pending_for(db, ws.id, agent.id)
    if record is None:
        raise NotFoundError("release.nothing_pending", "no release is open for this agent")
    release_svc.sign(db, record, agent, control_client(ws.context), actor=_actor(request),
                     note=req.note)
    db.commit()
    return _release_payload(db, ws, agent)


@router.post("/agents/{agent_id}/release/block")
def block_release(
    agent_id: str,
    req: ReleaseDecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent(db, ws, agent_id)
    record = release_svc.pending_for(db, ws.id, agent.id)
    if record is None:
        raise NotFoundError("release.nothing_pending", "no release is open for this agent")
    release_svc.block(db, record, actor=_actor(request), note=req.note)
    db.commit()
    return _release_payload(db, ws, agent)


@router.post("/agents/{agent_id}/release/rollback")
def rollback_release(
    agent_id: str,
    req: ReleaseDecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent(db, ws, agent_id)
    out = release_svc.rollback(db, agent, control_client(ws.context), actor=_actor(request),
                               note=req.note)
    db.commit()
    return {**out, **_release_payload(db, ws, agent)}


@router.get("/release-records")
def list_release_records(
    agent_id: str = Query(max_length=32),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"records": [release_svc.record_out(r)
                        for r in release_svc.records_for(db, ws.id, agent_id)]}


@router.get("/release-records/{record_id}")
def get_release_record(
    record_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return release_svc.record_out(release_svc.get_record(db, ws.id, record_id))


@router.post("/agents/{agent_id}/waivers", status_code=201)
def request_waiver(
    agent_id: str,
    req: WaiverIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent(db, ws, agent_id)
    waiver = release_svc.request_waiver(
        db, workspace_id=ws.id, agent_id=agent_id, criterion_key=req.criterion_key,
        actual=req.actual, threshold=req.threshold, reason=req.reason,
        risk_owner=req.risk_owner, compensating_control=req.compensating_control,
        expires_on=req.expires_on, actor=_actor(request),
    )
    db.commit()
    return release_svc.waiver_out(waiver)


def _waiver(db: Session, ws: WorkspaceScope, waiver_id: str) -> Waiver:
    row = db.get(Waiver, waiver_id)
    if row is None or row.workspace_id != ws.id:
        raise NotFoundError("waiver.not_found", "waiver not found")
    return row


@router.post("/waivers/{waiver_id}/approve")
def approve_waiver(
    waiver_id: str,
    req: WaiverDecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    waiver = release_svc.decide_waiver(db, _waiver(db, ws, waiver_id), actor=_actor(request),
                                       approve=True, note=req.note)
    db.commit()
    return release_svc.waiver_out(waiver)


@router.post("/waivers/{waiver_id}/reject")
def reject_waiver(
    waiver_id: str,
    req: WaiverDecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    waiver = release_svc.decide_waiver(db, _waiver(db, ws, waiver_id), actor=_actor(request),
                                       approve=False, note=req.note)
    db.commit()
    return release_svc.waiver_out(waiver)


@router.delete("/waivers/{waiver_id}")
def revoke_waiver(
    waiver_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    waiver = release_svc.revoke_waiver(db, _waiver(db, ws, waiver_id), actor=_actor(request))
    db.commit()
    return release_svc.waiver_out(waiver)


# ── admission ──────────────────────────────────────────────────────────────────


class AdmitIn(BaseModel):
    split: Literal["dev", "regression"] = "dev"
    dataset_id: str | None = Field(default=None, max_length=32)
    expected_response: str = Field(min_length=1, max_length=16000)
    expected_source: Literal["annotator", "consensus", "adjudicated"] = "annotator"
    criteria_ids: list[str] = Field(default_factory=list, max_length=40)
    case_tier: Literal["known_good", "known_bad", "ambiguous", "adversarial"] = "known_bad"
    scenario_id: str | None = Field(default=None, max_length=128)


class RejectIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class DuplicateIn(BaseModel):
    duplicate_of: str = Field(min_length=1, max_length=160)


@router.get("/admission")
def admission_queue(
    request: Request,
    agent_id: str | None = Query(default=None, max_length=32),
    status: str | None = "new",
    refresh: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    if refresh and agent_id and _writable(request):
        _agent(db, ws, agent_id)  # candidates are stamped from it
        admission_svc.collect(db, ws.id, agent_id=agent_id)
        db.commit()
    return admission_svc.queue(db, ws.id, agent_id=agent_id, status=status)


@router.get("/admission/{candidate_id}")
def admission_candidate(
    request: Request,
    candidate_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    candidate = admission_svc.get_candidate(db, ws.id, candidate_id)
    agent = db.get(Agent, candidate.agent_id)
    if not candidate.redaction:
        redaction = admission_svc.redaction_preview(
            candidate.question, candidate.answer, agent, ws.context
        )
        if _writable(request):
            candidate.redaction = redaction
            db.commit()
        else:
            return {
                **admission_svc.candidate_out(candidate), "redaction": redaction,
                "nearest": admission_svc.nearest_items(db, ws.id, candidate.question,
                                                       agent_id=candidate.agent_id),
            }
    return {
        **admission_svc.candidate_out(candidate),
        "nearest": admission_svc.nearest_items(db, ws.id, candidate.question,
                                               agent_id=candidate.agent_id),
    }


@router.post("/admission/{candidate_id}/admit", status_code=201)
def admit_candidate(
    candidate_id: str,
    req: AdmitIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    candidate = admission_svc.get_candidate(db, ws.id, candidate_id)
    if req.dataset_id:
        parent = golden_svc.get_parent(db, ws.id, req.dataset_id)
    else:
        cset = criteria_svc.agent_set(db, ws.id, candidate.agent_id)
        parent = golden_svc.for_criteria(db, ws.id, cset.lineage_id) if cset else None
    if parent is None:
        raise AppError("admission.no_golden_set",
                       "create the golden set for this agent's criteria first",
                       status_code=409)
    split = golden_svc.splits_of(db, parent).get(req.split)
    if split is None:
        raise NotFoundError("golden.split_not_found", "split not found")
    item = admission_svc.admit(
        db, candidate, split_dataset=split, expected_response=req.expected_response,
        expected_source=req.expected_source, criteria_ids=req.criteria_ids,
        case_tier=req.case_tier, actor=_actor(request), workspace_ctx=ws.context,
        scenario_id=req.scenario_id,
    )
    db.commit()
    return {"item": item, "candidate": admission_svc.candidate_out(candidate)}


@router.post("/admission/{candidate_id}/reject")
def reject_candidate(
    candidate_id: str,
    req: RejectIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    candidate = admission_svc.get_candidate(db, ws.id, candidate_id)
    admission_svc.reject(db, candidate, actor=_actor(request), note=req.note)
    db.commit()
    return admission_svc.candidate_out(candidate)


@router.post("/admission/{candidate_id}/duplicate")
def mark_candidate_duplicate(
    candidate_id: str,
    req: DuplicateIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    candidate = admission_svc.get_candidate(db, ws.id, candidate_id)
    admission_svc.mark_duplicate(db, candidate, of=req.duplicate_of, actor=_actor(request))
    db.commit()
    return admission_svc.candidate_out(candidate)


# ── watch ──────────────────────────────────────────────────────────────────────


class WatchIn(BaseModel):
    criteria_lineage_id: str | None = Field(default=None, max_length=32)
    dataset_id: str | None = Field(default=None, max_length=32)
    every: Literal["daily", "weekly"] = "daily"
    at_hour: int = Field(default=3, ge=0, le=23)
    tz: str = Field(default="UTC", max_length=48)
    repeats: int = Field(default=1, ge=1, le=10)
    max_cost_usd: float | None = Field(default=None, ge=0)
    enabled: bool = True


@router.get("/agents/{agent_id}/watch")
def get_watch(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent(db, ws, agent_id)
    return watch_svc.view(db, ws.id, agent_id)


@router.put("/agents/{agent_id}/watch")
def put_watch(
    agent_id: str,
    req: WatchIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent(db, ws, agent_id)
    cset = (
        criteria_svc.latest_published(db, ws.id, req.criteria_lineage_id)
        if req.criteria_lineage_id else criteria_svc.agent_set(db, ws.id, agent_id)
    )
    # resolve the dataset in THIS workspace: stored unchecked, another workspace's
    # golden parent would be replayed here (its sealed holdout against an agent the
    # caller controls) every time the scheduler fires
    dataset_id = None
    if req.dataset_id:
        dataset_id = golden_svc.get_parent(db, ws.id, req.dataset_id).id
    watch_svc.upsert(
        db, workspace_id=ws.id, agent_id=agent_id,
        criteria_set_id=cset.id if cset else None, dataset_id=dataset_id,
        every=req.every, at_hour=req.at_hour, tz=req.tz, repeats=req.repeats,
        enabled=req.enabled, actor=_actor(request), max_cost_usd=req.max_cost_usd,
    )
    db.commit()
    return watch_svc.view(db, ws.id, agent_id)


@router.post("/agents/{agent_id}/watch/run", status_code=202)
def run_watch_now(
    agent_id: str,
    request: Request,
    split: Literal["regression", "holdout"] = "regression",
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent(db, ws, agent_id)
    config = watch_svc.get_for_agent(db, ws.id, agent_id)
    if config is None:
        raise NotFoundError("watch.not_configured", "configure the watch first")
    run_id = watch_svc.run_now(db, config, workspace_ctx=ws.context, actor=_actor(request),
                               split=split)
    db.commit()
    return {"run_id": run_id, "watch": watch_svc.config_out(config)}


# ── runs: criteria results, comparison, cost ───────────────────────────────────


@router.get("/eval/runs/{run_id}/criteria")
def run_criteria(
    run_id: str,
    criterion_key: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    run = db.get(EvalRun, run_id)
    if run is None or run.workspace_id != ws.id:
        raise NotFoundError("run.not_found", "run not found")
    rows = engine_svc.results_for(db, run_id, criterion_key)
    return {
        "run_id": run.id,
        "criteria_set_id": run.criteria_set_id,
        "criteria_set_version": run.criteria_set_version,
        "split": run.split,
        "repeats": run.repeats,
        "agent_version": run.agent_version,
        "endpoint_qualifier": run.endpoint_qualifier,
        "summary": (run.criteria_summary or {}).get("criteria") or {},
        "denominator": run.denominator or {},
        "cost_estimate": run.cost_estimate or {},
        "cost_actual": run.cost_actual or {},
        "results": [engine_svc.result_out(r) for r in rows[:2000]],
        "truncated": len(rows) > 2000,
    }


@router.post("/eval/runs/{run_id}/criteria/snapshot", status_code=202)
def snapshot_run_criteria(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Re-read the results stream for a completed run (a retry after a transient read)."""
    run = db.get(EvalRun, run_id)
    if run is None or run.workspace_id != ws.id:
        raise NotFoundError("run.not_found", "run not found")
    if run.status != "completed" or not run.criteria_set_id:
        raise AppError("run.not_snapshotable",
                       "only a completed run tied to a criteria set can be snapshotted",
                       status_code=409)
    out = engine_svc.finalize_run(run_id, ws.context)
    return {"snapshotted": bool(out), "summary": (out or {}).get("criteria") or {}}


def _compare_runs(
    db: Session, ws: WorkspaceScope, viewer: Viewer, ids: list[str],
) -> dict[str, Any]:
    """The before/after comparison of ``ids`` (oldest first) — shared by the compare
    and fix-ladder routes. A plain function on purpose: a route calling another route
    function hands it its `Depends(...)`/`Query(...)` defaults as objects."""
    from app.models.ledger import SpecSnapshot

    specs: dict[str, dict] = {}
    for run_id in ids:
        run = db.get(EvalRun, run_id)
        if (
            run is None
            or run.workspace_id != ws.id
            or not visibility.sees(viewer, db, run.created_by)
        ):
            raise NotFoundError("run.not_found", f"run {run_id} not found")
        snapshot = db.scalars(
            select(SpecSnapshot).where(
                SpecSnapshot.agent_id == run.agent_id,
                SpecSnapshot.aws_version == run.agent_version,
            ).order_by(SpecSnapshot.seq.desc())
        ).first()
        if snapshot is not None:
            specs[run_id] = snapshot.spec or {}
    return compare_svc.compare(db, ws.id, ids, specs=specs)


@router.get("/eval/runs/compare")
def compare_runs(
    runs: str = Query(description="comma-separated run ids, oldest first"),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    ids = [r.strip() for r in runs.split(",") if r.strip()]
    return _compare_runs(db, ws, viewer, ids)


@router.get("/agents/{agent_id}/ladder")
def fix_ladder(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _agent(db, ws, agent_id)
    # the ladder is the viewer's view of the agent's runs: a run they may not see
    # drops out of it rather than turning the whole ladder into run.not_found
    ids = [
        run_id for run_id in compare_svc.ladder(db, ws.id, agent_id)
        if visibility.sees(viewer, db, db.get(EvalRun, run_id).created_by)
    ]
    if len(ids) < 2:
        # `runs` is the rung list everywhere else (the consoles draw it as such); a
        # bare id there broke the ladder chart for an agent with exactly one run
        return {"runs": [], "run_ids": ids, "comparable": False,
                "incomparable_reason": "fewer than two completed criteria runs"}
    return _compare_runs(db, ws, viewer, ids)


class EstimateIn(BaseModel):
    agent_id: str | None = Field(default=None, max_length=32)
    dataset_id: str | None = Field(default=None, max_length=32)
    items: int | None = Field(default=None, ge=0, le=1000)
    evaluators: list[str] = Field(default_factory=list, max_length=10)
    repeats: int = Field(default=1, ge=1, le=10)
    # price the agent's open release's gate evaluation (both golden splits with the
    # criteria's evaluators) — what `release/evaluate` will run and enforce
    release: bool = False


@router.post("/eval/runs/estimate")
def estimate_run(
    req: EstimateIn,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent(db, ws, req.agent_id) if req.agent_id else None
    gate_unavailable: str | None = None
    if req.release and agent is not None:
        record = release_svc.pending_for(db, ws.id, agent.id)
        if record is not None:
            try:
                return release_svc.gate_estimate(
                    db, record, agent, ws.context, repeats=req.repeats
                )
            except AppError as exc:  # no criteria / golden set yet: nothing to price
                gate_unavailable = exc.code
        else:
            gate_unavailable = "release.nothing_pending"
    items: list[dict[str, Any]] = []
    if req.dataset_id:
        dataset = db.get(EvalDataset, req.dataset_id)
        if dataset is None or dataset.workspace_id != ws.id:
            raise NotFoundError("dataset.not_found", "dataset not found")
        items = golden_svc.active_items(dataset) if dataset.role == "golden" else (
            dataset.items or []
        )
    elif req.items:
        items = [{"scenario_id": f"n{n}", "turns": [{"input": ""}]} for n in range(req.items)]
    rows = []
    if agent is not None:
        cset = criteria_svc.agent_set(db, ws.id, agent.id)
        rows = criteria_svc.criteria_of(db, cset.id) if cset else []
    out = cost_svc.estimate(
        db, agent=agent, workspace=ws.row, workspace_ctx=ws.context, items=items,
        evaluators=req.evaluators, repeats=req.repeats, criteria_rows=rows,
    )
    if gate_unavailable:
        out["gate_unavailable"] = gate_unavailable
    return out


# ── audit ──────────────────────────────────────────────────────────────────────


@router.get("/audit")
def audit_trail(
    target: str | None = Query(default=None, max_length=256),
    action: str | None = Query(default=None, max_length=160),
    project: str | None = Query(default=None, max_length=32),
    actor: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The decision history every workbench shows (append-only `audit_events`).

    Cross-project (the route is PROJECT_EXEMPT, design §5.10): `project` narrows to one
    project's events, `project=workspace` to the workspace-level ones (no project)."""
    query = select(AuditEvent).where(AuditEvent.workspace_id == ws.id)
    if target:
        query = query.where(AuditEvent.target == target)
    if action:
        query = query.where(AuditEvent.action.like(f"{action}%"))
    if project == "workspace":
        query = query.where(AuditEvent.project_id.is_(None))
    elif project:
        query = query.where(AuditEvent.project_id == project)
    if actor:
        query = query.where(AuditEvent.actor == actor)
    rows = db.scalars(query.order_by(AuditEvent.created_at.desc()).limit(limit)).all()
    return {
        "events": [
            {"id": r.id, "actor": r.actor, "action": r.action, "target": r.target,
             "project_id": r.project_id,
             "at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ]
    }


# ── scorecard ──────────────────────────────────────────────────────────────────


def _scorecard_release(db: Session, workspace_id: str, agent: Agent) -> dict[str, Any]:
    """Release tile of the scorecard, without a control-plane call.

    On live / candidate endpoints the ledger version is the newest deploy — the
    candidate while a release is open — not what serves production. The open record
    remembers the live version it was deployed over, so the tile shows that one; once
    the release is decided, the newest record does.
    """
    state = release_svc.live_state(None, agent)
    if agent.endpoint_mode != "live":
        return state
    pending = release_svc.pending_for(db, workspace_id, agent.id)
    if pending is not None and pending.previous_live_version:
        state["live_version"] = pending.previous_live_version
        state["candidate_version"] = pending.candidate_version
        return state
    # No open release: the newest record says what `live` was last pointed at. A sign
    # moves live to the candidate; a rollback record carries the version it restored on
    # `live`; a blocked record left live where it was. Without this, a release followed
    # by a rollback showed the rolled-back candidate (the ledger version) as production.
    latest = next(iter(release_svc.records_for(db, workspace_id, agent.id, limit=1)), None)
    if latest is None:
        return state
    if latest.decision == "released" or (
        latest.decision == "rolled_back" and latest.candidate_endpoint == release_svc.LIVE
    ):
        state["live_version"] = latest.candidate_version
    elif latest.previous_live_version:
        state["live_version"] = latest.previous_live_version
    return state


@router.get("/agents/{agent_id}/scorecard")
def scorecard(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Five dimensions, standard vs current, for one agent (§7.9)."""
    agent = _agent(db, ws, agent_id)
    cset = criteria_svc.agent_set(db, ws.id, agent.id)
    rows = criteria_svc.criteria_of(db, cset.id) if cset else []
    calibration, policy = _calibration(db, ws, agent.id, rows)
    runs = watch_svc.history(db, ws.id, agent.id, limit=10)
    latest = runs[-1] if runs else None
    summary = (latest.criteria_summary or {}).get("criteria") or {} if latest else {}
    series = watch_svc.dimension_series(runs)
    dimensions = []
    from app.models.dlc import DIMENSIONS

    for dimension in DIMENSIONS:
        members = [r for r in rows if r.dimension == dimension]
        entries = [summary.get(r.key) or {} for r in members]
        rates = [e.get("rate") for e in entries if e.get("rate") is not None]
        # cost / performance criteria report a measured value against a rule, not a
        # pass rate; a blank tile would read as "unmeasured" when it was measured
        metrics = [
            {"key": row.key, "metric": (row.metric_rule or {}).get("metric"),
             "op": (row.metric_rule or {}).get("op", "<="),
             "bound": (row.metric_rule or {}).get("value"),
             "value": entry.get("value"), "verdict": entry.get("verdict"),
             "n": entry.get("n")}
            for row, entry in zip(members, entries, strict=True)
            if (row.executor or {}).get("kind") == "metric"
        ]
        gates = [
            r for r in members
            if criteria_svc.effective_tier(
                r, calibrated=bool((calibration.get(r.key) or {}).get("calibrated"))
            ) == "gate"
        ]
        redlines = [r for r in members if r.tier == "redline"]
        violations = sum((summary.get(r.key) or {}).get("fail") or 0 for r in redlines)
        dimensions.append({
            "dimension": dimension,
            "criteria": len(members),
            "effective_gates": len(gates),
            "declared_gates": len([r for r in members if r.tier == "gate"]),
            "redlines": len(redlines),
            "redline_violations": violations,
            "current": (sum(rates) / len(rates)) if rates else None,
            "standard": min((r.threshold for r in gates if r.threshold is not None),
                            default=None),
            "series": series.get(dimension, []),
            "metrics": metrics,
            "not_applicable": not members,
        })
    # the last thing the gate said, which outlives the open release
    last_gate = release_svc.latest_decided(db, ws.id, agent.id)
    open_waivers = db.scalars(
        select(Waiver).where(Waiver.workspace_id == ws.id, Waiver.agent_id == agent.id,
                             Waiver.status == "approved")
    ).all()
    coverage = None
    if cset:
        parent = golden_svc.for_criteria(db, ws.id, cset.lineage_id)
        if parent is not None:
            coverage = golden_svc.coverage([r.key for r in rows],
                                           golden_svc.splits_of(db, parent))
    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "criteria_set": criteria_svc.set_out(cset) if cset else None,
        "dimensions": dimensions,
        "last_run": {"id": latest.id, "at": latest.created_at.isoformat()} if latest else None,
        "last_gate": (last_gate.gate_report or {}).get("verdict") if last_gate else None,
        "last_gate_decision": last_gate.decision if last_gate else None,
        "last_gate_at": (last_gate.gate_report or {}).get("decided_at") if last_gate else None,
        "release": _scorecard_release(db, ws.id, agent),
        "open_waivers": [release_svc.waiver_out(w) for w in open_waivers],
        "calibration_debt": [
            {"criterion_key": key, "reason": status.get("reason")}
            for key, status in calibration.items() if not status.get("calibrated")
        ],
        "calibration_policy": policy,
        "coverage": coverage,
        "alerts": watch_svc.alerts(runs),
    }


# ── annotation links: labelling without a console account (§7.4) ───────────────
#
# Console side mints and revokes; the public side lives in `routers/share_annotate.py`
# so no console session or `X-Workspace` header is ever read there.


class AnnotationLinkIn(BaseModel):
    """`label` names the person — it is what the audit trail and κ report show."""

    label: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=14, ge=1, le=90)


def _annotation_link_out(link: Any) -> dict[str, Any]:
    from app.services import annotation_links, share_links

    return {
        "id": link.id,
        "annotator": annotation_links.annotator_name(link.id),
        "label": link.label,
        "prefix": link.prefix,
        "state": share_links.link_state(link),
        "created_by": link.created_by,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "last_used_at": link.last_used_at.isoformat() if link.last_used_at else None,
        "use_count": link.use_count or 0,
    }


@router.get("/annotation-tasks/{task_id}/links")
def list_annotation_links(
    task_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    from app.models.ledger import ShareLink
    from app.services import annotation_links

    task = cal_svc.get_task(db, ws.id, task_id)
    rows = db.scalars(
        select(ShareLink).where(
            ShareLink.workspace_id == ws.id,
            ShareLink.target_id == task.id,
            ShareLink.kind == annotation_links.KIND_ANNOTATE,
        ).order_by(ShareLink.created_at.desc())
    ).all()
    allowed, reason = annotation_links.links_allowed(ws.row)
    return {
        "links": [_annotation_link_out(r) for r in rows],
        "allowed": allowed,
        "reason": reason,
    }


@router.post("/annotation-tasks/{task_id}/links", status_code=201)
def create_annotation_link(
    task_id: str,
    req: AnnotationLinkIn,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Mint a link that labels as its own annotator. Refused in a prod workspace."""
    from app.services import annotation_links

    task = cal_svc.get_task(db, ws.id, task_id)
    link, raw = annotation_links.create(
        db, task=task, workspace=ws.row, label=req.label, created_by=_actor(request),
        expires_in_days=req.expires_in_days,
    )
    db.commit()
    path = f"/r/annotate/{raw}"
    origin = (request.headers.get("origin") or "").rstrip("/")
    # the token is shown once, like an API key; revoke with DELETE on this route
    return {**_annotation_link_out(link), "token": raw, "path": path, "url": f"{origin}{path}"}


@router.delete("/annotation-tasks/{task_id}/links/{link_id}")
def revoke_annotation_link(
    task_id: str,
    link_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Revoke a link. The labels it already recorded stay — they were real votes."""
    from datetime import UTC

    from app.models.ledger import ShareLink
    from app.services import annotation_links

    cal_svc.get_task(db, ws.id, task_id)
    link = db.get(ShareLink, link_id)
    if link is None or link.workspace_id != ws.id or link.target_id != task_id:
        raise NotFoundError("share.not_found", "share link not found")
    if link.kind != annotation_links.KIND_ANNOTATE:
        raise NotFoundError("share.not_found", "share link not found")
    link.revoked_at = datetime.now(UTC)
    link.enabled = False
    db.commit()
    return _annotation_link_out(link)
