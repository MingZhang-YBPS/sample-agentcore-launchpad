"""Threshold alerts over data the platform already collects (roadmap T29).

Competitors treat alerting as table stakes and Launchpad had none: an agent could start
failing, slow down, lose quality or burn money and nobody learned until somebody opened a
console page. This closes that, deliberately as the smallest honest mechanism:

* **A rule is a threshold on a value the platform can already compute** — error rate and
  p95 latency from the spans the Observability console reads, online quality from the
  evaluation results it already aggregates, month-to-date spend from the price map
  (`services/costs.py`). No new telemetry path, no agent-side change.
* **Evaluation is explicit and cheap to skip.** Every read is billed (Logs Insights) or
  cached, so a rule names its own window and the whole set is evaluated in one pass, from
  the console's Alerts page, the inbox, or a scheduled tick.
* **A transition notifies, not a state.** `state` is remembered on the row, so a rule that
  stays firing does not re-notify on every pass — the thing that made pager fatigue a
  cliché.
* **Delivery is one generic JSON webhook.** A Slack or Feishu incoming hook already *is*
  one, so a single mechanism covers both without the platform storing channel
  credentials. A failed delivery never fails the evaluation: the state is still recorded
  and the inbox still shows it.

A value that cannot be read (no telemetry yet, a query failure) yields `unknown`, never a
silent `ok` — an alert that reports health it did not measure is worse than no alert.
"""

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.ledger import AlertRule
from app.services import costs
from app.services.workspace import WorkspaceContext

KINDS = ("error_rate", "latency_p95_ms", "online_quality", "cost_mtd_usd")
Kind = Literal["error_rate", "latency_p95_ms", "online_quality", "cost_mtd_usd"]
COMPARISONS = ("above", "below")
STATE_OK = "ok"
STATE_FIRING = "firing"
STATE_UNKNOWN = "unknown"
WEBHOOK_TIMEOUT_SECONDS = 5

# Which direction each kind is bad in, so a rule created with the wrong comparison is
# refused rather than silently never firing.
NATURAL_COMPARISON: dict[str, str] = {
    "error_rate": "above",
    "latency_p95_ms": "above",
    "online_quality": "below",
    "cost_mtd_usd": "above",
}

# A rule's window, in the observability range keys. `cost_mtd_usd` ignores it — the month
# is its window by definition.
WINDOWS = ("1h", "6h", "24h", "7d", "30d")
# Error rate and latency come from the observability dashboard, which has no 30-day range.
DASHBOARD_KINDS = ("error_rate", "latency_p95_ms")
DASHBOARD_WINDOWS = ("1h", "6h", "24h", "7d")
# A project's `quotas.monthly_cost_alert_usd` (projects §11.3) is kept as one ordinary
# `cost_mtd_usd` rule of the project, named this, in the project's home workspace (its
# workspace-default binding, else the first): it reads the project's spend across every
# workspace it is bound to, and is (re)written from the quota — never blocks anything.
BUDGET_RULE_NAME = "project monthly budget"
BUDGET_RULE_CREATOR = "quota"


def validate(kind: str, comparison: str, window: str, threshold: float) -> None:
    if kind not in KINDS:
        raise AppError("alert.unknown_kind", f"kind must be one of {', '.join(KINDS)}")
    if comparison not in COMPARISONS:
        raise AppError("alert.bad_comparison", "comparison must be 'above' or 'below'")
    if comparison != NATURAL_COMPARISON[kind]:
        raise AppError(
            "alert.comparison_never_fires",
            f"a '{kind}' rule is only meaningful as "
            f"'{NATURAL_COMPARISON[kind]}' — the other direction would never fire",
        )
    if window not in WINDOWS:
        raise AppError("alert.bad_window", f"window must be one of {', '.join(WINDOWS)}")
    if kind in DASHBOARD_KINDS and window not in DASHBOARD_WINDOWS:
        raise AppError(
            "alert.bad_window",
            f"a '{kind}' rule reads the observability dashboard, whose windows are "
            f"{', '.join(DASHBOARD_WINDOWS)}",
        )
    if kind == "online_quality" and not 0.0 <= threshold <= 1.0:
        raise AppError("alert.bad_threshold", "online_quality is a 0–1 score")
    if threshold < 0:
        raise AppError("alert.bad_threshold", "threshold must not be negative")


# ── reading the values ───────────────────────────────────────────────────────────


def _dashboard_value(
    kind: str, window: str, db: Session, workspace: WorkspaceContext
) -> tuple[float | None, str]:
    """Error rate (0–1) or p95 latency (ms) from the observability dashboard."""
    from app.services import observability

    if window not in observability.RANGE_HOURS:
        return None, f"window '{window}' is not an observability range"
    try:
        data = observability.get_dashboard(window, workspace)
    except Exception as exc:  # noqa: BLE001 - any read failure is `unknown`, never `ok`
        return None, f"could not read telemetry: {type(exc).__name__}"
    # the dashboard nests its numbers under `tiles` (observability.get_dashboard)
    tiles = data.get("tiles") or {}
    trace_tile = tiles.get("traces") or {}
    traces = int(trace_tile.get("total") or 0)
    if traces == 0:
        return None, "no traffic in this window"
    if kind == "error_rate":
        errors = int(trace_tile.get("error") or 0)
        return errors / traces, f"{errors}/{traces} traces failed"
    value = (tiles.get("latency") or {}).get("p95_ms")
    if value is None:
        return None, "no latency percentile in this window"
    return float(value), f"p95 over {traces} traces"


def _quality_value(db: Session, workspace: WorkspaceContext) -> tuple[float | None, str]:
    from app.evaluation.online import online_quality

    try:
        snapshot = online_quality(db, workspace)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not read online evaluation: {type(exc).__name__}"
    mean = snapshot.get("mean")
    if mean is None:
        return None, "no online evaluation scores in the last 24 h"
    return (
        float(mean),
        f"{snapshot.get('scores')} score(s) over {snapshot.get('sessions')} session(s)",
    )


def _cost_value(
    db: Session, workspace: WorkspaceContext, project: str | None = None
) -> tuple[float | None, str]:
    try:
        usd = costs.month_to_date_usd(db, workspace, project=project)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not estimate spend: {type(exc).__name__}"
    return usd, "month-to-date estimate from the price map"


def read_value(
    rule: AlertRule, db: Session, workspace: WorkspaceContext
) -> tuple[float | None, str]:
    """The rule's value, measured as the rule's own project (design §5.12): a project
    rule reads that project's dashboard, online configs and spend; a workspace rule
    (NULL) reads the whole workspace — whichever project the caller is in, and the
    same from the scheduler, which has none."""
    with project_context(rule.project_id):
        if rule.kind in ("error_rate", "latency_p95_ms"):
            return _dashboard_value(rule.kind, rule.window or "24h", db, workspace)
        if rule.kind == "online_quality":
            return _quality_value(db, workspace)
        if rule.kind == "cost_mtd_usd" and is_budget_rule(rule):
            return _project_cost_value(db, workspace, rule.project_id or "")
        if rule.kind == "cost_mtd_usd":
            # a single-project workspace's spend is all that project's, attributed or not
            several = rule.project_id is not None and _several_projects(db, workspace.id)
            return _cost_value(db, workspace, rule.project_id if several else None)
    return None, f"unknown kind '{rule.kind}'"


def _project_cost_value(
    db: Session, workspace: WorkspaceContext, project_id: str
) -> tuple[float | None, str]:
    """A budget rule's value: the project's month-to-date spend in every workspace it
    is bound to (`workspace` is the rule's own; the others are rebuilt from their ids)."""
    from app.services import project_quotas
    from app.services.workspace import context_for_workspace

    total = 0.0
    parts = []
    # the project's budget basis (CT-2): the estimate, or what AWS billed
    basis = "month-to-date estimate from the price map"
    for ws_id in _project_workspaces(db, project_id):
        try:
            ctx = workspace if ws_id == workspace.id else context_for_workspace(ws_id)
        except Exception as exc:  # noqa: BLE001
            return None, f"could not read workspace {ws_id}: {type(exc).__name__}"
        several = _several_projects(db, ws_id)
        usd, detail = project_quotas.budget_spend(db, ctx, project_id, several=several)
        if usd is None:
            return None, f"{ws_id}: {detail}"
        basis = detail
        total += usd
        parts.append(f"{ws_id} ${usd:.2f}")
    return round(total, 4), basis + (
        f" ({', '.join(parts)})" if len(parts) > 1 else "")


def _project_workspaces(db: Session, project_id: str) -> list[str]:
    from app.models.projects import ProjectWorkspace

    with project_context(None):
        return sorted(db.scalars(select(ProjectWorkspace.workspace_id).where(
            ProjectWorkspace.project_id == project_id)))


def _several_projects(db: Session, workspace_id: str) -> bool:
    from app.services.observability import several_projects

    return several_projects(db, workspace_id)


def breached(rule: AlertRule, value: float) -> bool:
    return value > rule.threshold if rule.comparison == "above" else value < rule.threshold


# ── delivery ─────────────────────────────────────────────────────────────────────


def _post_webhook(url: str, payload: dict[str, Any]) -> str:
    """One JSON POST. Returns a short outcome string; never raises."""
    body = json.dumps(payload).encode()
    request = urllib.request.Request(  # noqa: S310 - the URL is an operator's own config
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"{type(exc).__name__}"


def notify(rule: AlertRule, value: float, detail: str, workspace_id: str) -> str | None:
    """Deliver the transition, if the rule has somewhere to send it."""
    if not rule.webhook_url:
        return None
    text = (
        f"[Launchpad/{workspace_id}] {rule.name or rule.kind} is firing: "
        f"{rule.kind} = {value:g} ({rule.comparison} {rule.threshold:g}) — {detail}"
    )
    # `text` is what a Slack or Feishu incoming hook renders; the structured fields ride
    # alongside for anything else consuming the same hook.
    return _post_webhook(
        rule.webhook_url,
        {
            "text": text,
            "workspace_id": workspace_id,
            "project_id": rule.project_id,
            "rule_id": rule.id,
            "kind": rule.kind,
            "value": value,
            "threshold": rule.threshold,
            "comparison": rule.comparison,
            "detail": detail,
        },
    )


# ── evaluation ───────────────────────────────────────────────────────────────────


def evaluate_rules(
    db: Session,
    workspace: WorkspaceContext,
    *,
    notify_transitions: bool = True,
) -> dict[str, Any]:
    """Re-read every enabled rule in this workspace and record what it saw.

    Notifies only on an ok/unknown → firing transition. A delivery result is recorded in
    `last_detail` rather than raised: a broken webhook must not hide the breach.
    """
    sync_budget_rules(db, workspace.id)
    rules = db.scalars(
        select(AlertRule).where(AlertRule.workspace_id == workspace.id).order_by(
            AlertRule.created_at
        )
    ).all()
    now = datetime.now(UTC)
    evaluated: list[dict[str, Any]] = []
    inactive = _inactive_projects(db, {r.project_id for r in rules if r.project_id})
    for rule in rules:
        if not rule.enabled:
            evaluated.append(rule_out(rule, skipped="disabled"))
            continue
        if rule.project_id in inactive:
            # an archived project serves reads only (design §5.12): its rules keep the
            # state they last saw rather than re-reading (billed) and re-notifying
            evaluated.append(rule_out(rule, skipped="project_archived"))
            continue
        value, detail = read_value(rule, db, workspace)
        previous = rule.state
        if value is None:
            rule.state = STATE_UNKNOWN
        else:
            rule.state = STATE_FIRING if breached(rule, value) else STATE_OK
        rule.last_value = value
        rule.last_detail = detail
        rule.last_checked_at = now
        if rule.state == STATE_FIRING:
            rule.last_fired_at = now
            if notify_transitions and previous != STATE_FIRING and value is not None:
                outcome = notify(rule, value, detail, workspace.id)
                if outcome is not None:
                    rule.last_notified_at = now
                    rule.last_detail = f"{detail} · webhook {outcome}"
        evaluated.append(rule_out(rule))
    db.commit()
    firing = [row for row in evaluated if row["state"] == STATE_FIRING]
    return {
        "workspace_id": workspace.id,
        "evaluated_at": now.isoformat(),
        "rules": evaluated,
        "firing": len(firing),
        "unknown": len([row for row in evaluated if row["state"] == STATE_UNKNOWN]),
    }


# ── project budgets (projects §11.3) ─────────────────────────────────────────────


def is_budget_rule(rule: AlertRule) -> bool:
    return bool(rule.project_id) and rule.name == BUDGET_RULE_NAME \
        and rule.created_by == BUDGET_RULE_CREATOR


def budget_rule(db: Session, project_id: str) -> AlertRule | None:
    """The project's budget rule, in whichever workspace it lives."""
    with project_context(None):
        rows = db.scalars(select(AlertRule).where(
            AlertRule.project_id == project_id, AlertRule.name == BUDGET_RULE_NAME,
            AlertRule.created_by == BUDGET_RULE_CREATOR,
        ).order_by(AlertRule.created_at)).all()
    return rows[0] if rows else None


def _home_workspace(db: Session, project_id: str) -> str | None:
    from app.models.projects import ProjectWorkspace

    with project_context(None):
        rows = db.execute(select(ProjectWorkspace.workspace_id,
                                 ProjectWorkspace.is_workspace_default).where(
            ProjectWorkspace.project_id == project_id)).all()
    if not rows:
        return None
    defaults = sorted(ws for ws, is_default in rows if is_default)
    return (defaults or sorted(ws for ws, _ in rows))[0]


def sync_budget_rule(db: Session, project_id: str) -> AlertRule | None:
    """Make the project's budget rule match `quotas.monthly_cost_alert_usd`: create it
    in the home workspace, move it there, retune its threshold, or remove it when the
    quota is cleared. What someone set on the rule (a webhook, disabling it) stays."""
    from app.models.projects import Project
    from app.services.project_quotas import limit_of

    with project_context(None):
        project = db.get(Project, project_id)
        threshold = limit_of(db, project_id, "monthly_cost_alert_usd") if project else None
        rows = list(db.scalars(select(AlertRule).where(
            AlertRule.project_id == project_id, AlertRule.name == BUDGET_RULE_NAME,
            AlertRule.created_by == BUDGET_RULE_CREATOR).order_by(AlertRule.created_at)))
        home = _home_workspace(db, project_id) if threshold is not None else None
        keep = rows[0] if rows and home is not None else None
        for extra in rows[1:] if keep is not None else rows:
            db.delete(extra)
        if home is None:
            db.flush()
            return None
        if keep is None:
            keep = AlertRule(workspace_id=home, project_id=project_id, kind="cost_mtd_usd",
                             name=BUDGET_RULE_NAME, comparison="above", window="30d",
                             threshold=float(threshold), created_by=BUDGET_RULE_CREATOR)
            db.add(keep)
        else:
            if keep.workspace_id != home:
                keep.workspace_id = home
                keep.state = STATE_UNKNOWN
            if keep.threshold != float(threshold):
                keep.threshold = float(threshold)
                keep.state = STATE_UNKNOWN  # re-judged (and re-notified) on the next pass
        db.flush()
    return keep


def sync_budget_rules(db: Session, workspace_id: str) -> None:
    """Before an evaluation pass: the budget rules of every project bound here, so a
    quota set, a department cap changed or a binding moved since is picked up."""
    from app.models.projects import Project, ProjectWorkspace

    with project_context(None):
        bound = set(db.scalars(select(ProjectWorkspace.project_id).where(
            ProjectWorkspace.workspace_id == workspace_id)))
        ruled = set(db.scalars(select(AlertRule.project_id).where(
            AlertRule.workspace_id == workspace_id, AlertRule.name == BUDGET_RULE_NAME,
            AlertRule.created_by == BUDGET_RULE_CREATOR)))
        candidates = sorted(p for p in bound | ruled if p)
        if not candidates:
            return
        rows = db.scalars(select(Project).where(Project.id.in_(candidates))).all()
    from app.services.project_quotas import department_caps, project_values

    for row in rows:
        quota = project_values(row).get("monthly_cost_alert_usd")
        cap = department_caps(db, row).get("monthly_cost_alert_usd")
        if quota is not None or cap is not None or row.id in ruled:
            sync_budget_rule(db, row.id)


def _inactive_projects(db: Session, project_ids: set[str]) -> set[str]:
    """Which of these projects are not active (archived, or on their way out)."""
    if not project_ids:
        return set()
    from app.models.projects import Project

    with project_context(None):
        rows = db.execute(
            select(Project.id, Project.status).where(Project.id.in_(project_ids))
        ).all()
    return {pid for pid, status in rows if status != "active"}


def firing_rules(db: Session, workspace_id: str) -> list[AlertRule]:
    """Rules currently firing — read from the row, so the inbox costs no AWS call."""
    return list(
        db.scalars(
            select(AlertRule).where(
                AlertRule.workspace_id == workspace_id,
                AlertRule.state == STATE_FIRING,
                AlertRule.enabled.is_(True),
            )
        ).all()
    )


def rule_out(rule: AlertRule, skipped: str | None = None) -> dict[str, Any]:
    return {
        "id": rule.id,
        "kind": rule.kind,
        "name": rule.name,
        "comparison": rule.comparison,
        "threshold": rule.threshold,
        "window": rule.window,
        "enabled": rule.enabled,
        "has_webhook": bool(rule.webhook_url),
        "state": rule.state,
        "last_value": rule.last_value,
        "last_detail": rule.last_detail,
        "last_checked_at": rule.last_checked_at.isoformat() if rule.last_checked_at else None,
        "last_fired_at": rule.last_fired_at.isoformat() if rule.last_fired_at else None,
        "last_notified_at": (
            rule.last_notified_at.isoformat() if rule.last_notified_at else None
        ),
        # NULL = a workspace-level rule, measured across every project
        "scope": "project" if rule.project_id else "workspace",
        "project_id": rule.project_id,
        # kept from the project's `quotas.monthly_cost_alert_usd` (§11.3)
        "budget": is_budget_rule(rule),
        "created_by": rule.created_by,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        **({"skipped": skipped} if skipped else {}),
    }
