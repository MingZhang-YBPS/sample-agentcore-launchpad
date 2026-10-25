"""strands-builder SB-1 wiring (W10-BE): the summary model's IAM grant, the /v1
`context` field that fills {{variables}} per request, a guardrail turn never
returning an unscreened `structured` object, and the SB-1 settings in the
experiment's config bundles."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.routers.public_api as public_api
from app.optimization import service as opt
from app.schemas.agent import AgentSpec
from app.services import agent_iam, chat, invoke
from app.services.agentcore import runtime
from app.services.agentcore.runtime import _invoke_payload
from tests.test_agent_iam_policy import CTX
from tests.test_strands_builder import _v1_agent

CLAUDE = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
SUMMARY = "us.amazon.nova-lite-v1:0"

# ── IAM: the summarizing manager's own model ────────────────────────────────────


def _spec(**over) -> AgentSpec:
    base = {"name": "sb-agent", "method": "zip_runtime", "system_prompt": "p", "model_id": CLAUDE}
    return AgentSpec(**{**base, **over})


def test_the_summary_model_is_granted_beside_the_agents_model():
    plain = agent_iam.allowed_model_resources(_spec(), CTX)
    spec = _spec(conversation={"strategy": "summarizing", "summary_model": SUMMARY})
    resources = agent_iam.allowed_model_resources(spec, CTX)
    assert resources[: len(plain)] == plain
    extra = agent_iam.model_resources(SUMMARY, CTX)
    assert set(extra) <= set(resources) and len(resources) == len(set(resources))
    # summarizing with the agent's own model: nothing more
    same = _spec(conversation={"strategy": "summarizing"})
    assert agent_iam.allowed_model_resources(same, CTX) == plain


# ── the runtime payload's `context` ─────────────────────────────────────────────


def test_the_payload_carries_context_only_when_given():
    assert "context" not in json.loads(_invoke_payload("hi", "a"))
    body = json.loads(_invoke_payload("hi", "a", context={"city": "Oslo"}))
    assert body["context"] == {"city": "Oslo"} and body["prompt"] == "hi"


def test_both_transports_send_the_context():
    client = MagicMock()
    client.invoke_agent_runtime.return_value = {
        "response": SimpleNamespace(read=lambda: b'{"result": "ok"}'),
        "contentType": "application/json",
    }
    result = runtime.invoke_runtime_text(client, "arn", "hi", context={"city": "Oslo"})
    assert result["text"] == "ok"
    sent = json.loads(client.invoke_agent_runtime.call_args.kwargs["payload"])
    assert sent["context"] == {"city": "Oslo"}

    seen: dict = {}

    @contextmanager
    def http(url, headers, body):
        seen["body"] = json.loads(body)
        yield SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
                              read=lambda: b'{"result": "ok"}')

    runtime.invoke_runtime_text_bearer("us-east-1", "arn", "tok", "hi", http_response=http,
                                       context={"team": "ops"})
    assert seen["body"]["context"] == {"team": "ops"}


def _agent(spec: dict | None = None):
    return SimpleNamespace(id="a1", name="a", method="zip_runtime", arn="arn:rt",
                           spec=spec or {}, workspace_id="default", endpoint_mode=None,
                           inbound_auth_config=None)


def test_invoke_agent_text_passes_context_to_the_runtime(monkeypatch):
    calls: list[dict] = []

    def fake_text(client, arn, prompt, **kwargs):
        calls.append(kwargs)
        return {"text": "ok", "session_id": "s"}

    monkeypatch.setattr(invoke, "require_invoke_capability", lambda agent: None)
    monkeypatch.setattr(invoke, "refuse_assistant_session", lambda *a: None)
    monkeypatch.setattr(invoke, "_agent_workspace", lambda agent, ws: SimpleNamespace())
    monkeypatch.setattr(invoke, "data_client", lambda ws: object())
    monkeypatch.setattr(invoke.canary_service, "active_canary_route", lambda agent_id: None)
    monkeypatch.setattr(invoke.inbound_auth_service, "is_jwt_mode", lambda agent: False)
    monkeypatch.setattr(invoke.answer_rules, "match_for_agent", lambda agent, prompt: None)
    monkeypatch.setattr(invoke, "_pending_force_reauth", lambda *a: [])
    monkeypatch.setattr(invoke.rt, "invoke_runtime_text", fake_text)
    invoke.invoke_agent_text(_agent(), "hi", context={"city": "Oslo"})
    invoke.invoke_agent_text(_agent(), "hi")
    assert calls[0]["context"] == {"city": "Oslo"}
    assert "context" not in calls[1]


# ── guardrail: `structured` is not screened, so it is dropped ───────────────────


@pytest.mark.parametrize("guarded", [True, False])
def test_a_guardrail_turn_drops_structured(monkeypatch, guarded):
    spec = {"guardrail": {"enabled": True, "mode": "anonymize"}} if guarded else {}
    monkeypatch.setattr(invoke, "_agent_workspace", lambda agent, ws: SimpleNamespace())
    monkeypatch.setattr(invoke.guardrail, "screen",
                        lambda text, **_k: SimpleNamespace(text=text))
    monkeypatch.setattr(invoke.answer_rules, "match_for_agent", lambda agent, prompt: None)
    monkeypatch.setattr(invoke, "_dispatch_invoke", lambda *a, **k: {
        "text": "Sunny.", "session_id": "s", "structured": {"city": "Oslo"}})
    result = invoke.invoke_agent_text(_agent(spec), "hi")
    assert result["text"] == "Sunny."
    assert ("structured" in result) is not guarded


def test_the_withheld_stream_drops_structured_too():
    events = [{"event": "delta", "data": {"text": "a"}},
              {"event": "structured", "data": {"value": {"x": 1}}},
              {"event": "tool", "data": {"name": "t"}}]
    buffered: list[str] = []
    out = list(chat._withhold_deltas(iter(events), buffered))
    assert out == [{"event": "tool", "data": {"name": "t"}}] and buffered == ["a"]


def test_the_buffered_event_fallback_sends_structured(monkeypatch):
    monkeypatch.setattr(invoke, "require_invoke_capability", lambda agent: None)
    monkeypatch.setattr(invoke, "refuse_assistant_session", lambda *a: None)
    monkeypatch.setattr(invoke, "_agent_workspace", lambda agent, ws: SimpleNamespace())
    monkeypatch.setattr(invoke.inbound_auth_service, "is_jwt_mode", lambda agent: False)
    seen: dict = {}

    def fake_text(agent, prompt, **kwargs):
        seen.update(kwargs)
        return {"text": "Sunny.", "session_id": "s", "structured": {"city": "Oslo"}}

    monkeypatch.setattr(invoke, "invoke_agent_text", fake_text)
    agent = _agent({"protocol": "a2a"})
    out = list(invoke.invoke_agent_events(agent, "hi", context={"city": "Oslo"}))
    assert out[-1] == {"event": "structured", "data": {"value": {"city": "Oslo"}}}
    assert seen["context"] == {"city": "Oslo"}


# ── /v1: the `context` request field ────────────────────────────────────────────


def test_v1_invoke_passes_context(client, monkeypatch):
    seen: dict = {}

    def fake(agent, prompt, **kwargs):
        seen.update(kwargs)
        return {"text": "ok", "session_id": "s" * 40}

    monkeypatch.setattr(public_api, "invoke_agent_text", fake)
    agent_id = _v1_agent("sb-v1-ctx")
    key = client.post("/api/apikeys", json={"name": "k"}).json()["key"]
    headers = {"X-Api-Key": key}
    url = f"/v1/agents/{agent_id}/invoke"
    res = client.post(url, headers=headers, json={"prompt": "hi", "context": {"city": "Oslo"}})
    assert res.status_code == 200, res.text
    assert seen["context"] == {"city": "Oslo"}
    seen.clear()
    assert client.post(url, headers=headers, json={"prompt": "hi"}).status_code == 200
    assert "context" not in seen
    for bad in ({f"k{i}": "v" for i in range(21)}, {"bad-key": "v"}, {"k": "v" * 2001}):
        res = client.post(url, headers=headers, json={"prompt": "hi", "context": bad})
        assert res.status_code == 422, bad


def test_v1_stream_passes_context(client, monkeypatch):
    seen: dict = {}

    def fake_stream(agent, prompt, **kwargs):
        seen.update(kwargs)
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(public_api, "chat_stream", fake_stream)
    agent_id = _v1_agent("sb-v1-ctx-stream")
    key = client.post("/api/apikeys", json={"name": "k"}).json()["key"]
    res = client.post(f"/v1/agents/{agent_id}/invoke-stream", headers={"X-Api-Key": key},
                      json={"prompt": "hi", "context": {"team": "ops"}})
    assert res.status_code == 200, res.text
    assert seen["context"] == {"team": "ops"}


# ── experiments: the SB-1 settings in both bundles ──────────────────────────────


def test_bundle_config_keeps_only_the_set_keys():
    spec = {"model_params": {"temperature": 0.2}, "conversation": None,
            "few_shot_examples": [], "prompt_variables": {"city": "Rome"}, "x": 1}
    assert opt.bundle_config(spec) == {"model_params": {"temperature": 0.2},
                                       "prompt_variables": {"city": "Rome"}}


def test_stage_bundles_puts_the_settings_in_control_and_treatment(monkeypatch):
    control = MagicMock()
    control.create_configuration_bundle.side_effect = lambda **kw: {
        "bundleId": kw["bundleName"], "bundleArn": "arn:" + kw["bundleName"], "versionId": "1"}
    monkeypatch.setattr(opt, "control_client", lambda ws: control)
    agent = {"arn": "arn:rt", "system_prompt": "old", "tools": {"calc": "math"},
             "bundle_config": {"model_params": {"temperature": 0.2},
                               "prompt_variables": {"city": "Rome"}}}
    out = opt.stage_bundles("abcdef123456", agent, "new", SimpleNamespace(),
                            treatment_config={"model_params": {"temperature": 0.7},
                                              "system_prompt": "ignored"})
    assert out["control"]["bundle_id"] == "exp_abcdef12_control"
    configs = [c.kwargs["components"]["arn:rt"]["configuration"]
               for c in control.create_configuration_bundle.call_args_list]
    assert configs[0] == {"model_params": {"temperature": 0.2},
                          "prompt_variables": {"city": "Rome"}, "system_prompt": "old",
                          "tools": {"calc": {"description": "math"}}}
    assert configs[1]["model_params"] == {"temperature": 0.7}
    assert configs[1]["prompt_variables"] == {"city": "Rome"}
    assert configs[1]["system_prompt"] == "new"


def test_stage_bundles_without_settings_is_the_old_request(monkeypatch):
    control = MagicMock()
    control.create_configuration_bundle.return_value = {"bundleId": "b", "versionId": "1"}
    monkeypatch.setattr(opt, "control_client", lambda ws: control)
    opt.stage_bundles("abcdef123456", {"arn": "arn:rt", "system_prompt": "p"}, "q",
                      SimpleNamespace())
    config = control.create_configuration_bundle.call_args.kwargs["components"]["arn:rt"]
    assert set(config["configuration"]) == {"system_prompt", "tools"}
