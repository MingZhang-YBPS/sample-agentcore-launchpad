"""API key management — keys are stored as sha256 hashes, never plaintext.

A key records the console principal that minted it and the project it was
minted in (the request's current project, design projects §5.7); every route
here runs inside that project, so the list, the toggles and the usage only ever
see its keys — another project's key id reads as missing. Once the creator is
offboarded (or leaves the project) the key is disabled and cannot be re-enabled
(`services.api_keys`).
"""

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import false, true
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project
from app.models.ledger import Agent, ApiKey
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access, project_quotas
from app.services import api_keys as api_keys_service
from app.services.users import as_utc
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api", tags=["api-keys"])


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # T16 — all optional; omitted = the legacy unrestricted key.
    agent_ids: list[str] | None = Field(default=None, max_length=50)
    expires_at: datetime | None = None
    rate_per_minute: int | None = Field(default=None, ge=1, le=api_keys_service.MAX_RATE_PER_MINUTE)


class UpdateKeyRequest(BaseModel):
    """Only fields present in the body change; an explicit null clears the limit."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    agent_ids: list[str] | None = Field(default=None, max_length=50)
    expires_at: datetime | None = None
    rate_per_minute: int | None = Field(default=None, ge=1, le=api_keys_service.MAX_RATE_PER_MINUTE)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _key_out(key: ApiKey, offboarded: str | None = None) -> dict[str, Any]:
    last_used = as_utc(key.last_used_at)
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        # the project the key runs as on /v1 (design projects §5.7)
        "project_id": key.project_id,
        "enabled": key.enabled,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        # NULL on keys minted before creators were recorded — "unknown".
        "created_by": key.created_by,
        "created_by_name": key.created_by_name,
        # empty list = every agent in the workspace
        "agent_ids": list(key.agent_ids or []),
        "expires_at": _iso(key.expires_at),
        "expired": api_keys_service.is_expired(key),
        "rate_per_minute": key.rate_per_minute,
        "last_used_at": last_used.isoformat() if last_used else None,
        "use_count": key.use_count or 0,
        # why the creator may no longer hold this key, or null
        "creator_offboarded": offboarded,
    }


def _clean_scope(
    db: Session, ws: WorkspaceScope, agent_ids: list[str] | None, viewer: Viewer = Viewer.ALL
) -> list[str] | None:
    """De-duplicated scope, every id a live agent of this workspace that `viewer`
    may see (else 422 — a hidden agent reads like an unknown one)."""
    if not agent_ids:
        return None
    unique = list(dict.fromkeys(agent_ids))
    live = {
        a.id
        for a in db.query(Agent)
        .filter(
            Agent.workspace_id == ws.id,
            Agent.id.in_(unique),
            Agent.status != "deleted",
            agent_access.agent_clause(viewer),
        )
        .all()
    }
    unknown = [i for i in unique if i not in live]
    if unknown:
        raise AppError(
            "apikey.unknown_agent",
            "agent_ids must name existing agents of this workspace",
            {"agent_ids": unknown},
            status_code=422,
        )
    return unique


def _clean_expiry(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    if aware <= datetime.now(UTC):
        raise AppError(
            "apikey.expiry_in_past", "expires_at must be in the future", status_code=422
        )
    return aware


def _own_clause(viewer: Viewer) -> Any:
    """Under workspace isolation a member manages only the keys they minted.

    Stricter than the resource rule on purpose: a key is a credential, not a
    shared resource, so a key with no recorded creator (config admin, legacy) or
    an admin's key is not the member's to list or toggle. Admins and shared
    workspaces see every key (`true()`)."""
    if not viewer.filters:
        return true()
    return ApiKey.created_by == viewer.user_id if viewer.user_id else false()


def _key_in(
    db: Session, ws: WorkspaceScope, key_id: str, viewer: Viewer = Viewer.ALL
) -> ApiKey:
    key = db.get(ApiKey, key_id)
    if key is None or key.workspace_id != ws.id:
        raise NotFoundError("apikey.not_found", "api key not found")
    if viewer.filters and (not viewer.user_id or key.created_by != viewer.user_id):
        raise NotFoundError("apikey.not_found", "api key not found")
    return key


@router.get("/apikeys")
def list_keys(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    # the current project's keys only: the automatic criteria already confine the
    # query, the explicit term keeps it so should the route ever lose its project
    pid = current_project()
    keys = (
        db.query(ApiKey)
        .filter(
            ApiKey.workspace_id == ws.id,
            ApiKey.project_id == pid if pid is not None else true(),
            _own_clause(viewer),
        )
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    # one predicate evaluation per creator (the list is one project's in one workspace)
    reasons: dict[tuple[str | None, str | None], str | None] = {}
    out = []
    for key in keys:
        slot = (key.created_by, key.project_id)
        if slot not in reasons:
            reasons[slot] = api_keys_service.creator_offboarded(db, key)
        out.append(_key_out(key, reasons[slot]))
    return {"keys": out}


@router.post("/apikeys", status_code=201)
def create_key(
    req: CreateKeyRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    identity = require_identity(request)
    # the project's key quota (projects §11.3): enabled keys count
    project_quotas.assert_capacity(db, current_project(), "max_api_keys")
    raw = f"lp_live_{secrets.token_hex(16)}"
    # The key's workspace is what scopes it on /v1 — a key minted here reaches
    # this environment's agents only. Its creator is what keeps it alive: the
    # `users.id` when the principal has a row (the config admin and the
    # auth-disabled operator do not, so their keys are never offboarded). Its
    # project is the request's: /v1 runs every call of the key as that project.
    key = ApiKey(
        workspace_id=ws.id,
        project_id=current_project(),
        name=req.name,
        prefix=raw[:12] + "…",
        key_hash=hash_key(raw),
        agent_ids=_clean_scope(db, ws, req.agent_ids, viewer),
        expires_at=_clean_expiry(req.expires_at),
        rate_per_minute=req.rate_per_minute,
        created_by=identity.user_id,
        created_by_name=identity.username,
    )
    db.add(key)
    db.flush()
    project_quotas.recheck_after_insert(db, key.project_id, "max_api_keys")
    db.commit()
    # The full key is returned exactly once; only the hash is at rest.
    return {**_key_out(key), "key": raw}


@router.post("/apikeys/{key_id}/disable")
def disable_key(
    key_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    key = _key_in(db, ws, key_id, viewer)
    key.enabled = False
    db.commit()
    return _key_out(key, api_keys_service.creator_offboarded(db, key))


@router.post("/apikeys/{key_id}/enable")
def enable_key(
    key_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    key = _key_in(db, ws, key_id, viewer)
    reason = api_keys_service.creator_offboarded(db, key)
    if reason is not None:
        # A member may toggle a key they can manage, but not revive one whose creator
        # was offboarded — /v1 would refuse (and re-disable) it on first use.
        raise AppError(
            "apikey.creator_offboarded",
            "this key's creator no longer has access to its project in this workspace "
            "(or the project is archived); mint a new key",
            {"reason": reason},
            status_code=409,
        )
    if not key.enabled:
        # a disabled key frees its slot, so re-enabling takes one (max_api_keys, §11.3)
        project_quotas.assert_capacity(db, key.project_id, "max_api_keys")
    key.enabled = True
    db.commit()
    return _key_out(key)


@router.patch("/apikeys/{key_id}")
def update_key(
    key_id: str,
    req: UpdateKeyRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    key = _key_in(db, ws, key_id, viewer)
    sent = req.model_fields_set
    if "name" in sent and req.name is not None:
        key.name = req.name
    if "agent_ids" in sent:
        key.agent_ids = _clean_scope(db, ws, req.agent_ids, viewer)
    if "expires_at" in sent:
        key.expires_at = _clean_expiry(req.expires_at)
    if "rate_per_minute" in sent:
        key.rate_per_minute = req.rate_per_minute
    db.commit()
    return _key_out(key, api_keys_service.creator_offboarded(db, key))


@router.get("/apikeys/{key_id}/usage")
def key_usage(
    key_id: str,
    days: int = 14,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Admitted `/v1` calls per UTC day, oldest first (dense, zero-filled)."""
    key = _key_in(db, ws, key_id, viewer)
    return {
        "key_id": key.id,
        "total": key.use_count or 0,
        "last_used_at": _iso(key.last_used_at),
        "days": api_keys_service.usage_by_day(db, key.id, max(1, min(days, 90))),
    }
