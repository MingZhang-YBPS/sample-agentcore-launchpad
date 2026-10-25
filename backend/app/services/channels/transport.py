"""Outbound HTTPS for channel replies - the one place adapters reach the network.

Hosts are fixed per adapter (never taken from a request or a stored value), so a
channel link cannot be turned into an SSRF primitive. Tests replace `post_json`.
"""

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset(
    {"slack.com", "open.feishu.cn", "open.larksuite.com"}
)


def post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """POST JSON, return the decoded JSON body ({} when it is not JSON)."""
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError(f"channel replies may not go to {host!r}")
    response = httpx.post(url, json=payload, headers=headers or {}, timeout=10.0)
    try:
        body = response.json()
    except ValueError:
        logger.warning(
            "channel reply to %s returned non-JSON (HTTP %s)", host, response.status_code
        )
        return {}
    return body if isinstance(body, dict) else {}
