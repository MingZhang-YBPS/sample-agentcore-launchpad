#!/usr/bin/env python3
"""E2E for roadmap phase P2 (T13–T22) — REAL AWS for the agent legs, cleaned up.

Covers, in order:

  1. deploy       a harness agent to hang everything else off (one deploy, reused)
  2. share        a link is created once, answers without any console session, records
                  use, and every bad state (revoked, unknown) reads as the same 404 (T13/T14)
  3. feedback     a thumbs-down from the share page surfaces through /api/feedback and
                  its session is offered to the dataset builder (T15)
  4. api keys     a key scoped to one agent reaches it and nothing else, an expired key
                  401s, and usage is counted (T16)
  5. snapshots    a publish writes a snapshot; a redeploy writes a second; the diff names
                  the changed field; rollback re-publishes the older spec (T18)
  6. bundle       bundling is idempotent per digest and records the artifact (T20)
  7. promotion    a member requests a release, cannot approve it, and an operator can;
                  the gates and the target diff are present (T19/T21)
  8. inbox        the pending release shows up for an administrator with a link (T22)

Run:  cd backend && uv run python scripts/e2e_roadmap_p2.py [--keep] [--base URL]

Needs the login gate on (it exercises member vs operator vs admin), so the target must
run with LAUNCHPAD_AUTH_PASSWORD set, and LAUNCHPAD_E2E_USERNAME/PASSWORD must name the
administrator. Refuses a prod-tier workspace.
"""

import argparse
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from _e2e_client import e2e_client

RUN = uuid.uuid4().hex[:6]
AGENT_NAME = f"rm-e2e-p2-{RUN}"
TARGET_WS = f"rm-e2e-t-{RUN}"
PROMPT = "You are a terse assistant. Answer in one short sentence."
PROMPT_V2 = "You are a terse assistant. Always answer in exactly one sentence."
DEV = {"username": f"rm-dev-{RUN}", "email": f"rm-dev-{RUN}@example.com",
       "password": f"Pw-{RUN}-aA1!"}
OPS = {"username": f"rm-ops-{RUN}", "email": f"rm-ops-{RUN}@example.com",
       "password": f"Pw-{RUN}-bB2!"}

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
        print(f"{verdict:<5} {name:<26} {evidence}")


def session(label: str) -> str:
    """AgentCore needs ≥33 characters, so a script-chosen id is padded."""
    return f"{label}-{RUN}-{uuid.uuid4().hex}"[:80]


def account(client: httpx.Client, base: str, creds: dict, role: str) -> tuple[httpx.Client, str]:
    """Register, approve with a workspace grant and sign in as `creds`."""
    res = client.post("/api/auth/register", json=creds)
    step(f"acct.register[{role}]", res.status_code in (200, 201), f"HTTP {res.status_code}")
    listing = client.get("/api/users", params={"q": creds["username"]}).json()
    user_id = next(u["id"] for u in listing["items"] if u["username"] == creds["username"])
    patch = {"status": "active", "workspaces": ["default"]}
    if role != "member":
        patch["role"] = role
    res = client.patch(f"/api/users/{user_id}", json=patch)
    step(f"acct.approve[{role}]", res.status_code == 200, f"HTTP {res.status_code}")

    signed = httpx.Client(base_url=base, timeout=120)
    login = signed.post(
        "/api/auth/login",
        json={"username": creds["username"], "password": creds["password"]},
    )
    login.raise_for_status()
    token = login.cookies.get("launchpad_session") or signed.cookies.get("launchpad_session")
    signed.headers["Cookie"] = f"launchpad_session={token}"
    # pinned so a later second grant (the release target) never makes calls ambiguous
    signed.headers["X-Workspace"] = "default"
    return signed, user_id


def deploy(client: httpx.Client, timeout: int) -> str:
    res = client.post(
        "/api/agents",
        json={
            "name": AGENT_NAME,
            "display_name": "P2 验收助手",
            "method": "harness",
            "system_prompt": PROMPT,
            "memory": {"short_term": True, "long_term": False},
        },
    )
    step("deploy.accepted", res.status_code == 202, f"HTTP {res.status_code}")
    agent_id = res.json()["agent"]["id"]
    deadline = time.time() + timeout
    status = "deploying"
    while time.time() < deadline:
        status = client.get(f"/api/agents/{agent_id}").json()["status"]
        if status in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    step("deploy.active", status == "active", f"{agent_id} · {status}")
    return agent_id


# ── T13/T14: share links ─────────────────────────────────────────────────────────


def share(client: httpx.Client, base: str, agent_id: str) -> tuple[str, str, int]:
    res = client.post(
        f"/api/agents/{agent_id}/share-links",
        json={"label": "e2e", "expires_in_days": 1},
    )
    step("share.created", res.status_code in (200, 201), f"HTTP {res.status_code}")
    body = res.json()
    token, path = body["token"], body["path"]
    step("share.token_once", bool(token) and "token" not in str(body.get("prefix", "")),
         f"path={path}")

    # A brand-new client: no console cookie, no X-Workspace header.
    guest = httpx.Client(base_url=base, timeout=120)
    info = guest.get(f"/share/{token}")
    step("share.public_read", info.status_code == 200, f"HTTP {info.status_code}")
    step("share.agent_named", info.json().get("agent", {}).get("display_name") == "P2 验收助手",
         f"agent={info.json().get('agent')}")

    # The visitor does NOT name the session: the server mints it and hands it back on
    # `meta`, so a link cannot be used to probe or resume someone else's conversation
    # (`share._owned_session`). Later turns reuse what came back.
    sid = ""
    message_id = None
    with guest.stream(
        "POST",
        f"/share/{token}/chat",
        json={"prompt": "Say OK."},
        headers={"Accept": "text/event-stream"},
    ) as response:
        step("share.chat_open", response.status_code == 200, f"HTTP {response.status_code}")
        kind, saved, text = "", None, ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
                if kind == "meta":
                    sid = json.loads(data).get("session_id", "")
                elif kind == "saved":
                    saved = json.loads(data)
                elif kind == "delta":
                    text += json.loads(data).get("text", "")
            if kind == "done":
                break
    step("share.session_minted", bool(sid), f"session={sid[:16]}…")
    step("share.answered", bool(text.strip()), f"answer={text[:60]!r}")
    if isinstance(saved, dict):
        message_id = saved.get("message_id") or saved.get("id")
    step("share.saved_event", message_id is not None, f"saved={saved}")

    listed = client.get(f"/api/agents/{agent_id}/share-links").json()["links"]
    row = next((link for link in listed if link["id"] == body["id"]), None)
    step("share.use_counted", row is not None and (row["use_count"] or 0) >= 1,
         f"use_count={(row or {}).get('use_count')}")

    foreign = guest.post(
        f"/share/{token}/chat", json={"prompt": "hi", "session_id": session("not-mine")}
    )
    step(
        "share.foreign_session_404",
        foreign.status_code == 404,
        f"HTTP {foreign.status_code} (a visitor cannot name a session)",
    )

    unknown = guest.get(f"/share/{'z' * 40}")
    step("share.unknown_404", unknown.status_code == 404
         and unknown.json().get("code") == "share.not_found",
         f"HTTP {unknown.status_code} code={unknown.json().get('code')}")
    return token, sid, int(message_id or 0)


def share_revoked(client: httpx.Client, base: str, agent_id: str, token: str, link_id: str) -> None:
    res = client.post(f"/api/share-links/{link_id}/revoke")
    step("share.revoked", res.status_code in (200, 204), f"HTTP {res.status_code}")
    guest = httpx.Client(base_url=base, timeout=60)
    after = guest.get(f"/share/{token}")
    step(
        "share.revoked_404",
        after.status_code == 404 and after.json().get("code") == "share.not_found",
        f"HTTP {after.status_code} code={after.json().get('code')} (same as unknown)",
    )


# ── T15: feedback ────────────────────────────────────────────────────────────────


def feedback(client: httpx.Client, base: str, token: str, sid: str, message_id: int) -> None:
    guest = httpx.Client(base_url=base, timeout=60)
    res = guest.post(
        f"/share/{token}/feedback",
        json={"session_id": sid, "message_id": message_id, "verdict": "down",
              "comment": "not what I asked"},
    )
    step("fb.down_accepted", res.status_code in (200, 201, 204), f"HTTP {res.status_code}")
    listed = client.get("/api/feedback", params={"verdict": "down"})
    step("fb.listed", listed.status_code == 200, f"HTTP {listed.status_code}")
    body = listed.json()
    sessions = body.get("down_session_ids") or []
    step("fb.session_offered", sid in sessions, f"{len(sessions)} down session(s)")


# ── T16: scoped API keys ─────────────────────────────────────────────────────────


def api_keys(client: httpx.Client, base: str, agent_id: str) -> None:
    scoped = client.post(
        "/api/apikeys",
        json={"name": f"rm-e2e-scoped-{RUN}", "agent_ids": [agent_id]},
    )
    step("key.created", scoped.status_code in (200, 201), f"HTTP {scoped.status_code}")
    key_body = scoped.json()
    raw = key_body.get("key")
    step("key.secret_once", bool(raw), "raw key returned once")

    v1 = httpx.Client(base_url=base, timeout=120, headers={"X-Api-Key": raw})
    listed = v1.get("/v1/agents")
    step("key.v1_list", listed.status_code == 200, f"HTTP {listed.status_code}")
    ids = {row["id"] for row in listed.json()["agents"]}
    step("key.scope_list", ids == {agent_id}, f"sees {len(ids)} agent(s)")

    invoked = v1.post(
        f"/v1/agents/{agent_id}/invoke",
        json={"prompt": "Say OK.", "session_id": session("key")},
    )
    step("key.in_scope_ok", invoked.status_code == 200, f"HTTP {invoked.status_code}")

    other = v1.post(
        f"/v1/agents/{uuid.uuid4().hex[:12]}/invoke",
        json={"prompt": "hi", "session_id": session("key2")},
    )
    step(
        "key.out_of_scope_404",
        other.status_code == 404 and other.json().get("code") == "agent.not_found",
        f"HTTP {other.status_code} code={other.json().get('code')} (reads as missing)",
    )

    usage = client.get(f"/api/apikeys/{key_body['id']}/usage")
    step("key.usage", usage.status_code == 200 and usage.json().get("total", 0) >= 1,
         f"HTTP {usage.status_code} total={usage.json().get('total')}")

    # The API refuses a backdated expiry by design, so an already-expired key cannot be
    # produced over HTTP — that path is pinned hermetically
    # (`tests/test_api_key_scope.py::test_expired_key_is_401_...`). What is worth checking
    # against the real deployment is the refusal itself, and that a future expiry sticks.
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    refused = client.patch(f"/api/apikeys/{key_body['id']}", json={"expires_at": past})
    step(
        "key.past_expiry_refused",
        refused.status_code == 422 and refused.json().get("code") == "apikey.expiry_in_past",
        f"HTTP {refused.status_code} code={refused.json().get('code')}",
    )
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    patched = client.patch(f"/api/apikeys/{key_body['id']}", json={"expires_at": future})
    step("key.expiry_set", patched.status_code == 200, f"HTTP {patched.status_code}")
    still = v1.get("/v1/agents")
    step("key.valid_until_then", still.status_code == 200, f"HTTP {still.status_code}")


# ── T18: snapshots, diff, rollback ───────────────────────────────────────────────


def snapshots(client: httpx.Client, agent_id: str, timeout: int) -> None:
    first = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    step("snap.first", len(first) >= 1, f"{len(first)} snapshot(s) after create")

    spec = client.get(f"/api/agents/{agent_id}").json()["spec"]
    res = client.post(f"/api/agents/{agent_id}/redeploy", json={**spec, "system_prompt": PROMPT_V2})
    step("snap.redeploy", res.status_code == 202, f"HTTP {res.status_code}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get(f"/api/agents/{agent_id}").json()["status"] in ("active", "failed"):
            break
        time.sleep(5)  # nosemgrep: arbitrary-sleep

    rows = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    step("snap.second", len(rows) >= 2, f"{len(rows)} snapshot(s) after redeploy")
    seqs = sorted(row["seq"] for row in rows)
    diff = client.get(
        f"/api/agents/{agent_id}/snapshots/diff",
        params={"from_seq": seqs[-2], "to_seq": seqs[-1]},
    )
    step("snap.diff", diff.status_code == 200, f"HTTP {diff.status_code}")
    fields = json.dumps(diff.json())
    step("snap.diff_names_prompt", "system_prompt" in fields, "diff mentions system_prompt")

    back = client.post(f"/api/agents/{agent_id}/snapshots/{seqs[-2]}/rollback")
    step("snap.rollback", back.status_code == 202, f"HTTP {back.status_code}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get(f"/api/agents/{agent_id}").json()["status"] in ("active", "failed"):
            break
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    live = client.get(f"/api/agents/{agent_id}").json()
    step(
        "snap.rolled_back",
        live["status"] == "active" and live["spec"]["system_prompt"] == PROMPT,
        f"status={live['status']} prompt={live['spec']['system_prompt'][:40]!r}",
    )
    after = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    step("snap.rollback_is_a_publish", len(after) >= 3, f"{len(after)} snapshot(s)")


# ── T20/T21/T22: bundle, promotion, inbox ────────────────────────────────────────


def promote(
    admin: httpx.Client, dev: httpx.Client, ops: httpx.Client, agent_id: str,
    user_ids: list[str],
) -> None:
    created = admin.post("/api/workspaces", json={
        "id": TARGET_WS, "name": f"e2e target {RUN}",
        "account_id": "000000000000", "region": "us-east-2", "tier": "prod",
    })
    step("promo.target_registered", created.status_code in (200, 201),
         f"HTTP {created.status_code} tier={created.json().get('tier')}")

    first = dev.post(f"/api/agents/{agent_id}/release-bundles", json={"note": "e2e"})
    step("promo.bundled", first.status_code == 201, f"HTTP {first.status_code}")
    bundle = first.json()
    step("promo.digest", len(bundle["digest"]) == 64, f"digest={bundle['digest'][:12]}…")
    again = dev.post(f"/api/agents/{agent_id}/release-bundles", json={})
    step("promo.bundle_idempotent", again.json()["id"] == bundle["id"],
         f"same id={bundle['id']}")

    # Security regression (review finding): with no grant on the target, a member may
    # not even ask for a release into it — the target is a grant boundary too.
    ungranted = dev.post("/api/promotions", json={
        "bundle_id": bundle["id"], "target_workspace_id": TARGET_WS,
        "change_note": "x", "rollback_note": "x"})
    step("promo.ungranted_target_refused",
         ungranted.status_code == 403 and ungranted.json().get("code") == "workspace.forbidden",
         f"HTTP {ungranted.status_code} code={ungranted.json().get('code')}")

    # the legitimate hand-off: both people are granted on the environment it goes into
    for user_id in user_ids:
        res = admin.patch(f"/api/users/{user_id}", json={"workspaces": ["default", TARGET_WS]})
        step("promo.grant_target", res.status_code == 200, f"HTTP {res.status_code}")

    asked = dev.post("/api/promotions", json={
        "bundle_id": bundle["id"], "target_workspace_id": TARGET_WS,
        "change_note": "first release of the e2e agent",
        "rollback_note": "roll back to the previous publish",
    })
    step("promo.requested", asked.status_code == 201, f"HTTP {asked.status_code}")
    promotion_id = asked.json()["id"]
    gates = {check["key"] for check in asked.json()["gates"]["checks"]}
    step("promo.gates", {"evaluation", "artifact", "target_ready"} <= gates,
         f"gates={sorted(gates)}")

    # Checked while the request is still pending: an inbox that only reflects state after
    # the fact would be useless to the person meant to act on it.
    waiting = admin.get("/api/inbox").json()
    pending_item = next(
        (item for item in waiting["items"] if item["key"] == "promotions_pending"), None
    )
    step(
        "inbox.shows_pending_release",
        pending_item is not None and pending_item["count"] >= 1,
        f"count={(pending_item or {}).get('count')} to={(pending_item or {}).get('to')}",
    )

    denied = dev.post(f"/api/promotions/{promotion_id}/review", json={"decision": "approve"})
    step("promo.member_cannot_approve", denied.status_code == 403,
         f"HTTP {denied.status_code} code={denied.json().get('code')}")

    detail = dev.get(f"/api/promotions/{promotion_id}").json()
    step("promo.diff_present", isinstance(detail.get("diff"), list) and detail["diff"],
         f"{len(detail.get('diff') or [])} changed field(s) vs target")

    approved = ops.post(
        f"/api/promotions/{promotion_id}/review",
        json={"decision": "approve", "note": "gates read"},
    )
    step("promo.operator_approves", approved.status_code == 200
         and approved.json()["status"] == "approved",
         f"HTTP {approved.status_code} status={approved.json().get('status')}")

    box = admin.get("/api/inbox")
    step("inbox.readable", box.status_code == 200, f"HTTP {box.status_code}")
    items = {item["key"]: item for item in box.json()["items"]}
    step("inbox.has_links", all(item["to"] for item in box.json()["items"]),
         f"{len(items)} kind(s): {sorted(items)}")
    step(
        "inbox.clears_after_review",
        "promotions_pending" not in items,
        "the approved release no longer waits on anyone",
    )
    step("inbox.member_denied", dev.get("/api/inbox").status_code == 403, "member gets 403")


def cleanup(admin: httpx.Client, agent_id: str | None, user_ids: list[str]) -> None:
    if agent_id:
        gone = admin.delete(f"/api/agents/{agent_id}")
        print(f"     delete agent {agent_id}: HTTP {gone.status_code}")
    purge = admin.post(f"/api/workspaces/{TARGET_WS}/purge")
    if purge.status_code not in (200, 204):
        purge = admin.delete(f"/api/workspaces/{TARGET_WS}")
    print(f"     remove workspace {TARGET_WS}: HTTP {purge.status_code}")
    for user_id in user_ids:
        admin.delete(f"/api/users/{user_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    admin = e2e_client(args.base, timeout=180)
    if not admin.get("/api/auth/status").json().get("auth_required"):
        print("this phase needs the login gate on (member vs operator vs admin)")
        return 2
    rows = admin.get("/api/workspaces").json()["workspaces"]
    home = next((w for w in rows if w.get("is_default")), rows[0] if rows else None)
    if home is None or home.get("tier") == "prod":
        print("no usable workspace (missing, or prod tier)")
        return 2
    print(f"── base {args.base} · workspace {home['id']} · run {RUN}")

    agent_id: str | None = None
    user_ids: list[str] = []
    try:
        dev, dev_id = account(admin, args.base, DEV, "member")
        ops, ops_id = account(admin, args.base, OPS, "operator")
        user_ids = [dev_id, ops_id]

        agent_id = deploy(dev, args.timeout)
        token, sid, message_id = share(dev, args.base, agent_id)
        if message_id:
            feedback(dev, args.base, token, sid, message_id)
        else:
            step("fb.skipped", True, "no message id in the saved event")
        link_id = dev.get(f"/api/agents/{agent_id}/share-links").json()["links"][0]["id"]
        share_revoked(dev, args.base, agent_id, token, link_id)
        api_keys(dev, args.base, agent_id)
        snapshots(dev, agent_id, args.timeout)
        promote(admin, dev, ops, agent_id, user_ids)
    finally:
        if not args.keep:
            cleanup(admin, agent_id, user_ids)

    summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
