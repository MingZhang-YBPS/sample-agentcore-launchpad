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

**The suite runs in one business project.** Every new agent must be filed under an
active, department-owned, non-default project bound to its workspace
(`app/services/agent_project.py`, 422 `agent.project_*`), and a request without
`X-Project` resolves to the workspace *default* project. So the client sends
`X-Project: <e2e project>` on every request to the suite's workspace (`default`) that
does not name a project itself — the way the V3 console stamps its project picker's
choice. The project is `LAUNCHPAD_E2E_PROJECT` when set, else a dedicated `e2e-lab`
("E2E lab") in department `e2e-dept` ("E2E"), created on first use, bound to the
default workspace, with the e2e member user (`LAUNCHPAD_E2E_MEMBER_USERNAME`) as a
`developer`. It is never one of the workshop projects, whose agents show up in the
workshop screenshots. A request naming another workspace gets no header added: a
project is bound per workspace, and the script that targets one picks its project.
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

PROJECT_VAR = "LAUNCHPAD_E2E_PROJECT"
MEMBER_USERNAME_VAR = "LAUNCHPAD_E2E_MEMBER_USERNAME"
E2E_PROJECT_ID = "e2e-lab"
E2E_PROJECT_NAME = "E2E lab"
E2E_DEPARTMENT_ID = "e2e-dept"
E2E_DEPARTMENT_NAME = "E2E"
E2E_WORKSPACE = "default"
_project_memo: dict[str, str] = {}  # base url -> project id, ensured once per process


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


def e2e_client(base: str, timeout: float = 300, project: bool = True) -> httpx.Client:
    """Return an `httpx.Client` for `base`, logged in if the deployment requires it.

    Aborts with `SystemExit` — the failure mode the scripts already use — when the
    target needs auth and no usable credentials are available, so nothing is
    created before the problem surfaces. With `project` (the default) the client is
    pinned to the suite's project (module docstring); the caller must be an
    administrator the first time the project has to be created.
    """
    client = httpx.Client(
        base_url=base, timeout=timeout, transport=_RetryUpstream(httpx.HTTPTransport())
    )

    if _auth_status(client).get("auth_required"):
        _login(client, base)
    if project:
        pin_project(client, ensure_e2e_project(client))
    return client


def _login(client: httpx.Client, base: str) -> None:
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


# ── the suite's business project ──────────────────────────────────────────────


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def _must(response: httpx.Response, what: str, *ok: int) -> Any:
    data = _json(response)
    if response.status_code not in (ok or (200,)):
        tag = data.get("code", "") if isinstance(data, dict) else ""
        _fail(f"e2e project setup: {what} returned HTTP {response.status_code} {tag}".strip())
    return data


def ensure_e2e_project(client: httpx.Client, workspace: str = E2E_WORKSPACE) -> str:
    """The suite's project id: `LAUNCHPAD_E2E_PROJECT`, else `e2e-lab` — created
    (with its department), unarchived, bound to `workspace` and given the e2e member
    as a developer when any of that is missing. Idempotent; once per process."""
    explicit = os.environ.get(PROJECT_VAR, "").strip()
    if explicit:
        return explicit
    key = f"{client.base_url}|{workspace}"
    if key in _project_memo:
        return _project_memo[key]

    depts = _must(client.get("/api/departments"), "GET /api/departments")
    dept = next((d for d in depts.get("departments", []) if d.get("id") == E2E_DEPARTMENT_ID),
                None)
    if dept is None:
        _must(client.post("/api/departments", json={
            "id": E2E_DEPARTMENT_ID, "name": E2E_DEPARTMENT_NAME,
            "description": "Department of the live e2e suite's project (kept between runs)",
        }), "POST /api/departments", 201)
    elif dept.get("archived"):
        _must(client.post(f"/api/departments/{E2E_DEPARTMENT_ID}/unarchive"),
              "unarchive the e2e department")

    pid = E2E_PROJECT_ID
    res = client.get(f"/api/projects/{pid}")
    if res.status_code == 404:
        project = _must(client.post("/api/projects", json={
            "id": pid, "name": E2E_PROJECT_NAME, "department_id": E2E_DEPARTMENT_ID,
            "description": "Business project the live e2e suite files its agents under "
                           "(kept between runs; every run deletes its own resources)",
            "workspaces": [workspace],
        }), "POST /api/projects", 201)
    else:
        project = _must(res, f"GET /api/projects/{pid}")
    if project.get("status") == "archived":
        project = _must(client.post(f"/api/projects/{pid}/unarchive"),
                        "unarchive the e2e project")
    if project.get("department_id") != E2E_DEPARTMENT_ID:
        _must(client.patch(f"/api/projects/{pid}", json={"department_id": E2E_DEPARTMENT_ID}),
              "move the e2e project into its department")
    bound = {w.get("workspace_id") for w in project.get("workspaces") or []}
    if workspace not in bound:
        _must(client.put(f"/api/projects/{pid}/workspaces", json={"bind": [workspace]}),
              f"bind the e2e project to {workspace}")

    member = os.environ.get(MEMBER_USERNAME_VAR, "").strip()
    if member:
        add_project_member(client, pid, username=member)
    _project_memo[key] = pid
    return pid


def add_project_member(client: httpx.Client, project_id: str, *, username: str | None = None,
                       user_id: str | None = None, role_key: str = "developer",
                       overrides: dict[str, bool] | None = None) -> None:
    """Make a user (by id, or by username) a `role_key` member of `project_id` —
    for the member accounts a script signs in as, so they reach the suite's project.
    `overrides` grants / withholds single keys on top of the role, the way a
    workspace grant's permission overrides do in the default project."""
    if user_id is None:
        users = _must(client.get("/api/users", params={"q": username, "limit": 200}),
                      "GET /api/users").get("items", [])
        user_id = next((u["id"] for u in users if u.get("username") == username), None)
        if user_id is None:
            _fail(f"e2e project setup: user {username!r} not found")
    members = _must(client.get(f"/api/projects/{project_id}/members"),
                    "GET project members").get("members", [])
    if any(m.get("user_id") == user_id
           and (m.get("direct") or {}).get("role_key") == role_key
           and ((m.get("direct") or {}).get("overrides") or {}) == (overrides or {})
           for m in members):
        return
    upsert: dict[str, Any] = {"user_id": user_id, "role_key": role_key}
    if overrides:
        upsert["overrides"] = overrides
    _must(client.put(f"/api/projects/{project_id}/members", json={"upsert": [upsert]}),
          "PUT project members")


def pin_project(client: httpx.Client, project_id: str,
                workspaces: tuple[str, ...] = (E2E_WORKSPACE,)) -> httpx.Client:
    """Send `X-Project: project_id` on every request to one of `workspaces` (no
    `X-Workspace` header = the default workspace) that does not name its own project."""

    def stamp(request: httpx.Request) -> None:
        if "x-project" in request.headers:
            return
        if request.headers.get("x-workspace", E2E_WORKSPACE) in workspaces:
            request.headers["X-Project"] = project_id

    hooks = client.event_hooks.get("request", [])
    client.event_hooks["request"] = [*hooks, stamp]
    return client


def bind_project_workspace(client: httpx.Client, workspace: str, *, bind: bool = True,
                           project_id: str | None = None) -> int:
    """Bind (or unbind) the suite's project to a throwaway workspace a script created
    — a promotion's target must be bound to the project it is released from. Returns
    the HTTP status (an unbind of a workspace already gone is not an error here)."""
    key = "bind" if bind else "unbind"
    res = client.put(f"/api/projects/{project_id or e2e_project_id()}/workspaces",
                     json={key: [workspace]})
    return res.status_code


def e2e_project_id() -> str:
    """The suite's project id for headers / bodies (after `e2e_client` ensured it)."""
    return os.environ.get(PROJECT_VAR, "").strip() or E2E_PROJECT_ID


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
