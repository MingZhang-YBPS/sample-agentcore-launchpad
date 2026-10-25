#!/usr/bin/env python3
"""E2E for console API areas no other e2e script exercises — LIVE workshop console.

Read-only where the data is production state, a ledger-only create→get→delete cycle
where that is safe, and no AWS writes anywhere. Covers, in order:

  1. Agent-DLC records (READ-ONLY, header `X-Project: retail-service`) for the workshop
     agent ``lab-service-assistant`` (or ``E2E_DLC_AGENT_ID``, else the first agent of
     the project with a release record; SKIP when there is none yet):
       * ``GET /api/agents/{id}/release`` — the release payload carries ``waivers`` and
         ``records``. There is no ``/api/waivers`` list / get route in the API (waivers
         are only POST-created per agent and approve/reject/revoke); this payload and
         ``gate_report`` are the authoritative read for a waiver's status / signer /
         expiry / active flag (releases.waiver_out). Verifies the K1 waiver is active
         until 2026-11-01 and its fields are consistent.
       * ``GET /api/release-records?agent_id=`` — the list (the ``agent_id`` query is
         required). Verifies status/decision, requested_by, decided_by (signer) and the
         gate verdict are present.
       * ``GET /api/release-records/{id}`` — one record, consistent with the list.
     Nothing here is created, approved, revoked or signed.

  2. Studio codegen: ``GET /api/generate-code/status`` (there is no ``POST
     /api/generate-code`` — the generate pipeline was dropped; status gates the Fix
     button) and ``POST /api/fix-code/stream`` (AI-fix SSE) with a minimal canvas and a
     deliberately broken snippet — one model call, and the stream reports a backend
     outage as an ``error`` event rather than a broken response, so an unavailable
     backend is a SKIP, not a FAIL.

  3. Tool surfaces: list ``/api/gateway-tools/lambda``, ``/api/gateway-tools/external``,
     ``/api/custom-tools/approvals`` and ``/api/custom-tools/sandbox``. The custom-tool
     approval is ledger-only (no AWS deploy), so it gets a check → approve → list →
     delete cycle on an ``e2e-`` object. Lambda / external gateway-tool creation would
     deploy to the shared Gateway, so those are list-only. ``/api/execute`` is the
     studio local-exec endpoint — refused in prod (``studio.exec.disabled``); SKIP with
     a note there, and ``/api/custom-tools/check`` is the sandboxed, side-effect-free
     code path that is exercised instead. ``/api/mcp-servers`` and ``/api/agent-skills``
     have no plain list route and no ledger-only create (import needs S3 staging); noted
     and skipped.

  4. Negatives: a plain member (biz-owner) and sme get 403 on admin-only routes the
     route policy marks ADMIN (``/api/costs``, ``/api/users``, ``/api/audit``,
     ``PUT /api/custom-tools/sandbox``).

Run:  cd backend && uv run python scripts/e2e_dlc_records_and_tools.py --base URL

Auth: logs in as ``admin`` / ``biz-owner`` / ``sme``; passwords come from the
environment (``LP_PW_ADMIN`` / ``LP_PW_BIZ_OWNER`` / ``LP_PW_SME``, or the generic
``LAUNCHPAD_E2E_PASSWORD`` for admin). Passwords are never printed. Prints PASS/FAIL per
check and a final ``RESULT: <n> passed, <m> failed``; exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

AGENT_NAME = "lab-service-assistant"  # the workshop's agent, preferred when present
PROJECT = "retail-service"
WORKSPACE = "default"
RUN = uuid.uuid4().hex[:6]
SESSION_COOKIE = "launchpad_session"

# username -> the env vars its password may live under (checked in order)
ROLE_PASSWORD_VARS: dict[str, tuple[str, ...]] = {
    "admin": ("LP_PW_ADMIN", "LAUNCHPAD_E2E_PASSWORD", "LAUNCHPAD_AUTH_PASSWORD"),
    "biz-owner": ("LP_PW_BIZ_OWNER",),
    "sme": ("LP_PW_SME",),
}

RESULTS: list[tuple[str, str, str]] = []
# the one object this run creates, torn down in `finally`
CREATED_APPROVAL: dict[str, Any] = {"client": None, "code_hash": None}


def check(name: str, ok: bool, evidence: str = "") -> bool:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}"[:400], flush=True)
    return ok


def skip(name: str, why: str) -> None:
    RESULTS.append((name, "SKIP", why))
    print(f"── {name}: SKIP · {why}", flush=True)


def summary() -> int:
    passed = sum(v == "PASS" for _, v, _ in RESULTS)
    failed = sum(v == "FAIL" for _, v, _ in RESULTS)
    skipped = sum(v == "SKIP" for _, v, _ in RESULTS)
    print("\n═══ summary ═══")
    for name, verdict, evidence in RESULTS:
        print(f"{verdict:<5} {name:<42} {evidence[:110]}")
    print(f"\nRESULT: {passed} passed, {failed} failed ({skipped} skipped)")
    return 1 if failed else 0


def body(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError:
        return {}


def code(res: httpx.Response) -> str:
    data = body(res)
    extra = data.get("code", "") if isinstance(data, dict) else ""
    return f"HTTP {res.status_code} {extra}".strip()


def _password_for(user: str) -> str | None:
    for var in ROLE_PASSWORD_VARS[user]:
        value = os.environ.get(var)
        if value:
            return value
    return None


def login(base: str, user: str) -> httpx.Client:
    """A logged-in client for `user`, with the session pinned as a plain Cookie header
    (it is `Secure` on prod, which an httpx jar would then drop on an http:// base —
    see scripts/_e2e_client)."""
    password = _password_for(user)
    if not password:
        raise SystemExit(
            f"e2e auth: no password for {user!r} in the environment "
            f"({' / '.join(ROLE_PASSWORD_VARS[user])}). Source fresh-accounts.env first."
        )
    client = httpx.Client(base_url=base, timeout=120)
    res = client.post("/api/auth/login", json={"username": user, "password": password})
    if res.status_code != 200:
        raise SystemExit(f"e2e auth: login as {user!r} failed with {code(res)}")
    token = client.cookies.get(SESSION_COOKIE)
    if not token:
        raise SystemExit(f"e2e auth: login as {user!r} returned no session cookie")
    client.cookies.clear()
    client.headers["Cookie"] = f"{SESSION_COOKIE}={token}"
    client.headers["X-Workspace"] = WORKSPACE
    return client


# ── 1. Agent-DLC records (read-only) ────────────────────────────────────────────


def find_dlc_agent(admin: httpx.Client) -> str | None:
    """The workshop agent whose release records are read: `E2E_DLC_AGENT_ID`, else
    `lab-service-assistant`, else the first agent of the project with a release
    record. None while the workshop has not reached its release chapter (a fresh
    console has no workshop agent at all)."""
    hp = {"X-Project": PROJECT}
    if os.environ.get("E2E_DLC_AGENT_ID"):
        return os.environ["E2E_DLC_AGENT_ID"]
    res = admin.get("/api/agents", headers=hp)
    agents = [a for a in (body(res).get("agents") or []) if a.get("status") != "deleted"]
    agents.sort(key=lambda a: a.get("name") != AGENT_NAME)
    for agent in agents:
        listed = admin.get("/api/release-records", params={"agent_id": agent["id"]},
                           headers=hp)
        if listed.status_code == 200 and body(listed).get("records"):
            return agent["id"]
    return None


def dlc_records(admin: httpx.Client) -> None:
    hp = {"X-Project": PROJECT}
    agent_id = find_dlc_agent(admin)
    if agent_id is None:
        skip("release.payload", f"no agent with release records in {PROJECT} "
             "(the workshop has not reached its release chapter on this console)")
        return
    print(f"── release records of agent {agent_id}", flush=True)

    res = admin.get(f"/api/agents/{agent_id}/release", headers=hp)
    payload = body(res)
    waivers = payload.get("waivers") or []
    records = payload.get("records") or []
    check("release.payload", res.status_code == 200 and isinstance(waivers, list)
          and isinstance(records, list),
          f"{code(res)} waivers={len(waivers)} records={len(records)}")
    if res.status_code != 200:
        return

    # every waiver names its fields (status, signer, expiry, active flag)
    field_keys = {"criterion_key", "status", "risk_owner", "approved_by",
                  "expires_on", "active", "expired"}
    well_formed = all(field_keys <= set(w) for w in waivers) if waivers else False
    check("waiver.fields_present", well_formed,
          f"{len(waivers)} waivers, keys ok={well_formed}")

    # the K1 waiver: active, expiring 2026-11-01, consistent (approved has a signer)
    k1 = next((w for w in waivers if w.get("criterion_key") == "K1"), None)
    if k1 is None:
        skip("waiver.k1_active", "no K1 waiver on the agent (workshop step not done)")
    else:
        expires = str(k1.get("expires_on") or "")
        consistent = (k1.get("status") != "approved") or bool(k1.get("approved_by"))
        check("waiver.k1_active",
              bool(k1.get("active")) and not k1.get("expired")
              and expires.startswith("2026-11-01") and consistent,
              f"status={k1.get('status')} active={k1.get('active')} "
              f"signer={k1.get('approved_by')!r} expires={expires[:10]}")

    # the agent's release records as embedded in the payload: decision + gate verdict
    embedded_ok = all({"decision", "requested_by", "gate_report"} <= set(r)
                      for r in records) if records else False
    verdicts = {(r.get("gate_report") or {}).get("verdict") for r in records}
    check("release.records_embedded", embedded_ok and bool(records),
          f"{len(records)} records, verdicts={sorted(v for v in verdicts if v)}")

    # /api/release-records?agent_id= — the list endpoint (agent_id is required)
    res = admin.get("/api/release-records", params={"agent_id": agent_id}, headers=hp)
    listed = (body(res).get("records") or []) if res.status_code == 200 else []
    list_fields = all({"id", "decision", "requested_by", "decided_by", "gate_report"}
                      <= set(r) for r in listed) if listed else False
    check("release_records.list", res.status_code == 200 and bool(listed) and list_fields,
          f"{code(res)} n={len(listed)} fields_ok={list_fields}")

    # agent_id is required — omitting it is a 422, not a cross-agent dump
    res = admin.get("/api/release-records", headers=hp)
    check("release_records.list_requires_agent", res.status_code == 422, code(res))

    if not listed:
        skip("release_records.get_one", "no release records to fetch")
        return
    # a released record carries a signer and a PASS verdict — the consistency the task asks
    released = next((r for r in listed if r.get("decision") == "released"), listed[0])
    rid = released["id"]
    res = admin.get(f"/api/release-records/{rid}", headers=hp)
    one = body(res)
    same = one.get("id") == rid and one.get("decision") == released.get("decision") \
        and (one.get("gate_report") or {}).get("verdict") == \
        (released.get("gate_report") or {}).get("verdict")
    verdict = (one.get("gate_report") or {}).get("verdict")
    signer = one.get("decided_by")
    verdict_consistent = (one.get("decision") != "released") or (
        verdict == "PASS" and bool(signer))
    check("release_records.get_one", res.status_code == 200 and same and verdict_consistent,
          f"{code(res)} decision={one.get('decision')} verdict={verdict} signer={signer!r}")

    # a made-up record id is a clean 404 scoped to the workspace
    res = admin.get(f"/api/release-records/doesnotexist{RUN}", headers=hp)
    check("release_records.get_missing_404", res.status_code == 404, code(res))


# ── 2. Studio codegen (generate-code status + fix-code) ──────────────────────────


def codegen(admin: httpx.Client) -> None:
    hp = {"X-Project": PROJECT}
    res = admin.get("/api/generate-code/status", headers=hp)
    status = body(res)
    available = bool(status.get("available"))
    check("codegen.status", res.status_code == 200 and "available" in status,
          f"{code(res)} backend={status.get('backend')} available={available} "
          f"reason={str(status.get('reason'))[:60]!r}")

    # a minimal single-node canvas + a deliberately broken snippet (one model call).
    # the fix-code stream frames a backend outage as an `error` event and still closes
    # cleanly, so we PASS on a clean terminal stream and SKIP if the backend is down.
    broken = "def handler(event)\n    return 'missing colon'\n"  # SyntaxError on purpose
    flow = {
        "nodes": [{"id": "n1", "type": "agent",
                   "data": {"label": "e2e", "model": "", "system_prompt": "x"},
                   "position": {"x": 0, "y": 0}}],
        "edges": [],
    }
    payload = {"code": broken, "error": "SyntaxError: invalid syntax (line 1)",
               "flow_data": flow, "graph_mode": False}
    events: list[str] = []
    saw_error = saw_done = saw_end = False
    err_message = ""
    try:
        with admin.stream("POST", "/api/fix-code/stream", json=payload,
                          headers=hp, timeout=180) as res:
            if res.status_code != 200:
                check("fix_code.stream", False, code(res))
                return
            event = None
            for line in res.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    events.append(event)
                    saw_error = saw_error or event == "error"
                    saw_done = saw_done or event == "done"
                    saw_end = saw_end or event == "end"
                elif line.startswith("data:") and event == "error":
                    try:
                        err_message = json.loads(line[5:]).get("message", "")
                    except ValueError:
                        pass
    except (httpx.HTTPError, RuntimeError) as exc:
        check("fix_code.stream", False, f"{type(exc).__name__}: {exc}"[:160])
        return

    if saw_error and not saw_done and ("unavailable" in err_message.lower()
                                       or not available):
        skip("fix_code.stream", f"coding-agent backend unavailable: {err_message[:80]!r}")
        return
    # a healthy stream reaches a terminal frame (done and/or the always-sent end)
    check("fix_code.stream", saw_done or saw_end,
          f"events={events[:8]} done={saw_done} end={saw_end} "
          f"error={saw_error} msg={err_message[:60]!r}")


# ── 3. tool surfaces ─────────────────────────────────────────────────────────────


def tool_surfaces(admin: httpx.Client) -> None:
    hp = {"X-Project": PROJECT}

    for name, path in (
        ("gateway_tools.lambda", "/api/gateway-tools/lambda"),
        ("gateway_tools.external", "/api/gateway-tools/external"),
        ("custom_tools.approvals", "/api/custom-tools/approvals"),
    ):
        res = admin.get(path, headers=hp)
        data = body(res)
        listed = data.get("targets") if "targets" in data else data.get("approvals")
        check(f"{name}.list", res.status_code == 200 and isinstance(listed, list),
              f"{code(res)} n={len(listed) if isinstance(listed, list) else '?'}")

    res = admin.get("/api/custom-tools/sandbox", headers=hp)
    check("custom_tools.sandbox_read", res.status_code == 200
          and "allowed_tools" in body(res), code(res))

    # Lambda / external gateway-tool creation would deploy to the shared Gateway.
    skip("gateway_tools.create", "create deploys to the shared AgentCore Gateway — list/get only")

    # custom-tool approval: ledger-only (no AWS). check → approve → list → delete.
    # the tool name is the decorated function's name, so it must be a Python identifier
    # (no hyphens) — the `e2e_` prefix marks it; the note/description carry `e2e`.
    tool_name = f"e2e_echo_{RUN}"
    tool_code = (
        "from strands import tool\n\n"
        "@tool\n"
        f"def {tool_name}(text: str) -> str:\n"
        '    """Echo the given text back (e2e)."""\n'
        "    return text\n"
    )
    chk = admin.post("/api/custom-tools/check", headers=hp, json={
        "name": tool_name, "code": tool_code, "requirements": [], "execution": "runtime"})
    chk_body = body(chk)
    check("custom_tools.check", chk.status_code == 200 and chk_body.get("ok") is True
          and bool(chk_body.get("code_hash")),
          f"{code(chk)} ok={chk_body.get('ok')} schema={bool(chk_body.get('input_schema'))}")

    ap = admin.post("/api/custom-tools/approvals", headers=hp, json={
        "name": tool_name, "code": tool_code, "requirements": [], "execution": "runtime",
        "description": "e2e temporary approval — deleted by this script", "note": f"e2e {RUN}"})
    ap_body = body(ap)
    code_hash = ap_body.get("code_hash")
    if ap.status_code == 201 and code_hash:
        CREATED_APPROVAL["client"] = admin
        CREATED_APPROVAL["code_hash"] = code_hash
    check("custom_tools.approve", ap.status_code == 201 and bool(code_hash)
          and ap_body.get("approved_by") == "admin",
          f"{code(ap)} hash={str(code_hash)[:12]} by={ap_body.get('approved_by')}")

    if not code_hash:
        skip("custom_tools.list_after_approve", "approval was not created")
        skip("custom_tools.delete", "approval was not created")
        return

    lst = body(admin.get("/api/custom-tools/approvals", headers=hp)).get("approvals", [])
    check("custom_tools.list_after_approve",
          any(a.get("code_hash") == code_hash for a in lst),
          f"approval present among {len(lst)}")

    dele = admin.delete(f"/api/custom-tools/approvals/{code_hash}", headers=hp)
    deleted = dele.status_code == 200 and body(dele).get("revoked") == code_hash
    if deleted:
        CREATED_APPROVAL["code_hash"] = None  # cleaned up already
    after = body(admin.get("/api/custom-tools/approvals", headers=hp)).get("approvals", [])
    check("custom_tools.delete",
          deleted and not any(a.get("code_hash") == code_hash for a in after),
          f"{code(dele)} gone={not any(a.get('code_hash') == code_hash for a in after)}")

    # /api/execute: studio local code-exec, refused in prod — the sandboxed, no-side-effect
    # path is /api/custom-tools/check above. Try it; a prod refusal is the expected SKIP.
    ex = admin.post("/api/execute", headers=hp, json={"code": "print('e2e')"})
    ex_code = body(ex).get("code") if isinstance(body(ex), dict) else ""
    if ex.status_code in (403, 404, 409) or ex_code == "studio.exec.disabled":
        skip("execute", f"studio local exec disabled in this deployment ({code(ex)})")
    else:
        out = body(ex)
        check("execute", ex.status_code == 200 and out.get("success") is True
              and "e2e" in str(out.get("output", "")),
              f"{code(ex)} success={out.get('success')} out={str(out.get('output'))[:40]!r}")

    # /api/mcp-servers and /api/agent-skills: no plain list route, no ledger-only create
    skip("mcp_servers.list", "no plain /api/mcp-servers list route; reviews are agent-scoped only")
    skip("agent_skills.create", "no list route; /api/agent-skills/import needs S3 staging (deploy)")


# ── 4. negative permission checks ────────────────────────────────────────────────


def negatives(biz: httpx.Client, sme: httpx.Client) -> None:
    hp = {"X-Project": PROJECT}
    # admin-only routes (route_policy: ADMIN). A workspace member gets 403, not 401/404.
    cases = (
        ("biz.costs_403", biz, "GET", "/api/costs", {}),
        ("biz.users_403", biz, "GET", "/api/users", {}),
        ("biz.audit_403", biz, "GET", "/api/audit", {}),
        ("sme.costs_403", sme, "GET", "/api/costs", {}),
        ("sme.sandbox_put_403", sme, "PUT", "/api/custom-tools/sandbox",
         {"json": {"allowed_tools": []}}),
    )
    for name, client, method, path, kw in cases:
        res = client.request(method, path, headers=hp, **kw)
        check(name, res.status_code == 403, code(res))


# ── cleanup ──────────────────────────────────────────────────────────────────────


def cleanup() -> None:
    client = CREATED_APPROVAL.get("client")
    code_hash = CREATED_APPROVAL.get("code_hash")
    if client is None or not code_hash:
        return
    try:
        res = client.delete(f"/api/custom-tools/approvals/{code_hash}",
                            headers={"X-Project": PROJECT})
        print(f"── cleanup: deleted leftover approval {str(code_hash)[:12]} ({code(res)})",
              flush=True)
    except httpx.HTTPError as exc:
        print(f"── cleanup: could not delete approval {str(code_hash)[:12]}: {exc}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()

    started = time.monotonic()
    admin = login(args.base, "admin")
    biz = login(args.base, "biz-owner")
    sme = login(args.base, "sme")
    print(f"── base {args.base} · project {PROJECT} · run {RUN}", flush=True)
    try:
        for leg in (
            lambda: dlc_records(admin),
            lambda: codegen(admin),
            lambda: tool_surfaces(admin),
            lambda: negatives(biz, sme),
        ):
            try:
                leg()
            except Exception as exc:  # noqa: BLE001 — record, keep going, then clean up
                check(f"unexpected:{getattr(leg, '__name__', 'leg')}", False,
                      f"{type(exc).__name__}: {exc}"[:200])
    finally:
        cleanup()
        for client in (admin, biz, sme):
            client.close()
    print(f"\n(elapsed {time.monotonic() - started:.0f}s)")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
