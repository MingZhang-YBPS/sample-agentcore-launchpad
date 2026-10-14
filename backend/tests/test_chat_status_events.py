"""chat-streaming S2 §3.1: the console's working-status events — phase, thinking
(status only, never the reasoning text), tool start times and tool_end."""

import time

import app.services.chat as chat_service
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.services.chat import _status_events, chat_stream
from tests.conftest import ws_ctx
from tests.test_chat_api import make_active_agent
from tests.test_harness_execution import (
    SID,
    Stream,
    client_for,
    create_agent,
    stop,
    text,
)


def _kinds(events):
    return [e["event"] for e in events if e["event"] != "heartbeat"]


def _reasoning(value):
    return {"contentBlockDelta": {"delta": {"reasoningContent": {"text": value}}}}


def _tool_call(index, tool_id, name, status, result_text="ok"):
    return [
        {"contentBlockStart": {"contentBlockIndex": index, "start": {
            "toolUse": {"toolUseId": tool_id, "name": name, "type": "tool_use"},
        }}},
        {"contentBlockStop": {"contentBlockIndex": index}},
        stop("tool_use"),
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {
            "toolResult": {"toolUseId": tool_id, "status": status},
        }}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
            "toolResult": [{"text": result_text}],
        }}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        stop("tool_result"),
    ]


def _code_agent(name):
    with SessionLocal() as db:
        return db.get(Agent, make_active_agent(method="container", name=name))


def test_status_events_are_off_unless_asked(monkeypatch):
    # /v1 and the share page call chat_stream without them: their stream is unchanged
    stream = Stream([_reasoning("hmm"), *_tool_call(1, "t1", "kb", "success"),
                     text("hi"), stop("end_turn")])
    monkeypatch.setattr(chat_service, "data_client", lambda _ws=None: client_for(stream))
    events = list(chat_stream(create_agent(), "q", session_id=SID, workspace=ws_ctx()))
    assert _kinds(events) == ["meta", "tool", "delta", "done"]
    assert "started_at" not in events[0]["data"]
    assert events[1]["data"] == {"name": "kb", "id": "t1"}


def test_harness_turn_maps_phases_thinking_and_tool_ends(monkeypatch):
    deny = ("Tool Execution Denied: Tool call not allowed due to policy enforcement "
            "[No policy applies to the request (denied by default).]")
    stream = Stream([
        _reasoning("let me think"),
        *_tool_call(1, "t-ok", "kb_search", "success"),
        *_tool_call(1, "t-err", "broken", "error", "boom"),
        *_tool_call(1, "t-deny", "pay", "error", deny),
        text("done "), text("now"), stop("end_turn"),
    ])
    monkeypatch.setattr(chat_service, "data_client", lambda _ws=None: client_for(stream))
    before = int(time.time() * 1000)
    events = list(chat_stream(create_agent(), "q", session_id=SID, workspace=ws_ctx(),
                              status_events=True))
    assert _kinds(events) == [
        "meta", "phase", "phase", "thinking",
        "tool", "tool_end", "tool", "tool_end", "tool", "policy_denied", "tool_end",
        "phase", "delta", "delta", "done",
    ]
    assert events[0]["data"]["started_at"] >= before
    phases = [e["data"]["phase"] for e in events if e["event"] == "phase"]
    assert phases == ["starting", "waiting_model", "streaming"]
    # the reasoning text never leaves the server (D-2)
    assert next(e for e in events if e["event"] == "thinking")["data"] == {}
    assert all("let me think" not in str(e["data"]) for e in events)
    tools = [e["data"] for e in events if e["event"] == "tool"]
    assert [t["id"] for t in tools] == ["t-ok", "t-err", "t-deny"]
    assert all(t["started_at"] >= before for t in tools)
    ends = [e["data"] for e in events if e["event"] == "tool_end"]
    assert [(e["id"], e["status"]) for e in ends] == [
        ("t-ok", "success"), ("t-err", "error"), ("t-deny", "denied"),
    ]
    assert all(isinstance(e["duration_ms"], int) and e["duration_ms"] >= 0 for e in ends)
    assert stream.closed == 1


def test_thinking_is_throttled(monkeypatch):
    clock = iter([0.0, 0.05, 0.1, 0.25, 0.3, 0.6])

    class Clock:
        @staticmethod
        def monotonic():
            return next(clock, 10.0)

        time = staticmethod(time.time)

    stream = Stream([_reasoning(str(i)) for i in range(6)] + [text("x"), stop("end_turn")])
    monkeypatch.setattr(chat_service, "data_client", lambda _ws=None: client_for(stream))
    monkeypatch.setattr(chat_service, "time", Clock)
    out = list(chat_service._harness_events(
        create_agent(), "q", SID, "a", ws_ctx(), thinking=True,
    ))
    # reasoning at 0, .05, .1, .25, .3, .6 s → status at 0, .25 and .6 only
    assert [e["event"] for e in out] == ["thinking", "thinking", "thinking", "delta"]


def test_code_runtime_tool_ends_on_the_next_non_tool_event():
    def upstream():
        yield {"event": "heartbeat", "data": {}}
        yield {"event": "tool", "data": {"name": "search", "id": None}}
        yield {"event": "heartbeat", "data": {}}  # the tool is still running
        yield {"event": "tool", "data": {"name": "fetch", "id": "f1"}}
        time.sleep(0.02)
        yield {"event": "delta", "data": {"text": "answer"}}
        yield {"event": "tool", "data": {"name": "late", "id": "l1"}}

    out = list(_status_events(upstream()))
    kinds = [e["event"] for e in out]
    assert kinds == [
        "phase", "phase", "heartbeat", "tool", "heartbeat", "tool",
        "tool_end", "tool_end", "phase", "delta", "tool", "tool_end",
    ]
    assert out[3]["data"]["id"] == "tool-1"  # an unnamed call gets a matchable id
    ends = [e["data"] for e in out if e["event"] == "tool_end"]
    assert [e["id"] for e in ends] == ["tool-1", "f1", "l1"]
    assert all(e["status"] == "success" for e in ends)
    assert ends[0]["duration_ms"] >= 20


def test_harness_tool_is_not_ended_by_text_before_its_result():
    def upstream():
        yield {"event": "tool", "data": {"name": "kb", "id": "t1"}}
        yield {"event": "delta", "data": {"text": "while it runs"}}
        yield {"event": "tool_end", "data": {"id": "t1", "status": "error"}}

    out = list(_status_events(upstream(), reports_tool_end=True))
    ends = [e for e in out if e["event"] == "tool_end"]
    assert len(ends) == 1 and ends[0]["data"]["status"] == "error"
    assert [e["event"] for e in out].index("delta") < [e["event"] for e in out].index("tool_end")


def test_closing_the_status_wrapper_closes_the_upstream():
    closed = []

    def upstream():
        try:
            yield {"event": "delta", "data": {"text": "a"}}
            yield {"event": "delta", "data": {"text": "b"}}
        finally:
            closed.append(True)

    stream = _status_events(upstream())
    assert next(stream)["data"] == {"phase": "starting"}
    next(stream)
    stream.close()
    assert closed == [True]


def test_code_runtime_turn_with_status_events(monkeypatch):
    agent = _code_agent("cs2-code")

    def events(*_a, **_kw):
        yield {"event": "tool", "data": {"name": "calc", "id": "c1"}}
        yield {"event": "delta", "data": {"text": "4"}}

    monkeypatch.setattr(chat_service, "invoke_agent_events", events)
    out = list(chat_stream(agent, "2+2", workspace=ws_ctx(), status_events=True))
    assert _kinds(out) == [
        "meta", "phase", "phase", "tool", "tool_end", "phase", "delta", "done",
    ]


def test_a_rule_answer_has_no_phase_events(monkeypatch):
    agent = _code_agent("cs2-rule")

    class Hit:
        answer = "curated"

        @staticmethod
        def event():
            return {"event": "rule", "data": {"rule_id": "r1"}}

    monkeypatch.setattr(chat_service.answer_rules, "match_for_agent", lambda *_a: Hit())
    out = list(chat_stream(agent, "q", workspace=ws_ctx(), status_events=True))
    assert _kinds(out) == ["meta", "rule", "delta", "done"]
