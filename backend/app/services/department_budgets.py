"""Department budgets, budget allocation and the three cost views (projects §11.3).

Budgets are **project** budgets: a project's `quotas.monthly_budget_usd` is what the
gate (`project_quotas.assert_budget`) compares the project's month-to-date spend
with. There is no per-person budget. A department adds two things on top:

* **An allocation ceiling.** `departments.settings.budget.monthly_budget_usd`, set
  by a platform administrator (`PUT /api/departments/{id}/budget`). It never blocks
  an operation by itself: it caps the *sum of the department's project budgets*
  (`check_allocation`). Every project of the department counts, archived ones too
  (their budget stays allocated until it is cleared). A write that would break the
  ceiling — raising a project's budget, moving a project with a budget in, lowering
  the department budget below what is allocated — is refused with 409
  (`department.budget_overallocated` / `department.budget_below_allocated`); a
  write that only lowers an allocation always passes, so a legacy over-allocated
  department can be brought back under its ceiling.
* **An overrun switch.** `departments.settings.budget.allow_overrun` (default off,
  platform administrators only). Off: no project of the department may turn its own
  switch on (409 `department.overrun_disabled`), and turning it off while projects
  have theirs on is refused with their list (409 `department.overrun_in_use`) rather
  than silently flipping them.

**Who allocates.** A department administrator of the project's department (or any
platform administrator) sets the project's budget and overrun switch
(`PUT /api/projects/{id}/budget`). A department with **no budget** has nothing to
allocate: its administrators cannot give a project a budget (409
`department.budget_unset`; lowering or clearing one still passes), only a platform
administrator can, unconstrained — the behaviour before department budgets
existed. A project with no department (the built-in `default`) is the platform's
alone, as before.

**Department alerts.** At the end of every alert evaluation pass
(`alerts.evaluate_rules`), each budgeted department with a project bound to that
workspace sums its projects' month-to-date spend (the gate's cached figure) and
records crossing 80 % / 100 % of its budget, once per UTC month per line. Notify
only — nothing is blocked. The state lives in one `platform_settings` row per
department (`budget.department.<id>`: the month, the lines fired, the last reading
and a short daily history for the trend), each crossing is journalled
(`department.budget.alert`), and the department's administrators and the platform
administrators read the firing lines from `GET /api/budgets/me`. Not alert rules:
a workspace-level rule is readable by every member of the workspace.

**The cost views** (`department_view`, `platform_view`) read spend the way the gate
does; AWS is the source of truth, the ledger only holds budgets and switches.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.ledger import Workspace
from app.models.platform_settings import PlatformSetting
from app.models.projects import Department, Project, ProjectWorkspace
from app.services import access
from app.services import project_quotas as pq
from app.services import projects as projects_svc

logger = logging.getLogger(__name__)

BUDGET_SETTING = "budget"
ALERT_FRACTIONS = (0.8, 1.0)
STATE_PREFIX = "budget.department."
HISTORY_DAYS = 62
_EPS = 1e-6


def _now() -> datetime:
    return pq._now()


def _month() -> str:
    return pq._month_start().strftime("%Y-%m")


# ── reading budgets ───────────────────────────────────────────────────────────


def department_budget(row: Department | None) -> dict[str, Any]:
    """`{monthly_budget_usd, allow_overrun}` of a department (None / False unset)."""
    settings = row.settings if row is not None and isinstance(row.settings, dict) else {}
    stored = settings.get(BUDGET_SETTING) if isinstance(settings.get(BUDGET_SETTING),
                                                        dict) else {}
    return {"monthly_budget_usd": pq._number(stored.get("monthly_budget_usd")),
            "allow_overrun": stored.get("allow_overrun") is True}


def project_budget(row: Project | None) -> float | None:
    value = pq._number(pq.project_values(row).get(pq.BUDGET_KEY))
    return float(value) if value is not None else None


def department_projects(db: Session, department_id: str) -> list[Project]:
    with project_context(None):
        return list(db.scalars(select(Project).where(
            Project.department_id == department_id).order_by(Project.name, Project.id)))


def allocated_usd(db: Session, department_id: str, *, exclude: str | None = None) -> float:
    """The sum of the department's project budgets (every project, archived too)."""
    return round(sum(project_budget(p) or 0.0 for p in department_projects(db, department_id)
                     if p.id != exclude), 4)


def department_summary(db: Session, department_id: str | None) -> dict[str, Any] | None:
    """The department's ceiling for a project view: budget, switch, allocated."""
    if not department_id:
        return None
    with project_context(None):
        row = db.get(Department, department_id)
    if row is None:
        return None
    budget = department_budget(row)
    allocated = allocated_usd(db, department_id)
    ceiling = budget["monthly_budget_usd"]
    return {"department_id": row.id, "name": row.name, **budget,
            "allocated_usd": allocated,
            "unallocated_usd": round(float(ceiling) - allocated, 4)
            if ceiling is not None else None}


# ── the allocation rules ──────────────────────────────────────────────────────


def _overallocated(department_id: str, budget: float, allocated: float,
                   requested: float) -> AppError:
    remaining = round(float(budget) - allocated, 4)
    return AppError(
        "department.budget_overallocated",
        f"department '{department_id}' has ${float(budget):g} a month to allocate and "
        f"${allocated:g} is already allocated to its other projects: "
        f"${requested:g} does not fit (${max(0.0, remaining):g} remains)",
        {"department_id": department_id, "budget_usd": float(budget),
         "allocated_usd": allocated, "requested_usd": requested,
         "remaining_usd": remaining}, status_code=409)


def check_allocation(
    db: Session, *, project_id: str, old_department: str | None,
    new_department: str | None, old_budget: float | None, new_budget: float | None,
    old_overrun: bool, new_overrun: bool, platform: bool,
) -> None:
    """Refuse a project change that breaks its (new) department's budget rules. Only
    what the change *adds* is checked: lowering or clearing a budget, or turning
    overrun off, always passes."""
    if not new_department:
        return  # no department: the platform's alone, no ceiling
    with project_context(None):
        dept = db.get(Department, new_department)
    if dept is None:
        return  # `projects._check_department` answers for an unknown one
    budget = department_budget(dept)
    moved = old_department != new_department
    if new_overrun and (moved or not old_overrun) and not budget["allow_overrun"]:
        raise AppError(
            "department.overrun_disabled",
            f"department '{new_department}' does not allow its projects to overrun their "
            "budget — a platform administrator turns that on for the department first",
            {"department_id": new_department, "project_id": project_id}, status_code=409)
    if new_budget is None:
        return
    grows = moved or old_budget is None or new_budget > old_budget + _EPS
    if not grows:
        return
    ceiling = budget["monthly_budget_usd"]
    if ceiling is None:
        if platform:
            return  # nothing to allocate from: platform administrators set it freely
        raise AppError(
            "department.budget_unset",
            f"department '{new_department}' has no monthly budget to allocate from: a "
            "platform administrator sets the department's budget first",
            {"department_id": new_department, "project_id": project_id}, status_code=409)
    others = allocated_usd(db, new_department, exclude=project_id)
    if others + new_budget > float(ceiling) + _EPS:
        raise _overallocated(new_department, float(ceiling), others, new_budget)


# ── writes ────────────────────────────────────────────────────────────────────


def _check_usd(value: Any, code: str) -> float | int | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, int | float)
            or value != value or not 0 <= value <= projects_svc.MAX_QUOTA_USD):
        raise AppError(code, f"monthly_budget_usd must be an amount from 0 to "
                       f"{projects_svc.MAX_QUOTA_USD} USD", {"key": "monthly_budget_usd"})
    return value


def _check_flag(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise AppError(code, "allow_overrun must be true or false", {"key": "allow_overrun"})
    return value


def update_project_budget(
    db: Session, identity: Any, row: Project, fields: dict[str, Any],
) -> Project:
    """Set / clear a project's monthly budget and its overrun switch: a platform
    administrator, or an administrator of the project's department. Journalled as
    `project.budget.update`; the alert rules follow."""
    if not projects_svc.can_manage(db, identity, row):
        raise AppError("project.setting_forbidden",
                       "a platform administrator or the project's department administrator "
                       "sets its budget", {"keys": sorted(fields)}, status_code=403)
    if row.status != projects_svc.STATUS_ACTIVE_PROJECT:
        raise AppError("project.archived", f"project '{row.id}' is archived",
                       {"project_id": row.id}, status_code=409)
    unknown = sorted(set(fields) - {"monthly_budget_usd", "allow_overrun"})
    if unknown:
        raise AppError("project.budget_invalid", f"unknown budget fields: {unknown}",
                       {"keys": unknown})
    old_budget, old_overrun = project_budget(row), pq.allow_overrun(row)
    new_budget = old_budget
    if "monthly_budget_usd" in fields:
        value = _check_usd(fields["monthly_budget_usd"], "project.budget_invalid")
        new_budget = float(value) if value is not None else None
    new_overrun = old_overrun
    if "allow_overrun" in fields:
        new_overrun = _check_flag(fields["allow_overrun"], "project.budget_invalid")
    check_allocation(db, project_id=row.id, old_department=row.department_id,
                     new_department=row.department_id, old_budget=old_budget,
                     new_budget=new_budget, old_overrun=old_overrun,
                     new_overrun=new_overrun, platform=access.can_manage_structure(identity))
    changed = []
    clean: dict[str, Any] = {}
    if new_budget != old_budget:
        stored = fields["monthly_budget_usd"]
        clean["quotas"] = {pq.BUDGET_KEY: stored}
        changed.append(f"monthly_budget_usd={stored if stored is not None else 'none'}")
    if new_overrun != old_overrun:
        clean[pq.OVERRUN_KEY] = True if new_overrun else None  # off is stored as absence
        changed.append(f"allow_overrun={'on' if new_overrun else 'off'}")
    if not changed:
        return row
    row.settings = projects_svc._merge_settings(row.settings or {}, clean) or None
    row.updated_at = datetime.now(UTC)
    db.flush()
    from app.services import alerts

    alerts.sync_budget_rule(db, row.id)
    pq.clear_spend_cache(row.id)
    projects_svc._journal(db, identity, "project.budget.update",
                          f"project:{row.id} {' '.join(changed)}")
    return row


def update_department_budget(
    db: Session, identity: Any, row: Department, fields: dict[str, Any],
) -> Department:
    """Set / clear a department's monthly budget and its overrun switch — platform
    administrators only. Journalled as `department.budget.update`."""
    if not access.can_manage_structure(identity):
        raise AppError("department.setting_forbidden",
                       "only a platform administrator sets a department's budget",
                       {"department_id": row.id}, status_code=403)
    unknown = sorted(set(fields) - {"monthly_budget_usd", "allow_overrun"})
    if unknown:
        raise AppError("department.budget_invalid", f"unknown budget fields: {unknown}",
                       {"keys": unknown})
    current = department_budget(row)
    budget = current["monthly_budget_usd"]
    if "monthly_budget_usd" in fields:
        budget = _check_usd(fields["monthly_budget_usd"], "department.budget_invalid")
    overrun = current["allow_overrun"]
    if "allow_overrun" in fields:
        overrun = _check_flag(fields["allow_overrun"], "department.budget_invalid")
    if budget is not None and budget != current["monthly_budget_usd"]:
        allocated = allocated_usd(db, row.id)
        if float(budget) + _EPS < allocated:
            raise AppError(
                "department.budget_below_allocated",
                f"department '{row.id}' has ${allocated:g} allocated to its projects: its "
                f"budget cannot be lowered to ${float(budget):g} until their budgets are",
                {"department_id": row.id, "budget_usd": float(budget),
                 "allocated_usd": allocated, "requested_usd": float(budget),
                 "remaining_usd": round(float(budget) - allocated, 4)}, status_code=409)
    if not overrun and current["allow_overrun"]:
        using = [p.id for p in department_projects(db, row.id) if pq.allow_overrun(p)]
        if using:
            raise AppError(
                "department.overrun_in_use",
                f"projects {using} of department '{row.id}' allow overrun: turn theirs off "
                "first", {"department_id": row.id, "projects": using}, status_code=409)
    changed = []
    if budget != current["monthly_budget_usd"]:
        changed.append(f"monthly_budget_usd={budget if budget is not None else 'none'}")
    if overrun != current["allow_overrun"]:
        changed.append(f"allow_overrun={'on' if overrun else 'off'}")
    if not changed:
        return row
    stored = {k: v for k, v in {"monthly_budget_usd": budget,
                                "allow_overrun": True if overrun else None}.items()
              if v is not None}
    settings = dict(row.settings) if isinstance(row.settings, dict) else {}
    if stored:
        settings[BUDGET_SETTING] = stored
    else:
        settings.pop(BUDGET_SETTING, None)
    row.settings = settings or None
    row.updated_at = datetime.now(UTC)
    if budget != current["monthly_budget_usd"]:
        _clear_state(db, row.id)  # a new ceiling: the lines are judged afresh
    db.flush()
    projects_svc._journal(db, identity, "department.budget.update",
                          f"department:{row.id} {' '.join(changed)}")
    return row


# ── department alerts (notify only) ───────────────────────────────────────────


def _state_row(db: Session, department_id: str) -> PlatformSetting | None:
    return db.get(PlatformSetting, f"{STATE_PREFIX}{department_id}")


def read_state(db: Session, department_id: str) -> dict[str, Any]:
    row = _state_row(db, department_id)
    try:
        doc = json.loads(row.value) if row is not None and row.value else {}
    except ValueError:
        doc = {}
    return doc if isinstance(doc, dict) else {}


def _write_state(db: Session, department_id: str, doc: dict[str, Any]) -> None:
    row = _state_row(db, department_id)
    if row is None:
        row = PlatformSetting(key=f"{STATE_PREFIX}{department_id}", updated_by="budget")
        db.add(row)
    row.value = json.dumps(doc, sort_keys=True)
    row.updated_at = datetime.now(UTC)


def _clear_state(db: Session, department_id: str) -> None:
    doc = read_state(db, department_id)
    if doc:
        doc["fired"] = {}
        _write_state(db, department_id, doc)


def department_spend(db: Session, department_id: str) -> tuple[float | None, list[dict]]:
    """The department's month-to-date spend: the sum of its projects' (the gate's
    cached figure). None when any project's spend is unreadable."""
    total: float | None = 0.0
    rows = []
    for project in department_projects(db, department_id):
        try:
            usd, detail, measured = pq.cached_spend(db, project.id)
        except Exception:  # noqa: BLE001 - a view never fails on one project
            logger.exception("spend of project %s unreadable", project.id)
            usd, detail, measured = None, "unreadable", None
        rows.append({"project": project, "spent_usd": usd, "detail": detail,
                     "measured_at": measured})
        total = None if total is None or usd is None else round(total + usd, 4)
    return total, rows


def _level(fraction: float) -> str:
    return f"{round(fraction * 100):d}"


def evaluate_department(db: Session, row: Department) -> dict[str, Any] | None:
    """Read one budgeted department's spend and record the lines it crossed this
    month (journalled once per line per month). None without a budget."""
    budget = department_budget(row)["monthly_budget_usd"]
    if budget is None:
        return None
    spent, _ = department_spend(db, row.id)
    month = _month()
    doc = read_state(db, row.id)
    if doc.get("month") != month:
        doc = {"month": month, "fired": {}, "history": doc.get("history") or {}}
    now = _now()
    newly = []
    if spent is not None:
        for fraction in ALERT_FRACTIONS:
            level = _level(fraction)
            if spent >= float(budget) * fraction and level not in doc["fired"]:
                doc["fired"][level] = now.isoformat()
                newly.append(level)
        history = dict(doc.get("history") or {})
        history[now.strftime("%Y-%m-%d")] = spent
        doc["history"] = dict(sorted(history.items())[-HISTORY_DAYS:])
    doc.update(last_spent_usd=spent, budget_usd=float(budget),
               last_checked_at=now.isoformat())
    _write_state(db, row.id, doc)
    from app.services.audit import record_structure_event

    for level in newly:
        record_structure_event(
            actor="budget", action="department.budget.alert", db=db,
            target=f"department:{row.id} level:{level}% spent:{spent:g} "
                   f"budget:{float(budget):g}")
    return {"department_id": row.id, "budget_usd": float(budget), "spent_usd": spent,
            "fired": sorted(doc["fired"], key=float), "newly": newly}


def evaluate_departments(db: Session, *, workspace_id: str | None = None) -> list[dict]:
    """Every budgeted active department (with a project bound to `workspace_id`, when
    given). Never raises: a department that cannot be read is skipped (logged)."""
    with project_context(None):
        rows = list(db.scalars(select(Department).where(Department.archived_at.is_(None))))
        if workspace_id is not None:
            bound = set(db.scalars(select(Project.department_id).join(
                ProjectWorkspace, ProjectWorkspace.project_id == Project.id).where(
                ProjectWorkspace.workspace_id == workspace_id)))
            rows = [r for r in rows if r.id in bound]
    out = []
    for row in rows:
        if department_budget(row)["monthly_budget_usd"] is None:
            continue
        try:
            result = evaluate_department(db, row)
        except Exception:  # noqa: BLE001 - one department never stops the pass
            logger.exception("department budget %s not evaluated", row.id)
            continue
        if result is not None:
            out.append(result)
    return out


def _alerts(db: Session, rows: list[Department]) -> list[dict[str, Any]]:
    month = _month()
    out = []
    for row in rows:
        budget = department_budget(row)["monthly_budget_usd"]
        doc = read_state(db, row.id)
        if budget is None or doc.get("month") != month:
            continue
        for level, fired_at in sorted((doc.get("fired") or {}).items(),
                                      key=lambda item: float(item[0])):
            out.append({"department_id": row.id, "name": row.name, "level": int(level),
                        "fired_at": fired_at, "spent_usd": doc.get("last_spent_usd"),
                        "budget_usd": float(budget)})
    return out


# ── the views ─────────────────────────────────────────────────────────────────


def _project_row(project: Project, spent: float | None) -> dict[str, Any]:
    budget = project_budget(project)
    overrun = pq.allow_overrun(project)
    block_at = round(budget * pq.block_fraction(overrun), 4) if budget is not None else None
    return {
        "project_id": project.id, "name": project.name, "status": project.status,
        "budget_usd": budget, "spent_usd": spent,
        "fraction": (1.0 if budget <= 0 else round(spent / budget, 4))
        if budget is not None and spent is not None else None,
        "allow_overrun": overrun, "block_at_usd": block_at,
        "exhausted": block_at is not None and spent is not None and spent >= block_at,
        "lines": pq.budget_lines(budget, overrun) if budget is not None else [],
        "basis": pq.budget_basis(project),
    }


def department_view(db: Session, row: Department) -> dict[str, Any]:
    """The department cost page: its budget, what is allocated / unallocated, the
    month-to-date spend (the sum of its projects', the gate's figure), each project's
    budget, spend, fraction and overrun switch, this month's alerts and the trend."""
    budget = department_budget(row)
    ceiling = budget["monthly_budget_usd"]
    spent, rows = department_spend(db, row.id)
    allocated = round(sum(project_budget(r["project"]) or 0.0 for r in rows), 4)
    state = read_state(db, row.id)
    now = _now()
    return {
        "department_id": row.id, "name": row.name, **budget,
        "allocated_usd": allocated,
        "unallocated_usd": round(float(ceiling) - allocated, 4) if ceiling is not None
        else None,
        "spent_usd": spent,
        "fraction": (1.0 if ceiling <= 0 else round(spent / ceiling, 4))
        if ceiling is not None and spent is not None else None,
        "alert_fractions": list(ALERT_FRACTIONS),
        "projects": [_project_row(r["project"], r["spent_usd"]) for r in rows],
        "alerts": _alerts(db, [row]),
        "trend": [{"date": d, "spent_usd": v} for d, v in
                  sorted((state.get("history") or {}).items())
                  if d.startswith(_month())],
        "month_start": pq._month_start(now).isoformat(),
        "reset_at": pq._next_month_start(now).isoformat(),
    }


def _workspace_platform_line(db: Session, ws: Workspace) -> dict[str, Any]:
    """One workspace's spend no project carries: the estimate's unattributed part
    (a single-project workspace's goes to that project, as the gate counts it) and
    the shared infrastructure the estimate cannot price at all."""
    from app.services import costs
    from app.services.observability import several_projects
    from app.services.workspace import context_for_workspace

    try:
        ctx = context_for_workspace(ws.id)
    except Exception as exc:  # noqa: BLE001
        return {"workspace_id": ws.id, "name": ws.name, "estimate_usd": None,
                "unattributed_usd": None, "components": _platform_components(db, ws, None),
                "detail": f"could not read workspace: {type(exc).__name__}"}
    components = _platform_components(db, ws, getattr(ctx, "resources", None))
    try:
        raw = costs.month_to_date_breakdown(db, ctx)
    except Exception as exc:  # noqa: BLE001
        return {"workspace_id": ws.id, "name": ws.name, "estimate_usd": None,
                "unattributed_usd": None, "components": components,
                "detail": f"could not estimate spend: {type(exc).__name__}"}
    several = several_projects(db, ws.id)
    return {"workspace_id": ws.id, "name": ws.name, "estimate_usd": raw["usd"],
            "unattributed_usd": raw["unattributed_usd"] if several else 0.0,
            "components": components,
            "detail": "month-to-date estimate from the price map"}


def _platform_components(db: Session, ws: Workspace, resources: Any) -> list[str]:
    """The shared infrastructure of a workspace that belongs to no project (named for
    the reader; idle and per-request fees are on the billing view's shared row)."""
    from app.models.ledger import ContextLayer

    res = resources if isinstance(resources, dict) else (
        ws.resources if isinstance(ws.resources, dict) else {})
    out = []
    if res.get("gateway_arn") or res.get("gateway_id"):
        out.append("shared_gateway")
    if res.get("memory_id") or res.get("memory_arn"):
        out.append("shared_memory")
    if res.get("user_pool_id"):
        out.append("cognito")
    with project_context(None):
        layer = db.scalars(select(ContextLayer).where(
            ContextLayer.workspace_id == ws.id)).first()
    if layer is not None:
        out.append("coa_runtime")
        if (layer.resources or {}).get("gateway"):
            out.append("coa_gateway")
    if res.get("skill_lab_worker_runtime_arn") or res.get("skill_lab_worker_runtime_id"):
        out.append("skill_lab_worker")
    return out


def platform_view(db: Session) -> dict[str, Any]:
    """The platform cost page: every department (budget, allocated, spend), every
    project's spend (the gate's figure, grouped under its department or none), and the
    platform / unattributed line per workspace."""
    with project_context(None):
        departments = list(db.scalars(select(Department).order_by(Department.name)))
        projects = list(db.scalars(select(Project).order_by(Project.name, Project.id)))
        workspaces = list(db.scalars(select(Workspace).order_by(Workspace.id)))
    spend: dict[str, float | None] = {}
    for project in projects:
        try:
            spend[project.id] = pq.cached_spend(db, project.id)[0]
        except Exception:  # noqa: BLE001
            logger.exception("spend of project %s unreadable", project.id)
            spend[project.id] = None
    out_departments = []
    for dept in departments:
        mine = [p for p in projects if p.department_id == dept.id]
        budget = department_budget(dept)
        values = [spend.get(p.id) for p in mine]
        spent = None if any(v is None for v in values) else round(sum(values), 4)
        allocated = round(sum(project_budget(p) or 0.0 for p in mine), 4)
        out_departments.append({
            "department_id": dept.id, "name": dept.name, **budget,
            "archived": dept.archived_at is not None,
            "allocated_usd": allocated, "spent_usd": spent,
            "projects": [_project_row(p, spend.get(p.id)) for p in mine]})
    unassigned = [p for p in projects if not p.department_id]
    lines = [_workspace_platform_line(db, ws) for ws in workspaces]
    platform_values = [line["unattributed_usd"] for line in lines]
    platform_usd = None if any(v is None for v in platform_values) else round(
        sum(platform_values), 4)
    projects_usd = None if any(v is None for v in spend.values()) else round(
        sum(spend.values()), 4)
    now = _now()
    return {
        "departments": out_departments,
        "unassigned_projects": [_project_row(p, spend.get(p.id)) for p in unassigned],
        "projects_spent_usd": projects_usd,
        "platform": {"spent_usd": platform_usd, "workspaces": lines},
        "total_usd": round(projects_usd + platform_usd, 4)
        if projects_usd is not None and platform_usd is not None else None,
        "alerts": _alerts(db, departments),
        "month_start": pq._month_start(now).isoformat(),
        "reset_at": pq._next_month_start(now).isoformat(),
    }


def me_view(db: Session, identity: Any) -> dict[str, Any]:
    """Which cost views the caller can open, and the department alerts firing for
    them: platform administrators (data) every department and the platform page; a
    steward the department pages (budgets are structure); a department
    administrator their departments'."""
    platform = access.has_data_bypass(identity)
    structure = access.can_manage_structure(identity)
    with project_context(None):
        if structure:
            rows = list(db.scalars(select(Department).where(
                Department.archived_at.is_(None)).order_by(Department.name)))
        else:
            managed = access.managed_departments(db, identity)
            rows = list(db.scalars(select(Department).where(
                Department.id.in_(sorted(managed))).order_by(Department.name))) \
                if managed else []
    return {"platform": platform,
            "departments": [{"id": r.id, "name": r.name} for r in rows],
            "alerts": _alerts(db, rows)}
