"""Strands builder options (strands-builder SB-1) → generated code.

Both Strands templates carry three marker LINES — the builder source, a hook
inside the agent factory and (HTTP only) a per-turn hook. For a spec that sets
none of the SB-1 fields each marker line is removed whole, so the rendered code
is byte-identical to what the spec produced before SB-1 (pinned by
``tests/test_strands_builder.py`` against ``tests/fixtures/strands_builder``).
Two template lines are swapped instead of marked, because a marker could not
collapse back to them: the tool-list line and the ``complete`` event line.

SB-2 (agents-as-tools, model providers, tool hooks, retry) rides the same markers:
its source (``builder_sb2.py.tmpl``) follows the SB-1 source and wraps
``launchpad_builder_kwargs``, and its turn line joins the turn hook — both only
for a spec that sets an SB-2 field, so an SB-1 spec renders byte-identically too
(``tests/test_strands_builder_sb2.py`` against ``tests/fixtures/strands_builder_sb2``).

SB-3 (custom ``@tool`` functions, sandboxed high-risk tools) follows the SB-2 source
(``builder_sb3.py.tmpl``) and extends ``STRANDS_TOOLS``; the custom tools' source
files are not inlined but packaged next to ``main.py`` (``custom_tool_files``). An
SB-2 spec renders byte-identically (``tests/test_strands_builder_sb3.py`` against
``tests/fixtures/strands_builder_sb3``).
"""

from pathlib import Path

from app.schemas.agent import AgentSpec
from app.schemas.custom_tool_code import tool_signature
from app.schemas.strands_builder import DEFAULT_CODE_INTERPRETER_ID, SB2_FIELDS, SB3_FIELDS

BUILDER_TEMPLATE = Path(__file__).with_name("builder_support.py.tmpl")
SB2_TEMPLATE = Path(__file__).with_name("builder_sb2.py.tmpl")
SB3_TEMPLATE = Path(__file__).with_name("builder_sb3.py.tmpl")
# where the package stage drops each custom tool's source, next to main.py
CUSTOM_TOOLS_DIRNAME = "custom_tools"
SOURCE_MARKER = "__LAUNCHPAD_STRANDS_BUILDER_SOURCE__\n"
BUILD_HOOK_MARKER = "    __LAUNCHPAD_BUILD_AGENT_HOOK__\n"
TURN_HOOK_MARKER = "    __LAUNCHPAD_TURN_HOOK__\n"

HTTP_TOOLS_LINE = "    tools = list(TOOLKIT_TOOLS) or [calculator, current_utc_time]\n"
HTTP_TOOLS_LINE_BUILDER = (
    "    tools = list(TOOLKIT_TOOLS) + list(STRANDS_TOOLS) or [calculator, current_utc_time]\n"
)
HTTP_COMPLETE_LINE = (
    '    yield {"event": "complete", "result": str(result) if result is not None else ""}\n'
)
HTTP_COMPLETE_LINE_BUILDER = "    yield launchpad_complete_event(result)\n"
A2A_TOOLS_LINE = '        "tools": [calculator, current_utc_time],\n'
A2A_TOOLS_LINE_BUILDER = '        "tools": list(STRANDS_TOOLS) or [calculator, current_utc_time],\n'

# The A2A template has no config bundle, mantle branch or mounted KBs; the
# builder source reads all three, so it gets inert stand-ins.
_A2A_PRELUDE = '''from typing import Any

MODEL_SOURCE = "bedrock"
MANTLE_REGION = ""
MOUNTED_KBS: list[dict[str, str]] = []
DEFAULT_TOOL_DESCRIPTIONS: dict[str, str] = {}


def _config_bundle() -> dict[str, Any]:
    """A2A agents are excluded from config-bundle experiments."""
    return {}


def resolve_system_prompt() -> str:
    return SYSTEM_PROMPT


def build_model() -> Any:
    return MODEL_ID


'''

BUILDER_FIELDS = (
    "strands_tools", "strands_tool_config", "model_params", "conversation",
    "output_schema", "few_shot_examples", "prompt_variables", *SB2_FIELDS, *SB3_FIELDS,
)
# a turn of an agent-tool call waits at most this long for the other agent
AGENT_TOOL_TIMEOUT_CEILING_S = 900


def uses_builder(spec: AgentSpec) -> bool:
    return any(getattr(spec, name) for name in BUILDER_FIELDS)


def uses_sb2(spec: AgentSpec) -> bool:
    return any(getattr(spec, name) for name in SB2_FIELDS)


def uses_sb3(spec: AgentSpec) -> bool:
    return any(getattr(spec, name) for name in SB3_FIELDS)


def builder_tool_descriptions(spec: AgentSpec) -> dict[str, str]:
    """{tool name → ""} for the selected strands tools.

    Listed in DEFAULT_TOOL_DESCRIPTIONS so a config bundle can tune them by name.
    The empty default is filled at import time with the tool's own (SDK-authored)
    description — and, for ``calculator``, keeps the template's description of its
    own calculator from overwriting the strands one.
    """
    out = {name: "" for name in (*spec.strands_tools, *spec.sandbox_tools)}
    # a custom tool's description is the member's own (the wizard's field)
    out.update({t.name: t.description for t in spec.custom_tools})
    return out


def journal_root(spec: AgentSpec) -> str:
    storage = spec.filesystem.session_storage
    return storage.mount_path if storage is not None else "/tmp"


def render_builder_source(spec: AgentSpec, *, a2a: bool = False) -> str:
    http = spec.strands_tool_config.http_request if spec.strands_tool_config else None
    params = spec.model_params.model_dump() if spec.model_params else {}
    source = (
        BUILDER_TEMPLATE.read_text(encoding="utf-8")
        .replace("__LAUNCHPAD_SB_STRANDS_TOOLS__", repr(list(spec.strands_tools)))
        .replace(
            "__LAUNCHPAD_SB_HTTP_ALLOWED_HOSTS__", repr(list(http.allowed_hosts) if http else [])
        )
        .replace(
            "__LAUNCHPAD_SB_MODEL_PARAMS__",
            repr({k: v for k, v in params.items() if _is_set(v)}),
        )
        .replace(
            "__LAUNCHPAD_SB_CONVERSATION__",
            repr(spec.conversation.model_dump(exclude_none=True) if spec.conversation else {}),
        )
        .replace("__LAUNCHPAD_SB_OUTPUT_SCHEMA__", repr(spec.output_schema))
        .replace(
            "__LAUNCHPAD_SB_FEW_SHOT__", repr([e.model_dump() for e in spec.few_shot_examples])
        )
        .replace("__LAUNCHPAD_SB_PROMPT_VARIABLES__", repr(dict(spec.prompt_variables)))
        .replace("__LAUNCHPAD_SB_JOURNAL_ROOT__", repr(journal_root(spec)))
    )
    if uses_sb2(spec):
        source += "\n\n" + render_sb2_source(spec)
    if uses_sb3(spec):
        source += "\n\n" + render_sb3_source(spec)
    return (_A2A_PRELUDE + source) if a2a else source


def custom_tool_targets(spec: AgentSpec) -> list[dict[str, object]]:
    """The CUSTOM_TOOL_SPECS literal: name, description, where it runs, its file in
    the zip and the model-facing input schema (from the signature, never by import)."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "execution": t.execution,
            "file": f"{t.name}.py",
            "input_schema": tool_signature(t.code, t.name).input_schema,
        }
        for t in spec.custom_tools
    ]


def custom_tool_files(spec: AgentSpec) -> dict[str, str]:
    """{zip-relative path → source} the package stage writes next to main.py."""
    return {f"{CUSTOM_TOOLS_DIRNAME}/{t.name}.py": t.code for t in spec.custom_tools}


def custom_tool_requirements(spec: AgentSpec) -> list[str]:
    """The runtime custom tools' pins, deduplicated in declaration order (a
    sandboxed tool installs nothing — its schema refuses requirements)."""
    out: list[str] = []
    for t in spec.custom_tools:
        out.extend(r for r in t.requirements if r not in out)
    return out


def render_sb3_source(spec: AgentSpec) -> str:
    return (
        SB3_TEMPLATE.read_text(encoding="utf-8")
        .replace("__LAUNCHPAD_SB3_CUSTOM_TOOLS__", repr(custom_tool_targets(spec)))
        .replace("__LAUNCHPAD_SB3_SANDBOX_TOOLS__", repr(list(spec.sandbox_tools)))
        .replace(
            "__LAUNCHPAD_SB3_CODE_INTERPRETER_ID__",
            repr(spec.code_interpreter_id or DEFAULT_CODE_INTERPRETER_ID),
        )
        .replace("__LAUNCHPAD_SB3_SESSION_DIR__", repr(journal_root(spec)))
    )


def agent_tool_targets(spec: AgentSpec) -> list[dict[str, object]]:
    """The AGENT_TOOL_SPECS literal: the resolved targets only (a ref the ledger
    never resolved has no ARN to call, and the deploy refuses it before render)."""
    return [
        {
            "name": ref.tool_name,
            "description": ref.description,
            "runtime_arn": ref.runtime_arn,
            "protocol": ref.protocol,
            "qualifier": ref.qualifier,
            "runtime_user": ref.runtime_user,
        }
        for ref in spec.agent_tools
        if ref.runtime_arn
    ]


def render_sb2_source(spec: AgentSpec) -> str:
    provider = spec.model_provider.model_dump(exclude_none=True) if spec.model_provider else {}
    hooks = (
        spec.tool_hooks.model_dump(exclude_defaults=True)
        if spec.tool_hooks and not spec.tool_hooks.is_empty()
        else {}
    )
    return (
        SB2_TEMPLATE.read_text(encoding="utf-8")
        .replace("__LAUNCHPAD_SB2_AGENT_TOOLS__", repr(agent_tool_targets(spec)))
        .replace("__LAUNCHPAD_SB2_MODEL_PROVIDER__", repr(provider))
        .replace("__LAUNCHPAD_SB2_PROVIDER_MODEL_ID__", repr(spec.model_id if provider else ""))
        .replace("__LAUNCHPAD_SB2_TOOL_HOOKS__", repr(hooks))
        .replace(
            "__LAUNCHPAD_SB2_RETRY__",
            repr(spec.retry.model_dump() if spec.retry else {}),
        )
        .replace(
            "__LAUNCHPAD_SB2_AGENT_TOOL_TIMEOUT__",
            repr(min(spec.timeout_seconds, AGENT_TOOL_TIMEOUT_CEILING_S)),
        )
    )


def _is_set(value: object) -> bool:
    # 0 / 0.0 are real settings (temperature 0); unset means None, [] or False
    return value is not None and value is not False and value != []


def _swap(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"Strands template drifted: expected one {old.strip()!r} line")
    return source.replace(old, new)


def apply_builder(source: str, spec: AgentSpec, *, a2a: bool = False) -> str:
    """Resolve the builder markers of a raw template (before any other placeholder)."""
    if not uses_builder(spec):
        source = source.replace(SOURCE_MARKER, "").replace(BUILD_HOOK_MARKER, "")
        return source.replace(TURN_HOOK_MARKER, "")
    source = source.replace(SOURCE_MARKER, "\n\n" + render_builder_source(spec, a2a=a2a) + "\n")
    if a2a:
        source = source.replace(
            BUILD_HOOK_MARKER,
            '    kwargs["system_prompt"] = resolve_system_prompt()\n'
            "    kwargs = launchpad_builder_kwargs(kwargs)\n",
        )
        return _swap(source, A2A_TOOLS_LINE, A2A_TOOLS_LINE_BUILDER)
    source = source.replace(BUILD_HOOK_MARKER, "    kwargs = launchpad_builder_kwargs(kwargs)\n")
    turn_hook = (
        '    context = payload.get("context")\n'
        "    _LAUNCHPAD_PROMPT_CONTEXT.set(dict(context) if isinstance(context, dict) else {})\n"
    )
    if uses_sb2(spec):
        turn_hook += "    launchpad_turn_state(payload, session_id)\n"
    source = source.replace(TURN_HOOK_MARKER, turn_hook)
    source = _swap(source, HTTP_TOOLS_LINE, HTTP_TOOLS_LINE_BUILDER)
    return _swap(source, HTTP_COMPLETE_LINE, HTTP_COMPLETE_LINE_BUILDER)
