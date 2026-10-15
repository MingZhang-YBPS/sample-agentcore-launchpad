"""The storyline's facts (storyline-ux §2.3): what exists for one agent, in one read.

The console's storyline (``frontend/src/v3/story/stories.ts``) ticks each step from
data, never from clicks. Its inputs live in a dozen tables — criteria sets, golden
splits, evaluation runs, release records, API keys, alert rules, issues, the
admission queue — so ``agent_facts`` reads them once and the console gets one
answer instead of chaining a dozen requests. Pure ledger reads plus two cheap
signals that are already cached elsewhere:

* ``cedar_mode`` — the workspace gateway's Policy Engine mode (one ``GetGateway``,
  cached here per workspace like the facts);
* ``spans_7d`` — taken from the detail page's telemetry probe cache
  (``routers/agent_telemetry``) when it has an answer; ``None`` (unknown) otherwise,
  so opening the storyline never scans ``aws/spans`` itself.

Every query runs in the caller's project (the ORM filter of ``core/project_scope``):
another project's rows are simply not there.

``AGENT_STEPS`` mirrors ``AGENT_STORY`` for ``GET /api/me/next-steps`` — key, the
permission key the step needs and the workshop roles it belongs to. The console's
consistency test (``v3/story/stories.test.ts``) reads this file, so the two cannot
drift apart.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.evaluation.models import EvalRun
from app.models.dlc import (
    AdmissionCandidate,
    CalibrationRecord,
    CriteriaSet,
    ReleaseRecord,
    Waiver,
)
from app.models.ledger import Agent, AlertRule, ApiKey, ChatSession
from app.models.selfservice import Issue

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0
# one answer per (workspace, project, agent); bounded so a long-lived process stays small
_MAX_ENTRIES = 2000

_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_cedar_cache: dict[str, tuple[float, str | None]] = {}
_lock = threading.Lock()


def reset_cache_for_tests() -> None:
    with _lock:
        _cache.clear()
        _cedar_cache.clear()


# ── the steps (mirrors frontend/src/v3/story/stories.ts AGENT_STORY) ──────────────

Facts = dict[str, Any]


@dataclass(frozen=True)
class Step:
    key: str
    chapter: str
    perm: str
    roles: tuple[str, ...]
    done: Callable[[Facts], bool]
    optional: bool = False


AGENT_STEPS: tuple[Step, ...] = (
    Step("build.create", "build", "agents.deploy", ("developer",),
         lambda f: f["agent_active"]),
    Step("build.chat", "build", "project.use", ("developer",),
         lambda f: f["sessions"] > 0),
    Step("build.kb", "build", "agents.deploy", ("developer",),
         lambda f: f["knowledge_bases"] > 0, optional=True),
    Step("build.skill", "build", "agents.deploy", ("developer", "biz-owner"),
         lambda f: f["skills"] > 0, optional=True),
    Step("evaluate.criteria.draft", "evaluate", "criteria.manage", ("developer",),
         lambda f: f["criteria_draft"] or f["criteria_published"]),
    Step("evaluate.criteria.sign", "evaluate", "criteria.sign", ("biz-owner",),
         lambda f: f["criteria_published"] and f["criteria_signed"]),
    Step("evaluate.golden.seal", "evaluate", "golden.admit", ("biz-owner",),
         lambda f: f["golden_sealed"]),
    Step("evaluate.dev_run", "evaluate", "eval.run", ("developer",),
         lambda f: f["dev_run_done"]),
    # the migrate route is release.sign's (route_policy), so the step is too
    Step("evaluate.dual_endpoint", "evaluate", "release.sign", ("release-ops",),
         lambda f: f["endpoint_mode"] == "live"),
    Step("evaluate.candidate", "evaluate", "agents.deploy", ("developer",),
         lambda f: bool(f["candidate_version"]) or f["release_live"]),
    Step("evaluate.gate", "evaluate", "eval.run", ("release-ops",),
         lambda f: bool(f["gate_verdict"]) or f["release_live"]),
    Step("evaluate.calibrate", "evaluate", "project.use", ("sme", "biz-owner"),
         lambda f: f["calibrated"], optional=True),
    Step("release.sign", "release", "release.sign", ("release-ops",),
         lambda f: f["release_live"]),
    Step("release.api_key", "release", "project.write", ("developer",),
         lambda f: f["api_keys"] > 0),
    Step("release.cedar", "release", "project.write", ("release-ops",),
         lambda f: f["cedar_mode"] == "ENFORCE", optional=True),
    Step("observe.traces", "observe", "project.use", ("developer",),
         lambda f: f["spans_7d"] is True),
    # alert rules are workspace-wide, written by administrators (route_policy)
    Step("observe.alerts", "observe", "admin", ("release-ops",),
         lambda f: f["alert_rules"] > 0, optional=True),
    Step("feedback.triage", "feedback", "project.write", ("developer", "biz-owner"),
         lambda f: f["issues_open"] == 0 or f["issues_resolved"] > 0),
    Step("feedback.loop", "feedback", "golden.admit", ("biz-owner",),
         lambda f: f["golden_from_feedback"] > 0),
)

# project role templates → the workshop's role names (``access.ROLE_TEMPLATES``)
WORKSHOP_ROLES = {
    "developer": "developer",
    "biz_owner": "biz-owner",
    "release_ops": "release-ops",
    "sme": "sme",
}


def current_step(facts: Facts) -> Step | None:
    """The first required step not done yet; later ones are only dimmed (D-3)."""
    return next((s for s in AGENT_STEPS if not s.optional and not s.done(facts)), None)


def is_my_turn(step: Step, *, roles: frozenset[str], can: Callable[[str], bool]) -> bool:
    """The caller holds the step's key and plays one of its roles. The pseudo-key
    ``admin`` is an administrator-only step (``can("admin")`` is the bypass).

    An administrator (``admin`` role) or a caller whose project role is not a workshop
    template (a custom role, ``inherit``) is matched on the key alone — for the single
    local operator every current step is theirs."""
    if not can(step.perm):
        return False
    if "admin" in roles or not roles:
        return True
    return bool(roles & set(step.roles))


def viewer_roles(project: Any) -> frozenset[str]:
    """The workshop roles the caller plays in ``project`` (a ``ProjectScope``)."""
    if project is None or getattr(project, "bypass", False):
        return frozenset({"admin"})
    return frozenset(
        WORKSHOP_ROLES[role] for _source, role in project.role_sources if role in WORKSHOP_ROLES
    )


def story_kind(agent: Agent) -> str:
    """``agent`` walks the storyline; an MCP server and a system preset do not (§3.2)."""
    if agent.system_key:
        return "system"
    if ((agent.spec or {}).get("protocol")) == "mcp":
        return "mcp"
    return "agent"


# ── the facts ────────────────────────────────────────────────────────────────────


def _count(db: Session, stmt: Any) -> int:
    return int(db.scalar(stmt) or 0)


def _spans_7d(workspace_id: str, agent: Agent) -> bool | None:
    """The telemetry probe's cached answer: spans in the last 7 days, none, or unknown."""
    from app.routers import agent_telemetry

    now = time.monotonic()
    with agent_telemetry._cache_lock:  # noqa: SLF001 - a read of the shared probe cache
        hits = [v for k, v in agent_telemetry._cache.items()  # noqa: SLF001
                if k[:2] == (workspace_id, agent.id) and v[0] > now]
    if not hits:
        return None
    result = hits[0][1]
    if result.get("status") == "receiving":
        return result.get("last_span_within") in ("1h", "24h", "7d")
    if result.get("status") in ("none", "not_deployed"):
        return False
    return None


def _read_cedar(workspace_id: str) -> str | None:
    """The gateway's Policy Engine mode (``ENFORCE`` / ``LOG_ONLY``), None when there is
    no engine or it cannot be read. Module-level so tests substitute it."""
    from app.services import release_gates

    try:
        state = release_gates.read_policy_mode(workspace_id)
    except Exception as exc:  # noqa: BLE001 - unreadable reads as "not enforced"
        logger.info("storyline: policy mode of %s unreadable: %s", workspace_id,
                    type(exc).__name__)
        return None
    return state.get("mode") if state.get("engine") else None


def cedar_mode(workspace_id: str) -> str | None:
    now = time.monotonic()
    with _lock:
        hit = _cedar_cache.get(workspace_id)
    if hit and hit[0] > now:
        return hit[1]
    mode = _read_cedar(workspace_id)
    with _lock:
        _cedar_cache[workspace_id] = (now + CACHE_TTL_SECONDS, mode)
    return mode


def compute_facts(db: Session, workspace_id: str, agent: Agent) -> Facts:
    """The uncached answer. Every read is confined to the current project."""
    from app.dlc import criteria as criteria_svc
    from app.dlc import golden as golden_svc
    from app.dlc import releases as release_svc

    spec = agent.spec or {}
    published = criteria_svc.agent_set(db, workspace_id, agent.id)
    draft = db.scalars(
        select(CriteriaSet).where(
            CriteriaSet.workspace_id == workspace_id,
            CriteriaSet.agent_id == agent.id,
            CriteriaSet.kind == "agent",
            CriteriaSet.status == "draft",
        ).order_by(CriteriaSet.version.desc())
    ).first()
    lineage_id = (published or draft).lineage_id if (published or draft) else None
    golden = golden_svc.for_criteria(db, workspace_id, lineage_id) if lineage_id else None
    splits = golden_svc.splits_of(db, golden) if golden is not None else {}
    holdout = splits.get("holdout")
    golden_items = sum(len(golden_svc.active_items(s)) for s in splits.values())

    dev_run = db.scalars(
        select(EvalRun).where(
            EvalRun.workspace_id == workspace_id,
            EvalRun.agent_id == agent.id,
            EvalRun.status == "completed",
            (EvalRun.split.is_(None)) | (EvalRun.split == "dev"),
        ).order_by(EvalRun.created_at.desc())
    ).first()
    pending = release_svc.pending_for(db, workspace_id, agent.id)
    gated = release_svc.latest_decided(db, workspace_id, agent.id)
    released = _count(db, select(func.count()).select_from(ReleaseRecord).where(
        ReleaseRecord.workspace_id == workspace_id,
        ReleaseRecord.agent_id == agent.id,
        ReleaseRecord.decision == "released",
    ))
    now = datetime.now(UTC)
    keys = db.scalars(select(ApiKey).where(
        ApiKey.workspace_id == workspace_id, ApiKey.enabled.is_(True),
    )).all()
    api_keys = sum(
        1 for k in keys
        if (not k.agent_ids or agent.id in k.agent_ids)
        and (k.expires_at is None or _aware(k.expires_at) > now)
    )
    issues_open = _count(db, select(func.count()).select_from(Issue).where(
        Issue.workspace_id == workspace_id, Issue.agent_id == agent.id, Issue.status == "open"))
    issues_resolved = _count(db, select(func.count()).select_from(Issue).where(
        Issue.workspace_id == workspace_id, Issue.agent_id == agent.id, Issue.status != "open"))
    return {
        "agent_active": agent.status == "active",
        "sessions": _count(db, select(func.count()).select_from(ChatSession).where(
            ChatSession.workspace_id == workspace_id, ChatSession.agent_id == agent.id)),
        "knowledge_bases": len(spec.get("knowledge_bases") or []),
        "skills": len(spec.get("skills") or []),
        "criteria_lineage_id": lineage_id,
        "criteria_draft": draft is not None,
        "criteria_draft_version": draft.version if draft is not None else None,
        "criteria_published": published is not None,
        "criteria_version": published.version if published is not None else None,
        "criteria_signed": bool(published is not None and published.signed_by),
        "criteria_signed_by": published.signed_by if published is not None else None,
        "golden_id": golden.id if golden is not None else None,
        "golden_sealed": bool(holdout is not None and holdout.items),
        "golden_items": golden_items,
        "golden_from_feedback": _count(db, select(func.count()).select_from(
            AdmissionCandidate).where(
            AdmissionCandidate.workspace_id == workspace_id,
            AdmissionCandidate.agent_id == agent.id,
            AdmissionCandidate.status == "admitted",
        )),
        "dev_run_done": dev_run is not None,
        "dev_run_id": dev_run.id if dev_run is not None else None,
        "endpoint_mode": agent.endpoint_mode or "default",
        "candidate_version": pending.candidate_version if pending is not None else None,
        "candidate_record_id": pending.id if pending is not None else None,
        "gate_verdict": (gated.gate_report or {}).get("verdict") if gated is not None else None,
        "gate_record_id": gated.id if gated is not None else None,
        "release_live": released > 0,
        "api_keys": api_keys,
        "cedar_mode": cedar_mode(workspace_id),
        "spans_7d": _spans_7d(workspace_id, agent),
        "alert_rules": _count(db, select(func.count()).select_from(AlertRule).where(
            AlertRule.workspace_id == workspace_id, AlertRule.enabled.is_(True))),
        "issues_open": issues_open,
        "issues_resolved": issues_resolved,
        "calibrated": _count(db, select(func.count()).select_from(CalibrationRecord).where(
            CalibrationRecord.workspace_id == workspace_id,
            CalibrationRecord.agent_id == agent.id,
            CalibrationRecord.verdict == "aligned",
        )) > 0,
        "waivers_pending": _count(db, select(func.count()).select_from(Waiver).where(
            Waiver.workspace_id == workspace_id, Waiver.agent_id == agent.id,
            Waiver.status == "requested")),
    }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def agent_facts(db: Session, workspace_id: str, project_id: str | None,
                agent: Agent) -> tuple[Facts, bool]:
    """``(facts, cached)``: cached for 30 seconds per (workspace, project, agent)."""
    key = (workspace_id, project_id or "", agent.id)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1], True
    facts = compute_facts(db, workspace_id, agent)
    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            for stale in [k for k, v in _cache.items() if v[0] <= now] or list(_cache)[:100]:
                _cache.pop(stale, None)
        _cache[key] = (now + CACHE_TTL_SECONDS, facts)
    return facts, False


def agent_out(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "display_name": (agent.spec or {}).get("display_name") or None,
        "status": agent.status,
        "method": agent.method,
        "kind": story_kind(agent),
    }
