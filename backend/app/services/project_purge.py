"""Deleting a project: the export bundle and the resumable `project_purge` job
(projects design §11.5).

The lifecycle around it lives in ``services/projects.py``: an administrator asks for
the deletion of an **archived** project (``request_deletion``), which writes an
export bundle first and starts a cooling-off period (``platform_settings``
``project_deletion_cooling_off_days``, default 7 days) during which the request can
be cancelled. Only once it has run out does ``start_purge`` accept the purge:

1. ``agents`` — every Agent of the project not yet deleted goes through the one agent
   teardown (``routers.agents.delete_agent_row``: AWS resource, execution role,
   registry record, ledger row), the same path ``DELETE /api/agents/{agent_id}`` takes.
   ``AGENT_TEARDOWN`` is the injection point (tests replace it; no AWS).
2. ``ledger`` — every project-carrying row of the project (``_ProjectColumn`` tables,
   found from the mappers so a new table is covered by itself), the project's members,
   custom roles and workspace bindings. The journals stay: ``audit_events``,
   ``policy_changes`` and ``policy_decisions`` keep their ``project_id``.
3. ``finalize`` — the project row becomes a tombstone (``status = deleted``, name and
   department kept, so the audit trail still resolves it), the access epoch moves and
   ``project.delete`` is journaled.

Each stage checkpoints on the job's payload. A failure stops the job as ``failed``
with the project back to ``archived`` (design §11.5's state machine) and the deletion
request kept; the next ``POST …/purge`` starts a new job that carries the verified
checkpoints forward. ``dry_run`` answers the same inventory the job will delete.

The worker runs inside ``project_context(<project>)`` (design §5.12); the ledger
deletes are Core statements with an explicit ``project_id`` filter, so a NULL
(platform) row of a shared table is never touched.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import threading
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import DATA_DIR
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Agent, AuditEvent, Job, ReleaseBundle
from app.models.platform_settings import PlatformSetting
from app.models.projects import Project, ProjectMember, ProjectRole, ProjectWorkspace
from app.services import access, release_export
from app.services.audit import record_audit_event, record_structure_event

logger = logging.getLogger(__name__)

JOB_TYPE = "project_purge"
STAGES = ("agents", "ledger", "finalize")
STATUS_DELETING = "deleting"
STATUS_DELETED = "deleted"
LIVE_JOB_STATUSES = ("queued", "running")

COOLING_OFF_SETTING = "project_deletion_cooling_off_days"
DEFAULT_COOLING_OFF_DAYS = 7
MAX_COOLING_OFF_DAYS = 365
# a `running` purge job whose checkpoints stopped moving this long ago belongs to a
# process that died (a restart): the next request may take it over
STALE_AFTER = timedelta(minutes=30)
# kept with their `project_id`: the journals outlive the project (design §11.5-6)
RETAINED_TABLES = frozenset({"audit_events", "policy_changes", "policy_decisions"})
# what the export bundle carries per table (design §11.5 "导出": agents' specs,
# datasets, criteria, releases; the audit trail as CSV)
EXPORT_TABLES = (
    "agents", "spec_snapshots", "eval_datasets", "criteria_sets", "criteria",
    "answer_rule_sets", "answer_rules", "release_bundles", "release_records",
)
# never exported: key hashes, tokens, secrets (design §11.5 "不含密钥 / 令牌哈希")
_SECRET_COLUMN = re.compile(r"(hash|secret|token|password|credential)", re.IGNORECASE)
EXPORT_FORMAT = "launchpad.agentcore/v1 ProjectExport"
EXPORT_DIR = DATA_DIR / "project-exports"  # tests repoint it
_EXPORT_ID = re.compile(r"^[a-f0-9]{32}$")
EXPORT_HISTORY = 10  # export records kept on the project row

_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # SQLite hands timezone-aware columns back naive; they were written as UTC
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _default_agent_teardown(db: Session, agent: Agent) -> bool:
    """The one agent teardown (`DELETE /api/agents/{agent_id}`); commits the ledger."""
    from app.assistant import purge as assistant_purge
    from app.routers.agents import delete_agent_row
    from app.services.workspace import context_for_workspace

    # as the route does: a conversation CLEAR that owns this agent's teardown wins
    assistant_purge.refuse_while_clearing(agent.id, "delete")
    return delete_agent_row(db, agent, context_for_workspace(agent.workspace_id))


# (db, agent) -> whether an AWS resource was deleted; replaced in tests
AGENT_TEARDOWN: Callable[[Session, Agent], bool] = _default_agent_teardown


# ── lifecycle state on the project row ────────────────────────────────────────


def lifecycle(row: Project) -> dict[str, Any]:
    return dict((row.settings or {}).get("lifecycle") or {})


def set_lifecycle(row: Project, state: dict[str, Any]) -> None:
    """JSON columns only notice a new object: always assign a fresh dict."""
    settings = dict(row.settings or {})
    if state:
        settings["lifecycle"] = state
    else:
        settings.pop("lifecycle", None)
    row.settings = settings or None


def cooling_off_days(db: Session) -> int:
    setting = db.get(PlatformSetting, COOLING_OFF_SETTING)
    try:
        days = int(setting.value) if setting is not None and setting.value != "" else None
    except ValueError:
        days = None
    if days is None:
        return DEFAULT_COOLING_OFF_DAYS
    return max(0, min(days, MAX_COOLING_OFF_DAYS))


def journal(db: Session, identity: Any, action: str, project_id: str, target: str = ""
            ) -> None:
    """A lifecycle step in the project's own journal (hub-global, filed under the hub's
    `default` workspace like a structure event, but carrying the project): it is
    exported with the project and kept by the purge."""
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor=identity.username,
                       actor_user_id=getattr(identity, "user_id", None), action=action,
                       target=f"project:{project_id}{' ' + target if target else ''}",
                       db=db, project_id=project_id)


# ── the export bundle ─────────────────────────────────────────────────────────


def _models_by_table() -> dict[str, Any]:
    from app.services.projects import _project_tables

    return {m.__tablename__: m for m in _project_tables()}


def _row_dict(table: Any, row: Any) -> dict[str, Any]:
    return {c: getattr(row, c) for c in table.c.keys() if not _SECRET_COLUMN.search(c)}


def _jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, default=str, sort_keys=True, ensure_ascii=False) + "\n"
                   for r in rows)


def _yaml(document: dict[str, Any]) -> str:
    # the release export's dumper settings: sorted keys, block style, never folded
    return yaml.safe_dump(release_export._plain(document), sort_keys=True,
                          default_flow_style=False, allow_unicode=True, width=100000)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _file_name(*parts: str) -> str:
    return _SAFE_NAME.sub("_", "-".join(p for p in parts if p))[:120]


def _mentions(project_id: str) -> re.Pattern[str]:
    """`project:<id>` as a whole token of an audit target (not `project:<id>2`)."""
    return re.compile(rf"project:{re.escape(project_id)}(?![a-z0-9-])")


def audit_rows(db: Session, project_id: str) -> list[AuditEvent]:
    """The project's journal: its own rows plus the structure events that name it."""
    with project_context(None):
        rows = list(db.scalars(select(AuditEvent).where(
            (AuditEvent.project_id == project_id)
            | AuditEvent.target.like(f"%project:{project_id}%")
        ).order_by(AuditEvent.created_at, AuditEvent.id)))
    mention = _mentions(project_id)
    return [r for r in rows if r.project_id == project_id or mention.search(r.target or "")]


def _audit_csv(rows: list[AuditEvent]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["created_at", "workspace_id", "project_id", "actor", "actor_user_id",
                     "action", "target"])
    for r in rows:
        writer.writerow([_utc(r.created_at).isoformat() if r.created_at else "",
                         r.workspace_id or "", r.project_id or "", r.actor,
                         r.actor_user_id or "", r.action, r.target or ""])
    return out.getvalue()


def export_path(project_id: str, export_id: str) -> Path:
    return EXPORT_DIR / project_id / f"{export_id}.zip"


def build_export(db: Session, identity: Any, row: Project) -> dict[str, Any]:
    """Write the project's export bundle (a ZIP) and record it on the project row.

    ``manifest.json`` (tables, row counts, generated at / by), one JSONL per exported
    table (secret-looking columns dropped), every Agent's spec as a deterministic YAML
    document and every release bundle in the GitOps format of ``release_export``, and
    the audit trail as CSV. Kept after the project is deleted: it is the way back."""
    models = _models_by_table()
    export_id = uuid.uuid4().hex
    files: dict[str, str] = {}
    counts: dict[str, int] = {}
    with project_context(row.id):
        for name in EXPORT_TABLES:
            model = models.get(name)
            if model is None:
                continue
            table = model.__table__
            found = list(db.execute(select(table).where(table.c.project_id == row.id)))
            counts[name] = len(found)
            files[f"tables/{name}.jsonl"] = _jsonl([_row_dict(table, r) for r in found])
        agents = list(db.scalars(select(Agent).where(Agent.project_id == row.id)
                                 .order_by(Agent.created_at, Agent.id)))
        for agent in agents:
            files[f"agents/{_file_name(agent.name, agent.id)}.spec.yaml"] = _yaml({
                "apiVersion": release_export.API_VERSION,
                "kind": "AgentSpec",
                "metadata": {"name": agent.name, "id": agent.id, "method": agent.method,
                             "workspace_id": agent.workspace_id, "status": agent.status},
                "spec": agent.spec or {},
            })
        for bundle in db.scalars(select(ReleaseBundle).where(
            ReleaseBundle.project_id == row.id,
        ).order_by(ReleaseBundle.created_at, ReleaseBundle.id)):
            name = _file_name(bundle.agent_name, (bundle.digest or "")[:12], bundle.id)
            files[f"release-bundles/{name}.release.yaml"] = release_export.export_yaml(bundle)
    audit = audit_rows(db, row.id)
    counts["audit_events"] = len(audit)
    files["audit.csv"] = _audit_csv(audit)
    generated_at = _now().isoformat()
    manifest = {
        "format": EXPORT_FORMAT,
        "export_id": export_id,
        "project": {"id": row.id, "name": row.name, "description": row.description or "",
                    "department_id": row.department_id, "status": row.status},
        "generated_at": generated_at,
        "generated_by": identity.username,
        "tables": counts,
        "files": sorted(files),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True,
                                                ensure_ascii=False))
        for name in sorted(files):
            zf.writestr(name, files[name])
    data = buffer.getvalue()
    path = export_path(row.id, export_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    record = {"id": export_id, "created_at": generated_at, "created_by": identity.username,
              "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
              "tables": counts}
    state = lifecycle(row)
    state["exports"] = [record, *(state.get("exports") or [])][:EXPORT_HISTORY]
    set_lifecycle(row, state)
    journal(db, identity, "project.export", row.id, f"export:{export_id}")
    return record


def export_file(row: Project, export_id: str) -> Path:
    """The bundle's file — only an export recorded on THIS project (else 404)."""
    known = {e.get("id") for e in lifecycle(row).get("exports") or []}
    path = export_path(row.id, export_id)
    if not _EXPORT_ID.match(export_id) or export_id not in known or not path.is_file():
        raise NotFoundError("project.export_not_found", f"export '{export_id}' not found",
                            {"project_id": row.id, "export_id": export_id})
    return path


# ── the inventory (dry run) ───────────────────────────────────────────────────


def _purge_tables() -> list[Any]:
    return sorted((m for name, m in _models_by_table().items() if name not in RETAINED_TABLES),
                  key=lambda m: m.__tablename__)


def _live_agents(db: Session, project_id: str) -> list[Agent]:
    return list(db.scalars(select(Agent).where(
        Agent.project_id == project_id, Agent.status != "deleted",
    ).order_by(Agent.created_at, Agent.id)))


def purge_plan(db: Session, row: Project) -> dict[str, Any]:
    """What the purge removes: the agents it tears down and the rows per table."""
    with project_context(row.id):
        agents = [{"id": a.id, "name": a.name, "method": a.method,
                   "workspace_id": a.workspace_id, "status": a.status}
                  for a in _live_agents(db, row.id)]
        rows: dict[str, int] = {}
        for model in _purge_tables():
            table = model.__table__
            stmt = select(func.count()).select_from(table).where(table.c.project_id == row.id)
            if model is Job:
                stmt = stmt.where(table.c.type != JOB_TYPE)  # the purge's own history
            count = int(db.execute(stmt).scalar_one())
            if count:
                rows[table.name] = count
    for model, label in ((ProjectMember, "project_members"), (ProjectRole, "project_roles"),
                         (ProjectWorkspace, "project_workspaces")):
        count = int(db.execute(select(func.count()).select_from(model)
                               .where(model.project_id == row.id)).scalar_one())
        if count:
            rows[label] = count
    return {"project_id": row.id, "agents": agents, "rows": rows,
            "retained": sorted(RETAINED_TABLES)}


# ── starting the job ──────────────────────────────────────────────────────────


def purge_jobs(db: Session, project_id: str) -> list[Job]:
    with project_context(None):
        return list(db.scalars(select(Job).where(
            Job.type == JOB_TYPE, Job.project_id == project_id,
        ).order_by(Job.created_at.desc(), Job.id.desc())))


def latest_job(db: Session, project_id: str) -> Job | None:
    jobs = purge_jobs(db, project_id)
    return jobs[0] if jobs else None


def job_view(job: Job | None) -> dict[str, Any] | None:
    if job is None:
        return None
    payload = job.payload or {}
    return {"id": job.id, "status": job.status, "stage": payload.get("stage"),
            "agents_done": list(payload.get("agents_done") or []),
            "rows": payload.get("rows") or {}, "error": job.error,
            "created_at": _utc(job.created_at).isoformat() if job.created_at else None,
            "updated_at": _utc(job.updated_at).isoformat() if job.updated_at else None}


def _is_live_here(job_id: str) -> bool:
    with _LIVE_LOCK:
        thread = _LIVE.get(job_id)
        return thread is not None and thread.is_alive()


def _stale(job: Job) -> bool:
    updated = _utc(job.updated_at) or _utc(job.created_at)
    return updated is None or _now() - updated > STALE_AFTER


def start_purge(db: Session, identity: Any, row: Project, *, now: datetime | None = None
                ) -> Job:
    """Queue the purge of a project whose cooling-off has run out (the caller commits,
    then ``start_async``). A failed or interrupted earlier job hands its checkpoints on."""
    now = now or _now()
    state = lifecycle(row)
    deletion = state.get("deletion")
    if row.status == STATUS_DELETED:
        raise AppError("project.deleted", f"project '{row.id}' is deleted",
                       {"project_id": row.id}, status_code=409)
    if not deletion:
        raise AppError("project.deletion_not_requested",
                       f"request the deletion of project '{row.id}' first",
                       {"project_id": row.id}, status_code=409)
    purge_after = datetime.fromisoformat(deletion["purge_after"])
    if now < purge_after:
        raise AppError("project.cooling_off",
                       f"project '{row.id}' can be purged from {deletion['purge_after']}",
                       {"project_id": row.id, "purge_after": deletion["purge_after"]},
                       status_code=409)
    exports = {e.get("id") for e in state.get("exports") or []}
    if deletion.get("export_id") not in exports:
        raise AppError("project.export_required", "the deletion has no export bundle",
                       {"project_id": row.id}, status_code=409)
    previous = latest_job(db, row.id)
    if previous is not None and previous.status in LIVE_JOB_STATUSES:
        if _is_live_here(previous.id) or not _stale(previous):
            raise AppError("project.purge_running", "the purge of this project is running",
                           {"project_id": row.id, "job_id": previous.id}, status_code=409)
        # its process died (a restart): the new job takes over from its checkpoints
        previous.status = "failed"
        previous.error = previous.error or "interrupted — taken over by a new purge job"
    with project_context(None):
        busy = [j.id for j in db.scalars(select(Job).where(
            Job.project_id == row.id, Job.type != JOB_TYPE,
            Job.status.in_(LIVE_JOB_STATUSES),
        ))]
    if busy:
        raise AppError("project.busy", "jobs of this project are still running",
                       {"project_id": row.id, "jobs": sorted(busy)}, status_code=409)
    carried = (previous.payload or {}) if previous is not None else {}
    job = Job(
        type=JOB_TYPE, workspace_id=None, status="queued", project_id=row.id,
        payload={
            "project_id": row.id,
            "stage": STAGES[0],
            "agents_done": list(carried.get("agents_done") or []),
            "rows": dict(carried.get("rows") or {}),
            "requested_by": identity.username,
            "requested_by_user_id": getattr(identity, "user_id", None),
        },
    )
    db.add(job)
    db.flush()
    row.status = STATUS_DELETING
    row.updated_at = now
    state["deletion"] = {**deletion, "job_id": job.id}
    set_lifecycle(row, state)
    access.bump_epoch(db)
    journal(db, identity, "project.purge", row.id, f"job:{job.id}")
    return job


def _spawn(job_id: str) -> threading.Thread:
    thread = threading.Thread(target=_run_tracked, args=(job_id,), daemon=True,
                              name=f"project-purge-{job_id[:8]}")
    with _LIVE_LOCK:
        _LIVE[job_id] = thread
    thread.start()
    return thread


def _run_tracked(job_id: str) -> None:
    try:
        run_purge(job_id)
    finally:
        with _LIVE_LOCK:
            if _LIVE.get(job_id) is threading.current_thread():
                del _LIVE[job_id]


def start_async(job_id: str) -> Any:
    """Run a committed purge job on a daemon thread (tests replace ``_spawn``)."""
    return _spawn(job_id)


def start_purge_resume(job_id: str) -> Any:
    """Resume starter for ``pipeline.resume_pending_jobs``: a restart left the job
    queued or running; it continues from its checkpoints."""
    return _spawn(job_id)


def start_due_purges(db: Session, *, now: datetime | None = None) -> list[str]:
    """Start the purge of every project whose cooling-off has run out (for the DLC
    scheduler tick). Returns the job ids started; a refusal is logged and skipped."""
    from types import SimpleNamespace

    now = now or _now()
    started: list[str] = []
    for row in db.scalars(select(Project).where(Project.status == "archived")):
        deletion = lifecycle(row).get("deletion")
        if not deletion or now < datetime.fromisoformat(deletion["purge_after"]):
            continue
        previous = latest_job(db, row.id)
        if previous is not None and previous.status == "failed":
            continue  # a failed purge waits for an administrator's retry
        actor = SimpleNamespace(username=deletion.get("requested_by") or "system",
                                user_id=deletion.get("requested_by_user_id"))
        try:
            job = start_purge(db, actor, row, now=now)
            db.commit()
        except AppError as exc:
            db.rollback()
            logger.info("project %s: purge not started — %s", row.id, exc.message)
            continue
        start_async(job.id)
        started.append(job.id)
    return started


# ── the worker ────────────────────────────────────────────────────────────────


def _log(job: Job, stage: str, message: str, level: str = "info") -> None:
    line = json.dumps({"ts": _now().isoformat(), "stage": stage, "level": level,
                       "msg": message}, ensure_ascii=False)
    job.log = (job.log + "\n" + line) if job.log else line


def _checkpoint(db: Session, job: Job, **changes: Any) -> None:
    job.payload = {**(job.payload or {}), **changes}
    job.updated_at = _now()
    db.commit()


def run_purge(job_id: str) -> None:
    """The job, stage by stage from its checkpoint. Never raises: a failure is the
    job's `failed` status and the project's return to `archived`."""
    db = SessionLocal()
    try:
        with project_context(None):
            job = db.get(Job, job_id)
            if job is None or job.type != JOB_TYPE or job.status not in LIVE_JOB_STATUSES:
                return
            project_id = str((job.payload or {}).get("project_id") or job.project_id)
        with project_context(project_id):
            _run(db, job_id, project_id)
    finally:
        db.close()


def _run(db: Session, job_id: str, project_id: str) -> None:
    job = db.get(Job, job_id)
    row = db.get(Project, project_id)
    if job is None or row is None:
        return
    job.status = "running"
    done = len((job.payload or {}).get("agents_done") or [])
    _log(job, "start", f"purge of project {project_id}"
         + (f" ({done} agent(s) already torn down)" if done else ""))
    _checkpoint(db, job)
    stage = ""
    try:
        # every stage again, from the top: each is idempotent (a torn-down agent is
        # in `agents_done` or already `deleted`, a deleted row is gone), so a resumed
        # job cannot skip work an earlier attempt did not finish
        for stage in STAGES:
            _checkpoint(db, job, stage=stage)
            if stage == "agents":
                _stage_agents(db, job, project_id)
            elif stage == "ledger":
                _stage_ledger(db, job, project_id)
            else:
                _stage_finalize(db, job, row)
        job = db.get(Job, job_id)
        job.status = "succeeded"
        _log(job, "finalize", f"project {project_id} deleted")
        _checkpoint(db, job, stage="done")
    except Exception as exc:  # noqa: BLE001 — the job records the failure
        db.rollback()
        logger.warning("project purge %s failed at %s", job_id, stage, exc_info=True)
        job = db.get(Job, job_id)
        row = db.get(Project, project_id)
        message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        if job is not None:
            job.status = "failed"
            job.error = f"{stage}: {message}"
            _log(job, stage or "start", message, "error")
        if row is not None and row.status == STATUS_DELETING:
            row.status = "archived"  # purge failed → archived, retryable (§11.5)
            row.updated_at = _now()
            access.bump_epoch(db)
        db.commit()


def _stage_agents(db: Session, job: Job, project_id: str) -> None:
    done = list((job.payload or {}).get("agents_done") or [])
    for agent in _live_agents(db, project_id):
        if agent.id in done:
            continue
        _log(job, "agents", f"deleting agent {agent.name} ({agent.id})")
        aws = AGENT_TEARDOWN(db, agent)
        done.append(agent.id)
        _log(job, "agents", f"agent {agent.id} deleted"
             + ("" if aws else " (no AWS resource removed)"))
        _checkpoint(db, job, agents_done=done)


def _stage_ledger(db: Session, job: Job, project_id: str) -> None:
    """Every row of the project, one transaction: a half-deleted ledger would be
    worse than the project it describes."""
    counts: dict[str, int] = {}
    for model in _purge_tables():
        table = model.__table__
        stmt = delete(table).where(table.c.project_id == project_id)
        if model is Job:
            stmt = stmt.where(table.c.type != JOB_TYPE)  # this job and earlier attempts
        result = db.execute(stmt)
        if result.rowcount:
            counts[table.name] = int(result.rowcount)
    for model in (ProjectMember, ProjectRole, ProjectWorkspace):
        result = db.execute(delete(model).where(model.project_id == project_id))
        if result.rowcount:
            counts[model.__tablename__] = int(result.rowcount)
    access.bump_epoch(db)
    db.expire_all()  # the Core deletes bypassed the identity map
    job = db.get(Job, job.id)
    _log(job, "ledger", f"deleted {sum(counts.values())} row(s)")
    merged = {**((job.payload or {}).get("rows") or {})}
    for name, count in counts.items():
        merged[name] = merged.get(name, 0) + count
    _checkpoint(db, job, rows=merged)


def _stage_finalize(db: Session, job: Job, row: Project) -> None:
    payload = job.payload or {}
    row.status = STATUS_DELETED
    row.group_id = None
    row.updated_at = _now()
    state = lifecycle(row)
    deletion = dict(state.pop("deletion", None) or {})
    state["deleted"] = {"at": _now().isoformat(), "job_id": job.id,
                        "requested_at": deletion.get("requested_at"),
                        "requested_by": deletion.get("requested_by"),
                        "export_id": deletion.get("export_id")}
    set_lifecycle(row, state)
    access.bump_epoch(db)
    record_structure_event(db=db, action="project.delete", target=f"project:{row.id}",
                           actor=payload.get("requested_by") or "system",
                           actor_user_id=payload.get("requested_by_user_id"))
    db.commit()
