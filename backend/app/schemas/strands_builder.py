"""Strands builder options (design strands-builder §2 SB-1) — the optional
``AgentSpec`` fields that only the generated Strands code honours.

Every field here is optional and excluded from the stored spec while unset, so a
spec written before SB-1 serialises — and renders — exactly as it did. The
generated code reads the same keys from the config bundle at request time
(``model_params`` / ``conversation`` / ``few_shot_examples`` /
``prompt_variables``), so an A/B experiment can compare them.
"""

import json
import re
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

# The first batch of ``strands-agents-tools`` the wizard offers (D-1): no side
# effects outside the runtime, or side effects the spec bounds. shell /
# python_repl / file_write / use_aws need a sandbox and wait for SB-3.
StrandsTool = Literal[
    "current_time", "calculator", "http_request", "think", "retrieve",
    "image_reader", "journal",
]
STRANDS_TOOLS: tuple[str, ...] = get_args(StrandsTool)

# ``strands-agents-tools`` pin, verified with the template's strands-agents==1.47.0
# (the backend's own lock resolves the same pair through bedrock-agentcore[simulation]).
STRANDS_TOOLS_REQUIREMENT = "strands-agents-tools==0.8.3"

# a hostname, or "*." + hostname for its subdomains; no scheme, port or path
_HOST_RE = re.compile(
    r"^(\*\.)?(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
PROMPT_VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SCHEMA_PROPERTY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
OUTPUT_SCHEMA_MAX_CHARS = 20000
OUTPUT_SCHEMA_MAX_DEPTH = 5
_SCHEMA_SCALARS = {"string", "integer", "number", "boolean"}


class HttpRequestToolConfig(BaseModel):
    # Empty = the tool is refused (D-2): outbound HTTP is opt-in per host.
    allowed_hosts: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, hosts: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(h.strip().lower().rstrip(".") for h in hosts))
        for host in cleaned:
            if not _HOST_RE.match(host):
                raise ValueError(
                    f"http_request allowed host {host!r} must be a host name such as "
                    "api.example.com or *.example.com — no scheme, port or path"
                )
        return cleaned


class StrandsToolConfig(BaseModel):
    http_request: HttpRequestToolConfig | None = None


class ModelParams(BaseModel):
    """Per-model-call inference parameters of the generated Strands agent."""

    temperature: float | None = Field(default=None, ge=0, le=1)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    stop_sequences: list[str] = Field(default_factory=list, max_length=4)
    # Claude extended thinking (Bedrock ``additionalModelRequestFields.thinking``)
    thinking_budget_tokens: int | None = Field(default=None, ge=1024, le=131072)
    # Bedrock prompt caching — the SDK's automatic cache point, which covers the
    # system prompt and tool definitions as the cached prefix
    prompt_caching: bool = False

    @field_validator("stop_sequences")
    @classmethod
    def _stops(cls, stops: list[str]) -> list[str]:
        for stop in stops:
            if not 1 <= len(stop) <= 100:
                raise ValueError("stop sequences must be 1–100 characters")
        return stops

    def is_empty(self) -> bool:
        return self == ModelParams()


class ConversationConfig(BaseModel):
    # sliding_window 40 is what every agent gets today (the SDK default)
    strategy: Literal["sliding_window", "summarizing"] = "sliding_window"
    # sliding_window: messages kept; summarizing: recent messages kept verbatim
    window_size: int = Field(default=40, ge=2, le=1000)
    # summarizing only: a cheaper Bedrock model for the summaries (default: the agent's)
    summary_model: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _summary_model_needs_summarizing(self) -> "ConversationConfig":
        if self.summary_model and self.strategy != "summarizing":
            raise ValueError("conversation.summary_model applies to strategy 'summarizing' only")
        return self


class FewShotExample(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=8000)


def is_claude_model_id(model_id: str) -> bool:
    return "anthropic.claude" in model_id or "claude-" in model_id.lower()


def validate_output_schema(schema: dict[str, Any]) -> None:
    """The JSON Schema subset the generated code turns into a pydantic model.

    An object with named properties; each property a scalar (string / integer /
    number / boolean, optionally an ``enum``), an array of one of those, or a
    nested object of the same shape. Refused here, not at first invoke.
    """
    if len(json.dumps(schema)) > OUTPUT_SCHEMA_MAX_CHARS:
        raise ValueError(f"output_schema exceeds {OUTPUT_SCHEMA_MAX_CHARS} characters")
    _check_object(schema, "output_schema", 1)


def _check_object(node: Any, path: str, depth: int) -> None:
    if not isinstance(node, dict) or node.get("type") != "object":
        raise ValueError(f"{path} must be a JSON Schema object (\"type\": \"object\")")
    if depth > OUTPUT_SCHEMA_MAX_DEPTH:
        raise ValueError(f"output_schema nests deeper than {OUTPUT_SCHEMA_MAX_DEPTH} objects")
    props = node.get("properties")
    if not isinstance(props, dict) or not props:
        raise ValueError(f"{path} must declare at least one property")
    if len(props) > 50:
        raise ValueError(f"{path} declares more than 50 properties")
    for name, child in props.items():
        if not _SCHEMA_PROPERTY_RE.match(name):
            raise ValueError(
                f"{path} property {name!r} must match {_SCHEMA_PROPERTY_RE.pattern}"
            )
        _check_value(child, f"{path}.{name}", depth)
    required = node.get("required", [])
    if not isinstance(required, list) or any(r not in props for r in required):
        raise ValueError(f"{path}.required must list declared properties only")
    if "description" in node and not isinstance(node["description"], str):
        raise ValueError(f"{path}.description must be a string")


def _check_value(node: Any, path: str, depth: int) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{path} must be a JSON Schema")
    if "description" in node and not isinstance(node["description"], str):
        raise ValueError(f"{path}.description must be a string")
    kind = node.get("type")
    if kind == "object":
        _check_object(node, path, depth + 1)
        return
    if kind == "array":
        items = node.get("items")
        if not isinstance(items, dict) or items.get("type") == "array":
            raise ValueError(f"{path}.items must be a scalar or object schema")
        _check_value(items, f"{path}[]", depth)
        return
    if kind not in _SCHEMA_SCALARS:
        raise ValueError(
            f"{path}.type must be one of string, integer, number, boolean, array, object"
        )
    if "enum" in node:
        enum = node["enum"]
        if (
            not isinstance(enum, list)
            or not enum
            or len(enum) > 100
            or any(not isinstance(v, (str, int, float, bool)) for v in enum)
        ):
            raise ValueError(f"{path}.enum must be a non-empty list of scalars")


def check_builder_fields(spec: Any) -> None:
    """Where the SB-1 fields may sit, and what each needs. ``spec`` is the
    ``AgentSpec`` (typed loosely to keep this module free of the import cycle)."""
    used = [
        name
        for name in (
            "strands_tools", "strands_tool_config", "model_params", "conversation",
            "output_schema", "few_shot_examples", "prompt_variables",
        )
        if getattr(spec, name)
    ]
    if not used:
        return
    if spec.method != "zip_runtime":
        raise ValueError(
            f"{', '.join(used)} apply to the Strands method (zip_runtime) only — the "
            "other methods do not render the Strands template"
        )
    if spec.code or spec.code_bundle:
        raise ValueError(
            f"{', '.join(used)} cannot be combined with code/code_bundle — those specs "
            "deploy their own source and never render the Strands template"
        )
    if spec.protocol == "a2a" and spec.output_schema:
        raise ValueError(
            "output_schema is not supported by protocol=a2a runtimes — the A2A "
            "server answers in A2A message parts"
        )
    if spec.output_schema and spec.guardrail.enabled:
        raise ValueError(
            "output_schema cannot be combined with PII protection (guardrail) yet — the "
            "guardrail screens the text answer, and the structured answer would bypass it"
        )
    tools = list(spec.strands_tools)
    if len(tools) != len(set(tools)):
        raise ValueError("strands_tools must be unique")
    http = spec.strands_tool_config.http_request if spec.strands_tool_config else None
    if "http_request" in tools and not (http and http.allowed_hosts):
        raise ValueError(
            "the http_request tool needs strands_tool_config.http_request.allowed_hosts "
            "— with no allowed host it would reach nothing, so it is refused"
        )
    if http is not None and "http_request" not in tools:
        raise ValueError(
            "strands_tool_config.http_request is set but http_request is not in strands_tools"
        )
    if "retrieve" in tools and not spec.knowledge_bases:
        raise ValueError(
            "the retrieve tool searches the agent's mounted knowledge bases — mount one first"
        )
    _check_model_params(spec)
    conv = spec.conversation
    if conv is not None and conv.summary_model and spec.model_source != "bedrock":
        raise ValueError(
            "conversation.summary_model is a Bedrock (Converse) model id — not "
            "supported for model_source='mantle'"
        )
    if spec.output_schema is not None:
        validate_output_schema(spec.output_schema)
    for name, value in spec.prompt_variables.items():
        if not PROMPT_VARIABLE_RE.match(name):
            raise ValueError(
                f"prompt variable {name!r} must match {PROMPT_VARIABLE_RE.pattern}"
            )
        if len(value) > 2000:
            raise ValueError(f"prompt variable {name!r} default exceeds 2000 characters")


def _check_model_params(spec: Any) -> None:
    params = spec.model_params
    if params is None:
        return
    mantle = spec.model_source == "mantle" and spec.protocol != "a2a"
    if mantle and (params.stop_sequences or params.thinking_budget_tokens or params.prompt_caching):
        raise ValueError(
            "stop_sequences, thinking_budget_tokens and prompt_caching are Bedrock "
            "Converse options — not supported for model_source='mantle'"
        )
    claude = is_claude_model_id(spec.model_id)
    if params.thinking_budget_tokens is not None:
        if not claude:
            raise ValueError(
                f"thinking_budget_tokens (extended thinking) applies to Claude models only; "
                f"{spec.model_id!r} is not a Claude model id"
            )
        if params.temperature is not None:
            raise ValueError("extended thinking cannot be combined with a temperature")
        if params.top_p is not None and params.top_p < 0.95:
            raise ValueError("extended thinking needs top_p ≥ 0.95 (or no top_p)")
        if params.max_tokens is not None and params.max_tokens <= params.thinking_budget_tokens:
            raise ValueError("max_tokens must be larger than thinking_budget_tokens")
    if claude and params.temperature is not None and params.top_p is not None:
        raise ValueError(
            "Claude models accept temperature or top_p, not both — set one of them"
        )
