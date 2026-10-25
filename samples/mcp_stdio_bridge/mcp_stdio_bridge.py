"""stdio → streamable-HTTP bridge for MCP servers that only speak stdio.

AgentCore Runtime's MCP contract is streamable HTTP on ``0.0.0.0:8000/mcp``.
This process serves that endpoint and relays every JSON-RPC message to ONE
stdio MCP server it runs as a subprocess (newline-delimited JSON on its
stdin/stdout). Standard library only, Python 3.10+, so it runs unchanged on the
managed Python runtime (Launchpad's code-zip wrapper) and in the Python / Node
base images of this directory.

The stdio command, first match wins:

    python3 mcp_stdio_bridge.py -- node build/index.js --flag
    MCP_STDIO_COMMAND='["node", "build/index.js"]'  (JSON array)
    mcp_stdio_bridge.json next to this file: {"command": "node", "args": [...]}

Limits (shown in the console wizard too):

- one subprocess per container — fine for a stateless server or a single
  session; a server that keeps per-session state would see every client as
  the same session.
- responses are plain ``application/json``: no SSE stream, so the server's
  own notifications (progress, logging) are dropped and server→client
  requests (sampling, elicitation, roots) are answered "not supported".
- ``initialize`` reaches the subprocess once; later ``initialize`` calls are
  answered from that first result, and a restarted subprocess is re-initialized
  with the first client's parameters before it serves anything else.

Other settings (env): ``MCP_BRIDGE_PORT`` (8000), ``MCP_BRIDGE_PATH`` (/mcp),
``MCP_BRIDGE_TIMEOUT`` seconds per request (300), ``MCP_STDIO_CWD`` (the
directory of this file).
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger("mcp_stdio_bridge")

HERE = Path(__file__).resolve().parent
CONFIG_FILE = "mcp_stdio_bridge.json"
MAX_BODY = 8 * 1024 * 1024
# JSON-RPC error codes
INTERNAL_ERROR = -32603
METHOD_NOT_FOUND = -32601
PARSE_ERROR = -32700
INVALID_REQUEST = -32600


def resolve_command(argv: list[str], env: dict[str, str], here: Path = HERE) -> list[str]:
    """The stdio server's argv (see the module docstring for the precedence)."""
    if "--" in argv:
        command = argv[argv.index("--") + 1:]
    elif env.get("MCP_STDIO_COMMAND"):
        command = json.loads(env["MCP_STDIO_COMMAND"])
    elif (here / CONFIG_FILE).is_file():
        config = json.loads((here / CONFIG_FILE).read_text(encoding="utf-8"))
        command = [config["command"], *config.get("args", [])]
    else:
        command = []
    if not command or not all(isinstance(part, str) and part for part in command):
        raise SystemExit(
            "mcp_stdio_bridge: no stdio command — pass it after `--`, in "
            f"MCP_STDIO_COMMAND (JSON array) or in {CONFIG_FILE}"
        )
    # the managed Python runtime does not promise `python` on PATH: run the
    # stdio server on the interpreter running the bridge
    if command[0] in ("python", "python3"):
        command = [sys.executable, *command[1:]]
    return command


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class _Pending:
    __slots__ = ("event", "message", "proc")

    def __init__(self, proc: subprocess.Popen | None) -> None:
        self.event = threading.Event()
        self.message: dict | None = None
        self.proc = proc


class StdioSession:
    """One stdio subprocess plus the bookkeeping that lets many HTTP clients share it.

    Ids are rewritten on the way in (``lp-<n>``) and restored on the way out, so
    two clients that both send ``"id": 1`` never collide."""

    def __init__(self, command: list[str], cwd: Path, timeout: float = 300.0) -> None:
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        # lifecycle: held across a restart and its re-initialize, so nothing
        # else reaches a fresh subprocess before its handshake
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()  # one whole message per stdin line
        self._proc: subprocess.Popen | None = None
        self._pending: dict[str, _Pending] = {}
        self._ids = itertools.count(1)
        self._init_params: dict | None = None
        self._init_result: dict | None = None
        self._initialized_sent = False

    # ── process lifecycle ─────────────────────────────────────────────────

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            self._ensure_started()

    def _ensure_started(self) -> None:
        """Start the subprocess if it is not running (lifecycle lock held); a
        restart re-runs the first client's handshake before anything else."""
        if self._alive():
            return
        restarted = self._proc is not None
        if restarted:
            logger.warning("stdio server exited with %s — restarting", self._proc.returncode)
        self._proc = subprocess.Popen(  # noqa: S603 — the operator's own command, no shell
            self.command,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # inherited: the server's logs land in the runtime's logs
        )
        self._initialized_sent = False
        threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True).start()
        logger.info("stdio server started · pid %s · %s", self._proc.pid, " ".join(self.command))
        if self._init_params is not None:
            self._replay_initialize()

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                logger.warning("stdio server wrote a non-JSON line: %.200s", line)
                continue
            if isinstance(message, dict):
                self._on_message(message)
        # EOF: this process is gone — fail whatever still waits on it
        for pending in list(self._pending.values()):
            if pending.proc is proc and pending.message is None:
                pending.message = _error(None, INTERNAL_ERROR, "the stdio MCP server exited")
                pending.event.set()

    def _on_message(self, message: dict) -> None:
        method = message.get("method")
        if method is None:  # a response to one of our requests
            pending = self._pending.get(str(message.get("id")))
            if pending is not None:
                pending.message = message
                pending.event.set()
            return
        if "id" in message:  # server → client request: there is no channel back
            if method == "ping":
                reply: dict = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            else:
                reply = _error(
                    message["id"], METHOD_NOT_FOUND,
                    f"'{method}' is not supported through the stdio bridge",
                )
            try:
                self._write(reply)
            except OSError:
                pass
            return
        logger.debug("dropped server notification %s", method)

    def _write(self, message: dict) -> None:
        """One newline-delimited JSON message to the subprocess."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise OSError("stdio server is not running")
        with self._write_lock:
            proc.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            proc.stdin.flush()

    # ── relaying ──────────────────────────────────────────────────────────

    def _send(self, message: dict) -> tuple[str, _Pending]:
        """Write one request under a bridge-owned id (lifecycle lock held)."""
        key = f"lp-{next(self._ids)}"
        pending = _Pending(self._proc)
        self._pending[key] = pending  # registered before the write: replies can be instant
        try:
            self._write({**message, "id": key})
        except OSError:
            self._pending.pop(key, None)
            raise
        return key, pending

    def _wait(self, key: str, pending: _Pending) -> dict:
        try:
            if not pending.event.wait(self.timeout):
                return _error(None, INTERNAL_ERROR, f"no response within {self.timeout:.0f}s")
            return pending.message or _error(None, INTERNAL_ERROR, "empty response")
        finally:
            self._pending.pop(key, None)

    def _replay_initialize(self) -> None:
        """Handshake a restarted subprocess with the first client's parameters
        (lifecycle lock held; the reader thread delivers without it)."""
        try:
            key, pending = self._send(
                {"jsonrpc": "2.0", "method": "initialize", "params": self._init_params}
            )
            response = self._wait(key, pending)
            if "result" in response:
                self._init_result = response["result"]
                self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self._initialized_sent = True
            else:
                logger.warning("re-initialize of the restarted server failed: %s", response)
        except OSError as exc:
            logger.warning("re-initialize of the restarted server failed: %s", exc)

    def request(self, message: dict) -> dict:
        """A client request → the server's response, carrying the client's id."""
        client_id = message.get("id")
        initialize = message.get("method") == "initialize"
        try:
            with self._lock:
                self._ensure_started()
                if initialize and self._init_result is not None:
                    return {"jsonrpc": "2.0", "id": client_id, "result": self._init_result}
                key, pending = self._send(message)
        except OSError as exc:
            return _error(client_id, INTERNAL_ERROR, f"stdio server unavailable: {exc}")
        response = self._wait(key, pending)
        if initialize and "result" in response:
            self._init_params = message.get("params") or {}
            self._init_result = response["result"]
        return {**response, "id": client_id}

    def notify(self, message: dict) -> None:
        """A client notification. ``notifications/initialized`` reaches the
        subprocess once per process; the rest are forwarded as they are."""
        try:
            with self._lock:
                self._ensure_started()
                if message.get("method") == "notifications/initialized":
                    if self._initialized_sent:
                        return
                    self._initialized_sent = True
                self._write(message)
        except OSError:
            logger.warning("dropped client notification %s", message.get("method"))


def handle_body(session: StdioSession, body: bytes) -> tuple[int, Any]:
    """One POST body → (HTTP status, JSON payload or None for 202)."""
    try:
        payload = json.loads(body or b"null")
    except ValueError:
        return 400, _error(None, PARSE_ERROR, "body is not JSON")
    batch = isinstance(payload, list)
    messages = payload if batch else [payload]
    if not messages or not all(isinstance(m, dict) for m in messages):
        return 400, _error(None, INVALID_REQUEST, "expected a JSON-RPC message")
    responses = []
    for message in messages:
        if "method" not in message:
            continue  # a client response to a server request we never forwarded
        if "id" in message:
            responses.append(session.request(message))
        else:
            session.notify(message)
    if not responses:
        return 202, None
    return 200, responses if batch else responses[0]


def make_handler(session: StdioSession, path: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: Any = None, headers: dict | None = None) -> None:
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> str:
            return self.path.split("?", 1)[0].rstrip("/") or "/"

        def do_POST(self) -> None:  # noqa: N802 — http.server's naming
            if self._route() != path:
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, _error(None, INVALID_REQUEST, "body too large"))
                return
            status, payload = handle_body(session, self.rfile.read(length))
            self._send(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            route = self._route()
            if route == "/ping":
                self._send(200, {"status": "Healthy"})
            elif route == path:
                # streamable HTTP lets a server refuse the GET stream; this one has none
                self._send(405, None, {"Allow": "POST"})
            else:
                self._send(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802
            # no session ids are issued, so there is no session to end
            self._send(405, None, {"Allow": "POST"})

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("%s " + fmt, self.address_string(), *args)

    return Handler


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    env = dict(os.environ)
    command = resolve_command(sys.argv[1:] if argv is None else argv, env)
    session = StdioSession(
        command,
        cwd=Path(env.get("MCP_STDIO_CWD") or HERE),
        timeout=float(env.get("MCP_BRIDGE_TIMEOUT") or 300),
    )
    session.start()  # fail early: a broken command shows in the logs at boot
    path = "/" + (env.get("MCP_BRIDGE_PATH") or "/mcp").strip("/")
    port = int(env.get("MCP_BRIDGE_PORT") or 8000)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(session, path))  # noqa: S104 — the Runtime contract
    server.daemon_threads = True

    def _shutdown(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    logger.info("serving streamable HTTP on 0.0.0.0:%s%s", port, path)
    try:
        server.serve_forever()
    finally:
        session.stop()


if __name__ == "__main__":
    main()
