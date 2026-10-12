"""Channel publishing (T30): connect an agent to Slack or Feishu.

Two surfaces, one primitive (`ShareLink`, see `services/channels`):

* console (`/api`, MEMBER, workspace-scoped): create a channel link. Listing and
  revoking reuse the share-link routes - a channel link is a share link of another kind.
* public webhook (`POST /share/channels/{platform}/{token}`): the platform calls it.
  It sits under the hub-global `/share` prefix: no console session, no `X-Workspace`;
  the link row names the workspace. Order matters and is the security posture:
  resolve the token (one opaque 404) -> authenticate the platform's request -> only
  then parse, rate-limit and dispatch, so a forged request cannot spend a link's budget.
"""

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.share import _agent_in, _link_out
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access, channels, share_links

console_router = APIRouter(
    prefix="/api",
    tags=["channels"],
    # workspace isolation: a hidden `{agent_id}` / `{run_id}` reads as missing
    dependencies=[Depends(agent_access.require_path_ids_visible)],
)
router = APIRouter(prefix="/share/channels", tags=["channels"])

WEBHOOK_PATH = "/share/channels"


class CreateChannelLink(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    label: str = Field(default="", max_length=64)
    # Channel links default to a finite life too; None means never.
    expires_in_days: int | None = Field(default=None, ge=1, le=share_links.MAX_EXPIRY_DAYS)
    # Platform credentials; validated by the adapter and never returned.
    credentials: dict[str, str] = Field(default_factory=dict)


@console_router.post("/agents/{agent_id}/channel-links", status_code=201)
def create_channel_link(
    agent_id: str,
    req: CreateChannelLink,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = share_links.shareable_agent(_agent_in(db, ws, agent_id))
    if req.platform not in share_links.CHANNEL_KINDS:
        raise AppError(
            "channel.unsupported",
            f"unsupported channel '{req.platform}' (supported: "
            f"{', '.join(share_links.CHANNEL_KINDS)})",
            status_code=422,
        )
    if any(len(value) > 512 for value in req.credentials.values()):
        raise AppError("channel.invalid_setup", "a credential is too long", status_code=422)
    config, secrets_ = channels.adapter_for(req.platform).setup(req.credentials)
    identity = require_identity(request)
    link, raw = share_links.create_channel_link(
        db,
        workspace_id=ws.id,
        agent=agent,
        platform=req.platform,
        label=req.label,
        created_by=identity.username if auth_enabled() else "river",
        expires_in_days=req.expires_in_days,
        config=config,
        secrets_=secrets_,
    )
    path = f"{WEBHOOK_PATH}/{req.platform}/{raw}"
    origin = (request.headers.get("origin") or "").rstrip("/")
    # The webhook URL carries the link token, so like a share token it is shown once.
    return {**_link_out(link), "token": raw, "path": path, "url": f"{origin}{path}"}


# ── public webhook ──────────────────────────────────────────────────────────────


async def _raw_body(request: Request) -> bytes:
    # Signatures cover the exact bytes, so the body is read raw before any parsing.
    return await request.body()


@router.post("/{platform}/{token}", summary="Inbound webhook for an IM channel")
def inbound(
    platform: str,
    token: str,
    request: Request,
    background: BackgroundTasks,
    body: bytes = Depends(_raw_body),
    db: Session = Depends(get_db),
) -> JSONResponse:
    adapter = channels.adapter_for(platform)
    link, _agent = share_links.resolve(db, token, kinds=(platform,))
    adapter.verify(link, request.headers, body)
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise AppError("channel.bad_payload", "expected a JSON object", status_code=400)
    parsed = adapter.parse(link, payload)
    if parsed.kind == "handshake":
        return JSONResponse(parsed.response or {})
    if parsed.kind != "message" or parsed.message is None:
        return JSONResponse({"ok": True})
    message = parsed.message
    if channels.already_seen(link.id, message.event_id):
        return JSONResponse({"ok": True, "duplicate": True})
    share_links.enforce_rate_limit(link.id)
    channels.claim(link.id, message.event_id)
    share_links.record_use(link.id)
    # The platform wants an acknowledgement within ~3 s; a model turn takes longer, so
    # the answer is posted back out of band.
    background.add_task(channels.handle_message, link.id, message)
    return JSONResponse({"ok": True})
