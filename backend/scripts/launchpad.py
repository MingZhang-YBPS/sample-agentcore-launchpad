#!/usr/bin/env python3
"""`launchpad` - drive the console's release flow from a terminal or a CI job (T31).

Standard library only, on purpose: a customer's pipeline needs `python3` and this one
file, not a virtualenv or `uv`. It speaks the console's ordinary HTTP API.

Authentication is a console *session*: the `/api` surface is session-only (API keys
authorize the public `/v1` invoke surface, not release management). Give it either
LAUNCHPAD_USER + LAUNCHPAD_PASSWORD (it logs in) or an existing session token in
LAUNCHPAD_SESSION (the `launchpad_session` cookie value). Use a service account whose
role allows what you script: bundling and requesting a promotion need
`promotion.request`; approving stays a different person's job by design.

Environment / flags:
  LAUNCHPAD_URL        base URL of the backend           (--url, default http://127.0.0.1:8000)
  LAUNCHPAD_USER       console username                  (--user)
  LAUNCHPAD_PASSWORD   console password                  (--password; prefer the env var)
  LAUNCHPAD_SESSION    session token instead of user/password
  LAUNCHPAD_WORKSPACE  workspace the SOURCE agent lives in (--workspace)

Commands:
  bundle  --agent NAME [--output FILE] [--note TEXT]
      Freeze the agent's latest publish into a release bundle (idempotent per digest)
      and print its deterministic YAML export - commit that file to Git.
  promote --agent NAME --to WORKSPACE --change-note TEXT --rollback-note TEXT
      Bundle the agent and open a promotion request into WORKSPACE.
  compare --agent NAME
      What each workspace runs for this agent, and how it differs from the reference.
  drift   [--agent NAME]
      Ledger vs AWS for the workspace. Exits 2 when drift is found (and 3 when only
      "unknown" answers came back), so a pipeline can gate on it.

Exit codes: 0 ok - 1 request refused or transport failure - 2 drift - 3 unknown.
"""

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any, TextIO

SESSION_COOKIE = "launchpad_session"
EXIT_OK, EXIT_ERROR, EXIT_DRIFT, EXIT_UNKNOWN = 0, 1, 2, 3

# (method, url, headers, body) -> (status, response headers, body bytes)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any, bytes]]


class CliError(Exception):
    pass


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, Any, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise CliError(f"cannot reach {url}: {exc}") from exc


class Client:
    def __init__(
        self,
        base: str,
        *,
        workspace: str | None = None,
        session: str | None = None,
        transport: Transport = urllib_transport,
    ) -> None:
        self.base = base.rstrip("/")
        self.workspace = workspace
        self.session = session
        self.transport = transport

    def _headers(self, json_body: bool) -> dict[str, str]:
        headers = {"Accept": "application/json, application/yaml"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.session:
            # Pinned as a plain header rather than trusting a cookie jar: the session
            # cookie is `Secure` in prod mode and a jar never returns it over http://.
            headers["Cookie"] = f"{SESSION_COOKIE}={self.session}"
        if self.workspace:
            headers["X-Workspace"] = self.workspace
        return headers

    def raw(self, method: str, path: str, payload: Any = None) -> tuple[int, Any, bytes]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        return self.transport(method, self.base + path, self._headers(body is not None), body)

    def call(self, method: str, path: str, payload: Any = None) -> Any:
        status, _headers, body = self.raw(method, path, payload)
        if status >= 400:
            raise CliError(_describe_failure(method, path, status, body))
        return json.loads(body) if body else {}

    def text(self, path: str) -> str:
        status, _headers, body = self.raw("GET", path)
        if status >= 400:
            raise CliError(_describe_failure("GET", path, status, body))
        return body.decode("utf-8")

    def login(self, username: str, password: str) -> None:
        status, headers, body = self.raw(
            "POST", "/api/auth/login", {"username": username, "password": password}
        )
        if status >= 400:
            raise CliError(_describe_failure("POST", "/api/auth/login", status, body))
        for cookie in _set_cookies(headers):
            name, _, rest = cookie.partition("=")
            if name.strip() == SESSION_COOKIE:
                self.session = rest.split(";", 1)[0].strip()
                return
        # A backend with the login gate off answers 200 and sets nothing: no session
        # is needed there.


def _set_cookies(headers: Any) -> list[str]:
    if hasattr(headers, "get_all"):
        return list(headers.get_all("Set-Cookie") or [])
    if hasattr(headers, "get_list"):  # httpx / starlette
        return list(headers.get_list("set-cookie"))
    value = headers.get("set-cookie") if headers else None
    return [value] if value else []


def _describe_failure(method: str, path: str, status: int, body: bytes) -> str:
    try:
        data = json.loads(body)
        code, message = data.get("code", ""), data.get("message", "")
    except (ValueError, AttributeError):
        code, message = "", body[:200].decode("utf-8", "replace")
    return f"{method} {path} -> HTTP {status} {code} {message}".rstrip()


# ── commands ────────────────────────────────────────────────────────────────────


def find_agent(client: Client, name: str) -> dict[str, Any]:
    agents = client.call("GET", "/api/agents").get("agents", [])
    matches = [a for a in agents if a.get("name") == name]
    if not matches:
        raise CliError(f"no agent named '{name}' in this workspace")
    return matches[0]


def make_bundle(client: Client, name: str, note: str | None) -> dict[str, Any]:
    agent = find_agent(client, name)
    return client.call(
        "POST", f"/api/agents/{urllib.parse.quote(agent['id'])}/release-bundles",
        {"note": note} if note else {},
    )


def cmd_bundle(client: Client, args: argparse.Namespace, out: TextIO) -> int:
    bundle = make_bundle(client, args.agent, args.note)
    document = client.text(f"/api/release-bundles/{urllib.parse.quote(bundle['id'])}/export")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(document)
        print(f"wrote {args.output} (digest sha256:{bundle['digest']})", file=sys.stderr)
    else:
        out.write(document)
    return EXIT_OK


def cmd_promote(client: Client, args: argparse.Namespace, out: TextIO) -> int:
    bundle = make_bundle(client, args.agent, None)
    promotion = client.call(
        "POST",
        "/api/promotions",
        {
            "bundle_id": bundle["id"],
            "target_workspace_id": args.to,
            "change_note": args.change_note,
            "rollback_note": args.rollback_note,
        },
    )
    print(
        f"promotion {promotion.get('id')} requested: {args.agent} "
        f"(sha256:{bundle['digest'][:12]}) -> {args.to} [{promotion.get('status')}]",
        file=out,
    )
    return EXIT_OK


def cmd_compare(client: Client, args: argparse.Namespace, out: TextIO) -> int:
    result = client.call(
        "GET", "/api/environments/compare?agent=" + urllib.parse.quote(args.agent)
    )
    for row in result["environments"]:
        agent = row.get("agent") or {}
        print(
            f"{row['workspace']['id']:<16} {row['workspace']['tier']:<8} "
            f"{row['vs_reference']:<10} v{agent.get('version') or '-':<6} "
            f"{(agent.get('spec_digest') or '-')[:12]}",
            file=out,
        )
    return EXIT_OK


def cmd_drift(client: Client, args: argparse.Namespace, out: TextIO) -> int:
    path = "/api/environments/drift"
    if args.agent:
        path += "?agent=" + urllib.parse.quote(args.agent)
    result = client.call("GET", path)
    for item in result["agents"]:
        detail = ", ".join(f["code"] for f in item["findings"]) or (item.get("reason") or "")
        print(f"{item['name']:<32} {item['state']:<8} {detail}", file=out)
    print(f"workspace state: {result['state']} ({result['checked']} checked)", file=out)
    return {"drift": EXIT_DRIFT, "unknown": EXIT_UNKNOWN}.get(result["state"], EXIT_OK)


COMMANDS: dict[str, Callable[[Client, argparse.Namespace, TextIO], int]] = {
    "bundle": cmd_bundle,
    "promote": cmd_promote,
    "compare": cmd_compare,
    "drift": cmd_drift,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="launchpad", description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("LAUNCHPAD_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--user", default=os.environ.get("LAUNCHPAD_USER"))
    parser.add_argument("--password", default=os.environ.get("LAUNCHPAD_PASSWORD"))
    parser.add_argument("--workspace", default=os.environ.get("LAUNCHPAD_WORKSPACE"))
    sub = parser.add_subparsers(dest="command", required=True)

    bundle = sub.add_parser("bundle", help="freeze + export a release bundle as YAML")
    bundle.add_argument("--agent", required=True)
    bundle.add_argument("--output", "-o")
    bundle.add_argument("--note")

    promote = sub.add_parser("promote", help="request a promotion into another workspace")
    promote.add_argument("--agent", required=True)
    promote.add_argument("--to", required=True, help="target workspace id")
    promote.add_argument("--change-note", required=True)
    promote.add_argument("--rollback-note", required=True)

    compare = sub.add_parser("compare", help="what each workspace runs for an agent")
    compare.add_argument("--agent", required=True)

    drift = sub.add_parser("drift", help="ledger vs AWS; exit 2 on drift, 3 on unknown")
    drift.add_argument("--agent")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport = urllib_transport,
    out: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    client = Client(
        args.url,
        workspace=args.workspace,
        session=os.environ.get("LAUNCHPAD_SESSION"),
        transport=transport,
    )
    try:
        if not client.session and args.user and args.password:
            client.login(args.user, args.password)
        return COMMANDS[args.command](client, args, out)
    except CliError as exc:
        print(f"launchpad: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
