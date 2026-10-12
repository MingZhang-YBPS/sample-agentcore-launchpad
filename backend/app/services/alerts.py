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


def _cost_value(db: Session, workspace: WorkspaceContext) -> tuple[float | None, str]:
    try:
        usd = costs.month_to_date_usd(db, workspace)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not estimate spend: {type(exc).__name__}"
    return usd, "month-to-date estimate from the price map"


def read_value(
    rule: AlertRule, db: Session, workspace: WorkspaceContext
) -> tuple[float | None, str]:
    if rule.kind in ("error_rate", "latency_p95_ms"):
        return _dashboard_value(rule.kind, rule.window or "24h", db, workspace)
    if rule.kind == "online_quality":
        return _quality_value(db, workspace)
    if rule.kind == "cost_mtd_usd":
        return _cost_value(db, workspace)
    return None, f"unknown kind '{rule.kind}'"


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
    rules = db.scalars(
        select(AlertRule).where(AlertRule.workspace_id == workspace.id).order_by(
            AlertRule.created_at
        )
    ).all()
    now = datetime.now(UTC)
    evaluated: list[dict[str, Any]] = []
    for rule in rules:
        if not rule.enabled:
            evaluated.append(rule_out(rule, skipped="disabled"))
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
        "created_by": rule.created_by,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        **({"skipped": skipped} if skipped else {}),
    }
