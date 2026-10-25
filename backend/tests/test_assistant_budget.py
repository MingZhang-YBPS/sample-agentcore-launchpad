# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""assistant-context C3 (the replay budget follows the architect model's context window,
in estimated tokens) and C4 (a turn that overflows the model's context window inside
one execution is classified on its own, replayed once with less history, and its tool
results are counted into the turn's trace)."""

import pytest

from app.assistant import service
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.assistant import AssistantConversation, AssistantMessage
from app.models.ledger import Agent

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    BASE,
    _latest,
    _open,
    _turn,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

MODEL = "global.example.big-model-v1"


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "assistant_context_window_tokens", 0)
    monkeypatch.setattr(s, "model_context_windows", {})
    return s


def _set_model(agent_id: str, model_id: str | None) -> None:
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.spec = {**(agent.spec or {}), "model_id": model_id}
        db.commit()
    finally:
        db.close()


def _seed(cid: str, texts: list[str]) -> None:
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        for turn, text in enumerate(texts, start=1):
            db.add(AssistantMessage(workspace_id=row.workspace_id, conversation_id=cid,
                                    turn=turn, role="user", text=text))
            db.add(AssistantMessage(workspace_id=row.workspace_id, conversation_id=cid,
                                    turn=turn, role="assistant", text=f"a{turn}"))
        row.turns = len(texts)
        db.commit()
    finally:
        db.close()


def _preamble_tokens(cid: str) -> int:
    db = SessionLocal()
    try:
        return service.estimate_tokens(
            service._preamble(db.get(AssistantConversation, cid), inline=True))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# C3: estimate, window lookup, budget
# ---------------------------------------------------------------------------


def test_token_estimate_counts_cjk_per_character_and_others_per_four():
    assert service.estimate_tokens("") == 0
    assert service.estimate_tokens("abcd" * 10) == 10
    assert service.estimate_tokens("abcde") == 2  # rounded up
    assert service.estimate_tokens("数据不能出境") == 6
    assert service.estimate_tokens("データ。한국") == 6
    assert service.estimate_tokens("区域 us-east-1") == 2 + 3  # "区域" + ceil(10 / 4)


def test_the_window_comes_from_the_override_then_the_model_metadata(settings, monkeypatch):
    assert service.model_context_window(MODEL) == (None, "fallback")
    assert service.model_context_window(None) == (None, "fallback")
    monkeypatch.setattr(settings, "model_prices", {
        **settings.model_prices, "big-model": {"input": 1.0, "output": 2.0,
                                               "max_input_tokens": 300_000}})
    assert service.model_context_window(MODEL) == (300_000, "model")
    # model_context_windows: the longest matching key wins, over the price entry too
    monkeypatch.setattr(settings, "model_context_windows",
                        {"big": 100_000, "example.big-model": 400_000})
    assert service.model_context_window(MODEL) == (400_000, "model")
    monkeypatch.setattr(settings, "assistant_context_window_tokens", 50_000)
    assert service.model_context_window(MODEL) == (50_000, "config")


def test_the_budget_is_half_the_window_capped_and_falls_back_to_characters(
        settings, monkeypatch):
    fallback = service.budget_for_model(MODEL)
    assert (fallback.unit, fallback.limit, fallback.source) == (
        "chars", service.MAX_REPLAY_CHARS, "fallback")
    assert fallback.max_turns == service.MAX_REPLAY_TURNS
    monkeypatch.setattr(settings, "model_context_windows", {"big-model": 128_000})
    budget = service.budget_for_model(MODEL)
    assert (budget.unit, budget.limit, budget.context_window_tokens) == ("tokens", 64_000, 128_000)
    monkeypatch.setattr(settings, "model_context_windows", {"big-model": 1_000_000})
    assert service.budget_for_model(MODEL).limit == settings.assistant_replay_max_tokens
    monkeypatch.setattr(settings, "assistant_replay_max_turns", 24)
    assert service.budget_for_model(MODEL).max_turns == 24


def test_status_reports_the_presets_replay_budget(client, ready, settings, monkeypatch):
    status = client.get(BASE).json()
    assert status["replay_budget"]["unit"] == "chars"  # the test preset names no model
    assert status["replay_budget"]["limit"] == service.MAX_REPLAY_CHARS
    _set_model(ready, MODEL)
    monkeypatch.setattr(settings, "model_context_windows", {"big-model": 200_000})
    budget = client.get(BASE).json()["replay_budget"]
    assert budget == {"unit": "tokens", "limit": 100_000, "max_turns": service.MAX_REPLAY_TURNS,
                      "source": "model", "model_id": MODEL, "context_window_tokens": 200_000,
                      "window_share": service.REPLAY_WINDOW_SHARE}


def test_the_token_budget_trims_cjk_turns_sooner_than_ascii_ones(
        client, ready, settings, monkeypatch):
    _set_model(ready, MODEL)
    cjk, ascii_ = _open(client), _open(client)
    _seed(cjk, ["约" * 2000] * 8)  # 2000 tokens a turn
    _seed(ascii_, ["x" * 2000] * 8)  # 500 tokens a turn
    # room for the preamble plus about 5 000 tokens of history
    window = 2 * (_preamble_tokens(cjk) + 5_000)
    monkeypatch.setattr(settings, "model_context_windows", {"big-model": window})
    ctx = _latest(client, cjk)["context"]
    assert ctx["budget"] == {"unit": "tokens", "limit": window // 2, "source": "model"}
    assert ctx["replayed_turns"] == 2 and ctx["omitted_turns"] == 6
    assert _latest(client, ascii_)["context"]["omitted_turns"] == 0
    # the same characters fit the character fallback whole
    monkeypatch.setattr(settings, "model_context_windows", {})
    assert _latest(client, cjk)["context"]["omitted_turns"] == 0


def test_the_turn_count_is_configurable(client, ready, settings, monkeypatch):
    cid = _open(client)
    _seed(cid, [f"q{i}" for i in range(20)])
    assert _latest(client, cid)["context"]["replayed_turns"] == service.MAX_REPLAY_TURNS
    monkeypatch.setattr(settings, "assistant_replay_max_turns", 24)
    assert _latest(client, cid)["context"]["replayed_turns"] == 20
    listed = client.get(f"{BASE}/conversations").json()["conversations"]
    assert [c["context_trimmed"] for c in listed if c["id"] == cid] == [False]


def test_the_list_marker_measures_against_the_token_budget(client, ready, settings, monkeypatch):
    _set_model(ready, MODEL)
    cid = _open(client)
    _seed(cid, ["x" * 40_000] * 3)  # 120 000 characters ≥ 30 000 tokens
    listed = client.get(f"{BASE}/conversations").json()["conversations"]
    assert [c["context_trimmed"] for c in listed if c["id"] == cid] == [False]
    monkeypatch.setattr(settings, "model_context_windows", {"big-model": 40_000})
    listed = client.get(f"{BASE}/conversations").json()["conversations"]
    assert [c["context_trimmed"] for c in listed if c["id"] == cid] == [True]


def test_a_prompt_beyond_the_token_budget_is_refused(client, ready, harness, settings,
                                                     monkeypatch):
    _set_model(ready, MODEL)
    cid = _open(client)
    window = 2 * (_preamble_tokens(cid) + 1_000)
    monkeypatch.setattr(settings, "model_context_windows", {"big-model": window})
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "约" * 3_000})
    assert res.status_code == 413
    assert res.json()["detail"]["max_request_tokens"] == window // 2
    assert harness.calls == []


# ---------------------------------------------------------------------------
# C4: overflow inside one execution
# ---------------------------------------------------------------------------

OVERFLOW = [{"contentBlockDelta": {"delta": {"text": "reading"}}},
            {"messageStop": {"stopReason": "model_context_window_exceeded"}}]


def _user_texts(call: dict) -> str:
    return " ".join(m["content"][0]["text"] for m in call["messages"] if m["role"] == "user")


def test_an_overflow_is_replayed_once_with_half_the_history(client, ready, harness):
    cid = _open(client)
    _seed(cid, [f"question {i} " + "x" * 400 for i in range(1, 9)])
    harness.reply("Answer within the window.")
    harness.queued = [list(OVERFLOW)]
    events = _turn(client, cid, "summarize")
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done" and len(harness.calls) == 2
    meta = events[0][1]
    retry = next(d for k, d in events if k == "retry")
    assert retry["code"] == "harness.context_window_exceeded"
    assert meta["context"]["replayed_turns"] == 8
    assert retry["context"]["replayed_turns"] == 4 == 8 - retry["omitted_turns"]
    assert retry["first_replayed_turn"] == 5
    first, second = (_user_texts(c) for c in harness.calls)
    assert "question 1 " in first and "question 1 " not in second
    assert "question 8 " in second
    # the current message is never shortened, and the replay ran in a fresh session
    assert harness.calls[1]["messages"][-1]["content"][0]["text"].endswith("summarize")
    assert harness.calls[0]["runtimeSessionId"] != harness.calls[1]["runtimeSessionId"]
    assert _latest(client, cid)["messages"][-1]["text"] == "Answer within the window."
    done = events[-1][1]
    assert done["trace"]["replayed_turns"] == 4


def test_an_overflow_after_its_replay_surfaces_as_its_own_error(client, ready, harness):
    cid = _open(client)
    harness.script = list(OVERFLOW)
    events = _turn(client, cid, "q")
    assert len(harness.calls) == 1 + service.TURN_TRANSIENT_RETRIES
    assert events[-1] == ("error", {"code": "harness.context_window_exceeded",
                                    "message": "Harness execution exceeded the model's "
                                               "context window"})
    db = SessionLocal()
    try:
        trace = service.turn_trace(db, cid, 1)
    finally:
        db.close()
    assert trace["error"] == "harness.context_window_exceeded"


def test_an_execution_limit_is_still_not_replayed(client, ready, harness):
    cid = _open(client)
    harness.script = [{"messageStop": {"stopReason": "max_iterations"}}]
    events = _turn(client, cid, "q")
    assert "retry" not in [k for k, _ in events] and len(harness.calls) == 1
    assert events[-1][1]["code"] == "harness.execution_limit"


def _tool_result(index: int, tool_id: str, *chunks: str) -> list[dict]:
    return [
        {"contentBlockStart": {"contentBlockIndex": index,
                               "start": {"toolResult": {"toolUseId": tool_id,
                                                        "status": "success"}}}},
        *({"contentBlockDelta": {"contentBlockIndex": index,
                                 "delta": {"toolResult": [{"text": c}]}}} for c in chunks),
        {"contentBlockStop": {"contentBlockIndex": index}},
    ]


def test_tool_result_characters_are_counted_into_the_turn_trace(client, ready, harness):
    cid = _open(client)
    harness.script = [
        {"contentBlockStart": {"contentBlockIndex": 0,
                               "start": {"toolUse": {"name": "aws_docs", "toolUseId": "t-1"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        *_tool_result(1, "t-1", "a" * 1000, "b" * 500),
        {"contentBlockDelta": {"contentBlockIndex": 2, "delta": {
            "toolResult": [{"json": {"k": "v"}}]}}},
        *_tool_result(3, "t-2", "文档" * 100),
        {"contentBlockDelta": {"contentBlockIndex": 4, "delta": {"text": "Done."}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    events = _turn(client, cid, "look it up")
    trace = events[-1][1]["trace"]
    assert trace["tool_results"] == 2
    assert trace["tool_result_chars"] == 1000 + 500 + len('{"k": "v"}') + 200
    db = SessionLocal()
    try:
        assert service.turn_trace(db, cid, 1) == trace
    finally:
        db.close()
    # the trace row is ledger-only: neither in the transcript nor in the replay
    detail = _latest(client, cid)
    assert [m["role"] for m in detail["messages"]] == ["user", "tool", "assistant"]
    harness.reply("ok")
    _turn(client, cid, "next")
    assert "tool_result_chars" not in _user_texts(harness.calls[-1])


def test_a_replay_records_the_failed_attempts_tool_results(client, ready, harness):
    cid = _open(client)
    harness.reply("ok")
    harness.queued = [[*_tool_result(0, "t-1", "z" * 700), *OVERFLOW]]
    events = _turn(client, cid, "q")
    assert events[-1][0] == "done"
    assert events[-1][1]["trace"]["tool_result_chars"] == 0  # the replay read nothing
    db = SessionLocal()
    try:
        replay = (db.query(AssistantMessage)
                  .filter_by(conversation_id=cid, name="turn_replay").one())
    finally:
        db.close()
    assert "tool results 1, 700 chars" in replay.text


def test_workspace_without_the_preset_gets_the_fallback_budget():
    db = SessionLocal()
    try:
        budget = service.replay_budget(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    assert budget.unit == "chars" and budget.source == "fallback"
