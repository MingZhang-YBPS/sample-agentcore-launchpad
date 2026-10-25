"""The synchronous-invoke deadline (services/sync_deadline.py).

A sync invoke slower than a proxy's origin read timeout used to be cut off with the
proxy's own 504 page. It now answers a readable ``504 invoke.timeout`` naming the
session and the streaming route, while the turn keeps running.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.core.errors import AppError
from app.services import sync_deadline as sd


def test_fast_call_returns_its_value():
    assert sd.call_within(lambda: 42, session_id="s" * 40, stream_route="/x",
                          deadline_s=2) == 42


def test_error_inside_the_deadline_is_reraised_unchanged():
    class Boom(RuntimeError):
        pass

    def fail():
        raise Boom("upstream")

    with pytest.raises(Boom):
        sd.call_within(fail, session_id="s" * 40, stream_route="/x", deadline_s=2)


def test_slow_call_answers_invoke_timeout_and_keeps_running():
    release = threading.Event()
    finished = threading.Event()

    def slow():
        release.wait(5)
        finished.set()
        return "late answer"

    started = time.monotonic()
    with pytest.raises(AppError) as info:
        sd.call_within(slow, session_id="sess-" + "a" * 40,
                       stream_route="/v1/agents/a1/invoke-stream", deadline_s=0.2)
    assert time.monotonic() - started < 2
    err = info.value
    assert err.code == "invoke.timeout" and err.status_code == 504
    assert err.detail == {"session_id": "sess-" + "a" * 40, "deadline_s": 0.2,
                          "stream_route": "/v1/agents/a1/invoke-stream"}
    # the turn was not cancelled: it finishes on its own thread
    release.set()
    assert finished.wait(5)


def test_zero_deadline_runs_inline():
    caller = threading.current_thread()
    seen = []
    sd.call_within(lambda: seen.append(threading.current_thread()), session_id="s" * 40,
                   stream_route="/x", deadline_s=0)
    assert seen == [caller]


def test_default_deadline_comes_from_settings(monkeypatch):
    class S:
        sync_invoke_deadline_s = 7

    monkeypatch.setattr(sd, "get_settings", lambda: S())
    assert sd.deadline_seconds() == 7.0


def test_context_vars_reach_the_worker_thread():
    import contextvars

    var: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="unset")
    var.set("request-scoped")
    assert sd.call_within(var.get, session_id="s" * 40, stream_route="/x",
                          deadline_s=2) == "request-scoped"


def test_ensure_session_id_keeps_a_long_id_and_replaces_a_short_one():
    long_id = "x" * 33
    assert sd.ensure_session_id(long_id) == long_id
    fresh = sd.ensure_session_id(None)
    assert len(fresh) >= 33 and sd.ensure_session_id("short") != "short"
