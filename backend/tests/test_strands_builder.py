"""strands-builder SB-1: curated strands tools, model parameters, conversation
strategy, structured output and prompt templates for the zip_runtime (Strands)
method — spec validation, generated code, and the ``structured`` answer path."""

import asyncio
import importlib.metadata
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError
from strands.models.model import Model

import app.routers.public_api as public_api
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer.zip_runtime import _method_requirements, platform_requirements
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.schemas.strands_builder import STRANDS_TOOLS, STRANDS_TOOLS_REQUIREMENT
from app.services.agentcore import runtime
from app.templates.strands_a2a_agent import a2a_base_requirements, render_a2a_main_py
from app.templates.strands_agent import base_requirements, render_main_py
from tests.strands_builder_snapshots import OLD_SPECS, dump, dump_path, fixture_path, render

CLAUDE = "global.anthropic.claude-sonnet-4-5-v1:0"
BASE = {"name": "sb-agent", "method": "zip_runtime", "system_prompt": "Be brief.",
        "model_id": CLAUDE}
HTTP_CONFIG = {"http_request": {"allowed_hosts": ["api.example.com", "*.svc.example.org"]}}
SCHEMA = {
    "type": "object",
    "title": "Weather Report",
    "properties": {
        "city": {"type": "string", "description": "City name"},
        "temp": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string", "enum": ["sun", "rain"]}},
        "where": {"type": "object", "properties": {"lat": {"type": "number"}},
                  "required": ["lat"]},
    },
    "required": ["city"],
}


def _spec(**over) -> AgentSpec:
    return AgentSpec(**{**BASE, **over})


# ── old specs: byte-identical ────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(OLD_SPECS))
def test_old_spec_renders_byte_identical_code(key):
    assert render(OLD_SPECS[key]) == fixture_path(key).read_text(encoding="utf-8")


@pytest.mark.parametrize("key", sorted(OLD_SPECS))
def test_old_spec_stores_without_any_new_field(key):
    assert dump(OLD_SPECS[key]) == dump_path(key).read_text(encoding="utf-8")
    stored = OLD_SPECS[key].model_dump()
    for name in ("strands_tools", "strands_tool_config", "model_params", "conversation",
                 "output_schema", "few_shot_examples", "prompt_variables"):
        assert name not in stored


def test_old_spec_requirements_are_unchanged():
    assert platform_requirements("zip_runtime", "bedrock") == base_requirements()
    assert platform_requirements("zip_runtime", "bedrock", "a2a") == a2a_base_requirements()
    assert STRANDS_TOOLS_REQUIREMENT not in _method_requirements(OLD_SPECS["default"])


def test_old_stored_spec_round_trips_through_the_new_schema():
    stored = json.loads(dump_path("kb_skills").read_text(encoding="utf-8"))
    assert render(AgentSpec(**stored)) == fixture_path("kb_skills").read_text(encoding="utf-8")


# ── spec validation ──────────────────────────────────────────────────────────────


def test_curated_tool_list_is_the_seven_of_d1():
    assert STRANDS_TOOLS == ("current_time", "calculator", "http_request", "think",
                             "retrieve", "image_reader", "journal")


@pytest.mark.parametrize("tool", ["shell", "python_repl", "file_write", "use_aws"])
def test_sandbox_only_tools_are_refused(tool):
    with pytest.raises(ValidationError):
        _spec(strands_tools=[tool])


def test_http_request_without_allowed_hosts_is_refused():
    with pytest.raises(ValidationError, match="allowed_hosts"):
        _spec(strands_tools=["http_request"])
    with pytest.raises(ValidationError, match="allowed_hosts"):
        _spec(strands_tools=["http_request"],
              strands_tool_config={"http_request": {"allowed_hosts": []}})
    spec = _spec(strands_tools=["http_request"], strands_tool_config=HTTP_CONFIG)
    assert spec.strands_tool_config.http_request.allowed_hosts == [
        "api.example.com", "*.svc.example.org"]


@pytest.mark.parametrize("host", ["https://api.example.com", "api.example.com/x",
                                  "api.example.com:443", "*", "a..b"])
def test_allowed_hosts_are_bare_host_names(host):
    with pytest.raises(ValidationError, match="host name"):
        _spec(strands_tools=["http_request"],
              strands_tool_config={"http_request": {"allowed_hosts": [host]}})


def test_http_config_without_the_tool_is_refused():
    with pytest.raises(ValidationError, match="not in strands_tools"):
        _spec(strands_tool_config=HTTP_CONFIG)


def test_retrieve_needs_a_mounted_knowledge_base():
    with pytest.raises(ValidationError, match="knowledge bases"):
        _spec(strands_tools=["retrieve"])
    _spec(strands_tools=["retrieve"], knowledge_bases=[{"kb_id": "KB1234"}])


def test_duplicate_tools_are_refused():
    with pytest.raises(ValidationError, match="unique"):
        _spec(strands_tools=["calculator", "calculator"])


@pytest.mark.parametrize("field,value", [
    ("strands_tools", ["current_time"]),
    ("model_params", {"temperature": 0.2}),
    ("conversation", {"strategy": "sliding_window", "window_size": 10}),
    ("output_schema", {"type": "object", "properties": {"a": {"type": "string"}}}),
    ("few_shot_examples", [{"question": "q", "answer": "a"}]),
    ("prompt_variables", {"x": "1"}),
])
def test_builder_fields_are_strands_only(field, value):
    with pytest.raises(ValidationError, match="zip_runtime"):
        AgentSpec(name="sb-harness", method="harness", system_prompt="p", **{field: value})
    with pytest.raises(ValidationError, match="code/code_bundle"):
        _spec(code_bundle={"main.py": "print(1)"}, **{field: value})


def test_a2a_refuses_output_schema_but_takes_the_rest():
    with pytest.raises(ValidationError, match="A2A"):
        _spec(protocol="a2a", output_schema=SCHEMA)
    _spec(protocol="a2a", strands_tools=["current_time"], model_params={"top_p": 0.9},
          conversation={"window_size": 20}, prompt_variables={"team": "ops"})


def test_extended_thinking_rules():
    _spec(model_params={"thinking_budget_tokens": 2048, "max_tokens": 8000})
    with pytest.raises(ValidationError, match="Claude"):
        _spec(model_id="us.amazon.nova-pro-v1:0", model_params={"thinking_budget_tokens": 2048})
    with pytest.raises(ValidationError, match="temperature"):
        _spec(model_params={"thinking_budget_tokens": 2048, "temperature": 0.5})
    with pytest.raises(ValidationError, match="top_p"):
        _spec(model_params={"thinking_budget_tokens": 2048, "top_p": 0.5})
    with pytest.raises(ValidationError, match="larger"):
        _spec(model_params={"thinking_budget_tokens": 4096, "max_tokens": 4096})
    with pytest.raises(ValidationError):
        _spec(model_params={"thinking_budget_tokens": 512})


def test_claude_takes_temperature_or_top_p_not_both():
    with pytest.raises(ValidationError, match="not both"):
        _spec(model_params={"temperature": 0.3, "top_p": 0.9})
    _spec(model_id="us.amazon.nova-pro-v1:0", model_params={"temperature": 0.3, "top_p": 0.9})


def test_mantle_refuses_converse_only_params():
    mantle = {"model_source": "mantle", "model_id": "openai.gpt-5.6-sol"}
    _spec(**mantle, model_params={"temperature": 0.4, "max_tokens": 1000})
    for params in ({"stop_sequences": ["X"]}, {"prompt_caching": True}):
        with pytest.raises(ValidationError, match="mantle"):
            _spec(**mantle, model_params=params)
    with pytest.raises(ValidationError, match="mantle"):
        _spec(**mantle, conversation={"strategy": "summarizing", "summary_model": "x.y"})


def test_summary_model_needs_the_summarizing_strategy():
    with pytest.raises(ValidationError, match="summarizing"):
        _spec(conversation={"strategy": "sliding_window",
                            "summary_model": "us.amazon.nova-lite-v1:0"})


@pytest.mark.parametrize("schema,match", [
    ({"type": "string"}, "object"),
    ({"type": "object", "properties": {}}, "at least one"),
    ({"type": "object", "properties": {"bad-name": {"type": "string"}}}, "must match"),
    ({"type": "object", "properties": {"a": {"type": "date"}}}, "type must be"),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]}, "required"),
    ({"type": "object", "properties": {"a": {"type": "array", "items": {"type": "array"}}}},
     "items"),
    ({"type": "object", "properties": {"a": {"type": "string", "enum": []}}}, "enum"),
])
def test_output_schema_is_validated(schema, match):
    with pytest.raises(ValidationError, match=match):
        _spec(output_schema=schema)


def test_output_schema_depth_and_guardrail():
    node: dict = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for _ in range(6):
        node = {"type": "object", "properties": {"child": node}}
    with pytest.raises(ValidationError, match="deeper"):
        _spec(output_schema=node)
    with pytest.raises(ValidationError, match="guardrail"):
        _spec(output_schema=SCHEMA, guardrail={"enabled": True})


def test_prompt_variable_names_are_identifiers():
    with pytest.raises(ValidationError, match="prompt variable"):
        _spec(prompt_variables={"not ok": "x"})


# ── requirements ─────────────────────────────────────────────────────────────────


def test_strands_tools_add_the_pinned_tools_package():
    spec = _spec(strands_tools=["current_time"])
    assert _method_requirements(spec) == base_requirements() + [STRANDS_TOOLS_REQUIREMENT]
    a2a = _spec(protocol="a2a", strands_tools=["current_time"])
    assert _method_requirements(a2a) == a2a_base_requirements() + [STRANDS_TOOLS_REQUIREMENT]
    # other builder fields need nothing beyond strands-agents itself
    assert _method_requirements(_spec(model_params={"temperature": 0})) == base_requirements()


def test_pinned_tools_version_is_the_one_verified_with_the_strands_pin():
    """The backend's lock resolves the same pair the template pins; importing all
    seven curated tools against it is the compatibility check."""
    assert STRANDS_TOOLS_REQUIREMENT == (
        f"strands-agents-tools=={importlib.metadata.version('strands-agents-tools')}"
    )
    strands_pin = next(r for r in base_requirements() if r.startswith("strands-agents"))
    assert strands_pin.endswith(f"=={importlib.metadata.version('strands-agents')}")
    for name in STRANDS_TOOLS:
        module = importlib.import_module(f"strands_tools.{name}")
        assert hasattr(module, name)


# ── generated code ───────────────────────────────────────────────────────────────


class FakeModel(Model):
    """Answers with text; when the structured-output tool is offered it also calls
    it once with ``answer`` — enough to drive one real Strands turn locally."""

    def __init__(self, answer: dict | None = None):
        self.config: dict = {}
        self.calls: list[dict] = []
        self.answer = answer

    def update_config(self, **kw):
        self.config.update(kw)

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):  # pragma: no cover - unused path
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        names = [t["name"] for t in tool_specs or []]
        self.calls.append({"tools": names, "system": system_prompt})
        answered = any(
            "toolResult" in block for m in messages if m["role"] == "user" for block in m["content"]
        )
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "It is sunny."}}}
        yield {"contentBlockStop": {}}
        if self.answer is not None and not answered:
            tool = names[-1]
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": tool}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(self.answer)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"messageStop": {"stopReason": "end_turn"}}


def _load(spec: AgentSpec, tmp_path: Path, monkeypatch, stem: str = "sb_main"):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
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


def _bundle(module, monkeypatch, bundle: dict):
    monkeypatch.setattr(
        module.BedrockAgentCoreContext, "get_config_bundle", staticmethod(lambda: bundle)
    )


def test_strands_tools_replace_the_template_tools(tmp_path, monkeypatch):
    module = _load(_spec(strands_tools=["current_time", "calculator", "think", "image_reader",
                                        "journal", "http_request"],
                         strands_tool_config=HTTP_CONFIG), tmp_path, monkeypatch)
    agent = module.build_agent("u", "s")
    assert sorted(agent.tool_registry.registry) == [
        "calculator", "current_time", "http_request", "image_reader", "journal", "think"]
    # the SDK's own descriptions stand (the template's calculator text does not leak in)
    spec = agent.tool_registry.registry["calculator"].tool_spec
    assert spec["description"] != module.calculator.tool_spec["description"]
    assert module.DEFAULT_TOOL_DESCRIPTIONS["current_time"].startswith("Get the current time")


def test_strands_tool_descriptions_are_bundle_tunable(tmp_path, monkeypatch):
    module = _load(_spec(strands_tools=["current_time"]), tmp_path, monkeypatch)
    _bundle(module, monkeypatch, {"tool_descriptions": {"current_time": "Tell the time."}})
    agent = module.build_agent("u", "s")
    assert agent.tool_registry.registry["current_time"].tool_spec["description"] == "Tell the time."
    # the next request without a bundle gets the SDK description back
    _bundle(module, monkeypatch, {})
    agent = module.build_agent("u", "s")
    assert agent.tool_registry.registry["current_time"].tool_spec["description"].startswith(
        "Get the current time")


def test_promoted_override_beats_the_sdk_description(tmp_path, monkeypatch):
    module = _load(_spec(strands_tools=["journal"],
                         tool_description_overrides={"journal": "Keep notes."}),
                   tmp_path, monkeypatch)
    assert module.DEFAULT_TOOL_DESCRIPTIONS["journal"] == "Keep notes."
    agent = module.build_agent("u", "s")
    assert agent.tool_registry.registry["journal"].tool_spec["description"] == "Keep notes."


def test_http_request_refuses_hosts_off_the_list(tmp_path, monkeypatch):
    module = _load(_spec(strands_tools=["http_request"], strands_tool_config=HTTP_CONFIG),
                   tmp_path, monkeypatch)
    calls: list = []
    impl = types.SimpleNamespace(http_request=lambda tool: calls.append(tool) or {
        "content": [{"text": "Status Code: 200"}]})
    monkeypatch.setitem(sys.modules, "strands_tools.http_request", impl)
    monkeypatch.delenv("BYPASS_TOOL_CONSENT", raising=False)
    fn = module.launchpad_http_request
    for url in ("https://evil.example.net/", "https://example.org/", "ftp://api.example.com/",
                "https://api.example.com.evil.net/"):
        assert fn("GET", url).startswith("refused"), url
    assert calls == []
    assert fn("post", "https://api.example.com/v1", body="{}") == "Status Code: 200"
    assert fn("GET", "https://a.svc.example.org/x") == "Status Code: 200"
    first = calls[0]["input"]
    assert first["method"] == "POST" and first["allow_redirects"] is False
    assert "auth_type" not in first and "aws_auth" not in first


def test_retrieve_is_confined_to_the_mounted_kbs(tmp_path, monkeypatch):
    module = _load(_spec(strands_tools=["retrieve"],
                         knowledge_bases=[{"kb_id": "KB1111", "name": "One"},
                                          {"kb_id": "KB2222"}]), tmp_path, monkeypatch)
    calls: list = []
    impl = types.SimpleNamespace(retrieve=lambda tool: calls.append(tool) or {
        "content": [{"text": "passage"}]})
    monkeypatch.setitem(sys.modules, "strands_tools.retrieve", impl)
    assert module.launchpad_retrieve("q", "KB9999").startswith("unknown knowledge base")
    assert module.launchpad_retrieve("q") == "passage"
    assert module.launchpad_retrieve("q", "KB2222", 99) == "passage"
    assert [c["input"]["knowledgeBaseId"] for c in calls] == ["KB1111", "KB2222"]
    assert calls[1]["input"]["numberOfResults"] == 20


def test_journal_writes_under_session_storage(tmp_path, monkeypatch):
    module = _load(_spec(strands_tools=["journal"]), tmp_path, monkeypatch)
    assert module.JOURNAL_ROOT == "/mnt/workspace"
    monkeypatch.setattr(module, "JOURNAL_ROOT", str(tmp_path))
    journal = sys.modules["strands_tools.journal"]
    assert journal.ensure_journal_dir() == tmp_path / "journal"
    module = _load(_spec(strands_tools=["journal"], filesystem={"session_storage": None}),
                   tmp_path, monkeypatch, stem="sb_journal_tmp")
    assert module.JOURNAL_ROOT == "/tmp"


def test_model_params_build_a_bedrock_model(tmp_path, monkeypatch):
    module = _load(_spec(model_params={"temperature": 0, "max_tokens": 2000,
                                       "stop_sequences": ["END"], "prompt_caching": True}),
                   tmp_path, monkeypatch)
    agent = module.build_agent("u", "s")
    config = agent.model.config
    assert config["temperature"] == 0  # zero is a setting, not "unset"
    assert config["max_tokens"] == 2000 and config["stop_sequences"] == ["END"]
    assert config["cache_config"].strategy == "auto"
    assert "additional_request_fields" not in config


def test_thinking_budget_rides_additional_request_fields(tmp_path, monkeypatch):
    module = _load(_spec(model_params={"thinking_budget_tokens": 2048}), tmp_path, monkeypatch)
    config = module.build_agent("u", "s").model.config
    assert config["additional_request_fields"] == {
        "thinking": {"type": "enabled", "budget_tokens": 2048}}
    assert config["max_tokens"] == 2048 + 4096


def test_model_params_come_from_the_bundle_too(tmp_path, monkeypatch):
    module = _load(_spec(model_params={"temperature": 0.2}), tmp_path, monkeypatch)
    _bundle(module, monkeypatch, {"model_params": {"temperature": 0.9}})
    assert module.build_agent("u", "s").model.config["temperature"] == 0.9


def test_mantle_params_map_onto_the_responses_api(tmp_path, monkeypatch):
    module = _load(_spec(model_source="mantle", model_id="openai.gpt-5.6-sol",
                         model_params={"temperature": 0.4, "max_tokens": 900}),
                   tmp_path, monkeypatch)
    seen: dict = {}

    class FakeResponses:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    fake = types.ModuleType("strands.models.openai_responses")
    fake.OpenAIResponsesModel = FakeResponses
    monkeypatch.setitem(sys.modules, "strands.models.openai_responses", fake)
    monkeypatch.setattr(module, "_launchpad_mantle_model_class", lambda base: base)
    module.launchpad_model({"temperature": 0.4, "max_tokens": 900})
    assert seen["params"] == {"temperature": 0.4, "max_output_tokens": 900}
    assert seen["model_id"] == "openai.gpt-5.6-sol"


def test_conversation_strategies(tmp_path, monkeypatch):
    from strands.agent.conversation_manager import (
        SlidingWindowConversationManager,
        SummarizingConversationManager,
    )

    module = _load(_spec(conversation={"window_size": 12}), tmp_path, monkeypatch)
    manager = module.build_agent("u", "s").conversation_manager
    assert isinstance(manager, SlidingWindowConversationManager)
    assert manager.window_size == 12
    module = _load(_spec(conversation={"strategy": "summarizing", "window_size": 8,
                                       "summary_model": "us.amazon.nova-lite-v1:0"}),
                   tmp_path, monkeypatch, stem="sb_summary")
    manager = module.build_agent("u", "s").conversation_manager
    assert isinstance(manager, SummarizingConversationManager)
    assert manager.preserve_recent_messages == 8
    assert manager.summarization_agent.model.config["model_id"] == "us.amazon.nova-lite-v1:0"


def test_prompt_variables_resolve_context_then_env_then_default(tmp_path, monkeypatch):
    module = _load(_spec(system_prompt="City {{city}}, team {{ team }}, keep {{other}}.",
                         prompt_variables={"city": "Rome", "team": "core"}),
                   tmp_path, monkeypatch)
    monkeypatch.delenv("city", raising=False)
    monkeypatch.delenv("team", raising=False)
    assert module.resolve_system_prompt() == "City Rome, team core, keep {{other}}."
    monkeypatch.setenv("team", "ops")
    assert module.resolve_system_prompt() == "City Rome, team ops, keep {{other}}."
    token = module._LAUNCHPAD_PROMPT_CONTEXT.set({"city": "Paris", "other": "x"})
    try:
        # context wins, and only for declared variables
        assert module.resolve_system_prompt() == "City Paris, team ops, keep {{other}}."
    finally:
        module._LAUNCHPAD_PROMPT_CONTEXT.reset(token)


def test_few_shot_examples_and_bundle_prompt_contract(tmp_path, monkeypatch):
    module = _load(_spec(system_prompt="Hi {{who}}.", prompt_variables={"who": "you"},
                         few_shot_examples=[{"question": "2+2?", "answer": "4"}]),
                   tmp_path, monkeypatch)
    prompt = module.resolve_system_prompt()
    assert prompt.startswith("Hi you.\n\n## Examples")
    assert "User: 2+2?\nAssistant: 4" in prompt
    # the config bundle still overrides the prompt, and the template applies on top
    _bundle(module, monkeypatch, {"system_prompt": "Yo {{who}}.",
                                  "prompt_variables": {"who": "team"},
                                  "few_shot_examples": []})
    assert module.resolve_system_prompt() == "Yo team."


def test_output_schema_becomes_a_pydantic_model(tmp_path, monkeypatch):
    module = _load(_spec(output_schema=SCHEMA), tmp_path, monkeypatch)
    model = module.OUTPUT_MODEL
    assert model.__name__ == "WeatherReport"
    value = model(city="Oslo", tags=["sun"], where={"lat": 59.9})
    assert value.model_dump() == {"city": "Oslo", "temp": None, "tags": ["sun"],
                                  "where": {"lat": 59.9}}
    with pytest.raises(ValidationError):
        model(temp=3)  # city is required
    with pytest.raises(ValidationError):
        model(city="Oslo", tags=["snow"])  # enum
    assert module.build_agent("u", "s")._default_structured_output_model is model


def test_one_turn_with_a_fake_model_returns_text_and_structured(tmp_path, monkeypatch):
    """The design's local run: tools + structured output + a template variable from
    the invoke context, through the real Strands loop with a fake model."""
    module = _load(_spec(system_prompt="Weather for {{city}}", prompt_variables={"city": "Rome"},
                         strands_tools=["current_time", "calculator"],
                         output_schema=SCHEMA), tmp_path, monkeypatch)
    fake = FakeModel(answer={"city": "Paris", "temp": 21})
    monkeypatch.setattr(module, "build_model", lambda: fake)
    out = _drain(module.invoke({"prompt": "weather?", "context": {"city": "Paris"}}))
    assert out[0] == {"event": "delta", "text": "It is sunny."}
    assert out[-1] == {
        "event": "complete", "result": "It is sunny.",
        "structured": {"city": "Paris", "temp": 21.0, "tags": None, "where": None},
    }
    assert fake.calls[0]["system"] == "Weather for Paris"
    assert fake.calls[0]["tools"] == ["current_time", "calculator", "WeatherReport"]


def test_one_turn_without_schema_keeps_the_plain_complete_event(tmp_path, monkeypatch):
    module = _load(_spec(model_params={"top_p": 0.8}), tmp_path, monkeypatch)
    fake = FakeModel()
    monkeypatch.setattr(module, "launchpad_model", lambda params: fake)
    out = _drain(module.invoke({"prompt": "hi"}))
    assert out[-1] == {"event": "complete", "result": "It is sunny.\n"}
    # no strands tools selected → the template's own two tools
    assert fake.calls[0]["tools"] == ["calculator", "current_utc_time"]


def test_a2a_template_renders_the_builder_options(tmp_path, monkeypatch):
    spec = _spec(protocol="a2a", system_prompt="Team {{team}}", prompt_variables={"team": "ops"},
                 strands_tools=["current_time"], model_params={"temperature": 0.1},
                 conversation={"window_size": 6})
    source = render_a2a_main_py(spec)
    compile(source, "a2a_main.py", "exec")
    assert '"tools": list(STRANDS_TOOLS) or [calculator, current_utc_time],' in source
    # run agent_factory without the A2A server: stub the server and capture Agent kwargs
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
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    target = tmp_path / "a2a_main.py"
    target.write_text(source, encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location("a2a_main", target)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "Agent", lambda **kw: captured.update(kw) or kw)
    module.agent_factory("ctx-1")
    assert captured["system_prompt"] == "Team ops"
    assert [t.tool_name for t in captured["tools"]] == ["current_time"]
    assert captured["model"].config["temperature"] == 0.1
    assert captured["conversation_manager"].window_size == 6


def test_generated_code_compiles_for_every_field_alone(tmp_path):
    for over in (
        {"strands_tools": list(t for t in STRANDS_TOOLS if t not in ("http_request", "retrieve"))},
        {"strands_tools": ["http_request"], "strands_tool_config": HTTP_CONFIG},
        {"strands_tools": ["retrieve"], "knowledge_bases": [{"kb_id": "KB1"}]},
        {"model_params": {"max_tokens": 10}},
        {"conversation": {"strategy": "summarizing"}},
        {"output_schema": SCHEMA},
        {"few_shot_examples": [{"question": "q", "answer": "a"}]},
        {"prompt_variables": {"a": "b"}},
        {"toolkits": ["hr_assistant"], "strands_tools": ["calculator"]},
    ):
        compile(render_main_py(_spec(**over)), "main.py", "exec")


# ── platform: structured answer ──────────────────────────────────────────────────


def test_runtime_stream_carries_the_structured_answer():
    payloads = [
        {"event": "delta", "text": "Sunny."},
        {"event": "complete", "result": "Sunny.", "structured": {"city": "Oslo"}},
    ]
    events = list(runtime._normalized_runtime_events(payloads, require_text=True))
    assert events == [
        {"event": "delta", "data": {"text": "Sunny."}},
        {"event": "structured", "data": {"value": {"city": "Oslo"}}},
    ]
    # no deltas: the complete text becomes the reply, structured still follows
    events = list(runtime._normalized_runtime_events(payloads[1:]))
    assert events[0] == {"event": "delta", "data": {"text": "Sunny."}}
    assert events[1]["event"] == "structured"
    # a non-object `structured` is not passed through
    assert [e["event"] for e in runtime._normalized_runtime_events(
        [{"event": "complete", "result": "x", "structured": "nope"}])] == ["delta"]


def test_invoke_runtime_text_returns_structured(monkeypatch):
    def fake_stream(*args, **kwargs):
        yield {"event": "delta", "data": {"text": "Sunny."}}
        yield {"event": "structured", "data": {"value": {"city": "Oslo"}}}

    monkeypatch.setattr(runtime, "stream_runtime_events", fake_stream)
    result = runtime.invoke_runtime_text(object(), "arn", "hi", session_id="s" * 40)
    assert result["text"] == "Sunny." and result["structured"] == {"city": "Oslo"}
    monkeypatch.setattr(runtime, "stream_runtime_events",
                        lambda *a, **k: iter([{"event": "delta", "data": {"text": "x"}}]))
    assert "structured" not in runtime.invoke_runtime_text(object(), "arn", "hi")


def _v1_agent(name: str) -> str:
    db = SessionLocal()
    try:
        row = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="harness", status="active",
            arn=f"arn:aws:bedrock-agentcore:us-west-2:111:harness/{name}",
            spec={"name": name, "method": "harness", "system_prompt": "x"},
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


@pytest.mark.parametrize("structured", [{"city": "Oslo"}, None])
def test_v1_invoke_adds_structured_and_keeps_text(client, monkeypatch, structured):
    result = {"text": "Sunny.", "session_id": "s" * 40}
    if structured is not None:
        result["structured"] = structured
    monkeypatch.setattr(public_api, "invoke_agent_text", lambda agent, prompt, **_kw: dict(result))
    agent_id = _v1_agent(f"sb-v1-{'s' if structured else 'n'}")
    key = client.post("/api/apikeys", json={"name": "k"}).json()["key"]
    res = client.post(f"/v1/agents/{agent_id}/invoke", headers={"X-Api-Key": key},
                      json={"prompt": "hi"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["text"] == "Sunny."
    if structured is None:
        assert "structured" not in body
    else:
        assert body["structured"] == structured
