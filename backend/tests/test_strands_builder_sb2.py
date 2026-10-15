"""strands-builder SB-2: agents-as-tools, Anthropic API / LiteLLM gateway providers
(key through an Identity Connection), tool-call hooks (approval list, per-turn cap,
masked log) and throttling retry for the zip_runtime (Strands) method — spec
validation, ledger resolution, the IAM grant, generated code, and byte-identical
output for every spec that sets none of them."""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError
from strands.models.model import Model

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.deployer import zip_runtime
from app.deployer.zip_runtime import (
    MANTLE_EXTRA_REQUIREMENTS,
    _method_requirements,
    platform_requirements,
)
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import agent_iam, agent_tools
from app.templates.strands_a2a_agent import render_a2a_main_py
from app.templates.strands_agent import base_requirements, render_main_py
from tests import strands_builder_sb2_snapshots as sb2_snap
from tests.strands_builder_snapshots import OLD_SPECS, dump, render
from tests.test_project_scope import _project

CLAUDE = "global.anthropic.claude-sonnet-4-5-v1:0"
BASE = {"name": "sb2-agent", "method": "zip_runtime", "system_prompt": "Be brief.",
        "model_id": CLAUDE}
ACCT, REGION = "111122223333", "us-east-1"
ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:runtime/helper_ab12cd-XyZ0123456"
ARN2 = f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:runtime/peer_ab12cd-QwE0123456"
TOOL = {"agent_id": "a1", "tool_name": "ask_helper", "description": "Ask the helper agent."}
SB2_NAMES = ("agent_tools", "model_provider", "tool_hooks", "retry")


def _spec(**over) -> AgentSpec:
    return AgentSpec(**{**BASE, **over})


def _resolved(**over) -> dict:
    return {**TOOL, "runtime_arn": ARN, **over}


# ── older specs: byte-identical ──────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(sb2_snap.SB1_SPECS))
def test_sb1_spec_renders_byte_identical_code(key):
    spec = sb2_snap.SB1_SPECS[key]
    assert render(spec) == sb2_snap.fixture_path(key).read_text(encoding="utf-8")
    assert dump(spec) == sb2_snap.dump_path(key).read_text(encoding="utf-8")


@pytest.mark.parametrize("specs", [OLD_SPECS, sb2_snap.SB1_SPECS])
def test_older_specs_store_no_sb2_field(specs):
    for spec in specs.values():
        stored = spec.model_dump()
        assert not any(name in stored for name in SB2_NAMES)
        assert "launchpad_turn_state" not in render(spec)


def test_older_spec_requirements_are_unchanged():
    assert _method_requirements(OLD_SPECS["default"]) == base_requirements()
    assert platform_requirements("zip_runtime", "bedrock") == base_requirements()


# ── spec validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field,value", [
    ("agent_tools", [TOOL]),
    ("model_provider", {"kind": "anthropic", "connection": "anthropic-key"}),
    ("tool_hooks", {"max_tool_calls_per_turn": 3}),
    ("retry", {"max_attempts": 3}),
])
def test_sb2_fields_are_strands_only(field, value):
    with pytest.raises(ValidationError, match="zip_runtime"):
        AgentSpec(name="sb2-harness", method="harness", system_prompt="p", **{field: value})
    with pytest.raises(ValidationError, match="code/code_bundle"):
        _spec(code_bundle={"main.py": "print(1)"}, **{field: value})


@pytest.mark.parametrize("name", ["1abc", "ask-helper", "a b", "x" * 65, ""])
def test_agent_tool_names_are_identifiers(name):
    with pytest.raises(ValidationError):
        _spec(agent_tools=[{**TOOL, "tool_name": name}])


def test_agent_tools_unique_and_not_shadowing():
    with pytest.raises(ValidationError, match="unique"):
        _spec(agent_tools=[TOOL, {**TOOL, "agent_id": "a2"}])
    with pytest.raises(ValidationError, match="once"):
        _spec(agent_tools=[TOOL, {**TOOL, "tool_name": "other"}])
    with pytest.raises(ValidationError, match="already a tool"):
        _spec(agent_tools=[{**TOOL, "tool_name": "calculator"}])
    with pytest.raises(ValidationError, match="already a tool"):
        _spec(strands_tools=["think"], agent_tools=[{**TOOL, "tool_name": "think"}])
    with pytest.raises(ValidationError):
        _spec(agent_tools=[{**TOOL, "runtime_arn": "arn:aws:iam::1:role/x"}])


def test_model_provider_rules():
    _spec(model_id="claude-sonnet-4-5", model_provider={"kind": "anthropic", "connection": "k"})
    with pytest.raises(ValidationError, match="base_url"):
        _spec(model_provider={"kind": "litellm", "connection": "k"})
    _spec(model_id="gpt-4o", model_provider={
        "kind": "litellm", "connection": "k", "base_url": "http://litellm.internal:4000"})
    for url in ("ftp://x.example", "https://user:pw@x.example", "https://x.example/?key=1"):
        with pytest.raises(ValidationError, match="base_url"):
            _spec(model_provider={"kind": "litellm", "connection": "k", "base_url": url})
    with pytest.raises(ValidationError):
        _spec(model_provider={"kind": "anthropic", "connection": "has space"})
    with pytest.raises(ValidationError, match="A2A"):
        _spec(protocol="a2a", model_provider={"kind": "anthropic", "connection": "k"})
    with pytest.raises(ValidationError, match="model_source"):
        _spec(model_source="mantle", model_id="openai.gpt-5.6-sol",
              model_provider={"kind": "anthropic", "connection": "k"})
    with pytest.raises(ValidationError, match="Converse"):
        _spec(model_provider={"kind": "anthropic", "connection": "k"},
              model_params={"prompt_caching": True})


def test_the_key_never_sits_in_the_spec():
    spec = _spec(model_provider={"kind": "anthropic", "connection": "k", "api_key": "sk-123"})
    assert "sk-123" not in spec.model_dump_json()
    assert "sk-123" not in render_main_py(spec)


def test_hooks_and_retry_rules():
    hooks = _spec(tool_hooks={"approval_tools": [" send_email ", "crm___*", "send_email"],
                              "mask_args": ["Card_Number"]}).tool_hooks
    assert hooks.approval_tools == ["send_email", "crm___*"]
    assert hooks.mask_args == ["card_number"]
    with pytest.raises(ValidationError, match="approval tool"):
        _spec(tool_hooks={"approval_tools": ["a b"]})
    with pytest.raises(ValidationError):
        _spec(tool_hooks={"max_tool_calls_per_turn": 0})
    with pytest.raises(ValidationError, match="max_delay_s"):
        _spec(retry={"initial_delay_s": 30, "max_delay_s": 10})
    with pytest.raises(ValidationError):
        _spec(retry={"max_attempts": 11})


# ── requirements ─────────────────────────────────────────────────────────────────


def test_provider_requirements():
    anthropic = _spec(model_provider={"kind": "anthropic", "connection": "k"})
    assert _method_requirements(anthropic) == base_requirements() + [
        "strands-agents[anthropic]>=1.47,<2"]
    litellm = _spec(model_provider={"kind": "litellm", "connection": "k",
                                    "base_url": "https://llm.example.com"})
    assert _method_requirements(litellm) == base_requirements() + MANTLE_EXTRA_REQUIREMENTS
    # hooks / retry / agent tools need nothing beyond strands-agents and boto3
    assert _method_requirements(_spec(retry={}, tool_hooks={"log_tool_calls": True},
                                      agent_tools=[_resolved()])) == base_requirements()


# ── the IAM grant ────────────────────────────────────────────────────────────────


def _ctx():
    return agent_iam.RoleContext(account_id=ACCT, region=REGION, artifacts_bucket="b",
                                 ecr_repo_arn="arn:aws:ecr:us-east-1:111122223333:repository/r")


def test_agent_tools_grant_exactly_their_runtimes():
    spec = _spec(agent_tools=[
        _resolved(),
        {"agent_id": "a2", "tool_name": "ask_peer", "description": "Peer.",
         "runtime_arn": ARN2, "qualifier": "live", "runtime_user": True},
        {"agent_id": "a3", "tool_name": "ask_far", "description": "Elsewhere.",
         "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:999988887777:runtime/x_ab12cd-Q"},
        {"agent_id": "a4", "tool_name": "unresolved", "description": "No ARN yet."},
    ])
    statements = {s["Sid"]: s for s in agent_iam._agent_tool_statements(spec, _ctx())}
    assert statements["AgentToolsInvoke"]["Action"] == ["bedrock-agentcore:InvokeAgentRuntime"]
    assert statements["AgentToolsInvoke"]["Resource"] == sorted([
        ARN, f"{ARN}/runtime-endpoint/DEFAULT", ARN2, f"{ARN2}/runtime-endpoint/live"])
    assert statements["AgentToolsInvokeForUser"]["Resource"] == [
        ARN2, f"{ARN2}/runtime-endpoint/live"]
    doc = agent_iam.policy_document(spec, _ctx())
    assert any(s.get("Sid") == "AgentToolsInvoke" for s in doc["Statement"])
    # nothing for a spec without agent tools
    assert agent_iam._agent_tool_statements(_spec(), _ctx()) == []
    plain = agent_iam.policy_document(_spec(), _ctx())
    assert not any(s.get("Sid", "").startswith("AgentTools") for s in plain["Statement"])


# ── ledger resolution ────────────────────────────────────────────────────────────


def _agent_row(name, *, project="default", workspace_id=DEFAULT_WORKSPACE_ID, **fields) -> str:
    base = {"workspace_id": workspace_id, "name": name, "method": "zip_runtime",
            "status": "active", "arn": ARN, "spec": {"protocol": "http"},
            "inbound_auth_mode": "iam"}
    db = SessionLocal()
    try:
        with project_context(project):
            row = Agent(**{**base, **fields})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _resolve(spec, *, project="default", agent_id=None):
    db = SessionLocal()
    try:
        with project_context(project):
            return agent_tools.resolve_agent_tools(
                db, spec, agent_id=agent_id, workspace_id=DEFAULT_WORKSPACE_ID)
    finally:
        db.close()


def _ref_spec(agent_id: str, **over) -> AgentSpec:
    return _spec(agent_tools=[{**TOOL, "agent_id": agent_id, **over}])


def test_resolution_fills_the_server_owned_fields():
    helper = _agent_row("helper", endpoint_mode="live",
                        spec={"protocol": "http", "tools": [{"type": "gateway", "name": "gw"}]})
    peer = _agent_row("peer", arn=ARN2, spec={"protocol": "a2a"})
    # a forged ARN is replaced by the ledger's
    refs = _resolve(_spec(agent_tools=[
        {**TOOL, "agent_id": helper, "runtime_arn": ARN2},
        {"agent_id": peer, "tool_name": "ask_peer", "description": "Peer."},
    ]))
    assert refs[0]["runtime_arn"] == ARN and refs[0]["qualifier"] == "live"
    assert refs[0]["runtime_user"] is True and refs[0]["protocol"] == "http"
    assert refs[1]["runtime_arn"] == ARN2 and refs[1]["protocol"] == "a2a"
    assert refs[1]["qualifier"] == "DEFAULT" and refs[1]["runtime_user"] is False
    assert refs[0]["tool_name"] == "ask_helper" and refs[0]["shared_from"] is None


@pytest.mark.parametrize("fields,reason", [
    ({"status": "draft", "arn": None}, "not_deployed"),
    ({"method": "harness"}, "not_runtime"),
    ({"spec": {"protocol": "mcp"}}, "protocol"),
    ({"inbound_auth_mode": "jwt", "inbound_auth_config": {"mode": "jwt"}}, "jwt_inbound"),
])
def test_ineligible_agents_are_refused(fields, reason):
    target = _agent_row(f"t-{reason}", **fields)
    with pytest.raises(AppError) as err:
        _resolve(_ref_spec(target))
    assert err.value.code == f"agent_tools.{reason}" and err.value.status_code == 422


def test_self_and_foreign_ids():
    own = _agent_row("own")
    with pytest.raises(AppError, match="itself"):
        _resolve(_ref_spec(own), agent_id=own)
    _project("alpha")
    theirs = _agent_row("theirs", project="alpha")
    with pytest.raises(NotFoundError):
        _resolve(_ref_spec(theirs))
    other_ws = _agent_row("other-ws", workspace_id="acct-other")
    with pytest.raises(NotFoundError):
        _resolve(_ref_spec(other_ws))
    with pytest.raises(NotFoundError):
        _resolve(_ref_spec("missing"))
    # the owner project sees its own
    assert _resolve(_ref_spec(theirs), project="alpha")[0]["runtime_arn"] == ARN


def test_a_use_share_of_another_projects_agent_resolves(monkeypatch):
    _project("alpha")
    lent = _agent_row("lent", project="alpha")
    monkeypatch.setattr(agent_tools, "_shared_agent_ids",
                        lambda db, pid: frozenset({lent}) if pid == "default" else frozenset())
    refs = _resolve(_ref_spec(lent))
    assert refs[0]["runtime_arn"] == ARN and refs[0]["shared_from"] == "alpha"


def test_generate_stage_writes_resolved_refs_back(monkeypatch):
    helper = _agent_row("helper-gen")
    caller = _agent_row("caller", spec={**_ref_spec(helper, runtime_arn=ARN2).model_dump()})
    db = SessionLocal()
    try:
        with project_context("default"):
            row = db.get(Agent, caller)
            spec = zip_runtime._resolve_agent_tools(row, AgentSpec(**row.spec))
            assert spec.agent_tools[0].runtime_arn == ARN
            assert row.spec["agent_tools"][0]["runtime_arn"] == ARN
            code, _ = zip_runtime._generate_code(spec)
            assert repr(ARN) in code and repr(ARN2) not in code
    finally:
        db.close()


# ── generated code ───────────────────────────────────────────────────────────────


class ToolModel(Model):
    """Calls ``tool`` with ``tool_input`` on the first model call of a turn (unless
    no such tool is offered), then answers with text."""

    def __init__(self, tool: str | None = None, tool_input: dict | None = None, calls: int = 1):
        self.config: dict = {}
        self.tool, self.tool_input, self.max_calls = tool, tool_input or {}, calls
        self.seen: list[dict] = []

    def update_config(self, **kw):
        self.config.update(kw)

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):  # pragma: no cover - unused path
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        names = [t["name"] for t in tool_specs or []]
        results = [b["toolResult"] for m in messages if m["role"] == "user"
                   for b in m["content"] if "toolResult" in b]
        self.seen.append({"tools": names, "results": results})
        yield {"messageStart": {"role": "assistant"}}
        if self.tool in names and len(results) < self.max_calls:
            n = len(results) + 1
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{n}",
                                                               "name": self.tool}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps(self.tool_input)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "Done."}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _load(spec: AgentSpec, tmp_path: Path, monkeypatch, stem: str):
    monkeypatch.setenv("AWS_REGION", REGION)
    source = render_main_py(spec)
    compile(source, f"{stem}.py", "exec")
    target = tmp_path / f"{stem}.py"
    target.write_text(source, encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location(stem, target)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.BedrockAgentCoreContext, "get_config_bundle", staticmethod(lambda: {})
    )
    return module


def _drain(agen):
    async def collect():
        return [event async for event in agen]

    return asyncio.run(collect())


class FakeStream:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def iter_lines(self):
        return iter(self.body.splitlines())


class FakeRuntime:
    def __init__(self, content_type: str, body: bytes):
        self.calls: list[dict] = []
        self.content_type, self.body = content_type, body

    def invoke_agent_runtime(self, **params):
        self.calls.append(params)
        return {"contentType": self.content_type, "response": FakeStream(self.body)}


SSE = (b'data: {"event": "delta", "text": "Paris "}\n\n'
       b'data: {"event": "heartbeat"}\n\n'
       b'data: {"event": "delta", "text": "is sunny."}\n\n'
       b'data: {"event": "complete", "result": "Paris is sunny."}\n\n')


def test_agent_tools_join_the_default_tools_and_call_the_runtime(tmp_path, monkeypatch):
    spec = _spec(agent_tools=[_resolved(qualifier="live", runtime_user=True),
                              {"agent_id": "a2", "tool_name": "ask_peer",
                               "description": "Ask the A2A peer.", "runtime_arn": ARN2,
                               "protocol": "a2a"}])
    module = _load(spec, tmp_path, monkeypatch, "sb2_agents")
    agent = module.build_agent("u", "s")
    assert sorted(agent.tool_registry.registry) == [
        "ask_helper", "ask_peer", "calculator", "current_utc_time"]
    assert agent.tool_registry.registry["ask_helper"].tool_spec["description"] == (
        "Ask the helper agent.")
    assert module.DEFAULT_TOOL_DESCRIPTIONS["ask_peer"] == "Ask the A2A peer."

    fake = FakeRuntime("text/event-stream", SSE)
    monkeypatch.setattr(module, "_launchpad_runtime_client", lambda: fake)
    module.launchpad_turn_state({"actor_id": "alice"}, "sess-1")
    target = module.AGENT_TOOL_SPECS[0]
    assert module.launchpad_call_agent(target, "weather in Paris?") == "Paris is sunny."
    call = fake.calls[0]
    assert call["agentRuntimeArn"] == ARN and call["qualifier"] == "live"
    assert call["runtimeUserId"] == "alice"
    assert len(call["runtimeSessionId"]) == 64
    assert json.loads(call["payload"]) == {
        "prompt": "weather in Paris?", "actor_id": "alice", "agent_tool_depth": 1}
    # the same session continues the same sub-conversation
    module.launchpad_call_agent(target, "and tomorrow?")
    assert fake.calls[1]["runtimeSessionId"] == call["runtimeSessionId"]

    a2a_body = {"jsonrpc": "2.0", "result": {"kind": "task", "artifacts": [
        {"parts": [{"kind": "text", "text": "Peer says hi."}]}]}}
    fake = FakeRuntime("application/json", json.dumps(a2a_body).encode())
    monkeypatch.setattr(module, "_launchpad_runtime_client", lambda: fake)
    assert module.launchpad_call_agent(module.AGENT_TOOL_SPECS[1], "hi") == "Peer says hi."
    sent = json.loads(fake.calls[0]["payload"])
    assert sent["method"] == "message/send"
    assert sent["params"]["message"]["parts"] == [{"kind": "text", "text": "hi"}]
    assert "runtimeUserId" not in fake.calls[0]


def test_agent_tool_errors_and_depth(tmp_path, monkeypatch):
    module = _load(_spec(agent_tools=[_resolved()]), tmp_path, monkeypatch, "sb2_depth")
    target = module.AGENT_TOOL_SPECS[0]
    module.launchpad_turn_state({"agent_tool_depth": 2}, "s")
    assert module.launchpad_call_agent(target, "x").startswith("refused: agents calling agents")

    class Boom:
        def invoke_agent_runtime(self, **_):
            raise RuntimeError("AccessDenied")

    module.launchpad_turn_state({}, "s")
    monkeypatch.setattr(module, "_launchpad_runtime_client", lambda: Boom())
    assert module.launchpad_call_agent(target, "x") == (
        "error: the agent ask_helper could not be called (RuntimeError)")
    error_sse = b'data: {"event": "error", "message": "model unavailable"}\n\n'
    monkeypatch.setattr(module, "_launchpad_runtime_client",
                        lambda: FakeRuntime("text/event-stream", error_sse))
    assert module.launchpad_call_agent(target, "x") == "error from the agent: model unavailable"


def test_one_turn_through_an_agent_tool(tmp_path, monkeypatch):
    module = _load(_spec(agent_tools=[_resolved()]), tmp_path, monkeypatch, "sb2_turn")
    fake_model = ToolModel("ask_helper", {"request": "forecast?"})
    monkeypatch.setattr(module, "build_model", lambda: fake_model)
    fake = FakeRuntime("text/event-stream", SSE)
    monkeypatch.setattr(module, "_launchpad_runtime_client", lambda: fake)
    out = _drain(module.invoke({"prompt": "weather?", "actor_id": "bob"}))
    assert {"event": "tool", "name": "ask_helper", "id": "t1"} in out
    assert out[-1] == {"event": "complete", "result": "Done.\n"}
    assert json.loads(fake.calls[0]["payload"])["prompt"] == "forecast?"
    assert fake_model.seen[1]["results"][0]["content"][0]["text"] == "Paris is sunny."


def _fake_provider_modules(monkeypatch):
    seen: dict = {}

    def recorder(kind):
        class Fake:
            def __init__(self, **kwargs):
                seen[kind] = kwargs

        return Fake

    anthropic_mod = types.ModuleType("strands.models.anthropic")
    anthropic_mod.AnthropicModel = recorder("anthropic")
    openai_mod = types.ModuleType("strands.models.openai")
    openai_mod.OpenAIModel = recorder("openai")
    monkeypatch.setitem(sys.modules, "strands.models.anthropic", anthropic_mod)
    monkeypatch.setitem(sys.modules, "strands.models.openai", openai_mod)
    return seen


def test_anthropic_provider_reads_the_key_through_the_vault_helper(tmp_path, monkeypatch):
    spec = _spec(model_id="claude-sonnet-4-5",
                 model_provider={"kind": "anthropic", "connection": "anthropic-key"},
                 model_params={"temperature": 0.3, "stop_sequences": ["END"]})
    source = render_main_py(spec)
    # the identity block is rendered for its token-vault helper, with no tool of its own
    assert "def resource_api_key(" in source and "IDENTITY_AUTH_TOOLS = []" in source
    assert "IDENTITY_TOOLS = lambda" in source
    # an isolated project's Bedrock profile never replaces the provider's model name
    monkeypatch.setenv("LAUNCHPAD_MODEL_ID",
                       "arn:aws:bedrock:us-east-1:1:application-inference-profile/p")
    module = _load(spec, tmp_path, monkeypatch, "sb2_anthropic")
    seen = _fake_provider_modules(monkeypatch)
    asked: list = []
    monkeypatch.setattr(module, "resource_api_key",
                        lambda auth: asked.append(auth["connection"]) or "sk-vault")
    kwargs = module.launchpad_builder_kwargs({"model": "bedrock-id", "tools": []})
    assert type(kwargs["model"]).__name__ == "Fake"
    assert seen["anthropic"] == {
        "client_args": {"api_key": "sk-vault"}, "model_id": "claude-sonnet-4-5",
        "max_tokens": 4096, "params": {"temperature": 0.3, "stop_sequences": ["END"]}}
    module.launchpad_builder_kwargs({"model": "bedrock-id", "tools": []})
    assert asked == ["anthropic-key"]  # cached between turns
    module._LAUNCHPAD_PROVIDER_KEYS.clear()
    monkeypatch.setattr(module, "resource_api_key", lambda auth: None)
    with pytest.raises(RuntimeError, match="no API key from Connection 'anthropic-key'"):
        module.launchpad_builder_kwargs({"model": "bedrock-id", "tools": []})


def test_litellm_provider_is_an_openai_compatible_gateway(tmp_path, monkeypatch):
    spec = _spec(model_id="team-gpt", model_params={"max_tokens": 900},
                 model_provider={"kind": "litellm", "connection": "llm-gw",
                                 "base_url": "https://llm.example.com/v1"})
    module = _load(spec, tmp_path, monkeypatch, "sb2_litellm")
    seen = _fake_provider_modules(monkeypatch)
    monkeypatch.setattr(module, "resource_api_key", lambda auth: "sk-gw")
    module.launchpad_builder_kwargs({"model": "bedrock-id", "tools": []})
    assert seen["openai"] == {
        "client_args": {"api_key": "sk-gw", "base_url": "https://llm.example.com/v1"},
        "model_id": "team-gpt", "params": {"max_tokens": 900}}


def test_retry_strategy(tmp_path, monkeypatch):
    module = _load(_spec(retry={"max_attempts": 3, "initial_delay_s": 2, "max_delay_s": 20}),
                   tmp_path, monkeypatch, "sb2_retry")
    captured: dict = {}
    monkeypatch.setattr(module, "Agent", lambda **kw: captured.update(kw) or kw)
    module.build_agent("u", "s")
    strategy = captured["retry_strategy"]
    assert (strategy._max_attempts, strategy._initial_delay, strategy._max_delay) == (3, 2, 20)
    # unset → the SDK default (no keyword at all)
    module = _load(_spec(tool_hooks={"log_tool_calls": True}), tmp_path, monkeypatch,
                   "sb2_noretry")
    captured.clear()
    monkeypatch.setattr(module, "Agent", lambda **kw: captured.update(kw) or kw)
    module.build_agent("u", "s")
    assert "retry_strategy" not in captured and len(captured["hooks"]) == 1


HOOKS = {"approval_tools": ["calc*"], "log_tool_calls": True, "mask_args": ["expression"]}


def _hook_module(tmp_path, monkeypatch, stem, **hooks):
    module = _load(_spec(tool_hooks=hooks or HOOKS), tmp_path, monkeypatch, stem)
    model = ToolModel("calculator", {"expression": "6*7"})
    monkeypatch.setattr(module, "build_model", lambda: model)
    return module, model


def test_approval_tools_are_refused_without_a_console(tmp_path, monkeypatch, capsys):
    module, model = _hook_module(tmp_path, monkeypatch, "sb2_refuse")
    out = _drain(module.invoke({"prompt": "6*7?"}))  # a /v1-style payload
    refused = [e for e in out if e["event"] == "approval_refused"]
    assert refused == [{"event": "approval_refused", "tool": "calculator"}]
    result = model.seen[1]["results"][0]
    assert result["status"] == "error" and "only the Launchpad console" in str(result)
    log = capsys.readouterr().out
    assert '[launchpad-tool] calculator status=error' in log
    assert '"expression": "***"' in log and "6*7" not in log


def test_console_approval_pauses_then_runs_once(tmp_path, monkeypatch):
    module, model = _hook_module(tmp_path, monkeypatch, "sb2_approve")
    out = _drain(module.invoke({"prompt": "6*7?", "tool_approvals": {"interactive": True}}))
    asks = [e for e in out if e["event"] == "approval_required"]
    assert asks == [{"event": "approval_required", "tool": "calculator", "tool_use_id": "t1",
                     "input": {"expression": "***"}}]
    assert "paused" in str(model.seen[1]["results"][0])
    # the next turn carries the approval: the call runs
    module, model = _hook_module(tmp_path, monkeypatch, "sb2_approved")
    out = _drain(module.invoke({"prompt": "go ahead", "tool_approvals": {
        "interactive": True, "approved": ["calculator"]}}))
    assert not [e for e in out if e["event"].startswith("approval")]
    result = model.seen[1]["results"][0]
    assert result["status"] == "success" and "42" in str(result)


def test_per_turn_tool_call_cap(tmp_path, monkeypatch):
    module = _load(_spec(tool_hooks={"max_tool_calls_per_turn": 1}), tmp_path, monkeypatch,
                   "sb2_cap")
    model = ToolModel("calculator", {"expression": "1+1"}, calls=2)
    monkeypatch.setattr(module, "build_model", lambda: model)
    _drain(module.invoke({"prompt": "twice"}))
    first, second = model.seen[2]["results"]
    assert first["status"] == "success"
    assert second["status"] == "error" and "at most 1 tool calls per turn" in str(second)
    # the count starts over every turn
    model.seen.clear()
    _drain(module.invoke({"prompt": "again"}))
    assert model.seen[1]["results"][0]["status"] == "success"


def test_masking():
    module_source = render_main_py(_spec(tool_hooks={"mask_args": ["ssn"]}))
    namespace: dict = {}
    # only the helper, without importing the whole agent
    start = module_source.index("def launchpad_mask(")
    end = module_source.index("def _launchpad_needs_approval(")
    exec(  # noqa: S102 - generated helper under test
        "from typing import Any\n_MASKED_ARGS = frozenset({'password', 'ssn', 'api_key'})\n"
        + module_source[start:end], namespace)
    masked = namespace["launchpad_mask"](
        {"user": {"SSN": "1", "name": "n"}, "Password": "p", "list": [{"api_key": "k"}],
         "long": "x" * 600})
    assert masked["user"] == {"SSN": "***", "name": "n"} and masked["Password"] == "***"
    assert masked["list"] == [{"api_key": "***"}] and masked["long"].endswith("…")


def test_a2a_template_takes_agent_tools_hooks_and_retry(tmp_path, monkeypatch):
    spec = _spec(protocol="a2a", agent_tools=[_resolved()],
                 tool_hooks={"approval_tools": ["ask_helper"]}, retry={"max_attempts": 2})
    source = render_a2a_main_py(spec)
    compile(source, "a2a_main.py", "exec")
    captured: dict = {}
    fake_a2a = types.ModuleType("strands.multiagent.a2a")
    fake_a2a.A2AServer = lambda **kw: types.SimpleNamespace(to_fastapi_app=lambda: None)
    monkeypatch.setitem(sys.modules, "strands.multiagent.a2a", fake_a2a)
    a2a_types = types.ModuleType("a2a.types")
    a2a_types.AgentSkill = dict
    monkeypatch.setitem(sys.modules, "a2a", types.ModuleType("a2a"))
    monkeypatch.setitem(sys.modules, "a2a.types", a2a_types)
    fake_fastapi = types.ModuleType("fastapi")
    fake_fastapi.FastAPI = lambda: types.SimpleNamespace(
        get=lambda path: (lambda fn: fn), mount=lambda *a: None)
    monkeypatch.setitem(sys.modules, "fastapi", fake_fastapi)
    monkeypatch.setitem(sys.modules, "uvicorn", types.ModuleType("uvicorn"))
    monkeypatch.setenv("AWS_REGION", REGION)
    target = tmp_path / "sb2_a2a.py"
    target.write_text(source, encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location("sb2_a2a", target)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "Agent", lambda **kw: captured.update(kw) or kw)
    module.agent_factory("ctx-1")
    assert [t.tool_name for t in captured["tools"]] == [
        "calculator", "current_utc_time", "ask_helper"]
    assert len(captured["hooks"]) == 1 and captured["retry_strategy"]._max_attempts == 2


def test_generated_code_compiles_for_every_sb2_field_alone():
    for over in (
        {"agent_tools": [_resolved()]},
        {"agent_tools": [TOOL]},  # never resolved: rendered without a target
        {"model_provider": {"kind": "anthropic", "connection": "k"}},
        {"model_provider": {"kind": "litellm", "connection": "k",
                            "base_url": "https://gw.example.com"}},
        {"tool_hooks": {"approval_tools": ["x"]}},
        {"tool_hooks": {}},
        {"retry": {}},
        {"tools": [{"type": "rest", "name": "crm", "config": {"url": "https://crm.example"},
                    "auth": {"connection": "c", "kind": "api_key"}}],
         "model_provider": {"kind": "anthropic", "connection": "k"},
         "tool_hooks": {"log_tool_calls": True}},
    ):
        source = render_main_py(_spec(**over))
        compile(source, "main.py", "exec")
        assert source.count("def resource_api_key(") <= 1
    unresolved = render_main_py(_spec(agent_tools=[TOOL]))
    assert "AGENT_TOOL_SPECS: list[dict[str, Any]] = []" in unresolved
