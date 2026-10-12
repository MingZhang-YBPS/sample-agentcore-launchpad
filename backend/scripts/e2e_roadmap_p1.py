#!/usr/bin/env python3
"""E2E for roadmap phase P1 (T06–T12) — REAL AWS for the agent legs, cleaned up.

Covers, in order:

  1. templates    GET /api/agent-templates serves an applicable catalogue (T10)
  2. guardrail    the workspace PII preset provisions idempotently (T12)
  3. deploy       a harness agent built the way quick mode posts it — template
                  prompt, display name, PII protection on — reaches active (T06/T12)
  4. suggestions  GET /api/agents/{id}/suggested-questions answers (T09)
  5. invoke       a turn carrying a phone number comes back masked (T12 anonymize)
  6. stream       the chat stream reports buffered mode and masks the answer (T12)
  7. block        a `block`-mode agent refuses a PII turn with guardrail.blocked (T12)

T07 (inline KB) and T08 (deploy progress + try-chat) are console-side: the KB legs
they would exercise are already covered by `e2e_knowledge_base.py`, and the deploy
stages this script polls are the same ones the progress line renders.

Run:  cd backend && uv run python scripts/e2e_roadmap_p1.py [--keep] [--base URL]

Safety: refuses a base whose workspace tier is `prod`.
"""

import argparse
import json
import sys
import time
import uuid

import httpx
from _e2e_client import e2e_client

RUN = uuid.uuid4().hex[:6]
# AgentCore runtime session ids must be ≥33 characters (the platform mints 64), so a
# script-chosen one is padded rather than hand-written short.
def session(label: str) -> str:
    return f"{label}-{RUN}-{uuid.uuid4().hex}"[:80]


AGENT_NAME = f"rm-e2e-p1-{RUN}"
BLOCK_AGENT_NAME = f"rm-e2e-p1b-{RUN}"
DISPLAY_NAME = "IT 服务台助手"
# The masking probe. Deliberately an EMAIL and an echo-shaped agent prompt: Bedrock's
# PII detection is context-sensitive, and a scenario agent (IT desk) may decline to
# repeat the value at all — then the leg would pass or fail on the model's wording
# rather than on whether the platform screened anything. An address the agent is told
# to echo makes the masked token itself the assertion.
PII_EMAIL = "mei.chen@acme-corp.example"
PII_PROMPT = f"My email is {PII_EMAIL} - repeat it back exactly."
ECHO_PROMPT = "Repeat back exactly what the user says, and nothing else."
MASK_TOKEN = "{EMAIL}"

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
        print(f"{verdict:<5} {name:<24} {evidence}")


def templates(client: httpx.Client) -> dict:
    res = client.get("/api/agent-templates")
    step("tmpl.reachable", res.status_code == 200, f"HTTP {res.status_code}")
    rows = res.json()["templates"]
    step("tmpl.catalogue", len(rows) >= 3, f"{len(rows)} templates")
    keys = {row["key"] for row in rows}
    step("tmpl.expected_keys", {"hr-policy-qa", "it-service-desk", "blank"} <= keys,
         f"keys={sorted(keys)}")
    shape = {"key", "label_key", "description_key", "method", "system_prompt", "knowledge",
             "toolkits", "memory_long_term", "guardrail", "sample_questions", "tags"}
    step("tmpl.shape", all(shape <= row.keys() for row in rows), "every row carries the contract")
    it_desk = next(row for row in rows if row["key"] == "it-service-desk")
    step("tmpl.i18n_keys", it_desk["label_key"].startswith("templates."),
         f"label_key={it_desk['label_key']}")
    return it_desk


def guardrail_preset(client: httpx.Client) -> None:
    before = client.get("/api/governance/guardrail")
    step("guard.readable", before.status_code == 200, f"HTTP {before.status_code}")
    res = client.post("/api/governance/guardrail")
    step("guard.provision", res.status_code in (200, 201), f"HTTP {res.status_code}")
    first = res.json()
    step("guard.provisioned", first.get("provisioned") is True and bool(first.get("id")),
         f"id={first.get('id')} status={first.get('status')}")
    again = client.post("/api/governance/guardrail")
    step(
        "guard.idempotent",
        again.status_code in (200, 201) and again.json().get("id") == first.get("id"),
        f"same id={again.json().get('id')}",
    )
    entities = set(first.get("entities") or [])
    step("guard.entities", {"EMAIL", "PHONE"} <= entities, f"{len(entities)} entities")


def deploy(
    client: httpx.Client,
    *,
    name: str,
    template: dict,
    mode: str,
    timeout: int,
    prompt: str | None = None,
) -> str:
    """Post exactly what quick mode builds from a template, with PII protection on.

    ``prompt`` overrides the template's instructions for the masking probes, which
    need an agent that echoes rather than one that reasons about the request.
    """
    res = client.post(
        "/api/agents",
        json={
            "name": name,
            "display_name": DISPLAY_NAME,
            "method": template["method"],
            "system_prompt": prompt or template["system_prompt"],
            # long-term memory off on the probes: a recalled fact from an earlier turn
            # would make the leg depend on cross-session state
            "memory": {"short_term": True,
                       "long_term": bool(prompt is None and template["memory_long_term"])},
            "guardrail": {"enabled": True, "mode": mode},
        },
    )
    step(f"deploy.accepted[{mode}]", res.status_code == 202, f"HTTP {res.status_code}")
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
        step(f"deploy.active[{mode}]", False, f"status={status} error={job.get('error')}")
    step(f"deploy.active[{mode}]", True, f"{agent_id}")

    stored = client.get(f"/api/agents/{agent_id}").json()
    step(
        f"deploy.spec[{mode}]",
        (stored["spec"].get("guardrail") or {}).get("mode") == mode
        and stored.get("display_name") == DISPLAY_NAME,
        f"guardrail={stored['spec'].get('guardrail')} display_name={stored.get('display_name')!r}",
    )
    return agent_id


def suggestions(client: httpx.Client, agent_id: str) -> None:
    res = client.get(f"/api/agents/{agent_id}/suggested-questions", params={"lang": "en"})
    step("sugg.reachable", res.status_code == 200, f"HTTP {res.status_code}")
    body = res.json()
    questions = body.get("questions") or []
    step(
        "sugg.shape",
        3 <= len(questions) <= 5 and all(isinstance(q, str) and q for q in questions),
        f"{len(questions)} questions, source={body.get('source')}",
    )
    print(f"     e.g. {questions[0]!r}")
    # unknown agent must 404 rather than 500 the panel
    res = client.get(f"/api/agents/{uuid.uuid4().hex[:12]}/suggested-questions")
    step("sugg.unknown_404", res.status_code == 404, f"HTTP {res.status_code}")


def anonymize_invoke(client: httpx.Client, agent_id: str) -> None:
    res = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": PII_PROMPT, "session_id": session("pii")},
    )
    step("mask.invoked", res.status_code == 200, f"HTTP {res.status_code}")
    text = res.json().get("text", "")
    step("mask.token_present", MASK_TOKEN in text, f"answer={text[:120]!r}")
    step("mask.no_raw_value", PII_EMAIL not in text, f"answer={text[:120]!r}")


def anonymize_stream(client: httpx.Client, base: str, agent_id: str) -> None:
    """The stream must announce buffered mode and never emit the raw number."""
    events: list[tuple[str, str]] = []
    with httpx.stream(
        "POST",
        f"{base}/api/chat/{agent_id}",
        headers={"Cookie": client.headers.get("Cookie", ""), "Accept": "text/event-stream"},
        json={"prompt": PII_PROMPT, "session_id": session("pii-stream")},
        timeout=180,
    ) as response:
        step("stream.open", response.status_code == 200, f"HTTP {response.status_code}")
        name = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                events.append((name, line.split(":", 1)[1].strip()))
            if name == "done":
                break
    meta = next((json.loads(data) for kind, data in events if kind == "meta"), {})
    step("stream.buffered", meta.get("mode") == "buffered", f"mode={meta.get('mode')}")
    deltas = [json.loads(data).get("text", "") for kind, data in events if kind == "delta"]
    joined = "".join(deltas)
    step("stream.single_delta", len(deltas) <= 1, f"{len(deltas)} delta event(s)")
    step("stream.token_present", MASK_TOKEN in joined, f"answer={joined[:120]!r}")
    step("stream.no_raw_value", PII_EMAIL not in joined, f"answer={joined[:120]!r}")


def block_mode(client: httpx.Client, agent_id: str) -> None:
    res = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": PII_PROMPT, "session_id": session("blocked")},
    )
    body = res.json() or {}
    step(
        "block.refused",
        res.status_code == 422 and body.get("code") == "guardrail.blocked",
        f"HTTP {res.status_code} code={body.get('code')}",
    )
    clean = client.post(
        f"/api/agents/{agent_id}/invoke",
        json={"prompt": "How do I request a new laptop?", "session_id": session("clean")},
    )
    step("block.clean_passes", clean.status_code == 200, f"HTTP {clean.status_code}")


def cleanup(client: httpx.Client, agent_ids: list[str]) -> None:
    for agent_id in agent_ids:
        res = client.delete(f"/api/agents/{agent_id}")
        print(f"     delete agent {agent_id}: HTTP {res.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-aws", action="store_true",
                        help="catalogue-only legs (T10); skips every real deploy")
    args = parser.parse_args()

    client = e2e_client(args.base, timeout=180)
    rows = client.get("/api/workspaces").json()["workspaces"]
    ready = next((w for w in rows if w.get("is_default")), rows[0] if rows else None)
    if ready is None:
        print("no workspace registered — run bootstrap first")
        return 2
    if ready.get("tier") == "prod":
        print(f"refusing to run against a prod workspace ({ready['id']})")
        return 2
    print(f"── base {args.base} · workspace {ready['id']} · run {RUN}")

    agents: list[str] = []
    try:
        template = templates(client)
        if args.skip_aws:
            step("aws.skipped", True, "--skip-aws")
            summary()
            return 0
        guardrail_preset(client)
        # the template-shaped agent proves the quick-mode payload deploys and that
        # suggestions answer for it; the echo agent is the deterministic mask probe
        scenario = deploy(
            client, name=AGENT_NAME, template=template, mode="anonymize", timeout=args.timeout
        )
        agents.append(scenario)
        suggestions(client, scenario)
        masked = deploy(
            client, name=f"{AGENT_NAME}-echo", template=template, mode="anonymize",
            timeout=args.timeout, prompt=ECHO_PROMPT,
        )
        agents.append(masked)
        anonymize_invoke(client, masked)
        anonymize_stream(client, args.base, masked)
        blocked = deploy(
            client, name=BLOCK_AGENT_NAME, template=template, mode="block",
            timeout=args.timeout, prompt=ECHO_PROMPT,
        )
        agents.append(blocked)
        block_mode(client, blocked)
    finally:
        if not args.keep:
            cleanup(client, agents)

    summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
