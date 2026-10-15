"""Calling an agent from your own backend (design invoke-access §1).

Asserted: the route classification; the policy table — every method (zip / studio /
container / byoc / discovered / harness) × inbound mode (IAM / JWT) × endpoint
(DEFAULT / live / candidate) gives the exact resources, action and qualifier; the
`include=` options; samples carry the ARN, region and qualifier; the merged project
policy de-duplicates; the CSV / JSON export; and another workspace's or project's
agent answers 404 on both routes.
"""

import csv
import io
import json
from types import SimpleNamespace

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.core.route_policy import (
    PERM_PROJECT_USE,
    WORKSPACE_EXEMPT,
    required_role,
)
from app.models.ledger import Agent, Workspace
from app.services import invoke_access as svc
from tests.test_project_scope import _login, _member_of, _project, _user, app  # noqa: F401

DEFAULT = {"X-Project": "default"}
ACCT = "111122223333"
REGION = "us-west-2"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:runtime/lab-AbC123"
HARNESS_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:harness/h-Lab456"
JWT_CONFIG = {
    "mode": "jwt",
    "jwt": {
        "discovery_url": "https://idp.example.com/pool/.well-known/openid-configuration",
        "allowed_clients": ["client-1"], "allowed_audience": [], "allowed_scopes": ["agent/invoke"],
    },
}


def _ws(gated=False):
    return SimpleNamespace(account_id=ACCT, region=REGION, resources={"memory_id": "mem-1"},
                           release_policy={"release_mode": "gated"} if gated else {})


def _row(method="zip_runtime", *, mode="iam", endpoint_mode="default", arn=None, spec=None):
    harness = method == "harness" or (spec or {}).get("discovery", {}).get(
        "resource_type") == "harness"
    return Agent(
        id="a1", name="lab bot", method=method, status="active",
        arn=arn or (HARNESS_ARN if harness else RUNTIME_ARN), spec=spec or {},
        endpoint_mode=endpoint_mode, inbound_auth_mode=mode,
        inbound_auth_config=JWT_CONFIG if mode == "jwt" else None,
    )


def test_routes_are_classified():
    assert required_role("GET", "/api/agents/{agent_id}/invoke-access") == PERM_PROJECT_USE
    assert required_role("GET", "/api/projects/{project_id}/invoke-access") == "pperm:project.use"
    assert ("GET", "/api/projects/{project_id}/invoke-access") in WORKSPACE_EXEMPT
    assert ("GET", "/api/agents/{agent_id}/invoke-access") not in WORKSPACE_EXEMPT


RUNTIME_METHODS = ["zip_runtime", "studio", "container", "byoc", "discovered_runtime"]
# (endpoint_mode, gated) → the endpoints that exist
ENDPOINT_CASES = [
    ("default", False, ["DEFAULT"]),
    ("live", False, ["DEFAULT", "live"]),
    ("live", True, ["DEFAULT", "live", "candidate"]),
    ("default", True, ["DEFAULT"]),  # candidate only exists once migrated
]


@pytest.mark.parametrize("method", RUNTIME_METHODS + ["harness", "discovered_harness"])
@pytest.mark.parametrize("mode", ["iam", "jwt"])
@pytest.mark.parametrize("endpoint_mode,gated,expected", ENDPOINT_CASES)
@pytest.mark.parametrize("qualifier", [None, "DEFAULT", "live", "candidate"])
def test_policy_table(method, mode, endpoint_mode, gated, expected, qualifier):
    spec = {}
    if method == "discovered_harness":
        method, spec = "discovered_runtime", {"discovery": {"resource_type": "harness"}}
    agent = _row(method, mode=mode, endpoint_mode=endpoint_mode, spec=spec)
    harness = bool(spec) or method == "harness"
    ws = _ws(gated)
    if qualifier and qualifier not in expected:
        with pytest.raises(AppError) as err:
            svc.agent_access(agent, ws, qualifier=qualifier)
        assert err.value.code == "invoke_access.unknown_qualifier"
        assert err.value.detail["available"] == expected
        return
    out = svc.agent_access(agent, ws, qualifier=qualifier)
    chosen = [qualifier] if qualifier else expected
    arn = HARNESS_ARN if harness else RUNTIME_ARN
    assert out["kind"] == ("harness" if harness else "runtime")
    assert out["mode"] == mode and out["account"] == ACCT and out["region"] == REGION
    assert out["available_qualifiers"] == expected and out["qualifiers"] == chosen
    production = "live" if endpoint_mode == "live" else "DEFAULT"
    assert out["production_qualifier"] == production
    endpoint_rows = [r for r in out["resources"] if r["kind"].endswith("_endpoint")]
    assert [r["qualifier"] for r in endpoint_rows] == chosen
    assert [r["production"] for r in endpoint_rows] == [q == production for q in chosen]
    if harness:
        assert all(r["arn"] is None for r in endpoint_rows)
    else:
        assert [r["arn"] for r in endpoint_rows] == [
            f"{RUNTIME_ARN}/runtime-endpoint/{q}" for q in chosen]
    if mode == "jwt":
        assert out["policy"] is None and out["jwt"]["issuer"] == "https://idp.example.com/pool"
        assert out["notes"][0] == "jwt_no_iam"
        assert "Bearer" in out["samples"]["cli"] and "Authorization" in out["samples"]["ts"]
        return
    assert out["jwt"] is None
    (stmt,) = out["policy"]["Statement"]
    assert out["policy"]["Version"] == "2012-10-17" and stmt["Effect"] == "Allow"
    if harness:
        assert stmt["Action"] == ["bedrock-agentcore:InvokeHarness"]
        assert stmt["Resource"] == [HARNESS_ARN]
        assert "harness_action_unverified" in out["notes"]
        assert "invoke_harness" in out["samples"]["python"]
    else:
        assert stmt["Action"] == ["bedrock-agentcore:InvokeAgentRuntime"]
        assert stmt["Resource"] == [RUNTIME_ARN] + [
            f"{RUNTIME_ARN}/runtime-endpoint/{q}" for q in chosen]
        assert "invoke_agent_runtime" in out["samples"]["python"]
    sample_q = qualifier or production
    for lang in ("cli", "python", "ts"):
        assert arn in out["samples"][lang] and REGION in out["samples"][lang]
        if not harness or sample_q != "DEFAULT":
            assert sample_q in out["samples"][lang]


def test_include_adds_stop_session_and_memory():
    agent = _row(spec={"memory": {"short_term": True}})
    out = svc.agent_access(agent, _ws(), qualifier="DEFAULT",
                           include=svc.parse_include("stop_session,memory"))
    actions = [s["Action"] for s in out["policy"]["Statement"]]
    assert actions[1] == ["bedrock-agentcore:StopRuntimeSession"]
    assert "bedrock-agentcore:RetrieveMemoryRecords" in actions[2]
    assert out["policy"]["Statement"][2]["Resource"] == [
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:memory/mem-1"]
    pinned = _row(spec={"memory": {"long_term": True, "memory_id": "own-mem"}})
    stmt = svc.agent_access(pinned, _ws(), include=frozenset({"memory"}))["policy"]["Statement"]
    assert stmt[-1]["Resource"][0].endswith(":memory/own-mem")
    # a harness has no session-stop operation; an agent without memory gets a hint
    harness = svc.agent_access(_row("harness"), _ws(), include=frozenset({"stop_session",
                                                                          "memory"}))
    assert len(harness["policy"]["Statement"]) == 1
    assert {"harness_no_stop_session", "memory_unresolved"} <= set(harness["notes"])
    with pytest.raises(AppError) as err:
        svc.parse_include("stop_session,everything")
    assert err.value.code == "invoke_access.unknown_include"


def test_no_resource_and_unparsed_arns():
    draft = _row()
    draft.arn = None
    with pytest.raises(AppError) as err:
        svc.agent_access(draft, _ws())
    assert err.value.status_code == 409 and err.value.code == "agent.no_resource"
    odd = _row(arn="arn:aws:bedrock-agentcore:eu-west-1:999988887777:runtime/x/extra")
    out = svc.agent_access(odd, SimpleNamespace(account_id="444455556666", region="eu-west-1",
                                                resources={}, release_policy={}))
    assert out["account"] == "444455556666" and out["arn"] == odd.arn
    cn = svc.target_of(_row(arn=f"arn:aws-cn:bedrock-agentcore:cn-north-1:{ACCT}:runtime/r"),
                       None)
    assert cn.partition == "aws-cn" and cn.region == "cn-north-1"


def test_merged_policy_dedupes_and_skips_jwt_and_drafts():
    a = _row(endpoint_mode="live")
    b = _row(endpoint_mode="live")  # the same runtime twice: resources dedupe
    b.id = "a2"
    c = _row("harness")
    c.id, c.name = "a3", "helper"
    j = _row(mode="jwt")
    j.id = "a4"
    draft = _row()
    draft.id, draft.arn = "a5", None
    out = svc.project_access([(a, _ws(True)), (b, _ws(True)), (c, _ws()), (j, _ws()),
                              (draft, _ws())], qualifier="live")
    stmts = {s["Sid"]: s for s in out["policy"]["Statement"]}
    assert set(stmts) == {"InvokeRuntimes", "InvokeHarnesses"}
    assert stmts["InvokeRuntimes"]["Resource"] == [RUNTIME_ARN,
                                                   f"{RUNTIME_ARN}/runtime-endpoint/live"]
    assert stmts["InvokeHarnesses"]["Resource"] == [HARNESS_ARN]
    assert out["jwt_agents"] == ["a4"]
    assert out["skipped"] == [{"agent_id": "a5", "name": "lab bot", "reason": "no_resource"}]
    # the inventory lists every endpoint of every callable agent, JWT ones included
    assert [(r["agent_id"], r["qualifier"]) for r in out["inventory"]] == [
        ("a1", "DEFAULT"), ("a1", "live"), ("a1", "candidate"),
        ("a2", "DEFAULT"), ("a2", "live"), ("a2", "candidate"),
        ("a3", "DEFAULT"), ("a4", "DEFAULT")]
    everything = svc.project_access([(a, _ws(True))])
    assert everything["policy"]["Statement"][0]["Resource"] == [RUNTIME_ARN] + [
        f"{RUNTIME_ARN}/runtime-endpoint/{q}" for q in ("DEFAULT", "live", "candidate")]
    assert svc.project_access([(j, _ws())])["policy"] is None


def test_inventory_csv_format():
    out = svc.project_access([(_row(endpoint_mode="live"), _ws())])
    rows = list(csv.DictReader(io.StringIO(svc.inventory_csv(out["inventory"]))))
    assert list(rows[0]) == list(svc.INVENTORY_FIELDS)
    assert [(r["qualifier"], r["production"]) for r in rows] == [("DEFAULT", "false"),
                                                                ("live", "true")]
    assert rows[1]["endpoint_arn"] == f"{RUNTIME_ARN}/runtime-endpoint/live"


# ---- HTTP ----


def _agent(name="lab", *, project="default", workspace_id=DEFAULT_WORKSPACE_ID, **fields) -> str:
    base = {"workspace_id": workspace_id, "name": name, "method": "zip_runtime",
            "status": "active", "resource_id": "lab-AbC123", "arn": RUNTIME_ARN, "spec": {}}
    db = SessionLocal()
    try:
        with project_context(project):
            row = Agent(**{**base, **fields})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _gate_default_workspace():
    db = SessionLocal()
    try:
        db.get(Workspace, DEFAULT_WORKSPACE_ID).release_policy = {"release_mode": "gated"}
        db.commit()
    finally:
        db.close()


def test_agent_route(client):
    _gate_default_workspace()
    agent_id = _agent(endpoint_mode="live")
    res = client.get(f"/api/agents/{agent_id}/invoke-access?qualifier=live&include=stop_session",
                     headers=DEFAULT)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["qualifiers"] == ["live"] and body["sample_qualifier"] == "live"
    assert body["available_qualifiers"] == ["DEFAULT", "live", "candidate"]
    assert [s["Action"][0] for s in body["policy"]["Statement"]] == [
        "bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"]
    bad = client.get(f"/api/agents/{agent_id}/invoke-access?qualifier=blue", headers=DEFAULT)
    assert bad.status_code == 400 and bad.json()["code"] == "invoke_access.unknown_qualifier"
    draft = _agent("draft", arn=None, status="draft")
    res = client.get(f"/api/agents/{draft}/invoke-access", headers=DEFAULT)
    assert res.status_code == 409 and res.json()["code"] == "agent.no_resource"


def test_agent_route_another_workspace_or_project_answers_404(client):
    foreign = _agent(workspace_id="acct-other")
    res = client.get(f"/api/agents/{foreign}/invoke-access", headers=DEFAULT)
    assert res.status_code == 404 and res.json()["code"] == "agent.not_found"
    _project("alpha")
    theirs = _agent("theirs", project="alpha")
    res = client.get(f"/api/agents/{theirs}/invoke-access", headers=DEFAULT)
    assert res.status_code == 404 and res.json()["code"] == "agent.not_found"
    assert client.get(f"/api/agents/{theirs}/invoke-access",
                      headers={"X-Project": "alpha"}).status_code == 200
    assert client.get("/api/agents/nope/invoke-access", headers=DEFAULT).status_code == 404


def test_project_route_inventory_and_exports(client):
    ours = _agent("ours", endpoint_mode="live")
    harness = _agent("helper", method="harness", arn=HARNESS_ARN)
    _project("alpha")
    theirs = _agent("theirs", project="alpha")
    res = client.get("/api/projects/default/invoke-access")
    assert res.status_code == 200, res.text
    body = res.json()
    ids = {r["agent_id"] for r in body["inventory"]}
    assert ids == {ours, harness} and theirs not in ids
    assert {s["Sid"] for s in body["policy"]["Statement"]} == {"InvokeRuntimes",
                                                               "InvokeHarnesses"}
    only = client.get(f"/api/projects/default/invoke-access?agents={ours}&qualifier=live").json()
    assert only["policy"]["Statement"] == [{
        "Sid": "InvokeRuntimes", "Effect": "Allow",
        "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
        "Resource": [RUNTIME_ARN, f"{RUNTIME_ARN}/runtime-endpoint/live"]}]
    # an id of another project reads like a missing one
    res = client.get(f"/api/projects/default/invoke-access?agents={ours},{theirs}")
    assert res.status_code == 404 and res.json()["detail"]["agent_ids"] == [theirs]
    exported = client.get("/api/projects/default/invoke-access?format=csv")
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/csv")
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.text.splitlines()[0] == ",".join(svc.INVENTORY_FIELDS)
    as_json = client.get("/api/projects/default/invoke-access?format=json")
    assert json.loads(as_json.text)["project_id"] == "default"
    assert client.get("/api/projects/default/invoke-access?format=xml").status_code == 400
    assert client.get("/api/projects/nope/invoke-access").status_code == 404


def test_project_route_non_member_answers_404(app):  # noqa: F811
    _project("alpha")
    _agent("theirs", project="alpha")
    _member_of("default", _user("dev", grant=False), "developer")
    dev = _login(app, "dev")
    assert dev.get("/api/projects/default/invoke-access").status_code == 200
    res = dev.get("/api/projects/alpha/invoke-access")
    assert res.status_code == 404 and res.json()["code"] == "project.not_found"
