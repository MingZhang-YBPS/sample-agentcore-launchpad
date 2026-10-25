#!/usr/bin/env python3
"""E2E for Projects and the features built on them — REAL AWS, one workspace, cleaned up.

Covers, in order (docs/design/projects §3/§5/§11, cost-tags, invoke-access, eval-cost,
storyline-ux §2.3):

  1. structure   two projects pj-e2e-a-* / pj-e2e-b-*; user-a a developer of A only,
                 user-b of B only; a steward (organization administrator) sees the
                 structure but no project data
  2. isolation   user-a deploys a small zip_runtime agent in A and seeds a dataset and a
                 criteria set; user-b cannot list / get / invoke / redeploy / delete them
                 (404, design §5.4), nor reach A at all; the administrator sees both
  3. storyline   /api/agents/{id}/story and /api/me/next-steps answer for user-a; the
                 story is 404 for user-b and its next steps never name A's agent
  4. shares      A lends the agent (use) and the dataset (read) to B: B's incoming list
                 shows both, user-b reads the dataset and may name the agent as a tool
                 (agents-as-tools), while the agent row itself stays hidden (no row, §11.2);
                 revoke → both gone again
  5. quotas      B's max_agents=0 refuses user-b's create (409 project.quota_exceeded);
                 A's per-run evaluation cap refuses a priced run; both restored
  6. cost tags   the runtime and its execution role carry launchpad:project / workspace /
                 agent-id / managed-by; A's custom cost tag reaches the runtime after the
                 re-tag; the workspace back-fill runs and reports coverage
  7. invoke      /api/projects/{A}/invoke-access gives the ARN and an IAM policy; a
                 temporary role with exactly that policy invokes the agent's endpoint and
                 is AccessDenied on another runtime; the role is deleted
  8. eval cost   the estimate answers a range; a one-item run ends with cost {usd, kind}
  9. transfer    the administrator moves the agent A → B; the tags follow to B; user-b
                 now sees it, user-a no longer does

Run:  cd backend && uv run python scripts/e2e_projects.py --base URL [--keep]

Needs the login gate on (admin in LAUNCHPAD_E2E_USERNAME / _PASSWORD) and AWS
credentials for the workspace's account (boto3 reads tags and drives the IAM role).
Refuses a workspace of tier `prod`. Everything it creates is named `pj-e2e-…` and is
removed in `finally` (unless --keep). Projects cannot be hard-deleted through the API:
they are archived and their deletion requested — the platform purges them after its
cooling-off (`project_deletion_cooling_off_days`).
"""

import argparse
import json
import secrets
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import boto3
import httpx
from _e2e_client import E2E_DEPARTMENT_ID, chat_sse, e2e_client
from botocore.exceptions import ClientError

RUN = uuid.uuid4().hex[:6]
PREFIX = "pj-e2e-"
PROJECT_A = f"{PREFIX}a-{RUN}"
PROJECT_B = f"{PREFIX}b-{RUN}"
AGENT = f"{PREFIX}agent-{RUN}"
ROLE_NAME = f"{PREFIX}invoker-{RUN}"
COST_TAG_KEY = "CostCenter"
COST_TAG_VALUE = f"pj-e2e-{RUN}"
SYSTEM_PROMPT = "You are a concise arithmetic assistant. Reply with just the number."
# an id no workspace manages: the create path refuses it after resolving agent tools
# and before any row, so the agents-as-tools probe never deploys anything
PROBE_MEMORY = f"pjE2eProbe{RUN}-abcdefghij"
PEOPLE = {
    who: {"username": f"{PREFIX}{who}-{RUN}", "email": f"{PREFIX}{who}-{RUN}@example.com",
          "password": f"Pw-{secrets.token_urlsafe(12)}-aA1!"}
    for who in ("user-a", "user-b", "steward")
}

# failures of the account / service rather than of the platform: the eval leg runs once more
TRANSIENT_EVAL_ERRORS = ("no span documents", "internalServerException",
                         "InternalServerException", "ThrottlingException",
                         "ServiceUnavailableException")

RESULTS: list[tuple[str, str, str]] = []


class Abort(RuntimeError):
    """A failure later steps cannot run without."""


def check(name: str, ok: bool, evidence: str = "", *, fatal: bool = False) -> bool:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}"[:400], flush=True)
    if not ok and fatal:
        raise Abort(f"{name}: {evidence}")
    return ok


def skip(name: str, why: str) -> None:
    RESULTS.append((name, "SKIP", why))
    print(f"── {name}: SKIP · {why}", flush=True)


def summary() -> int:
    print("\n═══ summary ═══")
    for name, verdict, evidence in RESULTS:
        print(f"{verdict:<5} {name:<36} {evidence[:110]}")
    failed = sum(v == "FAIL" for _, v, _ in RESULTS)
    print(f"\n{sum(v == 'PASS' for _, v, _ in RESULTS)} passed, {failed} failed, "
          f"{sum(v == 'SKIP' for _, v, _ in RESULTS)} skipped")
    return 1 if failed else 0


def pause(seconds: float) -> None:
    # Real-AWS polling is intentionally paced and deadline-bounded.
    time.sleep(seconds)  # nosemgrep: arbitrary-sleep


def call(client: httpx.Client, method: str, path: str, *, project: str | None = None,
         **kw: Any) -> httpx.Response:
    """One request; a dropped network is waited out rather than failing the run."""
    headers = dict(kw.pop("headers", None) or {})
    if project:
        headers["X-Project"] = project
    for attempt in range(40):
        try:
            return client.request(method, path, headers=headers, **kw)
        except httpx.TransportError as exc:
            print(f"     network: {type(exc).__name__} on {method} {path} — retry "
                  f"{attempt + 1}", flush=True)
            pause(15)
    raise Abort(f"network never came back for {method} {path}")


def body(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError:
        return {}


def code(res: httpx.Response) -> str:
    data = body(res)
    return f"HTTP {res.status_code} {data.get('code', '') if isinstance(data, dict) else ''}"


def wait_for(what: str, probe: Callable[[], Any], timeout: float, every: float = 10) -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = probe()
        except (ClientError, httpx.TransportError) as exc:
            print(f"     {what}: {type(exc).__name__} {exc}"[:200], flush=True)
            last = None
        if last:
            return last
        pause(every)
    print(f"     {what}: timed out after {timeout:.0f}s", flush=True)
    return None


def login(base: str, who: str) -> httpx.Client:
    creds = PEOPLE[who]
    client = httpx.Client(base_url=base, timeout=240)
    res = call(client, "POST", "/api/auth/login",
               json={"username": creds["username"], "password": creds["password"]})
    if res.status_code != 200:
        raise Abort(f"login {who}: {code(res)}")
    token = res.cookies.get("launchpad_session") or client.cookies.get("launchpad_session")
    client.cookies.clear()
    # the cookie is `Secure` on prod; pin it as a header (see _e2e_client)
    client.headers["Cookie"] = f"launchpad_session={token}"
    client.headers["X-Workspace"] = "default"
    return client


def session_id(label: str) -> str:
    # AgentCore runtime session ids must be ≥33 characters
    return f"{label}-{RUN}-{uuid.uuid4().hex}"[:80]


# ── state: everything created, for the cleanup ───────────────────────────────

STATE: dict[str, Any] = {
    "users": {},          # who -> user id
    "projects": [],       # project ids
    "agent_id": None,
    "agent_project": None,  # where the agent lives now (it moves on transfer)
    "datasets": [],       # (dataset id, project id)
    "criteria": [],       # (lineage id, project id)
    "shares": [],         # (owner project, share id)
    "quotas": [],         # (project id, keys set)
    "role": None,
}


def cleanup_leftovers(admin: httpx.Client) -> None:
    """Remove what an interrupted earlier run of THIS script left (prefix-matched)."""
    for row in body(call(admin, "GET", "/api/projects")).get("projects", []):
        pid = row["id"]
        if not pid.startswith(PREFIX):
            continue
        if row.get("status") == "active":
            for agent in body(call(admin, "GET", "/api/agents", project=pid)).get("agents", []):
                if agent["name"].startswith(PREFIX) and agent["status"] != "deleted":
                    res = call(admin, "DELETE", f"/api/agents/{agent['id']}", project=pid)
                    print(f"     leftover agent {agent['id']} in {pid}: {code(res)}")
        retire_project(admin, pid, quiet=True)
    users = body(call(admin, "GET", "/api/users", params={"q": PREFIX, "limit": 200}))
    for user in users.get("items", []):
        if user["username"].startswith(PREFIX):
            res = call(admin, "DELETE", f"/api/users/{user['id']}")
            print(f"     leftover user {user['username']}: {code(res)}")
    iam = boto3.client("iam")
    for page in iam.get_paginator("list_roles").paginate():
        for role in page["Roles"]:
            if role["RoleName"].startswith(f"{PREFIX}invoker-"):
                delete_role(iam, role["RoleName"])
                print(f"     leftover role {role['RoleName']} deleted")


def retire_project(admin: httpx.Client, pid: str, *, quiet: bool = False) -> None:
    """Archive and request deletion (the purge follows the cooling-off)."""
    lifecycle = body(call(admin, "GET", f"/api/projects/{pid}/lifecycle"))
    if lifecycle.get("status") == "active":
        res = call(admin, "POST", f"/api/projects/{pid}/archive")
        if not quiet:
            print(f"     archive {pid}: {code(res)}")
        lifecycle = body(call(admin, "GET", f"/api/projects/{pid}/lifecycle"))
    if lifecycle.get("status") == "archived" and not lifecycle.get("deletion"):
        res = call(admin, "POST", f"/api/projects/{pid}/delete-request")
        if not quiet:
            print(f"     delete-request {pid}: {code(res)}")


def delete_role(iam: Any, name: str) -> None:
    try:
        for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        iam.delete_role(RoleName=name)
    except ClientError as exc:
        print(f"     delete role {name}: {exc}")


def cleanup(admin: httpx.Client) -> None:
    print("\n── cleanup")
    for owner, share_id in STATE["shares"]:
        res = call(admin, "DELETE", f"/api/projects/{owner}/shares/{share_id}")
        print(f"     revoke share {share_id}: {code(res)}")
    for pid, keys in STATE["quotas"]:
        res = call(admin, "PUT", f"/api/projects/{pid}/quotas",
                   json={"quotas": {k: None for k in keys}})
        print(f"     restore quotas {pid} {sorted(keys)}: {code(res)}")
    for lineage, pid in STATE["criteria"]:
        # an agent's criteria set moves with it on a transfer
        for where in dict.fromkeys((pid, STATE["agent_project"] or pid)):
            res = call(admin, "DELETE", f"/api/criteria-sets/{lineage}", project=where)
            if res.status_code != 404:
                break
        print(f"     delete criteria set {lineage}: {code(res)}")
    for dataset, pid in STATE["datasets"]:
        res = call(admin, "DELETE", f"/api/eval/datasets/{dataset}", project=pid)
        print(f"     delete dataset {dataset}: {code(res)}")
    if STATE["agent_id"]:
        aid, pid = STATE["agent_id"], STATE["agent_project"]
        res = call(admin, "DELETE", f"/api/agents/{aid}", project=pid)
        print(f"     delete agent {aid}: {code(res)}")
        gone = wait_for("agent deletion", lambda: body(call(
            admin, "GET", f"/api/agents/{aid}", project=pid)).get("status") in
            ("deleted", None), timeout=600, every=10)
        print(f"     agent deleted: {bool(gone)}")
    if STATE["role"]:
        delete_role(boto3.client("iam"), STATE["role"])
        print(f"     deleted role {STATE['role']}")
    for who, uid in STATE["users"].items():
        res = call(admin, "DELETE", f"/api/users/{uid}")
        print(f"     delete user {who}: {code(res)}")
    for pid in STATE["projects"]:
        retire_project(admin, pid)


# ── 1. structure ──────────────────────────────────────────────────────────────


def structure(admin: httpx.Client, base: str) -> dict[str, httpx.Client]:
    for pid, name in ((PROJECT_A, "PJ e2e A"), (PROJECT_B, "PJ e2e B")):
        # a project belongs to a department (422 `project.department_required`): the
        # suite's own, which e2e_client ensured
        res = call(admin, "POST", "/api/projects",
                   json={"id": pid, "name": f"{name} {RUN}", "workspaces": ["default"],
                         "department_id": E2E_DEPARTMENT_ID})
        check(f"project.create[{pid[-8:]}]", res.status_code == 201, code(res), fatal=True)
        STATE["projects"].append(pid)
    memberships = {"user-a": PROJECT_A, "user-b": PROJECT_B}
    for who in ("user-a", "user-b", "steward"):
        creds = PEOPLE[who]
        payload: dict[str, Any] = {"username": creds["username"], "email": creds["email"],
                                   "password": creds["password"], "valid_days": 1}
        if who == "steward":
            payload["role"] = "steward"
        else:
            # no workspace grant: the project membership alone reaches the workspace,
            # and no `inherit` row in the default project is minted
            payload |= {"role": "member", "workspaces": [],
                        "projects": [{"project_id": memberships[who],
                                      "role_key": "developer"}]}
        res = call(admin, "POST", "/api/users", json=payload)
        check(f"user.create[{who}]", res.status_code == 201, code(res), fatal=True)
        STATE["users"][who] = body(res)["id"]
    clients = {who: login(base, who) for who in PEOPLE}

    for who, mine, other in (("user-a", PROJECT_A, PROJECT_B),
                             ("user-b", PROJECT_B, PROJECT_A)):
        c = clients[who]
        listed = {p["id"] for p in body(call(c, "GET", "/api/projects")).get("projects", [])}
        check(f"iso.projects_listed[{who}]", listed == {mine}, f"sees {sorted(listed)}")
        res = call(c, "GET", f"/api/projects/{other}")
        check(f"iso.other_project_404[{who}]", res.status_code == 404, code(res))
        res = call(c, "GET", f"/api/projects/{mine}")
        perms = set(body(res).get("my_permissions") or [])
        check(f"roles.developer_template[{who}]",
              res.status_code == 200 and {"project.use", "project.write", "agents.deploy"}
              <= perms and "project.members" not in perms and "project.share" not in perms,
              f"{code(res)} perms={sorted(perms)}")
        res = call(c, "GET", f"/api/projects/{other}/members")
        check(f"iso.other_members_404[{who}]", res.status_code == 404, code(res))
        # a developer manages nobody (project.members is the owner's)
        res = call(c, "PUT", f"/api/projects/{mine}/members",
                   json={"upsert": [{"user_id": STATE["users"]["steward"],
                                     "role_key": "viewer"}]})
        check(f"roles.developer_no_members[{who}]", res.status_code in (403, 404), code(res))
    access = body(call(clients["user-a"], "GET", "/api/me/access"))
    reach = {p["id"] for w in access.get("workspaces", []) for p in w.get("projects", [])}
    defaults = {w.get("default_project_id") for w in access.get("workspaces", [])}
    check("iso.me_access_only_a", reach == {PROJECT_A} and defaults == {PROJECT_A},
          f"projects={sorted(reach)} default={sorted(map(str, defaults))}")

    steward = clients["steward"]
    listed = {p["id"] for p in body(call(steward, "GET", "/api/projects")).get("projects", [])}
    check("steward.sees_structure", {PROJECT_A, PROJECT_B} <= listed, f"{len(listed)} projects")
    res = call(steward, "GET", f"/api/projects/{PROJECT_A}/members")
    check("steward.members_readable", res.status_code == 200, code(res))
    return clients


# ── 2. deploy + seed in A ─────────────────────────────────────────────────────


def deploy(user_a: httpx.Client, timeout: int) -> str:
    res = call(user_a, "POST", "/api/agents", project=PROJECT_A, json={
        "name": AGENT, "method": "zip_runtime", "system_prompt": SYSTEM_PROMPT,
        "memory": {"short_term": False, "long_term": False},
    })
    check("deploy.accepted", res.status_code == 202, code(res), fatal=True)
    agent_id = body(res)["agent"]["id"]
    STATE["agent_id"], STATE["agent_project"] = agent_id, PROJECT_A
    seen: set[tuple[str, str]] = set()

    def ready() -> dict | None:
        agent = body(call(user_a, "GET", f"/api/agents/{agent_id}", project=PROJECT_A))
        for stage in (agent.get("deployments") or [{}])[0].get("stages", []):
            key = (stage["name"], stage["status"])
            if key not in seen and stage["status"] != "pending":
                seen.add(key)
                print(f"     stage {stage['name']:<10} {stage['status']:<10} "
                      f"{(stage.get('detail') or '')[:80]}", flush=True)
        return agent if agent.get("status") in ("active", "failed") else None

    agent = wait_for("deploy", ready, timeout, every=10) or {}
    check("deploy.active", agent.get("status") == "active",
          f"status={agent.get('status')} arn={agent.get('arn')}", fatal=True)
    check("deploy.in_project_a", agent.get("project_id") in (None, PROJECT_A),
          f"project_id={agent.get('project_id')}")
    # the first turn of a fresh runtime is a cold start; over the chat stream (heartbeats
    # throughout) it cannot be cut by CloudFront's origin timeout, unlike sync /invoke
    try:
        text = chat_sse(user_a, agent_id, "What is 6*7? Reply with just the number.",
                        session_id=session_id("a"), headers={"X-Project": PROJECT_A})["text"]
        check("deploy.invoke_user_a", "42" in text, f"text={text[:60]!r}")
    except (httpx.HTTPStatusError, RuntimeError) as exc:
        check("deploy.invoke_user_a", False, f"{type(exc).__name__}: {exc}"[:160])
    return agent_id


def seed(user_a: httpx.Client, agent_id: str) -> tuple[str, str]:
    res = call(user_a, "POST", "/api/eval/datasets", project=PROJECT_A, json={
        "name": f"{PREFIX}ds-{RUN}",
        "items": [{"prompt": "What is 2+3? Reply with just the number.", "expected": "5"}],
    })
    check("seed.dataset", res.status_code == 201, code(res), fatal=True)
    dataset_id = body(res)["id"]
    STATE["datasets"].append((dataset_id, PROJECT_A))
    res = call(user_a, "POST", "/api/criteria-sets", project=PROJECT_A,
               json={"kind": "agent", "name": f"{PREFIX}crit-{RUN}", "agent_id": agent_id})
    check("seed.criteria_set", res.status_code == 201, code(res), fatal=True)
    data = body(res)
    lineage = data.get("lineage_id") or (data.get("set") or {}).get("lineage_id")
    STATE["criteria"].append((lineage, PROJECT_A))
    return dataset_id, lineage


# ── 3. isolation + storyline ──────────────────────────────────────────────────


def isolation(admin: httpx.Client, users: dict[str, httpx.Client], agent_id: str,
              dataset_id: str, lineage: str) -> None:
    b = users["user-b"]
    ids = {a["id"] for a in body(call(b, "GET", "/api/agents", project=PROJECT_B))
           .get("agents", [])}
    check("iso.b_list_agents", agent_id not in ids, f"{len(ids)} agents listed")
    # route authorization, not a chat turn: project B must not reach these routes
    for name, method, path, kw in (
        ("get", "GET", f"/api/agents/{agent_id}", {}),
        ("invoke", "POST", f"/api/agents/{agent_id}/invoke",
         {"json": {"prompt": "hi", "session_id": session_id("b")}}),
        ("redeploy", "POST", f"/api/agents/{agent_id}/redeploy",
         {"json": {"name": AGENT, "method": "zip_runtime", "system_prompt": "changed"}}),
        ("delete", "DELETE", f"/api/agents/{agent_id}", {}),
        ("invoke_access", "GET", f"/api/agents/{agent_id}/invoke-access", {}),
        ("dataset_delete", "DELETE", f"/api/eval/datasets/{dataset_id}", {}),
        ("criteria_get", "GET", f"/api/criteria-sets/{lineage}", {}),
    ):
        res = call(b, method, path, project=PROJECT_B, **kw)
        check(f"iso.b_{name}_404", res.status_code == 404, code(res))
    datasets = {d["id"] for d in _rows(body(call(b, "GET", "/api/eval/datasets",
                                                project=PROJECT_B)), "datasets")}
    check("iso.b_list_datasets", dataset_id not in datasets, f"{len(datasets)} listed")
    sets = {s.get("lineage_id") for s in body(call(b, "GET", "/api/criteria-sets",
                                                   project=PROJECT_B)).get("sets", [])}
    check("iso.b_list_criteria", lineage not in sets, f"{len(sets)} listed")
    # naming A in the header is no way in either: a non-member's project is not found
    res = call(b, "GET", "/api/agents", project=PROJECT_A)
    check("iso.b_header_a_404", res.status_code == 404, code(res))
    # still alive for its owner
    res = call(users["user-a"], "GET", f"/api/agents/{agent_id}", project=PROJECT_A)
    check("iso.a_still_sees", res.status_code == 200, code(res))

    ids_a = {a["id"] for a in body(call(admin, "GET", "/api/agents", project=PROJECT_A))
             .get("agents", [])}
    ids_b = {a["id"] for a in body(call(admin, "GET", "/api/agents", project=PROJECT_B))
             .get("agents", [])}
    check("iso.admin_sees_a", agent_id in ids_a and agent_id not in ids_b,
          f"in A={agent_id in ids_a} in B={agent_id in ids_b}")
    listed = {p["id"] for p in body(call(admin, "GET", "/api/projects")).get("projects", [])}
    check("iso.admin_sees_both_projects", {PROJECT_A, PROJECT_B} <= listed,
          f"{len(listed)} projects")
    res = call(users["steward"], "GET", f"/api/agents/{agent_id}", project=PROJECT_A)
    check("steward.no_agent_data", res.status_code in (403, 404), code(res))


def _rows(data: Any, key: str) -> list[dict]:
    if isinstance(data, list):
        return data
    return data.get(key) or data.get("items") or []


def storyline(users: dict[str, httpx.Client], agent_id: str) -> None:
    a, b = users["user-a"], users["user-b"]
    res = call(a, "GET", f"/api/agents/{agent_id}/story", project=PROJECT_A)
    data = body(res)
    check("story.user_a", res.status_code == 200 and (data.get("agent") or {}).get("id")
          == agent_id and isinstance(data.get("facts"), dict),
          f"{code(res)} facts={list(data.get('facts') or {})[:6]}")
    res = call(a, "GET", "/api/me/next-steps", project=PROJECT_A)
    data = body(res)
    named = [r["agent"]["id"] for r in (data.get("mine") or []) + (data.get("waiting") or [])]
    check("next_steps.user_a", res.status_code == 200 and data.get("project_id") == PROJECT_A
          and agent_id in named, f"{code(res)} mine={len(data.get('mine') or [])} "
          f"waiting={len(data.get('waiting') or [])}")
    res = call(b, "GET", f"/api/agents/{agent_id}/story", project=PROJECT_B)
    check("story.user_b_404", res.status_code == 404, code(res))
    res = call(b, "GET", "/api/me/next-steps", project=PROJECT_B)
    data = body(res)
    named = [r["agent"]["id"] for r in (data.get("mine") or []) + (data.get("waiting") or [])]
    check("next_steps.user_b_without_a", res.status_code == 200 and agent_id not in named,
          f"{code(res)} names {len(named)} agents")
    res = call(b, "GET", "/api/me/next-steps", project=PROJECT_A)
    check("next_steps.user_b_header_a_404", res.status_code == 404, code(res))


# ── 4. shares ─────────────────────────────────────────────────────────────────


def tool_probe(b: httpx.Client, agent_id: str) -> httpx.Response:
    """user-b saves an agent in B that names A's agent as a tool. The pinned memory is
    one no workspace manages, so the save is refused right after the agent tools
    resolve (422 agent.memory_not_managed) — or at them (404 agent_tools.not_found)."""
    return call(b, "POST", "/api/agents", project=PROJECT_B, json={
        "name": f"{PREFIX}probe-{RUN}", "method": "zip_runtime",
        "system_prompt": SYSTEM_PROMPT,
        "memory": {"short_term": True, "long_term": False, "memory_id": PROBE_MEMORY},
        "agent_tools": [{"agent_id": agent_id, "tool_name": "arith",
                         "description": "arithmetic helper"}],
    })


def shares(admin: httpx.Client, users: dict[str, httpx.Client], agent_id: str,
           dataset_id: str) -> None:
    a, b = users["user-a"], users["user-b"]
    res = tool_probe(b, agent_id)
    check("share.tool_before_404", res.status_code == 404
          and body(res).get("code") == "agent_tools.not_found", code(res))
    res = call(a, "POST", f"/api/projects/{PROJECT_A}/shares", json={
        "workspace_id": "default", "kind": "agent", "resource_id": agent_id,
        "grantee_project_id": PROJECT_B, "access": "use"})
    check("share.developer_cannot_share", res.status_code in (403, 404), code(res))
    options = body(call(admin, "GET", f"/api/projects/{PROJECT_A}/shares/options",
                        params={"workspace_id": "default"}))
    offered = {(r["kind"], r["id"]) for r in options.get("resources", [])}
    check("share.options", ("agent", agent_id) in offered and ("eval_dataset", dataset_id)
          in offered and PROJECT_B in {g["id"] for g in options.get("grantees", [])},
          f"{len(offered)} resources")
    made: dict[str, str] = {}
    for kind, rid, level in (("agent", agent_id, "use"), ("eval_dataset", dataset_id, "read")):
        res = call(admin, "POST", f"/api/projects/{PROJECT_A}/shares", json={
            "workspace_id": "default", "kind": kind, "resource_id": rid,
            "grantee_project_id": PROJECT_B, "access": level, "expires_in_days": 1,
            "note": f"pj-e2e {RUN}"})
        data = body(res)
        check(f"share.grant[{kind}]", res.status_code == 201 and data.get("status") == "active",
              f"{code(res)} status={data.get('status')}")
        if res.status_code == 201:
            made[kind] = data["id"]
            STATE["shares"].append((PROJECT_A, data["id"]))
    incoming = body(call(b, "GET", f"/api/projects/{PROJECT_B}/shares",
                         params={"direction": "incoming"}))
    got = {s.get("resource_id") for s in _rows(incoming, "incoming") or
           _rows(incoming, "shares")}
    check("share.b_incoming", {agent_id, dataset_id} <= got, f"incoming={sorted(got)}")
    datasets = _rows(body(call(b, "GET", "/api/eval/datasets", project=PROJECT_B)), "datasets")
    shared = next((d for d in datasets if d["id"] == dataset_id), None)
    check("share.b_sees_dataset", shared is not None and bool(shared.get("shared_from")),
          f"shared_from={shared and shared.get('shared_from')}")
    res = call(b, "DELETE", f"/api/eval/datasets/{dataset_id}", project=PROJECT_B)
    check("share.b_dataset_no_write", res.status_code in (403, 404, 409), code(res))
    res = tool_probe(b, agent_id)
    check("share.tool_resolves", res.status_code == 422
          and body(res).get("code") == "agent.memory_not_managed", code(res))
    if res.status_code == 202:  # never expected: the probe must not have deployed
        STATE.setdefault("stray", []).append(body(res)["agent"]["id"])
    # an agent share opens no row: the console still answers not-found for it (§11.2)
    res = call(b, "GET", f"/api/agents/{agent_id}", project=PROJECT_B)
    check("share.agent_row_still_hidden", res.status_code == 404, code(res))

    for kind, share_id in made.items():
        res = call(admin, "DELETE", f"/api/projects/{PROJECT_A}/shares/{share_id}")
        check(f"share.revoke[{kind}]", res.status_code == 200
              and body(res).get("status") == "revoked", code(res))
        STATE["shares"].remove((PROJECT_A, share_id))
    datasets = _rows(body(call(b, "GET", "/api/eval/datasets", project=PROJECT_B)), "datasets")
    check("share.revoked_dataset_gone", dataset_id not in {d["id"] for d in datasets},
          f"{len(datasets)} listed")
    res = call(b, "DELETE", f"/api/eval/datasets/{dataset_id}", project=PROJECT_B)
    check("share.revoked_dataset_404", res.status_code == 404, code(res))
    res = tool_probe(b, agent_id)
    check("share.revoked_tool_404", res.status_code == 404, code(res))


# ── 5. quotas ─────────────────────────────────────────────────────────────────


def set_quota(admin: httpx.Client, pid: str, quotas: dict[str, Any]) -> httpx.Response:
    res = call(admin, "PUT", f"/api/projects/{pid}/quotas", json={"quotas": quotas})
    if res.status_code == 200:
        STATE["quotas"].append((pid, set(quotas)))
    return res


def quotas(admin: httpx.Client, users: dict[str, httpx.Client]) -> None:
    b = users["user-b"]
    res = call(b, "GET", f"/api/projects/{PROJECT_B}/quotas")
    check("quota.readable_by_member", res.status_code == 200, code(res))
    res = call(b, "PUT", f"/api/projects/{PROJECT_B}/quotas", json={"quotas": {"max_agents": 99}})
    check("quota.member_cannot_set", res.status_code in (403, 404), code(res))
    res = set_quota(admin, PROJECT_B, {"max_agents": 0})
    check("quota.set_max_agents", res.status_code == 200, code(res))
    res = call(b, "POST", "/api/agents", project=PROJECT_B, json={
        "name": f"{PREFIX}quota-{RUN}", "method": "harness", "system_prompt": SYSTEM_PROMPT})
    data = body(res)
    check("quota.refused", res.status_code == 409 and data.get("code") ==
          "project.quota_exceeded", f"{code(res)} detail={data.get('detail')}")
    if res.status_code == 202:
        STATE.setdefault("stray", []).append(data["agent"]["id"])
    res = call(admin, "PUT", f"/api/projects/{PROJECT_B}/quotas",
               json={"quotas": {"max_agents": None}})
    view = body(res)
    check("quota.restored", res.status_code == 200, code(res))
    print(f"     quotas view keys: {list(view)[:8]}")


# ── 6. cost tags ──────────────────────────────────────────────────────────────


def aws_tags(arn: str) -> tuple[dict[str, str], dict[str, str], str]:
    """(runtime tags, execution role tags, role name)."""
    # botocore reads AWS_DEFAULT_REGION, not AWS_REGION: the ARN names the region outright
    control = boto3.client("bedrock-agentcore-control", region_name=arn.split(":")[3])
    runtime_tags = control.list_tags_for_resource(resourceArn=arn).get("tags") or {}
    runtime_id = arn.rsplit("/", 1)[-1]
    role_arn = control.get_agent_runtime(agentRuntimeId=runtime_id)["roleArn"]
    role = role_arn.rsplit("/", 1)[-1]
    role_tags = {t["Key"]: t["Value"]
                 for t in boto3.client("iam").list_role_tags(RoleName=role)["Tags"]}
    return runtime_tags, role_tags, role


def standard_ok(tags: dict[str, str], project: str, agent_id: str) -> bool:
    return (tags.get("launchpad:project") == project
            and tags.get("launchpad:workspace") == "default"
            and tags.get("launchpad:agent-id") == agent_id
            and tags.get("launchpad:managed-by") == "launchpad")


def cost_tags(admin: httpx.Client, users: dict[str, httpx.Client], agent_id: str,
              arn: str) -> None:
    runtime, role, role_name = aws_tags(arn)
    check("tags.runtime_standard", standard_ok(runtime, PROJECT_A, agent_id),
          json.dumps({k: v for k, v in runtime.items() if k.startswith("launchpad:")}))
    check("tags.role_standard", standard_ok(role, PROJECT_A, agent_id),
          f"{role_name} " + json.dumps({k: v for k, v in role.items()
                                        if k.startswith("launchpad:")}))
    a = users["user-a"]
    res = call(a, "GET", f"/api/projects/{PROJECT_A}/cost-tags")
    check("tags.project_readable", res.status_code == 200, code(res))
    res = call(users["user-b"], "GET", f"/api/projects/{PROJECT_A}/cost-tags")
    check("tags.other_project_404", res.status_code == 404, code(res))
    res = call(admin, "PUT", f"/api/projects/{PROJECT_A}/cost-tags",
               json={"cost_tags": {"launchpad:project": "x"}})
    check("tags.reserved_refused", 400 <= res.status_code < 500, code(res))
    res = call(admin, "PUT", f"/api/projects/{PROJECT_A}/cost-tags",
               json={"cost_tags": {COST_TAG_KEY: COST_TAG_VALUE}})
    check("tags.custom_put", res.status_code == 200
          and body(res).get("cost_tags", {}).get(COST_TAG_KEY) == COST_TAG_VALUE, code(res))
    got = wait_for("custom tag on runtime", lambda: (
        lambda t: t if t[0].get(COST_TAG_KEY) == COST_TAG_VALUE
        and t[1].get(COST_TAG_KEY) == COST_TAG_VALUE else None)(aws_tags(arn)),
        timeout=300, every=15)
    check("tags.custom_retagged", got is not None,
          f"runtime={got[0].get(COST_TAG_KEY) if got else None} "
          f"role={got[1].get(COST_TAG_KEY) if got else None}")

    res = call(admin, "GET", "/api/cost-tags/backfill")
    check("backfill.readable", res.status_code == 200 and "tag_keys" in body(res), code(res))
    res = call(admin, "POST", "/api/cost-tags/backfill")
    data = body(res)
    check("backfill.started", res.status_code == 200 and bool(data.get("id")),
          f"{code(res)} status={data.get('status')}")
    job_id = data.get("id")
    final = wait_for("backfill run", lambda: (lambda r: r if r and r.get("id") == job_id
                     and r.get("status") in ("succeeded", "failed") else None)(
        body(call(admin, "GET", "/api/cost-tags/backfill")).get("run")),
        timeout=900, every=15) or {}
    cov = final.get("coverage") or {}
    check("backfill.succeeded", final.get("status") == "succeeded",
          f"status={final.get('status')} error={final.get('error')}")
    check("backfill.coverage", bool(cov) and isinstance(cov.get("total"), int)
          and cov["total"] > 0, json.dumps(cov)[:200])
    runtime, _, _ = aws_tags(arn)
    check("backfill.kept_agent_tags", standard_ok(runtime, PROJECT_A, agent_id)
          and runtime.get(COST_TAG_KEY) == COST_TAG_VALUE, "runtime tags intact")
    res = call(users["user-a"], "POST", "/api/cost-tags/backfill")
    check("backfill.member_refused", res.status_code in (403, 404), code(res))


# ── 7. invoke access ──────────────────────────────────────────────────────────


def invoke_access(users: dict[str, httpx.Client], agent_id: str, arn: str) -> None:
    a, b = users["user-a"], users["user-b"]
    res = call(a, "GET", f"/api/agents/{agent_id}/invoke-access", project=PROJECT_A)
    one = body(res)
    check("invoke.agent_route", res.status_code == 200 and one.get("arn") == arn
          and one.get("mode") == "iam" and bool(one.get("policy")),
          f"{code(res)} mode={one.get('mode')} qualifiers={one.get('qualifiers')}")
    res = call(a, "GET", f"/api/projects/{PROJECT_A}/invoke-access")
    data = body(res)
    row = next((r for r in data.get("inventory", []) if r.get("agent_id") == agent_id), {})
    policy = data.get("policy")
    check("invoke.project_route", res.status_code == 200 and row.get("arn") == arn
          and bool(policy), f"{code(res)} qualifier={row.get('qualifier')} "
          f"endpoint={row.get('endpoint_arn')}")
    res = call(b, "GET", f"/api/projects/{PROJECT_A}/invoke-access")
    check("invoke.project_route_b_404", res.status_code == 404, code(res))
    res = call(a, "GET", f"/api/projects/{PROJECT_A}/invoke-access", params={"format": "csv"})
    check("invoke.csv", res.status_code == 200 and arn in res.text, code(res))
    if not policy:
        return
    actions = {act for st in policy.get("Statement", []) for act in
               ([st["Action"]] if isinstance(st.get("Action"), str) else st.get("Action", []))}
    check("invoke.policy_actions", "bedrock-agentcore:InvokeAgentRuntime" in actions,
          f"actions={sorted(actions)}")

    iam = boto3.client("iam")
    account = boto3.client("sts").get_caller_identity()["Account"]
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{account}:root"},
        "Action": "sts:AssumeRole"}]}
    iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                    Description="pj-e2e temporary invoker (deleted by the e2e)",
                    MaxSessionDuration=3600)
    STATE["role"] = ROLE_NAME
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="launchpad-invoke",
                        PolicyDocument=json.dumps(policy))
    role_arn = f"arn:aws:iam::{account}:role/{ROLE_NAME}"
    sts = boto3.client("sts")
    creds = wait_for("assume invoker role", lambda: sts.assume_role(
        RoleArn=role_arn, RoleSessionName=f"pj-e2e-{RUN}")["Credentials"],
        timeout=120, every=10)
    check("invoke.assume_role", creds is not None, role_arn, fatal=True)
    region = arn.split(":")[3]  # the runtime's own region, whatever the shell sets
    data_plane = boto3.client(
        "bedrock-agentcore", region_name=region,
        aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])
    qualifier = row.get("qualifier") or "DEFAULT"

    def invoke(target_arn: str, qual: str) -> tuple[str, str]:
        try:
            out = data_plane.invoke_agent_runtime(
                agentRuntimeArn=target_arn, qualifier=qual, runtimeSessionId=session_id("iam"),
                payload=json.dumps({"prompt": "What is 3+4? Reply with just the number."}))
            raw = out["response"].read().decode("utf-8", "replace")
            return "ok", raw[:160]
        except ClientError as exc:
            return exc.response["Error"]["Code"], str(exc)[:160]

    # IAM policy propagation: retry the allowed call for a while before judging it
    result = wait_for("invoke with generated policy", lambda: (
        lambda r: r if r[0] == "ok" else None)(invoke(arn, qualifier)), timeout=180, every=15)
    check("invoke.allowed_endpoint", result is not None,
          f"{qualifier}: {result[1] if result else 'never allowed'}")
    other = other_runtime(arn)
    if other:
        verdict, detail = invoke(other, "DEFAULT")
        check("invoke.other_runtime_denied", verdict == "AccessDeniedException",
              f"{verdict}: {detail[:100]}")
    else:
        skip("invoke.other_runtime_denied", "no other runtime in the account")
    verdict, detail = invoke(arn, "pjE2eOther")
    check("invoke.other_endpoint_denied", verdict == "AccessDeniedException",
          f"{verdict}: {detail[:100]}")
    delete_role(iam, ROLE_NAME)
    STATE["role"] = None
    check("invoke.role_deleted", True, ROLE_NAME)


def other_runtime(arn: str) -> str | None:
    """Any runtime but ours — only ever called with the role that must be refused."""
    control = boto3.client("bedrock-agentcore-control", region_name=arn.split(":")[3])
    for rt in control.list_agent_runtimes(maxResults=50).get("agentRuntimes", []):
        if rt.get("agentRuntimeArn") and rt["agentRuntimeArn"] != arn:
            return rt["agentRuntimeArn"]
    return None


# ── 8. eval cost ──────────────────────────────────────────────────────────────


def eval_cost(admin: httpx.Client, users: dict[str, httpx.Client], agent_id: str,
              dataset_id: str) -> None:
    a = users["user-a"]
    evaluators = ["Builtin.Correctness"]
    res = call(a, "POST", "/api/eval/runs/estimate", project=PROJECT_A,
               json={"agent_id": agent_id, "dataset_id": dataset_id, "evaluators": evaluators})
    est = body(res)
    rng = est.get("range") or {}
    check("eval.estimate_range", res.status_code == 200 and rng.get("low") is not None
          and rng["low"] <= rng.get("mid", 0) <= rng.get("high", 0),
          f"{code(res)} total={est.get('total_usd')} range={rng}")
    # the monthly budget is the one money limit: a zero budget is spent from the start,
    # so even an administrator's run is refused until the budget is cleared
    res = set_quota(admin, PROJECT_A, {"monthly_budget_usd": 0})
    check("quota.set_monthly_budget", res.status_code == 200, code(res))
    res = call(admin, "POST", "/api/eval/runs", project=PROJECT_A, json={
        "agent_id": agent_id, "dataset_id": dataset_id, "evaluators": evaluators,
        "confirm_cost": True})
    data = body(res)
    check("quota.eval_refused_by_budget", res.status_code == 409 and data.get("code") ==
          "project.budget_exhausted" and (data.get("detail") or {}).get("budget_usd") == 0,
          f"{code(res)} {data.get('detail')}")
    if res.status_code == 201:
        print(f"     unexpected admin run {data.get('id')} started")
    res = call(admin, "PUT", f"/api/projects/{PROJECT_A}/quotas",
               json={"quotas": {"monthly_budget_usd": None}})
    check("quota.budget_restored", res.status_code == 200, code(res))
    run: dict = {}
    for attempt in range(2):
        res = call(a, "POST", "/api/eval/runs", project=PROJECT_A, json={
            "agent_id": agent_id, "dataset_id": dataset_id, "evaluators": evaluators,
            "wait_seconds": 180 + 120 * attempt})
        check("eval.run_started", res.status_code == 201, code(res), fatal=True)
        run_id = body(res)["id"]
        run = wait_for("eval run", lambda rid=run_id: (lambda r: r if r.get("status") in (
            "completed", "failed", "stopped") else None)(
            body(call(a, "GET", f"/api/eval/runs/{rid}", project=PROJECT_A))),
            timeout=1800, every=20) or {}
        # the batch reads the session's spans from Transaction Search; when they are
        # not indexed yet AgentCore fails the batch ("no span documents") — an ingest
        # delay of the account, not a verdict: one more run with a longer wait
        # likewise a 5xx / throttle of the AgentCore data plane mid-run
        error = run.get("error") or ""
        if not any(mark in error for mark in TRANSIENT_EVAL_ERRORS):
            break
        print(f"     eval: transient AWS failure ({error[:120]}) — one more run", flush=True)
    cost = run.get("cost") or {}
    if run.get("status") == "completed" and cost.get("kind") != "actual":
        # the measured cost is recorded just after the run turns terminal
        # (evaluation/run_cost.record) — until then the row shows the estimate
        later = wait_for("measured run cost", lambda rid=run_id: (
            lambda r: r if (r.get("cost") or {}).get("kind") == "actual" else None)(
            body(call(a, "GET", f"/api/eval/runs/{rid}", project=PROJECT_A))),
            timeout=180, every=15)
        cost = (later or {}).get("cost") or cost
    check("eval.run_completed", run.get("status") == "completed",
          f"status={run.get('status')} error={(run.get('error') or '')[:240]}")
    check("eval.run_cost", cost.get("kind") == "actual" and isinstance(cost.get("usd"),
          int | float), f"cost={cost}")
    res = call(users["user-b"], "GET", f"/api/eval/runs/{run_id}", project=PROJECT_B)
    check("iso.b_eval_run_404", res.status_code == 404, code(res))


# ── 9. transfer ───────────────────────────────────────────────────────────────


def transfer(admin: httpx.Client, users: dict[str, httpx.Client], agent_id: str,
             arn: str) -> None:
    res = call(users["user-a"], "POST", f"/api/projects/{PROJECT_A}/transfer",
               json={"resource_id": agent_id, "to_project_id": PROJECT_B})
    check("transfer.member_refused", res.status_code in (403, 404), code(res))
    res = call(admin, "POST", f"/api/projects/{PROJECT_A}/transfer",
               json={"resource_id": agent_id, "to_project_id": PROJECT_B})
    data = body(res)
    check("transfer.done", res.status_code == 200 and data.get("to_project_id") == PROJECT_B,
          f"{code(res)} retag={data.get('retag_job_ids')}")
    if res.status_code != 200:
        return
    STATE["agent_project"] = PROJECT_B
    got = wait_for("tags follow to B", lambda: (
        lambda t: t if standard_ok(t[0], PROJECT_B, agent_id)
        and standard_ok(t[1], PROJECT_B, agent_id) else None)(aws_tags(arn)),
        timeout=300, every=15)
    check("transfer.retagged", got is not None,
          f"runtime={got[0].get('launchpad:project') if got else None} "
          f"role={got[1].get('launchpad:project') if got else None}")
    if got:
        # A's cost center must not keep billing the moved agent (B sets none)
        gone = wait_for("A's custom tag removed", lambda: (
            lambda t: t if COST_TAG_KEY not in t[0] and COST_TAG_KEY not in t[1]
            else None)(aws_tags(arn)), timeout=120, every=15)
        check("transfer.custom_tag_dropped", gone is not None,
              "runtime and role" if gone else f"{COST_TAG_KEY} still on the runtime / role")
    res = call(users["user-b"], "GET", f"/api/agents/{agent_id}", project=PROJECT_B)
    check("transfer.b_sees", res.status_code == 200, code(res))
    res = call(users["user-a"], "GET", f"/api/agents/{agent_id}", project=PROJECT_A)
    check("transfer.a_404", res.status_code == 404, code(res))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true", help="leave everything in place")
    parser.add_argument("--timeout", type=int, default=1200, help="deploy timeout (s)")
    parser.add_argument("--skip-transfer", action="store_true")
    args = parser.parse_args()

    admin = e2e_client(args.base, timeout=240)
    admin.headers["X-Workspace"] = "default"
    workspaces = body(call(admin, "GET", "/api/workspaces")).get("workspaces", [])
    ws = next((w for w in workspaces if w["id"] == "default"), None)
    if ws is None or ws.get("tier") == "prod":
        print(f"refusing: workspace default is {ws and ws.get('tier')}")
        return 2
    print(f"── base {args.base} · workspace default ({ws['region']}) · run {RUN}")
    cleanup_leftovers(admin)
    try:
        users = structure(admin, args.base)
        agent_id = deploy(users["user-a"], args.timeout)
        arn = body(call(admin, "GET", f"/api/agents/{agent_id}", project=PROJECT_A))["arn"]
        dataset_id, lineage = seed(users["user-a"], agent_id)
        for leg in (
            lambda: isolation(admin, users, agent_id, dataset_id, lineage),
            lambda: storyline(users, agent_id),
            lambda: shares(admin, users, agent_id, dataset_id),
            lambda: quotas(admin, users),
            lambda: cost_tags(admin, users, agent_id, arn),
            lambda: invoke_access(users, agent_id, arn),
            lambda: eval_cost(admin, users, agent_id, dataset_id),
        ):
            try:
                leg()
            except Abort as exc:
                print(f"     leg aborted: {exc}")
        if args.skip_transfer:
            skip("transfer", "--skip-transfer")
        else:
            transfer(admin, users, agent_id, arn)
    except Abort as exc:
        print(f"ABORTED: {exc}")
    except Exception as exc:  # noqa: BLE001 — report, then clean up
        check("unexpected", False, f"{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        if args.keep:
            print(f"--keep: leaving {STATE}")
        else:
            try:
                for stray in STATE.get("stray", []):
                    call(admin, "DELETE", f"/api/agents/{stray}", project=PROJECT_B)
                cleanup(admin)
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup error: {type(exc).__name__}: {exc}")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
