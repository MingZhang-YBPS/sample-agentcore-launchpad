"""Hosted MCP servers beyond tool review (design projects §15.7, §15.8, §15.12, §15.15):
the Cedar scaffold through the governance service, health checks and the `mcp.health`
task, the target following the live endpoint and rollback, console-created Lambda tool
targets, and hand-made MCP targets brought under review — AWS stubbed."""

# `aws` is the AWS-stub fixture of the hosted-MCP tests, imported and then
# requested by name — which ruff reads as a redefinition.
# ruff: noqa: F811

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.deployer import byoc as byoc_dep
from app.dlc import scheduler
from app.main import create_app
from app.models.lambda_tools import ExternalMcpTarget, LambdaToolTarget, McpServerState
from app.models.ledger import Agent, Deployment, Job
from app.models.mcp_review import McpToolReview
from app.models.projects import Project, ProjectMember, ProjectWorkspace
from app.routers import mcp_reviews as mcp_reviews_router
from app.schemas.governance import POLICY_NAME_PATTERN
from app.services import (
    access,
    gateway_tools,
    governance,
    mcp_reviews,
    mcp_servers,
    workspace_iam,
)
from app.services import users as users_service
from app.services.agentcore import runtime as rt
from tests.conftest import TEST_PROJECT, client_error, set_default_resources, ws_ctx
from tests.test_byoc import _client_router, _get_agent, _stage_ctx
from tests.test_governance_policy_lifecycle import FakeControl, FakeIam
from tests.test_mcp_servers import (  # noqa: F401 — `aws` is a fixture
    FIND_ORDER,
    RESOURCES,
    RUNTIME_ARN,
    _registered,
    aws,
)

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
LAMBDA_ARN = "arn:aws:lambda:us-west-2:111122223333:function:crm-tools"
SCHEMA = [{"name": "lookup", "description": "Look a customer up",
           "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}}]


def _approved(agent_id: str, version: str, description: str = "Find") -> str:
    tools = [{"name": "find_order", "description": description, "input_schema": {}}]
    db = SessionLocal()
    try:
        with project_context(None):
            row = McpToolReview(
                workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, version=version,
                tools=tools, tools_hash=mcp_reviews.tools_hash(tools), status="approved",
                requested_by="dev", decided_by="reviewer", decided_at=datetime.now(UTC),
            )
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _state(agent_id: str) -> McpServerState | None:
    db = SessionLocal()
    try:
        with project_context(None):
            return db.get(McpServerState, agent_id)
    finally:
        db.close()


def _repointable(aws) -> list[dict]:
    """Give the stub Gateway UpdateGatewayTarget; returns the recorded calls."""
    calls: list[dict] = []

    def update(**kwargs):
        calls.append(kwargs)
        aws.control.targets[kwargs["targetId"]]["targetConfiguration"] = (
            kwargs["targetConfiguration"]
        )
        return {"targetId": kwargs["targetId"]}

    aws.control.update_gateway_target = update
    return calls


def _endpoint(aws, target_id: str) -> str:
    return aws.control.targets[target_id]["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"]


# ── Cedar scaffold (§15.7) ────────────────────────────────────────────────────


def test_policy_names_fit_the_aws_pattern_and_are_per_target():
    names = governance.mcp_policy_names("crm-lookup")
    assert names == {"unreviewed": "lp_mcp_crm_lookup_unreviewed",
                     "project": "lp_mcp_crm_lookup_project"}
    long = governance.mcp_policy_names("a" * 100)
    assert all(re.match(POLICY_NAME_PATTERN, n) for n in (*names.values(), *long.values()))


def test_statements_forbid_the_unreviewed_and_permit_the_project():
    gateway = {"gatewayArn": "arn:gw", "authorizerType": "CUSTOM_JWT"}
    forbid = governance.mcp_unreviewed_statement(gateway, "crm", ["b", "a"])
    assert forbid.startswith("forbid(") and "unless" not in forbid
    assert 'action in [AgentCore::Action::"crm___a", AgentCore::Action::"crm___b"]' in forbid
    assert "principal is AgentCore::OAuthUser" in forbid
    single = governance.mcp_project_statement(gateway, "crm", ["a"], project_id=None)
    assert single.startswith("permit(") and single.endswith(");") and "when" not in single
    assert 'action == AgentCore::Action::"crm___a"' in single
    scoped = governance.mcp_project_statement(gateway, "crm", ["a"], project_id="alpha")
    assert 'like "*lp-project-alpha*"' in scoped


def _engine(control: FakeControl) -> None:
    engine = control.create_policy_engine(name="lp")
    control.gateway["policyEngineConfiguration"] = {"arn": engine["policyEngineArn"],
                                                    "mode": "LOG_ONLY"}


def _policies(control: FakeControl) -> dict[str, str]:
    return {p["name"]: p["definition"]["cedar"]["statement"] for p in control.policies.values()}


def test_scaffold_forbids_an_open_review_then_permits_the_approved_list():
    control = FakeControl()
    _engine(control)
    plan = governance.mcp_scaffold_plan(control, "gw-1", target_name="crm",
                                        approved_tools=None, open_tools=["find"],
                                        project_id=None)
    assert [(s["name"], s["action"]) for s in plan["steps"]] == [
        ("lp_mcp_crm_unreviewed", "create")]
    assert plan["mode"] == "LOG_ONLY"
    results = governance.apply_mcp_scaffold(
        ws_ctx(RESOURCES), "gw-1", plan["steps"], target_name="crm",
        acknowledged_gateway_ids=[], control=control, iam=FakeIam(),
    )
    assert [r["status"] for r in results] == ["succeeded"]
    assert _policies(control)["lp_mcp_crm_unreviewed"].startswith("forbid(")
    again = governance.mcp_scaffold_plan(control, "gw-1", target_name="crm",
                                         approved_tools=None, open_tools=["find"],
                                         project_id=None)
    assert [s["action"] for s in again["steps"]] == ["keep"]

    # approved: the forbid goes, the permit comes, and ENFORCE goes through gateway_mode
    plan = governance.mcp_scaffold_plan(control, "gw-1", target_name="crm",
                                        approved_tools=["find"], open_tools=None,
                                        project_id="alpha")
    assert sorted((s["name"], s["action"]) for s in plan["steps"]) == [
        ("lp_mcp_crm_project", "create"), ("lp_mcp_crm_unreviewed", "delete")]
    results = governance.apply_mcp_scaffold(
        ws_ctx(RESOURCES), "gw-1", plan["steps"], target_name="crm",
        acknowledged_gateway_ids=[], control=control, iam=FakeIam(),
        mode={"mode": "ENFORCE", "confirmation_name": "payments-gw",
              "override_reason": "reviewed the LOG_ONLY decisions"},
    )
    assert [r["status"] for r in results] == ["succeeded"] * 3
    assert list(_policies(control)) == ["lp_mcp_crm_project"]
    assert "lp-project-alpha" in _policies(control)["lp_mcp_crm_project"]
    assert control.gateway["policyEngineConfiguration"]["mode"] == "ENFORCE"


def test_scaffold_stops_at_the_first_refused_step():
    control = FakeControl()
    _engine(control)
    results = governance.apply_mcp_scaffold(
        ws_ctx(RESOURCES), "gw-1", [{"name": "x", "action": "create", "statement": "permit;"}],
        target_name="crm", acknowledged_gateway_ids=[], control=control, iam=FakeIam(),
        mode={"mode": "ENFORCE", "confirmation_name": "wrong"},
    )
    assert [r["status"] for r in results] == ["succeeded", "failed"]
    assert "confirmation_mismatch" in results[-1]["error"]


def test_scaffold_without_an_engine_is_refused():
    control = FakeControl()
    with pytest.raises(AppError) as err:
        governance.mcp_scaffold_plan(control, "gw-1", target_name="crm", approved_tools=["a"],
                                     open_tools=None, project_id=None)
    assert err.value.code == "governance.policy_engine_missing"


# ── health (§15.8) ────────────────────────────────────────────────────────────


def test_health_records_healthy_drift_and_failing(aws, monkeypatch):
    agent_id = _registered(aws)
    health = mcp_servers.check_health(ws_ctx(RESOURCES), _get_agent(agent_id))
    assert health["status"] == "healthy" and health["tool_count"] == 1
    assert _state(agent_id).health["status"] == "healthy"

    def broken(_ws):
        raise AppError("gateway.unauthorized", "401", status_code=401)

    monkeypatch.setattr(mcp_servers.mcp_client, "tools_list", broken)
    for expected in (1, 2, 3):
        db = SessionLocal()
        row = db.get(Agent, agent_id)
        row.spec = {**row.spec, "mcp": {**row.spec["mcp"], "target_id": "T1"}}
        db.commit()
        db.close()
        aws.control.targets.setdefault("T1", {"name": "crm-lookup", "targetConfiguration": {
            "mcp": {"mcpServer": {"endpoint": rt.bearer_invoke_url(
                "us-west-2", RUNTIME_ARN, "DEFAULT")}}}})
        health = mcp_servers.check_health(ws_ctx(RESOURCES), _get_agent(agent_id))
        assert health["status"] == "failing"
        assert health["consecutive_failures"] == expected
    assert health["alert"] is True and "401" in health["error"]


def test_health_reports_drift_and_withholds(aws, monkeypatch):
    agent_id = _registered(aws)
    monkeypatch.setattr(mcp_servers.mcp_client, "tools_list", lambda _ws: [
        {"name": f"{t['name']}___find_order", "description": "changed"}
        for t in aws.control.targets.values()])
    health = mcp_servers.check_health(ws_ctx(RESOURCES), _get_agent(agent_id))
    assert health["status"] == "drift" and health["review_id"]
    assert aws.control.targets == {}
    # the next check finds no live target: withheld until the review is decided
    health = mcp_servers.check_health(ws_ctx(RESOURCES), _get_agent(agent_id))
    assert health["status"] == "withheld"


def test_the_scheduler_checks_each_server_in_its_project_and_skips_archived(aws, monkeypatch):
    agent_id = _registered(aws)
    db = SessionLocal()
    db.get(Agent, agent_id).status = "active"  # as the finished deploy leaves it
    db.commit()
    db.close()
    seen: list[tuple[str, str | None]] = []

    def check(_ws, agent):
        from app.core.project_scope import current_project

        seen.append((agent.id, current_project()))
        return {"status": "healthy"}

    monkeypatch.setattr(mcp_servers, "check_health", check)
    db = SessionLocal()
    try:
        out = scheduler.run_mcp_health(db)
    finally:
        db.close()
    project_id = _get_agent(agent_id).project_id
    assert seen == [(agent_id, project_id)]
    assert out["servers"] == [{"agent_id": agent_id, "status": "healthy"}]
    db = SessionLocal()
    with project_context(None):
        db.get(Project, project_id).status = "archived"
        db.commit()
    try:
        out = scheduler.run_mcp_health(db)
    finally:
        db.close()
    assert out["servers"][0]["status"] == "skipped" and len(seen) == 1
    assert any(name == "mcp.health" and every == 3600 for name, _t, every in scheduler.TASKS)


# ── live endpoint and rollback (§15.8) ────────────────────────────────────────


@pytest.fixture
def endpoints(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(rt, "ensure_runtime_endpoint", lambda client, **kw: calls.append(
        ("ensure", kw["endpoint_name"], str(kw["version"]))) or {})
    monkeypatch.setattr(rt, "delete_runtime_endpoint", lambda client, **kw: calls.append(
        ("delete", kw["endpoint_name"])))

    def gone(client, **kw):
        raise client_error("ResourceNotFoundException")

    monkeypatch.setattr(rt, "get_runtime_endpoint", gone)
    return calls


def _two_versions(aws) -> str:
    agent_id = _registered(aws)
    _approved(agent_id, "1")
    db = SessionLocal()
    db.get(Agent, agent_id).version = "2"
    db.commit()
    db.close()
    _approved(agent_id, "2", description="Find v2")
    return agent_id


def test_rollback_pins_live_restores_the_list_and_repoints_the_target(aws, endpoints):
    agent_id = _two_versions(aws)
    repoints = _repointable(aws)
    db = SessionLocal()
    review = mcp_servers.plan_rollback(db, db.get(Agent, agent_id), None)
    db.close()
    assert review.version == "1"
    operation = mcp_servers.run_rollback(ws_ctx(RESOURCES), agent_id, review.id, actor="ops")
    assert operation["status"] == "succeeded", operation
    assert ("ensure", "live", "1") in endpoints
    [(target_id, _)] = aws.control.targets.items()
    assert _endpoint(aws, target_id).endswith("qualifier=live") and repoints
    state = _state(agent_id)
    assert (state.qualifier, state.live_version) == ("live", "1")
    db = SessionLocal()
    latest = mcp_reviews.latest_approved(db, agent_id)
    db.close()
    assert latest.source == "rollback" and latest.version == "1"
    status = mcp_servers.server_status(ws_ctx(RESOURCES), _get_agent(agent_id))
    assert status["qualifier"] == "live" and status["endpoint"].endswith("qualifier=live")
    assert [v["version"] for v in status["rollback_versions"]] == ["1", "2"]

    # a re-publish un-pins: the target follows DEFAULT again
    byoc_dep._stage_register(_stage_ctx(agent_id, "d", ws_ctx(RESOURCES)), _get_agent(agent_id))
    assert _endpoint(aws, target_id).endswith("qualifier=DEFAULT")
    assert _state(agent_id).qualifier == "DEFAULT"

    # the endpoint the rollback made goes with the server
    byoc_dep.delete_agent_resources(_get_agent(agent_id), ws_ctx(RESOURCES))
    assert ("delete", "live") in endpoints


def test_rollback_needs_an_earlier_approved_version(aws):
    agent_id = _registered(aws)
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as err:
            mcp_servers.plan_rollback(db, db.get(Agent, agent_id), None)
        assert err.value.code == "mcp.rollback_unavailable"
        _approved(agent_id, "1")
        with pytest.raises(AppError) as err:
            mcp_servers.plan_rollback(db, db.get(Agent, agent_id), "1")
        assert err.value.code == "mcp.rollback_noop"
    finally:
        db.close()


def test_health_follows_the_live_endpoint(aws):
    agent_id = _registered(aws)
    _repointable(aws)
    with project_context(None):
        db = SessionLocal()
        row = db.get(McpServerState, agent_id)  # the deploy left it on DEFAULT
        row.qualifier, row.live_version = "live", "1"
        db.commit()
        db.close()
    health = mcp_servers.check_health(ws_ctx(RESOURCES), _get_agent(agent_id))
    assert health["repointed"] is True and health["status"] == "healthy"
    assert _endpoint(aws, "T1").endswith("qualifier=live")


# ── the API ───────────────────────────────────────────────────────────────────


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _alpha() -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            db.add(Project(id="alpha", name="ALPHA", status="active"))
            db.add(ProjectWorkspace(project_id="alpha", workspace_id=DEFAULT_WORKSPACE_ID,
                                    is_workspace_default=False))
            access.bump_epoch(db)
            db.commit()
    finally:
        db.close()


def _member(app, username: str, role_key: str, project: str = "alpha") -> tuple[TestClient, str]:
    db = SessionLocal()
    try:
        user, _ = users_service.create_user(
            db, username=username, email=f"{username}@acme-corp.com", role="member",
            password="sufficient-pass", workspaces=[], created_by="test",
        )
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        with project_context(None):
            db.add(ProjectMember(project_id=project, user_id=user.id, role_key=role_key))
            access.bump_epoch(db)
            db.commit()
        user_id = user.id
    finally:
        db.close()
    client = TestClient(app, client=PEER)
    res = client.post("/api/auth/login", json={"username": username, "password": "sufficient-pass"})
    assert res.status_code == 200, res.text
    return client, user_id


def _admin(app) -> TestClient:
    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


def _server(pid: str, *, deployer: tuple[str, str] | None = None) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            spec = {"name": f"mcp-{pid}", "method": "byoc", "protocol": "mcp",
                    "byoc": {"artifact_kind": "code_zip", "upload_id": "u1"},
                    "mcp": {"target_name": f"crm-{pid}", "client_id": "client-1",
                            "provider_arn": "arn:provider/p", "target_id": None}}
            agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=spec["name"], method="byoc",
                          status="active", spec=spec, arn=RUNTIME_ARN, resource_id="rt-1",
                          version="2")
            db.add(agent)
            db.flush()
            if deployer is not None:
                job = Job(workspace_id=DEFAULT_WORKSPACE_ID, type="deploy_agent",
                          payload={"actor": deployer[0], "actor_user_id": deployer[1]})
                db.add(job)
                db.flush()
                db.add(Deployment(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
                                  job_id=job.id))
            db.commit()
            return agent.id
    finally:
        db.close()


class _Lambda:
    def __init__(self):
        self.functions = {LAMBDA_ARN}

    def get_function(self, FunctionName):
        if FunctionName not in self.functions:
            raise client_error("ResourceNotFoundException")
        return {"Configuration": {"FunctionArn": FunctionName}}


class _Iam:
    def __init__(self):
        self.policies: dict[tuple[str, str], str] = {}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.policies[(RoleName, PolicyName)] = PolicyDocument

    def delete_role_policy(self, RoleName, PolicyName):
        if (RoleName, PolicyName) not in self.policies:
            raise client_error("NoSuchEntity")
        del self.policies[(RoleName, PolicyName)]


@pytest.fixture
def world(app, aws, monkeypatch):
    set_default_resources(RESOURCES)
    _alpha()
    dev, dev_id = _member(app, "dave", "developer")
    steward, _ = _member(app, "stella", "tool_steward")
    lam, iam = _Lambda(), _Iam()
    _client_router(monkeypatch, {"cognito-idp": aws.cognito, "s3": aws.s3, "lambda": lam,
                                 "iam": iam})
    monkeypatch.setattr(gateway_tools, "control_client", lambda _ws=None: aws.control)
    aws.control.get_gateway = lambda gatewayIdentifier: {
        "gatewayId": gatewayIdentifier, "roleArn": "arn:aws:iam::111122223333:role/gw-role"}
    aws.control.list_gateway_targets = lambda **_kw: {"items": [
        {"targetId": tid, "name": t.get("name")} for tid, t in aws.control.targets.items()]}
    # background work, inline
    monkeypatch.setattr(mcp_servers, "_background", lambda agent, name, fn: fn())
    monkeypatch.setattr(gateway_tools, "start_publish_lambda",
                        lambda ws, row: gateway_tools.publish_lambda(ws, row.id))
    governance_control = FakeControl()
    governance_control.gateway["gatewayId"] = "gw-1"
    _engine(governance_control)
    monkeypatch.setattr(mcp_reviews_router, "control_client", lambda _ws=None: governance_control)
    monkeypatch.setattr(governance, "control_client", lambda _ws=None: governance_control)
    monkeypatch.setattr(governance, "iam_client", lambda _ws=None: FakeIam())
    agent_id = _server("alpha", deployer=("dave", dev_id))
    return type("World", (), {"dev": dev, "steward": steward, "agent_id": agent_id,
                              "aws": aws, "app": app, "iam": iam, "lam": lam,
                              "governance": governance_control})


def _url(agent_id: str, *rest: str) -> str:
    return "/".join([f"/api/mcp-servers/{agent_id}", *rest])


def test_health_route_is_a_project_write(world):
    res = world.dev.post(_url(world.agent_id, "health"))
    assert res.status_code == 200, res.text
    assert res.json()["health"]["status"] == "withheld"  # never published
    res = world.steward.post(_url(world.agent_id, "health"))
    assert res.status_code == 403 and res.json()["code"] == "auth.permission_required"


def test_rollback_route_needs_agents_deploy_and_an_earlier_version(world, endpoints):
    res = world.steward.post(_url(world.agent_id, "rollback"), json={})
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["permission"] == "agents.deploy"
    res = world.dev.post(_url(world.agent_id, "rollback"), json={})
    assert res.status_code == 409 and res.json()["code"] == "mcp.rollback_unavailable"
    _approved(world.agent_id, "1")
    _approved(world.agent_id, "2")
    res = world.dev.post(_url(world.agent_id, "rollback"), json={})
    assert res.status_code == 202, res.text
    assert res.json()["version"] == "1"
    assert res.json()["operation"]["kind"] == "rollback"
    assert res.json()["operation"]["status"] == "succeeded", res.json()
    assert ("ensure", "live", "1") in endpoints


def test_scaffold_route_plans_and_applies_per_project(world):
    _approved(world.agent_id, "2")
    res = world.dev.post(_url(world.agent_id, "policies/scaffold"), json={"dry_run": True})
    assert res.status_code == 403
    res = world.steward.post(_url(world.agent_id, "policies/scaffold"), json={"dry_run": True})
    assert res.status_code == 200, res.text
    [step] = res.json()["plan"]["steps"]
    assert step["action"] == "create" and step["name"] == "lp_mcp_crm_alpha_project"
    # two projects share the workspace: the permit names the owning project
    assert "lp-project-alpha" in step["statement"]
    assert world.governance.policies == {}
    res = world.steward.post(_url(world.agent_id, "policies/scaffold"), json={})
    assert res.status_code == 202, res.text
    assert res.json()["operation"]["status"] == "succeeded"
    assert [p["name"] for p in world.governance.policies.values()] == [
        "lp_mcp_crm_alpha_project"]
    # the mode is shared infrastructure: with two projects only an administrator
    res = world.steward.post(_url(world.agent_id, "policies/scaffold"),
                             json={"mode": "ENFORCE", "dry_run": True})
    assert res.status_code == 403 and res.json()["code"] == "auth.infra_admin_required"


def test_scaffold_with_nothing_reviewed_is_refused(world):
    res = world.steward.post(_url(world.agent_id, "policies/scaffold"), json={"dry_run": True})
    assert res.status_code == 409 and res.json()["code"] == "mcp.scaffold_nothing_reviewed"


def test_another_projects_server_answers_404(world):
    theirs = _server(TEST_PROJECT)
    for client, path, body in ((world.dev, "health", None), (world.dev, "rollback", {}),
                               (world.steward, "policies/scaffold", {})):
        res = client.post(_url(theirs, path), json=body)
        assert res.status_code == 404, (path, res.text)


# ── Lambda tool targets (§15.12) ──────────────────────────────────────────────


def test_tool_schema_validation():
    assert gateway_tools.validate_tool_schema(SCHEMA)[0]["name"] == "lookup"
    for bad in ([], [{"name": "a b", "inputSchema": {"type": "object"}}],
                [{"name": "a", "inputSchema": {"type": "string"}}],
                [{"name": "a", "inputSchema": {"type": "object"}}] * 2,
                [{"name": "a", "inputSchema": {"type": "object"}, "evil": 1}]):
        with pytest.raises(AppError) as err:
            gateway_tools.validate_tool_schema(bad)
        assert err.value.code == "lambda_tool.invalid_schema"


def test_the_gateway_role_statement_names_each_function():
    other = "arn:aws:lambda:us-west-2:111122223333:function:billing"
    doc = workspace_iam.lambda_tools_policy([LAMBDA_ARN + ":prod", LAMBDA_ARN, other])
    assert [s["Resource"] for s in doc["Statement"]] == [
        [other, other + ":*"], [LAMBDA_ARN, LAMBDA_ARN + ":*"]]
    assert {s["Action"] for s in doc["Statement"]} == {"lambda:InvokeFunction"}
    iam = _Iam()
    workspace_iam.sync_lambda_tools_policy(iam, "gw-role", [LAMBDA_ARN])
    assert ("gw-role", workspace_iam.LAMBDA_TOOLS_POLICY_NAME) in iam.policies
    workspace_iam.sync_lambda_tools_policy(iam, "gw-role", [])
    workspace_iam.sync_lambda_tools_policy(iam, "gw-role", [])  # idempotent
    assert iam.policies == {}


def _lambda_body(**over) -> dict:
    return {"lambda_arn": LAMBDA_ARN, "target_name": "crm-tools", "tool_schema": SCHEMA,
            "description": "CRM lookups", **over}


def test_lambda_target_is_reviewed_before_it_exists_then_published(world):
    admin = _admin(world.app)
    # several projects share the Gateway: creating a target is an administrator's
    res = world.dev.post("/api/gateway-tools/lambda", json=_lambda_body())
    assert res.status_code == 403 and res.json()["code"] == "auth.infra_admin_required"
    res = admin.post("/api/gateway-tools/lambda", json=_lambda_body(),
                     headers={"X-Project": "alpha"})
    assert res.status_code == 201, res.text
    body = res.json()
    tool_id, review_id = body["id"], body["review_id"]
    assert body["status"] == "reviewing" and body["target_id"] is None
    assert body["review"]["state"] == "reviewing"
    assert world.aws.control.targets == {}

    # the creator cannot decide it, a steward can
    self_decide = admin.post(f"/api/gateway-tools/{tool_id}/reviews/{review_id}/approve",
                             json={}, headers={"X-Project": "alpha"})
    assert self_decide.status_code == 409 and self_decide.json()["code"] == (
        "mcp_review.self_decide")
    listed = world.steward.get(f"/api/gateway-tools/{tool_id}/reviews").json()
    assert listed["open_review_id"] == review_id
    detail = world.steward.get(f"/api/gateway-tools/{tool_id}/reviews/{review_id}").json()
    assert detail["tools"][0]["input_schema"]["type"] == "object"
    res = world.steward.post(f"/api/gateway-tools/{tool_id}/reviews/{review_id}/approve",
                             json={"note": "read-only lookup"})
    assert res.status_code == 200, res.text
    [(target_id, target)] = world.aws.control.targets.items()
    assert target["name"] == "crm-tools"
    lam = target["targetConfiguration"]["mcp"]["lambda"]
    assert lam["lambdaArn"] == LAMBDA_ARN and lam["toolSchema"]["inlinePayload"][0]["name"] == (
        "lookup")
    assert target["credentialProviderConfigurations"] == [
        {"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    policy = world.iam.policies[("gw-role", workspace_iam.LAMBDA_TOOLS_POLICY_NAME)]
    assert LAMBDA_ARN in policy
    [row] = world.steward.get("/api/gateway-tools/lambda").json()["targets"]
    assert row["status"] == "live" and row["target_id"] == target_id

    # delete: target, then the role's grant, then the row
    res = admin.delete(f"/api/gateway-tools/lambda/{tool_id}", headers={"X-Project": "alpha"})
    assert res.status_code == 200, res.text
    assert world.aws.control.targets == {}
    assert world.iam.policies == {}
    assert world.steward.get("/api/gateway-tools/lambda").json()["targets"] == []


def test_a_failed_publish_is_recorded_and_withdraws_the_grant(world):
    admin = _admin(world.app)
    body = admin.post("/api/gateway-tools/lambda", json=_lambda_body(),
                      headers={"X-Project": "alpha"}).json()

    def refuse(**_kw):
        raise client_error("ValidationException", "bad schema")

    world.aws.control.create_gateway_target = refuse
    res = world.steward.post(
        f"/api/gateway-tools/{body['id']}/reviews/{body['review_id']}/approve", json={})
    assert res.status_code == 200, res.text
    [row] = world.steward.get("/api/gateway-tools/lambda").json()["targets"]
    assert row["status"] == "failed" and "bad schema" in row["error"]
    assert world.iam.policies == {}
    detail = world.steward.get(f"/api/gateway-tools/{body['id']}/reviews/{body['review_id']}")
    assert detail.json()["publish_status"] == "failed"


def test_lambda_validation_errors(world):
    admin = _admin(world.app)
    headers = {"X-Project": "alpha"}
    res = admin.post("/api/gateway-tools/lambda", headers=headers, json=_lambda_body(
        lambda_arn="arn:aws:lambda:eu-west-1:111122223333:function:crm-tools"))
    assert res.status_code == 422 and res.json()["code"] == "lambda_tool.wrong_environment"
    res = admin.post("/api/gateway-tools/lambda", headers=headers, json=_lambda_body(
        lambda_arn="arn:aws:lambda:us-west-2:111122223333:function:nope"))
    assert res.status_code == 422 and res.json()["code"] == "lambda_tool.function_missing"
    world.aws.control.targets["X1"] = {"name": "crm-tools"}
    res = admin.post("/api/gateway-tools/lambda", headers=headers, json=_lambda_body())
    assert res.status_code == 409 and res.json()["code"] == "lambda_tool.target_name_taken"


def test_a_single_project_workspace_keeps_member_access(app, aws, monkeypatch):
    set_default_resources(RESOURCES)
    dev, _ = _member(app, "dora", "developer", project=TEST_PROJECT)
    _client_router(monkeypatch, {"cognito-idp": aws.cognito, "s3": aws.s3,
                                 "lambda": _Lambda(), "iam": _Iam()})
    monkeypatch.setattr(gateway_tools, "control_client", lambda _ws=None: aws.control)
    res = dev.post("/api/gateway-tools/lambda", json=_lambda_body())
    assert res.status_code == 201, res.text


def test_another_projects_lambda_target_answers_404(world):
    db = SessionLocal()
    with project_context(TEST_PROJECT):
        row = LambdaToolTarget(workspace_id=DEFAULT_WORKSPACE_ID, gateway_id="gw-1",
                               target_name="theirs", lambda_arn=LAMBDA_ARN, tool_schema=SCHEMA)
        db.add(row)
        db.commit()
        tool_id = row.id
    db.close()
    assert world.steward.get(f"/api/gateway-tools/{tool_id}/reviews").status_code == 404
    admin = _admin(world.app)
    res = admin.delete(f"/api/gateway-tools/lambda/{tool_id}", headers={"X-Project": "alpha"})
    assert res.status_code == 404


# ── hand-made MCP targets (§15.15) ────────────────────────────────────────────


def test_external_targets_are_listed_adopted_and_drift_checked(world, monkeypatch):
    world.aws.control.targets["X1"] = {"name": "legacy-crm", "targetConfiguration": {
        "mcp": {"mcpServer": {"endpoint": "https://crm.example.com/mcp"}}}}
    # a hosted server's own target is not "external"
    db = SessionLocal()
    row = db.get(Agent, world.agent_id)
    row.spec = {**row.spec, "mcp": {**row.spec["mcp"], "target_id": "T7"}}
    db.commit()
    db.close()
    world.aws.control.targets["T7"] = {"name": "crm-alpha", "targetConfiguration": {
        "mcp": {"mcpServer": {"endpoint": "https://runtime/mcp"}}}}

    [listed] = world.dev.get("/api/gateway-tools/external").json()["targets"]
    assert listed["target_id"] == "X1" and listed["hosting"] == "external"
    assert listed["tool_id"] is None

    res = world.dev.post("/api/gateway-tools/external/X1/adopt")
    assert res.status_code == 403  # mcp.approve
    res = world.steward.post("/api/gateway-tools/external/X1/adopt")
    assert res.status_code == 403  # two projects: shared infrastructure, admins only
    admin = _admin(world.app)
    res = admin.post("/api/gateway-tools/external/X1/adopt", headers={"X-Project": "alpha"})
    assert res.status_code == 201, res.text
    tool_id = res.json()["tool_id"]
    assert res.json()["review"]["state"] == "approved"
    again = admin.post("/api/gateway-tools/external/X1/adopt", headers={"X-Project": "alpha"})
    assert again.status_code == 409

    [listed] = world.dev.get("/api/gateway-tools/external").json()["targets"]
    assert listed["tool_id"] == tool_id and listed["review"]["state"] == "approved"

    # the scheduler's drift check opens a review, the target is left alone
    monkeypatch.setattr(gateway_tools.mcp_client, "tools_list", lambda _ws: [
        {"name": "legacy-crm___find_order", "description": "now exports everything"}])
    db = SessionLocal()
    try:
        out = scheduler.run_mcp_health(db)
    finally:
        db.close()
    assert out["external"] == [{"tool_id": tool_id, "status": "drift"}]
    assert "X1" in world.aws.control.targets
    reviews = world.steward.get(f"/api/gateway-tools/{tool_id}/reviews").json()
    assert reviews["state"] == "reviewing"
    res = world.steward.post(
        f"/api/gateway-tools/{tool_id}/reviews/{reviews['open_review_id']}/approve", json={})
    assert res.status_code == 200, res.text
    with project_context(None):
        db = SessionLocal()
        assert db.get(ExternalMcpTarget, tool_id).health["status"] == "drift"
        db.close()
