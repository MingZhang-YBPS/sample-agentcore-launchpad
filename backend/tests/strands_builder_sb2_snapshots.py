"""SB-1 specs whose generated code must stay byte-identical after SB-2 (strands-builder §2).

The fixtures under ``tests/fixtures/strands_builder_sb2/`` were rendered from these
specs on the commit before SB-2 landed; ``test_strands_builder_sb2.py`` re-renders
them and compares byte for byte (the pre-SB-1 specs keep their own pins in
``strands_builder_snapshots``). Regenerate only for a deliberate template change:

    uv run python -m tests.strands_builder_sb2_snapshots
"""

from pathlib import Path

from app.schemas.agent import AgentSpec
from tests.strands_builder_snapshots import dump, render

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "strands_builder_sb2"

_CLAUDE = "global.anthropic.claude-sonnet-4-5-v1:0"
_BASE = {"method": "zip_runtime", "model_id": _CLAUDE,
         "system_prompt": "Weather for {{city}}."}

SB1_SPECS: dict[str, AgentSpec] = {
    "sb1_http": AgentSpec(
        name="snap-sb1",
        strands_tools=["current_time", "calculator", "http_request", "journal"],
        strands_tool_config={"http_request": {"allowed_hosts": ["api.example.com"]}},
        model_params={"temperature": 0.2, "max_tokens": 2000, "prompt_caching": True},
        conversation={"strategy": "summarizing", "window_size": 8},
        output_schema={"type": "object", "properties": {"city": {"type": "string"}}},
        few_shot_examples=[{"question": "Rome?", "answer": "Sunny."}],
        prompt_variables={"city": "Rome"},
        **_BASE,
    ),
    "sb1_a2a": AgentSpec(
        name="snap-sb1-a2a", protocol="a2a", strands_tools=["think"],
        model_params={"top_p": 0.9}, prompt_variables={"city": "Rome"}, **_BASE,
    ),
}


def fixture_path(key: str) -> Path:
    return FIXTURE_DIR / f"{key}_main.py.txt"


def dump_path(key: str) -> Path:
    return FIXTURE_DIR / f"{key}_spec.json"


if __name__ == "__main__":
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for key, spec in SB1_SPECS.items():
        fixture_path(key).write_text(render(spec), encoding="utf-8")
        dump_path(key).write_text(dump(spec), encoding="utf-8")
        print("wrote", fixture_path(key))
