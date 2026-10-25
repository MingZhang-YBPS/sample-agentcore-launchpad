"""stdio MCP servers behind the platform bridge (projects §15.5): the
``mcp.stdio_wrapper`` spec field, the BYOC packaging of a wrapped code zip
(Python base → code runtime, Node base → image build), the deploy wiring, and
the bridge itself against a fake stdio server — no AWS."""

# `aws` is the AWS-stub fixture of the hosted-MCP tests, imported and then
# requested by name — which ruff reads as a redefinition.
# ruff: noqa: F811

import importlib.util
import io
import json
import sys
import textwrap
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.deployer import byoc as byoc_dep
from app.deployer.container import platform_buildspec_path
from app.schemas.agent import AgentSpec, McpStdioWrapper
from app.services import mcp_servers
from tests.conftest import ws_ctx
from tests.test_byoc import _get_agent, _mk_agent, _seed_upload, _stage_ctx
from tests.test_mcp_servers import RESOURCES, _mcp_spec, aws  # noqa: F401

STDIO_SERVER = b"from mcp.server.fastmcp import FastMCP\nFastMCP('x').run()\n"


def _wrapped(**wrapper) -> dict:
    return _mcp_spec(
        mcp={"target_name": "crm-lookup",
             "stdio_wrapper": {"command": "python", "args": ["server.py"], **wrapper}},
    )


# ── spec ──────────────────────────────────────────────────────────────────────


def test_stdio_wrapper_defaults_and_round_trip():
    spec = AgentSpec(**_wrapped())
    wrapper = spec.mcp.stdio_wrapper
    assert wrapper.base == "python" and not wrapper.builds_image
    assert wrapper.argv() == ["python", "server.py"]
    assert AgentSpec(**spec.model_dump()).mcp.stdio_wrapper == wrapper
    # every spec written before the field existed reads back unwrapped
    assert AgentSpec(**_mcp_spec()).mcp.stdio_wrapper is None
    assert McpStdioWrapper(command="node", base="node").builds_image


@pytest.mark.parametrize(
    "wrapper",
    [
        {"command": ""},
        {"command": "python server.py"},  # argv, not a shell line
        {"command": "python", "args": ["a\nb"]},
        {"command": "python", "args": ["x" * 1025]},
        {"command": "python", "args": ["a"] * 33},
        {"command": "python", "base": "deno"},
    ],
)
def test_stdio_wrapper_refusals(wrapper):
    with pytest.raises(ValidationError):
        McpStdioWrapper(**wrapper)


# ── packaging ─────────────────────────────────────────────────────────────────


def _ctx_for(spec_dict: dict, aws, files: dict[str, bytes]):
    _seed_upload(aws.s3, "u1", files)
    agent_id, dep_id = _mk_agent(AgentSpec(**spec_dict))
    return agent_id, _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))


def test_python_base_ships_the_bridge_in_the_code_zip(aws):
    # no main.py: the entrypoint field is unused once the bridge is the entrypoint
    agent_id, ctx = _ctx_for(_wrapped(), aws, {"server.py": STDIO_SERVER})

    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert "stdio bridge" in result.detail
    packaged = aws.s3.objects[("bkt", "agents/crm-mcp/byoc_package.zip")]
    with zipfile.ZipFile(io.BytesIO(packaged)) as zf:
        assert set(zf.namelist()) == {"server.py", "mcp_stdio_bridge.py"}
        shipped = zf.read("mcp_stdio_bridge.py")
    assert shipped == (byoc_dep.STDIO_BRIDGE_DIR / "mcp_stdio_bridge.py").read_bytes()


def test_a_missing_stdio_script_fails_the_package(aws):
    agent_id, ctx = _ctx_for(_wrapped(args=["srv/main.py"]), aws, {"server.py": STDIO_SERVER})
    with pytest.raises(RuntimeError, match="'srv/main.py' not found"):
        byoc_dep._stage_package(ctx, _get_agent(agent_id))


def test_module_and_binary_commands_are_not_checked(aws):
    agent_id, ctx = _ctx_for(_wrapped(args=["-m", "my_server"]), aws, {"x.py": b""})
    assert "stdio bridge" in byoc_dep._stage_package(ctx, _get_agent(agent_id)).detail


def test_stdio_wrapper_is_code_zip_only(aws):
    """Refused at the spec (the deployer keeps its own guard for stored specs)."""
    from pydantic import ValidationError

    from app.schemas.agent import AgentSpec

    spec = _wrapped()
    spec["byoc"] = {"artifact_kind": "container_source", "upload_id": "u1"}
    with pytest.raises(ValidationError, match="artifact_kind=code_zip only"):
        AgentSpec.model_validate(spec)


def test_node_base_builds_an_image_from_the_bridge_dockerfile(aws, monkeypatch):
    agent_id, ctx = _ctx_for(
        _wrapped(command="node", args=["index.js"], base="node"),
        aws,
        {"index.js": b"// stdio server\n", "package.json": b"{}",
         "Dockerfile": b"FROM evil\n", "buildspec.yml": b"evil"},
    )
    built = {}

    def build(ctx, agent, archive):
        with zipfile.ZipFile(archive) as zf:
            built.update({name: zf.read(name) for name in zf.namelist()})
        ctx.scratch["image_digest"] = "sha256:" + "0" * 64
        return "crm-mcp-v1", 0.5

    monkeypatch.setattr(byoc_dep, "build_and_push_image", build)
    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert "node base" in result.detail
    assert built["Dockerfile"] == (byoc_dep.STDIO_BRIDGE_DIR / "Dockerfile.node").read_bytes()
    assert built["buildspec.yml"] == platform_buildspec_path().read_bytes()
    assert {"index.js", "package.json", "mcp_stdio_bridge.py"} <= set(built)
    # no code-runtime bundle for a container deploy
    assert ("bkt", "agents/crm-mcp/byoc_package.zip") not in aws.s3.objects


# ── deploy ────────────────────────────────────────────────────────────────────


def _deploy(aws, spec_dict: dict, monkeypatch) -> tuple[str, dict]:
    spec = AgentSpec(**spec_dict)
    spec.mcp.client_id = "client-1"
    spec.inbound_auth = mcp_servers.inbound_auth(ws_ctx(RESOURCES), "client-1")
    agent_id, dep_id = _mk_agent(AgentSpec(**spec.model_dump()))
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({"s3_bucket": "bkt", "s3_key": "k", "execution_role_arn": "arn:r",
                        "image_uri": "111122223333.dkr.ecr.us-west-2.amazonaws.com/"
                                     "launchpad-agents@sha256:" + "0" * 64})
    calls = {}
    for name in ("create_code_runtime", "create_container_runtime"):
        real = getattr(byoc_dep.rt, name)

        def spy(client, _real=real, _name=name, **kwargs):
            calls[_name] = kwargs
            return _real(client, **kwargs)

        monkeypatch.setattr(byoc_dep.rt, name, spy)
    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    [(fn, kwargs)] = calls.items()
    return fn, kwargs


def test_python_base_deploys_the_bridge_as_the_code_entrypoint(aws, monkeypatch):
    spec = _wrapped(args=["server.py", "--verbose"])
    spec["env"] = {"MCP_STDIO_COMMAND": '["ignored"]', "LOG_LEVEL": "debug"}
    fn, kwargs = _deploy(aws, spec, monkeypatch)
    assert fn == "create_code_runtime"
    assert kwargs["entrypoint"] == "mcp_stdio_bridge.py"
    assert kwargs["protocol"] == "mcp"
    env = kwargs["environment"]
    assert json.loads(env["MCP_STDIO_COMMAND"]) == ["python", "server.py", "--verbose"]
    assert env["LOG_LEVEL"] == "debug"


def test_node_base_deploys_a_container(aws, monkeypatch):
    fn, kwargs = _deploy(
        aws, _wrapped(command="node", args=["index.js"], base="node"), monkeypatch
    )
    assert fn == "create_container_runtime"
    assert kwargs["container_uri"].endswith("@sha256:" + "0" * 64)
    assert json.loads(kwargs["environment"]["MCP_STDIO_COMMAND"]) == ["node", "index.js"]


def test_an_unwrapped_server_keeps_its_own_entrypoint(aws, monkeypatch):
    fn, kwargs = _deploy(aws, _mcp_spec(), monkeypatch)
    assert fn == "create_code_runtime"
    assert kwargs["entrypoint"] == "main.py"
    assert "MCP_STDIO_COMMAND" not in kwargs["environment"]


# ── the bridge ────────────────────────────────────────────────────────────────


def _load_bridge():
    path = byoc_dep.STDIO_BRIDGE_DIR / "mcp_stdio_bridge.py"
    spec = importlib.util.spec_from_file_location("mcp_stdio_bridge_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()

# A newline-delimited JSON-RPC stdio server: counts its initialize calls, can
# ask the client something (`ask`), and can be told to exit (`crash`).
FAKE_SERVER = textwrap.dedent("""
    import json, sys
    inits = 0
    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if method is None:
            if msg.get("id") == "srv-1":
                send({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {"answer": msg}})
            continue
        if mid is None:
            continue
        if method == "initialize":
            inits += 1
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"},
                "capabilities": {"tools": {}}, "inits": inits}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "echo"}]}})
        elif method == "ask":
            send({"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage"})
            send({"jsonrpc": "2.0", "id": mid, "result": {"asked": True}})
        elif method == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": msg.get("params")})
        elif method == "crash":
            sys.exit(3)
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "nope"}})
""")


@pytest.fixture
def served(tmp_path):
    (tmp_path / "server.py").write_text(FAKE_SERVER)
    session = bridge.StdioSession(
        bridge.resolve_command(["--", "python", "server.py"], {}), cwd=tmp_path, timeout=10
    )
    session.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler(session, "/mcp"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", session
    server.shutdown()
    session.stop()


def _post(base: str, payload, path: str = "/mcp") -> tuple[int, object]:
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else None


def _rpc(method: str, mid=1, params=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def test_resolve_command_precedence(tmp_path):
    (tmp_path / "mcp_stdio_bridge.json").write_text('{"command": "node", "args": ["a.js"]}')
    env = {"MCP_STDIO_COMMAND": '["uvx", "srv"]'}
    assert bridge.resolve_command(["--", "deno", "x"], env, tmp_path) == ["deno", "x"]
    assert bridge.resolve_command([], env, tmp_path) == ["uvx", "srv"]
    assert bridge.resolve_command([], {}, tmp_path) == ["node", "a.js"]
    assert bridge.resolve_command(["--", "python3", "s.py"], {}, tmp_path)[0] == sys.executable
    with pytest.raises(SystemExit):
        bridge.resolve_command([], {}, tmp_path / "missing")


def test_bridge_relays_requests_and_answers_initialize_once(served):
    base, _ = served
    status, body = _post(base, _rpc("initialize", 7, {"protocolVersion": "2025-06-18"}))
    assert status == 200 and body["id"] == 7 and body["result"]["inits"] == 1
    assert _post(base, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)

    # a second client's initialize is answered from the first result
    status, body = _post(base, _rpc("initialize", "b-1", {}))
    assert body["id"] == "b-1" and body["result"]["inits"] == 1

    # the server's progress notification is dropped; the response keeps the client id
    status, body = _post(base, _rpc("tools/list", 42))
    assert status == 200 and body == {"jsonrpc": "2.0", "id": 42,
                                      "result": {"tools": [{"name": "echo"}]}}
    status, body = _post(base, _rpc("missing", 3))
    assert body["error"]["code"] == -32601 and body["id"] == 3


def test_bridge_keeps_concurrent_clients_with_the_same_id_apart(served):
    base, _ = served
    results = {}

    def call(n):
        results[n] = _post(base, _rpc("echo", 1, {"n": n}))[1]

    threads = [threading.Thread(target=call, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results[n] == {"jsonrpc": "2.0", "id": 1, "result": {"n": n}} for n in range(8))


def test_bridge_batches_and_answers_server_requests_itself(served):
    base, _ = served
    status, body = _post(base, [_rpc("echo", 1, {"a": 1}),
                                {"jsonrpc": "2.0", "method": "notifications/x"},
                                _rpc("ask", 2)])
    assert status == 200
    assert [m["id"] for m in body] == [1, 2] and body[1]["result"] == {"asked": True}


def test_bridge_restarts_a_crashed_server_and_replays_initialize(served):
    base, session = served
    _post(base, _rpc("initialize", 1, {"clientInfo": {"name": "first"}}))
    status, body = _post(base, _rpc("crash", 2))
    assert body["error"]["message"] == "the stdio MCP server exited"
    # the next call starts a fresh process, re-initialized before it is served
    assert _post(base, _rpc("echo", 3, {"ok": True}))[1]["result"] == {"ok": True}
    assert _post(base, _rpc("initialize", 4, {}))[1]["result"]["inits"] == 1
    assert session._initialized_sent


def test_bridge_http_surface(served):
    base, _ = served
    with urllib.request.urlopen(base + "/ping", timeout=5) as resp:
        assert json.loads(resp.read()) == {"status": "Healthy"}
    for method in ("GET", "DELETE"):
        request = urllib.request.Request(base + "/mcp", method=method)
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(request, timeout=5)
        assert err.value.code == 405
    assert _post(base, _rpc("echo"), path="/other")[0] == 404
    request = urllib.request.Request(base + "/mcp", data=b"{not json", method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(request, timeout=5)
    assert err.value.code == 400
    # a client response to a server request it never got: nothing to relay
    assert _post(base, {"jsonrpc": "2.0", "id": 9, "result": {}}) == (202, None)


def test_dockerfiles_run_the_bridge_on_8000():
    for base in ("python", "node"):
        text = (byoc_dep.STDIO_BRIDGE_DIR / f"Dockerfile.{base}").read_text()
        assert "linux/arm64" in text and "EXPOSE 8000" in text
        assert "/app/mcp_stdio_bridge.py" in text and "MCP_STDIO_COMMAND" in text
    assert Path(byoc_dep.STDIO_BRIDGE_DIR / "README.md").is_file()
