"""Per-project AgentCore Memory for isolated projects (projects design §10.4).

An **isolated** project gets its own memory in every workspace it is bound to,
`launchpad_mem_<pid>` (`project_isolation.resource_names`; memory names take no `-`),
made by the `memory` stage of `project_provision` with the workspace memory's layout
(the four catalog strategies, 30-day event expiry — `bootstrap.ensure_memory`) and the
project's `launchpad:*` tags (`project_telemetry.project_tags`). The permissions
boundary (`project_iam`) allows `memory/launchpad_mem_<pid>-*`, so the memory id *is*
the isolation.

Which memory a project uses — `effective_memory_id` — is the project's own once its
`memory` row is `ready`, and the workspace's shared bootstrap memory otherwise: a
`shared` project (the `default` one, every single-project workspace) and an isolated
project whose run has not finished resolve exactly as before. The project is the
current one (`project_context`: the request's project, a deploy worker's job project),
so `services/memory.py`'s default memory — and with it the chat rail, the Memory
console and the observability transcript lookups — follows it without new arguments.

**Dual-read** (§10.4): short-term events cannot be moved, so for
`MEMORY_EVENT_EXPIRY_DAYS` after the project memory appeared (or until
`detail.dual_read_until`, which §10.11's migration may set at cutover / retire) the
readers also look in the old shared memory — but only at actors of *this* project's
agents (`<agent_id>__…`, the same prefix test as `observability`'s transcript lookup).
Another project's actors in the shared memory stay out of reach. Writes always go to
the effective memory.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.project_scope import current_project, project_context
from app.models.project_isolation import KIND_MEMORY, STATUS_READY, ProjectResource
from app.services import bootstrap
from app.services import project_isolation as isolation
from app.services.project_telemetry import project_tags

logger = logging.getLogger(__name__)

DUAL_READ_DAYS = bootstrap.MEMORY_EVENT_EXPIRY_DAYS
SCOPE_SEP = "__"  # memory.SCOPE_SEP — not imported: memory imports this module


def memory_description(project_id: str) -> str:
    return f"Launchpad project memory ({project_id}) — short-term events + long-term strategies"


# ── the provisioner (`project_provision` stage `memory`) ──────────────────────


class ProjectMemoryProvisioner:
    """Creates or adopts the project's memory in the workspace (idempotent: list by
    name prefix, then create), records it the moment it exists, then waits for it to
    be ACTIVE. `control` builds the AgentCore control client (tests inject a stub);
    `ensure` / `wait` default to bootstrap's, so the layout matches the workspace's."""

    def __init__(self, control: Callable[[isolation.ProvisionContext], Any] | None = None,
                 ensure: Callable[..., tuple[dict[str, Any], bool]] | None = None,
                 wait: Callable[..., None] | None = None, timeout_s: int = 300):
        self._control = control or (
            lambda ctx: ctx.workspace.client("bedrock-agentcore-control"))
        self._ensure = ensure or bootstrap.ensure_memory
        self._wait = wait or bootstrap._wait_memory_active
        self._timeout_s = timeout_s

    def ensure_memory(self, ctx: isolation.ProvisionContext) -> str:
        control = self._control(ctx)
        role_arn = (getattr(ctx.workspace, "resources", None) or {}).get("execution_role_arn")
        memory, created = self._ensure(
            control,
            name=ctx.names.memory,
            execution_role_arn=role_arn or None,
            wait=False,
            timeout_s=self._timeout_s,
            description=memory_description(ctx.project_id),
            tags=project_tags(ctx.project_id, workspace_id=ctx.workspace_id),
        )
        previous = (ctx.resources.get(KIND_MEMORY) or {}).get("detail") or {}
        # keep a dual-read deadline an earlier run or the migration recorded
        detail = {k: v for k, v in previous.items() if k == "dual_read_until"}
        detail.update({"name": ctx.names.memory,
                       "strategies_added": list(memory.get("strategies_added") or [])})
        ctx.record(isolation.ProvisionedResource(
            KIND_MEMORY, resource_id=memory["id"], arn=memory["arn"], detail=detail,
        ))
        ctx.log(("created " if created else "adopted ") + memory["id"])
        self._wait(control, memory["id"], timeout_s=self._timeout_s)
        return memory["id"] + ("" if created else " (adopted)")


# ── resolution ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectMemory:
    """An isolated project's ready memory in one workspace."""

    project_id: str
    workspace_id: str
    memory_id: str
    arn: str | None
    created_at: datetime | None
    dual_read_until: datetime | None

    def dual_read_open(self, now: datetime | None = None) -> bool:
        until = self.dual_read_until
        return until is not None and (now or datetime.now(UTC)) < until


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _dual_read_until(row: ProjectResource) -> datetime | None:
    raw = (row.detail or {}).get("dual_read_until")
    if raw is False:
        return None  # switched off (a project isolated from the start has nothing to read)
    if isinstance(raw, str) and raw:
        try:
            return _aware(datetime.fromisoformat(raw))
        except ValueError:
            logger.warning("project memory %s/%s: bad dual_read_until %r",
                           row.project_id, row.workspace_id, raw)
    created = _aware(row.created_at)
    return created + timedelta(days=DUAL_READ_DAYS) if created else None


def project_memory(project_id: str | None, workspace_id: str) -> ProjectMemory | None:
    """The project's ready memory in the workspace — None for a shared project, a
    platform row (no project) or an isolated project whose run has not finished."""
    if not project_id:
        return None
    from app.models.projects import Project

    db = SessionLocal()
    try:
        with project_context(None):
            if not isolation.is_isolated(db.get(Project, project_id)):
                return None
            row = db.get(ProjectResource, (project_id, workspace_id, KIND_MEMORY))
            if row is None or row.status != STATUS_READY or not row.resource_id:
                return None
            return ProjectMemory(project_id, workspace_id, row.resource_id, row.arn,
                                 _aware(row.created_at), _dual_read_until(row))
    finally:
        db.close()


def _project(project_id: str | None) -> str | None:
    return project_id if project_id is not None else current_project()


def effective_memory_id(workspace: Any, project_id: str | None = None) -> str | None:
    """The memory an agent of `project_id` (default: the current project) reads and
    writes by default in `workspace`: the project's own when it has one, else the
    workspace's bootstrap memory (None before bootstrap)."""
    own = project_memory(_project(project_id), workspace.id)
    if own is not None:
        return own.memory_id
    return (workspace.resources or {}).get("memory_id") or None


def memory_resources(resources: dict[str, Any] | None, workspace_id: str,
                     project_id: str | None = None) -> dict[str, Any]:
    """A workspace resource map with `memory_id` / `memory_arn` switched to the
    project's memory (default: the current project's) — for the deploy paths that
    read the map (`LAUNCHPAD_MEMORY_ID`, a harness's memory ARN, the execution role's
    memory grant). Unchanged for every shared project."""
    out = dict(resources or {})
    own = project_memory(_project(project_id), workspace_id)
    if own is not None:
        out["memory_id"] = own.memory_id
        if own.arn:
            out["memory_arn"] = own.arn
    return out


def project_memory_ids(workspace_id: str, exclude_project: str | None = None) -> set[str]:
    """Memory ids recorded by projects in the workspace (minus `exclude_project`) —
    the console hides another project's memory from its sibling list."""
    db = SessionLocal()
    try:
        rows = db.scalars(select(ProjectResource).where(
            ProjectResource.workspace_id == workspace_id, ProjectResource.kind == KIND_MEMORY,
        ))
        return {r.resource_id for r in rows
                if r.resource_id and r.project_id != exclude_project}
    finally:
        db.close()


# ── dual-read ─────────────────────────────────────────────────────────────────


def _project_agent_ids(project_id: str, workspace_id: str) -> frozenset[str]:
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        with project_context(None):
            return frozenset(db.scalars(select(Agent.id).where(
                Agent.project_id == project_id, Agent.workspace_id == workspace_id,
            )))
    finally:
        db.close()


@dataclass(frozen=True)
class DualRead:
    """The old shared memory of an isolated project's workspace, readable for the
    actors of the project's agents only."""

    project_id: str
    memory_id: str
    agent_ids: frozenset[str]

    def allows_actor(self, actor_id: str | None) -> bool:
        agent_id, sep, human = (actor_id or "").partition(SCOPE_SEP)
        return bool(sep and human and agent_id in self.agent_ids)

    def allows_namespace(self, namespace: str | None) -> bool:
        """A long-term namespace (`/facts/<actor>[/<session>]`) of an allowed actor."""
        parts = [p for p in (namespace or "").split("/") if p]
        return len(parts) >= 2 and self.allows_actor(parts[1])

    def filter_actors(self, actor_ids: Iterable[str]) -> list[str]:
        return [a for a in actor_ids if self.allows_actor(a)]


def dual_read(workspace: Any, project_id: str | None = None) -> DualRead | None:
    """The shared memory to read alongside the project's, or None (a shared project,
    no project memory yet, the window closed, no shared memory, no agents)."""
    pid = _project(project_id)
    own = project_memory(pid, workspace.id)
    if own is None or not own.dual_read_open():
        return None
    legacy = (workspace.resources or {}).get("memory_id") or None
    if not legacy or legacy == own.memory_id:
        return None
    agents = _project_agent_ids(own.project_id, workspace.id)
    if not agents:
        return None
    return DualRead(own.project_id, legacy, agents)


isolation.set_provisioners(memory=ProjectMemoryProvisioner())
