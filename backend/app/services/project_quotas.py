"""Project quotas and the monthly budget (projects design §11.3, G11-quota).

`projects.settings.quotas` holds a project's limits (keys and bounds in
`services/projects.QUOTA_COUNTS` / `QUOTA_USD`). A platform administrator sets them
all (`PUT /api/projects/{id}/quotas`, ADMIN with no department overlay); the monthly
budget and its overrun switch are also the project's department administrator's
(`PUT /api/projects/{id}/budget`, `services/department_budgets.py`). Every member —
and the department administrator — reads them with their usage. There is no
per-person budget anywhere: the project is the only budget subject.

| quota                      | enforced at                              | over the limit       |
|----------------------------|------------------------------------------|----------------------|
| `max_agents`               | agent creation                           | 409 quota_exceeded   |
| `max_api_keys`             | API key creation (enabled keys count)    | 409 quota_exceeded   |
| `max_concurrent_eval_runs` | the evaluation run queue                 | queued, not refused  |
| `monthly_budget_usd`       | every billable operation (`assert_budget`) | 409 budget_exhausted |
| `v1_rate_per_minute`       | `/v1` project window (`api_keys`)        | 429                  |

The monthly budget is the only money limit. Its month is the UTC calendar month:
it resets at 00:00 UTC on the 1st. Its spend is the project's month-to-date figure on
the project's budget basis (the price-map estimate across every workspace the project
is bound to, plus this month's evaluation judges; or the billed cost of the project's
`launchpad:project` tag) — read at most once per `SPEND_CACHE_SECONDS` per project.
Spend figures lag (Logs Insights minutes, Cost Explorer up to a day) and a turn or a
job already running is never cut off, so a project can overshoot its budget by what
it spends inside that lag; the gate is hard from the next operation on. An unreadable
spend never blocks (fail open, logged).

**Thresholds.** `settings.allow_overrun` (default off, absent = off):

| switch | alert rules (`alerts.sync_budget_rule`) | the gate blocks at |
|--------|-----------------------------------------|--------------------|
| off    | 80 %, 100 %                              | 100 %              |
| on     | 80 %, 105 % (`OVERRUN_ALERT_FRACTION`)   | 110 % (`OVERRUN_BLOCK_FRACTION`) |

The two overrun fractions are constants, not per-project settings. A project may
switch overrun on only while its department allows it (`department_budgets`).

**Departments.** A department's own monthly budget is an allocation ceiling only —
it never blocks an operation: the sum of its projects' budgets may not exceed it
(`department_budgets.check_allocation`), and its summed spend raises 80 % / 100 %
notices to its administrators (`department_budgets.evaluate_departments`).

A project without quotas (every project until an administrator sets one) meets none
of these checks.
"""

import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, next_step
from app.core.project_scope import project_context
from app.models.ledger import Agent, ApiKey
from app.models.projects import Project
from app.services import access
from app.services import api_keys as api_keys_service
from app.services import projects as projects_svc

logger = logging.getLogger(__name__)

BUDGET_KEY = "monthly_budget_usd"
# the order the view lists them in, and how each is enforced
ENFORCEMENT: dict[str, str] = {
    "max_agents": "block",
    "max_api_keys": "block",
    "max_concurrent_eval_runs": "queue",
    BUDGET_KEY: "budget",
    api_keys_service.PROJECT_RATE_KEY: "throttle",
}
# the money keys `monthly_budget_usd` replaced (migrate_budget_quotas)
RETIRED_QUOTAS = ("monthly_cost_alert_usd", "eval_monthly_usd_cap", "eval_run_max_usd")
# `settings.allow_overrun`: the project's overrun switch (absent = off)
OVERRUN_KEY = "allow_overrun"
WARN_FRACTION = 0.8
# overrun on: the second alert ("over by 5 %") and the block ("over by 10 %")
OVERRUN_ALERT_FRACTION = 1.05
OVERRUN_BLOCK_FRACTION = 1.10
# the alert fires at these fractions of the budget (alerts.BUDGET_RULES), by switch
ALERT_FRACTIONS = (WARN_FRACTION, 1.0)
OVERRUN_ALERT_FRACTIONS = (WARN_FRACTION, OVERRUN_ALERT_FRACTION)
SPEND_CACHE_SECONDS = 60.0
_ACTIVE_RUN_STATUSES = ("queued", "invoking", "waiting", "evaluating")


def _now() -> datetime:
    """The clock the budget month is read from (tests freeze it)."""
    return datetime.now(UTC)


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


def limit_of(db: Session, project_id: str | None, key: str) -> int | float | None:
    """The limit that applies to `key` in this project, or None (no limit)."""
    if not project_id:
        return None
    with project_context(None):
        return _number(project_values(db.get(Project, project_id)).get(key))


def allow_overrun(row: Project | None) -> bool:
    """The project's overrun switch (default off)."""
    settings = row.settings if row is not None and isinstance(row.settings, dict) else {}
    return settings.get(OVERRUN_KEY) is True


def allow_overrun_of(db: Session, project_id: str) -> bool:
    with project_context(None):
        return allow_overrun(db.get(Project, project_id))


def alert_fractions(overrun: bool) -> tuple[float, float]:
    return OVERRUN_ALERT_FRACTIONS if overrun else ALERT_FRACTIONS


def block_fraction(overrun: bool) -> float:
    return OVERRUN_BLOCK_FRACTION if overrun else 1.0


def budget_lines(budget: float, overrun: bool) -> list[dict[str, Any]]:
    """The budget's lines for the views: the alerts, then the block."""
    lines = [{"kind": "warn" if f == WARN_FRACTION else "alert", "fraction": f,
              "usd": round(float(budget) * f, 4)} for f in alert_fractions(overrun)]
    fraction = block_fraction(overrun)
    lines.append({"kind": "block", "fraction": fraction,
                  "usd": round(float(budget) * fraction, 4)})
    return lines


# ── the month ─────────────────────────────────────────────────────────────────


def _month_start(now: datetime | None = None) -> datetime:
    now = now or _now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(now: datetime | None = None) -> datetime:
    start = _month_start(now)
    return start.replace(year=start.year + 1, month=1) if start.month == 12 \
        else start.replace(month=start.month + 1)


# ── usage ─────────────────────────────────────────────────────────────────────


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


def run_judge_usd(run: Any) -> float:
    """What one evaluation run's judges cost (LLM-as-judge tokens and managed
    evaluations, EC-2) — the part of a run the price-map estimate of the project's
    agent traffic does not see. Measured once final, the estimate's until then; a
    run that failed or was stopped with nothing measured counts nothing."""
    actual = run.cost_actual or {}
    if actual.get("final"):
        return float(actual.get("judge_usd") or 0.0) + float(
            actual.get("managed_evaluations_usd") or 0.0)
    if run.status in ("failed", "stopped"):
        return 0.0
    estimate = run.cost_estimate or {}
    return float(estimate.get("judge_usd") or 0.0) + float(estimate.get("managed_usd") or 0.0)


def eval_judges_month_usd(db: Session, project_id: str, *, now: datetime | None = None) -> float:
    """This calendar month's (UTC) evaluation-judge spend of the project."""
    from app.evaluation.models import EvalRun

    with project_context(None):
        runs = db.scalars(select(EvalRun).where(
            EvalRun.project_id == project_id,
            EvalRun.created_at >= _month_start(now))).all()
    return round(sum(run_judge_usd(r) for r in runs), 4)


def _usage(db: Session, project_id: str, key: str) -> Any:
    if key == "max_agents":
        return agents_used(db, project_id)
    if key == "max_api_keys":
        return api_keys_used(db, project_id)
    if key == "max_concurrent_eval_runs":
        return eval_runs_active(db, project_id)
    return None


# ── count enforcement ─────────────────────────────────────────────────────────


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


# ── the monthly budget: spend ─────────────────────────────────────────────────


def _project_workspaces(db: Session, project_id: str) -> list[str]:
    from app.models.projects import ProjectWorkspace

    with project_context(None):
        return sorted(db.scalars(select(ProjectWorkspace.workspace_id).where(
            ProjectWorkspace.project_id == project_id)))


def project_spend(
    db: Session, project_id: str, *, workspace: Any = None,
) -> tuple[float | None, str]:
    """The project's month-to-date spend on its budget basis, across every workspace
    it is bound to (`workspace`, when given, is reused for its own id). On the
    estimate basis this month's evaluation judges are added (their tokens are not in
    the agent traffic the price map values). Never raises: (None, why) when any
    figure cannot be read."""
    from app.services.observability import several_projects
    from app.services.workspace import context_for_workspace

    total, parts = 0.0, []
    with project_context(None):
        estimate = budget_basis(db.get(Project, project_id)) == BASIS_ESTIMATE
    detail = "month-to-date estimate from the price map" if estimate else \
        "month-to-date billed cost (Cost Explorer, launchpad:project)"
    for ws_id in _project_workspaces(db, project_id):
        try:
            ctx = workspace if workspace is not None and getattr(
                workspace, "id", None) == ws_id else context_for_workspace(ws_id)
        except Exception as exc:  # noqa: BLE001
            return None, f"could not read workspace {ws_id}: {type(exc).__name__}"
        usd, why = budget_spend(db, ctx, project_id, several=several_projects(db, ws_id))
        if usd is None:
            return None, f"{ws_id}: {why}"
        detail = why
        total += usd
        parts.append(f"{ws_id} ${usd:.2f}")
    if estimate:
        judges = eval_judges_month_usd(db, project_id)
        if judges:
            total += judges
            parts.append(f"evaluation judges ${judges:.2f}")
    return round(total, 4), detail + (f" ({', '.join(parts)})" if len(parts) > 1 else "")


_CACHE: dict[tuple[str, str], tuple[float, float | None, str, str]] = {}
_CACHE_LOCK = threading.Lock()


def reset_cache_for_tests() -> None:
    clear_spend_cache()


def clear_spend_cache(project_id: str | None = None) -> None:
    """Forget the cached spend (one project's, or all): the next read measures."""
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if project_id is None or k[0] == project_id]:
            del _CACHE[key]


def cached_spend(
    db: Session, project_id: str, *, workspace: Any = None, fresh: bool = False,
) -> tuple[float | None, str, str]:
    """`project_spend` read at most once per `SPEND_CACHE_SECONDS` per project and
    month (a new month never sees last month's figure). (usd, detail, measured_at)."""
    month = _month_start().strftime("%Y-%m")
    key = (project_id, month)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None and not fresh and hit[0] > time.monotonic():
        return hit[1], hit[2], hit[3]
    usd, detail = project_spend(db, project_id, workspace=workspace)
    measured = _now().isoformat()
    with _CACHE_LOCK:
        for stale in [k for k in _CACHE if k[0] == project_id and k[1] != month]:
            del _CACHE[stale]
        _CACHE[key] = (time.monotonic() + SPEND_CACHE_SECONDS, usd, detail, measured)
    return usd, detail, measured


def budget_status(db: Session, project_id: str | None) -> dict[str, Any] | None:
    """The project's budget this month, or None without one: the budget, what is
    spent (None when unreadable), what remains, how full it is, and the reset date."""
    if not project_id:
        return None
    budget = limit_of(db, project_id, BUDGET_KEY)
    if budget is None:
        return None
    spent, detail, measured = cached_spend(db, project_id)
    now = _now()
    overrun = allow_overrun_of(db, project_id)
    block_at = round(float(budget) * block_fraction(overrun), 4)
    out: dict[str, Any] = {
        "project_id": project_id,
        "budget_usd": float(budget),
        "spent_usd": spent,
        "remaining_usd": round(max(0.0, float(budget) - spent), 4) if spent is not None
        else None,
        "fraction": (1.0 if budget <= 0 else round(spent / budget, 4))
        if spent is not None else None,
        # blocked: at the block line (100 %, or 110 % with overrun on)
        "exhausted": spent is not None and spent >= block_at,
        "allow_overrun": overrun,
        "block_at_usd": block_at,
        "lines": budget_lines(float(budget), overrun),
        "month_start": _month_start(now).isoformat(),
        "reset_at": _next_month_start(now).isoformat(),
        "measured_at": measured,
        "detail": detail,
        "basis": budget_basis_of(db, project_id),
    }
    return out


# ── the monthly budget: the gate ──────────────────────────────────────────────

_EXEMPT: ContextVar[bool] = ContextVar("budget_exempt", default=False)


@contextmanager
def budget_exempt() -> Iterator[None]:
    """A platform operation (an isolation-migration check, a job already past its
    start check) that the gate must not refuse mid-way."""
    token = _EXEMPT.set(True)
    try:
        yield
    finally:
        _EXEMPT.reset(token)


def budget_exhausted(status: dict[str, Any]) -> AppError:
    pid = status["project_id"]
    over = status.get("allow_overrun")
    line = (f"its ${status['budget_usd']:g} monthly budget plus the "
            f"{round((OVERRUN_BLOCK_FRACTION - 1) * 100):g} % overrun allowance"
            if over else f"its ${status['budget_usd']:g} monthly budget")
    return AppError(
        "project.budget_exhausted",
        f"project '{pid}' has spent ${status['spent_usd']:.2f} of {line}; billable "
        f"operations resume when the budget resets on {status['reset_at'][:10]} (UTC) "
        "or an administrator raises it",
        {"project_id": pid, "budget_usd": status["budget_usd"],
         "spent_usd": status["spent_usd"], "reset_at": status["reset_at"],
         "month_start": status["month_start"], "basis": status["basis"],
         "allow_overrun": bool(over),
         "block_at_usd": status.get("block_at_usd", status["budget_usd"])},
        status_code=409, next=[next_step("project.settings", project_id=pid)])


def assert_budget(db: Session, project_id: str | None) -> None:
    """Refuse a billable operation of a project whose month-to-date spend has reached
    its block line — `monthly_budget_usd`, or 110 % of it with the overrun switch on —
    (409 `project.budget_exhausted`). Nothing for a project without a budget, for
    work attributed to no project (platform / system calls), inside
    `budget_exempt()`, or when the spend cannot be read (fail open). A department
    budget never blocks here: it only caps what its projects may be allocated."""
    if not project_id or _EXEMPT.get():
        return
    try:
        status = budget_status(db, project_id)
    except Exception:  # noqa: BLE001 - a broken read never blocks work
        logger.exception("budget check of project %s failed; allowing", project_id)
        return
    if status is None:
        return
    if status["spent_usd"] is None:
        logger.warning("budget of project %s not measurable (%s); allowing",
                       project_id, status["detail"])
        return
    if status["exhausted"]:
        raise budget_exhausted(status)


def assert_project_budget(project_id: str | None) -> None:
    """`assert_budget` through its own session (callers without one: the invoke chain)."""
    if not project_id or _EXEMPT.get():
        return
    db = SessionLocal()
    try:
        assert_budget(db, project_id)
    finally:
        db.close()


def assert_agent_budget(agent: Any) -> None:
    """The invoke chain's gate: the agent's project's budget (a preset or any agent
    attributed to no project is exempt)."""
    assert_project_budget(getattr(agent, "project_id", None))


# ── the view and the update ───────────────────────────────────────────────────


def quotas_view(db: Session, row: Project) -> dict[str, Any]:
    """Usage against limits, one item per quota (set or not), and the budget."""
    from app.services import alerts

    project = project_values(row)
    budget = budget_status(db, row.id)
    rule = alerts.budget_rule(db, row.id) if budget is not None else None
    if budget is not None and budget["spent_usd"] is None and rule is not None \
            and rule.last_value is not None:
        # unreadable now: what the alert last measured
        budget.update(spent_usd=rule.last_value, measured_at=rule.last_checked_at.isoformat()
                      if rule.last_checked_at else None)
        budget.update(
            remaining_usd=round(max(0.0, budget["budget_usd"] - rule.last_value), 4),
            fraction=1.0 if budget["budget_usd"] <= 0
            else round(rule.last_value / budget["budget_usd"], 4),
            exhausted=rule.last_value >= budget["block_at_usd"])
    items = []
    for key, enforcement in ENFORCEMENT.items():
        item: dict[str, Any] = {
            "key": key,
            "unit": "usd" if key in projects_svc.QUOTA_USD else "count",
            "enforcement": enforcement,
            "project_value": project.get(key),
            "limit": _number(project.get(key)),
            "used": _usage(db, row.id, key),
        }
        if key == BUDGET_KEY:
            item["used"] = budget["spent_usd"] if budget is not None else None
            item["basis"] = budget_basis(row)
            if rule is not None:
                item.update(state=rule.state, workspace_id=rule.workspace_id)
        items.append(item)
    now = _now()
    from app.services import department_budgets

    return {"project_id": row.id, "department_id": row.department_id,
            "quotas": project, "items": items, "budget": budget,
            "budget_basis": budget_basis(row),
            "allow_overrun": allow_overrun(row),
            # the department's ceiling and switch (None without a department)
            "department_budget": department_budgets.department_summary(db, row.department_id),
            "month_start": _month_start(now).isoformat(),
            "reset_at": _next_month_start(now).isoformat()}


def _require_platform(identity: Any) -> None:
    if not access.can_manage_structure(identity):
        raise AppError("project.setting_forbidden",
                       "only a platform administrator sets a project's quotas and budget",
                       {"keys": ["quotas"]}, status_code=403)


def update_quotas(db: Session, identity: Any, row: Project, quotas: dict[str, Any]) -> Project:
    """Set (a value) or clear (None) some of the project's quotas — a platform
    administrator's only (the route is ADMIN without a department overlay; checked
    again here)."""
    _require_platform(identity)
    if row.status != projects_svc.STATUS_ACTIVE_PROJECT:
        raise AppError("project.archived", f"project '{row.id}' is archived",
                       {"project_id": row.id}, status_code=409)
    clean = projects_svc._check_quotas(quotas)
    if BUDGET_KEY in clean:
        from app.services import department_budgets

        old_budget = _number(project_values(row).get(BUDGET_KEY))
        if _number(clean[BUDGET_KEY]) != old_budget:
            department_budgets.require_project_budget(db, identity, row.department_id)
        department_budgets.check_allocation(
            db, project_id=row.id, old_department=row.department_id,
            new_department=row.department_id,
            old_budget=old_budget,
            new_budget=_number(clean[BUDGET_KEY]), old_overrun=allow_overrun(row),
            new_overrun=allow_overrun(row))
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
    clear_spend_cache(row.id)
    # its own structure action; the target names the project and the quotas changed
    projects_svc._journal(db, identity, "project.quotas.update",
                          f"project:{row.id} quotas:{','.join(changed)}")
    return row


# ── the migration (one money limit) ───────────────────────────────────────────


def _settings_of(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def migrate_budget_quotas(bind: Any) -> dict[str, int]:
    """Fold the retired money quotas into `monthly_budget_usd`, at every startup.

    A project's budget becomes its `monthly_cost_alert_usd` if set, else its
    `eval_monthly_usd_cap` (an existing `monthly_budget_usd` wins over both); the
    retired keys — `eval_run_max_usd` too — are dropped from `settings.quotas`
    (`budget_basis` stays). Every department's `settings.quotas` (the retired
    department caps) is dropped. Idempotent: a re-run finds nothing to change."""
    from sqlalchemy import inspect, text

    tables = set(inspect(bind).get_table_names())
    counts = {"projects": 0, "departments": 0}
    with bind.begin() as conn:
        if "projects" in tables:
            for pid, raw in conn.execute(text("SELECT id, settings FROM projects")).all():
                settings = _settings_of(raw)
                quotas = settings.get("quotas") if settings else None
                if not isinstance(quotas, dict) or not set(RETIRED_QUOTAS) & set(quotas):
                    continue
                quotas = dict(quotas)
                old = {k: quotas.pop(k, None) for k in RETIRED_QUOTAS}
                if _number(quotas.get(BUDGET_KEY)) is None:
                    quotas.pop(BUDGET_KEY, None)
                    for source in ("monthly_cost_alert_usd", "eval_monthly_usd_cap"):
                        if _number(old[source]) is not None:
                            quotas[BUDGET_KEY] = old[source]
                            break
                settings = {**settings, "quotas": quotas} if quotas else {
                    k: v for k, v in settings.items() if k != "quotas"}
                conn.execute(text("UPDATE projects SET settings = :s WHERE id = :id"),
                             {"s": json.dumps(settings) if settings else None, "id": pid})
                counts["projects"] += 1
        if "departments" in tables and "settings" in {
                c["name"] for c in inspect(bind).get_columns("departments")}:
            for did, raw in conn.execute(text("SELECT id, settings FROM departments")).all():
                settings = _settings_of(raw)
                if not settings or "quotas" not in settings:
                    continue
                rest = {k: v for k, v in settings.items() if k != "quotas"}
                conn.execute(text("UPDATE departments SET settings = :s WHERE id = :id"),
                             {"s": json.dumps(rest) if rest else None, "id": did})
                counts["departments"] += 1
    return counts


# ── the budget's basis (cost-tags design §1.4-4, CT-2) ────────────────────────
# `settings.budget_basis`: what `monthly_budget_usd` compares against — the
# token × price estimate (the default, and every project's until someone chooses)
# or the billed cost of the project's `launchpad:project` tag (Cost Explorer, with
# its shared-infrastructure allocation; billed figures trail by up to a day).

BASIS_ESTIMATE = "estimate"
BASIS_BILLING = "billing"
BUDGET_BASES = (BASIS_ESTIMATE, BASIS_BILLING)


def budget_basis(row: Project | None) -> str:
    stored = (row.settings or {}).get("budget_basis") if row is not None else None
    return stored if stored in BUDGET_BASES else BASIS_ESTIMATE


def budget_basis_of(db: Session, project_id: str) -> str:
    with project_context(None):
        return budget_basis(db.get(Project, project_id))


def update_budget_basis(db: Session, identity: Any, row: Project, basis: str) -> Project:
    """Choose the budget's basis; a platform administrator's, like the budget."""
    _require_platform(identity)
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
    clear_spend_cache(row.id)
    # it qualifies the budget quota: journalled with the quotas
    projects_svc._journal(db, identity, "project.quotas.update",
                          f"project:{row.id} budget_basis:{basis}")
    return row


def budget_spend(
    db: Session, workspace: Any, project_id: str, *, several: bool,
) -> tuple[float | None, str]:
    """The project's month-to-date spend in one workspace, on the project's basis
    (`several` False: a single-project workspace, whose spend is all the project's).
    Never raises: (None, why) when the figure cannot be read."""
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
