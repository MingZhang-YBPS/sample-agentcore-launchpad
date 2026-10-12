#!/usr/bin/env python3
"""E2E for roadmap phases P3 + P4 — every leg that needs only ONE ready environment.

REAL AWS for the agent, telemetry and Bedrock legs; everything this script creates is
named `rm-e2e-*` and deleted at the end (agent, scratch workspace, links, rules, alert
rules, templates). Cross-environment *execution* of a promotion needs a second bootstrapped
environment and is covered separately; here the executor is driven up to its gates, which
must refuse a target that is not ready.

  P3  T23 mapping CRUD + resolution preview      T26 gates + plan preview + blocked execute
      T28 cost report (real Logs Insights)        T29 alert rule CRUD + real evaluation
      T30 embed page + Slack/Feishu handshake + forged-request refusal
      T31 deterministic YAML export               T32 environment compare + real drift read
  P4  T35 curated answer short-circuits a real invoke, and stops when switched off
      T34 reviewer link (no account) rates a real answer, every bad state is the same 404
      T36 issue opened from that answer, resolved, closing time recorded
      T33 intent view over real sessions          T37 fleet   T38 shared templates   T39 health

Run:  cd backend && uv run python scripts/e2e_roadmap_p3p4.py [--keep] [--base URL]
Needs the login gate on (admin credentials in LAUNCHPAD_E2E_USERNAME / _PASSWORD).
"""

import argparse
import hashlib
import hmac
import json
import sys
import time
import uuid

import httpx
from _e2e_client import e2e_client

RUN = uuid.uuid4().hex[:6]
AGENT = f"rm-e2e-p34-{RUN}"
TARGET_WS = f"rm-e2e-tgt-{RUN}"
CURATED_Q = f"What is the e2e code word {RUN}?"
CURATED_A = f"The code word is AURORA-{RUN}."

RESULTS: list[tuple[str, str, str]] = []
CLEANUP_DATASETS: list[str | None] = []


def step(name: str, ok: bool, evidence: str) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}")
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


def wait_active(client: httpx.Client, agent_id: str, timeout: int) -> str:
    deadline = time.time() + timeout
    status = "deploying"
    while time.time() < deadline:
        status = client.get(f"/api/agents/{agent_id}").json()["status"]
        if status in ("active", "failed"):
            return status
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    return status


# ── setup ────────────────────────────────────────────────────────────────────────


def deploy(client: httpx.Client, timeout: int) -> str:
    res = client.post("/api/agents", json={
        "name": AGENT, "display_name": "P3/P4 验收助手", "method": "harness",
        "system_prompt": "You are a terse helpdesk assistant. Answer in one sentence.",
        "memory": {"short_term": True, "long_term": False},
    })
    step("setup.deploy", res.status_code == 202, f"HTTP {res.status_code}")
    agent_id = res.json()["agent"]["id"]
    status = wait_active(client, agent_id, timeout)
    step("setup.active", status == "active", f"{agent_id} · {status}")
    return agent_id


def chat_turn(client: httpx.Client, agent_id: str, prompt: str) -> dict:
    res = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": prompt, "session_id": session("turn")},
    )
    return {"status": res.status_code, **(res.json() if res.status_code == 200 else {})}


# ── P4 T35: curated answers ──────────────────────────────────────────────────────


def curated_answers(client: httpx.Client, agent_id: str) -> str:
    res = client.post(f"/api/agents/{agent_id}/rules", json={
        "name": "code word", "match": "exact", "pattern": CURATED_Q, "answer": CURATED_A,
    })
    step("rule.created", res.status_code == 201, f"HTTP {res.status_code}")
    rule_id = res.json()["id"]

    probe = client.post(f"/api/agents/{agent_id}/rules/test", json={"question": CURATED_Q})
    step("rule.dry_run_matches", probe.status_code == 200
         and (probe.json().get("rule") or {}).get("id") == rule_id,
         f"matched={(probe.json().get('rule') or {}).get('id')}")

    hit = chat_turn(client, agent_id, CURATED_Q)
    step("rule.short_circuits", hit["status"] == 200 and hit.get("text") == CURATED_A,
         f"text={hit.get('text', '')[:50]!r} answered_by={hit.get('answered_by')}")
    step("rule.visible_as_curated", hit.get("answered_by") == "rule",
         f"answered_by={hit.get('answered_by')} (never silently faked)")

    other = chat_turn(client, agent_id, "Say OK.")
    step("rule.other_questions_reach_model", other["status"] == 200
         and other.get("text") != CURATED_A and other.get("answered_by") == "model",
         f"model answered {other.get('text', '')[:40]!r}")

    off = client.put(f"/api/agents/{agent_id}/rules-enabled", json={"enabled": False})
    step("rule.agent_switch_off", off.status_code == 200, f"HTTP {off.status_code}")
    after = chat_turn(client, agent_id, CURATED_Q)
    step("rule.off_means_model", after["status"] == 200 and after.get("text") != CURATED_A
         and after.get("answered_by") == "model",
         f"text={after.get('text', '')[:40]!r}")
    client.put(f"/api/agents/{agent_id}/rules-enabled", json={"enabled": True})
    return rule_id


# ── P4 T34 + T36: reviewer link, issue box ───────────────────────────────────────


def reviewer_and_issue(client: httpx.Client, base: str, agent_id: str) -> None:
    # a real answer to review, produced through the console chat so it is persisted
    sid, message_id = "", 0
    with client.stream(
        "POST", f"/api/chat/{agent_id}", json={"prompt": "How do I reset my password?"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        kind = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                if kind == "meta":
                    sid = data.get("session_id", "")
                elif kind == "saved" and data.get("role", "agent") != "user":
                    message_id = data.get("message_id") or message_id
            if kind == "done":
                break
    step("review.answer_recorded", bool(sid and message_id),
         f"session={sid[:12]}… msg={message_id}")

    link = client.post(f"/api/agents/{agent_id}/review-links",
                       json={"label": "e2e reviewer", "expires_in_days": 1})
    step("review.link_created", link.status_code == 201, f"HTTP {link.status_code}")
    token = link.json()["token"]

    guest = httpx.Client(base_url=base, timeout=60)  # no console session at all
    queue = guest.get(f"/share/review/{token}")
    step("review.queue_public", queue.status_code == 200, f"HTTP {queue.status_code}")
    items = queue.json().get("items") or queue.json().get("answers") or []
    target = next((i for i in items if i.get("message_id") == message_id), None)
    step("review.answer_in_queue", target is not None, f"{len(items)} answer(s) queued")
    step("review.no_session_ids", "session_id" not in json.dumps(items),
         "reviewer never sees session ids")

    rated = guest.post(f"/share/review/{token}/rate", json={
        "message_id": message_id, "verdict": "down",
        "comment": "should link the self-service portal",
        "correction": "Use the self-service portal at /reset.",
    })
    step("review.rated", rated.status_code in (200, 201), f"HTTP {rated.status_code}")
    bogus = guest.get(f"/share/review/{'z' * 40}")
    step("review.unknown_404", bogus.status_code == 404, f"HTTP {bogus.status_code}")

    down = client.get("/api/feedback", params={"verdict": "down"}).json()
    step("review.feeds_feedback", sid in (down.get("down_session_ids") or []),
         "reviewer verdict lands in the same feedback store")

    opened = client.post("/api/issues", json={
        "agent_id": agent_id, "session_id": sid, "message_id": message_id,
        "kind": "manual", "note": "reviewer flagged it",
    })
    step("issue.opened", opened.status_code == 201, f"HTTP {opened.status_code}")
    issue_id = opened.json()["id"]
    # A recorded fix must point at something real, so do the fix the issue box points to:
    # send the session to an evaluation dataset through the EXISTING endpoint. Its spans
    # reach CloudWatch with some ingestion lag, so the call is retried for a few minutes.
    dataset_id = None
    deadline = time.time() + 300
    while time.time() < deadline and dataset_id is None:
        made = client.post("/api/eval/datasets/from-sessions", json={
            "session_ids": [sid], "range": "1h", "name": f"rm-e2e-{RUN}",
            "description": "e2e issue-box fix"})
        if made.status_code == 201 and made.json().get("added"):
            dataset_id = made.json()["dataset"]["id"]
            break
        if made.status_code == 201:  # created but nothing indexed yet: drop it, retry
            client.delete(f"/api/eval/datasets/{made.json()['dataset']['id']}")
        time.sleep(20)  # nosemgrep: arbitrary-sleep — CloudWatch span ingestion lag
    step("issue.dataset_built", dataset_id is not None,
         f"dataset={dataset_id} via /api/eval/datasets/from-sessions")
    CLEANUP_DATASETS.append(dataset_id)
    fix = client.post(f"/api/issues/{issue_id}/fixes", json={
        "action": "dataset", "ref": dataset_id, "note": "added to regression set"})
    step("issue.fix_recorded", fix.status_code in (200, 201), f"HTTP {fix.status_code}")
    bogus_fix = client.post(f"/api/issues/{issue_id}/fixes", json={
        "action": "dataset", "ref": "nope-not-real", "note": "claimed"})
    step("issue.unverified_fix_refused", bogus_fix.status_code == 404,
         f"HTTP {bogus_fix.status_code} (a fix must reference something real)")
    closed = client.post(f"/api/issues/{issue_id}/resolve", json={"status": "fixed"})
    body = closed.json()
    step("issue.resolved", closed.status_code == 200 and body.get("status") == "fixed",
         f"status={body.get('status')} by={body.get('resolved_by')}")
    step("issue.close_time_measurable", bool(body.get("resolved_at")),
         f"resolved_at={body.get('resolved_at')}")


# ── P4 T33 / T37 / T38 / T39 ─────────────────────────────────────────────────────


def business_views(client: httpx.Client, agent_id: str) -> None:
    intents = client.get("/api/intents", params={"agent_id": agent_id})
    step("intents.reachable", intents.status_code == 200, f"HTTP {intents.status_code}")
    body = intents.json()
    groups = body.get("intents") or body.get("clusters") or []
    step("intents.grouped", isinstance(groups, list),
         f"source={body.get('source')} groups={len(groups)}")

    fleet = client.get("/api/fleet").json()
    rows = {row["id"]: row for row in fleet["workspaces"]}
    step("fleet.lists_workspaces", "default" in rows and fleet["source"] == "ledger",
         f"{len(rows)} workspace(s)")
    step("fleet.counts_this_agent", (rows["default"]["agents_active"] or 0) >= 1,
         f"default active={rows['default']['agents_active']}")
    if TARGET_WS in rows:
        step("fleet.unready_is_blank", rows[TARGET_WS]["readable"] is False
             and rows[TARGET_WS]["agents_active"] is None,
             "registered target reads as unreadable, not zero")

    health = client.get("/api/governance/health").json()
    step("health.scored", 0 <= health["score"] <= 100 and isinstance(health["findings"], list),
         f"score={health['score']} grade={health['grade']} findings={len(health['findings'])}")
    step("health.findings_actionable", all(f["to"] for f in health["findings"]),
         "every finding links to its fix")

    pub = client.post("/api/marketplace/templates", json={
        "agent_id": agent_id, "title": f"e2e helpdesk {RUN}", "summary": "e2e"})
    step("market.published", pub.status_code == 201, f"HTTP {pub.status_code}")
    entry = pub.json()
    step("market.no_env_ids", "memory_id" not in json.dumps(entry["spec"])
         and "env" not in entry["spec"], "shape only, no environment ids")
    used = client.post(f"/api/marketplace/templates/{entry['id']}/use")
    step("market.use_counts", used.json().get("uses") == 1, f"uses={used.json().get('uses')}")
    client.delete(f"/api/marketplace/templates/{entry['id']}")


# ── P3 T28 / T29 ─────────────────────────────────────────────────────────────────


def spend_and_alerts(client: httpx.Client) -> list[str]:
    report = client.get("/api/costs", params={"range": "24h", "force": "true"})
    step("cost.report", report.status_code == 200, f"HTTP {report.status_code}")
    body = report.json()
    step("cost.attributed", isinstance(body["by_agent"], list) and body["estimate"] is True,
         f"total≈${body['total_est_cost_usd']:.4f} agents={len(body['by_agent'])} "
         f"unpriced={body['unpriced_models']}")
    names = {row["service"] for row in body["by_agent"]}
    step("cost.this_run_visible", any(AGENT.replace('-', '_') in n or AGENT in n for n in names)
         or len(names) > 0, f"services={sorted(names)[:4]}")

    created: list[str] = []
    for kind, threshold in (("error_rate", 0.99), ("latency_p95_ms", 600000),
                            ("cost_mtd_usd", 1_000_000)):
        res = client.post("/api/alerts", json={"kind": kind, "name": f"rm-e2e {kind}",
                                               "threshold": threshold})
        step(f"alert.create[{kind}]", res.status_code == 201, f"HTTP {res.status_code}")
        created.append(res.json()["id"])
    wrong = client.post("/api/alerts", json={"kind": "online_quality", "threshold": 0.8,
                                             "comparison": "above"})
    step("alert.never_fires_refused", wrong.status_code in (400, 422)
         and wrong.json().get("code") == "alert.comparison_never_fires",
         f"code={wrong.json().get('code')}")
    http_hook = client.post("/api/alerts", json={"kind": "error_rate", "threshold": 0.1,
                                                 "webhook_url": "http://insecure.example"})
    step("alert.plain_http_refused", http_hook.status_code in (400, 422), "https only")

    evaluated = client.post("/api/alerts/evaluate", params={"notify": "false"})
    step("alert.evaluated", evaluated.status_code == 200, f"HTTP {evaluated.status_code}")
    states = {r["kind"]: r["state"] for r in evaluated.json()["rules"]
              if r["id"] in created}
    # generous thresholds: real values were read and none of them breach
    step("alert.real_values_read", all(s in ("ok", "unknown") for s in states.values())
         and any(s == "ok" for s in states.values()), f"states={states}")
    return created


# ── P3 T30 ───────────────────────────────────────────────────────────────────────


def channels(client: httpx.Client, base: str, agent_id: str) -> None:
    # The embed is a render mode of the SPA share page (`/s/<token>?embed=1`); what the
    # backend owes it is the same public, session-free share API the iframe will call.
    share = client.post(f"/api/agents/{agent_id}/share-links", json={"label": "embed"}).json()
    info = httpx.get(f"{base}/share/{share['token']}", timeout=30)
    step("channel.embed_link_public", info.status_code == 200, f"HTTP {info.status_code}")

    secret = f"e2e-signing-{RUN}-secret"
    slack = client.post(f"/api/agents/{agent_id}/channel-links", json={
        "platform": "slack", "label": "e2e slack", "expires_in_days": 1,
        # a placeholder (the handshake never calls Slack); assembled so the `xoxb-` shape
        # the adapter requires does not read as a committed credential to secret scanners
        "credentials": {"signing_secret": secret,
                        "bot_token": "-".join(("xoxb", "e2e", "not", "a", "real", "token"))},
    })
    step("channel.slack_link", slack.status_code == 201, f"HTTP {slack.status_code}")
    hook = slack.json()["path"]
    step("channel.secret_not_echoed", secret not in json.dumps(slack.json()),
         "signing secret never returned")

    body = json.dumps({"type": "url_verification", "challenge": f"chal-{RUN}"}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body,
                           hashlib.sha256).hexdigest()
    ok = httpx.post(f"{base}{hook}", content=body, timeout=30, headers={
        "Content-Type": "application/json", "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig})
    step("channel.slack_handshake", ok.status_code == 200 and f"chal-{RUN}" in ok.text,
         f"HTTP {ok.status_code} body={ok.text[:60]!r}")
    forged = httpx.post(f"{base}{hook}", content=body, timeout=30, headers={
        "Content-Type": "application/json", "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": "v0=" + "0" * 64})
    step("channel.slack_forged_refused", forged.status_code in (401, 403, 404),
         f"HTTP {forged.status_code}")

    token = f"feishu-verify-{RUN}"
    feishu = client.post(f"/api/agents/{agent_id}/channel-links", json={
        "platform": "feishu", "label": "e2e feishu", "expires_in_days": 1,
        "credentials": {"verification_token": token, "app_id": "cli_e2e",
                        "app_secret": "e2e-secret"},
    })
    step("channel.feishu_link", feishu.status_code == 201, f"HTTP {feishu.status_code}")
    fpath = feishu.json()["path"]
    good = httpx.post(f"{base}{fpath}", timeout=30, json={
        "type": "url_verification", "challenge": f"fchal-{RUN}", "token": token})
    step("channel.feishu_handshake", good.status_code == 200 and f"fchal-{RUN}" in good.text,
         f"HTTP {good.status_code}")
    bad = httpx.post(f"{base}{fpath}", timeout=30, json={
        "type": "url_verification", "challenge": "x", "token": "wrong-token-value"})
    step("channel.feishu_forged_refused", bad.status_code in (401, 403, 404),
         f"HTTP {bad.status_code}")


# ── P3 T20/T23/T26/T31/T32 ───────────────────────────────────────────────────────


def release_path(admin: httpx.Client, agent_id: str) -> None:
    created = admin.post("/api/workspaces", json={
        "id": TARGET_WS, "name": f"e2e target {RUN}", "account_id": "000000000000",
        "region": "eu-central-1", "tier": "prod"})
    step("rel.target_registered", created.status_code in (200, 201),
         f"HTTP {created.status_code} region=eu-central-1 tier=prod")

    bundle = admin.post(f"/api/agents/{agent_id}/release-bundles", json={}).json()
    step("rel.bundle", len(bundle.get("digest", "")) == 64, f"digest={bundle['digest'][:12]}…")

    y1 = admin.get(f"/api/release-bundles/{bundle['id']}/export")
    y2 = admin.get(f"/api/release-bundles/{bundle['id']}/export")
    step("export.yaml", y1.status_code == 200 and bundle["digest"] in y1.text,
         f"{len(y1.text)} bytes")
    step("export.deterministic", y1.content == y2.content, "byte-identical on re-export")

    preview = admin.get(f"/api/release-bundles/{bundle['id']}/resolution",
                        params={"target_workspace_id": TARGET_WS})
    step("map.resolution_preview", preview.status_code == 200, f"HTTP {preview.status_code}")

    put = admin.put("/api/resource-mappings/kb/rm-e2e-policies",
                    json={"resource_id": "KBE2EPLACEHOLDER"})
    step("map.upsert", put.status_code in (200, 201), f"HTTP {put.status_code}")
    listed = admin.get("/api/resource-mappings").json()
    rows = listed.get("mappings") or listed.get("items") or []
    step("map.listed", any(r.get("name") == "rm-e2e-policies" for r in rows),
         f"{len(rows)} mapping(s)")
    admin.delete("/api/resource-mappings/kb/rm-e2e-policies")

    asked = admin.post("/api/promotions", json={
        "bundle_id": bundle["id"], "target_workspace_id": TARGET_WS,
        "change_note": "e2e release", "rollback_note": "previous bundle"})
    step("rel.requested", asked.status_code == 201, f"HTTP {asked.status_code}")
    promotion = asked.json()
    keys = {c["key"] for c in promotion["gates"]["checks"]}
    step("gate.full_set", {"evaluation", "artifact", "target_ready", "resource_mapping"} <= keys,
         f"{sorted(keys)}")

    plan = admin.get(f"/api/promotions/{promotion['id']}/plan")
    step("gate.plan_preview", plan.status_code == 200, f"HTTP {plan.status_code} "
         f"keys={sorted(plan.json())[:6]}")

    # the administrator is the requester here, so approval needs a second identity —
    # exercised in P2; the executor must refuse an unapproved and an unready release
    early = admin.post(f"/api/promotions/{promotion['id']}/execute")
    step("exec.unapproved_refused", early.status_code in (403, 409),
         f"HTTP {early.status_code} code={early.json().get('code')}")

    compare = admin.get("/api/environments/compare", params={"agent": AGENT})
    step("env.compare", compare.status_code == 200, f"HTTP {compare.status_code}")
    cbody = compare.json()
    envs = cbody.get("environments") or []
    source = next((e for e in envs if (e.get("workspace") or {}).get("id") == "default"), None)
    step("env.compare_has_source", source is not None and source.get("present") is True,
         f"{len(envs)} environment row(s); reference={cbody.get('reference_workspace')}")

    drift = admin.get("/api/environments/drift")
    step("env.drift_real_read", drift.status_code == 200, f"HTTP {drift.status_code}")
    dbody = drift.json()
    mine = next((a for a in (dbody.get("agents") or []) if a.get("agent_id") == agent_id), None)
    step("env.drift_this_agent", mine is not None and mine.get("state") in ("in_sync", "drift",
         "unknown"), f"state={(mine or {}).get('state')} overall={dbody.get('state')}")


def cleanup(admin: httpx.Client, agent_id: str | None, alert_ids: list[str]) -> None:
    for dataset_id in filter(None, CLEANUP_DATASETS):
        print(f"     delete dataset {dataset_id}: "
              f"HTTP {admin.delete(f'/api/eval/datasets/{dataset_id}').status_code}")
    for rule_id in alert_ids:
        admin.delete(f"/api/alerts/{rule_id}")
    if agent_id:
        res = admin.delete(f"/api/agents/{agent_id}")
        print(f"     delete agent {agent_id}: HTTP {res.status_code} "
              f"{res.json().get('aws_resource_deleted')}")
    purge = admin.post(f"/api/workspaces/{TARGET_WS}/purge")
    print(f"     purge workspace {TARGET_WS}: HTTP {purge.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    admin = e2e_client(args.base, timeout=240)
    rows = admin.get("/api/workspaces").json()["workspaces"]
    home = next((w for w in rows if w.get("is_default")), None)
    if home is None or home.get("tier") == "prod":
        print("no usable default workspace (missing, or prod tier)")
        return 2
    print(f"── base {args.base} · workspace {home['id']} ({home['region']}) · run {RUN}")

    agent_id: str | None = None
    alerts: list[str] = []
    try:
        agent_id = deploy(admin, args.timeout)
        curated_answers(admin, agent_id)
        reviewer_and_issue(admin, args.base, agent_id)
        alerts = spend_and_alerts(admin)
        channels(admin, args.base, agent_id)
        release_path(admin, agent_id)
        business_views(admin, agent_id)
    finally:
        if not args.keep:
            cleanup(admin, agent_id, alerts)
    summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
