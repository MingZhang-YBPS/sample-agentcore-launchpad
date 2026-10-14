# mcp_stdio_bridge — host a stdio-only MCP server

AgentCore Runtime hosts MCP servers that speak **streamable HTTP on
`0.0.0.0:8000/mcp`** (linux/arm64). Many MCP servers only speak **stdio**. If
you can change the server, switch its transport instead — it is the smaller
change (Python: `FastMCP(host="0.0.0.0", stateless_http=True)` +
`mcp.run(transport="streamable-http")`; TypeScript:
`StreamableHTTPServerTransport`). When you cannot, run it behind this bridge.

`mcp_stdio_bridge.py` (Python standard library only, 3.10+) serves
`POST /mcp` and relays every JSON-RPC message to one stdio server it starts as
a subprocess (newline-delimited JSON on stdin/stdout). `GET /ping` answers
`{"status": "Healthy"}`.

## Limits

- **One subprocess per container**: fine for a stateless server or a single
  session. A server that keeps per-session state sees every caller as the same
  session — give such a server a streamable-HTTP transport instead.
- **JSON responses only, no SSE stream**: the server's own notifications
  (progress, logging) are dropped, and server→client requests (sampling,
  elicitation, roots) are answered "not supported". `ping` is answered.
- `initialize` reaches the subprocess once; later `initialize` calls are
  answered from that first result. A crashed subprocess restarts on the next
  request and is re-initialized with the first client's parameters first.

## In Launchpad (code zip)

Console → **New agent** → **MCP server**, artifact **code zip**, then turn on
**stdio server** in the MCP server card and give the command and its
arguments (spec: `mcp.stdio_wrapper = {command, args, base}`):

| base | what the deployer does |
|---|---|
| `python` | adds `mcp_stdio_bridge.py` to the bundle (after resolving `requirements.txt`) and makes it the code runtime's entrypoint |
| `node` | adds the bridge and `Dockerfile.node` (any `Dockerfile` in the zip is replaced), builds the image in CodeBuild (arm64) and deploys a container runtime |

The command runs without a shell, from the bundle root (`/app` in the
images); `python` / `python3` resolve to the bridge's interpreter. The
deployer passes the argv as env `MCP_STDIO_COMMAND`.

Try it with [`example/`](example/) (a stdio FastMCP server):

```bash
cd samples/mcp_stdio_bridge/example && zip -r ../stdio-example.zip server.py requirements.txt
```

Upload `stdio-example.zip`, stdio server on, base `python`, command `python`,
arguments `server.py`.

## Elsewhere (your own image)

Copy `mcp_stdio_bridge.py` and `Dockerfile.python` or `Dockerfile.node` (as
`Dockerfile`) next to your server, set `MCP_STDIO_COMMAND` in the Dockerfile
(a JSON array), build for linux/arm64 and deploy the image — in Launchpad as a
**Dockerfile context** or **ECR image** MCP server. The Node image installs
`python3` for the bridge and runs `npm ci --omit=dev`; ship built JavaScript.

Run it locally:

```bash
python3 mcp_stdio_bridge.py -- python example/server.py
# then point an MCP client at http://localhost:8000/mcp
```

Command precedence: arguments after `--`, then env `MCP_STDIO_COMMAND`, then
`mcp_stdio_bridge.json` (`{"command": ..., "args": [...]}`) next to the
bridge. Other env: `MCP_BRIDGE_PORT` (8000), `MCP_BRIDGE_PATH` (`/mcp`),
`MCP_BRIDGE_TIMEOUT` (300 s per request), `MCP_STDIO_CWD` (the bridge's
directory).
