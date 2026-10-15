"""Memory console API — read-only views over the shared AgentCore Memory.

Thin router over ``app.services.memory_console`` plus the ledger joins that turn
raw AWS identifiers into console-legible rows (compound actor id → agent name,
session id → chat-session metadata). The service layer stays AWS-only so tests
can inject stub clients.

**Read-only by construction:** there is no handler here that mutates memory. See
``app/services/memory_console.py`` for the matching guarantee on the service side.

**Per-project memory (projects design §10.4).** The memory read is the request
project's (``project_memory.effective_memory_id``): an isolated project's own memory
once provisioned, the workspace's bootstrap memory otherwise. During an isolated
project's dual-read window the actors of its own agents in the old shared memory are
read as well (``project_memory.DualRead``): the first page of actors / sessions /
records / extraction jobs merges them in, every item then carrying ``source``
(``project`` | ``shared``), an empty session falls back to the shared one, and
``source=shared`` reads the shared memory explicitly. An actor of another project in
the shared memory answers 404. A shared project — every single-project workspace —
sees no ``source`` keys and exactly the responses it saw before.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError, mapped_aws_error
from app.core.project_scope import current_project
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import memory_console, memory_ownership, project_memory
from app.services.agent_access import HiddenAgents
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/memory", tags=["memory"])

SOURCE_PROJECT = "project"
SOURCE_SHARED = "shared"
Source = Literal["project", "shared"]
# legacy actors merged into the first actors page: ListActors pages of 100
_LEGACY_ACTOR_PAGES = 5


class SearchRequest(BaseModel):
    """Semantic retrieval over one namespace (RetrieveMemoryRecords)."""

    query: str = Field(min_length=1, max_length=2000)
    actor_id: str | None = None
    strategy_id: str | None = None
    namespace: str | None = None
    top_k: int = Field(default=5, ge=1, le=100)


def _guard(fn, *args, **kwargs):
    """Map AWS/botocore failures onto the ``memory.unavailable`` envelope.

    Domain errors raised by the service (``memory.not_configured``) already carry
    a code, so they pass through untouched — mirrors ``routers/chat.py``. An AWS
    ``ClientError`` the console can translate (``ResourceNotFoundException`` for
    an unknown actor, ``AccessDeniedException``, …) is left to the global handler
    in ``app.core.errors`` so the toast reads "not found", not raw boto text.
    """
    try:
        return fn(*args, **kwargs)
    except AppError:
        raise
    except Exception as exc:  # botocore ClientError, endpoint errors, ...
        if mapped_aws_error(exc):
            raise
        raise AppError(
            "memory.unavailable", f"memory lookup failed: {exc}", status_code=502
        ) from exc


def _dual(ws: WorkspaceScope) -> project_memory.DualRead | None:
    return project_memory.dual_read(ws.context)


def _actor_not_found() -> NotFoundError:
    return NotFoundError("memory.actor_not_found", "memory actor not found")


def _shared_for_actor(dual: project_memory.DualRead | None, actor_id: str | None) -> str:
    """`source=shared`: the shared memory id, for an actor of the project's own agents."""
    if dual is None or not dual.allows_actor(actor_id):
        raise _actor_not_found()
    return dual.memory_id


def _shared_for_namespace(dual: project_memory.DualRead | None, namespace: str) -> str:
    if dual is None or not dual.allows_namespace(namespace):
        raise _actor_not_found()
    return dual.memory_id


def _aws_not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code == "ResourceNotFoundException"


def _tolerant(fn, *args, **kwargs) -> dict[str, Any]:
    """A dual-read primary read: an actor the project memory has never seen is an
    empty page there (AWS answers ResourceNotFound), not an error."""
    try:
        return _guard(fn, *args, **kwargs)
    except Exception as exc:
        if _aws_not_found(exc):
            return {"items": [], "next_token": None}
        raise


def _tag(items: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    for item in items:
        item["source"] = source
    return items


def _merge(primary: list[dict[str, Any]], legacy: list[dict[str, Any]],
           key: str) -> list[dict[str, Any]]:
    seen = {item.get(key) for item in primary}
    return _tag(primary, SOURCE_PROJECT) + _tag(
        [item for item in legacy if item.get(key) not in seen], SOURCE_SHARED
    )


def _legacy_actors(ws: WorkspaceScope, dual: project_memory.DualRead) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(_LEGACY_ACTOR_PAGES):
        page = _guard(memory_console.list_actors, ws.context, token, None,
                      memory_id=dual.memory_id)
        out += [a for a in page["items"] if dual.allows_actor(a["actor_id"])]
        token = page.get("next_token")
        if not token:
            break
    return out


def _agent_names(db: Session, ws: WorkspaceScope, agent_ids: list[str]) -> dict[str, str]:
    """One batched lookup per page — never N+1 across the actor list.

    Scoped to the request's workspace: an actor id names an agent of *this*
    environment, and an id that resolves elsewhere stays nameless rather than
    leaking a foreign agent's name.
    """
    ids = [i for i in dict.fromkeys(agent_ids) if i]
    if not ids:
        return {}
    rows = (
        db.query(Agent.id, Agent.name)
        .filter(Agent.workspace_id == ws.id, Agent.id.in_(ids))
        .all()
    )
    return {row[0]: row[1] for row in rows}


def _hidden(db: Session, ws: WorkspaceScope, viewer: Viewer) -> HiddenAgents:
    """Agents workspace isolation hides from the caller (empty, no query, unless the
    viewer filters). Their actors (`<agent>__<human>`) are dropped from lists and
    every read keyed by such an actor — or a namespace carrying it — answers 404."""
    return HiddenAgents(db, viewer, ws.id)


def _require_actor_visible(hidden: HiddenAgents, actor_id: str | None) -> None:
    if hidden.hides_actor(actor_id):
        raise NotFoundError("memory.actor_not_found", "memory actor not found")


def _require_namespace_visible(hidden: HiddenAgents, namespace: str | None) -> None:
    if hidden.hides_namespace(namespace):
        raise NotFoundError("memory.actor_not_found", "memory actor not found")


def _resolve_namespace(
    ws: WorkspaceScope,
    namespace: str | None,
    actor_id: str | None,
    strategy_id: str | None,
) -> str:
    """Explicit namespace wins; otherwise derive it from (actor, strategy)."""
    if namespace:
        return namespace
    if not actor_id:
        raise AppError(
            "memory.namespace_required",
            "Provide either `namespace`, or `actor_id` (+ optional `strategy_id`).",
            status_code=400,
        )
    candidates = _guard(memory_console.resolve_namespaces, ws.context, actor_id)
    usable = [c for c in candidates if c["resolvable"]]
    if strategy_id:
        usable = [c for c in usable if c["strategy_id"] == strategy_id]
    if not usable:
        raise AppError(
            "memory.namespace_required",
            "No resolvable namespace for this actor/strategy combination.",
            status_code=400,
        )
    return usable[0]["namespace"]


@router.get("/overview")
def overview(
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    out = _guard(memory_console.memory_overview, ws.context)
    # AWS lists every memory of the account; one another project manages is not
    # named here (it would read as an unmanaged sibling). `actor_count` stays the
    # bootstrap memory's bounded, workspace-wide count — a number, no actor ids.
    foreign = memory_ownership.foreign_ids(db, ws.context)
    # nor another isolated project's own memory (§10.4)
    foreign = set(foreign or ()) | project_memory.project_memory_ids(
        ws.id, exclude_project=current_project()
    )
    own = project_memory.project_memory(current_project(), ws.id)
    if own is not None:
        dual = _dual(ws)
        out["project_memory"] = {
            "project_id": own.project_id,
            "memory_id": own.memory_id,
            "shared_memory_id": dual.memory_id if dual else None,
            "dual_read_until": (
                own.dual_read_until.isoformat() if dual and own.dual_read_until else None
            ),
        }
    if foreign:
        out["other_memories"] = [
            m for m in out.get("other_memories") or [] if (m.get("id") or "") not in foreign
        ]
    return out


@router.get("/actors")
def actors(
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    page = _guard(memory_console.list_actors, ws.context, next_token, max_results)
    dual = _dual(ws)
    if dual is not None:
        # the first page also names the project's actors still in the shared memory
        legacy = _legacy_actors(ws, dual) if not next_token else []
        page["items"] = _merge(page["items"], legacy, "actor_id")
    hidden = _hidden(db, ws, viewer)
    if hidden:
        page["items"] = [a for a in page["items"] if not hidden.hides_actor(a["actor_id"])]
    names = _agent_names(
        db, ws, [a["agent_id"] for a in page["items"] if a["agent_id"]]
    )
    for item in page["items"]:
        # A scoped actor whose agent row is gone keeps scoped=True with a null
        # name — the memory partition outlives the agent it belonged to.
        item["agent_name"] = names.get(item["agent_id"] or "")
    return page


@router.get("/sessions")
def sessions(
    actor_id: str = Query(min_length=1),
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    source: Source | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer)
    _require_actor_visible(hidden, actor_id)
    dual = _dual(ws)
    if source == SOURCE_SHARED:
        page = _guard(memory_console.list_sessions, ws.context, actor_id, next_token,
                      max_results, memory_id=_shared_for_actor(dual, actor_id))
        _tag(page["items"], SOURCE_SHARED)
    elif dual is not None and dual.allows_actor(actor_id) and source is None:
        page = _tolerant(memory_console.list_sessions, ws.context, actor_id,
                         next_token, max_results)
        legacy = [] if next_token else _tolerant(
            memory_console.list_sessions, ws.context, actor_id, None, max_results,
            memory_id=dual.memory_id,
        )["items"]
        page["items"] = _merge(page["items"], legacy, "session_id")
    else:
        page = _guard(
            memory_console.list_sessions, ws.context, actor_id, next_token, max_results
        )
    if hidden:
        # an unscoped actor ("default") is shared across agents: drop the sessions
        # the ledger attributes to a hidden agent (chat / eval run)
        page["items"] = [
            s for s in page["items"] if not hidden.hides_session(s.get("session_id"))
        ]
    session_ids = [s["session_id"] for s in page["items"] if s["session_id"]]
    ledger: dict[str, dict[str, Any]] = {}
    if session_ids:
        rows = (
            db.query(ChatSession, Agent.name)
            .outerjoin(Agent, Agent.id == ChatSession.agent_id)
            .filter(
                ChatSession.workspace_id == ws.id,
                ChatSession.session_id.in_(session_ids),
            )
            .all()
        )
        counts = {
            row[0]: row[1]
            for row in db.query(ChatMessage.session_id, func.count(ChatMessage.id))
            .filter(
                ChatMessage.workspace_id == ws.id,
                ChatMessage.session_id.in_(session_ids),
            )
            .group_by(ChatMessage.session_id)
            .all()
        }
        for chat_session, agent_name in rows:
            ledger[chat_session.session_id] = {
                "agent_id": chat_session.agent_id,
                "agent_name": agent_name,
                "human_actor": chat_session.actor_id,
                "turns": chat_session.turns,
                "message_count": counts.get(chat_session.session_id, 0),
            }
    for item in page["items"]:
        # None for sessions the console never wrote (eval runs, /v1 API callers).
        item["ledger"] = ledger.get(item["session_id"])
    return page


@router.get("/events")
def events(
    actor_id: str = Query(min_length=1),
    session_id: str = Query(min_length=1),
    include_payloads: bool = True,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    source: Source | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer)
    _require_actor_visible(hidden, actor_id)
    if hidden.hides_session(session_id):
        raise NotFoundError("memory.actor_not_found", "memory actor not found")
    args = (ws.context, actor_id, session_id, include_payloads, next_token, max_results)
    dual = _dual(ws)
    if source == SOURCE_SHARED:
        page = _guard(memory_console.list_events, *args,
                      memory_id=_shared_for_actor(dual, actor_id))
        return {**page, "source": SOURCE_SHARED}
    if dual is None or not dual.allows_actor(actor_id) or source is not None:
        return _guard(memory_console.list_events, *args)
    page = _tolerant(memory_console.list_events, *args)
    if page["items"] or page.get("next_token") or next_token:
        return {**page, "source": SOURCE_PROJECT}
    # a session from before the agent moved to the project memory
    page = _tolerant(memory_console.list_events, *args, memory_id=dual.memory_id)
    return {**page, "source": SOURCE_SHARED}


@router.get("/namespaces")
def namespaces(
    actor_id: str = Query(min_length=1),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_actor_visible(_hidden(db, ws, viewer), actor_id)
    return {"items": _guard(memory_console.resolve_namespaces, ws.context, actor_id)}


@router.get("/records")
def records(
    actor_id: str | None = None,
    strategy_id: str | None = None,
    namespace: str | None = None,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    source: Source | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer)
    _require_actor_visible(hidden, actor_id)
    _require_namespace_visible(hidden, namespace)
    resolved = _resolve_namespace(ws, namespace, actor_id, strategy_id)
    _require_namespace_visible(hidden, resolved)
    args = (ws.context, resolved, strategy_id, next_token, max_results)
    dual = _dual(ws)
    if source == SOURCE_SHARED:
        page = _guard(memory_console.list_records, ws.context, resolved, None,
                      next_token, max_results,
                      memory_id=_shared_for_namespace(dual, resolved))
        _tag(page["items"], SOURCE_SHARED)
        return page
    page = _guard(memory_console.list_records, *args)
    if (dual is not None and source is None and not next_token
            and dual.allows_namespace(resolved)):
        # the shared memory's strategy ids differ from the project memory's: the
        # namespace alone selects there
        legacy = _tolerant(memory_console.list_records, ws.context, resolved, None, None,
                           max_results, memory_id=dual.memory_id)
        page["items"] = _merge(page["items"], legacy["items"], "record_id")
    return page


@router.post("/records/search")
def search_records(
    req: SearchRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer)
    _require_actor_visible(hidden, req.actor_id)
    _require_namespace_visible(hidden, req.namespace)
    resolved = _resolve_namespace(ws, req.namespace, req.actor_id, req.strategy_id)
    _require_namespace_visible(hidden, resolved)
    out = _guard(
        memory_console.search_records,
        ws.context,
        resolved,
        req.query,
        req.top_k,
        req.strategy_id,
    )
    dual = _dual(ws)
    if dual is not None and dual.allows_namespace(resolved):
        legacy = _tolerant(memory_console.search_records, ws.context, resolved,
                           req.query, req.top_k, None, memory_id=dual.memory_id)
        merged = _merge(out["items"], legacy["items"], "record_id")
        out["items"] = sorted(merged, key=lambda r: -(r.get("score") or 0))[: req.top_k]
    return out


@router.get("/extraction-jobs")
def extraction_jobs(
    actor_id: str | None = None,
    session_id: str | None = None,
    strategy_id: str | None = None,
    status: str | None = None,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    source: Source | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer)
    _require_actor_visible(hidden, actor_id)
    dual = _dual(ws)
    if source == SOURCE_SHARED:
        # the shared memory holds every project's jobs: only one actor's are listed
        page = _guard(memory_console.list_extraction_jobs, ws.context, actor_id,
                      session_id, None, status, next_token, max_results,
                      memory_id=_shared_for_actor(dual, actor_id))
        _tag(page["items"], SOURCE_SHARED)
        return page
    page = _guard(
        memory_console.list_extraction_jobs,
        ws.context,
        actor_id,
        session_id,
        strategy_id,
        status,
        next_token,
        max_results,
    )
    if (dual is not None and source is None and not next_token
            and dual.allows_actor(actor_id)):
        legacy = _guard(memory_console.list_extraction_jobs, ws.context, actor_id,
                        session_id, None, status, None, max_results,
                        memory_id=dual.memory_id)
        page["items"] = _merge(page["items"], legacy["items"], "job_id")
    if hidden:
        page["items"] = [
            j for j in page["items"] if not hidden.hides_actor(j.get("actor_id"))
        ]
    return page
