"""Agent-DLC ledger tables (docs/agent-dlc-design.md §4).

The criterion is the unit everything hangs off: a versioned `CriteriaSet` holds
`Criterion` rows (dimension × tier × executor × threshold); a run's per-item verdicts
are snapshotted into `CriterionResult` so a gate report stays reproducible after the
CloudWatch results expire; calibration, release decisions, waivers, the admission
queue and watch schedules each get their own table. All of them are workspace-scoped
(`WORKSPACE_SCOPED_TABLES`) and registered from `core/db.init_db`.

AWS stays the source of truth for what it owns (datasets, evaluators, endpoints):
these rows hold identifiers and the derived decisions only.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now

DIMENSIONS = ("cognition", "quality", "responsibility", "cost", "performance")
TIERS = ("redline", "gate", "observe")
LEVELS = ("session", "trace", "tool_call")
EXPECTED_TYPES = ("deterministic", "redline", "trajectory", "compliance", "soft", "efficiency")
DENOMINATORS = ("sessions", "turns", "fields")
# what the platform can measure from spans today (first-token latency is not emitted)
METRICS = ("latency_p95_ms", "cost_per_success_usd", "tokens_per_session")
SET_KINDS = ("template", "agent")
SET_STATUSES = ("draft", "published", "superseded")
ROW_ORIGINS = ("own", "template", "override", "added")


class CriteriaSet(Base):
    """One version of a criteria set (判据表). `lineage_id` ties the versions together.

    `kind="template"` is a scenario template shared by agents; `kind="agent"` belongs to
    one agent and, when it inherits a template, records the template lineage and the
    version it was materialized from. A published version is immutable.
    """

    __tablename__ = "criteria_sets"
    __table_args__ = (
        UniqueConstraint("lineage_id", "version", name="uq_criteria_set_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    lineage_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="agent")
    agent_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    template_id: Mapped[str | None] = mapped_column(String(32), default=None)  # lineage
    template_version: Mapped[int | None] = mapped_column(Integer, default=None)
    name: Mapped[str] = mapped_column(String(96))
    description: Mapped[str] = mapped_column(Text, default="")
    scenario: Mapped[str] = mapped_column(String(96), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    parent_version: Mapped[int | None] = mapped_column(Integer, default=None)
    source: Mapped[str] = mapped_column(String(24), default="manual")
    signed_by: Mapped[str | None] = mapped_column(String(64), default=None)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    sign_note: Mapped[str] = mapped_column(Text, default="")
    # agent sets only: template criteria this set drops, each with its written reason
    removals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Criterion(Base):
    """One row of a criteria-set version. `key` is stable across versions (lineage)."""

    __tablename__ = "criteria"
    __table_args__ = (UniqueConstraint("set_id", "key", name="uq_criterion_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    set_id: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    dimension: Mapped[str] = mapped_column(String(16))
    tier: Mapped[str] = mapped_column(String(16))
    # gate: pass-rate threshold 0..1; redline: None (always 0 violations)
    threshold: Mapped[float | None] = mapped_column(Float, default=None)
    # cost / performance: {metric, op: "<=" | ">=", value, unit}
    metric_rule: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    level: Mapped[str] = mapped_column(String(16), default="session")
    # {kind: evaluator|metric|human, evaluator_id?, metric?, label_map?, score_rule?}
    executor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    denominator: Mapped[str] = mapped_column(String(16), default="sessions")
    expected_type: Mapped[str] = mapped_column(String(16), default="deterministic")
    pass_k: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    attribution_layer: Mapped[str | None] = mapped_column(String(4), default=None)
    owner: Mapped[str] = mapped_column(String(64), default="")
    examples: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    # own | template (inherited unchanged) | override (template key, changed) | added
    origin: Mapped[str] = mapped_column(String(16), default="own")
    removal_reason: Mapped[str] = mapped_column(Text, default="")


class CriterionResult(Base):
    """One verdict: criterion × scenario × attempt, snapshotted when a run completes."""

    __tablename__ = "criterion_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    criterion_key: Mapped[str] = mapped_column(String(32), index=True)
    scenario_id: Mapped[str] = mapped_column(String(128), default="")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    session_id: Mapped[str] = mapped_column(String(128), default="")
    evaluator_id: Mapped[str] = mapped_column(String(128), default="")
    level: Mapped[str] = mapped_column(String(16), default="")
    unit_ref: Mapped[str] = mapped_column(String(160), default="")  # trace / span id
    raw_value: Mapped[float | None] = mapped_column(Float, default=None)
    raw_label: Mapped[str | None] = mapped_column(String(64), default=None)
    explanation: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str] = mapped_column(String(16))  # pass | fail | inconclusive | error
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AnnotationTask(Base):
    __tablename__ = "annotation_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    criteria_set_id: Mapped[str | None] = mapped_column(String(32), default=None)
    criterion_key: Mapped[str] = mapped_column(String(32), default="")
    purpose: Mapped[str] = mapped_column(String(24), default="judge_calibration")
    dataset_id: Mapped[str | None] = mapped_column(String(32), default=None)
    run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # [{ref, session_id?, input, answer, context?, judge_label?, judge_explanation?}]
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    annotators: Mapped[list[str]] = mapped_column(JSON, default=list)
    adjudicator: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        UniqueConstraint("task_id", "item_ref", "annotator", name="uq_annotation"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    item_ref: Mapped[str] = mapped_column(String(160))
    annotator: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(32), default="")  # pass|fail|inconclusive
    answer: Mapped[str] = mapped_column(Text, default="")  # golden-answer tasks
    rationale: Mapped[str] = mapped_column(Text, default="")
    adjudicated: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CalibrationRecord(Base):
    __tablename__ = "calibration_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    criteria_lineage_id: Mapped[str | None] = mapped_column(String(32), default=None)
    criterion_key: Mapped[str] = mapped_column(String(32), index=True)
    criteria_set_version: Mapped[int | None] = mapped_column(Integer, default=None)
    evaluator_id: Mapped[str] = mapped_column(String(128), default="")
    evaluator_updated_at: Mapped[str | None] = mapped_column(String(40), default=None)
    task_id: Mapped[str | None] = mapped_column(String(32), default=None)
    n: Mapped[int] = mapped_column(Integer, default=0)
    human_human_kappa: Mapped[float | None] = mapped_column(Float, default=None)
    judge_human_kappa: Mapped[float | None] = mapped_column(Float, default=None)
    kappa_ci_low: Mapped[float | None] = mapped_column(Float, default=None)
    kappa_ci_high: Mapped[float | None] = mapped_column(Float, default=None)
    confusion: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    disagreements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    verdict: Mapped[str] = mapped_column(String(16))  # aligned|not_aligned|insufficient_n
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    note: Mapped[str] = mapped_column(Text, default="")


class ReleaseRecord(Base):
    __tablename__ = "release_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    promotion_id: Mapped[str | None] = mapped_column(String(32), default=None)
    candidate_version: Mapped[str | None] = mapped_column(String(32), default=None)
    candidate_endpoint: Mapped[str | None] = mapped_column(String(64), default=None)
    previous_live_version: Mapped[str | None] = mapped_column(String(32), default=None)
    criteria_set_id: Mapped[str | None] = mapped_column(String(32), default=None)
    criteria_set_version: Mapped[int | None] = mapped_column(Integer, default=None)
    golden_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evaluator_set_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    run_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    gate_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # pending | released | blocked | invalid | rolled_back
    decision: Mapped[str] = mapped_column(String(16), default="pending")
    requested_by: Mapped[str] = mapped_column(String(64), default="")
    decided_by: Mapped[str | None] = mapped_column(String(64), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str] = mapped_column(Text, default="")
    waiver_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    rollback_target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Waiver(Base):
    __tablename__ = "waivers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    criterion_key: Mapped[str] = mapped_column(String(32))
    criteria_lineage_id: Mapped[str | None] = mapped_column(String(32), default=None)
    criteria_set_version: Mapped[int | None] = mapped_column(Integer, default=None)
    actual: Mapped[float | None] = mapped_column(Float, default=None)
    threshold: Mapped[float | None] = mapped_column(Float, default=None)
    reason: Mapped[str] = mapped_column(Text, default="")
    risk_owner: Mapped[str] = mapped_column(String(64), default="")
    compensating_control: Mapped[str] = mapped_column(Text, default="")
    expires_on: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # requested | approved | rejected | revoked (expiry is derived from expires_on)
    status: Mapped[str] = mapped_column(String(16), default="requested")
    requested_by: Mapped[str] = mapped_column(String(64), default="")
    approved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AdmissionCandidate(Base):
    __tablename__ = "admission_candidates"
    __table_args__ = (
        UniqueConstraint("agent_id", "source", "source_ref", name="uq_admission_source"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(24))
    source_ref: Mapped[str] = mapped_column(String(160))
    session_id: Mapped[str | None] = mapped_column(String(128), default=None)
    cluster_id: Mapped[str | None] = mapped_column(String(128), default=None)
    cluster_name: Mapped[str] = mapped_column(String(256), default="")
    affected_sessions: Mapped[int] = mapped_column(Integer, default=1)
    fault_category: Mapped[str] = mapped_column(String(128), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    proposed_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    existing_judgement: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    duplicate_of: Mapped[str | None] = mapped_column(String(160), default=None)
    redaction: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # new | annotating | admitted | rejected | duplicate
    status: Mapped[str] = mapped_column(String(16), default="new")
    decided_by: Mapped[str | None] = mapped_column(String(64), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str] = mapped_column(Text, default="")
    admitted_item_ref: Mapped[str | None] = mapped_column(String(160), default=None)
    admitted_dataset_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WatchConfig(Base):
    __tablename__ = "watch_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    criteria_set_id: Mapped[str | None] = mapped_column(String(32), default=None)
    dataset_id: Mapped[str | None] = mapped_column(String(32), default=None)
    every: Mapped[str] = mapped_column(String(16), default="daily")  # daily | weekly
    at_hour: Mapped[int] = mapped_column(Integer, default=3)
    tz: Mapped[str] = mapped_column(String(48), default="UTC")
    repeats: Mapped[int] = mapped_column(Integer, default=1)
    max_cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_status: Mapped[str] = mapped_column(String(24), default="")
    last_detail: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SchedulerClaim(Base):
    """Single-writer guard for scheduled work (§10). Hub-global: one row per task key."""

    __tablename__ = "scheduler_claims"

    task: Mapped[str] = mapped_column(String(128), primary_key=True)
    claimed_by: Mapped[str] = mapped_column(String(64), default="")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
