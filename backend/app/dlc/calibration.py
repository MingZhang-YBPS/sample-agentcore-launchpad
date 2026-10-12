"""Judge calibration: blind double annotation → κ → "may this judge gate?" (§4.4, §7.3).

A calibration task puts the same items in front of ≥2 annotators who cannot see each
other's labels nor the judge's. Closing it computes human–human κ (the ceiling) and
judge–human κ against the adjudicated human label. A criterion counts as calibrated
for an evaluator when its latest record is `aligned`, the judge reached
`max(kappa_floor, human_human − 0.05)`, the record is younger than the workspace's
recalibration period, and the evaluator has not changed since.

The period and the κ floor are workspace release-policy settings
(`release_policy.calibration = {period_days, kappa_floor}`), defaults 90 days / 0.61.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.evaluation import stats
from app.models.dlc import Annotation, AnnotationTask, CalibrationRecord, Criterion

DEFAULT_PERIOD_DAYS = 90
DEFAULT_KAPPA_FLOOR = 0.61
MIN_ITEMS = 10
RECOMMENDED_ITEMS = 15
LABELS = ("pass", "fail", "inconclusive")
PURPOSES = ("judge_calibration", "golden_answer", "admission")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def policy_of(release_policy: dict[str, Any] | None) -> dict[str, Any]:
    raw = (release_policy or {}).get("calibration") or {}
    period = raw.get("period_days", DEFAULT_PERIOD_DAYS)
    floor = raw.get("kappa_floor", DEFAULT_KAPPA_FLOOR)
    try:
        period = int(period)
    except (TypeError, ValueError):
        period = DEFAULT_PERIOD_DAYS
    try:
        floor = float(floor)
    except (TypeError, ValueError):
        floor = DEFAULT_KAPPA_FLOOR
    return {
        "period_days": min(max(period, 7), 365),
        "kappa_floor": min(max(floor, 0.2), 0.95),
    }


def normalize_policy(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a policy update (raises on out-of-range values instead of clamping)."""
    raw = raw or {}
    period = raw.get("period_days", DEFAULT_PERIOD_DAYS)
    floor = raw.get("kappa_floor", DEFAULT_KAPPA_FLOOR)
    if not isinstance(period, int) or not 7 <= period <= 365:
        raise AppError("calibration.bad_policy", "period_days must be 7..365")
    if not isinstance(floor, (int, float)) or not 0.2 <= float(floor) <= 0.95:
        raise AppError("calibration.bad_policy", "kappa_floor must be 0.2..0.95")
    return {"period_days": period, "kappa_floor": float(floor)}


# ── tasks ──────────────────────────────────────────────────────────────────────


def create_task(
    db: Session,
    *,
    workspace_id: str,
    agent_id: str | None,
    criteria_set_id: str | None,
    criterion_key: str,
    purpose: str,
    items: list[dict[str, Any]],
    annotators: list[str],
    adjudicator: str | None,
    actor: str,
    run_id: str | None = None,
    dataset_id: str | None = None,
) -> AnnotationTask:
    if purpose not in PURPOSES:
        raise AppError("annotation.bad_purpose", f"purpose must be one of {', '.join(PURPOSES)}")
    clean = [a.strip() for a in annotators if a and a.strip()]
    if len(set(clean)) < 2:
        raise AppError("annotation.annotators",
                       "calibration needs at least two independent annotators")
    if not items:
        raise AppError("annotation.no_items", "a task needs items to label")
    refs = [str(i.get("ref") or "") for i in items]
    if any(not r for r in refs) or len(set(refs)) != len(refs):
        raise AppError("annotation.bad_items", "every item needs a unique ref")
    task = AnnotationTask(
        workspace_id=workspace_id,
        agent_id=agent_id,
        criteria_set_id=criteria_set_id,
        criterion_key=criterion_key,
        purpose=purpose,
        items=items,
        annotators=list(dict.fromkeys(clean)),
        adjudicator=(adjudicator or "").strip() or None,
        status="labeling",
        created_by=actor,
        run_id=run_id,
        dataset_id=dataset_id,
    )
    db.add(task)
    db.flush()
    return task


def get_task(db: Session, workspace_id: str, task_id: str) -> AnnotationTask:
    task = db.get(AnnotationTask, task_id)
    if task is None or task.workspace_id != workspace_id:
        raise NotFoundError("annotation.not_found", "annotation task not found")
    return task


def labels_of(db: Session, task_id: str) -> list[Annotation]:
    return list(db.scalars(select(Annotation).where(Annotation.task_id == task_id)).all())


def task_view(db: Session, task: AnnotationTask, *, viewer: str, privileged: bool) -> dict:
    """What a viewer may see. Annotators see only their own labels and never the
    judge's until the task is closed (blind labelling)."""
    labels = labels_of(db, task.id)
    closed = task.status == "closed"
    reveal = closed or privileged
    items = []
    for item in task.items or []:
        shown = {k: v for k, v in item.items()
                 if reveal or k not in ("judge_label", "judge_explanation")}
        mine = [a for a in labels if a.item_ref == item["ref"] and a.annotator == viewer]
        shown["my_label"] = mine[0].label if mine else None
        shown["my_answer"] = mine[0].answer if mine else None
        shown["my_rationale"] = mine[0].rationale if mine else None
        if reveal:
            shown["labels"] = [
                {"annotator": a.annotator, "label": a.label, "answer": a.answer,
                 "rationale": a.rationale, "adjudicated": a.adjudicated}
                for a in labels if a.item_ref == item["ref"]
            ]
        items.append(shown)
    progress = {
        name: sum(1 for a in labels if a.annotator == name and not a.adjudicated)
        for name in task.annotators or []
    }
    return {
        "id": task.id,
        "agent_id": task.agent_id,
        "criteria_set_id": task.criteria_set_id,
        "criterion_key": task.criterion_key,
        "purpose": task.purpose,
        "status": task.status,
        "annotators": task.annotators or [],
        "adjudicator": task.adjudicator,
        "items": items,
        "progress": progress,
        "total": len(task.items or []),
        "created_by": task.created_by,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "closed_at": task.closed_at.isoformat() if task.closed_at else None,
        "run_id": task.run_id,
    }


def record_label(
    db: Session,
    task: AnnotationTask,
    *,
    annotator: str,
    item_ref: str,
    label: str,
    rationale: str = "",
    answer: str = "",
) -> Annotation:
    if task.status not in ("labeling", "adjudicating"):
        raise AppError("annotation.closed", "this task is closed", status_code=409)
    refs = {i["ref"] for i in task.items or []}
    if item_ref not in refs:
        raise NotFoundError("annotation.item_not_found", "item not in this task")
    adjudicating = task.status == "adjudicating"
    if adjudicating:
        if annotator != task.adjudicator:
            raise AppError("annotation.not_adjudicator",
                           "only the adjudicator labels while adjudicating", status_code=403)
    elif annotator not in (task.annotators or []):
        raise AppError("annotation.not_annotator", "you are not an annotator on this task",
                       status_code=403)
    if task.purpose != "golden_answer" and label not in LABELS:
        raise AppError("annotation.bad_label", f"label must be one of {', '.join(LABELS)}")
    existing = db.scalar(
        select(Annotation).where(
            Annotation.task_id == task.id,
            Annotation.item_ref == item_ref,
            Annotation.annotator == annotator,
        )
    )
    if existing is None:
        existing = Annotation(workspace_id=task.workspace_id, task_id=task.id,
                              item_ref=item_ref, annotator=annotator)
        db.add(existing)
    existing.label = label
    existing.rationale = rationale[:4000]
    existing.answer = answer[:16000]
    existing.adjudicated = adjudicating
    db.flush()
    return existing


def start_adjudication(db: Session, task: AnnotationTask) -> AnnotationTask:
    if not task.adjudicator:
        raise AppError("annotation.no_adjudicator", "name an adjudicator first")
    task.status = "adjudicating"
    db.flush()
    return task


def close_task(db: Session, task: AnnotationTask) -> AnnotationTask:
    task.status = "closed"
    task.closed_at = _now()
    db.flush()
    return task


# ── agreement ──────────────────────────────────────────────────────────────────


def agreement(db: Session, task: AnnotationTask) -> dict[str, Any]:
    """Human–human κ, adjudicated human labels, judge–human κ and the disagreements."""
    labels = labels_of(db, task.id)
    annotators = (task.annotators or [])[:2]
    by_item: dict[str, dict[str, str]] = {}
    adjudicated: dict[str, str] = {}
    for a in labels:
        if a.adjudicated:
            adjudicated[a.item_ref] = a.label
        else:
            by_item.setdefault(a.item_ref, {})[a.annotator] = a.label
    pair_a, pair_b, human, judge, disagreements = [], [], [], [], []
    for item in task.items or []:
        ref = item["ref"]
        votes = by_item.get(ref, {})
        if len(annotators) == 2 and all(n in votes for n in annotators):
            pair_a.append(votes[annotators[0]])
            pair_b.append(votes[annotators[1]])
        values = list(votes.values())
        consensus = None
        if ref in adjudicated:
            consensus = adjudicated[ref]
        elif len(values) >= 2 and len(set(values)) == 1:
            # TWO raters agreeing is a consensus; one rater is just that rater. Counting
            # a single vote let one person label a run to match the judge and certify it
            consensus = values[0]
        judge_label = item.get("judge_label")
        if consensus and judge_label in LABELS:
            human.append(consensus)
            judge.append(judge_label)
            if consensus != judge_label:
                disagreements.append({
                    "ref": ref, "human": consensus, "judge": judge_label,
                    "votes": votes, "judge_explanation": item.get("judge_explanation"),
                    "input": item.get("input"), "answer": item.get("answer"),
                })
        elif values and len(set(values)) > 1 and ref not in adjudicated:
            disagreements.append({"ref": ref, "human": None, "judge": judge_label,
                                  "votes": votes, "needs_adjudication": True,
                                  "input": item.get("input")})
    hh = stats.cohen_kappa(pair_a, pair_b) if len(pair_a) >= 2 else None
    jh = stats.cohen_kappa(judge, human) if len(human) >= 2 else None
    ci = stats.kappa_bootstrap_ci(judge, human) if len(human) >= 5 else None
    return {
        "n": len(human),
        "pairs": len(pair_a),
        "human_human_kappa": hh,
        "judge_human_kappa": jh,
        "kappa_ci": list(ci) if ci else None,
        "band": stats.kappa_band(jh),
        "human_band": stats.kappa_band(hh),
        "confusion": stats.confusion(judge, human) if human else {},
        # which outcomes the agreed human labels contain — a sample people judged all
        # one way never asked the judge to tell a pass from a fail
        "human_classes": sorted({h for h in human if h in ("pass", "fail")}),
        "disagreements": disagreements,
        "accuracy": (sum(1 for j, h in zip(judge, human, strict=True) if j == h) / len(human))
        if human else None,
    }


def suggested_verdict(stats_out: dict[str, Any], policy: dict[str, Any]) -> str:
    n = stats_out.get("n") or 0
    jh = stats_out.get("judge_human_kappa")
    hh = stats_out.get("human_human_kappa")
    if n < MIN_ITEMS or jh is None:
        return "insufficient_n"
    # A judge may only be certified against *people who agreed with each other*: with a
    # single rater there is no human ceiling to compare it to, and the question "can a
    # judge stand in for a person" has no answer yet.
    if hh is None or (stats_out.get("pairs") or 0) < MIN_ITEMS:
        return "insufficient_n"
    # Cohen's κ is 1.0 by convention when everyone used one label throughout, which says
    # nothing about whether the judge can separate a pass from a fail. Certifying on
    # such a sample would let a judge gate that was never shown a single violation.
    if "human_classes" in stats_out and len(stats_out["human_classes"]) < 2:
        return "one_class"
    return "aligned" if jh >= max(policy["kappa_floor"], hh - 0.05) else "not_aligned"


def _evidence_authors(db: Session, task: AnnotationTask) -> set[str]:
    """Everyone who could have written or settled this task's labels.

    Not just the named annotators: the adjudicator sets the consensus on disputed
    items, and whoever minted an annotation link holds that link's token — two links
    minted by one person are two "agreeing raters" that are really one. Any of them
    deciding the calibration would be certifying a judge on their own labels.
    """
    from app.models.ledger import ShareLink

    authors = {name for name in (task.annotators or []) if name}
    if task.adjudicator:
        authors.add(task.adjudicator)
    authors.update(
        row for row in db.scalars(
            select(ShareLink.created_by).where(
                ShareLink.kind == "annotate", ShareLink.target_id == task.id
            )
        ).all() if row
    )
    return authors


def decide(
    db: Session,
    task: AnnotationTask,
    *,
    verdict: str,
    actor: str,
    policy: dict[str, Any],
    evaluator_id: str,
    evaluator_updated_at: str | None,
    criteria_lineage_id: str | None,
    criteria_set_version: int | None,
    note: str = "",
) -> CalibrationRecord:
    """Record a calibration verdict. `aligned` is refused when the numbers do not
    support it — the human decides only between what the data allows."""
    if verdict not in ("aligned", "not_aligned"):
        raise AppError("calibration.bad_verdict", "verdict must be aligned or not_aligned")
    if actor in _evidence_authors(db, task):
        raise AppError(
            "calibration.own_labels",
            "you produced labels on this task — as an annotator, its adjudicator, or by "
            "issuing one of its annotation links — so you cannot also certify its judge: "
            "the labels are the evidence, and whoever wrote them does not rule on them",
            status_code=403,
        )
    measured = agreement(db, task)
    allowed = suggested_verdict(measured, policy)
    if verdict == "aligned" and allowed != "aligned":
        raise AppError(
            "calibration.not_supported",
            "the judge has not reached the required agreement — record not_aligned, or "
            "improve the criterion and label another round",
            {"suggested": allowed, "judge_human_kappa": measured["judge_human_kappa"],
             "human_human_kappa": measured["human_human_kappa"], "n": measured["n"],
             "kappa_floor": policy["kappa_floor"]},
            status_code=409,
        )
    ci = measured.get("kappa_ci") or [None, None]
    record = CalibrationRecord(
        workspace_id=task.workspace_id,
        agent_id=task.agent_id,
        criteria_lineage_id=criteria_lineage_id,
        criterion_key=task.criterion_key,
        criteria_set_version=criteria_set_version,
        evaluator_id=evaluator_id,
        evaluator_updated_at=evaluator_updated_at,
        task_id=task.id,
        n=measured["n"],
        human_human_kappa=measured["human_human_kappa"],
        judge_human_kappa=measured["judge_human_kappa"],
        kappa_ci_low=ci[0],
        kappa_ci_high=ci[1],
        confusion=measured["confusion"],
        disagreements=measured["disagreements"][:50],
        verdict=verdict if allowed != "insufficient_n" else "insufficient_n",
        decided_by=actor,
        note=note[:2000],
    )
    db.add(record)
    if task.status != "closed":
        close_task(db, task)
    db.flush()
    return record


def latest_record(
    db: Session, workspace_id: str, agent_id: str | None, criterion_key: str,
    evaluator_id: str | None = None,
) -> CalibrationRecord | None:
    query = select(CalibrationRecord).where(
        CalibrationRecord.workspace_id == workspace_id,
        CalibrationRecord.criterion_key == criterion_key,
    )
    if agent_id:
        query = query.where(CalibrationRecord.agent_id == agent_id)
    if evaluator_id:
        query = query.where(CalibrationRecord.evaluator_id == evaluator_id)
    return db.scalars(query.order_by(CalibrationRecord.decided_at.desc())).first()


def status_of(
    record: CalibrationRecord | None,
    policy: dict[str, Any],
    *,
    evaluator_updated_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """`calibrated`, plus why not when it is not (for the editor and the gate report)."""
    if record is None:
        return {"calibrated": False, "reason": "never_calibrated"}
    now = now or _now()
    if record.verdict != "aligned":
        return {"calibrated": False, "reason": record.verdict, "record_id": record.id}
    decided = _aware(record.decided_at) or now
    expires = decided + timedelta(days=policy["period_days"])
    if now > expires:
        return {"calibrated": False, "reason": "expired", "record_id": record.id,
                "expired_at": expires.isoformat()}
    if (
        evaluator_updated_at
        and record.evaluator_updated_at
        and evaluator_updated_at != record.evaluator_updated_at
    ):
        return {"calibrated": False, "reason": "evaluator_changed", "record_id": record.id}
    return {
        "calibrated": True,
        "record_id": record.id,
        "judge_human_kappa": record.judge_human_kappa,
        "expires_at": expires.isoformat(),
    }


def calibrated_keys(
    db: Session,
    workspace_id: str,
    agent_id: str | None,
    rows: list[Criterion],
    policy: dict[str, Any],
    evaluator_versions: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """criterion key → calibration status, for every judge criterion in `rows`."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        executor = row.executor or {}
        if executor.get("kind") != "evaluator" or executor.get("evaluator_kind") != "judge":
            continue
        evaluator_id = executor.get("evaluator_id") or ""
        record = latest_record(db, workspace_id, agent_id, row.key, evaluator_id)
        out[row.key] = status_of(
            record, policy,
            evaluator_updated_at=(evaluator_versions or {}).get(evaluator_id),
        )
    return out


def record_out(record: CalibrationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "criterion_key": record.criterion_key,
        "criteria_set_version": record.criteria_set_version,
        "evaluator_id": record.evaluator_id,
        "evaluator_updated_at": record.evaluator_updated_at,
        "task_id": record.task_id,
        "n": record.n,
        "human_human_kappa": record.human_human_kappa,
        "judge_human_kappa": record.judge_human_kappa,
        "kappa_ci": [record.kappa_ci_low, record.kappa_ci_high],
        "band": stats.kappa_band(record.judge_human_kappa),
        "confusion": record.confusion or {},
        "disagreements": record.disagreements or [],
        "verdict": record.verdict,
        "decided_by": record.decided_by,
        "decided_at": record.decided_at.isoformat() if record.decided_at else None,
        "note": record.note,
    }


def history(
    db: Session, workspace_id: str, agent_id: str | None, criterion_key: str
) -> list[CalibrationRecord]:
    query = select(CalibrationRecord).where(
        CalibrationRecord.workspace_id == workspace_id,
        CalibrationRecord.criterion_key == criterion_key,
    )
    if agent_id:
        query = query.where(CalibrationRecord.agent_id == agent_id)
    return list(db.scalars(query.order_by(CalibrationRecord.decided_at.desc())).all())
