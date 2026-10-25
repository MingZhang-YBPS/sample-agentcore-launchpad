"""Network probe (design projects §12.7): run a tiny throwaway runtime inside a
profile's subnets and security groups and, from there, try each declared private
target — DNS resolution, a TCP connect, and (port 443 / `tls: true`) a TLS handshake.

The service (`network_profiles.start_probe`) only knows the `ProbeRunner` protocol:
`run(ctx, profile, targets) -> [per-target result]`. Tests inject a fake; the default
`AgentCoreProbeRunner` is the system-preset-style runtime (`system_key =
"network-probe"`): it uploads `PROBE_SOURCE` as a zip, creates a VPC-mode code runtime,
waits for READY, invokes it once and deletes it (and the zip) whatever happened.

UNVERIFIED LIVE: the default runner has not been run against a real account. Its
request shapes follow `services/agentcore/runtime.py` (CreateAgentRuntime with a code
artifact, InvokeAgentRuntime); the stdlib HTTP server in `PROBE_SOURCE` assumes the
code-runtime contract of `GET /ping` + `POST /invocations` on port 8080 without the
AgentCore SDK. Check both on the first live run.
"""

import io
import json
import logging
import socket
import ssl
import time
import uuid
import zipfile
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SYSTEM_KEY = "network-probe"
CONNECT_TIMEOUT_S = 5.0
READY_TIMEOUT_S = 900


class ProbeRunner(Protocol):
    def run(self, ctx: Any, profile: dict[str, Any],
            targets: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


# ── what runs inside the VPC (also importable here, so its logic is unit-tested) ──


def probe_target(target: dict[str, Any], *, timeout: float = CONNECT_TIMEOUT_S,
                 resolver: Any = socket.getaddrinfo,
                 connector: Any = socket.create_connection) -> dict[str, Any]:
    """DNS + TCP (+ TLS) for one `{cidr|host, port, tls?}` target. A CIDR target is
    probed at its first host address (a declared range has no single host)."""
    port = int(target["port"])
    host = target.get("host")
    out: dict[str, Any] = {"target": host or target.get("cidr"), "port": port}
    if not host:
        import ipaddress

        net = ipaddress.ip_network(str(target["cidr"]), strict=False)
        host = str(next(net.hosts(), net.network_address))
        out["address"] = host
        out["dns"] = "skipped"
    else:
        try:
            infos = resolver(host, port, proto=socket.IPPROTO_TCP)
            out["dns"] = "ok"
            out["address"] = infos[0][4][0] if infos else None
        except OSError as exc:
            out.update(dns="failed", tcp="skipped", error=f"DNS: {exc}")
            return out
    started = time.monotonic()
    try:
        sock = connector((out.get("address") or host, port), timeout=timeout)
    except OSError as exc:
        out.update(tcp="failed", error=f"TCP: {exc}")
        return out
    out["tcp"] = "ok"
    out["connect_ms"] = round((time.monotonic() - started) * 1000)
    try:
        if target.get("tls", port == 443):
            context = ssl.create_default_context()
            # the default already refuses SSLv3; say TLS 1.2+ outright (CodeQL py/insecure-protocol)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            with context.wrap_socket(sock, server_hostname=target.get("host") or None) as tls:
                out["tls"] = "ok"
                out["tls_version"] = tls.version()
    except (OSError, ssl.SSLError) as exc:
        out.update(tls="failed", error=f"TLS: {exc}")
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return out


def probe_all(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [probe_target(t) for t in targets]


# The runtime's main.py: this module's two probe functions plus a stdlib HTTP
# server — no dependencies, so the zip needs no requirements install.
_SERVER = '''

import json as _json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = _json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"status": "Healthy"}) if self.path == "/ping" else self._send(404, {})

    def do_POST(self):
        if self.path != "/invocations":
            self._send(404, {})
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = _json.loads(self.rfile.read(length) or b"{}")
        self._send(200, {"results": probe_all(payload.get("targets") or [])})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), _Handler).serve_forever()
'''


def probe_source() -> str:
    import inspect

    header = ("import ipaddress, socket, ssl, time\n"
              f"CONNECT_TIMEOUT_S = {CONNECT_TIMEOUT_S}\n\n")
    return (header + inspect.getsource(probe_target) + "\n\n"
            + inspect.getsource(probe_all) + _SERVER)


def probe_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", probe_source())
    return buf.getvalue()


# ── the default runner (live; unverified) ─────────────────────────────────────


class AgentCoreProbeRunner:
    """Create → wait → invoke → delete a VPC-mode code runtime in the workspace."""

    def __init__(self, sleeper: Any = time.sleep, ready_timeout_s: int = READY_TIMEOUT_S):
        self._sleep = sleeper
        self._ready_timeout_s = ready_timeout_s

    def run(self, ctx: Any, profile: dict[str, Any],
            targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from app.services.agentcore import runtime as rt

        bucket = (ctx.resources or {}).get("artifacts_bucket")
        role_arn = (ctx.resources or {}).get("execution_role_arn")
        if not bucket or not role_arn:
            raise RuntimeError("the workspace has no artifacts bucket / execution role yet — "
                               "finish its bootstrap before probing")
        suffix = uuid.uuid4().hex[:10]
        key = f"system/{SYSTEM_KEY}/{profile['id']}/{suffix}.zip"
        s3 = ctx.client("s3")
        control = ctx.client("bedrock-agentcore-control")
        s3.put_object(Bucket=bucket, Key=key, Body=probe_zip())
        runtime_id = None
        try:
            created = control.create_agent_runtime(
                agentRuntimeName=f"launchpad_netprobe_{suffix}",
                agentRuntimeArtifact=rt._code_artifact(bucket, key, instrument=False),
                networkConfiguration=rt._network_configuration({
                    "subnets": profile["subnet_ids"],
                    "security_groups": profile["security_group_ids"],
                }),
                roleArn=role_arn,
                description=f"Launchpad network probe for {profile['id']} (temporary)",
            )
            runtime_id = created["agentRuntimeId"]
            detail = rt.wait_runtime_ready(control, runtime_id, timeout_s=self._ready_timeout_s,
                                           interval_s=10, sleeper=self._sleep)
            reply = ctx.client("bedrock-agentcore").invoke_agent_runtime(
                agentRuntimeArn=detail.get("agentRuntimeArn") or created["agentRuntimeArn"],
                runtimeSessionId=rt.new_session_id(),
                payload=json.dumps({"targets": targets}).encode(),
                contentType="application/json", accept="application/json",
            )
            body = reply.get("response")
            raw = body.read() if hasattr(body, "read") else body
            return list(json.loads(raw or b"{}").get("results") or [])
        finally:
            if runtime_id is not None:
                try:
                    rt.delete_runtime(control, runtime_id)
                except Exception:  # noqa: BLE001 — teardown is best effort, logged
                    logger.warning("network probe: could not delete runtime %s", runtime_id,
                                   exc_info=True)
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:  # noqa: BLE001
                logger.warning("network probe: could not delete s3://%s/%s", bucket, key)
