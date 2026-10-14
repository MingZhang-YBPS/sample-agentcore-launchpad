"""A project's audit view and export (projects design §11.6).

Hub-global like the rest of `/api/projects/{project_id}`: the project is the one in
the path, checked by `core/route_policy.py` (`pperm:project.audit` — a non-member gets
404, a member without the key 403, administrators pass). The rows are the project's
own (every workspace it is bound to) plus the structure events naming it
(`services/audit.project_events`); the export reads the same query, so the two agree.
"""

import csv
import io
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, get_db
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.projects import Project
from app.routers.auth import require_identity
from app.services import audit as audit_svc
from app.services import projects as projects_svc

router = APIRouter(prefix="/api/projects", tags=["projects"])

EXPORT_CAP = 50_000  # rows per download; narrow the filters for more
_FORMATS = {"csv": "text/csv; charset=utf-8", "jsonl": "application/x-ndjson"}


def _existing(db: Session, project_id: str) -> None:
    """An administrator passes the policy check for any id; a missing one is still 404."""
    with project_context(None):
        found = db.scalar(select(Project.id).where(Project.id == project_id))
    if found is None:
        raise projects_svc.not_found(project_id)


def _filter(
    action: str | None = Query(default=None, max_length=160),
    actor: str | None = Query(default=None, max_length=64),
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    outcome: str | None = Query(default=None, max_length=16),
) -> audit_svc.AuditFilter:
    if outcome and outcome not in audit_svc.OUTCOMES:  # here, not mid-stream of an export
        raise AppError("audit.invalid_outcome", f"unknown outcome {outcome!r}",
                       {"outcomes": list(audit_svc.OUTCOMES)})
    return audit_svc.AuditFilter(action=action or None, actor=actor or None,
                                 since=since, until=until, outcome=outcome or None)


@router.get("/{project_id}/audit")
def project_audit(
    project_id: str,
    flt: audit_svc.AuditFilter = Depends(_filter),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """One page, newest first; `next_cursor` is None on the last page. The first page
    also carries the actors and actions present (the filter lists)."""
    _existing(db, project_id)
    rows, next_cursor = audit_svc.project_events(db, project_id, flt, limit=limit,
                                                 cursor=cursor)
    out: dict[str, Any] = {"events": [audit_svc.event_dict(r) for r in rows],
                           "next_cursor": next_cursor}
    if not cursor:
        out["facets"] = audit_svc.project_facets(db, project_id)
    return out


def _csv_lines(rows: Iterator[Any]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(audit_svc.EVENT_FIELDS)
    for row in rows:
        event = audit_svc.event_dict(row)
        writer.writerow(["" if event[f] is None else event[f] for f in audit_svc.EVENT_FIELDS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
    yield buffer.getvalue()


def _jsonl_lines(rows: Iterator[Any]) -> Iterator[str]:
    for row in rows:
        yield json.dumps(audit_svc.event_dict(row), ensure_ascii=False) + "\n"


@router.get("/{project_id}/audit/export")
def export_project_audit(
    project_id: str,
    request: Request,
    flt: audit_svc.AuditFilter = Depends(_filter),
    format: str = Query(default="csv", pattern="^(csv|jsonl)$"),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """The filtered journal as CSV or JSON Lines (at most EXPORT_CAP rows), streamed.
    The download itself is journaled in the project first (design §11.6: exports are
    audited), so it is part of what it exports."""
    _existing(db, project_id)
    identity = require_identity(request)
    audit_svc.record_audit_event(
        workspace_id=DEFAULT_WORKSPACE_ID, actor=identity.username,
        actor_user_id=getattr(identity, "user_id", None), action="audit.export",
        target=f"project:{project_id} format:{format}", project_id=project_id,
    )

    def body() -> Iterator[str]:
        # its own session: the request's closes when the handler returns, before streaming
        session = SessionLocal()
        try:
            rows = audit_svc.iter_project_events(session, project_id, flt, cap=EXPORT_CAP)
            yield from (_csv_lines(rows) if format == "csv" else _jsonl_lines(rows))
        finally:
            session.close()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        body(), media_type=_FORMATS[format],
        headers={"Content-Disposition":
                 f'attachment; filename="audit-{project_id}-{stamp}.{format}"'},
    )
