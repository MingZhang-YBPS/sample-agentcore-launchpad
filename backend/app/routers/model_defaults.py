"""Platform model settings for the console.

- ``GET /api/models/defaults`` (member): the effective model defaults the console
  pre-selects, so switching a model on the server changes the UI default without a
  frontend rebuild.
- ``/api/models/settings`` (admin): the Models page — every role with its source
  (default / config / env / console), set or clear a console override (applied at
  once, no restart).
- ``GET /api/model-catalog`` (member) and ``POST /api/model-probe`` (admin): the
  models the active workspace's Region offers, and a real tiny invocation of one.

Model ids only — nothing account-specific is stored, so the settings routes are
hub-global; the catalog and the probe call AWS and are workspace-scoped.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import model_settings

router = APIRouter(prefix="/api/models", tags=["models"])
workspace_router = APIRouter(prefix="/api", tags=["models"])


@router.get("/defaults")
def model_defaults() -> dict[str, Any]:
    return model_settings.effective_defaults()


@router.get("/settings")
def list_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"roles": model_settings.role_rows(db)}


class SettingIn(BaseModel):
    model_id: str = Field(max_length=128)


@router.put("/settings/{field}")
def put_setting(
    field: str, body: SettingIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    actor = require_identity(request)
    return model_settings.set_override(db, field, body.model_id, actor.username)


@router.delete("/settings/{field}")
def delete_setting(field: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return model_settings.clear_override(db, field)


@workspace_router.get("/model-catalog")
def model_catalog(
    refresh: bool = False, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    return model_settings.catalog(ws.context, force=refresh)


class ProbeIn(BaseModel):
    field: str = Field(max_length=64)
    model_id: str = Field(max_length=128)


@workspace_router.post("/model-probe")
def model_probe(
    body: ProbeIn, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    return model_settings.probe(ws.context, body.field, body.model_id)
