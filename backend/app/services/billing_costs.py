"""Billing-basis cost per project (cost-tags design §1.4, §1.6, CT-2).

`services/costs.py` estimates spend from tokens × the price map. This module reads
what AWS actually billed, through the `launchpad:project` cost allocation tag:

* **activation** — AWS bills by a tag only once it is activated as a cost
  allocation tag in Billing (`ce:ListCostAllocationTags` reads the state; taking
  effect takes about 24 hours). A standalone account (or an organization's
  management account) activates with `ce:UpdateCostAllocationTagsStatus`; an
  organization member account cannot — the page shows what to ask the management
  account for instead.
* **the billing view** — `ce:GetCostAndUsage` grouped by `TAG:launchpad:project` and
  by service, daily (30 days) or monthly (6 months), filtered to the workspace's
  region (Cost Explorer is account-wide; two workspaces of one account each see
  their own region). Cost Explorer bills per request, so a result is cached 6 hours.
* **shared infrastructure** — resources tagged `launchpad:project=shared` (the
  workspace's gateways, the shared memory, the bootstrap Cognito pool) form their
  own row, allocated to projects by invocation volume (design D-4) and marked as
  allocated. Rows with no project tag are listed as untagged and allocated to no
  one: in a shared account they include resources the platform never created.
* **custom cost tags** — a project's `settings.cost_tags` (design §1.1, D-3): up to
  10 key/value pairs under the AWS tag rules, never `aws:` or `launchpad:` keys,
  which the platform writes on the project's resources next to its own set.
"""

import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.projects import Project
from app.services.workspace import WorkspaceContext

# ── the tag set (design §1.1) ─────────────────────────────────────────────────

TAG_PROJECT = "launchpad:project"
TAG_WORKSPACE = "launchpad:workspace"
TAG_AGENT = "launchpad:agent-id"
TAG_MANAGED_BY = "launchpad:managed-by"
TAG_DEPARTMENT = "launchpad:department"
# the value workspace-shared resources carry instead of a project id
SHARED_VALUE = "shared"

TAG_SET: tuple[dict[str, str], ...] = (
    {"key": TAG_PROJECT, "value": "project id", "role": "primary"},
    {"key": TAG_WORKSPACE, "value": "workspace id", "role": "dimension"},
    {"key": TAG_AGENT, "value": "agent id", "role": "dimension"},
    {"key": TAG_MANAGED_BY, "value": "launchpad", "role": "dimension"},
    {"key": TAG_DEPARTMENT, "value": "department id", "role": "dimension"},
)
CORE_KEYS = tuple(entry["key"] for entry in TAG_SET)

# ── custom cost tags (design §1.1, D-3) ───────────────────────────────────────

MAX_CUSTOM_TAGS = 10
RESERVED_PREFIXES = ("aws:", "launchpad:")
# the AWS tag rule: letters, digits, spaces and _ . : / = + - @
_TAG_CHARS = re.compile(r"^[\w\s.:/=+\-@]*$", re.UNICODE)
_KEY_MAX, _VALUE_MAX = 128, 256


def _invalid(message: str, **details: Any) -> AppError:
    return AppError("cost_tags.invalid", message, details, status_code=422)


def check_custom_tags(value: Any) -> dict[str, str]:
    """The project's custom cost tags, validated; raises 422 `cost_tags.invalid`."""
    if not isinstance(value, dict):
        raise _invalid("cost_tags must be an object of tag key → value")
    if len(value) > MAX_CUSTOM_TAGS:
        raise _invalid(f"at most {MAX_CUSTOM_TAGS} custom cost tags",
                       count=len(value), max=MAX_CUSTOM_TAGS)
    clean: dict[str, str] = {}
    lowered: set[str] = set()
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key or len(key) > _KEY_MAX:
            raise _invalid(f"a tag key is 1–{_KEY_MAX} characters", key=key)
        if key.lower().startswith(RESERVED_PREFIXES):
            raise _invalid(f"'{key}' is reserved: keys may not start with aws: or "
                           "launchpad: (the platform writes those)", key=key)
        if not isinstance(raw_value, str):
            raise _invalid(f"the value of '{key}' must be a string", key=key)
        tag_value = raw_value.strip()
        if len(tag_value) > _VALUE_MAX:
            raise _invalid(f"the value of '{key}' is at most {_VALUE_MAX} characters",
                           key=key)
        for text, what in ((key, "key"), (tag_value, "value")):
            if not _TAG_CHARS.match(text):
                raise _invalid(f"the {what} of '{key}' may use letters, digits, spaces "
                               "and _ . : / = + - @ only", key=key)
        if key.lower() in lowered:  # AWS keys are case-sensitive, Cost Explorer's not
            raise _invalid(f"'{key}' is given twice", key=key)
        lowered.add(key.lower())
        clean[key] = tag_value
    return clean


def custom_tags(row: Project | None) -> dict[str, str]:
    """A project's stored custom cost tags ({} when it has none)."""
    stored = (row.settings or {}).get("cost_tags") if row is not None else None
    return {str(k): str(v) for k, v in stored.items()} if isinstance(stored, dict) else {}


def update_custom_tags(db: Session, identity: Any, row: Project, tags: Any) -> Project:
    """Replace the project's custom cost tags (an empty object clears them)."""
    from app.services import projects as projects_svc

    if row.status != projects_svc.STATUS_ACTIVE_PROJECT:
        raise AppError("project.archived", f"project '{row.id}' is archived",
                       {"project_id": row.id}, status_code=409)
    clean = check_custom_tags(tags)
    if clean == custom_tags(row):
        return row
    merged = projects_svc._merge_settings(row.settings or {}, {"cost_tags": None})
    if clean:
        merged["cost_tags"] = clean
    row.settings = merged or None
    row.updated_at = datetime.now(UTC)
    db.flush()
    # a project setting: journalled as one (`project.update`), naming the keys only
    projects_svc._journal(db, identity, "project.update",
                          f"project:{row.id} cost_tags:{','.join(sorted(clean)) or '-'}")
    # the custom tags are on every resource of the project (cost-tags §1.3): re-tag,
    # started after the caller's commit (`cost_tags.start_queued`)
    from app.services import cost_tags

    cost_tags.schedule_retag(db, project_id=row.id, reason="project_update",
                             **projects_svc._retag_actor(identity))
    return row


def custom_keys_in_use(db: Session) -> list[str]:
    """Every custom key some project uses — what the activation list adds to the set."""
    with project_context(None):
        rows = db.scalars(select(Project)).all()
    keys = {key for row in rows for key in custom_tags(row)}
    return sorted(keys, key=str.lower)


# ── the 6-hour cache (Cost Explorer bills per request) ────────────────────────

BILLING_TTL_SECONDS = 6 * 3600.0
STATUS_TTL_SECONDS = 600.0
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: dict[str, threading.Lock] = {}


def _now() -> float:
    return time.time()


def reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _KEY_LOCKS.clear()


def _drop(prefix: str) -> None:
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if k.startswith(prefix)]:
            _CACHE.pop(key, None)


def _cached(key: str, ttl: float, force: bool,
            build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """TTL cache with per-key single-flight: concurrent misses build once."""

    def lookup() -> dict[str, Any] | None:
        hit = _CACHE.get(key)
        if hit and not force and _now() - hit[0] < ttl:
            return {**hit[1], "cache": {"hit": True, "age_seconds": round(_now() - hit[0], 1),
                                        "ttl_seconds": ttl}}
        return None

    if (fresh := lookup()) is not None:
        return fresh
    with _CACHE_LOCK:
        key_lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        if not force and (fresh := lookup()) is not None:
            return fresh
        value = build()
        with _CACHE_LOCK:
            _CACHE[key] = (_now(), value)
    return {**value, "cache": {"hit": False, "age_seconds": 0.0, "ttl_seconds": ttl}}


# ── clients (stubbed in tests) ────────────────────────────────────────────────


def ce_client(workspace: WorkspaceContext) -> Any:
    # Cost Explorer has one endpoint per partition (us-east-1 in `aws`); botocore
    # resolves it from any region, so the workspace's own context serves
    return workspace.client("ce")


def org_client(workspace: WorkspaceContext) -> Any:
    return workspace.client("organizations")


def _code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    return type(exc).__name__


def _message(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Message") or exc)
    return str(exc)


# ── activation (design §1.4-1) ────────────────────────────────────────────────

ACCOUNT_STANDALONE = "standalone"
ACCOUNT_MANAGEMENT = "management"
ACCOUNT_MEMBER = "member"
ACCOUNT_UNKNOWN = "unknown"


def account_type(workspace: WorkspaceContext) -> tuple[str, str | None]:
    """(standalone / management / member / unknown, the management account id).
    Unknown: no `organizations:DescribeOrganization` — activation is then attempted
    and its error, if any, explained."""
    try:
        org = org_client(workspace).describe_organization().get("Organization") or {}
    except (ClientError, BotoCoreError) as exc:
        if _code(exc) == "AWSOrganizationsNotInUseException":
            return ACCOUNT_STANDALONE, None
        return ACCOUNT_UNKNOWN, None
    management = org.get("MasterAccountId") or org.get("ManagementAccountId")
    if not management:
        return ACCOUNT_UNKNOWN, None
    if str(management) == str(workspace.account_id):
        return ACCOUNT_MANAGEMENT, str(management)
    return ACCOUNT_MEMBER, str(management)


def activation_status(
    db: Session, workspace: WorkspaceContext, *, force: bool = False,
) -> dict[str, Any]:
    """Each key of the tag set (plus every project's custom keys) with its cost
    allocation state: `active`, `inactive`, or `not_found` (Billing has not seen the
    key on any resource yet — it appears up to 24 hours after the first tagged one)."""
    keys = [*CORE_KEYS, *custom_keys_in_use(db)]

    def build() -> dict[str, Any]:
        kind, management = account_type(workspace)
        found: dict[str, dict[str, Any]] = {}
        error: dict[str, str] | None = None
        try:
            client = ce_client(workspace)
            token: str | None = None
            # TagKeys takes at most 100 keys per call; the set is far below that
            while True:
                args: dict[str, Any] = {"TagKeys": keys, "MaxResults": 100}
                if token:
                    args["NextToken"] = token
                page = client.list_cost_allocation_tags(**args)
                for tag in page.get("CostAllocationTags") or []:
                    found[str(tag.get("TagKey"))] = tag
                token = page.get("NextToken")
                if not token:
                    break
        except (ClientError, BotoCoreError) as exc:
            error = {"code": _code(exc), "message": _message(exc)}
        tags = []
        for key in keys:
            tag = found.get(key)
            status = (str(tag.get("Status") or "").lower() or "inactive") if tag else "not_found"
            tags.append({
                "key": key,
                "custom": key not in CORE_KEYS,
                "status": "unknown" if error else status,
                "last_updated": _iso(tag.get("LastUpdatedDate")) if tag else None,
                "last_used": _iso(tag.get("LastUsedDate")) if tag else None,
            })
        project_tag = next(t for t in tags if t["key"] == TAG_PROJECT)
        return {
            "account_id": workspace.account_id,
            "account_type": kind,
            # a member account's activation happens in its management account
            "can_activate": kind != ACCOUNT_MEMBER and error is None,
            "management_account_id": management if kind == ACCOUNT_MEMBER else None,
            "project_tag_active": project_tag["status"] == "active",
            "tags": tags,
            "error": error,
            "checked_at": datetime.now(UTC).isoformat(),
            # Billing takes about a day to apply an activation to new line items
            "activation_delay_hours": 24,
        }

    return _cached(f"status:{workspace.id}", STATUS_TTL_SECONDS, force, build)


def activate(
    db: Session, workspace: WorkspaceContext, keys: list[str] | None = None,
) -> dict[str, Any]:
    """Activate the tag set's inactive keys (or `keys`) as cost allocation tags.

    A member account cannot (409 `cost_tags.org_member`, with the management
    account to ask); a key Billing has not seen yet cannot be activated and is
    reported back, not failed on."""
    status = activation_status(db, workspace, force=True)
    if status["account_type"] == ACCOUNT_MEMBER:
        raise AppError(
            "cost_tags.org_member",
            "this account belongs to an AWS Organization: cost allocation tags are "
            "activated in the organization's management account (Billing → Cost "
            "allocation tags)",
            {"management_account_id": status["management_account_id"],
             "keys": [t["key"] for t in status["tags"] if t["status"] != "active"]},
            status_code=409)
    known = {t["key"]: t["status"] for t in status["tags"]}
    wanted = [k for k in (keys or list(known)) if k in known]
    unknown = sorted(set(keys or []) - set(known))
    if unknown:
        raise _invalid(f"not in the tag set: {unknown}", keys=unknown)
    todo = [k for k in wanted if known[k] == "inactive"]
    pending = [k for k in wanted if known[k] == "not_found"]
    errors: list[dict[str, str]] = []
    if todo:
        try:
            result = ce_client(workspace).update_cost_allocation_tags_status(
                CostAllocationTagsStatus=[{"TagKey": k, "Status": "Active"} for k in todo])
        except (ClientError, BotoCoreError) as exc:
            code = _code(exc)
            if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedException"):
                raise AppError(
                    "cost_tags.activation_denied",
                    "the workspace role may not activate cost allocation tags "
                    "(ce:UpdateCostAllocationTagsStatus); an organization member "
                    "account activates them in its management account",
                    {"code": code, "message": _message(exc)}, status_code=403) from exc
            raise AppError("cost_tags.activation_failed", _message(exc),
                           {"code": code}, status_code=502) from exc
        errors = [{"key": str(e.get("TagKey")), "code": str(e.get("Code")),
                   "message": str(e.get("Message"))} for e in result.get("Errors") or []]
    failed = {e["key"] for e in errors}
    _drop(f"status:{workspace.id}")
    return {
        "activated": [k for k in todo if k not in failed],
        "already_active": [k for k in wanted if known[k] == "active"],
        # Billing has not seen these on a resource yet: activate again after a day
        "not_found": pending,
        "errors": errors,
        "status": activation_status(db, workspace, force=True),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# ── the billing view (design §1.4-2, -3) ──────────────────────────────────────

GRANULARITIES = {"daily": ("DAILY", 30), "monthly": ("MONTHLY", 6)}
DEFAULT_GRANULARITY = "daily"
METRIC = "UnblendedCost"


def period(granularity: str, today: date | None = None) -> tuple[str, str]:
    """[start, end) for the view: the last 30 days (through today), or the first of
    the month five months back through today (the current month is partial)."""
    today = today or datetime.now(UTC).date()
    end = today + timedelta(days=1)
    if granularity == "monthly":
        year, month = today.year, today.month - (GRANULARITIES["monthly"][1] - 1)
        while month <= 0:
            month += 12
            year -= 1
        return date(year, month, 1).isoformat(), end.isoformat()
    return (end - timedelta(days=GRANULARITIES["daily"][1])).isoformat(), end.isoformat()


def _tag_value(key: str) -> str:
    """`launchpad:project$alpha` → `alpha`; `launchpad:project$` (untagged) → ``."""
    _, _, value = key.partition("$")
    return value


def fetch_cost_and_usage(
    workspace: WorkspaceContext, *, start: str, end: str, granularity: str,
) -> list[dict[str, Any]]:
    """Every `ResultsByTime` entry for the window, all pages."""
    client = ce_client(workspace)
    args: dict[str, Any] = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": GRANULARITIES[granularity][0],
        "Metrics": [METRIC],
        "GroupBy": [{"Type": "TAG", "Key": TAG_PROJECT},
                    {"Type": "DIMENSION", "Key": "SERVICE"}],
        "Filter": {"Dimensions": {"Key": "REGION", "Values": [workspace.region]}},
    }
    results: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(50):  # a 30-day × service window is a handful of pages
        page = client.get_cost_and_usage(**args, **({"NextPageToken": token} if token else {}))
        results.extend(page.get("ResultsByTime") or [])
        token = page.get("NextPageToken")
        if not token:
            break
    return results


def _amount(group: dict[str, Any]) -> tuple[float, str]:
    metric = (group.get("Metrics") or {}).get(METRIC) or {}
    try:
        return float(metric.get("Amount") or 0), str(metric.get("Unit") or "USD")
    except (TypeError, ValueError):
        return 0.0, "USD"


def invocation_weights(db: Session, workspace: WorkspaceContext) -> dict[str, int]:
    """Invocations per project over the last 30 days — the D-4 allocation key."""
    from app.services import costs

    return costs.invocations_by_project(db, workspace)


def _allocate(shared: float, weights: dict[str, int]) -> dict[str, float]:
    total = sum(w for w in weights.values() if w > 0)
    if shared <= 0 or total <= 0:
        return {}
    return {pid: round(shared * w / total, 6) for pid, w in weights.items() if w > 0}


def billing_report(
    db: Session,
    workspace: WorkspaceContext,
    *,
    granularity: str = DEFAULT_GRANULARITY,
    force: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """What AWS billed in the workspace's region, per `launchpad:project` and service.

    The cached entry is workspace-wide; `project` narrows it after the cache to that
    project's row (with its allocated share of the shared row) and hides the
    untagged one, like `costs.cost_report`. Not activated, the tag groups nothing:
    every dollar lands in the untagged row, and the response says why
    (`project_tag_active`)."""
    if granularity not in GRANULARITIES:
        raise AppError("costs.bad_granularity",
                       f"granularity must be one of {', '.join(GRANULARITIES)}",
                       status_code=422)
    start, end = period(granularity)

    def build() -> dict[str, Any]:
        try:
            results = fetch_cost_and_usage(workspace, start=start, end=end,
                                           granularity=granularity)
        except (ClientError, BotoCoreError) as exc:
            code = _code(exc)
            status = 403 if "AccessDenied" in code else 502
            raise AppError("costs.billing_unavailable",
                           f"Cost Explorer could not be read: {_message(exc)}",
                           {"code": code}, status_code=status) from exc
        try:
            weights = invocation_weights(db, workspace)
            weights_error = None
        except Exception as exc:  # noqa: BLE001 - the billed rows still stand
            weights, weights_error = {}, type(exc).__name__
        currency = "USD"
        projects: dict[str, dict[str, Any]] = {}
        shared_services: dict[str, float] = {}
        untagged_services: dict[str, float] = {}
        series: list[dict[str, Any]] = []
        estimated = False
        for entry in results:
            point: dict[str, Any] = {
                "start": (entry.get("TimePeriod") or {}).get("Start"),
                "end": (entry.get("TimePeriod") or {}).get("End"),
                "estimated": bool(entry.get("Estimated")),
                "by_project": {}, "shared_usd": 0.0, "untagged_usd": 0.0,
            }
            estimated = estimated or point["estimated"]
            for group in entry.get("Groups") or []:
                keys = group.get("Keys") or ["", ""]
                tag = _tag_value(str(keys[0]))
                service = str(keys[1]) if len(keys) > 1 else "—"
                usd, currency = _amount(group)
                if not usd:
                    continue
                if tag == SHARED_VALUE:
                    shared_services[service] = shared_services.get(service, 0.0) + usd
                    point["shared_usd"] += usd
                elif not tag:
                    untagged_services[service] = untagged_services.get(service, 0.0) + usd
                    point["untagged_usd"] += usd
                else:
                    bucket = projects.setdefault(tag, {"project_id": tag, "services": {}})
                    bucket["services"][service] = bucket["services"].get(service, 0.0) + usd
                    point["by_project"][tag] = point["by_project"].get(tag, 0.0) + usd
            point["by_project"] = {k: round(v, 6) for k, v in point["by_project"].items()}
            point["shared_usd"] = round(point["shared_usd"], 6)
            point["untagged_usd"] = round(point["untagged_usd"], 6)
            series.append(point)
        shared_total = round(sum(shared_services.values()), 6)
        allocation = _allocate(shared_total, weights)
        rows = []
        for pid in sorted(set(projects) | set(allocation)):
            services = (projects.get(pid) or {}).get("services") or {}
            tagged = round(sum(services.values()), 6)
            rows.append({
                "project_id": pid,
                "tagged_usd": tagged,
                "allocated_shared_usd": allocation.get(pid, 0.0),
                "total_usd": round(tagged + allocation.get(pid, 0.0), 6),
                "invocations": weights.get(pid, 0),
                "services": _services(services),
            })
        return {
            "basis": "billing",
            "granularity": granularity,
            "start": start,
            "end": end,
            "region": workspace.region,
            "currency": currency,
            "generated_at": datetime.now(UTC).isoformat(),
            # Cost Explorer marks the current period's figures estimated until billed
            "estimated": estimated,
            "by_project": rows,
            "shared": {
                "usd": shared_total,
                "allocation": "invocations",
                "allocated": bool(allocation),
                "weights": {pid: w for pid, w in weights.items() if w > 0},
                "weights_error": weights_error,
                "services": _services(shared_services),
            },
            "untagged": {
                "usd": round(sum(untagged_services.values()), 6),
                "services": _services(untagged_services),
            },
            "series": series,
        }

    raw = _cached(f"billing:{workspace.id}:{granularity}:{start}", BILLING_TTL_SECONDS,
                  force, build)
    return _narrow(raw, project)


def _services(services: dict[str, float]) -> list[dict[str, Any]]:
    return sorted(({"service": k, "usd": round(v, 6)} for k, v in services.items()),
                  key=lambda row: row["usd"], reverse=True)


def _narrow(raw: dict[str, Any], project: str | None) -> dict[str, Any]:
    rows = raw["by_project"]
    if project is not None:
        rows = [row for row in rows if row["project_id"] == project]
    total = sum(row["total_usd"] for row in rows)
    if project is None:
        # the shared row's unallocated part, and the untagged row, still cost money
        unallocated = raw["shared"]["usd"] - sum(r["allocated_shared_usd"] for r in rows)
        total += max(0.0, unallocated) + raw["untagged"]["usd"]
    out = {
        **raw,
        "by_project": sorted(rows, key=lambda row: row["total_usd"], reverse=True),
        "total_usd": round(total, 6),
    }
    if project is not None:
        out["project"] = project
        out["untagged"] = None
        out["series"] = [{**p, "by_project": {k: v for k, v in p["by_project"].items()
                                              if k == project},
                          "shared_usd": None, "untagged_usd": None} for p in raw["series"]]
    return out


def project_month_to_date_usd(
    db: Session, workspace: WorkspaceContext, project_id: str | None, *,
    force: bool = False,
) -> float:
    """This month's billed spend of one project in the workspace's region: its tagged
    rows plus its allocated share of the shared row (None = every project's and the
    shared row, the single-project workspace's whole platform spend). The untagged
    row counts for no one."""
    today = datetime.now(UTC).date()

    def build() -> dict[str, Any]:
        start = today.replace(day=1).isoformat()
        end = (today + timedelta(days=1)).isoformat()
        results = fetch_cost_and_usage(workspace, start=start, end=end,
                                       granularity="monthly")
        by_project: dict[str, float] = {}
        shared = 0.0
        for entry in results:
            for group in entry.get("Groups") or []:
                tag = _tag_value(str((group.get("Keys") or [""])[0]))
                usd, _ = _amount(group)
                if tag == SHARED_VALUE:
                    shared += usd
                elif tag:
                    by_project[tag] = by_project.get(tag, 0.0) + usd
        try:
            weights = invocation_weights(db, workspace)
        except Exception:  # noqa: BLE001
            weights = {}
        allocation = _allocate(shared, weights)
        return {
            "shared": round(shared, 6),
            "by_project": {pid: round(by_project.get(pid, 0.0) + allocation.get(pid, 0.0), 6)
                           for pid in set(by_project) | set(allocation)},
            "tagged": {pid: round(v, 6) for pid, v in by_project.items()},
        }

    raw = _cached(f"billing-mtd:{workspace.id}:{today.isoformat()}", BILLING_TTL_SECONDS,
                  force, build)
    if project_id is None:
        return round(sum(raw["tagged"].values()) + raw["shared"], 6)
    return float(raw["by_project"].get(project_id, 0.0))
