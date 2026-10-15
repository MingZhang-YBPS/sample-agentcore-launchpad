"""Online console update (docs/design online-update §2.6): hub-global, administrators
only except the version read (route_policy). The source is deployment configuration —
no body here names a repository or a branch (`extra="forbid"`)."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import Identity, require_identity
from app.services import system_update

router = APIRouter(prefix="/api/system", tags=["system"])


def _caller(request: Request) -> Identity:
    return require_identity(request)


class _NoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # omitted / null = now (after the 60-second countdown)
    at: datetime | None = None
    # "I know N in-progress tasks will be interrupted"
    acknowledge_inflight: bool = False


@router.get("/version")
def version() -> dict[str, Any]:
    return system_update.version_payload()


@router.get("/update")
def update_state(db: Session = Depends(get_db)) -> dict[str, Any]:
    return system_update.state(db)


@router.post("/update/check")
def check(_body: _NoBody | None = None) -> dict[str, Any]:
    return system_update.check_remote(force=True)


@router.post("/update/prepare", status_code=202)
def prepare(
    _body: _NoBody | None = None,
    db: Session = Depends(get_db),
    caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    return system_update.serialize(system_update.prepare(db, caller))


@router.post("/update/rollback", status_code=202)
def rollback(
    _body: _NoBody | None = None,
    db: Session = Depends(get_db),
    caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    return system_update.serialize(system_update.rollback(db, caller))


@router.post("/update/{update_id}/schedule")
def schedule(
    update_id: str,
    body: ScheduleRequest,
    db: Session = Depends(get_db),
    caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    row = system_update.schedule(db, update_id, body.at, body.acknowledge_inflight, caller)
    return system_update.serialize(row)


@router.post("/update/{update_id}/cancel")
def cancel(
    update_id: str,
    db: Session = Depends(get_db),
    caller: Identity = Depends(_caller),
) -> dict[str, Any]:
    return system_update.serialize(system_update.cancel(db, update_id, caller))


@router.get("/update/{update_id}/log")
def log(update_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = system_update.get_update(db, update_id)
    return {"id": row.id, "status": row.status, "events": system_update.log_events(row)}
