"""BEFORE — the agent the team already had, written with LangChain.

This file is here to be migrated, not to be deployed: it serves its own FastAPI
app on whatever path the team picked, so it does not satisfy the Launchpad BYOC
contract (`POST /invocations` + `GET /ping` on :8080). Run it locally to see what
the agent did before the move:

    pip install -r requirements.txt
    uvicorn main:app --port 8000
    curl -s localhost:8000/chat -H 'content-type: application/json' \
      -d '{"question": "标准经济舱 1200 元，起飞前 3 天退票能退多少？"}'

The migrated version lives one directory up (`../main.py`): same tools, same
prompt, Strands instead of LangChain, and the AgentCore entrypoint Launchpad
expects.
"""

import os
import sys

from fastapi import FastAPI
from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from policy_tools import SYSTEM_PROMPT, baggage_allowance, refund_quote  # noqa: E402

MODEL_ID = os.environ.get("MODEL_ID", "global.zai.glm-5.3")

# Both frameworks read the description from the docstring and the schema from
# the type hints, so these two lines are identical in the migrated version —
# only the `tool` import above differs.
refund_tool = tool(refund_quote)
baggage_tool = tool(baggage_allowance)

agent = create_agent(
    model=ChatBedrockConverse(model=MODEL_ID),
    tools=[refund_tool, baggage_tool],
    system_prompt=SYSTEM_PROMPT,
)

app = FastAPI()


class Ask(BaseModel):
    question: str


def _text(content: str | list) -> str:
    """ChatBedrockConverse returns a list of content blocks, not a plain string."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


@app.post("/chat")
def chat(ask: Ask) -> dict[str, str]:
    result = agent.invoke({"messages": [{"role": "user", "content": ask.question}]})
    return {"answer": _text(result["messages"][-1].content)}
