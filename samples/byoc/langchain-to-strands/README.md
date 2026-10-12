# langchain-to-strands — migrate a LangChain agent to Strands and deploy it

A tool-calling agent that quotes Unicorn Air change/refund fees and baggage
allowances, in two versions:

| | File | Framework | Serves |
|---|---|---|---|
| **before** | `legacy_langchain/main.py` | LangChain `create_agent` + `ChatBedrockConverse` | its own FastAPI route `POST /chat` |
| **after** | `main.py` | Strands `Agent` + `BedrockModel` | `POST /invocations` + `GET /ping` on :8080, via `BedrockAgentCoreApp` |

Both import the same two tools from `policy_tools.py`, **which does not change
in the migration** — that is the point of the sample. All policy numbers are
fictional and come from the workshop's policy sheet (rules `A-02`…`A-15`).

## The four changes

```diff
-from langchain.agents import create_agent
-from langchain_aws import ChatBedrockConverse
-from langchain_core.tools import tool                 # 1. tool decorator
+from bedrock_agentcore.runtime import BedrockAgentCoreApp
+from strands import Agent, tool
+from strands.models import BedrockModel

 refund_tool = tool(refund_quote)                      # unchanged
 baggage_tool = tool(baggage_allowance)                # unchanged

-agent = create_agent(                                 # 2. model object
-    model=ChatBedrockConverse(model=MODEL_ID),        # 3. agent construction
+agent = Agent(
+    model=BedrockModel(model_id=MODEL_ID),
     tools=[refund_tool, baggage_tool],
     system_prompt=SYSTEM_PROMPT,
 )

-@app.post("/chat")                                    # 4. the server contract
-def chat(ask: Ask) -> dict[str, str]:
-    result = agent.invoke({"messages": [{"role": "user", "content": ask.question}]})
-    return {"answer": _text(result["messages"][-1].content)}
+@app.entrypoint
+async def invoke(payload: dict):
+    async for event in agent.stream_async(payload["prompt"]):
+        ...  # yield {"event": "delta" | "tool" | "complete", ...}
```

1. **Tool decorator.** `langchain_core.tools.tool` → `strands.tool`. Both read
   the description from the docstring and the schema from the type hints, so the
   tool functions themselves are untouched.
2. **Model object.** `ChatBedrockConverse(model=…)` → `BedrockModel(model_id=…)`.
3. **Agent construction and call.** `create_agent(...)` returns a graph invoked
   with a message list and answered with content blocks; a Strands `Agent` is
   called with the prompt and `str(result)` is the text.
4. **The server.** The Launchpad BYOC contract is ARM64 · port 8080 ·
   `POST /invocations` + `GET /ping`, with the payload
   `{"prompt": "...", "actor_id": "..."}`. `BedrockAgentCoreApp` plus
   `@app.entrypoint` implements all of it, so the hand-written FastAPI app goes
   away. The entrypoint streams the platform's runtime envelope — `delta` for
   text, one `tool` per tool call, `complete` with the answer — which is what
   lets the console show `refund_quote` / `baggage_allowance` next to the reply.
   Returning a plain `{"result": "..."}` also works, but then the console can
   show only the answer, not the tool calls behind it.

`MODEL_ID` is injected by Launchpad: the first entry of the wizard's **Allowed
models** list, which is also the only list the execution role may invoke.

## Try it before deploying

```bash
# the tools on their own — rule A-07: fee 300, taxes 80, refund 980
python policy_tools.py

# the migrated agent, locally (needs Bedrock credentials in the environment)
pip install -r requirements.txt
MODEL_ID=global.zai.glm-5.3 python -c "
import asyncio, main
async def ask(q):
    async for ev in main.invoke({'prompt': q}):
        if ev['event'] in ('tool', 'complete', 'error'): print(ev)
asyncio.run(ask('标准经济舱，票面价 1200 元，起飞前 3 天退票，能退多少钱？'))"

# or serve the real contract and call it the way Launchpad does
MODEL_ID=global.zai.glm-5.3 python main.py          # :8080
curl -s localhost:8080/invocations -H 'content-type: application/json' \
  -d '{"prompt": "轻享票带一个 23 公斤的箱子要多少钱？", "actor_id": "demo"}'
```

The agent must call a tool for every number: the system prompt in
`policy_tools.py` forbids estimating. A good answer quotes the rule ids and
shows the arithmetic, e.g. `1200 − 300 + 80 = 980`.

## Deploy as BYOC

```bash
cd samples/byoc/langchain-to-strands
zip -r ~/lab-legacy-agent.zip main.py policy_tools.py requirements.txt
```

`legacy_langchain/` is deliberately left out of the zip: it is the before state,
it pulls in LangChain, and it does not satisfy the runtime contract.

Then in the console: **New agent → Other ways to build → Bring your own code**,
upload the zip, entrypoint `main.py`, and leave **Allowed models** at its
default **GLM-5.3 (global)** (`global.zai.glm-5.3`) — the tools and the model
both ride Bedrock Converse.

## Verified

Against real Bedrock in `us-west-2` on 2026-10-08, both versions answered the
`A-07` question with **¥980** after calling `refund_quote`, and the Strands
version also priced a 23 kg Lite bag at **¥180** online / **¥260** at the
airport via `baggage_allowance`; its stream carried one `tool` event per call. Pinned versions: `strands-agents==1.47.0`,
`bedrock-agentcore==1.17.0`, `langchain` 1.4.3 + `langchain-aws` 1.0 for the
before state.
