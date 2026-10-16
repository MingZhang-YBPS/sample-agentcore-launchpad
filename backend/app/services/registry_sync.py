"""`POST /api/registry/sync-defaults` as a resumable Job (`registry_sync_defaults`).

Registering the default records waits every new record out of CREATING before it is
submitted, which on a real workspace takes longer than the 60 s a CloudFront origin
allows a request — so the route only queues this job (202 + `job_id`) and the console
polls `GET /api/registry/sync-defaults/{job_id}` (or `GET /api/jobs/{job_id}`).

The job runs inside the project that queued it (the gateway whose targets become MCP
records may be the project's), records each record's result in `payload.results` as
it settles, and is resumed after a restart by re-running the whole sync: every step
is an upsert, so a record already registered reads `created: false` the second time.
One active job per workspace + project: a second POST while one is queued or running
returns that job instead of starting another.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.project_scope import current_project, project_context
from app.models.ledger import Job

logger = logging.getLogger(__name__)

JOB_TYPE = "registry_sync_defaults"
ACTIVE = ("queued", "running")

_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()
_QUEUE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _log(job: Job, message: str, level: str = "info") -> None:
    line = json.dumps({"ts": _now_iso(), "stage": "sync", "level": level, "msg": message},
                      ensure_ascii=False)
    job.log = (job.log + "\n" + line) if job.log else line


def queue(db: Session, workspace_id: str, actor: str | None = None) -> tuple[Job, bool]:
    """The workspace's active sync for the current project, or a new queued one
    (committed). Returns ``(job, created)``; the caller starts a created job."""
    project_id = current_project()
    with _QUEUE_LOCK:
        with project_context(None):
            stmt = select(Job).where(Job.type == JOB_TYPE, Job.workspace_id == workspace_id,
                                     Job.status.in_(ACTIVE))
            if project_id is not None:
                stmt = stmt.where(Job.project_id == project_id)
            active = db.scalars(stmt.order_by(Job.created_at.desc())).first()
        if active is not None:
            return active, False
        job = Job(workspace_id=workspace_id, type=JOB_TYPE, status="queued",
                  project_id=project_id,
                  payload={"workspace_id": workspace_id, "actor": actor, "results": []})
        db.add(job)
        db.commit()
        db.refresh(job)
        return job, True


def execute(job_id: str, workspace_factory: Callable[[str | None], Any] | None = None,
            on_done: Callable[[str], None] | None = None) -> None:
    """Run (or re-run, on resume) one sync inside the job's project; never raises."""
    from app.services import registry_console
    from app.services.workspace import context_for_workspace

    db = SessionLocal()
    try:
        with project_context(None):
            job = db.get(Job, job_id)
            if job is None or job.type != JOB_TYPE or job.status not in ACTIVE:
                return
            project_id, workspace_id = job.project_id, job.workspace_id
        with project_context(project_id):
            job = db.get(Job, job_id)
            resumed = job.status == "running"
            job.status, job.error = "running", None
            job.payload = {**(job.payload or {}), "results": [],
                           "started_at": _now_iso()}
            _log(job, "resumed after a restart" if resumed else "started")
            db.commit()
            workspace = (workspace_factory or context_for_workspace)(workspace_id)

            def record(result: dict[str, Any]) -> None:
                row = db.get(Job, job_id)
                payload = dict(row.payload or {})
                payload["results"] = [*payload.get("results", []), result]
                row.payload = payload  # reassigned: in-place JSON edits are not tracked
                _log(row, f"{result['type']} {result['name']}: "
                          f"{'created' if result['created'] else 'refreshed'}")
                db.commit()

            try:
                registry_console.ensure_default_records(workspace, on_result=record)
            except Exception as exc:  # noqa: BLE001 — the job carries the failure
                db.rollback()
                logger.warning("%s %s failed", JOB_TYPE, job_id, exc_info=True)
                row = db.get(Job, job_id)
                row.status, row.error = "failed", f"{type(exc).__name__}: {exc}"
                _log(row, row.error, "error")
                db.commit()
            else:
                row = db.get(Job, job_id)
                row.status = "succeeded"
                row.payload = {**(row.payload or {}), "ended_at": _now_iso()}
                _log(row, "succeeded")
                db.commit()
            if on_done is not None and workspace_id:
                on_done(workspace_id)
    except Exception as exc:  # job-level failure — never crash the worker thread
        db.rollback()
        logger.exception("%s %s failed outside the sync", JOB_TYPE, job_id)
        with project_context(None):
            row = db.get(Job, job_id)
            if row is not None:
                row.status, row.error = "failed", f"{type(exc).__name__}: {exc}"
                db.commit()
    finally:
        db.close()


def _invalidate_attachables(workspace_id: str) -> None:
    # Lazy: the router imports this module.
    from app.routers import registry as registry_router

    registry_router._invalidate_attachables(workspace_id)


def start_async(job_id: str) -> threading.Thread:
    """One live worker per job in this process; also the resume starter."""
    with _LIVE_LOCK:
        existing = _LIVE.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def run() -> None:
            try:
                execute(job_id, on_done=_invalidate_attachables)
            finally:
                with _LIVE_LOCK:
                    if _LIVE.get(job_id) is threading.current_thread():
                        del _LIVE[job_id]

        thread = threading.Thread(target=run, daemon=True,
                                  name=f"registry-sync-{job_id[:8]}")
        _LIVE[job_id] = thread
        try:
            thread.start()
        except Exception:
            _LIVE.pop(job_id, None)
            raise
        return thread


def view(job: Job) -> dict[str, Any]:
    """The status shape the console polls; `results` matches the old synchronous body."""
    payload = job.payload or {}
    return {
        "job_id": job.id,
        "status": job.status,
        "done": job.status not in ACTIVE,
        "results": list(payload.get("results") or []),
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
