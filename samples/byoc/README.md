# BYOC samples — Bring Your Own Code

Two minimal agents that satisfy the Launchpad BYOC runtime contract, plus a
migration walkthrough (`langchain-to-strands/`: a tool-calling LangChain agent
moved to Strands Agents and deployed as `code_zip`; see its README):

- ARM64 (aarch64) · port **8080** · `POST /invocations` + `GET /ping`
- invoke payload: `{"prompt": "...", "actor_id": "..."}`
- response: any JSON (or SSE). `{"result": "..."}` is the convention the
  samples follow; `response` / `answer` / `output` / `text` / `message` /
  `content` are read too, a body with none of them is shown verbatim as JSON,
  and `{"error": "..."}` renders as a failed turn.
- the `bedrock-agentcore` SDK (`BedrockAgentCoreApp` + `@app.entrypoint`)
  implements all of the above.

Both call Bedrock Converse with the model id from env `MODEL_ID`
(default `us.anthropic.claude-3-5-haiku-20241022-v1:0`) and fall back to an
echo when the account has no model access, so they deploy and chat regardless.

Launchpad sets `MODEL_ID` for you: the agent's execution role may invoke only
the models in the wizard's **Allowed models** list (`spec.byoc.allowed_models`
via the API; without the list, just `spec.model_id`). The deployer injects the
primary (first) model as env `MODEL_ID` and the full list as env
`ALLOWED_MODEL_IDS` (comma-separated) unless you set them yourself — so these
samples always call a permitted model with no extra configuration. An agent
that switches models at runtime should pick from `ALLOWED_MODEL_IDS`.
Selections accept literal IDs, foundation-model ARNs and system inference-profile
ARNs. Wildcards, IAM variables and application inference-profile ARNs are refused;
an unknown ID never grants access to all foundation models.

## Response formats the console reads

Chat, the public `/v1` API and evaluation replays share one reader
(`backend/app/services/agentcore/runtime.py`), so any of these works unchanged:

| Shape | Example |
|---|---|
| Launchpad events (SSE) | `{"event": "delta", "text": "…"}` … `{"event": "complete", "result": "…"}`; `{"event": "tool", "name": "…"}` |
| Converse stream events (SSE) — what the AgentCore CLI Strands template emits | `{"event": {"contentBlockDelta": {"delta": {"text": "…"}}}}`, `contentBlockStart.start.toolUse` → a tool call |
| Strands loop events forwarded next to them | `init_event_loop`, `start`, `start_event_loop`, `message`, `result: {AgentResult}` — ignored as text; a `result` / assistant `message` supplies the reply only when nothing streamed |
| One JSON body | `{"result": "…"}`, or `response` / `answer` / `output` / `output_text` / `text` / `message` / `content` / `completion` / `reply` |
| Plain text | `text/plain` (or any non-JSON body) is the reply |

A response with nothing readable (an empty body, or only bookkeeping such as
`metadata` usage) ends the turn with `runtime.empty_reply` /
`runtime.unrecognized_reply`; the console shows the start of what the runtime sent.

## Telemetry — what makes an agent show up in Observability and Evaluation

Observability (sessions, traces, the agent drop-down) and evaluation read the
OpenTelemetry spans an agent exports to CloudWatch (`aws/spans`). AgentCore
Runtime exports them only when the process runs under the ADOT
`opentelemetry-instrument` launcher with the distro installed:

| Artifact kind | Spans |
|---|---|
| `code_zip` | Launchpad adds the launcher when the upload carries **aws-opentelemetry-distro** — listed in `requirements.txt` (with *install requirements* on) or vendored in the zip. The upload response says so (`detected.adot_detected` / `adot_source`) and the deploy log prints `telemetry: ADOT launcher on\|off`. Without it the agent chats normally but writes no span. |
| `container_source` / `container_image` | Your image decides: install `aws-opentelemetry-distro` (plus `strands-agents[otel]` for a Strands agent) and start with `CMD ["opentelemetry-instrument", "python", "main.py"]`. |

A code zip without ADOT stays in the Observability agent drop-down, greyed
with the reason, and `POST /api/eval/runs` refuses it up front with
`eval.no_telemetry` (400) instead of failing after a full replay with
"No evaluable agent spans found". Code that instruments itself and does export
spans is not refused.

## hello-mcp — an MCP server (protocol `mcp`)

Not an agent: an MCP server that Launchpad hosts on AgentCore Runtime and puts
behind the workspace Gateway (streamable HTTP on `0.0.0.0:8000/mcp`, no
`/invocations`). See [hello-mcp/README.md](hello-mcp/README.md).

## hello-http — artifact kind `code_zip`

Python source + `requirements.txt`; Launchpad resolves the requirements for
linux/aarch64 at deploy time and runs the zip on the managed Python runtime.

**requirements.txt guidance.** List direct index dependencies only; version
pins are optional — Launchpad compiles the file into a hashed lock
(`requirements.lock`, shipped in the artifact), which is what makes the build
reproducible. The pip file format is honoured (continuations, comments,
environment markers), but `--hash=` options are dropped: the platform re-locks
against its own deploy target — linux/aarch64, `manylinux_2_28` by default (the
runtime is Amazon Linux 2023 / glibc 2.34, measured 2026-09-18; the docs'
`manylinux2014` remains the conservative fallback via
`LAUNCHPAD_RUNTIME_PYTHON_PLATFORM`) — and generates fresh hashes. Refused, so
the file cannot reach outside the platform's package index: `-r`/`-c` includes,
editable installs, local paths, direct URL/VCS entries, and
`--index-url`/`--extra-index-url`/`--find-links`. Source builds never run: a
dependency with no compatible aarch64 wheel fails with the package named — pin
a release that ships one, switch to the `container_source` path, or vendor the
packages in the zip and set `install_requirements=false`. The upload response
(`detected.requirements`) reports the resolve verdict before you deploy.

```bash
cd samples/byoc/hello-http
zip -r ../hello-http.zip .        # zip the directory CONTENTS
# — or zip the directory itself; Launchpad normalizes a single top-level dir:
cd samples/byoc && zip -r hello-http.zip hello-http/
```

Upload the zip in **Create → Bring Your Own Code → Code zip** (entrypoint
`main.py`), or via the API — see `docs/api.md` (`POST /api/agents/uploads`).

## hello-container — artifact kind `container_source`

The same agent with a `Dockerfile`; Launchpad builds it ARM64 on CodeBuild,
pushes to ECR and deploys the image.

```bash
cd samples/byoc
zip -r hello-container.zip hello-container/
```

Upload in **Create → Bring Your Own Code → Dockerfile build**.

## artifact kind `container_image`

No sample needed — push any image satisfying the contract to a private ECR
repository in the workspace account/region and paste its URI
(`<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>`).
