"""Runtime response formats the console must read (design §16): the AgentCore CLI
Strands template's native Converse stream (captured live 2026-10-14), Strands
loop events forwarded alongside it, the platform contract, `result`, plain text,
one-shot JSON — and a named error instead of a blank turn when nothing readable
arrives."""

import json
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.services.agentcore import runtime as rt

FIXTURE = Path(__file__).parent / "fixtures" / "agentcore_cli_strands_stream.sse"


class Body:
    def __init__(self, raw: bytes):
        self.raw = raw

    def iter_lines(self, chunk_size=None):
        return iter(self.raw.split(b"\n"))

    def read(self):
        return self.raw


def sse(*payloads) -> bytes:
    return "".join(f"data: {json.dumps(p, ensure_ascii=False)}\n\n" for p in payloads).encode()


def run(raw: bytes, content_type: str = "text/event-stream") -> list[dict]:
    return list(rt._runtime_body_events(Body(raw), content_type, False))


def text_of(events: list[dict]) -> str:
    return "".join(e["data"]["text"] for e in events if e["event"] == "delta")


def test_the_cli_strands_template_stream_reads_as_one_tool_call_and_the_reply():
    events = run(FIXTURE.read_bytes(), "text/event-stream; charset=utf-8")
    assert [e["data"]["name"] for e in events if e["event"] == "tool"] == ["echo"]
    assert text_of(events) == "Echo工具返回的结果是：你真是个大聪明"


def test_forwarded_strands_loop_events_never_leak_into_the_reply():
    events = run(sse(
        {"init_event_loop": True},
        {"start": True},
        {"start_event_loop": True},
        {"event": {"messageStart": {"role": "assistant"}}},
        {"event": {"contentBlockDelta": {"delta": {"text": "你好"}, "contentBlockIndex": 0}}},
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
        {"event": {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
                                "metrics": {"latencyMs": 9}}}},
        {"message": {"role": "assistant", "content": [{"text": "你好"}]}},
        {"result": {"stop_reason": "end_turn",
                    "message": {"role": "assistant", "content": [{"text": "你好"}]}}},
    ))
    assert text_of(events) == "你好"


def test_a_result_object_alone_still_yields_its_text():
    events = run(sse({"result": {"stop_reason": "end_turn",
                                 "message": {"role": "assistant", "content": [{"text": "答案"}]}}}))
    assert text_of(events) == "答案"


@pytest.mark.parametrize("raw, content_type, expected", [
    (sse({"event": "delta", "text": "平"}, {"event": "delta", "text": "台"},
         {"event": "complete", "result": "平台"}), "text/event-stream", "平台"),
    (sse({"result": "直接结果"}), "text/event-stream", "直接结果"),
    (b"plain text answer", "text/plain", "plain text answer"),
    (json.dumps({"response": "一次性 JSON"}).encode(), "application/json", "一次性 JSON"),
    (json.dumps({"answer": "答"}).encode(), "application/json", "答"),
])
def test_known_shapes_keep_working(raw, content_type, expected):
    assert text_of(run(raw, content_type)) == expected


def test_an_empty_stream_is_a_named_error():
    with pytest.raises(AppError) as err:
        run(b"", "text/event-stream")
    assert err.value.code == "runtime.empty_reply"


def test_a_metadata_only_stream_is_unrecognized_with_a_sample():
    with pytest.raises(AppError) as err:
        usage = {"inputTokens": 1, "outputTokens": 0, "totalTokens": 1}
        run(sse({"event": {"metadata": {"usage": usage, "metrics": {"latencyMs": 1}}}}))
    assert err.value.code == "runtime.unrecognized_reply"
    assert "metadata" in err.value.detail["sample"]


def test_a_tool_only_turn_is_not_an_error():
    events = run(sse({"event": "tool", "name": "lookup", "id": "t1"}))
    assert [e["event"] for e in events] == ["tool"]


def test_flatten_sse_text_is_unchanged_for_text_less_streams():
    # the helper is used where None means "not text" — it never raises
    assert rt.flatten_sse_text(sse({"start": True}).decode()) is None
