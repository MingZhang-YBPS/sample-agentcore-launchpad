"""Slack Events API adapter.

Verification: Slack signs every request (`X-Slack-Signature` = `v0=` + HMAC-SHA256 of
`v0:<timestamp>:<raw body>` under the app's signing secret), including the
`url_verification` handshake. That is a real message authenticator, unlike the legacy
verification token, so it is the only scheme supported. A 5-minute timestamp window
bounds replay; event-id de-duplication (in `channels/__init__`) closes the rest.
"""

import hashlib
import hmac
import re
import time
from collections.abc import Mapping
from typing import Any

from app.core.errors import AppError
from app.models.ledger import ShareLink
from app.services.channels import transport
from app.services.channels.base import Inbound, Parsed

platform = "slack"
POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
TOLERANCE_S = 300
_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")


def _invalid(message: str) -> AppError:
    return AppError("channel.invalid_setup", message, status_code=422)


def setup(credentials: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    signing_secret = (credentials.get("signing_secret") or "").strip()
    bot_token = (credentials.get("bot_token") or "").strip()
    if len(signing_secret) < 8:
        raise _invalid("Slack needs the app's Signing Secret")
    if not bot_token.startswith("xoxb-"):
        raise _invalid("Slack needs the bot's OAuth token (it starts with xoxb-)")
    return {}, {"signing_secret": signing_secret, "bot_token": bot_token}


def sign(signing_secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()


def verify(link: ShareLink, headers: Mapping[str, str], body: bytes) -> None:
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    secret = (link.channel_secrets or {}).get("signing_secret") or ""
    forged = AppError("channel.signature_invalid", "request signature invalid", status_code=401)
    if not secret or not timestamp.isdigit() or not signature:
        raise forged
    if abs(time.time() - int(timestamp)) > TOLERANCE_S:
        raise forged
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        raise forged


def parse(link: ShareLink, payload: dict[str, Any]) -> Parsed:
    kind = payload.get("type")
    if kind == "url_verification":
        return Parsed("handshake", response={"challenge": str(payload.get("challenge", ""))})
    if kind != "event_callback":
        return Parsed("ignore")
    event = payload.get("event") or {}
    etype = event.get("type")
    # Bots (including this one) and edits/joins/etc. never trigger a turn.
    if event.get("bot_id") or event.get("subtype"):
        return Parsed("ignore")
    # A channel message that mentions the bot arrives twice (message + app_mention);
    # act on the mention, and on plain `message` events only in direct messages.
    if etype == "message" and event.get("channel_type") != "im":
        return Parsed("ignore")
    if etype not in ("message", "app_mention"):
        return Parsed("ignore")
    text = _MENTION.sub("", str(event.get("text") or "")).strip()
    channel = str(event.get("channel") or "")
    ts = str(event.get("ts") or "")
    if not text or not channel or not ts:
        return Parsed("ignore")
    thread_ts = str(event.get("thread_ts") or "")
    if thread_ts:
        key = f"{channel}:{thread_ts}"
    elif event.get("channel_type") == "im":
        key = channel  # a DM is one running conversation
    else:
        key = f"{channel}:{ts}"  # a fresh mention starts (and threads) a conversation
    return Parsed(
        "message",
        message=Inbound(
            event_id=str(payload.get("event_id") or f"{channel}:{ts}"),
            text=text,
            conversation_key=key,
            reply={"channel": channel, "thread_ts": thread_ts or ts},
        ),
    )


def reply(link: ShareLink, message: Inbound, text: str) -> None:
    token = (link.channel_secrets or {}).get("bot_token") or ""
    body = transport.post_json(
        POST_MESSAGE_URL,
        {"channel": message.reply["channel"], "thread_ts": message.reply["thread_ts"],
         "text": text},
        {"Authorization": f"Bearer {token}"},
    )
    if not body.get("ok"):
        raise RuntimeError(f"slack chat.postMessage refused: {body.get('error', 'unknown')}")
