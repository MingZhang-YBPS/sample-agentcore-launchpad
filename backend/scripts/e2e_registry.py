#!/usr/bin/env python3
"""E2E: registry records for all three types + approval workflow + search.

Flow: sync-defaults (MCP ×2 + AGENT_SKILLS) → deploy harness agent (A2A
auto-registered by pipeline) → status transitions on the skill record
(DRAFT→PENDING_APPROVAL→APPROVED) → SearchDiscoverableRegistryRecords → disable one
(this run's live office-facts) → sync-defaults again, which puts a live one back.

The search step asserts the endpoint's contract, not that a just-created record is
findable: the AWS-side semantic index lags creation by far longer than this script
can wait (see the comment at that step).

Run:  cd backend && uv run python scripts/e2e_registry.py [--keep]
"""

import argparse
import json
import sys
import time

from _e2e_client import e2e_client

AGENT_NAME = "e2e-registry-agent"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    client = e2e_client(args.base, timeout=300)

    print("── sync-defaults (MCP + AGENT_SKILLS records)…")
    # 202 + a job: the sync waits every new record out of CREATING (minutes)
    res = client.post("/api/registry/sync-defaults")
    res.raise_for_status()
    sync = res.json()
    for _ in range(120):
        if sync["done"]:
            break
        # Real-AWS job polling is intentionally paced and attempt-bounded.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
        res = client.get(f"/api/registry/sync-defaults/{sync['job_id']}")
        res.raise_for_status()
        sync = res.json()
    if sync["status"] != "succeeded":
        print(f"  sync-defaults {sync['status']}: {sync.get('error')}")
        return 1
    for row in sync["results"]:
        print(f"  {row['type']:<12} {row['name']:<24} {row['record_id']} "
              f"({'created' if row['created'] else 'refreshed'})")

    print("── deploying harness agent (A2A auto-register)…")
    for agent in client.get("/api/agents").json()["agents"]:
        if agent["name"] == AGENT_NAME:
            client.delete(f"/api/agents/{agent['id']}")
            # Registry/runtime deletion is eventually consistent in real AWS.
            time.sleep(60)  # nosemgrep: arbitrary-sleep
    res = client.post(
        "/api/agents",
        json={"name": AGENT_NAME, "method": "harness",
              "system_prompt": "You are a registry e2e agent."},
    )
    res.raise_for_status()
    agent_id, job_id = res.json()["agent"]["id"], res.json()["job_id"]
    status = "deploying"
    for _ in range(60):
        agent = client.get(f"/api/agents/{agent_id}").json()
        status = agent["status"]
        if status in ("active", "failed"):
            break
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    if status != "active":
        print(f"deploy failed: {client.get(f'/api/jobs/{job_id}').json().get('error')}")
        return 1
    job = client.get(f"/api/jobs/{job_id}").json()
    register_events = [e for e in job["events"] if e["stage"] == "register"]
    for ev in register_events:
        print(f"  register: {ev['msg']}")
    a2a_record_id = agent["registry_record_id"]
    print(f"  ledger registry_record_id: {a2a_record_id}")

    print("── three records (GetRegistryRecord evidence):")
    records = client.get("/api/registry/records").json()["records"]
    by_type = {}
    for rec in records:
        by_type.setdefault(rec["type"], rec)
    for typ in ("A2A", "MCP", "AGENT_SKILLS"):
        rec = by_type.get(typ)
        assert rec, f"missing {typ} record"
        detail = client.get(f"/api/registry/records/{rec['record_id']}").json()
        print(f"  {typ:<12} {detail['name']:<24} {detail['record_id']} status={detail['status']}")

    print("── approval workflow on the skill record:")
    skill = next(r for r in records if r["type"] == "AGENT_SKILLS")
    rid = skill["record_id"]
    print(f"  initial: {skill['status']}")
    if skill["status"] == "DRAFT":
        step = client.post(f"/api/registry/records/{rid}/action", json={"action": "submit"}).json()
        print(f"  after submit: {step['status']}")
    step = client.post(f"/api/registry/records/{rid}/action", json={"action": "approve"}).json()
    print(f"  after approve (published): {step['status']}")
    assert step["status"] == "APPROVED"

    # SearchDiscoverableRegistryRecords is AWS-side semantic search over an index that lags
    # record creation badly — measured in us-east-1: a record created 2026-08-04 was
    # still unfindable by name the NEXT DAY (>12h), while unrelated older records came
    # back as nearest neighbours. The GA discoverable API returns approved records, but
    # approval does not make its semantic index immediately consistent. So the endpoint
    # working is asserted; the fresh record showing up is polled for briefly and reported
    # either way, never asserted.
    print("── SearchDiscoverableRegistryRecords('expense'):")
    indexed = False
    for attempt in range(3):
        if attempt:
            time.sleep(10)  # nosemgrep: arbitrary-sleep
        response = client.get("/api/registry/records/search", params={"q": "expense"})
        assert response.status_code == 200, f"search endpoint returned {response.status_code}"
        found = response.json()["records"]
        assert isinstance(found, list), f"search must return a list, got {type(found)}"
        assert all({"name", "type", "status"} <= set(r) for r in found), \
            f"malformed search records: {found}"
        print(f"  attempt {attempt + 1}: "
              f"{json.dumps([{k: r[k] for k in ('name', 'type', 'status')} for r in found])}")
        if any(r["name"] == "expense-report-writer" for r in found):
            indexed = True
            break
    print(f"  endpoint OK · fresh record indexed: {indexed}"
          f"{'' if indexed else ' (expected — AWS index lag, not a failure)'}")

    print("── disable office-facts record:")
    # the LIVE record this run's sync returned: a DEPRECATED record is terminal and
    # stays listed under the same name, and the next sync creates a new version beside
    # it — so "the first record named office-facts" is an earlier run's dead one
    facts_id = next(r["record_id"] for r in sync["results"] if r["name"] == "office-facts")
    step = client.post(
        f"/api/registry/records/{facts_id}/action", json={"action": "disable"}
    ).json()
    print(f"  after disable: {step.get('status')}")
    assert step.get("status") == "DEPRECATED", step

    # leave the default record set as found: the sync puts a live office-facts back
    res = client.post("/api/registry/sync-defaults")
    res.raise_for_status()
    restore = res.json()
    for _ in range(120):
        if restore["done"]:
            break
        time.sleep(5)  # nosemgrep: arbitrary-sleep — paced job poll against real AWS
        restore = client.get(f"/api/registry/sync-defaults/{restore['job_id']}").json()
    live = next((r for r in restore.get("results") or [] if r["name"] == "office-facts"), {})
    print(f"  restored office-facts: {live.get('record_id')} ({restore['status']})")
    assert restore["status"] == "succeeded" and live.get("record_id") not in (None, facts_id)

    if not args.keep:
        print("── cleaning e2e agent…")
        client.delete(f"/api/agents/{agent_id}")
    print("E2E REGISTRY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
