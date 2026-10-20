"""Coding repos: the private-owner choke point, CRUD and the response shapes.

**Access rule** (parent design §7.2, deliberately stricter than
``resource_visibility``: a repo is a live shell plus code). A repo belongs
to the principal that created it (``assistant/principal.principal_of``) in every
workspace visibility mode. Every by-id route resolves through ``repo_in``; a
foreign or missing id is ``404 coding.repo_not_found`` — indistinguishable.
Administrators pass only where the route asks for it (``admin_ok=True``): the
workspace-wide list (``?all=1``) and DELETE, for cleanup. They can never open a
repo's IDE, read its files, stream its events or run a turn in it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.assistant.principal import principal_of
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent
from app.routers.auth import Identity
from app.services import creators
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import data_client
from app.services.creators import Creator
from app.services.workspace import WorkspaceContext

from . import runtime_client, s3urls, snapshots
from .models import (
    DESCRIPTION_MAX,
    NAME_MAX,
    REPO_DELETING,
    REPO_IDLE,
    REPO_RUNNING,
    CodingRepo,
    CodingTurn,
)

logger = logging.getLogger("launchpad.coding")

TURNS_IN_DETAIL = 20


def now() -> datetime:
    return datetime.now(UTC)


def not_found() -> NotFoundError:
    return NotFoundError("coding.repo_not_found", "coding repo not found")


def turn_running() -> AppError:
    return AppError("coding.turn_running", "a turn is running in this repo",
                    status_code=409)


# ── the choke point ─────────────────────────────────────────────────────────


def owns(row: CodingRepo | None, identity: Identity) -> bool:
    """The caller's principal is the repo's owner. A NULL owner matches nobody."""
    return (
        row is not None
        and row.owner_principal is not None
        and row.owner_principal == principal_of(identity)
    )


def repo_in(
    db: Session,
    workspace_id: str,
    repo_id: str,
    identity: Identity,
    *,
    admin_ok: bool = False,
) -> CodingRepo | None:
    """The repo when it lives in this workspace and the caller owns it — or,
    with ``admin_ok``, when the caller is an administrator. Else None."""
    if not repo_id:
        return None
    row = db.get(CodingRepo, repo_id)
    if row is None or row.workspace_id != workspace_id:
        return None
    if owns(row, identity):
        return row
    if admin_ok and identity.is_admin:
        return row
    return None


def require_repo(
    db: Session, workspace_id: str, repo_id: str, identity: Identity, *,
    admin_ok: bool = False,
) -> CodingRepo:
    row = repo_in(db, workspace_id, repo_id, identity, admin_ok=admin_ok)
    if row is None:
        raise not_found()
    return row


# ── response shapes (parent design §7.3) ────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _live_agents(db: Session, workspace_id: str, ids: set[str]) -> set[str]:
    """Which deployed-Agent ids still exist (a deleted Agent is not redeployable:
    the next deploy creates a new one)."""
    if not ids:
        return set()
    rows = (
        db.query(Agent.id)
        .filter(Agent.id.in_(ids), Agent.workspace_id == workspace_id,
                Agent.status != "deleted")
        .all()
    )
    return {row.id for row in rows}


def repo_out(row: CodingRepo, identity: Identity, *,
                live_agents: set[str] | None = None) -> dict[str, Any]:
    agent_id = row.agent_id
    if agent_id and live_agents is not None and agent_id not in live_agents:
        agent_id = None
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        # `deleting` is a transient claim of an in-flight DELETE: still idle to a reader
        "status": REPO_RUNNING if row.status == REPO_RUNNING else REPO_IDLE,
        "active_turn_id": row.active_turn_id,
        "model": row.model,
        "effort": row.effort,
        "agent_id": agent_id,
        "snapshot_at": _iso(row.snapshot_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        **creators.fields(row),
        "owned": owns(row, identity),
    }


def turn_out(row: CodingTurn) -> dict[str, Any]:
    return {
        "id": row.id,
        "turn_id": row.turn_id,
        "thread_id": row.thread_id,
        "prompt": row.prompt or "",
        "model": row.model,
        "effort": row.effort,
        "status": row.status,
        "error": row.error,
        "usage": row.usage,
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
    }


def detail_out(db: Session, row: CodingRepo, identity: Identity) -> dict[str, Any]:
    """The repo + its newest ``TURNS_IN_DETAIL`` turns (oldest first)."""
    turns = (
        db.query(CodingTurn)
        .filter(CodingTurn.repo_id == row.id, CodingTurn.workspace_id == row.workspace_id)
        .order_by(CodingTurn.started_at.desc(), CodingTurn.id.desc())
        .limit(TURNS_IN_DETAIL)
        .all()
    )
    live = _live_agents(db, row.workspace_id or "", {row.agent_id} if row.agent_id else set())
    out = repo_out(row, identity, live_agents=live)
    out["turns"] = [turn_out(t) for t in reversed(turns)]
    out["active_turn"] = row.active_turn_id if row.status == REPO_RUNNING else None
    return out


# ── CRUD ────────────────────────────────────────────────────────────────────


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise AppError("validation.invalid_request", "a repo name is required",
                       status_code=422)
    return cleaned[:NAME_MAX]


def list_repos(db: Session, workspace_id: str, identity: Identity, *,
                  all_repos: bool = False) -> list[dict[str, Any]]:
    """The caller's own repos; ``all_repos`` (administrators) every repo
    of the workspace — for cleanup only (``owned`` tells the console which)."""
    query = db.query(CodingRepo).filter(CodingRepo.workspace_id == workspace_id)
    if not (all_repos and identity.is_admin):
        query = query.filter(CodingRepo.owner_principal == principal_of(identity))
    rows = query.order_by(CodingRepo.updated_at.desc(), CodingRepo.id.desc()).all()
    live = _live_agents(db, workspace_id, {r.agent_id for r in rows if r.agent_id})
    return [repo_out(r, identity, live_agents=live) for r in rows]


def create_repo(db: Session, workspace_id: str, identity: Identity, *, name: str,
                   description: str = "") -> CodingRepo:
    """A ledger row only — no runtime call: the session starts at the first action."""
    stamp = now()
    row = CodingRepo(
        workspace_id=workspace_id,
        owner_principal=principal_of(identity),
        name=_clean_name(name),
        description=(description or "").strip()[:DESCRIPTION_MAX],
        session_id=runtime_client.new_session_id(),
        created_at=stamp,
        updated_at=stamp,
    )
    creators.stamp(row, Creator.of(identity))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_repo(db: Session, row: CodingRepo, *, name: str | None = None,
                   description: str | None = None) -> CodingRepo:
    if name is not None:
        row.name = _clean_name(name)
    if description is not None:
        row.description = description.strip()[:DESCRIPTION_MAX]
    row.updated_at = now()
    db.commit()
    db.refresh(row)
    return row


def delete_repo(db: Session, ctx: WorkspaceContext, row: CodingRepo) -> None:
    """Stop the repo's runtime session (best effort), delete its snapshots and
    exports, then its rows. Refused while a turn runs. An S3 failure fails the
    request (the user's code must not outlive the repo silently); the rows stay
    so the delete can be retried."""
    repo_id, workspace_id = row.id, row.workspace_id or ""
    with snapshots.repo_lock(repo_id):
        # the claim (idle → deleting) shuts out a turn start for the whole delete
        claimed = db.execute(
            update(CodingRepo)
            .where(CodingRepo.id == repo_id, CodingRepo.status == REPO_IDLE)
            .values(status=REPO_DELETING, updated_at=now())
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if claimed != 1:
            raise turn_running()
        arn = runtime_client.installed_runtime_arn(ctx)
        if arn:
            try:
                rt.stop_runtime_session(data_client(ctx, read_timeout=60), runtime_arn=arn,
                                        session_id=row.session_id)
            except Exception as exc:  # noqa: BLE001 — an ended session is the goal anyway
                logger.info("coding repo %s: StopRuntimeSession: %s", repo_id, exc)
        try:
            s3urls.delete_repo_prefix(ctx, workspace_id, repo_id)
        except Exception as exc:  # noqa: BLE001 — surfaced, the delete is retryable
            logger.warning("coding repo %s: snapshot delete failed: %s", repo_id, exc)
            db.execute(
                update(CodingRepo)
                .where(CodingRepo.id == repo_id,
                       CodingRepo.status == REPO_DELETING)
                .values(status=REPO_IDLE)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            raise AppError(
                "coding.delete_failed",
                f"the repo's snapshots could not be deleted ({exc}); try again",
                status_code=502,
            ) from exc
        db.query(CodingTurn).filter(CodingTurn.repo_id == repo_id).delete(
            synchronize_session=False
        )
        db.query(CodingRepo).filter(CodingRepo.id == repo_id).delete(
            synchronize_session=False
        )
        db.commit()
