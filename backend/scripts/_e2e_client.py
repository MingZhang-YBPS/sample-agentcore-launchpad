"""Shared HTTP client for the `e2e_*` scripts — authenticates when the target asks.

The scripts hit a real deployment. A local dev backend usually runs with the login
gate off, but the remote prod box runs `LAUNCHPAD_RUN_MODE=prod` with a password
set, where every `/api/*` call except health/status/login answers
`{"code": "auth.required"}`. This module is the one place that knows how to get a
session, so a script keeps its single `client = e2e_client(...)` line and works
against either.

Two traps this deliberately handles:

* **Credentials are checked before any work.** A 401 discovered halfway through a
  deploy leaves a half-built agent on real AWS, so a missing password aborts the
  run before the first mutating call.
* **The session cookie is `Secure` on prod** — `cookie_secure(settings)` is
  `auth_cookie_secure or run_mode == "prod"` (see `.trellis/spec/launchpad/console-auth.md`
  §3.4). httpx's RFC 6265 jar never attaches a `Secure` cookie to an `http://`
  request, so trusting the jar means login succeeds and every following call 401s
  whenever the base is plain HTTP — which is what these scripts use on the box
  (`http://127.0.0.1:8000`), and the only base the scripts with a hardcoded
  localhost URL can use at all. The token is therefore re-pinned as a plain
  `Cookie` header. That is a test-client relaxation only: `Secure` is a hint to
  the client, the server neither sees nor cares, and the product's cookie posture
  is untouched.

An invoke the console answers with `502 agent.upstream_error` (the model or runtime
failed before answering — live 2026-10-16 on GLM-5.3) is sent again up to
`UPSTREAM_RETRIES` times, the "send the turn again" the console tells a person.
"""

import json
import os
import time
from typing import Any, NoReturn

import httpx

SESSION_COOKIE = "launchpad_session"
# checked in order, so a runner can keep its own creds separate from the ones the
# deployment itself is configured with
USERNAME_VARS = ("LAUNCHPAD_E2E_USERNAME", "LAUNCHPAD_AUTH_USERNAME")
PASSWORD_VARS = ("LAUNCHPAD_E2E_PASSWORD", "LAUNCHPAD_AUTH_PASSWORD")
UPSTREAM_RETRIES = 2


class _RetryUpstream(httpx.BaseTransport):
    """Re-sends an invoke that died upstream (`502 agent.upstream_error`)."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(UPSTREAM_RETRIES + 1):
            response = self._inner.handle_request(request)
            if not (request.method == "POST" and request.url.path.endswith("/invoke")
                    and response.status_code == 502) or attempt == UPSTREAM_RETRIES:
                return response
            response.read()
            try:
                code = json.loads(response.content).get("code")
            except ValueError:
                code = None
            if code != "agent.upstream_error":
                return response
            print(f"  (invoke failed upstream — sending it again, "
                  f"{attempt + 1}/{UPSTREAM_RETRIES})")
            response.close()
            time.sleep(5)  # nosemgrep: arbitrary-sleep — paced retry against real AWS
        return response

    def close(self) -> None:
        self._inner.close()


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"e2e auth: {message}")


def _auth_status(client: httpx.Client) -> dict[str, Any]:
    try:
        response = client.get("/api/auth/status")
    except httpx.HTTPError as exc:
        _fail(f"cannot reach {client.base_url} — {type(exc).__name__}: {exc}")
    if response.status_code != 200:
        _fail(f"GET /api/auth/status returned HTTP {response.status_code}")
    return response.json()


def e2e_client(base: str, timeout: float = 300) -> httpx.Client:
    """Return an `httpx.Client` for `base`, logged in if the deployment requires it.

    Aborts with `SystemExit` — the failure mode the scripts already use — when the
    target needs auth and no usable credentials are available, so nothing is
    created before the problem surfaces.
    """
    client = httpx.Client(
        base_url=base, timeout=timeout, transport=_RetryUpstream(httpx.HTTPTransport())
    )

    if not _auth_status(client).get("auth_required"):
        return client  # dev deployment: behaves exactly as before this helper existed

    username = _first_env(USERNAME_VARS)
    password = _first_env(PASSWORD_VARS)
    if not (username and password):
        _fail(
            f"{base} requires authentication. Export credentials before running: "
            f"{USERNAME_VARS[0]} / {PASSWORD_VARS[0]} "
            f"(or {USERNAME_VARS[1]} / {PASSWORD_VARS[1]})."
        )

    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    if response.status_code != 200:
        # the body carries the error code; the password never gets echoed
        code = ""
        try:
            code = f" ({response.json().get('code')})"
        except ValueError:
            pass
        _fail(f"login as {username!r} failed with HTTP {response.status_code}{code}")

    token = client.cookies.get(SESSION_COOKIE)
    if not token:
        _fail(
            f"login succeeded but no {SESSION_COOKIE} cookie came back — "
            "the auth contract changed"
        )
    # drop the jar copy (it may be flagged Secure and would then be skipped on an
    # http:// base) and carry the session as an explicit header instead
    client.cookies.clear()
    client.headers["Cookie"] = f"{SESSION_COOKIE}={token}"

    status = _auth_status(client)
    if not status.get("authenticated"):
        _fail(
            "session did not survive the login round-trip "
            f"(auth_required={status.get('auth_required')}, "
            f"authenticated={status.get('authenticated')}) — the session cookie is "
            "not reaching the backend"
        )
    return client


def chat_sse(client: httpx.Client, agent_id: str, prompt: str,
             session_id: str | None = None,
             headers: dict[str, str] | None = None) -> dict[str, Any]:
    """One console chat turn over `/api/chat/{id}` (SSE) →
    {text, session_id, latency_ms, answered_by}.

    The default for every chat turn an e2e script sends: keep the sync `/invoke` only
    where the server has no streaming equivalent (e.g. an `actor_id` override) or
    the test is about the sync route's own contract. `headers` carries
    `X-Workspace` / `X-Project`.

    Use it instead of the sync `/api/agents/{id}/invoke` wherever the turn can be
    slow (a cold container, the first call to a just-redeployed endpoint): `/invoke`
    sends nothing until the answer is complete, so behind CloudFront (60 s origin
    read timeout) a slow turn becomes `504`. The chat stream sends named heartbeats
    for the whole turn (chat-streaming S1). It routes exactly like `/invoke`,
    including the `live` endpoint of a migrated agent."""
    text, sid, event, answered_by = "", session_id, None, "model"
    body: dict[str, Any] = {"prompt": prompt}
    if session_id:
        body["session_id"] = session_id
    started = time.monotonic()
    with client.stream("POST", f"/api/chat/{agent_id}", json=body, headers=headers,
                       timeout=900) as res:
        res.raise_for_status()
        for line in res.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event:
                data = json.loads(line[5:])
                if event == "meta":
                    sid = data["session_id"]
                elif event == "delta":
                    text += data["text"]
                elif event == "done":
                    answered_by = data.get("answered_by", answered_by)
                elif event == "error":
                    raise RuntimeError(f"chat turn failed: {data.get('message')!r}")
    if not sid:
        raise RuntimeError("chat stream ended without a meta event (no session id)")
    return {"text": text, "session_id": sid, "answered_by": answered_by,
            "latency_ms": int((time.monotonic() - started) * 1000)}
