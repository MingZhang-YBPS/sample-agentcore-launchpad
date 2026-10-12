"""Feishu / Lark event-subscription adapter.

Verification: the event payload carries the app's *Verification Token*
(`header.token`, or top-level `token` on the handshake); it is compared, in constant
time, against the value given when the link was created. Feishu's request signature
(`X-Lark-Signature`) exists only alongside payload encryption (an Encrypt Key + AES),
so it is not an independent option: this adapter supports the plain-payload mode with
the token and rejects encrypted payloads with an actionable error rather than
guessing. Two things offset the token's weakness (it is a static bearer in the body):
the webhook URL itself embeds the 256-bit link token, and only the token's SHA-256 is
stored, so the value cannot be read back from the ledger or the API.
"""

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from app.core.errors import AppError
from app.models.ledger import ShareLink
from app.services.channels import transport
from app.services.channels.base import Inbound, Parsed

platform = "feishu"
DOMAINS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


def _invalid(message: str) -> AppError:
    return AppError("channel.invalid_setup", message, status_code=422)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def setup(credentials: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    token = (credentials.get("verification_token") or "").strip()
    app_id = (credentials.get("app_id") or "").strip()
    app_secret = (credentials.get("app_secret") or "").strip()
    domain = (credentials.get("domain") or "feishu").strip().lower()
    if len(token) < 8:
        raise _invalid("Feishu needs the app's Verification Token")
    if not app_id or not app_secret:
        raise _invalid("Feishu needs the app's App ID and App Secret to reply")
    if domain not in DOMAINS:
        raise _invalid("domain must be 'feishu' or 'lark'")
    return (
        {"domain": domain, "app_id": app_id},
        {"verification_token_sha256": _digest(token), "app_secret": app_secret},
    )


def _token_of(payload: dict[str, Any]) -> str:
    header = payload.get("header")
    if isinstance(header, dict) and header.get("token"):
        return str(header["token"])
    return str(payload.get("token") or "")


def verify(link: ShareLink, headers: Mapping[str, str], body: bytes) -> None:
    forged = AppError("channel.signature_invalid", "verification token invalid", status_code=401)
    try:
        payload = json.loads(body)
    except ValueError:
        raise forged from None
    if not isinstance(payload, dict):
        raise forged
    if "encrypt" in payload:
        raise AppError(
            "channel.encrypted_unsupported",
            "this link verifies with the Verification Token; turn off Encrypt Key in the "
            "Feishu app's event settings",
            status_code=400,
        )
    expected = (link.channel_secrets or {}).get("verification_token_sha256") or ""
    received = _token_of(payload)
    if not expected or not received or not hmac.compare_digest(_digest(received), expected):
        raise forged


def parse(link: ShareLink, payload: dict[str, Any]) -> Parsed:
    if payload.get("type") == "url_verification":
        return Parsed("handshake", response={"challenge": str(payload.get("challenge", ""))})
    header = payload.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return Parsed("ignore")
    event = payload.get("event") or {}
    if (event.get("sender") or {}).get("sender_type") != "user":
        return Parsed("ignore")
    message = event.get("message") or {}
    if message.get("message_type") != "text":
        return Parsed("ignore")
    try:
        text = str(json.loads(message.get("content") or "{}").get("text") or "")
    except ValueError:
        return Parsed("ignore")
    for mention in message.get("mentions") or []:
        key = mention.get("key")
        if key:
            text = text.replace(str(key), "")
    text = text.strip()
    message_id = str(message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    if not text or not message_id or not chat_id:
        return Parsed("ignore")
    if message.get("chat_type") == "p2p":
        key = chat_id
    else:
        key = f"{chat_id}:{message.get('root_id') or message_id}"
    return Parsed(
        "message",
        message=Inbound(
            event_id=str(header.get("event_id") or message_id),
            text=text,
            conversation_key=key,
            reply={"message_id": message_id},
        ),
    )


def reply(link: ShareLink, message: Inbound, text: str) -> None:
    config = link.channel_config or {}
    base = DOMAINS.get(str(config.get("domain")), DOMAINS["feishu"])
    auth = transport.post_json(
        f"{base}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": config.get("app_id"),
         "app_secret": (link.channel_secrets or {}).get("app_secret")},
    )
    access = auth.get("tenant_access_token")
    if not access:
        raise RuntimeError(f"feishu token exchange refused (code {auth.get('code')})")
    body = transport.post_json(
        f"{base}/open-apis/im/v1/messages/{message.reply['message_id']}/reply",
        {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        {"Authorization": f"Bearer {access}"},
    )
    if body.get("code") not in (0, None):
        raise RuntimeError(f"feishu reply refused (code {body.get('code')})")
