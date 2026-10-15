"""Phase 2 runtime isolation: the project setting, its resource ledger and the
resumable provisioning job (projects design §10.2, §10.12, §10.13).

A project's `settings.isolation.mode` is `shared` (phase 1: data visibility only, the
default for every project, the `default` one included) or `isolated`. An isolated
project gets its own runtime resources in every workspace it is bound to, made by the
Job `project_provision`:

    preflight → boundary → memory → kb-role → m2m → cedar → ecr → s3-prefix → finalize

The shape is `workspace_bootstrap.py`'s on purpose — ordered stages, per-stage status
on `Job.payload["stages"]`, JSONL events on `Job.log`, a resume that skips what already
succeeded — with two differences:

* **the stages are pluggable.** Each one calls a provisioner (`Provisioners`: IAM,
  Memory, KB, Gateway, Storage, plus the quota checker); this node ships the
  interfaces and placeholders that report the stage *skipped* until the R10 node that
  owns it (`PENDING_NODE`) registers the real one with `set_provisioners`. Every real
  provisioner must be idempotent (list-or-get then create) and record what it made
  through `ProvisionContext.record` the moment it exists, exactly like bootstrap's
  `ctx.record`, so a resumed run adopts it.
* **a retry is a fresh run.** A new job re-runs every stage instead of carrying earlier
  successes forward: the stages are idempotent and cheap, and a stage skipped while
  its provisioner was a placeholder must run once the real one exists. Only a
  *resume* of the same job (a restart, `resume_pending_jobs`) skips completed stages.

The job runs inside `project_context(<project>)`; a job is queued when isolation is
switched on (one per bound workspace) and when an isolated project is bound to another
workspace (`on_workspaces_bound`). Switching isolation off only changes the setting:
the resources stay recorded, and undoing what agents carry is §10.11's reverse
migration (R10-migrate).
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Job
from app.models.project_isolation import (
    KIND_BOUNDARY_POLICY,
    KIND_GATEWAY,
    KIND_KB_ROLE,
    KIND_MEMORY,
    STATUS_FAILED,
    STATUS_PROVISIONING,
    STATUS_READY,
    ProjectResource,
    valid_kind,
)
from app.models.projects import Project, ProjectWorkspace
from app.services.audit import record_audit_event

logger = logging.getLogger(__name__)

JOB_TYPE = "project_provision"

STAGE_ORDER = [
    "preflight",
    "boundary",
    "memory",
    "kb-role",
    "m2m",
    "cedar",
    "ecr",
    "s3-prefix",
    "finalize",
]
# the R10 node that replaces each placeholder provisioner (roadmap-dag §1.3)
PENDING_NODE = {
    "boundary": "R10-iam",
    "memory": "R10-mem",
    "kb-role": "R10-kb",
    "m2m": "R10-gw",
    "cedar": "R10-gw",
    "ecr": "R10-misc",
    "s3-prefix": "R10-misc",
}

MODE_SHARED = "shared"
MODE_ISOLATED = "isolated"
MODES = (MODE_SHARED, MODE_ISOLATED)
# §10.6-7: `dedicated` gives a strongly regulated project its own gateway
GATEWAY_SHARED = "shared"
GATEWAY_DEDICATED = "dedicated"
GATEWAY_MODES = (GATEWAY_SHARED, GATEWAY_DEDICATED)

# per-(project, workspace) state shown by the views
STATE_NONE = "not_provisioned"
STATE_PROVISIONING = "provisioning"
STATE_READY = "ready"
STATE_PARTIAL = "partial"  # succeeded, but some stages were placeholders
STATE_FAILED = "failed"

_ACTIVE = ("queued", "running")
_DONE = ("succeeded", "skipped")


class ProvisionError(RuntimeError):
    """A stage failed for a reason the operator can act on."""


class QuotaExceeded(ProvisionError):
    """The preflight found a quota the run would exceed. `payload_extra` keeps the
    findings on the job, so the console shows what was short."""

    def __init__(self, message: str, payload_extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload_extra = payload_extra


class StageSkipped(Exception):  # noqa: N818 — a signal, not an error
    """Raised by a provisioner with nothing to do here; the message is the stage detail."""


# ── names (design §10.3–§10.10) ────────────────────────────────────────────────


@dataclass(frozen=True)
class ResourceNames:
    """The deterministic names an isolated project's resources take in a workspace, so
    every provisioner (and a list-or-get on resume) agrees on them. Project ids are
    slugs (`PROJECT_ID_PATTERN`); memory names do not take `-`, so it becomes `_`."""

    boundary_policy: str
    memory: str
    kb_role: str
    m2m_client: str
    m2m_provider: str
    ecr_repo: str
    s3_prefix: str
    cognito_group: str


def resource_names(project_id: str) -> ResourceNames:
    return ResourceNames(
        # one boundary policy per (project, workspace), §10.3 (R10-iam); the workspace's
        # region is appended outside us-west-2 (`workspace_iam.project_boundary_policy_name`)
        boundary_policy=f"launchpad-project-{project_id}",
        memory=f"launchpad_mem_{project_id.replace('-', '_')}",
        kb_role=f"launchpad-kb-role-{project_id}",
        m2m_client=f"launchpad-agent-m2m-{project_id}",
        m2m_provider=f"launchpad-gw-m2m-{project_id}",
        ecr_repo=f"launchpad-agents/{project_id}",
        s3_prefix=f"projects/{project_id}/",
        cognito_group=f"lp-project-{project_id}",
    )


# ── the setting ───────────────────────────────────────────────────────────────


def isolation_settings(row: Project) -> dict[str, Any]:
    """`settings.isolation`, normalized: anything unrecognized reads as `shared`."""
    raw = (row.settings or {}).get("isolation")
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in MODES else MODE_SHARED
    gateway = raw.get("gateway") if raw.get("gateway") in GATEWAY_MODES else GATEWAY_SHARED
    return {
        "mode": mode,
        "enabled": mode == MODE_ISOLATED,
        "gateway": gateway,
        "updated_by": raw.get("updated_by"),
        "updated_at": raw.get("updated_at"),
    }


def is_isolated(row: Project | None) -> bool:
    return row is not None and isolation_settings(row)["mode"] == MODE_ISOLATED


# ── provisioner interfaces ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProvisionedResource:
    """What a provisioner made (or adopted); becomes one `project_resources` row."""

    kind: str
    resource_id: str | None = None
    arn: str | None = None
    detail: dict[str, Any] | None = None


@dataclass
class ProvisionContext:
    """One stage's view of the run. `workspace` builds the workspace's AWS context on
    first use (provisioners call `ctx.workspace.client(...)`), so a placeholder stage
    never touches it."""

    project_id: str
    workspace_id: str
    job_id: str
    settings: dict[str, Any]
    names: ResourceNames
    # the rows already recorded for this (project, workspace), by kind
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    log: Callable[[str], None] = lambda _msg: None
    _record: Callable[[ProvisionedResource], None] = lambda _r: None
    _workspace_factory: Callable[[str], Any] | None = None
    _workspace: Any = None

    @property
    def workspace(self) -> Any:
        if self._workspace is None:
            from app.services.workspace import context_for_workspace

            factory = self._workspace_factory or context_for_workspace
            self._workspace = factory(self.workspace_id)
        return self._workspace

    @property
    def dedicated_gateway(self) -> bool:
        return self.settings.get("gateway") == GATEWAY_DEDICATED

    def record(self, resource: ProvisionedResource) -> None:
        """Persist a resource the moment it exists (not at stage end): a job killed
        mid-stage must leave it recorded, or the resume would make a second one."""
        if not valid_kind(resource.kind):
            raise ValueError(f"unknown project resource kind: {resource.kind!r}")
        self._record(resource)
        self.resources[resource.kind] = asdict(resource)


class IamProvisioner(Protocol):
    """§10.3 (R10-iam): the project's permissions-boundary policy in the workspace
    (`boundary_policy`); the real one is `project_iam.IamBoundaryProvisioner`."""

    def ensure_boundary(self, ctx: ProvisionContext) -> str: ...


class MemoryProvisioner(Protocol):
    """§10.4 (R10-mem): the project's memory `launchpad_mem_<pid>` (`memory`); the
    real one is `project_memory.ProjectMemoryProvisioner`."""

    def ensure_memory(self, ctx: ProvisionContext) -> str: ...


class KbProvisioner(Protocol):
    """§10.5 (R10-kb): the project's KB service role (`kb_role`)."""

    def ensure_kb_role(self, ctx: ProvisionContext) -> str: ...


class GatewayProvisioner(Protocol):
    """§10.6 (R10-gw): the project's M2M client + token-vault provider (`m2m_client`,
    `m2m_provider`), and the Cedar rules plus the ENFORCE check (`cedar_policy`);
    a dedicated gateway (`gateway`) when `settings.isolation.gateway` asks for one."""

    def ensure_m2m(self, ctx: ProvisionContext) -> str: ...

    def ensure_cedar(self, ctx: ProvisionContext) -> str: ...


class StorageProvisioner(Protocol):
    """§10.10 (R10-misc): the project's ECR repository (`ecr_repo`) and S3 prefix
    (`s3_prefix`)."""

    def ensure_ecr_repo(self, ctx: ProvisionContext) -> str: ...

    def ensure_s3_prefix(self, ctx: ProvisionContext) -> str: ...


@dataclass(frozen=True)
class QuotaFinding:
    """One quota the run consumes: `limit` / `used` are None when they could not be read."""

    key: str
    label: str
    needed: int
    limit: float | None = None
    used: int | None = None
    note: str = ""

    @property
    def known(self) -> bool:
        return self.limit is not None and self.used is not None

    @property
    def blocked(self) -> bool:
        return self.needed > 0 and self.known and self.used + self.needed > self.limit

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "known": self.known, "blocked": self.blocked}


class QuotaChecker(Protocol):
    """§10.12: what the run will consume against the account's quotas."""

    def check(self, ctx: ProvisionContext) -> list[QuotaFinding]: ...


class _Pending:
    """The placeholder behind every stage until its R10 node lands."""

    def _skip(self, stage: str) -> str:
        raise StageSkipped(f"not provisioned yet — arrives with {PENDING_NODE[stage]}")

    def ensure_boundary(self, ctx: ProvisionContext) -> str:
        return self._skip("boundary")

    def ensure_memory(self, ctx: ProvisionContext) -> str:
        return self._skip("memory")

    def ensure_kb_role(self, ctx: ProvisionContext) -> str:
        return self._skip("kb-role")

    def ensure_m2m(self, ctx: ProvisionContext) -> str:
        return self._skip("m2m")

    def ensure_cedar(self, ctx: ProvisionContext) -> str:
        return self._skip("cedar")

    def ensure_ecr_repo(self, ctx: ProvisionContext) -> str:
        return self._skip("ecr")

    def ensure_s3_prefix(self, ctx: ProvisionContext) -> str:
        return self._skip("s3-prefix")


def _needed(ctx: ProvisionContext, kind: str) -> int:
    return 0 if kind in ctx.resources else 1


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "")) or type(exc).__name__
    return type(exc).__name__


class ServiceQuotasChecker:
    """The default preflight: reads quotas and current usage from the workspace's account.

    IAM's account summary carries both the role / policy counts and their quotas. The
    AgentCore quotas are looked up by name in Service Quotas (service code
    `bedrock-agentcore`): the design could not pin their codes (§10.12), so the match is
    on the quota name, and a quota or usage that cannot be read is reported unknown —
    shown, never blocking."""

    def __init__(self, shared_boundary_exists: Callable[[ProvisionContext], bool] | None = None):
        # The boundary is one policy per (project, workspace) (R10-iam), so another
        # project's never covers this one; the hook stays for callers that inject it.
        self._shared_boundary_exists = shared_boundary_exists or (lambda _ctx: False)

    def check(self, ctx: ProvisionContext) -> list[QuotaFinding]:
        out = [*self._iam(ctx), self._agentcore(
            ctx, key="agentcore.memory", label="AgentCore memories",
            needed=_needed(ctx, KIND_MEMORY), name_has="memor", name_lacks="",
            usage=lambda c: _count_pages(c, "list_memories", "memories"),
        )]
        if ctx.dedicated_gateway:
            out.append(self._agentcore(
                ctx, key="agentcore.gateway", label="AgentCore gateways",
                needed=_needed(ctx, KIND_GATEWAY), name_has="gateway", name_lacks="target",
                usage=lambda c: _count_pages(c, "list_gateways", "items"),
            ))
        return out

    def _iam(self, ctx: ProvisionContext) -> list[QuotaFinding]:
        roles_needed = _needed(ctx, KIND_KB_ROLE)
        policies_needed = 0 if (KIND_BOUNDARY_POLICY in ctx.resources
                                or self._shared_boundary_exists(ctx)) else 1
        try:
            summary = ctx.workspace.client("iam").get_account_summary()["SummaryMap"]
        except (ClientError, BotoCoreError, KeyError) as exc:
            note = f"IAM account summary unreadable: {_error_text(exc)}"
            return [QuotaFinding("iam.roles", "IAM roles", roles_needed, note=note),
                    QuotaFinding("iam.policies", "IAM customer managed policies",
                                 policies_needed, note=note)]
        return [
            QuotaFinding("iam.roles", "IAM roles", roles_needed,
                         limit=summary.get("RolesQuota"), used=summary.get("Roles")),
            QuotaFinding("iam.policies", "IAM customer managed policies", policies_needed,
                         limit=summary.get("PoliciesQuota"), used=summary.get("Policies")),
        ]

    def _agentcore(self, ctx: ProvisionContext, *, key: str, label: str, needed: int,
                   name_has: str, name_lacks: str,
                   usage: Callable[[Any], int]) -> QuotaFinding:
        limit: float | None = None
        notes: list[str] = []
        try:
            quotas = ctx.workspace.client("service-quotas")
            for page in quotas.get_paginator("list_service_quotas").paginate(
                ServiceCode="bedrock-agentcore"
            ):
                for quota in page.get("Quotas") or []:
                    name = str(quota.get("QuotaName") or "").lower()
                    if name_has in name and not (name_lacks and name_lacks in name):
                        limit = quota.get("Value")
                        notes.append(f"quota '{quota.get('QuotaName')}'")
                        break
                if limit is not None:
                    break
            if limit is None:
                notes.append("no matching Service Quotas entry")
        except (ClientError, BotoCoreError) as exc:
            notes.append(f"Service Quotas unreadable: {_error_text(exc)}")
        used: int | None = None
        try:
            used = usage(ctx.workspace.client("bedrock-agentcore-control"))
        except (ClientError, BotoCoreError) as exc:
            notes.append(f"usage unreadable: {_error_text(exc)}")
        return QuotaFinding(key, label, needed, limit=limit, used=used, note="; ".join(notes))


def _count_pages(client: Any, operation: str, items_key: str) -> int:
    total, token = 0, None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = getattr(client, operation)(**kwargs)
        total += len(page.get(items_key) or [])
        token = page.get("nextToken")
        if not token:
            return total


@dataclass(frozen=True)
class Provisioners:
    iam: IamProvisioner
    memory: MemoryProvisioner
    kb: KbProvisioner
    gateway: GatewayProvisioner
    storage: StorageProvisioner
    quotas: QuotaChecker


_PENDING = _Pending()
_provisioners = Provisioners(iam=_PENDING, memory=_PENDING, kb=_PENDING, gateway=_PENDING,
                             storage=_PENDING, quotas=ServiceQuotasChecker())


def get_provisioners() -> Provisioners:
    return _provisioners


def set_provisioners(**changes: Any) -> Provisioners:
    """Register real provisioners (`iam=…`, `memory=…`) — the later R10 nodes call this
    at import of their module; tests swap fakes in. Returns the previous set."""
    global _provisioners
    previous = _provisioners
    _provisioners = replace(_provisioners, **changes)
    return previous


def _stage_preflight(ctx: ProvisionContext, p: Provisioners) -> tuple[str, dict[str, Any]]:
    findings = p.quotas.check(ctx)
    report = [f.to_dict() for f in findings]
    blocked = [f for f in findings if f.blocked]
    if blocked:
        raise QuotaExceeded(
            "quota too low for this project: " + "; ".join(
                f"{f.label} {f.used}/{f.limit:g} in use, {f.needed} more needed"
                for f in blocked
            ) + " — request an increase in Service Quotas, then retry",
            {"preflight": report},
        )
    unknown = [f.label for f in findings if f.needed and not f.known]
    detail = f"{len(findings)} quota(s) checked"
    if unknown:
        detail += f"; could not read: {', '.join(unknown)}"
    return detail, {"preflight": report}


_STAGE_CALLS: dict[str, Callable[[Provisioners, ProvisionContext], str]] = {
    "boundary": lambda p, ctx: p.iam.ensure_boundary(ctx),
    "memory": lambda p, ctx: p.memory.ensure_memory(ctx),
    "kb-role": lambda p, ctx: p.kb.ensure_kb_role(ctx),
    "m2m": lambda p, ctx: p.gateway.ensure_m2m(ctx),
    "cedar": lambda p, ctx: p.gateway.ensure_cedar(ctx),
    "ecr": lambda p, ctx: p.storage.ensure_ecr_repo(ctx),
    "s3-prefix": lambda p, ctx: p.storage.ensure_s3_prefix(ctx),
}


# ── the resource ledger ───────────────────────────────────────────────────────


def _resource_rows(db: Session, project_id: str, workspace_id: str | None = None,
                   ) -> list[ProjectResource]:
    stmt = select(ProjectResource).where(ProjectResource.project_id == project_id)
    if workspace_id is not None:
        stmt = stmt.where(ProjectResource.workspace_id == workspace_id)
    return list(db.scalars(stmt.order_by(ProjectResource.workspace_id, ProjectResource.kind)))


def _upsert_resource(db: Session, project_id: str, workspace_id: str, job_id: str,
                     resource: ProvisionedResource) -> None:
    row = db.get(ProjectResource, (project_id, workspace_id, resource.kind))
    if row is None:
        row = ProjectResource(project_id=project_id, workspace_id=workspace_id,
                              kind=resource.kind)
        db.add(row)
    row.resource_id = resource.resource_id
    row.arn = resource.arn
    row.detail = dict(resource.detail) if resource.detail else None
    row.status = STATUS_PROVISIONING
    row.error = None
    row.job_id = job_id
    db.commit()


def resource_view(row: ProjectResource) -> dict[str, Any]:
    return {
        "workspace_id": row.workspace_id,
        "kind": row.kind,
        "resource_id": row.resource_id,
        "arn": row.arn,
        "status": row.status,
        "detail": row.detail or {},
        "error": row.error,
        "job_id": row.job_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ── job bookkeeping ───────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_stages() -> list[dict[str, Any]]:
    return [{"name": name, "status": "pending", "detail": ""} for name in STAGE_ORDER]


def job_stages(job: Job) -> list[dict[str, Any]]:
    return list((job.payload or {}).get("stages") or [])


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
    job = db.get(Job, job_id)
    if job is None:
        return
    payload = dict(job.payload or {})
    stages = [dict(s) for s in payload.get("stages") or _new_stages()]
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
    job.payload = payload  # reassigned: in-place JSON edits are not tracked
    db.commit()


def _jobs(db: Session, project_id: str, workspace_id: str):
    return (
        db.query(Job)
        .filter(Job.type == JOB_TYPE, Job.project_id == project_id,
                Job.workspace_id == workspace_id)
    )


def latest_job(db: Session, project_id: str, workspace_id: str) -> Job | None:
    return _jobs(db, project_id, workspace_id).order_by(Job.created_at.desc(),
                                                        Job.id.desc()).first()


def active_job(db: Session, project_id: str, workspace_id: str) -> Job | None:
    return _jobs(db, project_id, workspace_id).filter(Job.status.in_(_ACTIVE)).first()


# One queue at a time in this process: the active-job check and the insert must not
# interleave, or two binds could queue two runs that race on every create. The backend
# is single-host (like the deploy worker registry).
_QUEUE_LOCK = threading.Lock()


def queue_provision(db: Session, project_id: str, workspace_id: str, *, actor: str,
                    actor_user_id: str | None = None) -> tuple[Job, bool]:
    """Queue a run for (project, workspace) unless one is already queued or running.
    Commits. Returns (job, created); the caller starts a created one
    (`start_provision_async`) after its own transaction is committed."""
    with _QUEUE_LOCK, project_context(None):
        existing = active_job(db, project_id, workspace_id)
        if existing is not None:
            return existing, False
        job = Job(
            workspace_id=workspace_id,
            project_id=project_id,
            type=JOB_TYPE,
            payload={"project_id": project_id, "workspace_id": workspace_id,
                     "stages": _new_stages(), "actor": actor,
                     "actor_user_id": actor_user_id},
        )
        db.add(job)
        db.commit()
        return job, True


def start_jobs(job_ids: Iterable[str]) -> None:
    for job_id in job_ids:
        start_provision_async(job_id)


def on_workspaces_bound(db: Session, project_id: str, workspace_ids: Iterable[str], *,
                        actor: str, actor_user_id: str | None = None,
                        start: bool = True) -> list[str]:
    """After `PUT /api/projects/{id}/workspaces` committed new bindings: an isolated
    project gets a provisioning run in each newly bound workspace (design §10.2). A
    shared project — every single-project workspace today — gets nothing. Returns the
    ids of the jobs queued."""
    row = db.get(Project, project_id)
    if not is_isolated(row) or row.status != "active":
        return []
    queued = [
        job.id for ws in sorted(set(workspace_ids))
        for job, created in [queue_provision(db, project_id, ws, actor=actor,
                                             actor_user_id=actor_user_id)]
        if created
    ]
    if start:
        start_jobs(queued)
    return queued


# ── the runner ────────────────────────────────────────────────────────────────


def execute_provision_job(job_id: str, provisioners: Provisioners | None = None,
                          workspace_factory: Callable[[str], Any] | None = None) -> None:
    """Run (or resume) one provisioning job to completion, inside the job's project.
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
            _run(db, job_id, project_id, workspace_id, provisioners or get_provisioners(),
                 workspace_factory)
    except Exception as exc:  # job-level failure — never crash the worker thread
        db.rollback()
        logger.exception("project_provision %s failed outside a stage", job_id)
        with project_context(None):
            _fail(db, job_id, f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


def _refusal(db: Session, project_id: str, workspace_id: str) -> str | None:
    """Why this run must not provision anything now, or None."""
    row = db.get(Project, project_id)
    if row is None or row.status != "active":
        return f"project '{project_id}' is archived or gone"
    if not is_isolated(row):
        return "isolation was switched off before this run started"
    if db.get(ProjectWorkspace, (project_id, workspace_id)) is None:
        return f"project '{project_id}' is no longer bound to workspace '{workspace_id}'"
    return None


def _run(db: Session, job_id: str, project_id: str, workspace_id: str, p: Provisioners,
         workspace_factory: Callable[[str], Any] | None) -> None:
    job = db.get(Job, job_id)
    job.status = "running"
    db.commit()
    refusal = _refusal(db, project_id, workspace_id)
    if refusal:
        _append_log(db, job_id, "job", refusal, level="error")
        _fail(db, job_id, refusal)
        return
    project = db.get(Project, project_id)
    ctx = ProvisionContext(
        project_id=project_id,
        workspace_id=workspace_id,
        job_id=job_id,
        settings=isolation_settings(project),
        names=resource_names(project_id),
        resources={r.kind: resource_view(r) for r in _resource_rows(db, project_id,
                                                                    workspace_id)},
        _workspace_factory=workspace_factory,
    )
    stage_kinds: list[str] = []

    def record(resource: ProvisionedResource) -> None:
        _upsert_resource(db, project_id, workspace_id, job_id, resource)
        stage_kinds.append(resource.kind)

    ctx._record = record
    done = {e["name"] for e in job_stages(job) if e.get("status") in _DONE}
    for stage in STAGE_ORDER:
        if stage in done:
            continue
        ctx.log = lambda msg, _stage=stage: _append_log(db, job_id, _stage, msg)
        stage_kinds.clear()
        _set_stage(db, job_id, stage, "running")
        extra: dict[str, Any] = {}
        try:
            if stage == "preflight":
                detail, extra = _stage_preflight(ctx, p)
            elif stage == "finalize":
                detail = _stage_finalize(db, project_id, workspace_id, job_id)
            else:
                detail = _STAGE_CALLS[stage](p, ctx)
        except StageSkipped as skip:
            _set_stage(db, job_id, stage, "skipped", str(skip))
            _append_log(db, job_id, stage, str(skip))
            continue
        except Exception as exc:
            db.rollback()
            detail = f"{type(exc).__name__}: {exc}"
            _mark_failed(db, project_id, workspace_id, stage_kinds, detail)
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


def _stage_finalize(db: Session, project_id: str, workspace_id: str, job_id: str) -> str:
    """Every row this (project, workspace) holds in `provisioning` becomes `ready` (a
    `failed` row stays failed until the stage that owns it records it again)."""
    rows = _resource_rows(db, project_id, workspace_id)
    for row in rows:
        if row.status == STATUS_PROVISIONING:
            row.status = STATUS_READY
            row.error = None
    db.commit()
    job = db.get(Job, job_id)
    skipped = [e["name"] for e in job_stages(job) if e.get("status") == "skipped"]
    detail = f"{len(rows)} resource(s) ready"
    if skipped:
        detail += f"; not provisioned yet: {', '.join(skipped)}"
    return detail


def _mark_failed(db: Session, project_id: str, workspace_id: str, kinds: list[str],
                 error: str) -> None:
    for kind in dict.fromkeys(kinds):
        row = db.get(ProjectResource, (project_id, workspace_id, kind))
        if row is not None:
            row.status = STATUS_FAILED
            row.error = error
    db.commit()


def _fail(db: Session, job_id: str, error: str) -> None:
    job = db.get(Job, job_id)
    if job is not None:
        job.status = "failed"
        job.error = error
        db.commit()


# One live worker per job in this process (as for deploys): a repeated start while the
# first thread runs is a no-op, so a job never runs twice at once.
_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()


def start_provision_async(job_id: str) -> threading.Thread:
    with _LIVE_LOCK:
        existing = _LIVE.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def run() -> None:
            try:
                execute_provision_job(job_id)
            finally:
                with _LIVE_LOCK:
                    if _LIVE.get(job_id) is threading.current_thread():
                        del _LIVE[job_id]

        thread = threading.Thread(target=run, daemon=True, name=f"proj-prov-{job_id[:8]}")
        _LIVE[job_id] = thread
        try:
            thread.start()
        except Exception:
            _LIVE.pop(job_id, None)
            raise
        return thread


# ── API operations (design §10.13) ────────────────────────────────────────────


def _bound(db: Session, project_id: str) -> list[str]:
    return list(db.scalars(
        select(ProjectWorkspace.workspace_id).where(ProjectWorkspace.project_id == project_id)
        .order_by(ProjectWorkspace.workspace_id)
    ))


def require_bound(db: Session, project_id: str, workspace_id: str,
                  visible: set[str] | None) -> None:
    """404 for a workspace the project is not bound to — or one the caller does not
    reach the project in (`visible`, None = every one)."""
    if (db.get(ProjectWorkspace, (project_id, workspace_id)) is None
            or (visible is not None and workspace_id not in visible)):
        raise NotFoundError("project.not_bound",
                            f"project '{project_id}' is not bound to workspace "
                            f"'{workspace_id}'",
                            {"project_id": project_id, "workspace_id": workspace_id})


def workspace_state(db: Session, project_id: str, workspace_id: str) -> dict[str, Any]:
    job = latest_job(db, project_id, workspace_id)
    if job is None:
        state, skipped = STATE_NONE, []
    else:
        skipped = [e["name"] for e in job_stages(job) if e.get("status") == "skipped"]
        state = (STATE_PROVISIONING if job.status in _ACTIVE
                 else STATE_FAILED if job.status == "failed"
                 else STATE_PARTIAL if skipped else STATE_READY)
    rows = _resource_rows(db, project_id, workspace_id)
    return {
        "workspace_id": workspace_id,
        "state": state,
        "job_id": job.id if job else None,
        "job_status": job.status if job else None,
        "last_error": job.error if job else None,
        "skipped_stages": skipped,
        "resources": {r.kind: r.status for r in rows},
        "updated_at": job.updated_at.isoformat() if job and job.updated_at else None,
    }


def isolation_view(db: Session, row: Project, visible: set[str] | None) -> dict[str, Any]:
    with project_context(None):
        workspaces = [workspace_state(db, row.id, ws) for ws in _bound(db, row.id)
                      if visible is None or ws in visible]
    return {"project_id": row.id, **isolation_settings(row), "workspaces": workspaces}


def provisioning_view(db: Session, project_id: str, workspace_id: str) -> dict[str, Any]:
    with project_context(None):
        job = latest_job(db, project_id, workspace_id)
        state = workspace_state(db, project_id, workspace_id)
    if job is None:
        return {**state, "job": None}
    payload = job.payload or {}
    return {**state, "job": {
        "id": job.id,
        "status": job.status,
        "error": job.error,
        "stages": job_stages(job),
        "preflight": payload.get("preflight") or [],
        "actor": payload.get("actor"),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }}


def resources_list(db: Session, project_id: str, workspace_id: str | None,
                   visible: set[str] | None) -> list[dict[str, Any]]:
    return [resource_view(r) for r in _resource_rows(db, project_id, workspace_id)
            if visible is None or r.workspace_id in visible]


def _require_active(row: Project) -> None:
    if row.status != "active":
        raise AppError("project.archived", f"project '{row.id}' is archived",
                       {"project_id": row.id}, status_code=409)


def set_isolation(db: Session, identity: Any, row: Project, *, mode: str,
                  gateway: str | None = None) -> list[str]:
    """Switch the project's isolation mode (administrators). Switching on queues a run
    in every bound workspace that has none in flight; switching off only records the
    mode (§10.11's reverse migration is R10-migrate). Commits; returns the job ids
    queued — the caller starts them."""
    if mode not in MODES:
        raise AppError("project.isolation_invalid", f"mode must be one of {list(MODES)}",
                       {"mode": mode})
    if gateway is not None and gateway not in GATEWAY_MODES:
        raise AppError("project.isolation_invalid",
                       f"gateway must be one of {list(GATEWAY_MODES)}", {"gateway": gateway})
    _require_active(row)
    current = isolation_settings(row)
    gateway = gateway or current["gateway"]
    if current["mode"] == mode and current["gateway"] == gateway:
        return []
    actor = getattr(identity, "username", "admin")
    actor_user_id = getattr(identity, "user_id", None)
    settings = dict(row.settings or {})
    settings["isolation"] = {"mode": mode, "gateway": gateway, "updated_by": actor,
                             "updated_at": _now_iso()}
    row.settings = settings  # reassigned: in-place JSON edits are not tracked
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor=actor,
                       actor_user_id=actor_user_id, action="project.isolation.update",
                       target=f"project:{row.id} mode:{mode} gateway:{gateway}", db=db,
                       project_id=row.id)
    db.commit()
    if mode != MODE_ISOLATED:
        return []
    return [
        job.id for ws in _bound(db, row.id)
        for job, created in [queue_provision(db, row.id, ws, actor=actor,
                                             actor_user_id=actor_user_id)]
        if created
    ]


def retry_provision(db: Session, identity: Any, row: Project, workspace_id: str,
                    ) -> tuple[Job, bool]:
    """`POST …/provision`: a fresh run for one workspace (an in-flight one is returned
    as is). Refused unless the project is active and isolated."""
    _require_active(row)
    if not is_isolated(row):
        raise AppError("project.isolation_disabled",
                       f"project '{row.id}' is not isolated — enable isolation first",
                       {"project_id": row.id}, status_code=409)
    require_bound(db, row.id, workspace_id, None)
    actor = getattr(identity, "username", "admin")
    actor_user_id = getattr(identity, "user_id", None)
    job, created = queue_provision(db, row.id, workspace_id, actor=actor,
                                   actor_user_id=actor_user_id)
    if created:
        record_audit_event(workspace_id=workspace_id, actor=actor,
                           actor_user_id=actor_user_id, action="project.provision.retry",
                           target=f"project:{row.id} job:{job.id}", project_id=row.id)
    return job, created


# The real provisioners register themselves on import (`set_provisioners`); imported
# last so they can import this module.
from app.services import project_iam as _project_iam  # noqa: E402,F401 — registers iam
from app.services import project_kb as _project_kb  # noqa: E402,F401 — registers kb
from app.services import project_memory as _project_memory  # noqa: E402,F401 — registers memory
