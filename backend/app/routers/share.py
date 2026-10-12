"""Share links: the console side (create / list / revoke) and the public `/share`
surface an outsider uses with nothing but the link (T13, T14).

The public router is deliberately *outside* `/api`: it carries no console session
and never reads `X-Workspace`. The token resolves to its row, and the row names
the workspace (`services/share_links.resolve`). It reuses the one invoke chain —
`chat_stream` — so a shared agent behaves exactly like the console's.
"""

import html
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotFoundError
from app.models.ledger import Agent, ChatSession, ShareLink
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import display_name_of
from app.schemas.attachments import SessionIdField
from app.services import agent_access, channels, share_links
from app.services import feedback as feedback_service
from app.services import memory as memory_service
from app.services.agentcore.harness import new_session_id
from app.services.chat import chat_stream, sse_encode
from app.services.chat_ledger import persist_events
from app.services.workspace import get_workspace_row, workspace_context

# ── console (member) ────────────────────────────────────────────────────────

console_router = APIRouter(
    prefix="/api",
    tags=["share-links"],
    # workspace isolation: a hidden `{agent_id}` / `{run_id}` reads as missing
    dependencies=[Depends(agent_access.require_path_ids_visible)],
)

SHARE_PAGE_PATH = "/s"


def embed_snippet(embed_url: str, label: str = "") -> str:
    """Copy-ready `<iframe>` for the chromeless share page (`/s/<token>?embed=1`).

    Built here, once, beside the token it embeds: the snippet is as secret as the link
    and is shown only in the creation response. Attribute values are escaped so a link
    label can never break out of the tag.
    """
    title = html.escape(label.strip() or "Assistant", quote=True)
    return (
        f'<iframe src="{html.escape(embed_url, quote=True)}" title="{title}" '
        'style="width:100%;height:600px;border:0" allow="clipboard-write" '
        'loading="lazy"></iframe>'
    )


class CreateShareLink(BaseModel):
    label: str = Field(default="", max_length=64)
    # None = never expires. The console defaults to a finite window.
    expires_in_days: int | None = Field(default=None, ge=1, le=share_links.MAX_EXPIRY_DAYS)


def _iso(value: datetime | None) -> str | None:
    # SQLite returns timezone-aware columns naive; they are stored UTC.
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _link_out(link: ShareLink) -> dict[str, Any]:
    out = _link_fields(link)
    channel = channels.describe(link)  # platform + which secrets are set, never a value
    if channel is not None:
        out["channel"] = channel
    return out


def _link_fields(link: ShareLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "kind": link.kind,
        "agent_id": link.target_id,
        "label": link.label,
        "prefix": link.prefix,
        "state": share_links.link_state(link),
        "created_by": link.created_by,
        "expires_at": _iso(link.expires_at),
        "revoked_at": _iso(link.revoked_at),
        "last_used_at": _iso(link.last_used_at),
        "use_count": link.use_count or 0,
        "created_at": _iso(link.created_at),
    }


def _agent_in(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != ws.id:
        raise NotFoundError("agent.not_found", "agent not found")
    return agent


def _link_in(db: Session, ws: WorkspaceScope, link_id: str) -> ShareLink:
    link = db.get(ShareLink, link_id)
    if link is None or link.workspace_id != ws.id:
        raise NotFoundError("share.link_not_found", "share link not found")
    return link


@console_router.get("/agents/{agent_id}/share-links")
def list_share_links(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent_in(db, ws, agent_id)
    rows = (
        db.query(ShareLink)
        .filter(ShareLink.workspace_id == ws.id, ShareLink.target_id == agent_id)
        .order_by(ShareLink.created_at.desc())
        .all()
    )
    return {"links": [_link_out(r) for r in rows]}


@console_router.post("/agents/{agent_id}/share-links", status_code=201)
def create_share_link(
    agent_id: str,
    req: CreateShareLink,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = share_links.shareable_agent(_agent_in(db, ws, agent_id))
    identity = require_identity(request)
    link, raw = share_links.create_link(
        db,
        workspace_id=ws.id,
        agent=agent,
        label=req.label,
        created_by=identity.username if auth_enabled() else "river",
        expires_in_days=req.expires_in_days,
    )
    path = f"{SHARE_PAGE_PATH}/{raw}"
    origin = (request.headers.get("origin") or "").rstrip("/")
    # The token is returned exactly once; only its hash is at rest.
    embed_url = f"{origin}{path}?embed=1"
    return {
        **_link_out(link),
        "token": raw,
        "path": path,
        "url": f"{origin}{path}",
        "embed_url": embed_url,
        "embed_snippet": embed_snippet(embed_url, link.label),
    }


@console_router.post("/share-links/{link_id}/revoke")
def revoke_share_link(
    link_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    link = _link_in(db, ws, link_id)
    if link.revoked_at is None:  # idempotent: the first revocation time stands
        link.revoked_at = datetime.now(UTC)
        link.enabled = False
        db.commit()
    share_links.limiter.forget(link.id)
    return _link_out(link)


# ── public (no session, no X-Workspace) ─────────────────────────────────────

router = APIRouter(prefix="/share", tags=["share"])

_GENERIC_ERROR = "The assistant could not answer right now. Please try again."


class ShareChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    session_id: str | None = SessionIdField


class ShareFeedbackRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=80)
    message_id: int = Field(ge=1)
    verdict: Literal["up", "down", "none"]
    comment: str | None = Field(default=None, max_length=feedback_service.MAX_COMMENT)


def _owned_session(db: Session, link: ShareLink, agent: Agent, session_id: str) -> ChatSession:
    """A session this link started — a visitor cannot resume (or rate) a console
    user's conversation, or another link's, by presenting its id."""
    row = (
        db.query(ChatSession)
        .filter(
            ChatSession.workspace_id == link.workspace_id,
            ChatSession.agent_id == agent.id,
            ChatSession.session_id == session_id,
        )
        .first()
    )
    if row is None or not share_links.owns_session_actor(link.id, row.actor_id):
        raise NotFoundError("share.session_not_found", "conversation not found")
    return row


def _visitor_events(events):
    """What an outsider may see of the stream: text, completion, and generic
    errors. Tool names, runtime mode and raw exception text stay server-side."""
    for event in events:
        kind, data = event["event"], event["data"]
        if kind == "meta":
            yield {"event": "meta", "data": {"session_id": data["session_id"]}}
        elif kind in ("delta", "saved", "done", "heartbeat"):
            yield event
        elif kind == "error":
            yield {
                "event": "error",
                "data": {"code": data.get("code") or "share.unavailable",
                         "message": _GENERIC_ERROR},
            }


@router.get("/{token}", summary="Public info for a share link")
def share_info(token: str, embed: bool = False, db: Session = Depends(get_db)) -> dict[str, Any]:
    link, agent = share_links.resolve(db, token)
    return {
        "kind": link.kind,
        # Echoed so the page can pick its chromeless layout from one place; embedding
        # changes only rendering - same link, same limits, same visitor visibility.
        "embed": embed,
        "label": link.label,
        "agent": {"display_name": display_name_of(agent.spec) or agent.name},
        "expires_at": _iso(link.expires_at),
    }


@router.post("/{token}/chat", summary="Chat with the shared agent (SSE)")
def share_chat(
    token: str, req: ShareChatRequest, db: Session = Depends(get_db)
) -> StreamingResponse:
    link, agent = share_links.resolve(db, token)
    share_links.enforce_rate_limit(link.id)
    if req.session_id:
        actor_id = _owned_session(db, link, agent, req.session_id).actor_id
        session_id = req.session_id
    else:
        # Minted here (not by chat_stream) so the anonymous actor can be derived
        # from it before the first turn is dispatched.
        session_id = new_session_id()
        actor_id = share_links.share_actor(link.id, session_id)
    workspace_row = get_workspace_row(db, link.workspace_id or "")
    if workspace_row is None:
        raise share_links.not_found()
    workspace = workspace_context(workspace_row)
    workspace_id, link_id, agent_id = link.workspace_id or "", link.id, agent.id
    version = agent.version
    share_links.record_use(link_id)

    def generate():
        events = chat_stream(
            agent, req.prompt, session_id=session_id,
            actor_id=memory_service.scoped_actor(agent.id, actor_id),
            workspace=workspace,
        )
        persisted = persist_events(
            events, workspace_id=workspace_id, agent_id=agent_id, prompt=req.prompt,
            actor_id=actor_id, session_id=session_id, runtime_version=version,
        )
        for event in _visitor_events(persisted):
            yield sse_encode(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/{token}/feedback", summary="Thumbs up/down on an answer")
def share_feedback(
    token: str, req: ShareFeedbackRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    link, agent = share_links.resolve(db, token)
    share_links.enforce_rate_limit(link.id)
    session = _owned_session(db, link, agent, req.session_id)
    return feedback_service.record_feedback(
        db,
        workspace_id=link.workspace_id or "",
        agent_id=agent.id,
        session_id=req.session_id,
        message_id=req.message_id,
        verdict=req.verdict,
        comment=req.comment,
        actor=session.actor_id,
        source="share",
    )
