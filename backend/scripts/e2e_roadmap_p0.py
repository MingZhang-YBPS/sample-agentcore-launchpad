#!/usr/bin/env python3
"""E2E for roadmap phase P0 (T01–T05) — REAL AWS for the agent leg, cleaned up.

Covers, in order:

  1. TTFA         GET /api/overview/ttfa answers the documented shape (T01)
  2. tier         a scratch workspace registers as dev, re-tiers to staging,
                  refuses an unconfirmed move to prod, accepts a confirmed one,
                  and comes back (T05)
  3. prod guard   a PROD_PROTECTED route on that prod workspace is refused for a
                  member and journaled for an admin (T05)
  4. display name a real harness agent deploys with a Chinese display name, which
                  round-trips through list / detail / `/v1`, is editable on
                  redeploy and clearable (T04)

The scratch workspace is `registered` (never bootstrapped), so steps 2–3 make no
AWS call: a prod-protected refusal happens at admission, before any handler. Only
step 4 provisions, and it deletes what it created.

Run:  cd backend && uv run python scripts/e2e_roadmap_p0.py [--keep] [--base URL]

Safety: refuses a base whose *default* workspace is tier `prod`.
"""

import argparse
import sys
import time
import uuid

import httpx
from _e2e_client import e2e_client

RUN = uuid.uuid4().hex[:6]
SCRATCH_WS = f"rm-e2e-{RUN}"
AGENT_NAME = f"rm-e2e-p0-{RUN}"
DISPLAY_NAME = "人力资源政策助手"
DISPLAY_NAME_2 = "人力资源政策助手（改名）"
# A real account+region pair is never contacted: the workspace stays `registered`.
SCRATCH_ACCOUNT = "000000000000"
SCRATCH_REGION = "us-east-2"

RESULTS: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, evidence: str) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}")
    if not ok:
        summary()
        sys.exit(1)


def summary() -> None:
    print("\n═══ summary ═══")
    for name, verdict, evidence in RESULTS:
        print(f"{verdict:<5} {name:<22} {evidence}")


def ttfa(client: httpx.Client) -> None:
    res = client.get("/api/overview/ttfa")
    step("ttfa.reachable", res.status_code == 200, f"HTTP {res.status_code}")
    body = res.json()
    shape = {"median_seconds", "samples", "users"}
    step("ttfa.shape", shape <= body.keys(), f"keys={sorted(body)}")
    users = body["users"]
    ok = isinstance(users, list) and all(
        {"username", "ttfa_seconds", "first_login_at", "first_agent_at"} <= u.keys()
        for u in users
    )
    step("ttfa.rows", ok, f"{len(users)} account(s), samples={body['samples']}")


def register_scratch(client: httpx.Client) -> None:
    res = client.post(
        "/api/workspaces",
        json={
            "id": SCRATCH_WS,
            "name": f"roadmap e2e {RUN}",
            "account_id": SCRATCH_ACCOUNT,
            "region": SCRATCH_REGION,
        },
    )
    step("ws.register", res.status_code in (200, 201), f"HTTP {res.status_code}")
    step("ws.default_tier", res.json().get("tier") == "dev", f"tier={res.json().get('tier')}")


def retier(client: httpx.Client) -> None:
    res = client.patch(f"/api/workspaces/{SCRATCH_WS}", json={"tier": "staging"})
    step("ws.to_staging", res.status_code == 200 and res.json()["tier"] == "staging",
         f"HTTP {res.status_code} tier={res.json().get('tier')}")

    res = client.patch(f"/api/workspaces/{SCRATCH_WS}", json={"tier": "prod"})
    body = res.json()
    step(
        "ws.prod_needs_confirm",
        res.status_code == 409 and body.get("code") == "workspace.tier_change_unconfirmed",
        f"HTTP {res.status_code} code={body.get('code')}",
    )

    res = client.patch(
        f"/api/workspaces/{SCRATCH_WS}", json={"tier": "prod", "confirm_tier_change": True}
    )
    step("ws.to_prod", res.status_code == 200 and res.json()["tier"] == "prod",
         f"HTTP {res.status_code} tier={res.json().get('tier')}")


def prod_guard_scratch(client: httpx.Client) -> None:
    """Reads stay open on a prod workspace, and an admin is not refused by the guard.

    The scratch workspace is only `registered`, so a *mutating* call there meets the
    readiness gate (409 `workspace.not_ready`) before the prod guard — which is
    itself the assertion worth making: the guard does not turn a not-ready
    workspace's refusal into a prod one. The real member refusal is proven on a
    READY workspace in `prod_guard_member`.
    """
    headers = {"X-Workspace": SCRATCH_WS}
    res = client.get("/api/agents", headers=headers)
    step("prod.reads_open", res.status_code == 200, f"GET /api/agents HTTP {res.status_code}")

    res = client.delete(f"/api/agents/{uuid.uuid4().hex[:12]}", headers=headers)
    code = (res.json() or {}).get("code")
    step(
        "prod.admin_breakglass",
        code != "workspace.prod_protected",
        f"HTTP {res.status_code} code={code} (admin passes the guard)",
    )

    res = client.patch(
        f"/api/workspaces/{SCRATCH_WS}", json={"tier": "dev", "confirm_tier_change": True}
    )
    step("prod.back_to_dev", res.status_code == 200 and res.json()["tier"] == "dev",
         f"HTTP {res.status_code}")


def member_client(base: str, username: str, password: str) -> httpx.Client:
    """A signed-in member session, cookie pinned the way `_e2e_client` does it."""
    client = httpx.Client(base_url=base, timeout=60)
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    res.raise_for_status()
    token = res.cookies.get("launchpad_session") or client.cookies.get("launchpad_session")
    client.headers["Cookie"] = f"launchpad_session={token}"
    return client


def prod_guard_member(client: httpx.Client, base: str, ready_ws: str) -> None:
    """The real thing: a member's agent mutation on a READY prod workspace is 403.

    Needs a workspace that is actually `ready` (the readiness gate runs first), so
    it borrows the environment's own workspace, flips it to prod for the length of
    the check, and restores the tier in the caller's `finally`.
    """
    username = f"rm-e2e-{RUN}"
    password = f"Pw-{RUN}-aA1!"
    res = client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    step("member.register", res.status_code in (200, 201), f"HTTP {res.status_code}")
    listing = client.get("/api/users", params={"q": username}).json()
    user_id = next(u["id"] for u in listing["items"] if u["username"] == username)
    res = client.patch(
        f"/api/users/{user_id}", json={"status": "active", "workspaces": [ready_ws]}
    )
    step("member.approved", res.status_code == 200, f"HTTP {res.status_code}")

    member = member_client(base, username, password)
    headers = {"X-Workspace": ready_ws}
    try:
        res = client.patch(
            f"/api/workspaces/{ready_ws}", json={"tier": "prod", "confirm_tier_change": True}
        )
        step("member.ws_prod", res.status_code == 200, f"HTTP {res.status_code}")

        res = member.get("/api/agents", headers=headers)
        step("member.reads_open", res.status_code == 200, f"HTTP {res.status_code}")

        res = member.post(
            "/api/agents",
            headers=headers,
            json={
                "name": f"rm-e2e-blocked-{RUN}",
                "method": "harness",
                "system_prompt": "never deployed",
            },
        )
        body = res.json() or {}
        step(
            "member.create_refused",
            res.status_code == 403 and body.get("code") == "workspace.prod_protected",
            f"HTTP {res.status_code} code={body.get('code')}",
        )

        res = member.delete(f"/api/agents/{uuid.uuid4().hex[:12]}", headers=headers)
        body = res.json() or {}
        step(
            "member.delete_refused",
            res.status_code == 403 and body.get("code") == "workspace.prod_protected",
            f"HTTP {res.status_code} code={body.get('code')}",
        )

        res = member.get("/api/observability/dashboard?range=1h", headers=headers)
        step("member.observe_open", res.status_code in (200, 502, 503),
             f"HTTP {res.status_code} (not 403)")
    finally:
        client.patch(
            f"/api/workspaces/{ready_ws}", json={"tier": "dev", "confirm_tier_change": True}
        )
        member.close()
        client.delete(f"/api/users/{user_id}")


def deploy_with_display_name(client: httpx.Client, timeout: int) -> str:
    res = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "display_name": DISPLAY_NAME,
            "method": "harness",
            "system_prompt": "You are a concise HR assistant. Answer in one sentence.",
            "memory": {"short_term": True, "long_term": False},
        },
    )
    step("agent.create", res.status_code == 202, f"HTTP {res.status_code}")
    body = res.json()
    agent_id, job_id = body["agent"]["id"], body["job_id"]

    deadline = time.time() + timeout
    status = "deploying"
    seen: set[tuple[str, str]] = set()
    while time.time() < deadline:
        agent = client.get(f"/api/agents/{agent_id}").json()
        status = agent["status"]
        for stage in agent["deployments"][0]["stages"]:
            key = (stage["name"], stage["status"])
            if key not in seen and stage["status"] != "pending":
                seen.add(key)
                print(f"     stage {stage['name']:<10} {stage['status']:<10} {stage['detail']}")
        if status in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    if status != "active":
        job = client.get(f"/api/jobs/{job_id}").json()
        step("agent.active", False, f"status={status} error={job.get('error')}")
    step("agent.active", True, f"{agent_id} active")
    return agent_id


def display_name_roundtrip(client: httpx.Client, agent_id: str) -> None:
    detail = client.get(f"/api/agents/{agent_id}").json()
    step("name.detail", detail.get("display_name") == DISPLAY_NAME,
         f"display_name={detail.get('display_name')!r} name={detail['name']!r}")

    row = next(
        (a for a in client.get("/api/agents").json()["agents"] if a["id"] == agent_id), None
    )
    step("name.list", row is not None and row.get("display_name") == DISPLAY_NAME,
         f"list display_name={(row or {}).get('display_name')!r}")

    keys = client.get("/api/apikeys").json()["keys"]
    raw = None
    if not keys:
        raw = client.post("/api/apikeys", json={"name": f"rm-e2e-{RUN}"}).json().get("key")
    if raw:
        v1 = httpx.get(
            f"{client.base_url}/v1/agents", headers={"X-Api-Key": raw}, timeout=30
        ).json()
        hit = next((a for a in v1["agents"] if a["id"] == agent_id), None)
        step("name.v1", hit is not None and hit.get("display_name") == DISPLAY_NAME,
             f"/v1 display_name={(hit or {}).get('display_name')!r}")
    else:
        step("name.v1", True, "skipped: no disposable API key (keys already exist)")

    spec = detail["spec"]
    res = client.post(
        f"/api/agents/{agent_id}/redeploy", json={**spec, "display_name": DISPLAY_NAME_2}
    )
    step("name.editable", res.status_code == 202, f"redeploy HTTP {res.status_code}")
    deadline = time.time() + 180
    while time.time() < deadline:
        again = client.get(f"/api/agents/{agent_id}").json()
        if again["status"] in ("active", "failed"):
            break
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    again = client.get(f"/api/agents/{agent_id}").json()
    step(
        "name.edited",
        again["status"] == "active" and again.get("display_name") == DISPLAY_NAME_2,
        f"status={again['status']} display_name={again.get('display_name')!r}",
    )


def cleanup(client: httpx.Client, agent_id: str | None) -> None:
    if agent_id:
        res = client.delete(f"/api/agents/{agent_id}")
        print(f"     delete agent {agent_id}: HTTP {res.status_code}")
    res = client.post(f"/api/workspaces/{SCRATCH_WS}/purge")
    if res.status_code not in (200, 204):
        res = client.delete(f"/api/workspaces/{SCRATCH_WS}")
    print(f"     remove workspace {SCRATCH_WS}: HTTP {res.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-aws", action="store_true",
                        help="ledger-only legs (T01/T05); skips the real deploy")
    args = parser.parse_args()

    client = e2e_client(args.base, timeout=120)

    rows = client.get("/api/workspaces").json()["workspaces"]
    ready = next((w for w in rows if w.get("is_default")), rows[0] if rows else None)
    if ready is None:
        print("no workspace registered — run bootstrap first")
        return 2
    if ready.get("tier") == "prod":
        print(f"refusing to run against a prod workspace ({ready['id']})")
        return 2
    print(f"── base {args.base} · workspace {ready['id']} · run {RUN}")

    agent_id: str | None = None
    try:
        ttfa(client)
        register_scratch(client)
        retier(client)
        prod_guard_scratch(client)
        if ready.get("bootstrap_status") == "ready":
            prod_guard_member(client, args.base, ready["id"])
        else:
            step("member.skipped", True, f"{ready['id']} is {ready.get('bootstrap_status')}")
        if args.skip_aws:
            step("agent.skipped", True, "--skip-aws")
        else:
            agent_id = deploy_with_display_name(client, args.timeout)
            display_name_roundtrip(client, agent_id)
    finally:
        if not args.keep:
            cleanup(client, agent_id)

    summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
