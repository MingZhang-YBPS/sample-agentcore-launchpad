"""chat-streaming S1: the heartbeat runs through the whole turn, and §5.1: a turn
cut short (client gone, stream broken) keeps the partial answer it showed."""

import threading
import time

import pytest

import app.services.chat as chat_service
from app.core.db import SessionLocal
from app.models.ledger import Agent, ChatMessage
from app.services.chat import chat_stream, sse_encode, with_keepalive
from app.services.chat_ledger import persist_events
from tests.conftest import ws_ctx
from tests.test_chat_api import make_active_agent


def _kinds(events):
    return [e["event"] for e in events]


def test_heartbeats_keep_coming_after_the_first_event():
    # the 70 s CloudFront case, scaled: one event, then a long silence
    def stalls_after_first():
        yield {"event": "delta", "data": {"text": "a"}}
        time.sleep(0.36)
        yield {"event": "delta", "data": {"text": "b"}}

    out = list(with_keepalive(stalls_after_first(), interval=0.05))
    kinds = _kinds(out)
    assert kinds[0] == "delta" and kinds[-1] == "delta"
    beats = [e for e in out[1:-1]]
    assert len(beats) >= 4 and all(e["event"] == "heartbeat" for e in beats)
    elapsed = [e["data"]["elapsed_ms"] for e in beats]
    assert elapsed == sorted(elapsed) and elapsed[0] >= 40


def test_a_runtime_heartbeat_is_passed_on_and_not_doubled():
    def chatty():
        for _ in range(5):
            time.sleep(0.02)
            yield {"event": "heartbeat", "data": {}}
        yield {"event": "delta", "data": {"text": "x"}}

    out = list(with_keepalive(chatty(), interval=0.2, started=time.monotonic() - 1))
    # the runtime never fell silent for an interval: no platform heartbeat added
    assert _kinds(out) == ["heartbeat"] * 5 + ["delta"]
    assert all(e["data"]["elapsed_ms"] >= 1000 for e in out[:5])


def test_closing_mid_silence_closes_the_upstream_once_its_step_returns():
    closed = threading.Event()
    release = threading.Event()

    def upstream():
        try:
            yield {"event": "delta", "data": {"text": "a"}}
            release.wait(2)
            yield {"event": "delta", "data": {"text": "b"}}
            yield {"event": "delta", "data": {"text": "never"}}
        finally:
            closed.set()

    stream = with_keepalive(upstream(), interval=0.02)
    assert next(stream)["event"] == "delta"
    assert next(stream)["event"] == "heartbeat"  # step 2 is in flight
    stream.close()  # the client went away
    assert not closed.is_set()
    release.set()
    assert closed.wait(2)


def test_closing_between_events_closes_the_upstream_at_once():
    closed = threading.Event()

    def upstream():
        try:
            yield {"event": "delta", "data": {"text": "a"}}
            yield {"event": "delta", "data": {"text": "b"}}
        finally:
            closed.set()

    stream = with_keepalive(upstream(), interval=1)
    next(stream)
    stream.close()
    assert closed.is_set()


def test_every_step_runs_in_the_callers_context():
    import contextvars

    var: contextvars.ContextVar[str] = contextvars.ContextVar("cs1", default="unset")
    var.set("request")

    def upstream():
        yield {"event": "delta", "data": {"text": var.get()}}
        time.sleep(0.06)
        yield {"event": "delta", "data": {"text": var.get()}}

    texts = [e["data"]["text"] for e in with_keepalive(upstream(), interval=0.02)
             if e["event"] == "delta"]
    assert texts == ["request", "request"]


def test_a_guardrail_turn_still_heartbeats_while_text_is_withheld(monkeypatch):
    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(method="container", name="cs1-guarded"))
    db.close()
    monkeypatch.setattr(chat_service.guardrail, "guardrail_config",
                        lambda _spec: {"mode": "mask"})
    monkeypatch.setattr(
        chat_service.guardrail, "screen",
        lambda text, **_kw: type("S", (), {"text": text.replace("secret", "***")})(),
    )
    monkeypatch.setattr(chat_service, "KEEPALIVE_SECONDS", 0.05)

    def trickle(*_a, **_kw):
        # text keeps coming upstream, but none of it goes down until the screen
        for part in ("my ", "secret ", "answer"):
            time.sleep(0.12)
            yield {"event": "delta", "data": {"text": part}}

    monkeypatch.setattr(chat_service, "invoke_agent_events", trickle)
    out = list(chat_stream(agent, "hello", workspace=ws_ctx()))
    kinds = _kinds(out)
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert kinds.count("heartbeat") >= 4
    assert [e["data"]["text"] for e in out if e["event"] == "delta"] == ["my *** answer"]


def test_sse_encode_names_the_heartbeat_only_when_asked():
    beat = {"event": "heartbeat", "data": {"elapsed_ms": 1500}}
    assert sse_encode(beat) == ": keep-alive\n\n"  # /v1, share, assistant
    assert sse_encode(beat, named_heartbeat=True) == (
        'event: heartbeat\ndata: {"elapsed_ms": 1500}\n\n'
    )


# --- §5.1 partial answer on disconnect / stream error -------------------------


def _persist(events, session_id=None):
    return persist_events(
        events, workspace_id="default", agent_id="a-cs1", prompt="question",
        actor_id="river", session_id=session_id,
    )


def _thread(session_id):
    db = SessionLocal()
    try:
        rows = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.asc()).all())
        return [(r.role, r.text) for r in rows]
    finally:
        db.close()


def test_a_disconnect_keeps_the_partial_answer_and_says_so():
    closed = threading.Event()

    def turn():
        try:
            yield {"event": "meta", "data": {"session_id": "s-cs1-gone"}}
            yield {"event": "delta", "data": {"text": "half an "}}
            yield {"event": "delta", "data": {"text": "answer"}}
            yield {"event": "heartbeat", "data": {"elapsed_ms": 15000}}
            yield {"event": "delta", "data": {"text": "never seen"}}
        finally:
            closed.set()

    stream = _persist(turn())
    for event in stream:
        if event["event"] == "heartbeat":
            break
    stream.close()
    assert closed.is_set()  # the chat stream (and so the upstream) is closed too
    assert _thread("s-cs1-gone") == [
        ("user", "question"),
        ("agent", "half an answer"),
        ("error", "interrupted: client disconnected"),
    ]


def test_a_broken_stream_keeps_the_partial_answer_and_reraises():
    def turn():
        yield {"event": "meta", "data": {"session_id": "s-cs1-broken"}}
        yield {"event": "delta", "data": {"text": "partial"}}
        raise RuntimeError("socket reset")

    with pytest.raises(RuntimeError, match="socket reset"):
        list(_persist(turn()))
    assert _thread("s-cs1-broken") == [
        ("user", "question"),
        ("agent", "partial"),
        ("error", "interrupted: RuntimeError: socket reset"),
    ]


def test_a_disconnect_after_done_records_no_interruption():
    def turn():
        yield {"event": "meta", "data": {"session_id": "s-cs1-done"}}
        yield {"event": "delta", "data": {"text": "whole answer"}}
        yield {"event": "done", "data": {"latency_ms": 5}}

    stream = _persist(turn())
    kinds = []
    for event in stream:
        kinds.append(event["event"])
        if event["event"] == "done":
            break
    stream.close()
    assert kinds[-2:] == ["saved", "done"]
    assert _thread("s-cs1-done") == [("user", "question"), ("agent", "whole answer")]


def test_a_disconnect_before_meta_writes_nothing():
    def turn():
        yield {"event": "heartbeat", "data": {"elapsed_ms": 15000}}
        yield {"event": "meta", "data": {"session_id": "s-cs1-early"}}

    stream = _persist(turn())
    next(stream)
    stream.close()
    assert _thread("s-cs1-early") == []
