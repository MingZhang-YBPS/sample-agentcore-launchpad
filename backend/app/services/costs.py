"""Spend attribution in dollars (roadmap T28).

The Observability console already estimates cost per model and per session
(`observability.estimate_cost`, priced from the `model_prices` map). What it could not
answer is the question an administrator actually asks: **who spent it** — which agent,
which person, how much of this month's budget is left.

Two dimensions, each derived from data the platform already has rather than a new
telemetry path:

* **by agent** — spans carry the runtime's service name, so one Logs Insights query
  grouped by service gives per-agent tokens, which the shared price map turns into
  dollars. A service name that matches no ledger agent is still reported (an imported or
  deleted agent spent real money).
* **by person** — spans do not carry a console identity, so attribution goes through the
  ledger: per-session tokens joined to `ChatSession.actor_id`. Sessions the console did
  not open (direct `/v1` traffic, evaluation runs) land under a single `—` bucket rather
  than being silently dropped, because an unattributed total that looks like zero is
  worse than one that says "unattributed".

* **by project** (design §10.8) — each agent row's project comes from, in order: the
  `launchpad.project` span resource attribute an isolated project's agents carry; the
  project's application inference profile when the span's model id is one (its
  `project_resources` row); the ledger agent's `project_id`. Cost Explorer's view of
  the `launchpad:project` tag (runtimes, harnesses, inference profiles) is the
  independent check of the same split.

Every figure is an **estimate** and says so: prices are a config map matched by
substring, a model missing from it contributes tokens but no dollars, and the response
names those models so the number is never quietly wrong.
"""

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.project_scope import project_context
from app.models.ledger import Agent, ChatSession
from app.models.projects import PROJECT_ID_PATTERN
from app.services import inference_profiles
from app.services.observability import (
    _IS_LLM_FIELDS,
    SPAN_PROJECT_ATTR,
    SPANS_SOURCE,
    cached,
    estimate_cost,
    logs_client,
    run_insights_queries,
)
from app.services.workspace import WorkspaceContext

# The same whitelist the observability console uses, plus the month-to-date window the
# budget is expressed against.
RANGES: dict[str, int] = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}
DEFAULT_RANGE = "24h"
_PROJECT_RE = re.compile(PROJECT_ID_PATTERN)


def q_cost_by_service() -> str:
    """Tokens per runtime service name and span project — the per-agent dimension
    (`span_project` is "" for the spans that name no project)."""
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano)
| {_IS_LLM_FIELDS}
| fields coalesce(resource.attributes.{SPAN_PROJECT_ATTR}, "") as span_project
| stats sum(llm_in) as tokens_in, sum(llm_out) as tokens_out,
        sum(llm_cache_read) as cache_read, sum(llm_cache_write) as cache_write,
        sum(is_llm) as llm_calls, latest(telemetry_model) as model,
        count_distinct(attributes.session.id) as sessions
  by resource.attributes.service.name as service, span_project
| sort tokens_out desc
| limit 200
"""


def q_cost_by_session() -> str:
    """Tokens per session — joined to `ChatSession.actor_id` for the per-person view."""
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| {_IS_LLM_FIELDS}
| stats sum(llm_in) as tokens_in, sum(llm_out) as tokens_out,
        sum(llm_cache_read) as cache_read, sum(llm_cache_write) as cache_write,
        latest(telemetry_model) as model,
        latest(resource.attributes.{SPAN_PROJECT_ATTR}) as span_project
  by attributes.session.id as session_id
| limit 1000
"""


def _num(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_project(row: dict[str, Any], agent: Agent | None) -> tuple[str | None, str | None]:
    """(project id, how it was known) for one aggregate row: the span attribute, else
    the project's inference profile the model id names, else the ledger agent."""
    span = row.get("span_project")
    if isinstance(span, str) and _PROJECT_RE.fullmatch(span):
        return span, "span"
    profile = inference_profiles.profile_for_model(row.get("model"))
    if profile is not None:
        return profile.project_id, "inference_profile"
    if agent is not None and agent.project_id:
        return agent.project_id, "agent"
    return None, None


def _priced(row: dict[str, Any], prices: dict[str, Any]) -> tuple[float, float | None]:
    """(tokens, dollars-or-None) for one aggregate row."""
    tokens_in = _num(row, "tokens_in")
    tokens_out = _num(row, "tokens_out")
    cache_read = _num(row, "cache_read")
    cache_write = _num(row, "cache_write")
    usd = estimate_cost(
        row.get("model"), tokens_in, tokens_out, cache_read, cache_write, prices=prices
    )
    return tokens_in + tokens_out + cache_read + cache_write, usd


def cost_report(
    db: Session,
    workspace: WorkspaceContext,
    *,
    range_key: str = DEFAULT_RANGE,
    force: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """Estimated spend for the window, broken down by agent, by person and by model.

    Cached on the same terms as the observability views (Logs Insights is billed per
    scan), keyed by workspace and range. The cached entry is workspace-wide — built
    across projects, every per-agent and per-session row carrying its project — and
    `project` (design §5.9) narrows it after the cache: that project's agents, and
    the people of that project's sessions. The unattributed rows (a service no agent
    claims, the `—` bucket) belong to no project and appear only without one.
    """
    hours = RANGES.get(range_key, RANGES[DEFAULT_RANGE])

    def build() -> dict[str, Any]:
        prices = get_settings().model_prices or {}
        logs = logs_client(workspace)
        results = run_insights_queries(
            {"by_service": q_cost_by_service(), "by_session": q_cost_by_session()},
            hours=hours,
            logs=logs,
        )

        from app.services.observability import build_agent_resolver

        with project_context(None):  # attribution sees every project's rows
            agents_by_name = {
                row.name: row
                for row in db.scalars(
                    select(Agent).where(Agent.workspace_id == workspace.id)
                ).all()
            }
        # service.name carries the endpoint suffix (`<runtime>.DEFAULT`) and the runtime
        # id, not the ledger name — resolve it the way the observability views do, and
        # keep the exact-name match for log sources registered under their own name.
        resolve = build_agent_resolver(db, workspace.id, all_projects=True)
        # one row per (service, project): a runtime's stamped and unstamped spans (a
        # publish before and after its project was isolated) group apart in the query
        merged: dict[tuple[str, str | None], dict[str, Any]] = {}
        for row in results.get("by_service") or []:
            service = row.get("service") or "—"
            tokens, usd = _priced(row, prices)
            agent = agents_by_name.get(service) or resolve(service)
            project_id, source = _row_project(row, agent)
            entry = merged.get((service, project_id))
            if entry is None:
                merged[(service, project_id)] = {
                    "service": service,
                    "agent_id": agent.id if agent else None,
                    "display_name": (agent.spec or {}).get("display_name") if agent else None,
                    "known": agent is not None,
                    "project_id": project_id,
                    "project_source": source,
                    "tokens": int(tokens),
                    "llm_calls": int(_num(row, "llm_calls")),
                    "sessions": int(_num(row, "sessions")),
                    "est_cost_usd": usd,
                    "_unpriced": str(row["model"]) if usd is None and row.get("model") else None,
                }
                continue
            entry["tokens"] += int(tokens)
            entry["llm_calls"] += int(_num(row, "llm_calls"))
            entry["sessions"] += int(_num(row, "sessions"))
            if usd is not None:
                entry["est_cost_usd"] = round((entry["est_cost_usd"] or 0.0) + usd, 6)
            elif row.get("model") and not entry["_unpriced"]:
                entry["_unpriced"] = str(row["model"])
        by_agent = list(merged.values())

        # session → (actor, project), for the rows the console opened
        session_rows = results.get("by_session") or []
        ids = [str(row.get("session_id")) for row in session_rows if row.get("session_id")]
        chats: dict[str, tuple[str, str | None]] = {}
        if ids:
            with project_context(None):
                for chat in db.scalars(
                    select(ChatSession).where(
                        ChatSession.workspace_id == workspace.id,
                        ChatSession.session_id.in_(ids),
                    )
                ).all():
                    chats[chat.session_id] = (chat.actor_id, chat.project_id)
        sessions: list[dict[str, Any]] = []
        for row in session_rows:
            tokens, usd = _priced(row, prices)
            # An unmatched session is real traffic with no console identity (/v1, an
            # evaluation replay). It gets its own bucket rather than vanishing.
            actor, pid = chats.get(str(row.get("session_id")), ("—", None))
            if pid is None:  # no console identity, but the spans may name the project
                pid, _ = _row_project(row, None)
            sessions.append({
                "actor": actor, "project_id": pid, "tokens": int(tokens), "usd": usd,
                "_unpriced": str(row["model"]) if usd is None and row.get("model") else None,
            })
        return {
            "range": range_key,
            "hours": hours,
            "generated_at": datetime.now(UTC).isoformat(),
            "by_agent": by_agent,
            "sessions": sessions,
        }

    return _report(cached(f"costs:{workspace.id}:{range_key}", force, build), project)


def _report(raw: dict[str, Any], project: str | None) -> dict[str, Any]:
    """The response for one project (None = the whole workspace) from the cached rows."""
    agents = [
        row for row in raw["by_agent"] if project is None or row["project_id"] == project
    ]
    sessions = [
        row for row in raw["sessions"] if project is None or row["project_id"] == project
    ]
    unpriced = {row["_unpriced"] for row in (*agents, *sessions) if row["_unpriced"]}
    per_actor: dict[str, dict[str, Any]] = {}
    for row in sessions:
        bucket = per_actor.setdefault(
            row["actor"], {"actor": row["actor"], "sessions": 0, "tokens": 0, "est_cost_usd": 0.0}
        )
        bucket["sessions"] += 1
        bucket["tokens"] += row["tokens"]
        if row["usd"] is not None:
            bucket["est_cost_usd"] = round(bucket["est_cost_usd"] + row["usd"], 6)
    by_agent = [{k: v for k, v in row.items() if k != "_unpriced"} for row in agents]
    per_project: dict[str | None, dict[str, Any]] = {}
    for row in by_agent:
        bucket = per_project.setdefault(row["project_id"], {
            "project_id": row["project_id"], "agents": 0, "tokens": 0, "est_cost_usd": 0.0,
        })
        bucket["agents"] += 1
        bucket["tokens"] += row["tokens"]
        bucket["est_cost_usd"] = round(bucket["est_cost_usd"] + (row["est_cost_usd"] or 0.0), 6)
    return {
        "range": raw["range"],
        "hours": raw["hours"],
        "generated_at": raw["generated_at"],
        **({"project": project} if project is not None else {}),
        "total_est_cost_usd": round(sum(row["est_cost_usd"] or 0.0 for row in by_agent), 6),
        "total_tokens": sum(row["tokens"] for row in by_agent),
        "by_agent": sorted(by_agent, key=lambda row: row["est_cost_usd"] or 0.0, reverse=True),
        "by_actor": sorted(
            per_actor.values(), key=lambda row: row["est_cost_usd"], reverse=True
        ),
        # the project dimension (§10.8); `project_id` None = attributed to no project
        "by_project": sorted(
            per_project.values(), key=lambda row: row["est_cost_usd"], reverse=True
        ),
        # Stated, never implied: these models burned tokens the price map cannot value.
        "unpriced_models": sorted(unpriced),
        "estimate": True,
        **({"cache": raw["cache"]} if "cache" in raw else {}),
    }


def month_to_date_usd(
    db: Session, workspace: WorkspaceContext, *, force: bool = False,
    project: str | None = None,
) -> float:
    """Spend since the first of the month, for the budget comparison.

    Logs Insights ranges are relative, so this uses the hours elapsed this month capped
    at the 30-day window the console offers — a budget check at 02:00 on the 1st looks at
    two hours, which is correct, not a bug.

    `project` counts only the spend attributed to that project (span attribute,
    inference profile, else the resolved agent — `_row_project`); the cached entry is
    workspace-wide.
    """
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hours = max(1, min(720, int((now - month_start).total_seconds() // 3600) or 1))

    def build() -> dict[str, Any]:
        from app.services.observability import build_agent_resolver

        prices = get_settings().model_prices or {}
        results = run_insights_queries(
            {"by_service": q_cost_by_service()}, hours=hours, logs=logs_client(workspace)
        )
        resolve = build_agent_resolver(db, workspace.id, all_projects=True)
        total = 0.0
        by_project: dict[str, float] = {}
        for row in results.get("by_service") or []:
            _, usd = _priced(row, prices)
            total += usd or 0.0
            project_id, _ = _row_project(row, resolve(row.get("service")))
            if project_id:
                by_project[project_id] = by_project.get(project_id, 0.0) + (usd or 0.0)
        return {
            "usd": round(total, 6),
            "hours": hours,
            "by_project": {pid: round(usd, 6) for pid, usd in by_project.items()},
        }

    raw = cached(f"costs-mtd:{workspace.id}:{hours}", force, build)
    if project is None:
        return float(raw["usd"])
    return float((raw.get("by_project") or {}).get(project, 0.0))
