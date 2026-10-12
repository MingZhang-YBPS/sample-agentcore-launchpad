#!/usr/bin/env python3
"""E2E for the Agent-DLC loop (docs/agent-dlc-design.md) — REAL AWS, one workspace.

The whole define → evaluate → gate → release → rollback loop, with three different
people, against a real Harness agent:

  1. accounts: an engineer (edits criteria, deploys), a business owner (signs the
     standard, curates the golden set), an operator (signs the release)
  2. the engineer deploys a Harness agent and moves it onto `live` / `candidate`
     endpoints; the workspace policy is switched to `release_mode=gated`
  3. the criteria table: engineer drafts → publishes; the engineer's own signature
     is refused; the owner signs
  4. the golden set: the owner seeds all three splits once; the holdout is sealed
  5. a redeploy lands on `candidate` — `live` still serves the previous version
  6. the gate: regression + holdout runs against `candidate` (priced first), read
     per criterion from the evaluation output; the verdict and every row's reason
  7. PASS → the operator signs and `live` moves; anything else → the operator blocks
     and `live` stays where it was. Then rollback re-points `live`
  8. calibration: a labelling task from the gate run, an account-free annotation
     link labels blind, the agreement endpoint answers
  9. scorecard, fix ladder and decision history read back
 10. cleanup: the agent (and its endpoints), the golden datasets, the accounts, and
     the workspace's release policy restored exactly

Run:  cd backend && uv run python scripts/e2e_agent_dlc.py --base URL [--keep]

Needs the login gate on (admin in LAUNCHPAD_E2E_USERNAME / _PASSWORD) — separation of
duties cannot be exercised with one anonymous identity — and a workspace that is NOT
tier `prod`: the script refuses to touch one.
"""

import argparse
import secrets
import signal
import sys
import time
import uuid
from typing import Any

import httpx
from _e2e_client import e2e_client

RUN = uuid.uuid4().hex[:6]
AGENT = f"dlc-e2e-{RUN}"
PROMPT_V1 = "You are a concise arithmetic assistant. Reply with just the number."
PROMPT_V2 = "You are a precise arithmetic assistant. Reply with only the resulting number."
PEOPLE = {
    "engineer": {"username": f"dlc-eng-{RUN}", "email": f"dlc-eng-{RUN}@example.com",
                 "password": f"Pw-{secrets.token_urlsafe(12)}-aA1!", "role": "member",
                 "grant": ["criteria.manage", "agents.deploy", "eval.run"]},
    "owner": {"username": f"dlc-own-{RUN}", "email": f"dlc-own-{RUN}@example.com",
              "password": f"Pw-{secrets.token_urlsafe(12)}-aA1!", "role": "member",
              "grant": ["criteria.sign", "golden.admit", "judge.calibrate"]},
    "operator": {"username": f"dlc-ops-{RUN}", "email": f"dlc-ops-{RUN}@example.com",
                 "password": f"Pw-{secrets.token_urlsafe(12)}-aA1!", "role": "operator",
                 "grant": ["release.sign", "waiver.approve"]},
}
# a latency red line keeps the standard deterministic (no LLM judge may hold a red
# line); the helpfulness judge is declared a gate but stays observed until calibrated
CRITERIA = [
    {"key": "P1", "text": "the agent answers within 120 seconds at P95",
     "dimension": "performance", "tier": "redline",
     "executor": {"kind": "metric"},
     "metric_rule": {"metric": "latency_p95_ms", "op": "<=", "value": 120000},
     "notes": "n/a:cognition n/a:responsibility n/a:cost (arithmetic demo agent)"},
    {"key": "Q1", "text": "the answer is helpful to the person who asked",
     "dimension": "quality", "tier": "gate", "threshold": 0.6,
     "executor": {"kind": "evaluator", "evaluator_id": "Builtin.Helpfulness"}},
]
ITEMS = [
    ("holdout", "h1", "What is 6 * 7?", "42"),
    ("holdout", "h2", "What is 100 - 1?", "99"),
    ("regression", "r1", "What is 21 * 2?", "42"),
    ("regression", "r2", "What is 9 + 10?", "19"),
    ("regression", "r3", "What is 81 / 9?", "9"),
    ("dev", "d1", "What is 2 + 2?", "4"),
]

RESULTS: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, evidence: str, *, fatal: bool = True) -> bool:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}", flush=True)
    if not ok and fatal:
        raise RuntimeError(f"{name} failed: {evidence}")
    return ok


def summary() -> None:
    print("\n═══ summary ═══")
    for name, verdict, evidence in RESULTS:
        print(f"{verdict:<5} {name:<34} {evidence[:110]}")
    print(f"\n{sum(v == 'PASS' for _, v, _ in RESULTS)} passed, "
          f"{sum(v == 'FAIL' for _, v, _ in RESULTS)} failed")


def login(base: str, creds: dict, workspace: str) -> httpx.Client:
    signed = httpx.Client(base_url=base, timeout=240)
    res = signed.post("/api/auth/login", json={"username": creds["username"],
                                               "password": creds["password"]})
    res.raise_for_status()
    token = res.cookies.get("launchpad_session") or signed.cookies.get("launchpad_session")
    # the cookie is `Secure` on prod; pin it as a header (see _e2e_client)
    signed.headers["Cookie"] = f"launchpad_session={token}"
    signed.headers["X-Workspace"] = workspace
    return signed


def account(admin: httpx.Client, base: str, who: str, workspace: str,
            created: list[str]) -> httpx.Client:
    creds = PEOPLE[who]
    admin.post("/api/auth/register", json={k: creds[k] for k in ("username", "email",
                                                                  "password")}).raise_for_status()
    listing = admin.get("/api/users", params={"q": creds["username"]}).json()
    user_id = next(u["id"] for u in listing["items"] if u["username"] == creds["username"])
    created.append(user_id)
    perms = {key: True for key in creds["grant"]}
    patch: dict[str, Any] = {"status": "active", "workspaces": [workspace],
                             "permissions": perms}
    if creds["role"] != "member":
        patch["role"] = creds["role"]
    res = admin.patch(f"/api/users/{user_id}", json=patch)
    step(f"acct.{who}", res.status_code == 200, f"{creds['username']} · {sorted(perms)}")
    return login(base, creds, workspace)


def wait(predicate, timeout: int, every: int, label: str) -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        # Real-AWS polling is intentionally paced and attempt-bounded.
        time.sleep(every)  # nosemgrep: arbitrary-sleep
    raise RuntimeError(f"timed out waiting for {label}")


def wait_active(client: httpx.Client, agent_id: str, *, version_after: str | None = None,
                timeout: int = 900) -> dict:
    def ready():
        agent = client.get(f"/api/agents/{agent_id}").json()
        if agent.get("status") == "failed":
            raise RuntimeError(f"deploy failed: {agent.get('deployments', [{}])[0]}")
        if agent.get("status") != "active":
            return None
        if version_after is not None and str(agent.get("version")) == str(version_after):
            return None
        # the redeploy job is finished only when its deployment says so
        latest = (agent.get("deployments") or [{}])[0]
        if latest.get("status") not in (None, "succeeded"):
            return None
        return agent
    return wait(ready, timeout, 10, "the agent to become active")


def _raise_interrupt(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--keep", action="store_true", help="leave everything in place")
    parser.add_argument("--gate-timeout", type=int, default=2700)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _raise_interrupt)

    admin = e2e_client(args.base, timeout=240)
    if not admin.get("/api/auth/status").json().get("auth_required"):
        raise SystemExit("this e2e needs the login gate on: start the backend with "
                         "LAUNCHPAD_AUTH_USERNAME / LAUNCHPAD_AUTH_PASSWORD set")
    admin.headers["X-Workspace"] = args.workspace
    policy_before = admin.get(f"/api/release-policies/{args.workspace}").json()
    if policy_before.get("tier") == "prod":
        raise SystemExit(f"workspace {args.workspace} is tier prod — refusing to run")
    step("preflight", True, f"workspace {args.workspace} · tier {policy_before.get('tier')}")

    users: list[str] = []
    datasets: list[str] = []
    # a labelling task and its link are a live bearer credential: a test that mints one
    # must take it back, or every run leaves a usable token pointing at its transcripts
    links: list[tuple[str, str, str]] = []
    tasks: list[str] = []
    agent_id: str | None = None
    try:
        eng = account(admin, args.base, "engineer", args.workspace, users)
        own = account(admin, args.base, "owner", args.workspace, users)
        ops = account(admin, args.base, "operator", args.workspace, users)
        # an operator does not hold agents.deploy by design (T19), so the engineer
        # deploys and the operator decides what production serves

        # ── 2. a Harness agent on named endpoints, the workspace gated ─────────────
        res = eng.post("/api/agents", json={"name": AGENT, "method": "harness",
                                            "system_prompt": PROMPT_V1,
                                            "memory": {"short_term": False,
                                                       "long_term": False}})
        step("agent.create", res.status_code == 202, f"HTTP {res.status_code}")
        agent_id = res.json()["agent"]["id"]
        agent = wait_active(eng, agent_id)
        v1 = str(agent["version"])
        step("agent.active", True, f"{agent_id} · version {v1}")

        # moving production onto named endpoints is a release decision, so it carries
        # `release.sign` — the engineer who builds the agent may not do it
        refused = eng.post(f"/api/agents/{agent_id}/release/migrate")
        step("release.migrate_needs_release_sign", refused.status_code == 403,
             f"HTTP {refused.status_code} {refused.json().get('code')}")
        migrated = ops.post(f"/api/agents/{agent_id}/release/migrate")
        state = migrated.json().get("state") or {}
        step("release.migrate", migrated.status_code == 200
             and state.get("endpoint_mode") == "live",
             f"HTTP {migrated.status_code} · live → {state.get('live_version')} "
             f"{migrated.text[:160] if migrated.status_code != 200 else ''}")

        gated = admin.put(f"/api/release-policies/{args.workspace}",
                          json={**policy_before.get("policy", {}), "release_mode": "gated"})
        step("policy.gated", gated.status_code == 200
             and gated.json()["policy"].get("release_mode") == "gated", f"HTTP {gated.status_code}")

        # ── 3. the criteria table, signed by someone else ──────────────────────────
        created = eng.post("/api/criteria-sets", json={"kind": "agent", "agent_id": agent_id,
                                                      "name": f"arithmetic {RUN}"})
        step("criteria.create", created.status_code == 201, f"HTTP {created.status_code}")
        lineage = created.json()["set"]["lineage_id"]
        saved = eng.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
        body = saved.json()
        step("criteria.save", saved.status_code == 200,
             f"tiers {body.get('summary', {}).get('tiers')} · effective gates "
             f"{body.get('summary', {}).get('effective_gates')}/"
             f"{body.get('summary', {}).get('declared_gates')}")
        q1 = next(c for c in body["criteria"] if c["key"] == "Q1")
        step("criteria.judge_observed", q1["effective_tier"] == "observe",
             f"Q1 declared {q1['tier']} → effective {q1['effective_tier']} (uncalibrated)")
        published = eng.post(f"/api/criteria-sets/{lineage}/publish")
        step("criteria.publish", published.status_code == 200, f"HTTP {published.status_code}")
        self_sign = eng.post(f"/api/criteria-sets/{lineage}/sign", json={"note": "mine"})
        step("criteria.self_sign_refused", self_sign.status_code == 403,
             f"HTTP {self_sign.status_code} {self_sign.json().get('code')}")
        signed = own.post(f"/api/criteria-sets/{lineage}/sign", json={"note": "e2e"})
        step("criteria.owner_signs", signed.status_code == 200
             and signed.json()["set"]["signed_by"] == PEOPLE["owner"]["username"],
             f"signed by {signed.json()['set'].get('signed_by')}")

        # ── 4. the golden set, curated once ───────────────────────────────────────
        golden = eng.post("/api/golden-sets", json={"name": f"dlc-e2e-{RUN}",
                                                   "criteria_lineage_id": lineage})
        step("golden.create", golden.status_code == 201, f"HTTP {golden.status_code}")
        golden_id = golden.json()["id"]
        datasets.append(golden_id)
        datasets.extend(s["id"] for s in golden.json()["splits"].values())
        seed_items = [{"split": split, "scenario_id": sid,
                       "turns": [{"input": q, "expected_response": a}],
                       "metadata": {"dlc": {"case_tier": "known_good",
                                            "criteria_ids": ["P1", "Q1"]}}}
                      for split, sid, q, a in ITEMS]
        seeded = own.post(f"/api/golden-sets/{golden_id}/seed", json={"items": seed_items})
        step("golden.seed", seeded.status_code == 201, f"counts {seeded.json().get('counts')}")
        again = own.post(f"/api/golden-sets/{golden_id}/seed", json={"items": seed_items[:1]})
        step("golden.holdout_sealed", again.status_code == 409
             and again.json().get("code") == "golden.holdout_sealed", f"HTTP {again.status_code}")

        # ── 5. a redeploy lands on candidate; live is untouched ────────────────────
        redeploy = eng.post(f"/api/agents/{agent_id}/redeploy",
                            json={"name": AGENT, "method": "harness",
                                  "system_prompt": PROMPT_V2,
                                  "memory": {"short_term": False, "long_term": False}})
        step("agent.redeploy", redeploy.status_code == 202, f"HTTP {redeploy.status_code}")
        agent = wait_active(eng, agent_id, version_after=v1)
        v2 = str(agent["version"])
        release = wait(lambda: (lambda r: r if r.get("pending") else None)(
            eng.get(f"/api/agents/{agent_id}/release").json()), 300, 10, "the release record")
        state = release["state"]
        step("release.candidate_only",
             release["pending"]["candidate_version"] == v2
             and str(state.get("live_version")) == v1
             and str(state.get("candidate_version")) == v2,
             f"live {state.get('live_version')} · candidate {state.get('candidate_version')} "
             f"(ledger {v2})")
        invoked = eng.post(f"/api/agents/{agent_id}/invoke",
                           json={"prompt": "What is 2 + 3? Reply with just the number."})
        step("invoke.through_live", invoked.status_code == 200 and "5" in invoked.json()["text"],
             f"HTTP {invoked.status_code} · {invoked.json().get('text', '')[:40]!r}")

        # ── 6. the gate ────────────────────────────────────────────────────────────
        estimate = eng.post("/api/eval/runs/estimate", json={"agent_id": agent_id,
                                                           "items": 5, "repeats": 1})
        step("cost.estimate", estimate.status_code == 200,
             f"{estimate.json().get('sessions')} sessions · ${estimate.json().get('total_usd')} "
             f"({estimate.json().get('agent_basis')})")
        evaluated = eng.post(f"/api/agents/{agent_id}/release/evaluate",
                             json={"repeats": 1, "confirm_cost": True})
        step("gate.evaluate", evaluated.status_code == 202,
             f"HTTP {evaluated.status_code} {evaluated.json().get('code', '')} · runs "
             f"{evaluated.json().get('pending', {}).get('run_ids')}")
        run_ids = evaluated.json()["pending"]["run_ids"]

        def decided():
            gate = eng.get(f"/api/agents/{agent_id}/release/gate").json()
            return gate if gate.get("status") == "decided" else None

        gate = wait(decided, args.gate_timeout, 30, "the gate runs")
        report = gate["report"]
        rows = "; ".join(f"{r['key']}={r['verdict']}"
                         + (f" ({r.get('reason')})" if r.get("reason") else "")
                         for r in report["criteria"])
        step("gate.decided", report["verdict"] in ("PASS", "BLOCKED", "INVALID"),
             f"{report['verdict']} · {rows} · issues {report['provenance'].get('issues')}")
        for run_id in run_ids:
            crit = eng.get(f"/api/eval/runs/{run_id}/criteria").json()
            step(f"run.criteria.{crit.get('split')}",
                 crit.get("endpoint_qualifier") == "candidate" and bool(crit.get("summary")),
                 f"qualifier {crit.get('endpoint_qualifier')} · "
                 + ", ".join(f"{k}: n={v.get('n')} rate={v.get('rate')} value={v.get('value')}"
                             for k, v in crit.get("summary", {}).items()))
        q1_row = next(r for r in report["criteria"] if r["key"] == "Q1")
        step("gate.judge_does_not_block", q1_row["verdict"] == "OBSERVED",
             f"Q1 {q1_row['verdict']} · {q1_row.get('reason')}")

        # ── 7. sign or block, then roll back ───────────────────────────────────────
        self_release = eng.post(f"/api/agents/{agent_id}/release/sign", json={"note": "x"})
        step("release.engineer_cannot_sign", self_release.status_code == 403,
             f"HTTP {self_release.status_code} {self_release.json().get('code')}")
        if report["verdict"] == "PASS":
            out = ops.post(f"/api/agents/{agent_id}/release/sign", json={"note": "e2e"})
            live = out.json()["state"].get("live_version")
            step("release.signed", out.status_code == 200 and str(live) == v2,
                 f"HTTP {out.status_code} · live → {live}")
            back = ops.post(f"/api/agents/{agent_id}/release/rollback",
                            json={"note": "e2e rollback"})
            live = back.json()["state"].get("live_version")
            step("release.rollback", back.status_code == 200 and str(live) == v1,
                 f"HTTP {back.status_code} · live → {live}")
        else:
            refused = ops.post(f"/api/agents/{agent_id}/release/sign", json={"note": "e2e"})
            step("release.sign_refused", refused.status_code == 409,
                 f"{report['verdict']} cannot be signed · HTTP {refused.status_code}")
            out = ops.post(f"/api/agents/{agent_id}/release/block",
                           json={"note": f"e2e: gate said {report['verdict']}"})
            live = out.json()["state"].get("live_version")
            step("release.blocked", out.status_code == 200 and str(live) == v1,
                 f"HTTP {out.status_code} · live stays {live}")

        # ── 8. calibration through an account-free link ────────────────────────────
        regression_run = next(
            r for r in run_ids
            if eng.get(f"/api/eval/runs/{r}/criteria").json().get("split") == "regression")
        task = own.post("/api/annotation-tasks", json={
            "agent_id": agent_id, "criteria_lineage_id": lineage, "criterion_key": "Q1",
            "annotators": [PEOPLE["owner"]["username"], PEOPLE["engineer"]["username"]],
            "run_id": regression_run})
        if task.status_code == 201:
            task_id = task.json()["id"]
            tasks.append(task_id)
            step("calibration.task", True, f"{task.json()['total']} items from the gate run")
            link = own.post(f"/api/annotation-tasks/{task_id}/links",
                            json={"label": "e2e SME", "expires_in_days": 1})
            step("calibration.link", link.status_code == 201, f"HTTP {link.status_code}")
            if link.status_code == 201:
                links.append((task_id, link.json()["id"], link.json()["token"]))
            token = link.json()["token"]
            anon = httpx.Client(base_url=args.base, timeout=60)  # no session, no header
            queue = anon.get(f"/share/annotate/{token}").json()
            step("calibration.blind", all("judge_label" not in i for i in queue["items"]),
                 f"{queue['total']} items, no judge verdict served")
            labelled = anon.post(f"/share/annotate/{token}/label",
                                 json={"item_ref": queue["items"][0]["ref"], "label": "pass"})
            step("calibration.link_labels", labelled.status_code == 200
                 and labelled.json()["labelled"] == 1, f"HTTP {labelled.status_code}")
            agreement = own.get(f"/api/annotation-tasks/{task_id}/agreement").json()
            step("calibration.agreement", agreement.get("suggested_verdict") == "insufficient_n",
                 f"n={agreement.get('n')} · suggested {agreement.get('suggested_verdict')}")
        else:
            step("calibration.task", False, f"HTTP {task.status_code} {task.text[:160]}",
                 fatal=False)

        # ── 9. read-backs ──────────────────────────────────────────────────────────
        card = eng.get(f"/api/agents/{agent_id}/scorecard").json()
        step("scorecard", [d["dimension"] for d in card["dimensions"]] == [
            "cognition", "quality", "responsibility", "cost", "performance"],
            f"last gate {card.get('last_gate')} · calibration debt "
            f"{[d['criterion_key'] for d in card['calibration_debt']]}")
        ladder = eng.get(f"/api/agents/{agent_id}/ladder").json()
        step("ladder", "runs" in ladder, f"{len(ladder['runs'])} rung(s) · comparable "
             f"{ladder.get('comparable')}")
        trail = admin.get("/api/audit", params={"limit": 200}).json()["events"]
        actions = {e["action"] for e in trail}
        step("audit", {"criteria.sign", "golden.seed", "release.evaluate"} <= actions,
             str(sorted(a for a in actions
                        if a.split('.')[0] in ('criteria', 'golden', 'release'))))
    except (RuntimeError, httpx.HTTPError, KeyError, StopIteration, KeyboardInterrupt) as exc:
        RESULTS.append(("aborted", "FAIL", f"{type(exc).__name__}: {exc}"[:300]))
    finally:
        print("\n── cleanup")
        if args.keep:
            print("   --keep: leaving the agent, datasets and accounts in place")
        else:
            restored = admin.put(f"/api/release-policies/{args.workspace}",
                                 json=policy_before.get("policy", {}))
            print(f"   release policy restored: HTTP {restored.status_code}")
            for task_id, link_id, raw in links:
                res = admin.delete(f"/api/annotation-tasks/{task_id}/links/{link_id}")
                # and prove the token is really dead, not just marked revoked
                anon = httpx.Client(base_url=args.base, timeout=30)
                after = anon.get(f"/share/annotate/{raw}").status_code
                print(f"   revoke annotation link {link_id}: HTTP {res.status_code} "
                      f"· token now answers {after} (expected 404)")
            if agent_id:
                res = admin.delete(f"/api/agents/{agent_id}")
                print(f"   delete agent {agent_id}: HTTP {res.status_code}")
            for ds in reversed(datasets):
                res = admin.delete(f"/api/eval/datasets/{ds}")
                print(f"   delete dataset {ds}: HTTP {res.status_code}")
            for user_id in users:
                res = admin.delete(f"/api/users/{user_id}")
                print(f"   delete user {user_id}: HTTP {res.status_code}")
        summary()
    return 0 if all(v == "PASS" for _, v, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
