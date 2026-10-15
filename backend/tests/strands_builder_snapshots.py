"""Pre-SB-1 specs whose generated code must stay byte-identical (strands-builder §2).

The fixtures under ``tests/fixtures/strands_builder/`` were rendered from these
specs on the commit before SB-1 landed; ``test_strands_builder.py`` re-renders
them and compares byte for byte. Regenerate only for a deliberate template change:

    uv run python -m tests.strands_builder_snapshots
"""

from pathlib import Path

from app.schemas.agent import A2ASkill, AgentSpec, KnowledgeBaseRef, ToolRef

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "strands_builder"

_BASE = {"method": "zip_runtime", "system_prompt": "You are a snapshot agent. {{city}} stays literal."}

OLD_SPECS: dict[str, AgentSpec] = {
    "default": AgentSpec(name="snap-default", model_id="global.anthropic.claude-sonnet-4-5-v1:0", **_BASE),
    "mantle": AgentSpec(
        name="snap-mantle", model_source="mantle", model_id="openai.gpt-5.6-sol", **_BASE
    ),
    "kb_skills": AgentSpec(
        name="snap-kb",
        model_id="global.anthropic.claude-sonnet-4-5-v1:0",
        knowledge_bases=[KnowledgeBaseRef(kb_id="KB12345678", name="Handbook")],
        skills=["s3://launchpad-artifacts/skills/pirate-speak/"],
        env={"TEAM": "ops"},
        **_BASE,
    ),
    "toolkit": AgentSpec(
        name="snap-toolkit", model_id="global.anthropic.claude-sonnet-4-5-v1:0",
        toolkits=["hr_assistant"], **_BASE,
    ),
    "gateway_rest": AgentSpec(
        name="snap-gateway",
        model_id="global.anthropic.claude-sonnet-4-5-v1:0",
        tools=[
            ToolRef(type="gateway", name="launchpad-gw"),
            ToolRef(
                type="rest", name="crm",
                config={"url": "https://crm.example/api", "description": "CRM"},
                auth={"connection": "team-idp", "kind": "oauth2", "scopes": ["crm/read"]},
            ),
        ],
        tool_description_overrides={"calculator": "Do sums."},
        **_BASE,
    ),
    "a2a": AgentSpec(
        name="snap-a2a",
        model_id="global.anthropic.claude-sonnet-4-5-v1:0",
        protocol="a2a",
        a2a_skills=[A2ASkill(id="answer", name="Answer", description="Answers")],
        **_BASE,
    ),
}


def render(spec: AgentSpec) -> str:
    if spec.protocol == "a2a":
        from app.templates.strands_a2a_agent import render_a2a_main_py

        return render_a2a_main_py(spec)
    from app.templates.strands_agent import render_main_py

    return render_main_py(spec)


def fixture_path(key: str) -> Path:
    return FIXTURE_DIR / f"{key}_main.py.txt"


def dump(spec: AgentSpec) -> str:
    """The stored-spec JSON (what the ledger keeps) — new fields must not appear."""
    return spec.model_dump_json(indent=2) + "\n"


def dump_path(key: str) -> Path:
    return FIXTURE_DIR / f"{key}_spec.json"


if __name__ == "__main__":
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for key, spec in OLD_SPECS.items():
        fixture_path(key).write_text(render(spec), encoding="utf-8")
        dump_path(key).write_text(dump(spec), encoding="utf-8")
        print("wrote", fixture_path(key))
