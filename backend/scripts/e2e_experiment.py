#!/usr/bin/env python3
"""E2E: configuration A/B plus independent Runtime Canary against real AWS.

Drives both user workflows in order, exactly like the UI:

1. Configuration experiment on ``eval-target``: recommend → accept → bundles →
   gateway → abtest → traffic → verdict → promote (non-significant override).
2. Runtime Canary (Model 1) on the SAME agent, sourced from that experiment:
   ``POST /api/runtime-canaries {agent_id, candidate: {system_prompt}, ...}`` →
   setup (mints a candidate runtime version, dedicated gateway, stable/treatment
   endpoints, per-arm online evals, 90/10 target A/B) → traffic → verdict →
   advance 50/50 → traffic → verdict → advance 1/99 → traffic → verdict →
   complete (override when the verdict is a tie / not significant). A
   ``control-wins`` verdict rolls the canary back instead — the honest outcome.
   The AWS state (A/B test, online evals, gateway, endpoints) is read back
   before cleanup, and their deletion is verified after it.
3. Canary cleanup, then experiment cleanup; the experiment's shared gateway
   must survive.

The script owns a throwaway 40-prompt traffic dataset (deleted at the end): the
canary's 90/10 split needs enough sessions for the treatment arm to be scored.
On failure it rolls back and cleans the canary and the experiment it created
unless ``--keep`` is given (then a re-run resumes them).

Async actions return 202; this waits on running_action/progress like the
page's poll loop does.

Run:  cd backend && uv run python scripts/e2e_experiment.py --base URL [--keep]
"""

import argparse
import json
import os
import secrets
import sys
import time

import boto3
import httpx
from _e2e_client import e2e_client

AGENT = "eval-target"
AGENT_PROMPT = (
    "You are a precise math assistant. Use the calculator tool for arithmetic "
    "and answer with just the result."
)
CANDIDATE_PROMPT = (
    "You are a rigorous math assistant v2. ALWAYS verify with the calculator "
    "tool and reply with ONLY the final number."
)
# 40 distinct sessions per send: at 90/10 the treatment arm gets ~4 of them, so one
# send is usually enough for both arms to be scored
TRAFFIC_PROMPTS = [
    f"What is {17 + 7 * i} * {23 + 3 * i} + {5 + i}?" for i in range(40)
]
# resend this many times before giving up on an insufficient verdict at one stage
MAX_TRAFFIC_ROUNDS = 4
BLOCKED = ("insufficient-data", "insufficient-n")


# a laptop on flaky Wi-Fi loses DNS for a minute or two; an hour-long run must ride
# that out instead of dying (and stranding the canary) mid-poll
NETWORK_RETRY_S = 30
NETWORK_RETRIES = 20
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
IDEMPOTENT_ONLY = (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError)


def ride_out_network_drops(client: httpx.Client) -> httpx.Client:
    """Retry a request that never reached the console (any method), and a GET whose
    answer was lost; a POST that may have been received is never re-sent."""
    send = client.send

    def send_retrying(request, **kwargs):
        for attempt in range(1, NETWORK_RETRIES + 1):
            try:
                return send(request, **kwargs)
            except NEVER_SENT + IDEMPOTENT_ONLY as exc:
                retryable = isinstance(exc, NEVER_SENT) or request.method == "GET"
                if not retryable or attempt == NETWORK_RETRIES:
                    raise
                print(f"  [{time.strftime('%H:%M:%S')}] network: {type(exc).__name__} on "
                      f"{request.method} {request.url.path} — retry {attempt} in "
                      f"{NETWORK_RETRY_S}s", flush=True)
                # Network-drop backoff is intentionally paced and attempt-bounded.
                time.sleep(NETWORK_RETRY_S)  # nosemgrep: arbitrary-sleep
        raise AssertionError("unreachable")

    client.send = send_retrying
    return client


def action(
    client,
    collection,
    row_id,
    response_key,
    name,
    timeout_s=1800,
    **fields,
):
    """POST one stage action, then poll the row until the runner is idle."""
    res = client.post(f"{collection}/{row_id}/action",
                      json={"action": name, **fields})
    res.raise_for_status()
    row = res.json()[response_key]
    if res.status_code != 202:  # sync action — done already
        return row
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        row = client.get(f"{collection}/{row_id}").json()
        line = row.get("progress") or row.get("stage")
        if line != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {line}")
            last = line
        if not row.get("running_action"):
            if row.get("error"):
                raise RuntimeError(f"action {name} failed: {row['error']}")
            return row
        # Real-AWS action polling is intentionally paced and attempt-bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"action {name} timed out")


def ensure_agent(client, name, prompt):
    agents = client.get("/api/agents").json()["agents"]
    agent = next((a for a in agents if a["name"] == name and a["status"] == "active"), None)
    if agent:
        return agent
    print(f"── deploying {name}…")
    res = client.post("/api/agents", json={
        "name": name, "method": "zip_runtime", "system_prompt": prompt,
        "memory": {"short_term": False, "long_term": False},
    })
    res.raise_for_status()
    agent_id = res.json()["agent"]["id"]
    for _ in range(90):
        agent = client.get(f"/api/agents/{agent_id}").json()
        if agent["status"] in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    assert agent["status"] == "active"
    return agent


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"),
                        help="region of the AWS readback (the workspace region)")
    parser.add_argument("--keep", action="store_true",
                        help="on failure, leave the canary/experiment for a resume")
    args = parser.parse_args()

    client = ride_out_network_drops(e2e_client(args.base, timeout=300))
    agent = ensure_agent(client, AGENT, AGENT_PROMPT)
    dataset = client.post("/api/eval/datasets", json={
        "name": f"e2e-exp-traffic-{secrets.token_hex(3)}",
        "description": "e2e_experiment.py throwaway traffic (deleted at the end)",
        "items": [{"prompt": prompt} for prompt in TRAFFIC_PROMPTS],
    })
    dataset.raise_for_status()
    dataset = dataset.json()
    print(f"── traffic dataset: {dataset['name']} ({dataset['item_count']} prompts)")
    state = {"exp_id": None, "canary_id": None, "done": False}
    try:
        return run(client, agent, dataset, state, args.region)
    finally:
        if not state["done"] and not args.keep:
            best_effort_cleanup(client, state)
        client.delete(f"/api/eval/datasets/{dataset['id']}")
        print(f"── deleted traffic dataset {dataset['name']}")


def run(client, agent, dataset, state, region) -> int:

    # resume an interrupted run instead of tripping the one-running guard —
    # completed actions are skipped by artifact presence, like the UI does
    running = [e for e in client.get("/api/experiments").json()["experiments"]
               if e["status"] in ("running", "ready") and e["agent_id"] == agent["id"]]
    if running:
        exp = running[0]
        exp_id = exp["id"]
        state["exp_id"] = exp_id
        print(f"── resuming running experiment {exp_id} "
              f"(artifacts: {sorted(exp['artifacts'])})…")
    else:
        print("── create experiment (no stage work yet)…")
        res = client.post("/api/experiments", json={"agent_id": agent["id"]})
        res.raise_for_status()
        exp = res.json()
        exp_id = exp["id"]
        state["exp_id"] = exp_id
        assert exp["stage"] == "recommend" and set(exp["artifacts"]) == {"agent_meta"}, \
            "create must defer all stage work"

    def ensure(name, done, **fields):
        nonlocal exp
        if done(exp["artifacts"]):
            print(f"── {name}: already done, skipping")
            return exp
        print(f"── {name}…")
        exp = action(
            client,
            "/api/experiments",
            exp_id,
            "experiment",
            name,
            **fields,
        )
        return exp

    ensure("recommend", lambda a: "recommend" in a)
    rec = exp["artifacts"]["recommend"]
    print("  prompt suggestion:", rec["recommended_prompt"][:220].replace("\n", " "))
    print("  tool suggestions:", json.dumps(rec["tool_descriptions"])[:220])

    ensure("accept", lambda a: a["recommend"].get("accepted_prompt"),
           accepted_prompt=rec["recommended_prompt"])

    ensure("bundles", lambda a: "bundles" in a)
    art = exp["artifacts"]
    print("  control:  ", art["bundles"]["control"]["arn"])
    print("  treatment:", art["bundles"]["treatment"]["arn"])

    ensure("gateway", lambda a: "gateway" in a)
    art = exp["artifacts"]
    print("  gateway:", art["gateway"]["gateway_id"], "· target",
          art["gateway"]["target_v1"])

    ensure("abtest", lambda a: "abtest" in a)
    art = exp["artifacts"]
    print("  abtest:", art["abtest"]["ab_test_id"], "· weights",
          [v["weight"] for v in art["abtest"]["variants"]])

    traffic_fields = {"dataset_id": dataset["id"]}
    ensure("traffic", lambda a: "traffic" in a, **traffic_fields)
    print("  traffic:", {k: v for k, v in exp["artifacts"]["traffic"].items()
                         if k != "session_ids"})

    print("── verdict (monitoring)…")
    exp = action(
        client, "/api/experiments", exp_id, "experiment", "verdict"
    )
    verdict = exp["artifacts"]["verdict"]
    print("  verdict:", json.dumps({k: v for k, v in verdict.items()
                                    if k != "metrics"}))
    print("  metrics:", json.dumps(verdict["metrics"])[:400])

    # a small replay never makes a significant verdict: the e2e checks the promote
    # mechanics, so it confirms the override the console asks for
    ensure("promote", lambda a: "promote" in a, allow_non_significant=True)
    promote = exp["artifacts"]["promote"]
    print("  deployed version:", promote["agent_version"],
          "· A/B test:", promote["ab_test_status"])

    canary, canary_outcome = run_canary(
        client, agent, exp_id, traffic_fields, state, region
    )

    print("── configuration experiment cleanup…")
    exp = action(
        client, "/api/experiments", exp_id, "experiment", "cleanup"
    )
    experiment_cleanup = exp["artifacts"]["cleanup"]
    for row in experiment_cleanup:
        print(f"  {row['status']:<8} {row['category']:<28} {row['detail'][:60]}")

    print("── experiment's shared gateway untouched check:")
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    gw = control.get_gateway(gatewayIdentifier=exp["artifacts"]["gateway"]["gateway_id"])
    print(f"  {gw['name']} status: {gw['status']}")
    assert gw["status"] == "READY", "experiment cleanup must keep the shared gateway"
    state["done"] = True

    failed = [
        row
        for row in canary["artifacts"]["cleanup"] + experiment_cleanup
        if row["status"] == "skipped" and not row["category"].startswith("s3:")
    ]
    if failed:
        print(f"!! cleanup left {len(failed)} resources: {[r['category'] for r in failed]}")
        return 1
    print(f"E2E EXPERIMENT: PASS (canary {canary_outcome})")
    return 0


def current_round(canary):
    """The ramp round of the canary's current stage (the console's view)."""
    stage = int(canary["artifacts"]["setup"].get("ramp_stage", 0))
    rounds = canary["artifacts"].get("rounds") or []
    return stage, next((r for r in rounds if r["ramp_stage"] == stage), None)


def run_canary(client, agent, exp_id, traffic_fields, state, region):
    """Create → setup → (traffic → verdict → advance)×2 → complete → cleanup.

    Returns ``(canary, outcome)`` where outcome is ``completed`` or
    ``rolled-back`` (a ``control-wins`` verdict at some stage)."""
    running = [
        row
        for row in client.get("/api/runtime-canaries").json()["canaries"]
        if row["status"] == "running" and row["champion_agent_id"] == agent["id"]
    ]
    if running:
        canary = running[0]
        print(f"── resuming Runtime Canary {canary['id']}…")
    else:
        print("── create Runtime Canary (candidate = edited system prompt)…")
        res = client.post("/api/runtime-canaries", json={
            "agent_id": agent["id"],
            "candidate": {"system_prompt": CANDIDATE_PROMPT},
            "source_experiment_id": exp_id,
        })
        if res.status_code != 201:
            raise RuntimeError(f"canary create → {res.status_code} {res.text[:300]}")
        canary = res.json()
        assert canary["champion_agent_id"] == agent["id"]
        assert canary["source_experiment_id"] == exp_id
        assert set(canary["artifacts"]) >= {"agent_meta", "edited_spec"}, \
            "create must defer all AWS work to setup"
        assert canary["artifacts"]["edited_spec"]["system_prompt"] == CANDIDATE_PROMPT
    canary_id = canary["id"]
    state["canary_id"] = canary_id

    def canary_action(name, **fields):
        nonlocal canary
        print(f"── canary {name}…")
        canary = action(
            client, "/api/runtime-canaries", canary_id, "canary", name, **fields,
        )
        return canary

    if not (canary["artifacts"].get("setup") or {}).get("ab_test_id"):
        canary_action("setup")
    setup = canary["artifacts"]["setup"]
    print(f"  v_current={setup['v_current']} v_candidate={setup['v_candidate']} "
          f"gateway={setup['gateway_id']}")
    print(f"  endpoints stable={setup['stable_endpoint']} "
          f"treatment={setup['treatment_endpoint']} · A/B {setup['ab_test_id']} "
          f"weights {setup['weights']}")
    assert setup["v_candidate"] != setup["v_current"], "candidate must be a new version"

    outcome = None
    while outcome is None:
        canary = client.get(f"/api/runtime-canaries/{canary_id}").json()
        stage, current = current_round(canary)
        sends = len((current or {}).get("traffic_attempts") or [])
        verdict = (current or {}).get("verdict")
        if not sends or (verdict and verdict["verdict"] in BLOCKED):
            if sends >= MAX_TRAFFIC_ROUNDS:
                raise RuntimeError(
                    f"canary stage {stage} still {verdict['verdict']} after "
                    f"{sends} traffic rounds: {json.dumps(verdict)[:300]}"
                )
            canary_action("traffic", **traffic_fields)
            _, current = current_round(canary)
            attempt = current["traffic_attempts"][-1]
            print(f"  stage {stage} traffic #{len(current['traffic_attempts'])}: "
                  f"{attempt.get('sent', '?')} sent · {attempt.get('failed', 0)} failed "
                  f"· baseline_n {attempt.get('baseline_n')}")
            verdict = None
        if not verdict:
            canary_action("verdict")
            _, current = current_round(canary)
            verdict = current["verdict"]
        summary = {k: v for k, v in verdict.items() if k != "metrics"}
        print(f"  stage {stage} ({canary['artifacts']['setup']['weights']}) verdict:",
              json.dumps(summary)[:400])
        result = verdict["verdict"]
        if result in BLOCKED:
            continue  # more traffic at the same weights
        if result == "control-wins":
            print("── control wins: rolling the canary back (production keeps v_current)")
            canary_action("rollback")
            rollback = canary["artifacts"].get("rollback") or {}
            print("  rollback:", json.dumps(rollback)[:300])
            assert rollback.get("restored_version"), "rollback must restore a version"
            outcome = "rolled-back"
            break
        override = result == "tie" or verdict.get("significant") is False
        if stage < 2:
            canary_action("advance", allow_non_significant=override)
            print("  advanced to", canary["artifacts"]["setup"]["weights"])
        else:
            canary_action("complete", allow_non_significant=override)
            complete = canary["artifacts"]["complete"]
            print("  complete:", json.dumps(complete))
            assert canary["status"] == "completed"
            ledger = client.get(f"/api/agents/{agent['id']}").json()
            print(f"  ledger: {ledger['name']} version={ledger['version']} "
                  f"prompt={ledger['spec']['system_prompt'][:50]!r}")
            assert str(ledger["version"]) == str(setup["v_candidate"]), \
                "complete must record the candidate as production"
            outcome = "completed"

    aws_readback(canary["artifacts"]["setup"], region)

    canary_action("cleanup")
    for row in canary["artifacts"]["cleanup"]:
        print(f"  {row['status']:<8} {row['category']:<34} {row['detail'][:60]}")
    assert canary["status"] == "cleaned"
    verify_canary_gone(canary["artifacts"]["setup"], region)
    return canary, outcome


def aws_readback(setup, region):
    """What AWS holds for the canary right before cleanup."""
    print("── AWS readback (before cleanup):")
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    data = boto3.client("bedrock-agentcore", region_name=region)
    ab = data.get_ab_test(abTestId=setup["ab_test_id"])
    print(f"  A/B test {ab.get('name')} · status={ab.get('status')} "
          f"· execution={ab.get('executionStatus')} · variants="
          f"{[(v.get('name'), v.get('weight')) for v in ab.get('variants', [])]}")
    for role in ("champion", "challenger"):
        oe = control.get_online_evaluation_config(
            onlineEvaluationConfigId=setup[role]["online_eval_id"]
        )
        print(f"  online eval {oe.get('onlineEvaluationConfigName')} · status="
              f"{oe.get('status')} · execution={oe.get('executionStatus')}")
    gw = control.get_gateway(gatewayIdentifier=setup["gateway_id"])
    print(f"  gateway {gw['name']} · status={gw['status']}")
    targets = control.list_gateway_targets(
        gatewayIdentifier=setup["gateway_id"], maxResults=20
    ).get("items", [])
    print(f"  gateway targets: {[(t.get('name'), t.get('status')) for t in targets]}")
    endpoints = control.list_agent_runtime_endpoints(
        agentRuntimeId=setup["runtime_id"], maxResults=50
    ).get("runtimeEndpoints", [])
    for ep in endpoints:
        print(f"  endpoint {ep.get('name')} · live v{ep.get('liveVersion')} "
              f"· {ep.get('status')}")


def verify_canary_gone(setup, region):
    """Cleanup deletes the dedicated gateway, both named endpoints and the evals."""
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    deadline = time.time() + 300
    while True:
        try:
            gw = control.get_gateway(gatewayIdentifier=setup["gateway_id"])
            status = gw["status"]
        except control.exceptions.ResourceNotFoundException:
            status = "gone"
        if status == "gone" or time.time() > deadline:
            break
        # DeleteGateway is async; real-AWS polling is paced and bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    print(f"  dedicated gateway {setup['gateway_id']}: {status}")
    assert status == "gone", "the canary's dedicated gateway outlived cleanup"
    names = {
        ep.get("name")
        for ep in control.list_agent_runtime_endpoints(
            agentRuntimeId=setup["runtime_id"], maxResults=50
        ).get("runtimeEndpoints", [])
        if ep.get("status") != "DELETING"
    }
    for name in (setup["stable_endpoint"], setup["treatment_endpoint"]):
        print(f"  endpoint {name}: {'LEFT' if name in names else 'gone'}")
        assert name not in names, f"endpoint {name} outlived cleanup"
    for role in ("champion", "challenger"):
        config_id = setup[role]["online_eval_id"]
        try:
            oe = control.get_online_evaluation_config(onlineEvaluationConfigId=config_id)
            status = oe.get("status")
        except control.exceptions.ResourceNotFoundException:
            status = "gone"
        print(f"  online eval {config_id}: {status}")
        assert status in ("gone", "DELETING"), f"online eval {config_id} outlived cleanup"


def best_effort_cleanup(client, state):
    """A failed run must not leave the canary routing traffic or AWS resources."""
    print("── FAILED: best-effort cleanup (pass --keep to resume instead)…")
    canary_id = state.get("canary_id")
    if canary_id:
        row = client.get(f"/api/runtime-canaries/{canary_id}").json()
        steps = (["rollback"] if row.get("status") == "running" else []) + ["cleanup"]
        for step in steps:
            try:
                action(client, "/api/runtime-canaries", canary_id, "canary", step)
                print(f"  canary {step}: ok")
            except Exception as exc:  # noqa: BLE001 — keep going to the next step
                print(f"  canary {step} failed: {type(exc).__name__}: {exc}")
    exp_id = state.get("exp_id")
    if exp_id:
        try:
            action(client, "/api/experiments", exp_id, "experiment", "cleanup")
            print("  experiment cleanup: ok")
        except Exception as exc:  # noqa: BLE001
            print(f"  experiment cleanup failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
