"""Admission: what becomes part of the standard, decided by a person (§7.6).

Candidates are collected from everything the platform already records — thumbs-down
verdicts and SME corrections, the issue box, Insights failure clusters and intent
clusters, and online-evaluation failures — then shown with the evidence the decision
needs:

* the transcript **with a PII redaction preview** (a candidate that cannot be redacted
  cannot be admitted);
* the cluster it belongs to and how many sessions it affects (impact);
* **what the current evaluators say about it** — a candidate the judges got wrong is
  both a new case and a calibration sample, so it is ranked first;
* the nearest existing golden item (exact and normalized text), to catch duplicates.

Admission writes the dev or regression split, never the holdout, and the expected answer
must be written or confirmed by a person (`expected_source`).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.dlc import golden as golden_svc
from app.models.dlc import AdmissionCandidate
from app.models.ledger import ChatFeedback, ChatMessage
from app.models.selfservice import Issue
from app.services.audit import record_audit_event

logger = logging.getLogger(__name__)

SOURCES = ("feedback", "issue", "insight_failure", "insight_intent", "review", "manual",
           "online")
STATUSES = ("new", "annotating", "admitted", "rejected", "duplicate")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)
# a space between CJK and anything else carries no meaning for a dedupe key
_CJK_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff\uf900-\ufaff]) | (?=[\u3400-\u9fff\uf900-\ufaff])")


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_text(text: str) -> str:
    """Case, whitespace, punctuation and full/half-width folded — the dedupe key."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    collapsed = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", folded)).strip()
    return _CJK_SPACE_RE.sub("", collapsed)


def _first_input(item: dict[str, Any]) -> str:
    turns = item.get("turns") or []
    if turns:
        value = turns[0].get("input")
        if isinstance(value, dict):
            return str(value.get("prompt") or value.get("text") or "")
        return str(value or "")
    return str(item.get("input") or "")


def nearest_items(
    db: Session, workspace_id: str, question: str, *, agent_id: str | None = None
) -> list[dict[str, Any]]:
    """Golden items whose first input matches exactly or after normalization."""
    target = normalize_text(question)
    if not target:
        return []
    from app.evaluation.models import EvalDataset

    out: list[dict[str, Any]] = []
    rows = db.scalars(
        select(EvalDataset).where(
            EvalDataset.workspace_id == workspace_id, EvalDataset.role == "golden"
        )
    ).all()
    for dataset in rows:
        if not dataset.split:
            continue
        for item in dataset.items or []:
            text = _first_input(item)
            if not text:
                continue
            if text.strip() == question.strip():
                kind = "exact"
            elif normalize_text(text) == target:
                kind = "normalized"
            else:
                continue
            out.append({
                "dataset_id": dataset.id, "split": dataset.split,
                "scenario_id": item.get("scenario_id"), "input": text, "match": kind,
            })
    return out[:5]


def redaction_preview(
    question: str, answer: str, agent: Any, workspace_ctx: Any
) -> dict[str, Any]:
    """Mask PII with the workspace guardrail; a block means the item cannot be admitted."""
    from app.core.errors import AppError as _AppError
    from app.services import guardrail

    try:
        masked_q = guardrail.screen(question, source="INPUT", mode="anonymize",
                                    workspace=workspace_ctx)
        masked_a = guardrail.screen(answer, source="OUTPUT", mode="anonymize",
                                    workspace=workspace_ctx)
    except _AppError as exc:
        return {"status": "blocked", "reason": exc.code, "entities": []}
    except Exception as exc:  # noqa: BLE001 - no guardrail configured yet
        return {"status": "unavailable", "reason": f"{type(exc).__name__}", "entities": []}
    entities = sorted({*(masked_q.entities or []), *(masked_a.entities or [])})
    return {
        "status": "redacted" if entities else "clean",
        "entities": entities,
        "question": masked_q.text,
        "answer": masked_a.text,
    }


# ── collecting ─────────────────────────────────────────────────────────────────


def _upsert(
    db: Session, workspace_id: str, agent_id: str, source: str, source_ref: str,
    **fields: Any,
) -> AdmissionCandidate:
    existing = db.scalar(
        select(AdmissionCandidate).where(
            AdmissionCandidate.agent_id == agent_id,
            AdmissionCandidate.source == source,
            AdmissionCandidate.source_ref == source_ref,
        )
    )
    if existing is not None:
        for key, value in fields.items():
            if value is not None and getattr(existing, key, None) in (None, "", 0, [], {}):
                setattr(existing, key, value)
        return existing
    row = AdmissionCandidate(workspace_id=workspace_id, agent_id=agent_id, source=source,
                             source_ref=source_ref, **fields)
    db.add(row)
    db.flush()
    return row


def collect(
    db: Session, workspace_id: str, *, agent_id: str, limit: int = 50
) -> list[AdmissionCandidate]:
    """Refresh the queue from feedback, issues and the agent's latest insights."""
    found: list[AdmissionCandidate] = []
    downs = db.scalars(
        select(ChatFeedback).where(
            ChatFeedback.workspace_id == workspace_id,
            ChatFeedback.agent_id == agent_id,
            ChatFeedback.verdict == "down",
        ).order_by(ChatFeedback.updated_at.desc()).limit(limit)
    ).all()
    for row in downs:
        answer = db.get(ChatMessage, row.message_id)
        question = db.scalars(
            select(ChatMessage.text).where(
                ChatMessage.workspace_id == workspace_id,
                ChatMessage.session_id == row.session_id,
                ChatMessage.role == "user",
                ChatMessage.id < row.message_id,
            ).order_by(ChatMessage.id.desc())
        ).first()
        found.append(_upsert(
            db, workspace_id, agent_id,
            "review" if row.source == "review" else "feedback", str(row.id),
            session_id=row.session_id, question=str(question or "")[:8000],
            answer=(answer.text if answer else "")[:8000],
            fault_category="thumbs_down" if row.source != "review" else "sme_review",
            note=(row.correction or row.comment or "")[:2000],
        ))
    issues = db.scalars(
        select(Issue).where(
            Issue.workspace_id == workspace_id, Issue.agent_id == agent_id,
            Issue.status == "open",
        ).order_by(Issue.updated_at.desc()).limit(limit)
    ).all()
    for issue in issues:
        found.append(_upsert(
            db, workspace_id, agent_id, "issue", issue.id,
            session_id=issue.session_id, question=(issue.question or "")[:8000],
            answer=(issue.answer or "")[:8000], fault_category=issue.kind,
            note=(issue.correction or issue.comment or "")[:2000],
        ))
    db.flush()
    return found


def collect_from_insights(
    db: Session, workspace_id: str, agent_id: str, insights: dict[str, Any]
) -> list[AdmissionCandidate]:
    """Failure clusters and intent clusters from an Insights run, with their impact."""
    out: list[AdmissionCandidate] = []
    for failure in insights.get("failures") or []:
        for sub in failure.get("subCategories") or []:
            for cause in sub.get("rootCauses") or []:
                for session in (cause.get("affectedSessions") or [])[:5]:
                    out.append(_upsert(
                        db, workspace_id, agent_id, "insight_failure",
                        f"{cause.get('clusterId')}:{session.get('sessionId')}",
                        session_id=session.get("sessionId"),
                        cluster_id=str(cause.get("clusterId") or ""),
                        cluster_name=str(cause.get("name") or failure.get("name") or "")[:256],
                        affected_sessions=int(cause.get("affectedSessionCount") or 1),
                        fault_category=str(failure.get("name") or "")[:128],
                        note=str(cause.get("recommendation") or "")[:2000],
                    ))
    for intent in insights.get("userIntents") or []:
        for session in (intent.get("affectedSessions") or [])[:3]:
            messages = session.get("userMessages") or []
            out.append(_upsert(
                db, workspace_id, agent_id, "insight_intent",
                f"{intent.get('clusterId')}:{session.get('sessionId')}",
                session_id=session.get("sessionId"),
                cluster_id=str(intent.get("clusterId") or ""),
                cluster_name=str(intent.get("name") or "")[:256],
                affected_sessions=int(intent.get("affectedSessionCount") or 1),
                question=str(messages[0] if messages else "")[:8000],
                fault_category="intent",
            ))
    db.flush()
    return out


# ── views ──────────────────────────────────────────────────────────────────────


def judged_wrong(candidate: AdmissionCandidate) -> bool:
    """A candidate the current evaluators called a pass is also a calibration sample."""
    return any(v == "pass" for v in (candidate.existing_judgement or {}).values())


def candidate_out(row: AdmissionCandidate) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "source": row.source,
        "source_ref": row.source_ref,
        "session_id": row.session_id,
        "cluster_id": row.cluster_id,
        "cluster_name": row.cluster_name,
        "affected_sessions": row.affected_sessions,
        "fault_category": row.fault_category,
        "question": row.question,
        "answer": row.answer,
        "proposed_criteria": row.proposed_criteria or [],
        "existing_judgement": row.existing_judgement or {},
        "judged_wrong": judged_wrong(row),
        "duplicate_of": row.duplicate_of,
        "redaction": row.redaction or {},
        "status": row.status,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "note": row.note,
        "admitted_item_ref": row.admitted_item_ref,
        "admitted_dataset_id": row.admitted_dataset_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def queue(
    db: Session, workspace_id: str, *, agent_id: str | None = None,
    status: str | None = "new", limit: int = 100,
) -> dict[str, Any]:
    query = select(AdmissionCandidate).where(AdmissionCandidate.workspace_id == workspace_id)
    if agent_id:
        query = query.where(AdmissionCandidate.agent_id == agent_id)
    if status:
        query = query.where(AdmissionCandidate.status == status)
    rows = list(db.scalars(query.order_by(
        AdmissionCandidate.affected_sessions.desc(), AdmissionCandidate.created_at.desc()
    ).limit(limit)).all())
    clusters: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row.cluster_id:
            continue
        bucket = clusters.setdefault(row.cluster_id, {
            "cluster_id": row.cluster_id, "name": row.cluster_name,
            "affected_sessions": row.affected_sessions, "candidates": 0, "noise": 0,
        })
        bucket["candidates"] += 1
        if row.status == "duplicate":
            bucket["noise"] += 1
    return {
        "candidates": [candidate_out(r) for r in rows],
        # judged-wrong first, then by impact: both are what the reviewer should see first
        "priority": [r.id for r in sorted(
            rows, key=lambda r: (not judged_wrong(r), -(r.affected_sessions or 1))
        )],
        "clusters": sorted(clusters.values(), key=lambda c: -c["affected_sessions"]),
        "counts": {
            status_name: db.query(AdmissionCandidate).filter(
                AdmissionCandidate.workspace_id == workspace_id,
                AdmissionCandidate.status == status_name,
                *([AdmissionCandidate.agent_id == agent_id] if agent_id else []),
            ).count()
            for status_name in STATUSES
        },
    }


def get_candidate(db: Session, workspace_id: str, candidate_id: str) -> AdmissionCandidate:
    row = db.get(AdmissionCandidate, candidate_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError("admission.not_found", "candidate not found")
    return row


# ── decisions ──────────────────────────────────────────────────────────────────


def admit(
    db: Session,
    candidate: AdmissionCandidate,
    *,
    split_dataset: Any,
    expected_response: str,
    expected_source: str,
    criteria_ids: list[str],
    case_tier: str,
    actor: str,
    workspace_ctx: Any = None,
    scenario_id: str | None = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Admit one candidate into a dev or regression split, with a human expected answer."""
    if candidate.status in ("admitted", "duplicate"):
        raise AppError("admission.decided", "this candidate is already decided",
                       status_code=409)
    if split_dataset.split == "holdout":
        raise AppError("golden.holdout_closed",
                       "the holdout split stays out of the development loop", status_code=409)
    if expected_source not in ("annotator", "consensus", "adjudicated"):
        raise AppError("admission.expected_source",
                       "the expected answer must be written or confirmed by a person")
    if not expected_response.strip():
        raise AppError("admission.no_expected", "write the expected answer before admitting")
    redaction = candidate.redaction or {}
    # The preview used to be computed only by the detail route, so admitting without
    # opening the candidate first — or on a workspace with no guardrail configured —
    # put the raw transcript into a dataset every member can read. Compute it here, and
    # treat anything other than a finished screening as a refusal.
    if not redaction.get("status"):
        from app.models.ledger import Agent

        agent = db.get(Agent, candidate.agent_id)
        redaction = redaction_preview(
            candidate.question, candidate.answer, agent, workspace_ctx
        )
        candidate.redaction = redaction
        db.flush()
    if redaction.get("status") == "blocked":
        raise AppError(
            "admission.redaction_blocked",
            "this candidate cannot be redacted — it must not enter the golden set",
            {"redaction": redaction}, status_code=409,
        )
    question = candidate.question
    if redact and redaction.get("question"):
        question = redaction["question"]
    item = {
        "scenario_id": scenario_id or f"adm-{candidate.id}",
        "turns": [{"input": question, "expected_response": expected_response}],
        "metadata": {"dlc": {
            "case_tier": case_tier,
            # how this text was screened. `unavailable` = the workspace has no PII
            # guardrail configured, so nothing masked it — the coverage view says so
            # rather than the set quietly holding an unscreened transcript.
            "redaction": redaction.get("status") or "unknown",
            "criteria_ids": criteria_ids,
            "origin": "issue" if candidate.source == "issue" else (
                "insight" if candidate.source.startswith("insight") else candidate.source
            ),
            "source_session": candidate.session_id,
            "cluster_id": candidate.cluster_id,
            "fault_category": candidate.fault_category,
            "expected_source": expected_source,
            "admitted_from": candidate.id,
        }},
    }
    added = golden_svc.add_items(db, split_dataset, [item], actor=actor)
    candidate.status = "admitted"
    candidate.decided_by = actor
    candidate.decided_at = _now()
    candidate.admitted_item_ref = added[0]["scenario_id"]
    candidate.admitted_dataset_id = split_dataset.id
    candidate.proposed_criteria = criteria_ids
    record_audit_event(workspace_id=candidate.workspace_id, actor=actor,
                       action="golden.admit", target=candidate.id, db=db)
    db.flush()
    return added[0]


def reject(db: Session, candidate: AdmissionCandidate, *, actor: str, note: str) -> None:
    if not note.strip():
        raise AppError("admission.reason_required", "say why this candidate is not admitted")
    candidate.status = "rejected"
    candidate.decided_by = actor
    candidate.decided_at = _now()
    candidate.note = note[:2000]
    db.flush()


def mark_duplicate(
    db: Session, candidate: AdmissionCandidate, *, of: str, actor: str
) -> None:
    candidate.status = "duplicate"
    candidate.duplicate_of = of[:160]
    candidate.decided_by = actor
    candidate.decided_at = _now()
    db.flush()
