"""IM channel publishing (T30): Slack and Feishu on top of the share-link primitive.

A channel is a `ShareLink` whose `kind` is the platform. The platform calls one public
webhook (`POST /share/channels/{platform}/{token}`); the link row names the workspace
and the agent, the adapter authenticates the request and extracts a message, and the
turn goes through `invoke_agent_text` - the one invoke chain - before the adapter posts
the answer back. Microsoft Teams is deliberately not an adapter: see
docs/architecture.md (its inbound path is the Bot Framework, which needs an Azure bot
registration, JWT validation against Microsoft's rotating JWKS and a proactive-reply
flow, none of which fits a stateless, secret-per-link webhook).
"""

import hashlib
import logging
import threading
from collections import OrderedDict
from types import ModuleType

from app.core.db import SessionLocal
from app.core.errors import NotFoundError
from app.models.ledger import ShareLink
from app.services import memory as memory_service
from app.services import share_links
from app.services.channels.base import Inbound
from app.services.chat_ledger import save_message, track_session
from app.services.invoke import invoke_agent_text
from app.services.workspace import get_workspace_row, workspace_context

logger = logging.getLogger(__name__)

GENERIC_ERROR = "The assistant could not answer right now. Please try again."
# IM messages are read in a chat pane, not a document; cap what one reply carries.
MAX_REPLY_CHARS = 8000


def adapters() -> dict[str, ModuleType]:
    # Imported here: the adapters import `transport` from this package.
    from app.services.channels import feishu, slack

    return {slack.platform: slack, feishu.platform: feishu}


def adapter_for(platform: str) -> ModuleType:
    found = adapters().get(platform)
    if found is None:
        raise NotFoundError("share.not_found", "share link not found")
    return found


def describe(link: ShareLink) -> dict | None:
    """What the console may know about a channel link: never a secret value."""
    if link.kind not in share_links.CHANNEL_KINDS:
        return None
    return {
        "platform": link.kind,
        "config": dict(link.channel_config or {}),
        "secrets_set": sorted(
            key.removesuffix("_sha256") for key, value in (link.channel_secrets or {}).items()
            if value
        ),
    }


# ── de-duplication ──────────────────────────────────────────────────────────────
# Platforms redeliver an event they think was not acknowledged in time (Slack after
# 3 s, Feishu likewise); a redelivery must not become a second agent turn.

_SEEN_MAX = 2048
_seen: OrderedDict[tuple[str, str], None] = OrderedDict()
_seen_lock = threading.Lock()


def already_seen(link_id: str, event_id: str) -> bool:
    with _seen_lock:
        return (link_id, event_id) in _seen


def claim(link_id: str, event_id: str) -> None:
    with _seen_lock:
        _seen[(link_id, event_id)] = None
        while len(_seen) > _SEEN_MAX:
            _seen.popitem(last=False)


def reset_seen() -> None:
    with _seen_lock:
        _seen.clear()


# ── the turn ────────────────────────────────────────────────────────────────────


def session_id_for(link_id: str, conversation_key: str) -> str:
    """Deterministic runtime session id: the same platform conversation always
    continues the same agent session. 3 + 64 chars satisfies the 33-char floor."""
    digest = hashlib.sha256(f"{link_id}|{conversation_key}".encode()).hexdigest()
    return f"ch-{digest}"


def handle_message(link_id: str, message: Inbound) -> None:
    """Run one inbound message through the agent and post the answer. Never raises:
    it runs after the webhook has been acknowledged, so there is nobody to tell."""
    db = SessionLocal()
    try:
        link = db.get(ShareLink, link_id)
        if link is None:
            return
        adapter = adapter_for(link.kind)
        agent = share_links.live_agent(db, link)
        workspace_row = get_workspace_row(db, link.workspace_id or "")
        if workspace_row is None:
            return
        workspace = workspace_context(workspace_row)
        workspace_id, agent_id = link.workspace_id or "", agent.id
        session_id = session_id_for(link.id, message.conversation_key)
        actor_id = share_links.share_actor(link.id, session_id)
        track_session(workspace_id, agent_id, session_id, actor_id,
                      runtime_version=agent.version)
        save_message(workspace_id, agent_id, session_id, "user", message.text)
        try:
            result = invoke_agent_text(
                agent, message.text, session_id=session_id,
                actor_id=memory_service.scoped_actor(agent.id, actor_id),
                workspace=workspace,
            )
            answer = str(result.get("text") or "").strip() or GENERIC_ERROR
            save_message(workspace_id, agent_id, session_id, "agent", answer)
        except Exception as exc:  # the platform user only ever sees the generic text
            logger.warning("channel turn failed for link %s: %s", link_id, exc)
            answer = GENERIC_ERROR
            save_message(workspace_id, agent_id, session_id, "error", f"{type(exc).__name__}")
        if len(answer) > MAX_REPLY_CHARS:
            answer = answer[: MAX_REPLY_CHARS - 1] + "…"
        try:
            adapter.reply(link, message, answer)
        except Exception as exc:
            logger.warning("channel reply failed for link %s: %s", link_id, exc)
    except Exception as exc:
        logger.warning("channel message dropped for link %s: %s", link_id, exc)
    finally:
        db.close()
