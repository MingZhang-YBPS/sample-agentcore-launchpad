"""Token usage a runtime reports in its stream (design §16.3-3): Converse
`metadata.usage` events (one per model call, as the AgentCore CLI Strands
template forwards them) are summed into one normalized `usage` event at the
end of the turn, and onto `invoke_runtime_text`'s result."""

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

    def close(self):
        pass


class StubDataPlane:
    def __init__(self, raw: bytes, content_type: str = "text/event-stream"):
        self.raw = raw
        self.content_type = content_type

    def invoke_agent_runtime(self, **params):
        return {"response": Body(self.raw), "contentType": self.content_type}


def sse(*payloads) -> bytes:
    return "".join(f"data: {json.dumps(p, ensure_ascii=False)}\n\n" for p in payloads).encode()


def run(raw: bytes, content_type: str = "text/event-stream") -> list[dict]:
    return list(rt._runtime_body_events(Body(raw), content_type, False))


def usage_events(events: list[dict]) -> list[dict]:
    return [e["data"] for e in events if e["event"] == "usage"]


def test_the_cli_strands_stream_sums_both_model_calls_into_one_usage_event():
    events = run(FIXTURE.read_bytes(), "text/event-stream; charset=utf-8")
    assert usage_events(events) == [{
        "input_tokens": 2236 + 2318,
        "output_tokens": 58 + 21,
        "total_tokens": 2294 + 2339,
        "model_calls": 2,
    }]
    # it closes the turn: after every delta and tool event
    assert events[-1]["event"] == "usage"


@pytest.mark.parametrize("wrapped", [False, True])
def test_usage_is_read_wrapped_or_bare_with_cache_counters(wrapped):
    meta = {"metadata": {"usage": {
        "inputTokens": 10, "outputTokens": 4, "totalTokens": 14,
        "cacheReadInputTokens": 6, "cacheWriteInputTokens": 2,
    }, "metrics": {"latencyMs": 5}}}
    events = run(sse(
        {"event": {"contentBlockDelta": {"delta": {"text": "hi"}, "contentBlockIndex": 0}}},
        {"event": meta} if wrapped else meta,
    ))
    assert usage_events(events) == [{
        "input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
        "cache_read_input_tokens": 6, "cache_write_input_tokens": 2, "model_calls": 1,
    }]


def test_a_missing_total_is_input_plus_output_and_junk_values_are_ignored():
    events = run(sse(
        {"event": "delta", "text": "ok"},
        {"event": {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 3}}}},
        {"event": {"metadata": {"usage": {"inputTokens": "9", "outputTokens": -1,
                                          "totalTokens": True}}}},
        {"event": {"metadata": {"metrics": {"latencyMs": 1}}}},
    ))
    assert usage_events(events) == [
        {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10, "model_calls": 1},
    ]


def test_snake_case_usage_counts_too():
    events = run(sse(
        {"event": "delta", "text": "ok"},
        {"metadata": {"usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}},
    ))
    assert usage_events(events) == [
        {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3, "model_calls": 1},
    ]


@pytest.mark.parametrize("raw, content_type", [
    (sse({"event": "delta", "text": "平"}, {"event": "complete", "result": "平"}),
     "text/event-stream"),
    (b"plain text answer", "text/plain"),
    # a free-form body's own `metadata` is not a Converse event
    (json.dumps({"response": "x", "metadata": {"usage": {"inputTokens": 1}}}).encode(),
     "application/json"),
])
def test_no_usage_reported_means_no_usage_event(raw, content_type):
    assert usage_events(run(raw, content_type)) == []


def test_a_metadata_only_stream_still_reports_its_usage_before_the_error():
    events: list[dict] = []
    usage = {"inputTokens": 1, "outputTokens": 0, "totalTokens": 1}
    with pytest.raises(AppError) as err:
        for event in rt._runtime_body_events(
            Body(sse({"event": {"metadata": {"usage": usage}}})), "text/event-stream", False,
        ):
            events.append(event)
    assert err.value.code == "runtime.unrecognized_reply"
    assert usage_events(events) == [
        {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1, "model_calls": 1},
    ]


def test_invoke_runtime_text_carries_the_turn_usage():
    stub = StubDataPlane(FIXTURE.read_bytes())
    result = rt.invoke_runtime_text(stub, "arn:rt-1", "hi")
    assert result["text"] == "Echo工具返回的结果是：你真是个大聪明"
    assert result["usage"]["total_tokens"] == 2294 + 2339
    assert result["usage"]["model_calls"] == 2


def test_invoke_runtime_text_omits_usage_when_none_was_reported():
    stub = StubDataPlane(sse({"event": "delta", "text": "hi"}))
    assert "usage" not in rt.invoke_runtime_text(stub, "arn:rt-1", "hi")


def test_the_bearer_twin_carries_usage_too():
    class Response:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_lines(self):
            return iter(sse(
                {"event": "delta", "text": "hi"},
                {"event": {"metadata": {"usage": {"inputTokens": 4, "outputTokens": 1}}}},
            ).decode().split("\n"))

        def read(self):
            return b""

    result = rt.invoke_runtime_text_bearer(
        "us-east-1", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r", "tok", "hi",
        http_response=lambda url, headers, body: Response(),
    )
    assert result["text"] == "hi"
    assert result["usage"] == {
        "input_tokens": 4, "output_tokens": 1, "total_tokens": 5, "model_calls": 1,
    }


def test_flatten_sse_text_ignores_usage():
    raw = sse(
        {"event": {"contentBlockDelta": {"delta": {"text": "a"}, "contentBlockIndex": 0}}},
        {"event": {"metadata": {"usage": {"inputTokens": 1}}}},
    ).decode()
    assert rt.flatten_sse_text(raw) == "a"
