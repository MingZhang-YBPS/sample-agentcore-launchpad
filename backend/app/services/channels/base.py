"""The adapter contract for IM channels (T30)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.models.ledger import ShareLink


@dataclass(frozen=True)
class Inbound:
    """One user message, reduced to what the shared pipeline needs."""

    event_id: str
    text: str
    # Stable id of the conversation on the platform (DM, or channel thread): the
    # same key always lands in the same agent session.
    conversation_key: str
    # Opaque, adapter-private routing data for the reply (channel, thread, ...).
    reply: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Parsed:
    kind: str  # "handshake" | "message" | "ignore"
    response: dict[str, Any] | None = None  # handshake body
    message: Inbound | None = None


class Adapter(Protocol):
    platform: str

    def setup(self, credentials: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate console input -> (channel_config, channel_secrets). Raises AppError."""

    def verify(self, link: ShareLink, headers: Mapping[str, str], body: bytes) -> None:
        """Authenticate the platform's request. Raises AppError 401 when forged."""

    def parse(self, link: ShareLink, payload: dict[str, Any]) -> Parsed: ...

    def reply(self, link: ShareLink, message: Inbound, text: str) -> None: ...
