"""A stalled runtime stream: the console keeps the response alive and the invocation
is retried once when nothing at all came back (live 2026-10-13, three stalls)."""

import time

import pytest
from botocore.exceptions import ReadTimeoutError

from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.services import chat as chat_service
from app.services import invoke as invoke_service
from tests.conftest import ws_ctx
from tests.test_chat_api import make_active_agent


def test_keepalive_fills_the_silence_before_the_first_event():
    def slow():
        time.sleep(0.25)
        yield {"event": "delta", "data": {"text": "a"}}
        yield {"event": "delta", "data": {"text": "b"}}

    out = list(chat_service.with_keepalive(slow(), interval=0.05))
    kinds = [e["event"] for e in out]
    assert kinds[0] == "heartbeat" and kinds[-2:] == ["delta", "delta"]
    assert [e["data"]["text"] for e in out if e["event"] == "delta"] == ["a", "b"]


def test_keepalive_reraises_the_source_error():
    def broken():
        yield {"event": "delta", "data": {"text": "a"}}
        raise RuntimeError("upstream failed")

    with pytest.raises(RuntimeError, match="upstream failed"):
        list(chat_service.with_keepalive(broken(), interval=0.05))


def _agent(method):
    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(method=method, name=f"stall-{method}"))
    db.close()
    return agent


def _patch(monkeypatch, behaviours):
    calls = []

    def stream(client, arn, prompt, **_kw):
        calls.append(client)
        behaviour = behaviours[len(calls) - 1]
        if behaviour == "stall":
            raise ReadTimeoutError(endpoint_url="https://runtime")
        if behaviour == "partial":
            yield {"event": "delta", "data": {"text": "half"}}
            raise ReadTimeoutError(endpoint_url="https://runtime")
        yield {"event": "delta", "data": {"text": "ok"}}

    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events", stream)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: "long")
    monkeypatch.setattr(invoke_service, "heartbeat_data_client", lambda _ws=None: "short")
    monkeypatch.setattr(invoke_service.canary_service, "active_canary_route", lambda _id: None)
    return calls


def test_a_silent_zip_runtime_stream_is_invoked_once_more(monkeypatch):
    calls = _patch(monkeypatch, ["stall", "ok"])
    agent = _agent("zip_runtime")
    events = list(invoke_service.invoke_agent_events(agent, "hi", workspace=ws_ctx()))
    assert [e["data"]["text"] for e in events] == ["ok"]
    assert calls == ["short", "short"]


def test_a_stream_that_already_answered_is_not_replayed(monkeypatch):
    _patch(monkeypatch, ["partial", "ok"])
    with pytest.raises(ReadTimeoutError):
        list(invoke_service.invoke_agent_events(_agent("zip_runtime"), "hi", workspace=ws_ctx()))


def test_byoc_keeps_the_long_timeout_and_no_retry(monkeypatch):
    calls = _patch(monkeypatch, ["stall", "ok"])
    with pytest.raises(ReadTimeoutError):
        list(invoke_service.invoke_agent_events(_agent("byoc"), "hi", workspace=ws_ctx()))
    assert calls == ["long"]
