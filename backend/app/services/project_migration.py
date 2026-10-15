"""Zero-downtime migration of existing agents into (and out of) an isolated project
(projects design §10.11–§10.13, R10-migrate).

`project_provision` (`project_isolation`) makes an isolated project's resources; this
module moves the agents that already run onto them. One resumable Job
`project_isolate` per (project, workspace) and *direction*:

    isolate  preflight → tag-backfill → agents → finalize
    revert   preflight → agents → finalize                 (isolated → shared)
    retire   preflight → agents → boundary → finalize      (drop the old grants)

Each agent of `isolate` walks four steps, always "先加后减" (§10.16):

* **grant-new** — the role keeps everything it has and gains the bridge inline
  policy (the old shared memory *and* the project memory) and the project boundary,
  whose legacy rows still hold the old memory / M2M provider / KB gateway provider.
  An agent still on the shared execution role gets its own role from the republish.
* **republish** — an ordinary re-publish (`pipeline.create_deployment`, mode
  `update`): the deploy resolves the project's memory, M2M provider, ECR repository
  and inference profiles. The old version keeps serving until the new one is
  routed, and both can reach what they need through the bridge.
* **verify** — health plus one invoke through the injectable invoker.
* **switch** — a `default`-endpoint agent rolled with its deploy; a `live` agent in a
  gated workspace has a release candidate open and waits (`awaiting_release`) until
  it is signed; a retry of the run re-checks it.

`revert` republishes onto the shared resources and then removes the boundary and
the bridge; it is refused once `retire` has run. `retire` deletes every bridge, sets
`settings.isolation.legacy_retired` (before the job — `project_iam` reads it) and
re-runs the boundary stage, which now writes the boundary without the legacy rows,
and closes the project memory's dual-read window (`project_memory`).

All AWS work goes through `MigrationOps`, so tests swap every piece. The job runs in
`project_context(<project>)`; a restart resumes it from the first unfinished agent
step (`resume_pending_jobs`), a republish already started is waited on, never
started twice.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError, is_not_found
from app.core.project_scope import project_context
from app.models.ledger import Agent, Job, ManagedMemory
from app.models.project_isolation import (
    KIND_BOUNDARY_POLICY,
    KIND_MEMORY,
    STATUS_READY,
    ProjectResource,
)
from app.models.projects import Project, ProjectWorkspace
from app.services import project_isolation as isolation
from app.services.audit import record_audit_event
from app.services.project_telemetry import project_tags

logger = logging.getLogger(__name__)

JOB_TYPE = "project_isolate"

DIRECTION_ISOLATE = "isolate"
DIRECTION_REVERT = "revert"
DIRECTION_RETIRE = "retire"
DIRECTIONS = (DIRECTION_ISOLATE, DIRECTION_REVERT, DIRECTION_RETIRE)

STAGES = {
    DIRECTION_ISOLATE: ["preflight", "tag-backfill", "agents", "finalize"],
    DIRECTION_REVERT: ["preflight", "agents", "finalize"],
    DIRECTION_RETIRE: ["preflight", "agents", "boundary", "finalize"],
}
AGENT_STEPS = {
    DIRECTION_ISOLATE: ["grant-new", "republish", "verify", "switch"],
    DIRECTION_REVERT: ["grant-new", "republish", "verify", "switch"],
    DIRECTION_RETIRE: ["retire"],
}

# agent states on the run
AGENT_PENDING = "pending"
AGENT_RUNNING = "running"
AGENT_DONE = "succeeded"
AGENT_SKIPPED = "skipped"
AGENT_FAILED = "failed"
AGENT_AWAITING = "awaiting_release"

# per-workspace migration state shown by the views
STATE_NONE = "not_started"
STATE_RUNNING = "running"
STATE_DONE = "succeeded"
STATE_AWAITING = "awaiting_release"
STATE_FAILED = "failed"

# the inline policy that carries the old and new resources side by side
BRIDGE_POLICY = "launchpad-isolation-bridge"
VERIFY_PROMPT = "ping"
REPUBLISH_NOTE = {DIRECTION_ISOLATE: "isolation migration", DIRECTION_REVERT: "isolation revert"}
# the methods a republish can move (a discovered runtime is externally owned)
_UNMOVABLE_METHODS = ("discovered",)

_ACTIVE = ("queued", "running")
_DONE_STAGE = ("succeeded", "skipped")


class MigrationError(RuntimeError):
    """An agent step or stage failed for a reason the operator can act on."""


# ── the context a step sees ───────────────────────────────────────────────────


@dataclass
class MigrationContext:
    project_id: str
    workspace_id: str
    job_id: str
    direction: str
    actor: str = "admin"
    log: Callable[[str], None] = lambda _msg: None
    _workspace_factory: Callable[[str], Any] | None = None
    _workspace: Any = None

    @property
    def workspace(self) -> Any:
        if self._workspace is None:
            from app.services.workspace import context_for_workspace

            factory = self._workspace_factory or context_for_workspace
            self._workspace = factory(self.workspace_id)
        return self._workspace

    def memory_arn(self, memory_id: str) -> str:
        ws = self.workspace
        return (f"arn:{ws.partition}:bedrock-agentcore:{ws.region}:{ws.account_id}"
                f":memory/{memory_id}")


# ── the pluggable operations ──────────────────────────────────────────────────


class Granter(Protocol):
    def grant_new(self, ctx: MigrationContext, agent: Agent) -> str: ...

    def finish_revert(self, ctx: MigrationContext, agent: Agent) -> str: ...

    def retire(self, ctx: MigrationContext, agent: Agent) -> str: ...


class Republisher(Protocol):
    def start(self, db: Session, agent: Agent, *, actor: str, note: str) -> str: ...

    def status(self, job_id: str) -> tuple[str, str | None]: ...


class Invoker(Protocol):
    def health(self, ctx: MigrationContext, agent: Agent) -> None: ...

    def invoke(self, ctx: MigrationContext, agent: Agent, prompt: str) -> str: ...


class Tagger(Protocol):
    def tag(self, ctx: MigrationContext, arn: str, tags: dict[str, str]) -> None: ...


class ReleaseCheck(Protocol):
    def awaiting(self, db: Session, agent: Agent) -> bool: ...


class BoundaryRunner(Protocol):
    def run(self, ctx: MigrationContext) -> str: ...


def _project_memory_id(project_id: str, workspace_id: str) -> str | None:
    """The project memory recorded for (project, workspace), whatever the mode —
    `project_memory.project_memory` answers None once a revert switched it shared."""
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(ProjectResource, (project_id, workspace_id, KIND_MEMORY))
            return row.resource_id if row is not None and row.resource_id else None
    finally:
        db.close()


def _uses_memory(agent: Agent) -> bool:
    memory = (agent.spec or {}).get("memory") or {}
    return bool(memory.get("short_term") or memory.get("long_term"))


def bridge_policy_document(ctx: MigrationContext, memory_ids: list[str]) -> dict[str, Any]:
    """The memory both versions of an agent need while a republish rolls: the old
    shared memory and the project memory. The gateway token grant of the identity
    policy is already `*` (`agent_iam`), so the providers need no bridge — the
    boundary's legacy rows carry them."""
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "IsolationBridgeMemory", "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent",
            "bedrock-agentcore:ListEvents", "bedrock-agentcore:ListSessions",
            "bedrock-agentcore:ListActors", "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:GetMemoryRecord", "bedrock-agentcore:ListMemoryRecords",
        ],
        "Resource": [ctx.memory_arn(m) for m in memory_ids],
    }]}


def _is_no_such_entity(exc: Exception) -> bool:
    return is_not_found(exc) or "NoSuchEntity" in f"{type(exc).__name__}{exc}"


def _set_applied(agent: Agent, label: str | None) -> None:
    from sqlalchemy import update

    agent.isolation_applied = label
    db = SessionLocal()
    try:
        with project_context(None):
            db.execute(update(Agent).where(Agent.id == agent.id)
                       .values(isolation_applied=label))
            db.commit()
    finally:
        db.close()


class IamGranter:
    """The default grants, on the agent's own execution role."""

    def _role(self, ctx: MigrationContext, agent: Agent) -> tuple[Any, str, bool]:
        from app.services.agent_iam import role_name_for

        iam = ctx.workspace.client("iam")
        name = role_name_for(agent.name, agent.id)
        try:
            iam.get_role(RoleName=name)
        except Exception as exc:  # noqa: BLE001 — the SDK's typed error is client-specific
            if not _is_no_such_entity(exc):
                raise
            return iam, name, False
        return iam, name, True

    def _put_bridge(self, ctx: MigrationContext, iam: Any, role: str, agent: Agent) -> str:
        legacy = (ctx.workspace.resources or {}).get("memory_id") or None
        own = _project_memory_id(ctx.project_id, ctx.workspace_id)
        memories = [m for m in dict.fromkeys([legacy, own]) if m]
        if not _uses_memory(agent) or len(memories) < 2:
            return "no memory to bridge"
        iam.put_role_policy(RoleName=role, PolicyName=BRIDGE_POLICY,
                            PolicyDocument=json.dumps(bridge_policy_document(ctx, memories)))
        return f"bridge {' + '.join(memories)}"

    def _drop_bridge(self, iam: Any, role: str) -> bool:
        try:
            iam.delete_role_policy(RoleName=role, PolicyName=BRIDGE_POLICY)
            return True
        except Exception as exc:  # noqa: BLE001
            if not _is_no_such_entity(exc):
                raise
            return False

    def grant_new(self, ctx: MigrationContext, agent: Agent) -> str:
        iam, role, exists = self._role(ctx, agent)
        if not exists:
            # a legacy agent on the shared execution role: the republish's provision
            # stage creates its own role with the boundary (§10.11)
            return "on the shared execution role — the republish creates its own role"
        parts = [self._put_bridge(ctx, iam, role, agent)]
        if ctx.direction == DIRECTION_ISOLATE:
            from app.schemas.agent import AgentSpec
            from app.services import project_iam
            from app.services.agent_iam import ProjectBoundary

            state = project_iam.boundary_for_agent(agent, AgentSpec(**(agent.spec or {})),
                                                   ctx.workspace, iam, ctx.log)
            if state is None:
                raise MigrationError(f"project '{ctx.project_id}' is not isolated")
            boundary = ProjectBoundary(project_id=ctx.project_id,
                                       workspace_id=ctx.workspace_id,
                                       policy_arn=state.arn, label=state.label)
            # tags first: they mark the role isolated for the spoke and drift checks
            iam.tag_role(RoleName=role, Tags=boundary.tags())
            iam.put_role_permissions_boundary(RoleName=role,
                                              PermissionsBoundary=state.arn)
            _set_applied(agent, state.label)
            parts.append(f"boundary {state.name} {state.label}")
        return f"{role}: " + " · ".join(parts)

    def finish_revert(self, ctx: MigrationContext, agent: Agent) -> str:
        from app.services.project_iam import ISOLATION_TAG_KEY

        iam, role, exists = self._role(ctx, agent)
        if not exists:
            _set_applied(agent, None)
            return "no own role"
        try:
            iam.delete_role_permissions_boundary(RoleName=role)
        except Exception as exc:  # noqa: BLE001
            if not _is_no_such_entity(exc):
                raise
        iam.untag_role(RoleName=role, TagKeys=[ISOLATION_TAG_KEY])
        self._drop_bridge(iam, role)
        _set_applied(agent, None)
        return f"{role}: boundary and bridge removed"

    def retire(self, ctx: MigrationContext, agent: Agent) -> str:
        iam, role, exists = self._role(ctx, agent)
        if not exists:
            return "no own role"
        return f"{role}: " + ("bridge removed" if self._drop_bridge(iam, role)
                              else "no bridge")


class PipelineRepublisher:
    """An ordinary in-place re-publish through the deploy pipeline."""

    def start(self, db: Session, agent: Agent, *, actor: str, note: str) -> str:
        from app.deployer import pipeline

        agent.status = "deploying"
        agent.error = None
        agent.updated_at = datetime.now(UTC)
        db.flush()
        _, job = pipeline.create_deployment(db, agent, mode="update", actor=actor, note=note)
        pipeline.start_deploy_async(job.id)
        return job.id

    def status(self, job_id: str) -> tuple[str, str | None]:
        db = SessionLocal()
        try:
            with project_context(None):
                job = db.get(Job, job_id)
                return (job.status, job.error) if job is not None else ("failed",
                                                                       "deploy job gone")
        finally:
            db.close()


class ConsoleInvoker:
    """Health from the ledger, one turn through the shared invoke chain."""

    def health(self, ctx: MigrationContext, agent: Agent) -> None:
        if agent.status != "active":
            raise MigrationError(f"agent is {agent.status}: {agent.error or 'not active'}")

    def invoke(self, ctx: MigrationContext, agent: Agent, prompt: str) -> str:
        from app.services.invoke import invoke_agent_text

        result = invoke_agent_text(agent, prompt,
                                   session_id=f"isolation-verify-{uuid.uuid4().hex}",
                                   actor_id="isolation-verify", workspace=ctx.workspace,
                                   console_user=False)
        return str(result.get("text") or "")


class ControlTagger:
    def tag(self, ctx: MigrationContext, arn: str, tags: dict[str, str]) -> None:
        ctx.workspace.client("bedrock-agentcore-control").tag_resource(resourceArn=arn,
                                                                       tags=tags)


class GatedReleaseCheck:
    """A `live` agent of a gated workspace switches when its candidate is signed."""

    def awaiting(self, db: Session, agent: Agent) -> bool:
        if getattr(agent, "endpoint_mode", None) != "live" or not agent.version:
            return False
        from app.dlc.releases import pending_for

        record = pending_for(db, agent.workspace_id, agent.id)
        return record is not None and str(record.candidate_version) == str(agent.version)


class ProvisionBoundaryRunner:
    """`retire`'s boundary stage: the project's `boundary` provisioner, re-run now
    that `legacy_retired` is set (so the legacy rows leave the document)."""

    def run(self, ctx: MigrationContext) -> str:
        db = SessionLocal()
        try:
            with project_context(None):
                rows = {r.kind: isolation.resource_view(r) for r in db.scalars(
                    select(ProjectResource).where(
                        ProjectResource.project_id == ctx.project_id,
                        ProjectResource.workspace_id == ctx.workspace_id))}
                project = db.get(Project, ctx.project_id)
                settings = isolation.isolation_settings(project)

            def record(resource: isolation.ProvisionedResource) -> None:
                with project_context(None):
                    row = db.get(ProjectResource, (ctx.project_id, ctx.workspace_id,
                                                   resource.kind))
                    if row is None:
                        row = ProjectResource(project_id=ctx.project_id,
                                              workspace_id=ctx.workspace_id,
                                              kind=resource.kind)
                        db.add(row)
                    row.resource_id, row.arn = resource.resource_id, resource.arn
                    row.detail = dict(resource.detail) if resource.detail else None
                    row.status, row.error, row.job_id = STATUS_READY, None, ctx.job_id
                    db.commit()

            pctx = isolation.ProvisionContext(
                project_id=ctx.project_id, workspace_id=ctx.workspace_id, job_id=ctx.job_id,
                settings=settings, names=isolation.resource_names(ctx.project_id),
                resources=rows, log=ctx.log, _record=record,
                _workspace=ctx.workspace,
            )
            try:
                return isolation.get_provisioners().iam.ensure_boundary(pctx)
            except isolation.StageSkipped as skip:
                return str(skip)
        finally:
            db.close()


@dataclass(frozen=True)
class MigrationOps:
    granter: Granter = field(default_factory=IamGranter)
    republisher: Republisher = field(default_factory=PipelineRepublisher)
    invoker: Invoker = field(default_factory=ConsoleInvoker)
    tagger: Tagger = field(default_factory=ControlTagger)
    releases: ReleaseCheck = field(default_factory=GatedReleaseCheck)
    boundary: BoundaryRunner = field(default_factory=ProvisionBoundaryRunner)
    # a republish is waited on in this loop (tests: no sleep, a short budget)
    poll_seconds: float = 5.0
    republish_timeout_s: float = 1800.0
    sleep: Callable[[float], None] = time.sleep


_ops = MigrationOps()


def get_ops() -> MigrationOps:
    return _ops


def set_ops(**changes: Any) -> MigrationOps:
    """Swap operations (tests); returns the previous set."""
    global _ops
    previous = _ops
    _ops = replace(_ops, **changes)
    return previous


# ── job bookkeeping ───────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _payload(db: Session, job_id: str) -> dict[str, Any]:
    job = db.get(Job, job_id)
    return dict(job.payload or {}) if job is not None else {}


def _save(db: Session, job_id: str, payload: dict[str, Any]) -> None:
    job = db.get(Job, job_id)
    if job is not None:
        job.payload = payload  # reassigned: in-place JSON edits are not tracked
        db.commit()


def _append_log(db: Session, job_id: str, stage: str, message: str, level: str = "info") -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    line = json.dumps({"ts": _now_iso(), "stage": stage, "level": level, "msg": message},
                      ensure_ascii=False)
    job.log = (job.log + "\n" + line) if job.log else line
    db.commit()


def _set_stage(db: Session, job_id: str, stage: str, status: str, detail: str = "",
               extra: dict[str, Any] | None = None) -> None:
    payload = _payload(db, job_id)
    stages = [dict(s) for s in payload.get("stages") or []]
    for entry in stages:
        if entry["name"] != stage:
            continue
        entry["status"] = status
        if detail:
            entry["detail"] = detail
        if status == "running":
            entry["started_at"] = _now_iso()
        if status in ("succeeded", "skipped", "failed"):
            entry["ended_at"] = _now_iso()
    payload["stages"] = stages
    payload.update(extra or {})
    _save(db, job_id, payload)


def _update_agent(db: Session, job_id: str, agent_id: str, **changes: Any) -> dict[str, Any]:
    """Persist one agent's progress (a restart resumes from what is written here)."""
    payload = _payload(db, job_id)
    agents = [dict(a) for a in payload.get("agents") or []]
    out: dict[str, Any] = {}
    for entry in agents:
        if entry["agent_id"] != agent_id:
            continue
        steps = dict(entry.get("steps") or {})
        for name, value in (changes.pop("steps", None) or {}).items():
            steps[name] = {**(steps.get(name) or {}), **value}
        entry.update(changes)
        entry["steps"] = steps
        entry["updated_at"] = _now_iso()
        out = entry
    payload["agents"] = agents
    _save(db, job_id, payload)
    return out


def _jobs(db: Session, project_id: str, workspace_id: str):
    return db.query(Job).filter(Job.type == JOB_TYPE, Job.project_id == project_id,
                                Job.workspace_id == workspace_id)


def latest_run(db: Session, project_id: str, workspace_id: str) -> Job | None:
    return _jobs(db, project_id, workspace_id).order_by(Job.created_at.desc(),
                                                        Job.id.desc()).first()


def active_run(db: Session, project_id: str, workspace_id: str) -> Job | None:
    return _jobs(db, project_id, workspace_id).filter(Job.status.in_(_ACTIVE)).first()


def _fail(db: Session, job_id: str, error: str) -> None:
    job = db.get(Job, job_id)
    if job is not None:
        job.status = "failed"
        job.error = error
        db.commit()


# ── the runner ────────────────────────────────────────────────────────────────


def execute_migration_job(job_id: str, ops: MigrationOps | None = None,
                          workspace_factory: Callable[[str], Any] | None = None) -> None:
    """Run (or resume) one migration run to completion, inside the job's project.
    Never raises."""
    db = SessionLocal()
    try:
        with project_context(None):
            job = db.get(Job, job_id)
            if job is None or job.type != JOB_TYPE:
                return
            payload = job.payload or {}
            project_id = job.project_id or payload.get("project_id") or ""
            workspace_id = job.workspace_id or payload.get("workspace_id") or ""
        with project_context(project_id):
            _run(db, job_id, project_id, workspace_id, ops or get_ops(), workspace_factory)
    except Exception as exc:  # job-level failure — never crash the worker thread
        db.rollback()
        logger.exception("project_isolate %s failed outside a stage", job_id)
        with project_context(None):
            _fail(db, job_id, f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


def _refusal(db: Session, project_id: str, workspace_id: str, direction: str) -> str | None:
    row = db.get(Project, project_id)
    if row is None or row.status != "active":
        return f"project '{project_id}' is archived or gone"
    if db.get(ProjectWorkspace, (project_id, workspace_id)) is None:
        return f"project '{project_id}' is no longer bound to workspace '{workspace_id}'"
    isolated = isolation.is_isolated(row)
    if direction in (DIRECTION_ISOLATE, DIRECTION_RETIRE) and not isolated:
        return "isolation was switched off before this run started"
    if direction == DIRECTION_REVERT and isolated:
        return "isolation was switched back on before this revert ran"
    if direction == DIRECTION_REVERT and isolation.legacy_retired(row):
        return "the project retired its shared resources — a revert is no longer possible"
    return None


def _run(db: Session, job_id: str, project_id: str, workspace_id: str, ops: MigrationOps,
         workspace_factory: Callable[[str], Any] | None) -> None:
    job = db.get(Job, job_id)
    job.status = "running"
    job.error = None
    db.commit()
    payload = job.payload or {}
    direction = payload.get("direction") or DIRECTION_ISOLATE
    refusal = _refusal(db, project_id, workspace_id, direction)
    if refusal:
        _append_log(db, job_id, "job", refusal, level="error")
        _fail(db, job_id, refusal)
        return
    ctx = MigrationContext(project_id=project_id, workspace_id=workspace_id, job_id=job_id,
                           direction=direction, actor=payload.get("actor") or "admin",
                           _workspace_factory=workspace_factory)
    done = {e["name"] for e in payload.get("stages") or [] if e.get("status") in _DONE_STAGE}
    for stage in STAGES[direction]:
        if stage in done:
            continue
        ctx.log = lambda msg, _stage=stage: _append_log(db, job_id, _stage, msg)
        _set_stage(db, job_id, stage, "running")
        extra: dict[str, Any] = {}
        try:
            if stage == "preflight":
                detail, extra = _stage_preflight(db, ctx)
            elif stage == "tag-backfill":
                detail = _stage_tags(db, ctx, ops)
            elif stage == "agents":
                detail = _stage_agents(db, ctx, ops)
            elif stage == "boundary":
                detail = ops.boundary.run(ctx)
            else:
                detail = _stage_finalize(db, ctx)
        except Exception as exc:
            db.rollback()
            detail = f"{type(exc).__name__}: {exc}" if not isinstance(
                exc, MigrationError | isolation.ProvisionError) else str(exc)
            _set_stage(db, job_id, stage, "failed", detail,
                       getattr(exc, "payload_extra", None))
            _append_log(db, job_id, stage, detail, level="error")
            _append_log(db, job_id, stage, traceback.format_exc(limit=3), level="debug")
            _fail(db, job_id, detail)
            return
        _set_stage(db, job_id, stage, "succeeded", detail or "", extra)
        _append_log(db, job_id, stage, detail or "succeeded")
    job = db.get(Job, job_id)
    if job is not None:
        job.status = "succeeded"
        job.error = None
        db.commit()


# ── stages ────────────────────────────────────────────────────────────────────


def _project_agents(db: Session, project_id: str, workspace_id: str) -> list[Agent]:
    with project_context(None):
        return list(db.scalars(select(Agent).where(
            Agent.project_id == project_id, Agent.workspace_id == workspace_id,
            Agent.status != "deleted", Agent.system_key.is_(None),
        ).order_by(Agent.name)))


def _snapshot(agent: Agent, direction: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "agent_id": agent.id, "name": agent.name, "method": agent.method,
        "endpoint_mode": getattr(agent, "endpoint_mode", None) or "default",
        "status": AGENT_PENDING, "step": None, "detail": "", "deploy_job_id": None,
        "steps": {s: {"status": "pending"} for s in AGENT_STEPS[direction]},
        "updated_at": _now_iso(),
    }
    if agent.method in _UNMOVABLE_METHODS:
        entry.update(status=AGENT_SKIPPED, detail="discovered runtime — externally owned")
    elif direction != DIRECTION_RETIRE and agent.status != "active":
        # a draft or failed agent has nothing serving: its next deploy resolves the
        # project's resources on its own
        entry.update(status=AGENT_SKIPPED,
                     detail=f"{agent.status} — its next deploy uses the target resources")
    return entry


def _stage_preflight(db: Session, ctx: MigrationContext) -> tuple[str, dict[str, Any]]:
    extra: dict[str, Any] = {}
    if ctx.direction == DIRECTION_ISOLATE:
        from app.core.config import get_settings

        if not get_settings().per_agent_execution_roles:
            raise MigrationError("per-agent execution roles are switched off — an isolated "
                                 "project's agents need their own role (§10.3)")
        with project_context(None):
            state = isolation.workspace_state(db, ctx.project_id, ctx.workspace_id)
        if state["state"] not in (isolation.STATE_READY, isolation.STATE_PARTIAL):
            raise MigrationError(
                f"the project's resources are {state['state']} in workspace "
                f"'{ctx.workspace_id}' — finish provisioning first")
        # §10.12: the provisioning preflight, against what exists now
        with project_context(None):
            rows = {r.kind: isolation.resource_view(r) for r in db.scalars(
                select(ProjectResource).where(ProjectResource.project_id == ctx.project_id,
                                              ProjectResource.workspace_id == ctx.workspace_id))}
            project = db.get(Project, ctx.project_id)
        pctx = isolation.ProvisionContext(
            project_id=ctx.project_id, workspace_id=ctx.workspace_id, job_id=ctx.job_id,
            settings=isolation.isolation_settings(project),
            names=isolation.resource_names(ctx.project_id), resources=rows,
            _workspace_factory=ctx._workspace_factory, _workspace=ctx._workspace,
        )
        _quota_detail, extra = isolation.evaluate_preflight(
            isolation.get_provisioners().quotas.check(pctx))
    payload = _payload(db, ctx.job_id)
    if not payload.get("agents"):
        payload["agents"] = [_snapshot(a, ctx.direction)
                             for a in _project_agents(db, ctx.project_id, ctx.workspace_id)]
        _save(db, ctx.job_id, payload)
    agents = payload["agents"]
    moving = sum(1 for a in agents if a["status"] != AGENT_SKIPPED)
    return f"{moving} agent(s) to move, {len(agents) - moving} skipped", extra


def _stage_tags(db: Session, ctx: MigrationContext, ops: MigrationOps) -> str:
    """§10.8 tag-backfill: the runtimes / harnesses and the managed memories that
    predate the project's isolation get its `launchpad:*` tags (A2A and KB
    conditions of the boundary key on them)."""
    tagged = 0
    for agent in _project_agents(db, ctx.project_id, ctx.workspace_id):
        if agent.arn and agent.method not in _UNMOVABLE_METHODS:
            ops.tagger.tag(ctx, agent.arn, project_tags(
                ctx.project_id, workspace_id=ctx.workspace_id, agent_id=agent.id))
            tagged += 1
    with project_context(None):
        memories = list(db.scalars(select(ManagedMemory.memory_id).where(
            ManagedMemory.project_id == ctx.project_id,
            ManagedMemory.workspace_id == ctx.workspace_id)))
    for memory_id in memories:
        ops.tagger.tag(ctx, ctx.memory_arn(memory_id),
                       project_tags(ctx.project_id, workspace_id=ctx.workspace_id))
        tagged += 1
    return f"{tagged} resource(s) tagged"


def _wait_deploy(ctx: MigrationContext, ops: MigrationOps, job_id: str) -> None:
    deadline = time.monotonic() + ops.republish_timeout_s
    while True:
        status, error = ops.republisher.status(job_id)
        if status == "succeeded":
            return
        if status == "failed":
            raise MigrationError(f"republish failed: {error or 'see the deploy log'}")
        if time.monotonic() >= deadline:
            raise MigrationError(f"republish {job_id} still {status} — retry to keep waiting")
        ops.sleep(ops.poll_seconds)


def _load_agent(db: Session, agent_id: str) -> Agent | None:
    with project_context(None):
        agent = db.get(Agent, agent_id)
        if agent is not None:
            db.refresh(agent)
        return agent


def _run_step(db: Session, ctx: MigrationContext, ops: MigrationOps, entry: dict[str, Any],
              step: str) -> tuple[str, str]:
    """One agent step → (status, detail); raises on failure."""
    agent = _load_agent(db, entry["agent_id"])
    if agent is None or agent.status == "deleted":
        return AGENT_SKIPPED, "deleted during the migration"
    if step == "grant-new":
        return AGENT_DONE, ops.granter.grant_new(ctx, agent)
    if step == "retire":
        return AGENT_DONE, ops.granter.retire(ctx, agent)
    if step == "republish":
        deploy_job = entry.get("deploy_job_id")
        if not deploy_job:
            if agent.status == "deploying":
                raise MigrationError("a deployment is already in progress — retry after it")
            with project_context(ctx.project_id):
                deploy_job = ops.republisher.start(db, agent, actor=ctx.actor,
                                                   note=REPUBLISH_NOTE[ctx.direction])
            db.commit()
            # written before the wait: a restart waits on this job, never starts another
            _update_agent(db, ctx.job_id, agent.id, deploy_job_id=deploy_job)
        _wait_deploy(ctx, ops, deploy_job)
        agent = _load_agent(db, agent.id)
        return AGENT_DONE, f"deploy {deploy_job} · version {agent.version or '?'}"
    if step == "verify":
        ops.invoker.health(ctx, agent)
        if ((agent.spec or {}).get("protocol") or "http") == "mcp":
            return AGENT_DONE, "healthy (an MCP server takes no prompt)"
        ops.invoker.invoke(ctx, agent, VERIFY_PROMPT)
        return AGENT_DONE, "healthy · one invoke answered"
    # switch
    if ops.releases.awaiting(db, agent):
        return AGENT_AWAITING, f"version {agent.version} waits for its release to be signed"
    if ctx.direction == DIRECTION_REVERT:
        return AGENT_DONE, ops.granter.finish_revert(ctx, agent)
    return AGENT_DONE, f"serving version {agent.version or '?'}"


def _stage_agents(db: Session, ctx: MigrationContext, ops: MigrationOps) -> str:
    steps = AGENT_STEPS[ctx.direction]
    for entry in list(_payload(db, ctx.job_id).get("agents") or []):
        if entry["status"] in (AGENT_DONE, AGENT_SKIPPED):
            continue
        aid = entry["agent_id"]
        _update_agent(db, ctx.job_id, aid, status=AGENT_RUNNING, error=None)
        for step in steps:
            current = _payload_agent(db, ctx.job_id, aid)
            if (current["steps"].get(step) or {}).get("status") == AGENT_DONE:
                continue
            _update_agent(db, ctx.job_id, aid, step=step,
                          steps={step: {"status": AGENT_RUNNING, "started_at": _now_iso()}})
            try:
                status, detail = _run_step(db, ctx, ops, current, step)
            except Exception as exc:  # one agent's failure leaves the others moving
                db.rollback()
                detail = str(exc) if isinstance(exc, MigrationError | AppError) else (
                    f"{type(exc).__name__}: {exc}")
                _update_agent(db, ctx.job_id, aid, status=AGENT_FAILED, error=detail,
                              detail=detail, steps={step: {"status": AGENT_FAILED,
                                                           "detail": detail}})
                ctx.log(f"{entry['name']}: {step} failed — {detail}")
                break
            _update_agent(db, ctx.job_id, aid, detail=detail,
                          steps={step: {"status": status, "detail": detail,
                                        "ended_at": _now_iso()}})
            ctx.log(f"{entry['name']}: {step} — {detail}")
            if status == AGENT_SKIPPED:
                _update_agent(db, ctx.job_id, aid, status=AGENT_SKIPPED)
                break
            if status == AGENT_AWAITING:
                _update_agent(db, ctx.job_id, aid, status=AGENT_AWAITING)
                break
        else:
            _update_agent(db, ctx.job_id, aid, status=AGENT_DONE, step=None)
    agents = _payload(db, ctx.job_id).get("agents") or []
    counts = _counts(agents)
    failed = [a["name"] for a in agents if a["status"] == AGENT_FAILED]
    if failed:
        raise MigrationError(f"{len(failed)} agent(s) failed: {', '.join(failed)} — "
                             "fix the cause, then retry the run")
    return _counts_text(counts)


def _payload_agent(db: Session, job_id: str, agent_id: str) -> dict[str, Any]:
    return next(a for a in _payload(db, job_id).get("agents") or []
                if a["agent_id"] == agent_id)


def _counts(agents: list[dict[str, Any]]) -> dict[str, int]:
    out = {s: 0 for s in (AGENT_DONE, AGENT_AWAITING, AGENT_SKIPPED, AGENT_FAILED,
                          AGENT_PENDING, AGENT_RUNNING)}
    for a in agents:
        out[a["status"]] = out.get(a["status"], 0) + 1
    return out


def _counts_text(counts: dict[str, int]) -> str:
    text = f"{counts[AGENT_DONE]} moved"
    if counts[AGENT_AWAITING]:
        text += f", {counts[AGENT_AWAITING]} awaiting release"
    if counts[AGENT_SKIPPED]:
        text += f", {counts[AGENT_SKIPPED]} skipped"
    return text


def _stage_finalize(db: Session, ctx: MigrationContext) -> str:
    if ctx.direction == DIRECTION_RETIRE:
        # the shared memory left the boundary: its dual-read window closes now (§10.4)
        with project_context(None):
            row = db.get(ProjectResource, (ctx.project_id, ctx.workspace_id, KIND_MEMORY))
            if row is not None:
                row.detail = {**(row.detail or {}), "dual_read_until": _now_iso()}
            boundary = db.get(ProjectResource, (ctx.project_id, ctx.workspace_id,
                                                KIND_BOUNDARY_POLICY))
            version = (boundary.detail or {}).get("version") if boundary is not None else None
            db.commit()
        if version:
            # every migrated role carries the policy; its default version is the new one
            for agent in _project_agents(db, ctx.project_id, ctx.workspace_id):
                if agent.isolation_applied:
                    _set_applied(agent, f"boundary-v{version}")
    return _counts_text(_counts(_payload(db, ctx.job_id).get("agents") or []))


# ── starting runs ─────────────────────────────────────────────────────────────

_QUEUE_LOCK = threading.Lock()


def _queue(db: Session, project_id: str, workspace_id: str, direction: str, *, actor: str,
           actor_user_id: str | None) -> tuple[Job, bool]:
    """A run for (project, workspace) unless one is in flight (returned as is)."""
    with _QUEUE_LOCK, project_context(None):
        existing = active_run(db, project_id, workspace_id)
        if existing is not None:
            return existing, False
        job = Job(workspace_id=workspace_id, project_id=project_id, type=JOB_TYPE,
                  payload={"project_id": project_id, "workspace_id": workspace_id,
                           "direction": direction, "actor": actor,
                           "actor_user_id": actor_user_id,
                           "stages": [{"name": s, "status": "pending", "detail": ""}
                                      for s in STAGES[direction]],
                           "agents": []})
        db.add(job)
        db.commit()
        return job, True


_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()


def start_migration_async(job_id: str) -> threading.Thread:
    """One live worker per run in this process (as for provisioning)."""
    with _LIVE_LOCK:
        existing = _LIVE.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def run() -> None:
            try:
                execute_migration_job(job_id)
            finally:
                with _LIVE_LOCK:
                    if _LIVE.get(job_id) is threading.current_thread():
                        del _LIVE[job_id]

        thread = threading.Thread(target=run, daemon=True, name=f"proj-iso-{job_id[:8]}")
        _LIVE[job_id] = thread
        try:
            thread.start()
        except Exception:
            _LIVE.pop(job_id, None)
            raise
        return thread


def start_jobs(job_ids: list[str]) -> None:
    for job_id in job_ids:
        start_migration_async(job_id)


def _actor(identity: Any) -> tuple[str, str | None]:
    return getattr(identity, "username", "admin"), getattr(identity, "user_id", None)


def _targets(db: Session, row: Project, workspace_id: str | None) -> list[str]:
    if workspace_id is not None:
        isolation.require_bound(db, row.id, workspace_id, None)
        return [workspace_id]
    return isolation._bound(db, row.id)


def _require_no_provisioning(db: Session, project_id: str, workspace_id: str) -> None:
    with project_context(None):
        busy = isolation.active_job(db, project_id, workspace_id)
    if busy is not None:
        raise AppError("project.provisioning_in_progress",
                       f"provisioning is still running in workspace '{workspace_id}'",
                       {"project_id": project_id, "workspace_id": workspace_id,
                        "job_id": busy.id}, status_code=409)


def migrate(db: Session, identity: Any, row: Project, *, direction: str,
            workspace_id: str | None = None) -> list[str]:
    """`POST …/isolation/migrate` (administrators): queue `isolate` (the project must
    be isolated and provisioned) or `revert` (switches the mode to shared first) in
    one or every bound workspace. Returns the ids queued; the caller starts them."""
    if direction not in (DIRECTION_ISOLATE, DIRECTION_REVERT):
        raise AppError("project.migration_invalid",
                       f"direction must be one of {[DIRECTION_ISOLATE, DIRECTION_REVERT]}",
                       {"direction": direction})
    isolation._require_active(row)
    targets = _targets(db, row, workspace_id)
    actor, actor_user_id = _actor(identity)
    if direction == DIRECTION_ISOLATE:
        if not isolation.is_isolated(row):
            raise AppError("project.isolation_disabled",
                           f"project '{row.id}' is not isolated — enable isolation first",
                           {"project_id": row.id}, status_code=409)
        for ws in targets:
            _require_no_provisioning(db, row.id, ws)
            with project_context(None):
                state = isolation.workspace_state(db, row.id, ws)["state"]
            if state not in (isolation.STATE_READY, isolation.STATE_PARTIAL):
                raise AppError("project.not_provisioned",
                               f"the project's resources are {state} in workspace '{ws}' "
                               "— provisioning must finish first",
                               {"project_id": row.id, "workspace_id": ws}, status_code=409)
    else:
        if isolation.legacy_retired(row):
            raise AppError("project.isolation_retired",
                           f"project '{row.id}' retired its shared resources — it stays "
                           "isolated", {"project_id": row.id}, status_code=409)
        if isolation.is_isolated(row):
            isolation.set_isolation(db, identity, row, mode=isolation.MODE_SHARED)
    queued = []
    for ws in targets:
        job, created = _queue(db, row.id, ws, direction, actor=actor,
                              actor_user_id=actor_user_id)
        if created:
            queued.append(job.id)
            record_audit_event(workspace_id=ws, actor=actor, actor_user_id=actor_user_id,
                               action=f"project.isolation.{direction}",
                               target=f"project:{row.id} job:{job.id}", project_id=row.id)
    return queued


def retire(db: Session, identity: Any, row: Project) -> list[str]:
    """`POST …/isolation/retire` (administrators): every bound workspace's latest run
    is a finished `isolate` with no agent waiting — then `legacy_retired` is set and
    a `retire` run is queued per workspace. Irreversible (no revert afterwards)."""
    isolation._require_active(row)
    if not isolation.is_isolated(row):
        raise AppError("project.isolation_disabled",
                       f"project '{row.id}' is not isolated", {"project_id": row.id},
                       status_code=409)
    targets = isolation._bound(db, row.id)
    for ws in targets:
        with project_context(None):
            run = latest_run(db, row.id, ws)
        payload = (run.payload or {}) if run is not None else {}
        retired_before = payload.get("direction") == DIRECTION_RETIRE
        if run is None or (payload.get("direction") != DIRECTION_ISOLATE
                           and not retired_before) or run.status != "succeeded":
            raise AppError("project.migration_unfinished",
                           f"workspace '{ws}' has no finished migration — migrate its "
                           "agents first", {"project_id": row.id, "workspace_id": ws},
                           status_code=409)
        waiting = [a["name"] for a in payload.get("agents") or []
                   if a["status"] == AGENT_AWAITING]
        if waiting:
            raise AppError("project.migration_awaiting_release",
                           f"{len(waiting)} agent(s) in '{ws}' still wait for their "
                           f"release: {', '.join(waiting)}",
                           {"project_id": row.id, "workspace_id": ws, "agents": waiting},
                           status_code=409)
    actor, actor_user_id = _actor(identity)
    settings = dict(row.settings or {})
    raw = dict(settings.get("isolation") or {})
    raw.update(legacy_retired=True, legacy_retired_at=_now_iso(), legacy_retired_by=actor)
    settings["isolation"] = raw
    row.settings = settings
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor=actor,
                       actor_user_id=actor_user_id, action="project.isolation.retire",
                       target=f"project:{row.id}", db=db, project_id=row.id)
    db.commit()
    return [job.id for ws in targets
            for job, created in [_queue(db, row.id, ws, DIRECTION_RETIRE, actor=actor,
                                        actor_user_id=actor_user_id)] if created]


def retry(db: Session, identity: Any, row: Project, run_id: str) -> Job:
    """`POST …/migration/{run}/retry`: the run resumes from its checkpoints — a failed
    agent from its failed step, an agent awaiting release re-checks its switch. Only
    the latest run of its workspace, and not while it is in flight."""
    isolation._require_active(row)
    with project_context(None):
        job = db.get(Job, run_id)
        if job is None or job.type != JOB_TYPE or job.project_id != row.id:
            raise NotFoundError("project.migration_not_found",
                                f"migration run '{run_id}' not found", {"run_id": run_id})
        latest = latest_run(db, row.id, job.workspace_id)
    if job.status in _ACTIVE:
        return job
    if latest is not None and latest.id != job.id:
        raise AppError("project.migration_superseded",
                       "a newer run exists for this workspace — retry that one",
                       {"run_id": run_id, "latest": latest.id}, status_code=409)
    payload = dict(job.payload or {})
    awaiting = any(a["status"] == AGENT_AWAITING for a in payload.get("agents") or [])
    if job.status == "succeeded" and not awaiting:
        raise AppError("project.migration_finished", "this run already finished",
                       {"run_id": run_id}, status_code=409)
    stages = [dict(s) for s in payload.get("stages") or []]
    for entry in stages:
        if entry["status"] == "failed" or (entry["name"] in ("agents", "finalize")
                                           and awaiting):
            entry["status"] = "pending"
    agents = []
    for a in payload.get("agents") or []:
        a = dict(a)
        if a["status"] == AGENT_FAILED:
            a["status"] = AGENT_PENDING
            steps = {k: dict(v) for k, v in (a.get("steps") or {}).items()}
            for step in steps.values():
                if step.get("status") == AGENT_FAILED:
                    step["status"] = "pending"
                    # a failed republish starts a new deploy; a failed wait keeps it
            if (steps.get("republish") or {}).get("status") == "pending" and a.get(
                    "deploy_job_id"):
                status, _ = get_ops().republisher.status(a["deploy_job_id"])
                if status == "failed":
                    a["deploy_job_id"] = None
            a["steps"] = steps
        agents.append(a)
    payload.update(stages=stages, agents=agents)
    job.payload = payload
    job.status = "queued"
    job.error = None
    actor, actor_user_id = _actor(identity)
    db.commit()
    record_audit_event(workspace_id=job.workspace_id, actor=actor,
                       actor_user_id=actor_user_id, action="project.isolation.retry",
                       target=f"project:{row.id} job:{job.id}", project_id=row.id)
    return job


# ── views ─────────────────────────────────────────────────────────────────────


def run_state(job: Job | None) -> str:
    if job is None:
        return STATE_NONE
    if job.status in _ACTIVE:
        return STATE_RUNNING
    if job.status == "failed":
        return STATE_FAILED
    agents = (job.payload or {}).get("agents") or []
    return STATE_AWAITING if any(a["status"] == AGENT_AWAITING for a in agents) else STATE_DONE


def run_view(job: Job) -> dict[str, Any]:
    payload = job.payload or {}
    agents = payload.get("agents") or []
    return {
        "id": job.id,
        "direction": payload.get("direction") or DIRECTION_ISOLATE,
        "status": job.status,
        "state": run_state(job),
        "error": job.error,
        "actor": payload.get("actor"),
        "stages": list(payload.get("stages") or []),
        "preflight": payload.get("preflight") or [],
        "agents": agents,
        "counts": _counts(agents),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def migration_view(db: Session, row: Project, visible: set[str] | None,
                   workspace_id: str | None = None) -> dict[str, Any]:
    with project_context(None):
        targets = [workspace_id] if workspace_id else isolation._bound(db, row.id)
        out = []
        for ws in targets:
            if visible is not None and ws not in visible:
                continue
            job = latest_run(db, row.id, ws)
            out.append({"workspace_id": ws, "state": run_state(job),
                        "run": run_view(job) if job is not None else None})
    return {"project_id": row.id, "mode": isolation.isolation_settings(row)["mode"],
            "legacy_retired": isolation.legacy_retired(row), "workspaces": out}


def agent_boundary_view(db: Session, row: Project, agent: Agent) -> dict[str, Any]:
    """§10.14 identity card: the boundary the agent's role carries against the
    project's current one in its workspace (`drift` when they differ)."""
    with project_context(None):
        boundary = db.get(ProjectResource, (row.id, agent.workspace_id, KIND_BOUNDARY_POLICY))
        run = latest_run(db, row.id, agent.workspace_id)
    current = None
    if boundary is not None and (boundary.detail or {}).get("version"):
        current = f"boundary-v{boundary.detail['version']}"
    applied = agent.isolation_applied
    isolated = isolation.is_isolated(row)
    migration = None
    if run is not None:
        entry = next((a for a in (run.payload or {}).get("agents") or []
                      if a["agent_id"] == agent.id), None)
        if entry is not None:
            migration = {"run_id": run.id, "direction": (run.payload or {}).get("direction"),
                         "status": entry["status"], "step": entry.get("step"),
                         "detail": entry.get("detail")}
    return {
        "agent_id": agent.id,
        "project_id": row.id,
        "workspace_id": agent.workspace_id,
        "mode": isolation.isolation_settings(row)["mode"],
        "legacy_retired": isolation.legacy_retired(row),
        "policy_name": boundary.resource_id if boundary is not None else None,
        "policy_arn": boundary.arn if boundary is not None else None,
        "applied": applied,
        "current": current,
        # an isolated project's agent without the boundary, or on an older version
        "drift": bool(isolated and (applied is None or (current and applied != current))),
        "migration": migration,
    }
