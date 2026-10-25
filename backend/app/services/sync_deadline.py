"""A deadline for the two synchronous invoke routes (console ``/api/agents/{id}/invoke``
and public ``/v1/agents/{id}/invoke``).

A synchronous HTTP request sends nothing until the agent has answered. Behind a
proxy with an origin read timeout -- CloudFront's default is 60 s -- a slow answer
is cut off with the proxy's own 504 page: not JSON, no session id, and the client
cannot tell a slow agent from a dead one. The streaming routes do not have this
problem: they send heartbeats the whole time.

So a sync invoke waits at most ``settings.sync_invoke_deadline_s`` (default 50 s,
under CloudFront's 60 s; 0 = no deadline, for deployments without such a proxy).
Past it the caller gets a readable ``504 invoke.timeout`` that names the session id
and the streaming route to use instead. The turn itself is NOT cancelled: it keeps
running on its own thread and finishes as it would have, so the conversation's
memory holds it and a retry on the same session id continues from it.

Each sync invoke runs on its own daemon thread rather than a shared pool, so turns
that outlive their deadline can never queue up in front of new requests.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.core.errors import AppError
from app.services.agentcore import harness as hc

logger = logging.getLogger(__name__)

TIMEOUT_CODE = "invoke.timeout"


def ensure_session_id(session_id: str | None) -> str:
    """The id the turn runs under, fixed BEFORE the call so a timeout can name it.
    A caller's id is kept; a missing or too-short one gets a fresh runtime-grade id
    (``invoke_agent_text`` would replace a short one the same way)."""
    return session_id if session_id and len(session_id) >= 33 else hc.new_session_id()


def deadline_seconds() -> float:
    return float(get_settings().sync_invoke_deadline_s)


def call_within[T](
    fn: Callable[[], T], *, session_id: str, stream_route: str,
    deadline_s: float | None = None,
) -> T:
    """``fn()``, or ``AppError(invoke.timeout, 504)`` once ``deadline_s`` passes.

    An exception ``fn`` raises inside the deadline is re-raised unchanged, so the
    routes' upstream-error mapping still applies. After a timeout ``fn`` keeps
    running; its eventual failure is logged, its result discarded.
    """
    limit = deadline_seconds() if deadline_s is None else deadline_s
    if limit <= 0:
        return fn()
    done = threading.Event()
    box: dict[str, Any] = {}
    late = threading.Event()  # set once the caller has given up

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc
            if late.is_set():
                logger.warning("sync invoke for session %s failed after its deadline: %s",
                               session_id, exc)
        finally:
            done.set()

    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(run,), name="sync-invoke", daemon=True).start()
    if not done.wait(limit):
        late.set()
        logger.info("sync invoke for session %s passed its %ss deadline; still running",
                    session_id, limit)
        raise AppError(
            TIMEOUT_CODE,
            f"The agent is still answering after {limit:g} s, longer than a synchronous "
            f"request can wait. The turn keeps running in session {session_id}; for long "
            f"answers call the streaming route {stream_route} instead.",
            detail={"session_id": session_id, "deadline_s": limit,
                    "stream_route": stream_route},
            status_code=504,
        )
    if "error" in box:
        raise box["error"]
    return box["value"]
