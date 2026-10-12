#!/usr/bin/env python3
"""E2E for multi-workspace (multi-tenant) isolation — REAL AWS, three workspaces.

Needs a hub with three bootstrapped workspaces in one account — by default
`default` (dev), `staging` and `prod` — and the login gate on. Everything it creates
is named `mw-e2e-*` and deleted at the end.

  1. preflight     the three workspaces are ready, in three regions, with the tiers
                   the test expects
  2. accounts      a member granted only `default`, a member granted only `staging`,
                   and a member granted `staging` + `prod`
  3. visibility    each member lists only its own workspaces; naming another one is
                   `403 workspace.forbidden`; a multi-grant member must name one
                   (`400 workspace.header_required`)
  4. tenants       each tenant deploys a Harness agent in its own workspace; every
                   agent's ARN is in that workspace's region
  5. isolation     an agent is invisible from every other workspace — even to the
                   admin, an id from one workspace is `404` in another — and each
                   workspace's list holds only its own
  6. invoke        each agent answers, through its own workspace
  7. prod guard    in the prod workspace a member's deploy is `403
                   workspace.prod_protected`; the admin's is admitted
  8. cleanup       agents in every workspace, then the accounts

Run:  cd backend && uv run python scripts/e2e_multi_workspace.py --base URL [--keep]
      [--dev default --staging staging --prod prod]
"""

import argparse
import secrets
import signal
import sys
import time
import uuid

import httpx
from _e2e_client import e2e_client

RUN = uuid.uuid4().hex[:6]
PROMPT = "You are a concise arithmetic assistant. Reply with just the number."
RESULTS: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, evidence: str, *, fatal: bool = True) -> bool:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}", flush=True)
    if not ok and fatal:
        raise RuntimeError(f"{name}: {evidence}")
    return ok


def summary() -> None:
    print("\n═══ summary ═══")
    for name, verdict, evidence in RESULTS:
        print(f"{verdict:<5} {name:<34} {evidence[:110]}")
    print(f"\n{sum(v == 'PASS' for _, v, _ in RESULTS)} passed, "
          f"{sum(v == 'FAIL' for _, v, _ in RESULTS)} failed")


def login(base: str, creds: dict) -> httpx.Client:
    signed = httpx.Client(base_url=base, timeout=240)
    res = signed.post("/api/auth/login", json={"username": creds["username"],
                                               "password": creds["password"]})
    res.raise_for_status()
    token = res.cookies.get("launchpad_session") or signed.cookies.get("launchpad_session")
    signed.headers["Cookie"] = f"launchpad_session={token}"
    return signed


def member(admin: httpx.Client, base: str, label: str, grants: list[str],
           users: list[str]) -> httpx.Client:
    creds = {"username": f"mw-{label}-{RUN}", "email": f"mw-{label}-{RUN}@example.com",
             "password": f"Pw-{secrets.token_urlsafe(12)}-aA1!"}
    admin.post("/api/auth/register", json=creds).raise_for_status()
    listing = admin.get("/api/users", params={"q": creds["username"]}).json()
    user_id = next(u["id"] for u in listing["items"] if u["username"] == creds["username"])
    users.append(user_id)
    res = admin.patch(f"/api/users/{user_id}", json={"status": "active", "workspaces": grants})
    step(f"acct.{label}", res.status_code == 200, f"{creds['username']} → {grants}")
    return login(base, creds)


def wait_active(client: httpx.Client, agent_id: str, ws: str, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    agent: dict = {}
    while time.time() < deadline:
        agent = client.get(f"/api/agents/{agent_id}", headers={"X-Workspace": ws}).json()
        if agent.get("status") in ("active", "failed"):
            return agent
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    return agent


def _raise_interrupt(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--dev", default="default")
    parser.add_argument("--staging", default="staging")
    parser.add_argument("--prod", default="prod")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _raise_interrupt)
    tenants = {"dev": args.dev, "staging": args.staging, "prod": args.prod}

    admin = e2e_client(args.base, timeout=240)
    if not admin.get("/api/auth/status").json().get("auth_required"):
        raise SystemExit("this e2e needs the login gate on")

    users: list[str] = []
    agents: list[tuple[str, str]] = []  # (workspace, agent id)
    try:
        # ── 1. preflight ──────────────────────────────────────────────────────
        listed = {w["id"]: w for w in admin.get("/api/workspaces").json()["workspaces"]}
        regions = {}
        for role, ws in tenants.items():
            row = listed.get(ws) or {}
            regions[ws] = row.get("region")
            step(f"preflight.{role}", row.get("bootstrap_status") == "ready",
                 f"{ws} · {row.get('region')} · tier {row.get('tier')} · "
                 f"{row.get('bootstrap_status')}")
        step("preflight.distinct_regions", len(set(regions.values())) == 3,
             f"{regions}")
        step("preflight.prod_tier", listed[args.prod].get("tier") == "prod",
             f"{args.prod} is tier {listed[args.prod].get('tier')}")

        # ── 2. accounts ───────────────────────────────────────────────────────
        dev_m = member(admin, args.base, "dev", [args.dev], users)
        stg_m = member(admin, args.base, "stg", [args.staging], users)
        ops_m = member(admin, args.base, "ops", [args.staging, args.prod], users)

        # ── 3. visibility ─────────────────────────────────────────────────────
        def visible(client):
            return sorted(w["id"] for w in client.get("/api/workspaces").json()["workspaces"])

        step("visible.dev_member", visible(dev_m) == [args.dev], f"{visible(dev_m)}")
        step("visible.staging_member", visible(stg_m) == [args.staging], f"{visible(stg_m)}")
        step("visible.ops_member", visible(ops_m) == sorted([args.staging, args.prod]),
             f"{visible(ops_m)}")
        for who, client, other in (("dev", dev_m, args.staging), ("dev", dev_m, args.prod),
                                   ("stg", stg_m, args.dev), ("stg", stg_m, args.prod)):
            res = client.get("/api/agents", headers={"X-Workspace": other})
            step(f"forbidden.{who}→{other}", res.status_code == 403
                 and res.json().get("code") == "workspace.forbidden",
                 f"HTTP {res.status_code} {res.json().get('code')}")
        res = ops_m.get("/api/agents")
        step("header_required.ops", res.status_code == 400
             and res.json().get("code") == "workspace.header_required",
             f"HTTP {res.status_code} {res.json().get('code')} · "
             f"available {res.json().get('detail', {}).get('available')}")

        # ── 4. each tenant deploys in its own workspace ───────────────────────
        def deploy(client, ws, label):
            res = client.post("/api/agents", headers={"X-Workspace": ws},
                              json={"name": f"mw-e2e-{label}-{RUN}", "method": "harness",
                                    "system_prompt": PROMPT,
                                    "memory": {"short_term": False, "long_term": False}})
            step(f"deploy.{label}", res.status_code == 202,
                 f"HTTP {res.status_code} {res.json().get('code', '')}")
            agent_id = res.json()["agent"]["id"]
            agents.append((ws, agent_id))
            return agent_id

        ids = {
            args.dev: deploy(dev_m, args.dev, "dev"),
            args.staging: deploy(stg_m, args.staging, "stg"),
        }
        # ── 7a. prod guard: a member cannot deploy into prod, the admin can ──
        refused = ops_m.post("/api/agents", headers={"X-Workspace": args.prod},
                             json={"name": f"mw-e2e-refused-{RUN}", "method": "harness",
                                   "system_prompt": PROMPT})
        step("prod_guard.member_refused", refused.status_code == 403
             and refused.json().get("code") == "workspace.prod_protected",
             f"HTTP {refused.status_code} {refused.json().get('code')}")
        ids[args.prod] = deploy(admin, args.prod, "prod")

        for ws, agent_id in ids.items():
            agent = wait_active(admin, agent_id, ws)
            step(f"active.{ws}", agent.get("status") == "active"
                 and f":{regions[ws]}:" in (agent.get("arn") or ""),
                 f"{agent.get('status')} · {agent.get('arn')}")

        # ── 5. isolation ──────────────────────────────────────────────────────
        for ws, agent_id in ids.items():
            for other in tenants.values():
                if other == ws:
                    continue
                res = admin.get(f"/api/agents/{agent_id}", headers={"X-Workspace": other})
                step(f"isolated.{ws}→{other}", res.status_code == 404,
                     f"admin reading {ws}'s agent through {other}: HTTP {res.status_code}")
            # this run's agents listed in `ws` must be exactly the one deployed there
            mine = sorted(a["id"] for a in admin.get(
                "/api/agents", headers={"X-Workspace": ws}).json()["agents"]
                if a["name"].endswith(f"-{RUN}"))
            step(f"list_only_own.{ws}", mine == [agent_id],
                 f"this run's agents listed in {ws}: {mine}")

        # ── 6. each agent answers through its own workspace ───────────────────
        for ws, agent_id in ids.items():
            res = admin.post(f"/api/agents/{agent_id}/invoke", headers={"X-Workspace": ws},
                             json={"prompt": "What is 6 * 7? Reply with just the number."})
            text = res.json().get("text", "") if res.status_code == 200 else res.text[:120]
            step(f"invoke.{ws}", res.status_code == 200 and "42" in text,
                 f"HTTP {res.status_code} · {text[:40]!r}")
    except (RuntimeError, httpx.HTTPError, KeyError, StopIteration, KeyboardInterrupt) as exc:
        RESULTS.append(("aborted", "FAIL", f"{type(exc).__name__}: {exc}"[:300]))
    finally:
        print("\n── cleanup")
        if args.keep:
            print("   --keep: leaving agents and accounts in place")
        else:
            for ws, agent_id in agents:
                res = admin.delete(f"/api/agents/{agent_id}", headers={"X-Workspace": ws})
                print(f"   delete agent {agent_id} in {ws}: HTTP {res.status_code}")
            for user_id in users:
                res = admin.delete(f"/api/users/{user_id}")
                print(f"   delete user {user_id}: HTTP {res.status_code}")
        summary()
    return 0 if all(v == "PASS" for _, v, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
