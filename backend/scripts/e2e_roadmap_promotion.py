#!/usr/bin/env python3
"""E2E for the cross-environment release path (T19–T27) — REAL AWS, two regions.

A real dev → staging hand-off between two bootstrapped workspaces in different regions:

  1. a member deploys a zip_runtime agent in the SOURCE workspace (e.g. us-east-1)
  2. a real batch evaluation scores it, so the release gate has evidence to read
  3. the member bundles the publish and asks for a release into the TARGET workspace
     (e.g. us-west-2); an operator — a different person — approves it
  4. execution: mapping → artifact → provision → deploy → smoke → (canary) → observe
  5. the released agent answers in the target region, and `compare` sees both
  6. a second release of a changed prompt goes through the canary path
  7. rollback re-deploys the first bundle in the target
  8. cleanup: agents in both workspaces, the dataset, the two accounts

Run:  cd backend && uv run python scripts/e2e_roadmap_promotion.py \\
        --base URL --target <workspace-id> [--keep]

Needs the login gate on (admin in LAUNCHPAD_E2E_USERNAME / _PASSWORD) and a READY target
workspace that is not tier `prod`.
"""

import argparse
import signal
import sys
import time
import uuid

import httpx
from _e2e_client import e2e_client

RUN = uuid.uuid4().hex[:6]
AGENT = f"rm-e2e-rel-{RUN}"
PROMPT_V1 = "You are a friendly, concise assistant. Answer in one or two sentences."
PROMPT_V2 = "You are a friendly, concise assistant. Always answer in exactly one sentence."
DEV = {"username": f"rm-dev-{RUN}", "email": f"rm-dev-{RUN}@example.com",
       "password": f"Pw-{RUN}-aA1!"}
OPS = {"username": f"rm-ops-{RUN}", "email": f"rm-ops-{RUN}@example.com",
       "password": f"Pw-{RUN}-bB2!"}

RESULTS: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, evidence: str) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}", flush=True)
    if not ok:
        summary()
        sys.exit(1)


def summary() -> None:
    print("\n═══ summary ═══")
    for name, verdict, evidence in RESULTS:
        print(f"{verdict:<5} {name:<30} {evidence}")
    print(f"\n{sum(v == 'PASS' for _, v, _ in RESULTS)} passed, "
          f"{sum(v == 'FAIL' for _, v, _ in RESULTS)} failed")


def session(label: str) -> str:
    return f"{label}-{RUN}-{uuid.uuid4().hex}"[:80]


def account(admin: httpx.Client, base: str, creds: dict, role: str,
            workspaces: list[str]) -> tuple[httpx.Client, str]:
    admin.post("/api/auth/register", json=creds).raise_for_status()
    listing = admin.get("/api/users", params={"q": creds["username"]}).json()
    user_id = next(u["id"] for u in listing["items"] if u["username"] == creds["username"])
    patch = {"status": "active", "workspaces": workspaces}
    if role != "member":
        patch["role"] = role
    res = admin.patch(f"/api/users/{user_id}", json=patch)
    step(f"acct.{role}", res.status_code == 200, f"{creds['username']} → {workspaces}")
    signed = httpx.Client(base_url=base, timeout=240)
    login = signed.post("/api/auth/login", json={"username": creds["username"],
                                                 "password": creds["password"]})
    login.raise_for_status()
    token = login.cookies.get("launchpad_session") or signed.cookies.get("launchpad_session")
    signed.headers["Cookie"] = f"launchpad_session={token}"
    # Granted two workspaces, a member has no implicit one (`workspace.header_required`):
    # pin the source, and name the target explicitly on target-side calls.
    signed.headers["X-Workspace"] = workspaces[0]
    return signed, user_id


def wait_agent(client: httpx.Client, agent_id: str, timeout: int,
               headers: dict | None = None) -> dict:
    deadline = time.time() + timeout
    agent: dict = {}
    while time.time() < deadline:
        agent = client.get(f"/api/agents/{agent_id}", headers=headers or {}).json()
        if agent.get("status") in ("active", "failed"):
            return agent
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    return agent


def evaluate(client: httpx.Client, agent_id: str) -> tuple[str, str]:
    dataset = client.post("/api/eval/datasets", json={
        "name": f"rm-e2e-rel-{RUN}",
        "items": [{"prompt": "What is 21 * 2? Reply with just the number."},
                  {"prompt": "Say hello in one word."}],
    })
    step("eval.dataset", dataset.status_code in (200, 201), f"HTTP {dataset.status_code}")
    dataset_id = dataset.json()["id"]
    run = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": dataset_id,
        "evaluators": ["Builtin.Helpfulness"], "mode": "evaluators", "wait_seconds": 180,
    })
    step("eval.started", run.status_code in (200, 201, 202), f"HTTP {run.status_code}")
    run_id = run.json()["id"]
    deadline = time.time() + 1500
    body: dict = {}
    while time.time() < deadline:
        body = client.get(f"/api/eval/runs/{run_id}").json()
        if body["status"] in ("completed", "failed", "stopped"):
            break
        # Real-AWS evaluation polling is intentionally paced and attempt-bounded.
        time.sleep(20)  # nosemgrep: arbitrary-sleep
    scores = ", ".join(f"{s['evaluatorId']}={s['score']:.2f}" for s in body.get("scores", []))
    step("eval.completed", body.get("status") == "completed" and bool(body.get("scores")),
         f"run {run_id} · {body.get('status')} · {scores or body.get('error')}")
    return dataset_id, run_id


def execute_and_watch(ops: httpx.Client, promotion_id: str, label: str,
                      timeout: int = 2400) -> dict:
    res = ops.post(f"/api/promotions/{promotion_id}/execute")
    step(f"{label}.execute_accepted", res.status_code == 202,
         f"HTTP {res.status_code} {res.json().get('code', '')}")
    deadline = time.time() + timeout
    seen: set[tuple[str, str]] = set()
    status: dict = {}
    while time.time() < deadline:
        # the execution view is {"promotion": {...status, stages...}, "log": [...]}
        status = ops.get(f"/api/promotions/{promotion_id}/execution").json()["promotion"]
        for stage in status.get("stages") or []:
            key = (stage.get("name"), stage.get("status"))
            if key not in seen and stage.get("status") not in ("pending", None):
                seen.add(key)
                print(f"     {stage.get('name'):<10} {stage.get('status'):<10} "
                      f"{str(stage.get('detail') or '')[:110]}", flush=True)
        if status.get("status") in ("succeeded", "failed", "rolled_back"):
            break
        time.sleep(10)  # nosemgrep: arbitrary-sleep — release execution polling
    return status


def target_agent(admin: httpx.Client, target: str, name: str = AGENT) -> dict | None:
    rows = admin.get("/api/agents", headers={"X-Workspace": target}).json()["agents"]
    return next((a for a in rows if a["name"] == name and a["status"] != "deleted"), None)


def release(dev: httpx.Client, ops: httpx.Client, agent_id: str, target: str,
            note: str) -> tuple[dict, str]:
    bundle = dev.post(f"/api/agents/{agent_id}/release-bundles", json={"note": note})
    step(f"{note}.bundle", bundle.status_code == 201, f"HTTP {bundle.status_code}")
    bundle_body = bundle.json()
    step(f"{note}.eval_pinned", bool((bundle_body.get("evaluation") or {}).get("run_id")),
         f"evidence={bundle_body.get('evaluation', {}).get('run_id')}")
    asked = dev.post("/api/promotions", json={
        "bundle_id": bundle_body["id"], "target_workspace_id": target,
        "change_note": f"{note}: e2e release", "rollback_note": "redeploy previous bundle"})
    step(f"{note}.requested", asked.status_code == 201, f"HTTP {asked.status_code}")
    promotion_id = asked.json()["id"]
    self_ok = dev.post(f"/api/promotions/{promotion_id}/review", json={"decision": "approve"})
    step(f"{note}.member_cannot_approve", self_ok.status_code == 403, f"HTTP {self_ok.status_code}")
    plan = ops.get(f"/api/promotions/{promotion_id}/plan")
    step(f"{note}.plan", plan.status_code == 200, f"keys={sorted(plan.json())[:6]}")
    approved = ops.post(f"/api/promotions/{promotion_id}/review",
                        json={"decision": "approve", "note": "gates read"})
    step(f"{note}.approved", approved.status_code == 200
         and approved.json()["status"] == "approved", f"HTTP {approved.status_code}")
    failing = [c["key"] for c in approved.json()["gates"].get("checks", []) if not c["ok"]]
    print(f"     gates at approval — failing: {failing or 'none'}", flush=True)
    return bundle_body, promotion_id


HARNESS = f"rm-e2e-relh-{RUN}"
CREATED: dict[str, list[str]] = {"datasets": []}


def harness_rollback(dev: httpx.Client, ops: httpx.Client, admin: httpx.Client,
                     target: str) -> None:
    """Release v1, release v2 over it, then roll v2 back: v1 must serve again."""
    made = dev.post("/api/agents", json={
        "name": HARNESS, "method": "harness", "system_prompt": PROMPT_V1,
        "memory": {"short_term": True, "long_term": False}})
    step("h.deploy", made.status_code == 202, f"HTTP {made.status_code}")
    agent_id = made.json()["agent"]["id"]
    step("h.active", wait_agent(dev, agent_id, 600).get("status") == "active", agent_id)
    dataset_id, _ = evaluate(dev, agent_id)
    CREATED["datasets"].append(dataset_id)

    _, h1 = release(dev, ops, agent_id, target, "h1")
    first = execute_and_watch(ops, h1, "h1")
    step("h1.succeeded", first.get("status") == "succeeded",
         f"status={first.get('status')} error={first.get('error')}")
    none_before = ops.post(f"/api/promotions/{h1}/rollback")
    step("h1.no_previous_refused", none_before.status_code in (400, 409),
         f"HTTP {none_before.status_code} {none_before.json().get('code')} "
         "(nothing to roll back to)")

    spec = dev.get(f"/api/agents/{agent_id}").json()["spec"]
    dev.post(f"/api/agents/{agent_id}/redeploy", json={**spec, "system_prompt": PROMPT_V2})
    step("h.v2_active", wait_agent(dev, agent_id, 600).get("status") == "active", "")
    _, h2 = release(dev, ops, agent_id, target, "h2")
    second = execute_and_watch(ops, h2, "h2")
    step("h2.succeeded", second.get("status") == "succeeded",
         f"status={second.get('status')} error={second.get('error')}")
    live = target_agent(admin, target, HARNESS)
    step("h2.v2_live", (live or {}).get("spec", {}).get("system_prompt") == PROMPT_V2,
         "harness replaced directly (no canary)")

    back = ops.post(f"/api/promotions/{h2}/rollback")
    step("rollback.accepted", back.status_code == 202,
         f"HTTP {back.status_code} {back.json().get('code', '')}")
    deadline = time.time() + 1800
    rb: dict = {}
    while time.time() < deadline:
        rb = ops.get(f"/api/promotions/{h2}/execution").json()["promotion"]
        if rb.get("status") in ("rolled_back", "failed"):
            break
        time.sleep(10)  # nosemgrep: arbitrary-sleep — rollback polling
    step("rollback.done", rb.get("status") == "rolled_back",
         f"status={rb.get('status')} error={rb.get('error')}")
    restored = target_agent(admin, target, HARNESS)
    step("rollback.v1_restored",
         (restored or {}).get("spec", {}).get("system_prompt") == PROMPT_V1,
         "the target serves the first bundle again")
    reply = admin.post(f"/api/agents/{restored['id']}/invoke", headers={"X-Workspace": target},
                       json={"prompt": "Say hello in one word.", "session_id": session("rb")})
    step("rollback.answers", reply.status_code == 200 and reply.json().get("text"),
         f"{reply.json().get('text', '')[:40]!r}")


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--target", required=True, help="the READY target workspace id")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    # SIGTERM (a CI timeout, a manual stop) raises instead of killing the process, so the
    # `finally` below still deletes what this run created in BOTH regions.
    signal.signal(signal.SIGTERM, _raise_interrupt)
    admin = e2e_client(args.base, timeout=240)
    rows = {w["id"]: w for w in admin.get("/api/workspaces").json()["workspaces"]}
    source = next((w for w in rows.values() if w.get("is_default")), None)
    target = rows.get(args.target)
    if source is None or target is None:
        print("source (default) or target workspace missing")
        return 2
    if target.get("tier") == "prod" or target.get("bootstrap_status") != "ready":
        print(f"target must be READY and not prod (is {target.get('tier')}/"
              f"{target.get('bootstrap_status')})")
        return 2
    print(f"── {source['id']} ({source['region']}) → {target['id']} ({target['region']}) "
          f"· run {RUN}", flush=True)

    # The target's release policy: a lenient score bar and a short observation window keep
    # the run bounded; the policy API itself is part of what is being exercised.
    policy = admin.put(f"/api/release-policies/{args.target}",
                       json={"min_eval_score": 0.5, "observe_seconds": 30})
    step("policy.set", policy.status_code == 200, f"HTTP {policy.status_code}")

    dev_id = ops_id = None
    source_agent = dataset_id = None
    try:
        dev, dev_id = account(admin, args.base, DEV, "member", [source["id"], args.target])
        ops, ops_id = account(admin, args.base, OPS, "operator", [source["id"], args.target])

        made = dev.post("/api/agents", json={
            "name": AGENT, "display_name": "跨区域发布验收", "method": "zip_runtime",
            "system_prompt": PROMPT_V1, "memory": {"short_term": False, "long_term": False}})
        step("dev.deploy", made.status_code == 202, f"HTTP {made.status_code}")
        source_agent = made.json()["agent"]["id"]
        state = wait_agent(dev, source_agent, 900)
        step("dev.active", state.get("status") == "active",
             f"{source_agent} · {state.get('status')}")

        dataset_id, _ = evaluate(dev, source_agent)

        _, rel1 = release(dev, ops, source_agent, args.target, "rel1")
        first = execute_and_watch(ops, rel1, "rel1")
        step("rel1.succeeded", first.get("status") == "succeeded",
             f"status={first.get('status')} error={first.get('error')}")

        landed = target_agent(admin, args.target)
        step("target.agent_exists", landed is not None,
             f"{(landed or {}).get('id')} in {args.target}")
        step("target.active", (landed or {}).get("status") == "active",
             f"status={(landed or {}).get('status')}")
        reply = admin.post(f"/api/agents/{landed['id']}/invoke",
                           headers={"X-Workspace": args.target},
                           json={"prompt": "Say hello in one word.",
                                 "session_id": session("target")})
        step("target.answers", reply.status_code == 200 and reply.json().get("text"),
             f"{target['region']} says {reply.json().get('text', '')[:40]!r}")
        step("target.region_correct", target["region"] in (landed.get("arn") or ""),
             f"arn region ok: {(landed.get('arn') or '')[:70]}")

        cmp_ = admin.get("/api/environments/compare", params={"agent": AGENT}).json()
        present = [e["workspace"]["id"] for e in cmp_.get("environments", []) if e.get("present")]
        step("compare.both_envs", {source["id"], args.target} <= set(present),
             f"present in {present}; aligned={cmp_.get('summary', {}).get('aligned')}")

        # ── release 2 through the canary path: the target now runs a champion ──────────
        spec = dev.get(f"/api/agents/{source_agent}").json()["spec"]
        res = dev.post(f"/api/agents/{source_agent}/redeploy",
                       json={**spec, "system_prompt": PROMPT_V2})
        step("dev.redeploy", res.status_code == 202, f"HTTP {res.status_code}")
        state = wait_agent(dev, source_agent, 900)
        step("dev.v2_active", state.get("status") == "active", state.get("status", ""))
        _, rel2 = release(dev, ops, source_agent, args.target, "rel2")
        outcome = execute_and_watch(ops, rel2, "rel2")
        after = target_agent(admin, args.target)
        live_prompt = (after or {}).get("spec", {}).get("system_prompt")
        if outcome.get("status") == "succeeded":
            step("rel2.canary_promoted", live_prompt == PROMPT_V2,
                 "the candidate won the ramp and is now live")
        else:
            # The judge decides, not the script: a candidate scored worse than the running
            # version is blocked, and that is the safety property. What must hold either
            # way is that nothing is left half-released.
            step("rel2.blocked_honestly",
                 outcome.get("status") == "failed" and "canary" in (outcome.get("error") or ""),
                 f"status={outcome.get('status')} · {outcome.get('error')}")
            step("rel2.champion_still_live", live_prompt == PROMPT_V1,
                 "the previous version kept serving")
        # The canary the release used, found by its champion — an assertion that must not
        # pass vacuously when no canary is found.
        listing = admin.get("/api/runtime-canaries", headers={"X-Workspace": args.target}).json()
        canaries = [c for c in (listing.get("canaries") or listing.get("items") or [])
                    if c.get("champion_agent_name") == AGENT]
        step("rel2.canary_found", bool(canaries), f"{len(canaries)} canary(ies) for {AGENT}")
        latest = max(canaries, key=lambda c: c.get("created_at") or "")
        step("rel2.canary_cleaned", latest.get("status") == "cleaned"
             and not latest.get("running_action"),
             f"canary {latest.get('id')} · {latest.get('status')} (no leaked endpoint)")

        # ── harness: a deterministic replace + rollback (canaries are runtime-only) ─────
        harness_rollback(dev, ops, admin, args.target)
    finally:
        if not args.keep:
            print("── cleanup", flush=True)
            for ws in (args.target, source["id"]):
                for name in (AGENT, HARNESS):
                    row = target_agent(admin, ws, name)
                    if row:
                        res = admin.delete(f"/api/agents/{row['id']}", headers={"X-Workspace": ws})
                        print(f"     delete {name} in {ws}: HTTP {res.status_code}")
            for ds in filter(None, [dataset_id, *CREATED["datasets"]]):
                admin.delete(f"/api/eval/datasets/{ds}")
            for user_id in filter(None, (dev_id, ops_id)):
                admin.delete(f"/api/users/{user_id}")
    summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
