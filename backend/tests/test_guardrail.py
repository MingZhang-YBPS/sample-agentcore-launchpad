"""T12 — the PII guardrail preset on the platform invoke chain.

Covers: the opt-in reader, provisioning (create, adopt-by-name, persistence on the
workspace resource map), `anonymize` masking and `block` refusal on both ends of a
turn, failing open when Bedrock errors, and the chat stream's deliberate switch to
buffered mode so an entity cannot straddle two deltas.
"""

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.core.errors import AppError
from app.models.ledger import Agent
from app.services import chat as chat_service
from app.services import guardrail
from app.services import invoke as invoke_service
from app.services.workspace import WorkspaceContext

MASKED = "call me at {PHONE}"
RAW = "call me at 555-0100"


def _client_error(code: str = "ValidationException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "ApplyGuardrail")


class FakeBedrock:
    """The control-plane half: list/create/get guardrail."""

    def __init__(self, existing: list[dict] | None = None, fail_create: bool = False):
        self.existing = existing or []
        self.fail_create = fail_create
        self.created: list[dict] = []

    def get_paginator(self, _name):
        pages = [{"guardrails": self.existing}]
        return SimpleNamespace(paginate=lambda: pages)

    def create_guardrail(self, **kwargs):
        if self.fail_create:
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "CreateGuardrail")
        self.created.append(kwargs)
        return {"guardrailId": "gr-new", "version": "DRAFT"}

    def get_guardrail(self, **_kwargs):
        return {"status": "READY", "name": guardrail.GUARDRAIL_NAME}


class FakeRuntime:
    """The data-plane half: apply_guardrail."""

    def __init__(self, *, intervene: bool = False, error: bool = False):
        self.intervene = intervene
        self.error = error
        self.calls: list[dict] = []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise _client_error()
        if not self.intervene:
            return {"action": "NONE"}
        return {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": MASKED}],
            "assessments": [
                {"sensitiveInformationPolicy": {"piiEntities": [{"type": "PHONE"}]}}
            ],
        }


def _ctx(bedrock: FakeBedrock, runtime: FakeRuntime, resources: dict | None = None):
    ctx = WorkspaceContext(account_id="111122223333", region="us-west-2",
                           resources=resources if resources is not None else {})
    clients = {"bedrock": bedrock, "bedrock-runtime": runtime}
    object.__setattr__(ctx, "client", lambda service, *a, **k: clients[service])
    return ctx


# ── the opt-in reader ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,expected",
    [
        (None, None),
        ({}, None),
        ({"guardrail": {"enabled": False}}, None),
        ({"guardrail": {"enabled": True}}, {"mode": "anonymize"}),
        ({"guardrail": {"enabled": True, "mode": "block"}}, {"mode": "block"}),
        # an unknown mode falls back rather than refusing: the spec is stored JSON
        ({"guardrail": {"enabled": True, "mode": "nonsense"}}, {"mode": "anonymize"}),
        ({"guardrail": "yes"}, None),
    ],
)
def test_guardrail_config_reads_the_opt_in(spec, expected):
    assert guardrail.guardrail_config(spec) == expected


def test_spec_defaults_to_disabled():
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="a-guardrail-agent", method="harness", system_prompt="hi")
    assert spec.guardrail.enabled is False
    assert spec.guardrail.mode == "anonymize"
    assert guardrail.guardrail_config(spec.model_dump()) is None


# ── provisioning ─────────────────────────────────────────────────────────────────


def test_ensure_creates_once_and_remembers_it_on_the_context():
    bedrock, runtime = FakeBedrock(), FakeRuntime()
    ctx = _ctx(bedrock, runtime)
    assert guardrail.ensure_guardrail(ctx) == ("gr-new", "DRAFT")
    assert ctx.resources["guardrail_id"] == "gr-new"
    # a second call reads the map instead of creating a second resource
    assert guardrail.ensure_guardrail(ctx) == ("gr-new", "DRAFT")
    assert len(bedrock.created) == 1
    entities = {e["type"] for e in bedrock.created[0]["sensitiveInformationPolicyConfig"][
        "piiEntitiesConfig"
    ]}
    assert entities == set(guardrail.PII_ENTITIES)


def test_ensure_adopts_an_existing_guardrail_by_name():
    bedrock = FakeBedrock(existing=[{"name": guardrail.GUARDRAIL_NAME, "id": "gr-old",
                                    "version": "3"}])
    ctx = _ctx(bedrock, FakeRuntime())
    assert guardrail.ensure_guardrail(ctx) == ("gr-old", "3")
    assert bedrock.created == []


def test_ensure_surfaces_a_create_failure():
    ctx = _ctx(FakeBedrock(fail_create=True), FakeRuntime())
    with pytest.raises(AppError) as exc:
        guardrail.ensure_guardrail(ctx)
    assert exc.value.code == "guardrail.create_failed"


def test_describe_reports_absent_then_present():
    ctx = _ctx(FakeBedrock(), FakeRuntime())
    absent = guardrail.describe(ctx)
    assert absent["provisioned"] is False and absent["entities"]
    guardrail.ensure_guardrail(ctx)
    present = guardrail.describe(ctx)
    assert present["provisioned"] is True and present["status"] == "READY"


# ── screening ────────────────────────────────────────────────────────────────────


def test_anonymize_masks_and_block_refuses():
    ctx = _ctx(FakeBedrock(), FakeRuntime(intervene=True))
    result = guardrail.screen(RAW, source="INPUT", mode="anonymize", workspace=ctx)
    assert result.text == MASKED and result.intervened and result.entities == ("PHONE",)

    with pytest.raises(AppError) as exc:
        guardrail.screen(RAW, source="OUTPUT", mode="block", workspace=ctx)
    assert exc.value.code == "guardrail.blocked"
    assert exc.value.status_code == 422


def test_clean_text_passes_through_untouched():
    runtime = FakeRuntime()
    ctx = _ctx(FakeBedrock(), runtime)
    assert guardrail.screen("how much leave do I have?", source="INPUT", mode="block",
                            workspace=ctx).text == "how much leave do I have?"
    assert runtime.calls[0]["source"] == "INPUT"


def test_empty_text_makes_no_aws_call():
    runtime = FakeRuntime(error=True)
    assert guardrail.screen("", source="INPUT", mode="block",
                            workspace=_ctx(FakeBedrock(), runtime)).text == ""
    assert runtime.calls == []


def test_screening_fails_open_on_an_aws_error():
    """A guardrail that cannot be reached must not take the agent offline."""
    ctx = _ctx(FakeBedrock(), FakeRuntime(error=True))
    assert guardrail.screen(RAW, source="INPUT", mode="block", workspace=ctx).text == RAW

    ctx_no_resource = _ctx(FakeBedrock(fail_create=True), FakeRuntime())
    assert guardrail.screen(RAW, source="INPUT", mode="block",
                            workspace=ctx_no_resource).text == RAW


# ── the invoke chain ─────────────────────────────────────────────────────────────


def _agent(mode: str | None) -> Agent:
    spec = {"name": "pii-agent", "method": "harness"}
    if mode:
        spec["guardrail"] = {"enabled": True, "mode": mode}
    return Agent(id="a1", name="pii-agent", method="harness", status="active",
                 arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/h", spec=spec)


@pytest.fixture
def stub_dispatch(monkeypatch):
    seen: dict = {}

    def fake_dispatch(agent, prompt, **kwargs):
        seen["prompt"] = prompt
        return {"text": RAW, "session_id": "s1"}

    monkeypatch.setattr(invoke_service, "_dispatch_invoke", fake_dispatch)
    return seen


def test_invoke_screens_both_ends_when_enabled(monkeypatch, stub_dispatch):
    ctx = _ctx(FakeBedrock(), FakeRuntime(intervene=True))
    monkeypatch.setattr(invoke_service, "_agent_workspace", lambda agent, ws: ctx)
    out = invoke_service.invoke_agent_text(_agent("anonymize"), RAW)
    assert stub_dispatch["prompt"] == MASKED  # input screened before dispatch
    assert out["text"] == MASKED  # answer screened on the way out


def test_invoke_leaves_an_opted_out_agent_alone(monkeypatch, stub_dispatch):
    runtime = FakeRuntime(intervene=True)
    ctx = _ctx(FakeBedrock(), runtime)
    monkeypatch.setattr(invoke_service, "_agent_workspace", lambda agent, ws: ctx)
    out = invoke_service.invoke_agent_text(_agent(None), RAW)
    assert stub_dispatch["prompt"] == RAW and out["text"] == RAW
    assert runtime.calls == []  # no guardrail call at all


def test_invoke_block_refuses_before_dispatch(monkeypatch):
    called = {"dispatch": False}

    def fake_dispatch(agent, prompt, **kwargs):  # pragma: no cover - must not run
        called["dispatch"] = True
        return {"text": ""}

    ctx = _ctx(FakeBedrock(), FakeRuntime(intervene=True))
    monkeypatch.setattr(invoke_service, "_dispatch_invoke", fake_dispatch)
    monkeypatch.setattr(invoke_service, "_agent_workspace", lambda agent, ws: ctx)
    with pytest.raises(AppError) as exc:
        invoke_service.invoke_agent_text(_agent("block"), RAW)
    assert exc.value.code == "guardrail.blocked"
    assert called["dispatch"] is False


# ── the chat stream ──────────────────────────────────────────────────────────────


def _stream(agent: Agent, monkeypatch, ctx, deltas: list[str]):
    def fake_events(agent_, prompt, **kwargs):
        yield {"event": "tool", "data": {"name": "hr", "id": "t1"}}
        for chunk in deltas:
            yield {"event": "delta", "data": {"text": chunk}}

    monkeypatch.setattr(chat_service, "_harness_events",
                        lambda *a, **k: fake_events(a[0], a[1]))
    monkeypatch.setattr(chat_service, "context_for_workspace", lambda _id: ctx)
    return list(chat_service.chat_stream(agent, RAW, session_id="s1"))


def test_stream_buffers_and_screens_the_whole_answer(monkeypatch):
    ctx = _ctx(FakeBedrock(), FakeRuntime(intervene=True))
    # the phone number straddles two deltas — the reason buffering exists
    events = _stream(_agent("anonymize"), monkeypatch, ctx, ["call me at 555-", "0100"])
    meta = next(e for e in events if e["event"] == "meta")
    assert meta["data"]["mode"] == "buffered"
    deltas = [e["data"]["text"] for e in events if e["event"] == "delta"]
    assert deltas == [MASKED]
    assert [e["event"] for e in events if e["event"] == "tool"]  # tool events still flow
    assert events[-1]["event"] == "done"


def test_stream_keeps_token_streaming_when_disabled(monkeypatch):
    ctx = _ctx(FakeBedrock(), FakeRuntime(intervene=True))
    events = _stream(_agent(None), monkeypatch, ctx, ["call me at 555-", "0100"])
    assert next(e for e in events if e["event"] == "meta")["data"]["mode"] == "stream"
    assert [e["data"]["text"] for e in events if e["event"] == "delta"] == [
        "call me at 555-",
        "0100",
    ]


def test_stream_block_surfaces_an_error_event(monkeypatch):
    ctx = _ctx(FakeBedrock(), FakeRuntime(intervene=True))
    events = _stream(_agent("block"), monkeypatch, ctx, ["whatever"])
    error = next(e for e in events if e["event"] == "error")
    assert error["data"]["code"] == "guardrail.blocked"
    assert not any(e["event"] == "done" for e in events)


# ── the session-id contract (found by the P1 e2e) ────────────────────────────────


def test_a_short_session_id_is_refused_cleanly(monkeypatch):
    """AgentCore needs ≥33 characters; a shorter one used to surface as a 500.

    Validated at the schema boundary, so all three invoke entrances answer 422 with
    the rule named instead of letting botocore's ParamValidationError escape.
    """
    from fastapi.testclient import TestClient

    from app.core.db import SessionLocal
    from app.main import create_app
    from app.schemas.attachments import SESSION_ID_MIN

    agent = _agent(None)
    db = SessionLocal()
    try:
        agent.workspace_id = "default"
        db.add(agent)
        db.commit()
        with TestClient(create_app()) as client:
            res = client.post(
                f"/api/agents/{agent.id}/invoke",
                json={"prompt": "hi", "session_id": "too-short"},
            )
            assert res.status_code == 422
            body = res.json()
            assert "session_id" in json_dumps(body)
            # a long enough id gets past validation (it then fails on AWS, not here)
            assert SESSION_ID_MIN == 33
    finally:
        db.delete(agent)
        db.commit()
        db.close()


def json_dumps(value) -> str:
    import json

    return json.dumps(value)
