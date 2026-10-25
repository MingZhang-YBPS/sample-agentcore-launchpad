"""chat-streaming S3: retrying a failed chat turn — `retry_of` on the request and the
ledger, a retried turn replayed once, and the `retry` hint on console error events
(which failures offer a retry button, which are retried once automatically)."""

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app.routers.chat as chat_router
import app.services.chat as chat_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, envelope
from app.core.project_scope import project_context
from app.main import create_app
from app.models.ledger import Agent, ChatMessage
from tests.test_project_scope import _project

SESSION = "r" * 40


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


def _frames(body: str) -> list[tuple[str, dict]]:
    import json

    out = []
    for frame in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def upstream(monkeypatch):
    """What the next turns' runtime does: a list of event lists or exceptions."""
    script: list = []

    def invoke(*_args, **_kwargs):
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        yield from step

    monkeypatch.setattr(chat_service, "invoke_agent_events", invoke)
    return script


def _history(client: TestClient, agent_id: str) -> list[tuple[str, str]]:
    rows = client.get(f"/api/chat/{agent_id}/history", params={"session_id": SESSION}).json()
    return [(m["role"], m["text"]) for m in rows["messages"]]


def test_a_retried_turn_is_recorded_and_replayed_once(upstream):
    agent_id = _agent("retry-agent")
    client = TestClient(create_app())
    upstream.append(RuntimeError("runtime client error: boom"))
    failed = _frames(client.post(f"/api/chat/{agent_id}",
                                 json={"prompt": "hello", "session_id": SESSION}).text)
    meta = dict(failed)["meta"]
    error = dict(failed)["error"]
    # the turn's user row and its error row are both named, so either can be retried
    assert isinstance(meta["user_message_id"], int)
    assert error["message_id"] > meta["user_message_id"]
    assert error["retry"] == {"category": "busy", "manual": True, "auto": True}

    upstream.append([{"event": "delta", "data": {"text": "hi there"}}])
    res = client.post(f"/api/chat/{agent_id}", json={
        "prompt": "hello", "session_id": SESSION, "retry_of": error["message_id"]})
    assert res.status_code == 200, res.text
    retried = dict(_frames(res.text))["meta"]["user_message_id"]

    db = SessionLocal()
    try:
        row = db.get(ChatMessage, retried)
        assert row.retry_of == meta["user_message_id"]  # the failed turn's user row
        # the failed turn stays in the ledger
        assert db.query(ChatMessage).filter(ChatMessage.session_id == SESSION).count() == 4
    finally:
        db.close()
    assert _history(client, agent_id) == [("user", "hello"), ("agent", "hi there")]


def test_only_the_latest_failed_turn_can_be_retried(upstream):
    agent_id = _agent("retry-guard")
    client = TestClient(create_app())
    upstream.append([{"event": "delta", "data": {"text": "fine"}}])
    ok = dict(_frames(client.post(f"/api/chat/{agent_id}",
                                  json={"prompt": "one", "session_id": SESSION}).text))
    # a turn that answered is not retried
    res = client.post(f"/api/chat/{agent_id}", json={
        "prompt": "one", "session_id": SESSION, "retry_of": ok["meta"]["user_message_id"]})
    assert res.status_code == 409 and res.json()["code"] == "chat.retry_not_allowed"

    upstream.append(RuntimeError("runtime client error: boom"))
    failed = dict(_frames(client.post(f"/api/chat/{agent_id}",
                                      json={"prompt": "two", "session_id": SESSION}).text))
    upstream.append([{"event": "delta", "data": {"text": "three"}}])
    client.post(f"/api/chat/{agent_id}", json={"prompt": "three", "session_id": SESSION})
    # no longer the latest turn
    res = client.post(f"/api/chat/{agent_id}", json={
        "prompt": "two", "session_id": SESSION, "retry_of": failed["meta"]["user_message_id"]})
    assert res.status_code == 409


def test_an_empty_turn_can_be_retried(upstream):
    agent_id = _agent("retry-empty")
    client = TestClient(create_app())
    upstream.append([])  # the runtime answered nothing at all
    meta = dict(_frames(client.post(f"/api/chat/{agent_id}",
                                    json={"prompt": "hi", "session_id": SESSION}).text))["meta"]
    upstream.append([{"event": "delta", "data": {"text": "now"}}])
    res = client.post(f"/api/chat/{agent_id}", json={
        "prompt": "hi", "session_id": SESSION, "retry_of": meta["user_message_id"]})
    assert res.status_code == 200
    assert _history(client, agent_id) == [("user", "hi"), ("agent", "now")]


def test_a_foreign_message_answers_404(upstream):
    _project("alpha")
    _project("beta")
    mine = _agent("retry-mine", "alpha")
    other = _agent("retry-other", "alpha")
    theirs = _agent("retry-theirs", "beta")
    client = TestClient(create_app())
    upstream.append(RuntimeError("runtime client error: boom"))
    meta = dict(_frames(client.post(f"/api/chat/{mine}", json={
        "prompt": "hi", "session_id": SESSION}, headers={"X-Project": "alpha"}).text))["meta"]
    message_id = meta["user_message_id"]
    # another session, another agent, another project: all a missing message
    for agent_id, session_id, project in [
        (mine, "s" * 40, "alpha"), (other, SESSION, "alpha"), (theirs, SESSION, "beta"),
    ]:
        res = client.post(f"/api/chat/{agent_id}", json={
            "prompt": "hi", "session_id": session_id, "retry_of": message_id},
            headers={"X-Project": project})
        assert res.status_code == 404, (agent_id, res.text)
        assert res.json()["code"] == "chat.message_not_found"
    # without a session there is nothing to retry
    res = client.post(f"/api/chat/{mine}", json={"prompt": "hi", "retry_of": message_id},
                      headers={"X-Project": "alpha"})
    assert res.status_code == 404


def test_a_failure_after_output_is_not_retried_automatically(upstream):
    agent_id = _agent("retry-output")
    client = TestClient(create_app())

    def partial():
        yield {"event": "tool", "data": {"name": "write_file"}}
        raise RuntimeError("internal server error: x")

    upstream.append(partial())
    error = dict(_frames(client.post(f"/api/chat/{agent_id}",
                                     json={"prompt": "hi", "session_id": SESSION}).text))["error"]
    # a replay could repeat the tool's side effect: the user decides
    assert error["retry"] == {"category": "busy", "manual": True, "auto": False}


def _hint(exc: BaseException, *, produced: bool = False, timeout_s=None) -> dict:
    if isinstance(exc, AppError):
        data = envelope(exc.code, exc.message, exc.detail)
    else:
        data = {"message": f"{type(exc).__name__}: {exc}"}
    return chat_router.retry_hint(data, produced=produced, timeout_s=timeout_s)


def test_retry_hints_by_failure():
    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}},
                            "InvokeAgentRuntime")
    assert _hint(throttled)["category"] == "busy" and _hint(throttled)["auto"]
    assert _hint(httpx.ReadTimeout("read timed out")) == {
        "category": "connection", "manual": True, "auto": True}
    # a Harness loop cut short with nothing at all, and one that stopped after a tool
    assert _hint(AppError("harness.incomplete_response", "x", {"stop_reason": None}))["auto"]
    assert _hint(AppError("harness.incomplete_response", "x", {"stop_reason": "tool_result"}),
                 produced=True)["auto"] is False
    # the agent spent its whole budget: no replay, the copy names the budget
    assert _hint(AppError("harness.execution_timeout", "x"), timeout_s=600) == {
        "category": "timeout", "manual": False, "auto": False, "timeout_s": 600}
    # a named, final error (permission, limit, configuration): no retry at all
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Invoke")
    assert _hint(denied) == {"category": "config", "manual": False, "auto": False}
    assert _hint(AppError("harness.execution_limit", "x")) == {
        "category": "config", "manual": False, "auto": False}
    # an unclassified failure: the user may retry, nothing retries by itself
    assert _hint(ValueError("odd")) == {"category": "other", "manual": True, "auto": False}
    # an explicit flag from the stream wins over the rebuilt exception
    assert chat_router.retry_hint({"message": "x", "transient": True}, produced=False)["auto"]


def test_history_hides_only_the_retried_turn():
    from app.services.chat_ledger import visible_rows

    def row(id_, role, retry_of=None):
        return ChatMessage(id=id_, role=role, text=str(id_), retry_of=retry_of)

    rows = [row(1, "user"), row(2, "agent"), row(3, "user"), row(4, "error"),
            row(5, "user", retry_of=3), row(6, "error"), row(7, "user", retry_of=5),
            row(8, "agent")]
    assert [r.id for r in visible_rows(rows)] == [1, 2, 7, 8]


def test_a_retried_turn_does_not_record_its_late_interruption():
    """The page gave up on a silent turn and retried it before the server noticed the
    disconnect: the old stream's interruption must not land inside the retry's turn."""
    from app.services.chat_ledger import persist_events, save_message

    agent_id = _agent("retry-late")

    def upstream():
        yield {"event": "meta", "data": {"session_id": SESSION}}
        yield {"event": "delta", "data": {"text": "half"}}
        yield {"event": "delta", "data": {"text": " an answer"}}

    def run(retried: bool) -> list[str]:
        session = SESSION if retried else "q" * 40
        events = ({**e, "data": {**e["data"], "session_id": session}} if e["event"] == "meta" else e
                  for e in upstream())
        stream = persist_events(events, workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                                prompt="hi", actor_id="river", session_id=session)
        meta = next(stream)
        next(stream)  # the first delta reached the browser
        if retried:
            save_message(DEFAULT_WORKSPACE_ID, agent_id, session, "user", "hi",
                         retry_of=meta["data"]["user_message_id"])
        stream.close()  # the browser is gone
        db = SessionLocal()
        try:
            return [r.role for r in db.query(ChatMessage).filter(ChatMessage.session_id == session)
                    .order_by(ChatMessage.id)]
        finally:
            db.close()

    assert run(retried=False) == ["user", "agent", "error"]  # kept, as before
    assert run(retried=True) == ["user", "user"]
