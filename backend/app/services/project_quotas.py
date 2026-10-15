"""Project quotas and budgets (projects design §11.3, G11-quota).

`projects.settings.quotas` holds a project's caps (keys and bounds in
`services/projects.QUOTA_COUNTS` / `QUOTA_USD`); `departments.settings.quotas` has
the same shape and caps every project of the department — the limit that applies is
the smaller of the two, and a project value may not exceed its department's.

| quota                      | enforced at                              | over the limit      |
|----------------------------|------------------------------------------|---------------------|
| `max_agents`               | agent creation                           | 409 quota_exceeded  |
| `max_api_keys`             | API key creation (enabled keys count)    | 409 quota_exceeded  |
| `max_concurrent_eval_runs` | the evaluation run queue                 | queued, not refused |
| `eval_run_max_usd`         | a run's cost estimate                    | 409 quota_exceeded  |
| `eval_monthly_usd_cap`     | this month's evaluation spend + estimate | 409 quota_exceeded  |
| `monthly_cost_alert_usd`   | a project budget alert rule (`alerts`)   | alert, never blocks |
| `v1_rate_per_minute`       | `/v1` project window (`api_keys`)        | 429                 |

A project without quotas (every project until someone sets one, so every
single-project workspace) meets none of these checks. Spend comes from estimates and
the price map with minutes of delay: the money caps are soft, as the design says.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.ledger import Agent, ApiKey
from app.models.projects import Department, Project
from app.services import access
from app.services import api_keys as api_keys_service
from app.services import projects as projects_svc

# the order the view lists them in, and how each is enforced
ENFORCEMENT: dict[str, str] = {
    "max_agents": "block",
    "max_api_keys": "block",
    "max_concurrent_eval_runs": "queue",
    "eval_run_max_usd": "block",
    "eval_monthly_usd_cap": "block",
    "monthly_cost_alert_usd": "alert",
    api_keys_service.PROJECT_RATE_KEY: "throttle",
}
_ACTIVE_RUN_STATUSES = ("queued", "invoking", "waiting", "evaluating")


def _stored(settings: Any) -> dict[str, Any]:
    quotas = settings.get("quotas") if isinstance(settings, dict) else None
    return dict(quotas) if isinstance(quotas, dict) else {}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def project_values(row: Project | None) -> dict[str, Any]:
    """The project's own quotas (what an administrator stored)."""
    return _stored(row.settings) if row is not None else {}


def department_caps(db: Session, row: Project | None) -> dict[str, Any]:
    """The project's department's caps (`departments.settings.quotas`), or {}."""
    if row is None or not row.department_id:
        return {}
    department = db.get(Department, row.department_id)
    return _stored(department.settings) if department is not None else {}


def _effective(project: dict[str, Any], caps: dict[str, Any], key: str) -> int | float | None:
    values = [v for v in (_number(project.get(key)), _number(caps.get(key))) if v is not None]
    return min(values) if values else None


def limit_of(db: Session, project_id: str | None, key: str) -> int | float | None:
    """The limit that applies to `key` in this project, or None (no cap)."""
    if not project_id:
        return None
    with project_context(None):
        row = db.get(Project, project_id)
        return _effective(project_values(row), department_caps(db, row), key)


# ── usage ─────────────────────────────────────────────────────────────────────


def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def agents_used(db: Session, project_id: str) -> int:
    """Every live agent of the project, in all of its workspaces."""
    with project_context(None):
        return int(db.execute(select(func.count()).select_from(Agent).where(
            Agent.project_id == project_id, Agent.status != "deleted")).scalar_one())


def api_keys_used(db: Session, project_id: str) -> int:
    """The project's enabled keys (a disabled key frees its slot)."""
    with project_context(None):
        return int(db.execute(select(func.count()).select_from(ApiKey).where(
            ApiKey.project_id == project_id, ApiKey.enabled.is_(True))).scalar_one())


def eval_runs_active(db: Session, project_id: str) -> int:
    from app.evaluation.models import EvalRun

    with project_context(None):
        return int(db.execute(select(func.count()).select_from(EvalRun).where(
            EvalRun.project_id == project_id,
            EvalRun.status.in_(_ACTIVE_RUN_STATUSES))).scalar_one())


def run_spend_usd(run: Any) -> float:
    """What one run counts against the month: its measured agent spend once known,
    its estimate until then; a run that failed or was stopped with nothing measured
    counts nothing (nor does an unpriced one)."""
    actual = run.cost_actual or {}
    if actual.get("agent_usd") is not None:
        return float(actual["agent_usd"])
    if run.status in ("failed", "stopped"):
        return 0.0
    estimate = (run.cost_estimate or {}).get("total_usd")
    return float(estimate) if estimate is not None else 0.0


def eval_month_usd(db: Session, project_id: str, *, now: datetime | None = None) -> float:
    """This calendar month's (UTC) evaluation spend of the project, every workspace."""
    from app.evaluation.models import EvalRun

    with project_context(None):
        runs = db.scalars(select(EvalRun).where(
            EvalRun.project_id == project_id,
            EvalRun.created_at >= _month_start(now))).all()
    return round(sum(run_spend_usd(r) for r in runs), 4)


def _usage(db: Session, project_id: str, key: str) -> Any:
    if key == "max_agents":
        return agents_used(db, project_id)
    if key == "max_api_keys":
        return api_keys_used(db, project_id)
    if key == "max_concurrent_eval_runs":
        return eval_runs_active(db, project_id)
    if key == "eval_monthly_usd_cap":
        return eval_month_usd(db, project_id)
    return None


# ── enforcement ───────────────────────────────────────────────────────────────


def exceeded(key: str, used: Any, limit: Any, **extra: Any) -> AppError:
    return AppError(
        "project.quota_exceeded",
        f"this project's {key} quota is {limit:g} and {used:g} is already used"
        if isinstance(used, int | float) else f"this project's {key} quota is {limit:g}",
        {"quota": key, "used": used, "limit": limit, **extra}, status_code=409)


def assert_capacity(db: Session, project_id: str | None, key: str) -> None:
    """Before creating one more of `key`'s things (`max_agents`, `max_api_keys`)."""
    limit = limit_of(db, project_id, key)
    if limit is None or project_id is None:
        return
    used = _usage(db, project_id, key)
    if used >= limit:
        raise exceeded(key, used, limit)


def recheck_after_insert(db: Session, project_id: str | None, key: str) -> None:
    """After the new row is flushed in the caller's transaction: the count includes it,
    so two concurrent creators of the last slot cannot both keep theirs (SQLite
    serializes the writers; the second one counts the first's row). Rolls back the
    caller's transaction when over."""
    limit = limit_of(db, project_id, key)
    if limit is None or project_id is None:
        return
    used = _usage(db, project_id, key)
    if used > limit:
        db.rollback()
        raise exceeded(key, used - 1, limit)


def assert_eval_cost(db: Session, project_id: str | None, estimate_usd: float | None) -> None:
    """A new evaluation run against `eval_run_max_usd` and `eval_monthly_usd_cap`.
    An unpriced run (estimate None) passes the per-run cap; the monthly cap still
    refuses it once the month is already over."""
    if not project_id:
        return
    per_run = limit_of(db, project_id, "eval_run_max_usd")
    if per_run is not None and estimate_usd is not None and estimate_usd > per_run:
        raise AppError(
            "project.quota_exceeded",
            f"the estimated cost (${estimate_usd:g}) exceeds this project's per-run "
            f"evaluation limit (${per_run:g})",
            {"quota": "eval_run_max_usd", "used": estimate_usd, "limit": per_run},
            status_code=409)
    cap = limit_of(db, project_id, "eval_monthly_usd_cap")
    if cap is None:
        return
    spent = eval_month_usd(db, project_id)
    if spent + (estimate_usd or 0.0) > cap or (estimate_usd is None and spent >= cap):
        raise AppError(
            "project.quota_exceeded",
            f"this project has spent ${spent:g} on evaluations this month; with this run "
            f"(${estimate_usd or 0:g}) that exceeds its monthly cap (${cap:g})",
            {"quota": "eval_monthly_usd_cap", "used": spent, "limit": cap,
             "estimate": estimate_usd}, status_code=409)


def concurrent_eval_limit(project_id: str | None) -> int | None:
    """The queue's per-project slot count (`max_concurrent_eval_runs`), read through
    its own session (the queue has none). Never raises: no limit when unreadable."""
    if not project_id:
        return None
    db = SessionLocal()
    try:
        limit = limit_of(db, project_id, "max_concurrent_eval_runs")
    except Exception:  # noqa: BLE001 - the queue must keep going
        return None
    finally:
        db.close()
    return int(limit) if limit is not None else None


# ── the view and the update ───────────────────────────────────────────────────


def quotas_view(db: Session, row: Project) -> dict[str, Any]:
    """Usage against limits, one item per quota (set or not)."""
    from app.services import alerts

    project, caps = project_values(row), department_caps(db, row)
    budget = alerts.budget_rule(db, row.id)
    items = []
    for key, enforcement in ENFORCEMENT.items():
        item: dict[str, Any] = {
            "key": key,
            "unit": "usd" if key in projects_svc.QUOTA_USD else "count",
            "enforcement": enforcement,
            "project_value": project.get(key),
            "department_cap": caps.get(key),
            "limit": _effective(project, caps, key),
            "used": _usage(db, row.id, key),
        }
        if key == "monthly_cost_alert_usd" and budget is not None:
            # what the alert last measured (no billed read here)
            item.update(used=budget.last_value, state=budget.state,
                        checked_at=budget.last_checked_at.isoformat()
                        if budget.last_checked_at else None,
                        workspace_id=budget.workspace_id)
        if key == "monthly_cost_alert_usd":
            item["basis"] = budget_basis(row)
        items.append(item)
    return {"project_id": row.id, "department_id": row.department_id,
            "quotas": project, "department_caps": caps, "items": items,
            "budget_basis": budget_basis(row),
            "month_start": _month_start().isoformat()}


def update_quotas(db: Session, identity: Any, row: Project, quotas: dict[str, Any]) -> Project:
    """Set (a value) or clear (None) some of the project's quotas.

    An administrator or the project's department administrator (the route's `dadmin`
    overlay); neither may exceed the department's cap. The `/v1` window stays
    platform infrastructure (§10.7): a department administrator sets it only under a
    cap the platform gave the department."""
    if row.status != projects_svc.STATUS_ACTIVE_PROJECT:
        raise AppError("project.archived", f"project '{row.id}' is archived",
                       {"project_id": row.id}, status_code=409)
    clean = projects_svc._check_quotas(quotas)
    caps = department_caps(db, row)
    platform = access.can_manage_structure(identity)
    for key, value in clean.items():
        if value is None:
            continue
        cap = _number(caps.get(key))
        if (not platform and key == api_keys_service.PROJECT_RATE_KEY and cap is None):
            raise AppError("project.setting_forbidden",
                           f"only an administrator may set {key} without a department cap",
                           {"keys": [key]}, status_code=403)
        if cap is not None and value > cap:
            raise AppError("project.quota_above_department",
                           f"quotas.{key} ({value:g}) exceeds department "
                           f"'{row.department_id}' cap ({cap:g})",
                           {"quota": key, "value": value, "cap": cap,
                            "department_id": row.department_id}, status_code=422)
    merged = projects_svc._merge_settings(row.settings or {}, {"quotas": clean})
    if merged == (row.settings or {}):
        return row
    before = project_values(row)
    row.settings = merged or None
    row.updated_at = datetime.now(UTC)
    after = project_values(row)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    db.flush()
    from app.services import alerts

    alerts.sync_budget_rule(db, row.id)
    # its own structure action; the target names the project and the quotas changed
    projects_svc._journal(db, identity, "project.quotas.update",
                          f"project:{row.id} quotas:{','.join(changed)}")
    return row


# ── the budget's basis (cost-tags design §1.4-4, CT-2) ────────────────────────
# `settings.budget_basis`: what `monthly_cost_alert_usd` compares against — the
# token × price estimate (the default, and every project's until someone chooses)
# or the billed cost of the project's `launchpad:project` tag (Cost Explorer, with
# its shared-infrastructure allocation; billed figures trail by up to a day).

BASIS_ESTIMATE = "estimate"
BASIS_BILLING = "billing"
BUDGET_BASES = (BASIS_ESTIMATE, BASIS_BILLING)


def budget_basis(row: Project | None) -> str:
    stored = (row.settings or {}).get("budget_basis") if row is not None else None
    return stored if stored in BUDGET_BASES else BASIS_ESTIMATE


def update_budget_basis(db: Session, identity: Any, row: Project, basis: str) -> Project:
    """Choose the budget's basis; an administrator or the project's department
    administrator, like the quota it qualifies."""
    if row.status != projects_svc.STATUS_ACTIVE_PROJECT:
        raise AppError("project.archived", f"project '{row.id}' is archived",
                       {"project_id": row.id}, status_code=409)
    if basis not in BUDGET_BASES:
        raise AppError("project.setting_invalid",
                       f"budget_basis must be one of {', '.join(BUDGET_BASES)}",
                       {"key": "budget_basis"}, status_code=422)
    if basis == budget_basis(row):
        return row
    # the default is stored as its absence, so an untouched project stays untouched
    row.settings = projects_svc._merge_settings(
        row.settings or {}, {"budget_basis": basis if basis != BASIS_ESTIMATE else None}
    ) or None
    row.updated_at = datetime.now(UTC)
    db.flush()
    # it qualifies the budget quota: journalled with the quotas
    projects_svc._journal(db, identity, "project.quotas.update",
                          f"project:{row.id} budget_basis:{basis}")
    return row


def budget_spend(
    db: Session, workspace: Any, project_id: str, *, several: bool,
) -> tuple[float | None, str]:
    """The project budget rule's month-to-date value in one workspace, on the
    project's basis (`several` False: a single-project workspace, whose spend is all
    the project's). Never raises: (None, why) when the figure cannot be read."""
    from app.services import billing_costs, costs

    with project_context(None):
        basis = budget_basis(db.get(Project, project_id))
    scope = project_id if several else None
    if basis == BASIS_BILLING:
        try:
            usd = billing_costs.project_month_to_date_usd(db, workspace, scope)
        except Exception as exc:  # noqa: BLE001
            return None, f"could not read billed spend: {type(exc).__name__}"
        return usd, "month-to-date billed cost (Cost Explorer, launchpad:project)"
    try:
        usd = costs.month_to_date_usd(db, workspace, project=scope)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not estimate spend: {type(exc).__name__}"
    return usd, "month-to-date estimate from the price map"
