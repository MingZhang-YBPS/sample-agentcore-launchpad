"""Agent telemetry status (projects §17.3-4): has the agent's endpoint written spans?

The detail page's "Tools & capabilities" card shows when the agent last sent
telemetry, or that it never did — the visible half of §17: a BYOC code zip without
the ADOT launcher emits no span, so it is missing from Observability and cannot be
evaluated. The read reuses the evaluation probes (`evaluation.service.
resolve_telemetry` for the service name, `log_streams.service_has_spans` for the
scan) and is cached per agent version, so opening a detail page does not scan
``aws/spans`` every time.

Read-only: nothing is written to AWS or the ledger. A foreign, hidden or another
project's agent answers 404 like a missing one (``agent_access.agent_in`` under the
request's project).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.evaluation import log_streams
from app.evaluation.service import (
    EVAL_SUPPORTED_METHODS,
    NO_TELEMETRY_LOOKBACK_HOURS,
    byoc_code_zip_without_adot,
    resolve_telemetry,
    telemetry_endpoint,
)
from app.models.ledger import Agent
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.visibility import Viewer, require_viewer
from app.services.workspace import WorkspaceContext

router = APIRouter(prefix="/api", tags=["agents"])

# Probed narrowest first: the first window holding a span bounds how recent the
# last one is. The widest is the evaluation pre-check's lookback (14 days).
WINDOWS: tuple[tuple[str, int], ...] = (
    ("1h", 1),
    ("24h", 24),
    ("7d", 168),
    ("14d", NO_TELEMETRY_LOOKBACK_HOURS),
)
CACHE_TTL_SECONDS = 300
# Statuses whose answer may change by itself soon (a probe that could not tell);
# cached for a shorter time so a retry is not five minutes away.
_SHORT_TTL_SECONDS = 30

_cache: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def reset_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()


def _logs_client(workspace: WorkspaceContext) -> Any:
    return workspace.client("logs")


def _service_name(agent: Agent, workspace: WorkspaceContext, logs: Any, endpoint: str) -> str:
    """The span service name of the agent's endpoint. An imported runtime is resolved
    like a platform runtime (``<runtimeName>.<endpoint>``); the platform methods go
    through the evaluation resolver, so both surfaces read the same spans."""
    if agent.method == "discovered_runtime":
        detail = rt.get_runtime(control_client(workspace), agent.resource_id)
        return f"{detail['agentRuntimeName']}.{endpoint}"
    service_name, _log_group = resolve_telemetry(agent, workspace, logs, qualifier=endpoint)
    return service_name


def _probeable(agent: Agent) -> str | None:
    """Why the agent cannot be probed, or None when it can."""
    if not agent.resource_id:
        return "not_deployed"
    if agent.method == "discovered_runtime":
        discovery = (agent.spec or {}).get("discovery") or {}
        if (discovery.get("resource_type") or "runtime") != "runtime":
            return "method_unsupported"
        return None
    if agent.method not in EVAL_SUPPORTED_METHODS:
        return "method_unsupported"
    return None


def probe_telemetry(agent: Agent, workspace: WorkspaceContext) -> dict[str, Any]:
    """The uncached answer: the narrowest window with a span, or why there is none."""
    endpoint = telemetry_endpoint(agent)
    out: dict[str, Any] = {
        "agent_id": agent.id,
        "endpoint": endpoint,
        "service_name": None,
        "status": "unknown",
        "last_span_within": None,
        "lookback_days": NO_TELEMETRY_LOOKBACK_HOURS // 24,
        "reason": None,
        # the §17 cause the card names when nothing arrives
        "byoc_no_adot": byoc_code_zip_without_adot(agent),
        "checked_at": datetime.now(UTC).isoformat(),
    }
    blocked = _probeable(agent)
    if blocked:
        out["status"] = "not_deployed" if blocked == "not_deployed" else "unsupported"
        out["reason"] = blocked
        return out
    logs = _logs_client(workspace)
    try:
        service_name = _service_name(agent, workspace, logs, endpoint)
    except AppError as exc:
        if exc.code == "eval.harness_no_telemetry":
            # no log group yet: the harness has never been invoked on this endpoint
            out["status"] = "none"
            out["reason"] = "no_log_group"
        else:
            out["reason"] = exc.code
        return out
    except (ClientError, BotoCoreError):
        out["reason"] = "probe_failed"
        return out
    out["service_name"] = service_name
    now_ms = int(time.time() * 1000)
    inconclusive = False
    for label, hours in WINDOWS:
        found = log_streams.service_has_spans(
            logs, service_name, since_ms=now_ms - hours * 3_600_000
        )
        if found:
            out["status"] = "receiving"
            out["last_span_within"] = label
            return out
        if found is None:
            inconclusive = True
    if inconclusive:
        out["reason"] = "probe_failed"
    else:
        out["status"] = "none"
        if out["byoc_no_adot"]:
            out["reason"] = "byoc_code_zip_no_adot"
    return out


@router.get("/agents/{agent_id}/telemetry")
def get_agent_telemetry(
    agent_id: str,
    refresh: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Whether the agent's production endpoint has written spans in the last 14 days,
    and how recently (``last_span_within``: 1h / 24h / 7d / 14d). Cached for five
    minutes per agent version; ``refresh=true`` probes again."""
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    key = (
        ws.id,
        agent.id,
        str(agent.resource_id or ""),
        str(agent.version or ""),
        telemetry_endpoint(agent),
    )
    now = time.monotonic()
    if not refresh:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and hit[0] > now:
            return {**hit[1], "cached": True}
    result = probe_telemetry(agent, ws.context)
    ttl = _SHORT_TTL_SECONDS if result["status"] == "unknown" else CACHE_TTL_SECONDS
    with _cache_lock:
        # drop this agent's older versions so the cache stays one entry per agent
        for stale in [k for k in _cache if k[:2] == key[:2] and k != key]:
            del _cache[stale]
        _cache[key] = (now + ttl, result)
    return {**result, "cached": False}
