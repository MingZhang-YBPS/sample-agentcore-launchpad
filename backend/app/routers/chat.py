"""Chat playground endpoints — SSE streaming over the shared invoke chain."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.assistant.sessions import refuse_assistant_session
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError, envelope, mapped_aws_error
from app.core.project_scope import current_project, project_context
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.attachments import AttachmentRequest, SessionIdField
from app.services import agent_access, memory_ownership, oauth_sessions, policy_identity
from app.services import feedback as feedback_service
from app.services import inbound_auth as inbound_auth_service
from app.services import memory as memory_service
from app.services.attachments import prepare_attachments
from app.services.chat import chat_stream, sse_encode
from app.services.chat_ledger import persist_events
from app.services.invoke import stop_agent_session
from app.services.runtime_discovery import require_invoke_capability
from app.services.visibility import Viewer, require_viewer
from app.templates import gateway_support

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(AttachmentRequest):
    session_id: str | None = SessionIdField
    # "Invoke as me" for JWT-inbound agents (identity P3): True presents the
    # signed-in user's own Cognito JWT, False the workspace M2M token, None
    # (older clients) the user JWT when one can be minted, else M2M. Ignored
    # by IAM agents, whose caller is always the platform's SigV4 role.
    as_user: bool | None = None


def _session_actor(
    db: Session,
    workspace_id: str,
    agent_id: str,
    session_id: str | None,
    requested_actor: str,
) -> str:
    if not session_id:
        return requested_actor
    existing = (
        db.query(ChatSession.actor_id)
        .filter(
            ChatSession.workspace_id == workspace_id,
            ChatSession.agent_id == agent_id,
            ChatSession.session_id == session_id,
        )
        .first()
    )
    return existing[0] if existing and existing[0] else requested_actor


def _refuse_foreign_session(
    db: Session, workspace_id: str, agent_id: str, session_id: str | None,
) -> None:
    """A session id another project already holds for this agent is not continued
    here. Only a system preset (shared by every project) can reach this: without the
    check the turn would open a second ledger row in this project on the other
    project's AgentCore runtime session. It answers like a missing session."""
    pid = current_project()
    if not session_id or pid is None:
        return
    with project_context(None):
        foreign = (
            db.query(ChatSession.id)
            .filter(
                ChatSession.workspace_id == workspace_id,
                ChatSession.agent_id == agent_id,
                ChatSession.session_id == session_id,
                ChatSession.project_id != pid,
            )
            .first()
        )
    if foreign is not None:
        raise NotFoundError("chat.session_not_found", "chat session not found")


def _agent_in(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent:
    """The agent, or 404 — including when it belongs to another workspace or is
    hidden by workspace isolation, which the caller must not be able to tell apart
    from a missing one."""
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer, include_deleted=True)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return agent


def _get_active_agent(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent:
    agent = _agent_in(db, ws, agent_id, viewer)
    require_invoke_capability(agent)
    return agent


def _chat_bearer_token(
    agent: Agent,
    as_user: bool | None,
    ws: WorkspaceScope,
    identity: Any,
    gateway_access_token: str | None,
) -> str | None:
    """The bearer a JWT-inbound agent's chat turn presents, or None (= M2M).

    The user JWT is the same Cognito token the Gateway policy path mints
    (console app client, groups per role) — reused when this turn already
    minted it. The Memory/session actor is unaffected either way: it stays
    the compound ``scoped_actor`` the payload carries, so toggling mid-session
    keeps one conversation and one memory partition.
    """
    if not inbound_auth_service.is_jwt_mode(agent) or as_user is False:
        return None
    token = gateway_access_token or policy_identity.gateway_user_token(
        ws.context, identity.username, identity.role, identity.email,
    )
    if token is None and as_user:
        # An explicit "as me" with no signed-in pool user (auth gate off) must
        # not quietly become the machine identity.
        raise AppError(
            "chat.as_user_unavailable",
            "invoking as yourself needs a console sign-in through the workspace "
            "user pool — turn off \"invoke as me\" to call with the workspace "
            "machine token",
            status_code=409,
        )
    return token


def _console_events(events: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Rewrite an as_user refusal for the console: its fix is the "invoke as me"
    toggle, not a user JWT. Runs before persistence, so the saved error matches."""
    for event in events:
        data = event["data"]
        code = data.get("code")
        if event["event"] == "error" and code == oauth_sessions.AS_USER_REQUIRES_USER_JWT:
            detail = data.get("detail") or {}
            refusal = oauth_sessions.as_user_requires_user_jwt(
                provider=detail.get("provider", ""), tool=detail.get("tool", ""),
                agent_id=detail.get("agent_id", ""), console=True,
            )
            data = envelope(refusal.code, refusal.message, refusal.detail)
            event = {"event": "error", "data": data}
        yield event


@router.post("/chat/{agent_id}")
def chat(
    agent_id: str,
    req: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> StreamingResponse:
    agent = _get_active_agent(db, ws, agent_id, viewer)
    # An assistant-owned session of the preset is private to the assistant page.
    refuse_assistant_session(agent, req.session_id)
    _refuse_foreign_session(db, ws.id, agent.id, req.session_id)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    identity = require_identity(request)
    human_actor = identity.username if auth_enabled() else "river"

    # Memory partitions per agent: the runtime writes short-term events and the
    # extractor lands long-term records under this compound actor. The ledger
    # still records the bare authenticated username. A continuing session keeps
    # its original Memory owner, while policy authorization follows the current
    # signed-in caller.
    actor_id = _session_actor(db, ws.id, agent.id, req.session_id, human_actor)
    mem_actor = memory_service.scoped_actor(agent.id, actor_id)
    needs_gateway_identity = (
        gateway_support.runtime_user_id(agent.spec, identity.username) is not None
    )
    gateway_access_token = (
        policy_identity.gateway_user_token(
            ws.context,
            identity.username,
            identity.role,
            identity.email,
        )
        if needs_gateway_identity
        else None
    )
    bearer_token = _chat_bearer_token(agent, req.as_user, ws, identity, gateway_access_token)

    # The stream outlives the request scope, so it carries the plain id and the
    # already-built context rather than reaching back for the resolved scope.
    workspace_id = ws.id
    workspace = ws.context

    def generate():
        stream_kwargs: dict[str, Any] = {}
        if prepared:
            stream_kwargs["attachments"] = prepared
        if needs_gateway_identity:
            stream_kwargs["runtime_user_id"] = identity.username
        if gateway_access_token:
            stream_kwargs["gateway_access_token"] = gateway_access_token
        if bearer_token:
            stream_kwargs["bearer_token"] = bearer_token
        events = _console_events(chat_stream(
            agent,
            req.prompt,
            session_id=req.session_id,
            actor_id=mem_actor,
            workspace=workspace,
            status_events=True,  # console only: phase / thinking / tool_end (chat-streaming S2)
            **stream_kwargs,
        ))
        # Thread items are persisted in event order (int-pk = replay order) so
        # the playground can restore a session's history exactly as rendered.
        for event in persist_events(
            events,
            workspace_id=workspace_id,
            agent_id=agent.id,
            prompt=req.prompt,
            actor_id=actor_id,
            session_id=req.session_id,
            runtime_version=agent.version,
            attachments=prepared.metadata if prepared else None,
        ):
            # a named heartbeat (with `elapsed_ms`) lets the page time the turn and
            # tell a live connection from a dead one; /v1 keeps the comment
            yield sse_encode(event, named_heartbeat=True)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/{agent_id}/sessions")
def list_sessions(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _agent_in(db, ws, agent_id, viewer)
    # Only sessions with a replayable transcript: rows that predate the
    # ChatMessage ledger have nothing to open (clicking them showed an empty
    # thread with an id-only preview), so they are filtered out here.
    # The correlated exists matches on session_id alone, so it carries the
    # workspace predicate itself — a bare id match can cross environments.
    rows = (
        db.query(ChatSession)
        .filter(
            ChatSession.workspace_id == ws.id,
            ChatSession.agent_id == agent_id,
            exists().where(
                ChatMessage.session_id == ChatSession.session_id,
                ChatMessage.workspace_id == ws.id,
            ),
        )
        .order_by(ChatSession.last_at.desc())
        .limit(50)
        .all()
    )

    def preview(session_id: str) -> str:
        first = (
            db.query(ChatMessage.text)
            .filter(
                ChatMessage.workspace_id == ws.id,
                ChatMessage.session_id == session_id,
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.id.asc())
            .first()
        )
        return (first[0] if first else "")[:120]

    return {
        "sessions": [
            {
                "session_id": r.session_id,
                "actor_id": r.actor_id,
                "turns": r.turns,
                "tokens": {
                    "input": r.input_tokens or 0,
                    "output": r.output_tokens or 0,
                    "total": r.total_tokens or 0,
                },
                "last_at": r.last_at.isoformat() if r.last_at else None,
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "preview": preview(r.session_id),
            }
            for r in rows
        ]
    }


@router.post("/chat/{agent_id}/sessions/{session_id}/stop")
def stop_session(
    agent_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """End the live AgentCore Runtime session behind a conversation.

    The ledger row is kept (its transcript stays replayable) and stamped
    `ended_at`; only the runtime session is terminated, so the next prompt starts
    a fresh session on the runtime's current version. A session already gone on
    the AWS side is a success with `already_ended: true`. Harness agents have no
    session-stop operation → 409 `chat.session_stop_unsupported`.
    """
    agent = _agent_in(db, ws, agent_id, viewer)
    row = (
        db.query(ChatSession)
        .filter(
            ChatSession.workspace_id == ws.id,
            ChatSession.agent_id == agent.id,
            ChatSession.session_id == session_id,
        )
        .first()
    )
    if row is None:
        # Another agent's or workspace's session must look like a missing one.
        raise NotFoundError("chat.session_not_found", "chat session not found")
    result = stop_agent_session(agent, session_id, workspace=ws.context)
    if row.ended_at is None:
        row.ended_at = datetime.now(UTC)
        db.commit()
        db.refresh(row)  # the rendered timestamp is the stored one, as the list reads it
    return {
        "session_id": session_id,
        "ended": result["ended"],
        "already_ended": result["already_ended"],
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
    }


@router.get("/chat/{agent_id}/history")
def session_history(
    agent_id: str,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Replay a session's thread exactly as it was rendered (int pk = order)."""
    _agent_in(db, ws, agent_id, viewer)
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.workspace_id == ws.id,
            ChatMessage.agent_id == agent_id,
            ChatMessage.session_id == session_id,
        )
        .order_by(ChatMessage.id.asc())
        .limit(500)
        .all()
    )
    mine = feedback_service.verdicts_for(
        db, ws.id,
        require_identity(request).username if auth_enabled() else "river",
        [r.id for r in rows if r.role == "agent"],
    )
    return {
        "messages": [
            {
                "id": r.id,
                "verdict": mine.get(r.id),
                "role": r.role,
                "text": r.text,
                "name": r.name,
                "attachments": r.attachments or [],
                "answered_by": r.answered_by,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/chat/{agent_id}/memory")
def session_memory(
    agent_id: str,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = _agent_in(db, ws, agent_id, viewer)
    # an agent that pins its own memory writes there — read it back, but only
    # from a memory this workspace manages (issue #55)
    pinned = memory_ownership.readable_spec_memory_id(db, ws.context, agent.spec)
    try:
        # Read back the same agent-scoped partition the chat write path uses.
        session_actor = _session_actor(
            db,
            ws.id,
            agent_id,
            session_id,
            require_identity(request).username if auth_enabled() else "river",
        )
        mem_actor = memory_service.scoped_actor(agent_id, session_actor)
        # Echo the compound actor so the rail can deep-link into the Memory
        # console at the exact partition it just summarised — the console keys
        # on this id, and re-deriving it in the frontend would fork the scoping
        # rule (a session may have recorded a different human actor entirely).
        return {
            **memory_service.session_memory_summary(
                ws.context,
                mem_actor,
                session_id,
                memory_id=pinned,
            ),
            "actor_id": mem_actor,
        }
    except Exception as exc:
        if mapped_aws_error(exc):
            raise  # translatable AWS error → 4xx envelope via app.core.errors
        raise AppError(
            "memory.unavailable", f"memory lookup failed: {exc}", status_code=502
        ) from exc
