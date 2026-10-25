"""AFTER — the same agent on Strands Agents, deployable as Launchpad BYOC.

Four things changed from `legacy_langchain/main.py`; the tools in
`policy_tools.py` did not change at all:

1. the tool decorator: `langchain_core.tools.tool` -> `strands.tool`
2. the model object: `ChatBedrockConverse(model=...)` -> `BedrockModel(model_id=...)`
3. the agent call: `create_agent(...).invoke({"messages": [...]})` -> `Agent(...)(prompt)`
4. the server: a FastAPI route -> `BedrockAgentCoreApp` + `@app.entrypoint`,
   which is what gives Launchpad `POST /invocations` and `GET /ping` on :8080;
   the entrypoint streams `delta` / `tool` / `complete` events so the console
   shows each tool call next to the answer

`MODEL_ID` is injected by Launchpad: it is the first entry of the wizard's
**Allowed models** list, and the execution role may invoke only that list.
"""

import os
from collections.abc import AsyncIterator
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

# isort: split
# the sample's own module, shared with legacy_langchain/main.py
from policy_tools import SYSTEM_PROMPT, baggage_allowance, refund_quote

MODEL_ID = os.environ.get("MODEL_ID", "global.zai.glm-5.3")

# Identical to the LangChain version: both frameworks read the description from
# the docstring and the schema from the type hints.
refund_tool = tool(refund_quote)
baggage_tool = tool(baggage_allowance)

app = BedrockAgentCoreApp()


def build_agent() -> Agent:
    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        tools=[refund_tool, baggage_tool],
        system_prompt=SYSTEM_PROMPT,
        # the default handler prints every token to stdout; the stream below
        # already carries them to the console, so keep the runtime log clean
        callback_handler=None,
    )


@app.entrypoint
async def invoke(payload: dict) -> AsyncIterator[dict[str, Any]]:
    """Launchpad sends {"prompt": "...", "actor_id": "..."}.

    The reply is streamed in the platform's runtime envelope, the same one the
    platform-generated Strands agents use: `delta` for text as it arrives, one
    `tool` per tool call (so the console shows which tool ran), and `complete`
    with the final answer. A plain `{"result": "..."}` would work too, but the
    console could then show the answer only, not the tool calls behind it.
    """
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        yield {"event": "error", "message": "payload must include a non-empty 'prompt'"}
        return
    # A fresh Agent per request keeps turns isolated: Launchpad passes the actor
    # and session itself, and this sample holds no conversation state.
    seen: set[str] = set()
    result: Any = None
    try:
        async for event in build_agent().stream_async(prompt):
            if not isinstance(event, dict):
                continue
            text = event.get("data")
            if isinstance(text, str) and text:
                yield {"event": "delta", "text": text}
            tool_use = event.get("current_tool_use")
            if isinstance(tool_use, dict) and tool_use.get("name"):
                key = str(tool_use.get("toolUseId") or tool_use["name"])
                if key not in seen:
                    seen.add(key)
                    yield {
                        "event": "tool",
                        "name": str(tool_use["name"]),
                        "id": tool_use.get("toolUseId"),
                    }
            if "result" in event:
                result = event["result"]
    except Exception as exc:  # surfaced as a failed turn in the console
        yield {"event": "error", "message": f"{type(exc).__name__}: {exc}"}
        return
    yield {"event": "complete", "result": str(result) if result is not None else ""}


if __name__ == "__main__":
    app.run()
