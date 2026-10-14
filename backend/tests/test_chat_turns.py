"""chat-streaming S4 (§5.2): a console chat turn runs on a background worker — it keeps
running when the browser goes away, `GET …/turns/{turn_id}/events?after=<seq>` resumes
it, a session runs one turn at a time, the stop button and the time limit end it, and
the worker runs inside the turn's project."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.services.chat as chat_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project, project_context
from app.main import create_app
from app.models.ledger import Agent, ChatMessage
from app.services import chat_turns
from app.services.chat import StopSignal, TurnStopped, with_keepalive
from tests.test_project_scope import _project

SESSION = "t" * 40


@pytest.fixture(autouse=True)
def fresh_registry():
    chat_turns.registry.clear()
    yield
    chat_turns.registry.clear()


def _agent(name: str, pid: str | None = None, **spec) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime",
                        status="active", arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
                        spec={"name": name, **spec})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _frames(body: str) -> list[tuple[int | None, str, dict]]:
    out = []
    for frame in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines:
            seq = int(lines["id"]) if "id" in lines else None
            out.append((seq, lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def gated(monkeypatch):
    """A runtime that answers "part one ", waits for the gate, then "part two"."""
    gate = threading.Event()
    seen: list[str | None] = []

    def invoke(*_args, **_kwargs):
        seen.append(current_project())
        yield {"event": "delta", "data": {"text": "part one "}}
        gate.wait(10)
        yield {"event": "delta", "data": {"text": "part two"}}

    monkeypatch.setattr(chat_service, "invoke_agent_events", invoke)
    yield gate, seen
    gate.set()  # no worker outlives the test


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _running_turn(agent_id: str) -> chat_turns.Turn:
    _wait(lambda: chat_turns.registry.running_for(DEFAULT_WORKSPACE_ID, agent_id, SESSION)
          is not None)
    turn = chat_turns.registry.running_for(DEFAULT_WORKSPACE_ID, agent_id, SESSION)
    assert turn is not None
    _wait(lambda: turn.user_message_id is not None)
    return turn


def _start_in_background(client: TestClient, agent_id: str, **headers) -> threading.Thread:
    """Post a turn whose response this test never reads (the browser that went away)."""
    thread = threading.Thread(target=client.post, args=(f"/api/chat/{agent_id}",),
                              kwargs={"json": {"prompt": "hello", "session_id": SESSION},
                                      "headers": headers}, daemon=True)
    thread.start()
    return thread


def _rows(session_id: str = SESSION) -> list[tuple[str, str]]:
    db = SessionLocal()
    try:
        with project_context(None):
            rows = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.id).all())
            return [(r.role, r.text) for r in rows]
    finally:
        db.close()


# ── the route ────────────────────────────────────────────────────────────────


def test_a_turn_streams_with_seq_and_names_its_turn(monkeypatch):
    agent_id = _agent("turn-seq")
    monkeypatch.setattr(chat_service, "invoke_agent_events",
                        lambda *a, **k: iter([{"event": "delta", "data": {"text": "hi"}}]))
    client = TestClient(create_app())
    frames = _frames(client.post(f"/api/chat/{agent_id}",
                                 json={"prompt": "hello", "session_id": SESSION}).text)
    seqs = [seq for seq, kind, _ in frames if kind != "heartbeat"]
    assert seqs == list(range(1, len(seqs) + 1))
    meta = next(data for _, kind, data in frames if kind == "meta")
    assert len(meta["turn_id"]) == 32 and isinstance(meta["user_message_id"], int)
    assert [kind for _, kind, _ in frames][-1] == "done"
    # a finished turn no longer holds its session
    assert chat_turns.registry.running_for(DEFAULT_WORKSPACE_ID, agent_id, SESSION) is None


def test_a_turn_keeps_running_without_its_browser_and_can_be_resumed(gated):
    gate, _ = gated
    agent_id = _agent("turn-resume")
    client = TestClient(create_app())
    _start_in_background(client, agent_id)
    turn = _running_turn(agent_id)
    _wait(lambda: any(e["event"] == "delta" for _, _, e, _ in turn._events))

    # reload: history names the running turn
    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": SESSION}).json()
    assert history["running_turn"] == {"turn_id": turn.id,
                                       "user_message_id": turn.user_message_id}

    # resume after the first delta: only what came after it
    delta_seq = next(seq for _, seq, e, _ in turn._events if e["event"] == "delta")
    threading.Timer(0.3, gate.set).start()
    res = client.get(f"/api/chat/{agent_id}/turns/{turn.id}/events",
                     params={"after": delta_seq})
    assert res.status_code == 200
    frames = _frames(res.text)
    assert all(seq is None or seq > delta_seq for seq, _, _ in frames)
    kinds = [kind for _, kind, _ in frames if kind != "heartbeat"]
    assert "meta" not in kinds and kinds[-1] == "done"
    assert [d["text"] for _, k, d in frames if k == "delta"] == ["part two"]
    # the turn was saved whole, with no interruption
    assert _rows() == [("user", "hello"), ("agent", "part one part two")]
    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": SESSION}).json()
    assert history["running_turn"] is None


def test_resuming_from_zero_replays_the_whole_turn(gated):
    gate, _ = gated
    agent_id = _agent("turn-replay")
    client = TestClient(create_app())
    _start_in_background(client, agent_id)
    turn = _running_turn(agent_id)
    gate.set()
    frames = _frames(client.get(f"/api/chat/{agent_id}/turns/{turn.id}/events").text)
    kinds = [kind for _, kind, _ in frames if kind not in ("heartbeat", "phase")]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert "".join(d["text"] for _, k, d in frames if k == "delta") == "part one part two"


def test_a_session_runs_one_turn_at_a_time(gated):
    gate, _ = gated
    agent_id = _agent("turn-busy")
    client = TestClient(create_app())
    _start_in_background(client, agent_id)
    turn = _running_turn(agent_id)
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "again", "session_id": SESSION})
    assert res.status_code == 409
    assert res.json()["code"] == "chat.turn_in_progress"
    assert res.json()["detail"]["turn_id"] == turn.id
    # another session of the same agent is not held up
    gate.set()
    other = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "session_id": "o" * 40})
    assert other.status_code == 200
    _wait(lambda: not turn.running)
    again = client.post(f"/api/chat/{agent_id}", json={"prompt": "again", "session_id": SESSION})
    assert again.status_code == 200


def test_the_stop_button_ends_the_turn_and_frees_the_session(gated):
    agent_id = _agent("turn-stop")
    client = TestClient(create_app())
    _start_in_background(client, agent_id)
    turn = _running_turn(agent_id)
    _wait(lambda: any(e["event"] == "delta" for _, _, e, _ in turn._events))
    started = time.monotonic()
    res = client.post(f"/api/chat/{agent_id}/turns/{turn.id}/stop")
    assert res.status_code == 200 and res.json() == {"turn_id": turn.id, "stopped": True}
    assert time.monotonic() - started < 3  # not at the next heartbeat or upstream event
    assert not turn.running
    assert _rows() == [("user", "hello"), ("agent", "part one "),
                       ("error", "interrupted: stopped by the user")]
    # the resumed stream ends with the stop
    frames = _frames(client.get(f"/api/chat/{agent_id}/turns/{turn.id}/events").text)
    assert frames[-1][1] == "error" and frames[-1][2]["code"] == "chat.turn_stopped"
    # stopping again is a no-op; the session takes its next turn at once
    assert client.post(f"/api/chat/{agent_id}/turns/{turn.id}/stop").json()["stopped"] is False
    gated[0].set()
    assert client.post(f"/api/chat/{agent_id}",
                       json={"prompt": "next", "session_id": SESSION}).status_code == 200


def test_a_turn_past_its_limit_is_closed_and_recorded(gated, monkeypatch):
    monkeypatch.setattr(chat_turns, "turn_limit_seconds", lambda _timeout: 61)
    monkeypatch.setattr(chat_turns.threading, "Timer", _fast_timer)
    agent_id = _agent("turn-limit")
    client = TestClient(create_app())
    frames = _frames(client.post(f"/api/chat/{agent_id}",
                                 json={"prompt": "hello", "session_id": SESSION}).text)
    error = frames[-1][2]
    assert frames[-1][1] == "error" and error["code"] == "chat.turn_timeout"
    assert error["retry"] == {"category": "timeout", "manual": False, "auto": False,
                              "timeout_s": 1}
    assert _rows()[-1] == ("error", "interrupted: the turn ran past its 61 s limit")


class _fast_timer(threading.Timer):  # noqa: N801 — stands in for threading.Timer
    """The turn's limit timer, fired after 0.3 s instead of its interval."""

    def __init__(self, _interval, function, args=None, kwargs=None):
        super().__init__(0.3, function, args=args, kwargs=kwargs)


def test_turn_limits_follow_the_agents_timeout():
    assert chat_turns.turn_limit_seconds(600) == 660
    assert chat_turns.turn_limit_seconds(None) == 3660


def test_another_projects_turn_is_not_found(gated):
    gate, seen = gated
    _project("alpha")
    _project("beta")
    agent_id = _agent("turn-alpha", "alpha")
    client = TestClient(create_app())
    _start_in_background(client, agent_id, **{"X-Project": "alpha"})
    turn = _running_turn(agent_id)
    assert turn.project_id == "alpha" and seen == ["alpha"]  # the worker's project
    for path in (f"/api/chat/{agent_id}/turns/{turn.id}/events",):
        res = client.get(path, headers={"X-Project": "beta"})
        assert res.status_code == 404
    res = client.post(f"/api/chat/{agent_id}/turns/{turn.id}/stop", headers={"X-Project": "beta"})
    assert res.status_code == 404
    # a turn of another agent, or no turn at all, is just as missing
    other = _agent("turn-alpha-2", "alpha")
    for agent, turn_id in ((other, turn.id), (agent_id, "0" * 32)):
        res = client.get(f"/api/chat/{agent}/turns/{turn_id}/events",
                         headers={"X-Project": "alpha"})
        assert res.status_code == 404
        assert res.json()["code"] in ("chat.turn_not_found", "agent.not_found")
    assert turn.running
    gate.set()


def test_stopping_a_session_stops_its_running_turn(gated, monkeypatch):
    import app.routers.chat as chat_router

    monkeypatch.setattr(chat_router, "stop_agent_session",
                        lambda *a, **k: {"ended": True, "already_ended": False})
    agent_id = _agent("turn-session-stop")
    client = TestClient(create_app())
    _start_in_background(client, agent_id)
    turn = _running_turn(agent_id)
    assert client.post(f"/api/chat/{agent_id}/sessions/{SESSION}/stop").status_code == 200
    assert not turn.running


# ── the buffer ───────────────────────────────────────────────────────────────


def _turn() -> chat_turns.Turn:
    return chat_turns.Turn(id="x", workspace_id="w", agent_id="a", project_id=None,
                           session_id="s", limit_s=60)


def test_a_late_subscriber_is_told_what_left_the_buffer(monkeypatch):
    monkeypatch.setattr(chat_turns, "MAX_EVENTS", 3)
    turn = _turn()
    for n in range(5):
        turn.publish({"event": "delta", "data": {"text": str(n)}})
    turn.finish()
    got = list(turn.subscribe(after=0))
    assert got[0] == (None, {"event": "gap", "data": {"after": 0, "first_seq": 3}})
    assert [seq for seq, _ in got[1:]] == [3, 4, 5]
    # a subscriber that saw seq 3 misses nothing
    assert [seq for seq, _ in turn.subscribe(after=3)] == [4, 5]


def test_heartbeats_are_kept_in_order_once_and_not_numbered():
    turn = _turn()
    turn.publish({"event": "meta", "data": {}})
    turn.publish({"event": "heartbeat", "data": {}})
    turn.publish({"event": "heartbeat", "data": {}})
    turn.publish({"event": "delta", "data": {"text": "x"}})
    turn.finish()
    got = [(seq, e["event"]) for seq, e in turn.subscribe()]
    assert got == [(1, "meta"), (None, "heartbeat"), (2, "delta")]
    assert "elapsed_ms" in next(e for _, e in turn.subscribe() if e["event"] == "heartbeat")["data"]


def test_a_quiet_live_turn_heartbeats_to_its_subscriber():
    turn = _turn()
    stream = turn.subscribe(interval=0.05)
    assert next(stream)[1]["event"] == "heartbeat"
    turn.publish({"event": "delta", "data": {"text": "x"}})
    assert next(stream) == (1, {"event": "delta", "data": {"text": "x"}})
    turn.finish()
    assert list(stream) == []


# ── the stop signal in the stream ────────────────────────────────────────────


def test_a_stop_ends_a_silent_stream_and_closes_the_upstream():
    release = threading.Event()
    closed = threading.Event()

    def upstream():
        try:
            yield {"event": "delta", "data": {"text": "a"}}
            release.wait(10)
            yield {"event": "delta", "data": {"text": "b"}}
        finally:
            closed.set()

    stop = StopSignal()
    stream = with_keepalive(upstream(), interval=30, stop=stop)
    assert next(stream)["event"] == "delta"
    threading.Timer(0.1, stop.set, args=("enough",)).start()
    started = time.monotonic()
    with pytest.raises(TurnStopped) as caught:
        next(stream)
    assert caught.value.interruption == "enough"
    assert time.monotonic() - started < 2
    release.set()
    assert closed.wait(2)  # closed once its in-flight step returned
    assert stop.set("again") is False and stop.reason == "enough"



def test_a_subscriber_that_goes_away_does_not_end_the_turn():
    # (TestClient reads a response to its end, so the drop is made on the subscriber)
    release = threading.Event()
    seen: list[str | None] = []

    def source(stop):
        seen.append(current_project())
        yield {"event": "meta", "data": {"session_id": SESSION, "user_message_id": 1}}
        release.wait(10)
        yield {"event": "done", "data": {}}

    turn = chat_turns.start_turn(source, workspace_id="w", agent_id="a", project_id="alpha",
                                 session_id=SESSION)
    stream = turn.subscribe()
    assert next(stream)[1]["event"] == "meta"
    stream.close()  # the browser went away
    release.set()
    _wait(lambda: not turn.running)
    assert [e["event"] for _, e in turn.subscribe()] == ["meta", "done"]
    assert seen == ["alpha"]  # the worker runs as the turn's project
