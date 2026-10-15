"""SB-2 specs whose generated code must stay byte-identical after SB-3 (strands-builder §2).

The fixtures under ``tests/fixtures/strands_builder_sb3/`` were rendered from these
specs on the commit before SB-3 landed; ``test_strands_builder_sb3.py`` re-renders
them and compares byte for byte (older specs keep their own pins in
``strands_builder_snapshots`` / ``strands_builder_sb2_snapshots``). Regenerate only
for a deliberate template change:

    uv run python -m tests.strands_builder_sb3_snapshots
"""

from pathlib import Path

from app.schemas.agent import AgentSpec
from tests.strands_builder_snapshots import dump, render

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "strands_builder_sb3"

_CLAUDE = "global.anthropic.claude-sonnet-4-5-v1:0"
_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/helper_ab12cd-XyZ0123456"
_BASE = {"method": "zip_runtime", "model_id": _CLAUDE, "system_prompt": "Be brief."}

SB2_SPECS: dict[str, AgentSpec] = {
    "sb2_http": AgentSpec(
        name="snap-sb2",
        strands_tools=["current_time", "think"],
        agent_tools=[{"agent_id": "a1", "tool_name": "ask_helper",
                      "description": "Ask the helper agent.", "runtime_arn": _ARN}],
        tool_hooks={"approval_tools": ["ask_*"], "log_tool_calls": True,
                    "mask_args": ["ssn"], "max_tool_calls_per_turn": 5},
        retry={"max_attempts": 3},
        **_BASE,
    ),
    "sb2_provider": AgentSpec(
        name="snap-sb2-provider",
        model_provider={"kind": "anthropic", "connection": "anthropic-key"},
        model_params={"temperature": 0.3},
        **{**_BASE, "model_id": "claude-sonnet-4-5"},
    ),
    "sb2_a2a": AgentSpec(
        name="snap-sb2-a2a", protocol="a2a",
        tool_hooks={"max_tool_calls_per_turn": 2}, retry={}, **_BASE,
    ),
}


def fixture_path(key: str) -> Path:
    return FIXTURE_DIR / f"{key}_main.py.txt"


def dump_path(key: str) -> Path:
    return FIXTURE_DIR / f"{key}_spec.json"


if __name__ == "__main__":
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for key, spec in SB2_SPECS.items():
        fixture_path(key).write_text(render(spec), encoding="utf-8")
        dump_path(key).write_text(dump(spec), encoding="utf-8")
        print("wrote", fixture_path(key))
