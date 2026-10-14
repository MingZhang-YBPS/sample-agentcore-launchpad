"""Cross-workspace fleet overview and governance health (roadmap T37/T39).

Every other console read is scoped to one workspace, which is right for the work but wrong
for the question an administrator of several environments actually has: *where is anything
wrong*. Switching workspaces one at a time to find out is the gap this closes.

Two deliberate constraints shape the implementation:

* **Ledger first, AWS never in the loop.** A fleet view that fans out Logs Insights across
  every account would be slow and billed per scan, and would fail whole-page when one
  spoke's credentials lapsed. So the row counts, statuses and alert states come from the
  ledger — which is exactly where the platform already records them — and the per-workspace
  telemetry stays one click away on that workspace's own pages.
* **A workspace that cannot be read is said to be unreadable.** A `failed` bootstrap or a
  revoked spoke role shows as its own state rather than as zero agents, because a fleet
  table whose quiet rows might mean "healthy" or might mean "unreachable" is worse than no
  table.

Governance health (T39) is the same shape: findings computed from ledger state, each with
the console link that fixes it, so the score is never a number without a next action.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.project_scope import project_context
from app.models.ledger import (
    Agent,
    AlertRule,
    Deployment,
    Job,
    Promotion,
    Workspace,
)
from app.models.projects import Project, ProjectWorkspace
from app.services import alerts as alert_service

# A bootstrap state that means the environment cannot serve anything yet.
UNUSABLE_STATES = ("registered", "bootstrapping", "failed")
STALE_DEPLOY_DAYS = 90


def _counts_by_workspace_project(
    db: Session, model: Any, **filters: Any
) -> dict[tuple[str, str | None], int]:
    """`(workspace, project) → rows`; project None = workspace / platform-level rows."""
    query = select(model.workspace_id, model.project_id, func.count()).group_by(
        model.workspace_id, model.project_id
    )
    for column, value in filters.items():
        query = query.where(getattr(model, column) == value)
    return {(row[0], row[1]): int(row[2]) for row in db.execute(query).all() if row[0]}


def _per_workspace(
    counts: dict[tuple[str, str | None], int], project: str | None
) -> dict[str, int]:
    """Fold the per-project counts into per-workspace ones — one project's only when
    the view is filtered to it."""
    out: dict[str, int] = {}
    for (workspace_id, project_id), n in counts.items():
        if project is None or project_id == project:
            out[workspace_id] = out.get(workspace_id, 0) + n
    return out


def fleet_overview(db: Session, project: str | None = None) -> dict[str, Any]:
    """One row per workspace: what it runs, what is failing, what is waiting — and,
    per workspace, the same numbers per bound project (design §6.6).

    Admin-only by classification — it spans environments a member may not be granted,
    and the projects in them: the one business view that reads across projects, so it
    runs unscoped whatever project the caller's console is in. `project` keeps the
    workspaces that project is bound to and counts only its rows.
    """
    with project_context(None):
        return _fleet_overview(db, project)


def _fleet_overview(db: Session, project: str | None) -> dict[str, Any]:
    workspaces = list(db.scalars(select(Workspace).order_by(Workspace.id)))
    names = {row.id: row.name for row in db.scalars(select(Project))}
    bound: dict[str, list[str]] = {}
    for binding in db.scalars(
        select(ProjectWorkspace).order_by(
            ProjectWorkspace.is_workspace_default.desc(), ProjectWorkspace.project_id
        )
    ):
        bound.setdefault(binding.workspace_id, []).append(binding.project_id)
    if project is not None:
        workspaces = [row for row in workspaces if project in bound.get(row.id, [])]
    counts = {
        "agents_active": _counts_by_workspace_project(db, Agent, status="active"),
        "agents_deploying": _counts_by_workspace_project(db, Agent, status="deploying"),
        "agents_failed": _counts_by_workspace_project(db, Agent, status="failed"),
        "jobs_failed": _counts_by_workspace_project(db, Job, status="failed"),
        "promotions_pending": _counts_by_workspace_project(db, Promotion, status="pending"),
        "alerts_firing": _counts_by_workspace_project(
            db, AlertRule, state=alert_service.STATE_FIRING
        ),
    }
    active = _per_workspace(counts["agents_active"], project)
    deploying = _per_workspace(counts["agents_deploying"], project)
    failed_agents = _per_workspace(counts["agents_failed"], project)
    failed_jobs = _per_workspace(counts["jobs_failed"], project)
    pending_promotions = _per_workspace(counts["promotions_pending"], project)
    firing = _per_workspace(counts["alerts_firing"], project)

    rows: list[dict[str, Any]] = []
    for row in workspaces:
        usable = row.bootstrap_status not in UNUSABLE_STATES
        rows.append(
            {
                "id": row.id,
                "name": row.name,
                "account_id": row.account_id,
                "region": row.region,
                "tier": row.tier or "dev",
                "cross_account": row.role_arn is not None,
                "bootstrap_status": row.bootstrap_status,
                # Stated explicitly: a row with no agents because it was never
                # bootstrapped is not the same as a healthy empty environment.
                "readable": usable,
                "agents_active": active.get(row.id, 0) if usable else None,
                "agents_deploying": deploying.get(row.id, 0) if usable else None,
                "agents_failed": failed_agents.get(row.id, 0) if usable else None,
                "jobs_failed": failed_jobs.get(row.id, 0) if usable else None,
                "promotions_pending": pending_promotions.get(row.id, 0) if usable else None,
                "alerts_firing": firing.get(row.id, 0) if usable else None,
                "projects": [
                    {
                        "id": pid,
                        "name": names.get(pid, pid),
                        **{
                            key: (by.get((row.id, pid), 0) if usable else None)
                            for key, by in counts.items()
                        },
                    }
                    for pid in bound.get(row.id, [])
                    if project is None or pid == project
                ],
            }
        )

    attention = [
        row
        for row in rows
        if not row["readable"]
        or (row["agents_failed"] or 0)
        or (row["jobs_failed"] or 0)
        or (row["alerts_firing"] or 0)
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "workspaces": rows,
        "totals": {
            "workspaces": len(rows),
            "readable": len([row for row in rows if row["readable"]]),
            "agents_active": sum(row["agents_active"] or 0 for row in rows),
            "alerts_firing": sum(row["alerts_firing"] or 0 for row in rows),
            "promotions_pending": sum(row["promotions_pending"] or 0 for row in rows),
            "needs_attention": len(attention),
        },
        **({"project": project} if project is not None else {}),
        "source": "ledger",
    }


# ── T39: governance health ───────────────────────────────────────────────────────


def _finding(
    key: str, severity: str, count: int, to: str, sample: list[str]
) -> dict[str, Any]:
    return {"key": key, "severity": severity, "count": count, "to": to, "sample": sample}


def governance_health(db: Session, workspace_id: str) -> dict[str, Any]:
    """Findings for one workspace, each with the link that fixes it, plus a score.

    The score is a blunt instrument on purpose: it exists to make a trend visible, and
    every point it deducts is attributable to a listed finding. A score with no findings
    behind it would be a number nobody could act on.
    """
    findings: list[dict[str, Any]] = []

    agents = list(
        db.scalars(
            select(Agent).where(
                Agent.workspace_id == workspace_id,
                Agent.status == "active",
                Agent.system_key.is_(None),
            )
        )
    )

    # 1. Active agents with PII protection off. Advisory, not a failure: not every agent
    #    handles personal data — which is why it is `info`, not `warn`.
    unguarded = [a for a in agents if not ((a.spec or {}).get("guardrail") or {}).get("enabled")]
    if unguarded:
        findings.append(
            _finding(
                "agents_without_guardrail",
                "info",
                len(unguarded),
                "/v2/agents",
                [a.name for a in unguarded[:5]],
            )
        )

    # 2. Never evaluated. An agent nobody measured is the one a release should not carry.
    from app.evaluation.models import EvalRun

    evaluated = {
        row
        for row in db.scalars(
            select(EvalRun.agent_id).where(EvalRun.workspace_id == workspace_id).distinct()
        )
    }
    unevaluated = [a for a in agents if a.id not in evaluated]
    if unevaluated:
        findings.append(
            _finding(
                "agents_unevaluated",
                "warn",
                len(unevaluated),
                "/v2/eval/tasks?view=new",
                [a.name for a in unevaluated[:5]],
            )
        )

    # 3. Alert rules firing, and the absence of any rule at all — an environment nobody
    #    watches is a governance finding in its own right.
    firing = alert_service.firing_rules(db, workspace_id)
    if firing:
        findings.append(
            _finding(
                "alerts_firing",
                "action",
                len(firing),
                "/v2/costs?view=alerts",
                [r.name or r.kind for r in firing[:5]],
            )
        )
    rule_count = db.scalar(
        select(func.count()).select_from(AlertRule).where(AlertRule.workspace_id == workspace_id)
    )
    if not rule_count and agents:
        findings.append(_finding("no_alert_rules", "warn", 1, "/v2/costs?view=alerts", []))

    # 4. Stale deployments: an active agent whose last successful deploy is months old.
    cutoff = datetime.now(UTC) - timedelta(days=STALE_DEPLOY_DAYS)
    stale: list[str] = []
    for agent in agents:
        last = db.scalars(
            select(Deployment)
            .where(Deployment.agent_id == agent.id, Deployment.status == "succeeded")
            .order_by(Deployment.started_at.desc())
        ).first()
        if last is None:
            continue
        started = last.started_at
        if started is None:
            continue
        if (started if started.tzinfo else started.replace(tzinfo=UTC)) < cutoff:
            stale.append(agent.name)
    if stale:
        findings.append(
            _finding("deployments_stale", "info", len(stale), "/v2/agents", stale[:5])
        )

    weights = {"action": 15, "warn": 8, "info": 3}
    score = 100
    for finding in findings:
        score -= weights.get(finding["severity"], 0) * min(finding["count"], 3)
    score = max(0, score)
    order = {"action": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), -f["count"]))
    return {
        "workspace_id": workspace_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "score": score,
        "grade": "good" if score >= 85 else "fair" if score >= 60 else "poor",
        "findings": findings,
        "agents_considered": len(agents),
    }
