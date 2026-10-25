# hello-mcp — an MCP server for the "MCP server" deploy

Two tools (`lookup_order`, `refund_policy`) served by the official MCP Python SDK
over streamable HTTP on `0.0.0.0:8000/mcp`, the AgentCore Runtime MCP contract.

Deploy: V3 console → **New agent** → **MCP server** (or
`/v2/agents?view=new&method=byoc&protocol=mcp`), artifact **code zip**:

```bash
cd samples/byoc/hello-mcp && zip -r ../hello-mcp.zip main.py requirements.txt
```

Upload `hello-mcp.zip`, keep entrypoint `main.py`, and pick a Gateway target name
such as `order-lookup`. The deploy creates the runtime (`serverProtocol=MCP`,
inbound JWT that allows only the server's own Cognito client), an OAuth2 provider
for that client and the Gateway target, then lists the tools. Agents attach them as
Gateway tools: `order-lookup___lookup_order`, `order-lookup___refund_policy`.

Run it locally first if you like: `pip install mcp && python main.py`, then point an
MCP client at `http://localhost:8000/mcp`.
