"""Phase 2 isolation, gateway tools (projects design §10.6, R10-gw): the per-project M2M
client and group, the Cedar rule that keeps a project's targets to the project, the
`m2m` / `cedar` provisioner, the credentials an isolated agent deploys with, the
project groups of a member's policy token, and the infra routes lowered once every
project of a workspace is isolated (§10.6-6).

Cedar is not evaluated by AWS here: `_admits` is a small evaluator of the condition
`project_principal_condition` writes (an OR of `getTag == / like` tests), enough to
show that project `a`'s rule admits `a`'s client, scope and group and nothing of
project `a-b`. AWS is stubbed throughout.
"""

import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select, text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import NotFoundError
from app.core.project_scope import current_project, project_context
from app.models.ledger import Job, ResourceCreator
from app.models.project_isolation import (
    KIND_CEDAR_POLICY,
    KIND_M2M_CLIENT,
    KIND_M2M_PROVIDER,
    ProjectResource,
)
from app.models.projects import Project
from app.services import (
    access,
    gateway_bootstrap,
    governance,
    identity_gateway_targets,
    policy_identity,
    project_gateway,
)
from app.services import project_isolation as svc
from tests.conftest import TEST_PROJECT
from tests.test_project_scope import _member_of, _project, app  # noqa: F401
from tests.test_route_policy import (  # noqa: F401
    MEMBER_CREDS,
    gated_app,
    member_session,
)

WS = DEFAULT_WORKSPACE_ID
GW_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/gw-1"
ENGINE_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:policy-engine/eng-1"
RESOURCES = {"user_pool_id": "pool-1", "gateway_id": "gw-1", "kb_gateway_id": "kbgw-1"}


# ── a minimal evaluator of the principal condition ───────────────────────────


_TEST = re.compile(r'principal\.getTag\("([^"]+)"\) (==|like) "((?:[^"\\]|\\.)*)"')


def _like(pattern: str, value: str) -> bool:
    parts = [re.escape(p) for p in pattern.split("*")]
    return re.fullmatch(".*".join(parts), value, flags=re.S) is not None


def _admits(condition: str, tags: dict[str, str]) -> bool:
    """The condition is an OR of `getTag(t) == v` / `like p` tests (each guarded by
    `hasTag`), so the principal is admitted when any one test holds."""
    for tag, op, raw in _TEST.findall(condition):
        value = raw.replace('\\"', '"').replace("\\\\", "\\")
        if tag not in tags:
            continue
        if (op == "==" and tags[tag] == value) or (op == "like" and _like(value, tags[tag])):
            return True
    return False


def test_policy_names_fit_and_stay_distinct():
    assert governance.project_policy_name("risk-desk", "crm-lookup") == (
        "lp_proj_risk_desk_crm_lookup")
    long_pid = "p" * 32
    a = governance.project_policy_name(long_pid, "target-one")
    b = governance.project_policy_name(long_pid, "target-two")
    c = governance.project_policy_name("alpha", "x" * 60 + "1")
    d = governance.project_policy_name("alpha", "x" * 60 + "2")
    for name in (a, b, c, d):
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", name), name
    assert a != b and c != d


def test_the_forbid_admits_only_the_project():
    gateway = {"gatewayArn": GW_ARN, "authorizerType": "CUSTOM_JWT"}
    statement = governance.project_forbid_statement(
        gateway, "crm-lookup", ["search", "get"], project_id="a", client_id="client-a",
        scope=project_gateway.project_scope("a"),
    )
    assert statement.startswith("forbid(") and ") unless {" in statement
    assert 'AgentCore::Action::"crm-lookup___get", AgentCore::Action::"crm-lookup___search"' \
        in statement
    assert f'resource == AgentCore::Gateway::"{GW_ARN}"' in statement
    cond = statement.split("unless {", 1)[1]
    # project a's agent: by client id, or by scope should client_id not be a tag
    assert _admits(cond, {"client_id": "client-a"})
    assert _admits(cond, {"scope": "launchpad-gw/invoke launchpad-gw/project.a"})
    # a's members, however the group list is rendered
    for groups in ("lp-project-a", "hr-analyst,lp-project-a", '["lp-project-a","x"]',
                   "lp-project-a hr-analyst"):
        assert _admits(cond, {"cognito:groups": groups}), groups
    # nobody of project a-b (whose names extend a's), nor the workspace client
    assert not _admits(cond, {"client_id": "client-a-b"})
    assert not _admits(cond, {"scope": "launchpad-gw/invoke launchpad-gw/project.a-b"})
    assert not _admits(cond, {"cognito:groups": "hr-analyst,lp-project-a-b"})
    assert not _admits(cond, {"scope": "launchpad-gw/invoke", "client_id": "shared-m2m"})


def test_an_isolated_projects_mcp_permit_names_its_principal():
    gateway = {"gatewayArn": GW_ARN, "authorizerType": "CUSTOM_JWT"}
    shared = governance.mcp_project_statement(gateway, "srv", ["t"], project_id="a")
    assert 'like "*lp-project-a*"' in shared  # unchanged for a shared project
    isolated = governance.mcp_project_statement(
        gateway, "srv", ["t"], project_id="a", client_id="client-a",
        scope=project_gateway.project_scope("a"))
    assert isolated.startswith("permit(") and _admits(isolated, {"client_id": "client-a"})


def test_policy_steps_create_update_keep_and_delete():
    control = MagicMock()
    control.list_policies.return_value = {"policies": [
        {"name": "lp_proj_a_keep", "policyId": "p1"},
        {"name": "lp_proj_a_change", "policyId": "p2"},
        {"name": "lp_proj_a_gone", "policyId": "p3"},
    ]}
    statements = {"p1": "S1", "p2": "OLD", "p3": "S3"}
    control.get_policy.side_effect = lambda **kw: {
        "policyId": kw["policyId"],
        "definition": {"cedar": {"statement": statements[kw["policyId"]]}},
    }
    steps = governance.policy_steps(control, "eng-1", {
        "lp_proj_a_keep": "S1", "lp_proj_a_change": "NEW", "lp_proj_a_gone": None,
        "lp_proj_a_new": "S4", "lp_proj_a_never": None,
    })
    assert {s["name"]: s["action"] for s in steps} == {
        "lp_proj_a_keep": "keep", "lp_proj_a_change": "update", "lp_proj_a_gone": "delete",
        "lp_proj_a_new": "create",
    }


# ── fakes ─────────────────────────────────────────────────────────────────────


def _code_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


class _Cognito:
    def __init__(self):
        self.scopes = [{"ScopeName": "invoke", "ScopeDescription": "x"}]
        self.groups: set[str] = set()
        self.clients: dict[str, dict[str, Any]] = {}
        self.creates = 0

    def describe_resource_server(self, UserPoolId, Identifier):
        return {"ResourceServer": {"Identifier": Identifier, "Name": Identifier,
                                   "Scopes": list(self.scopes)}}

    def update_resource_server(self, UserPoolId, Identifier, Name, Scopes):
        self.scopes = list(Scopes)

    def create_group(self, UserPoolId, GroupName, Description):
        if GroupName in self.groups:
            raise _code_error("GroupExistsException")
        self.groups.add(GroupName)

    def list_user_pool_clients(self, **kw):
        return {"UserPoolClients": [{"ClientName": c["ClientName"], "ClientId": cid}
                                    for cid, c in self.clients.items()]}

    def create_user_pool_client(self, **kw):
        self.creates += 1
        cid = f"client-{len(self.clients) + 1}"
        self.clients[cid] = {**kw, "ClientId": cid, "ClientSecret": "s3cret"}
        return {"UserPoolClient": self.clients[cid]}

    def describe_user_pool_client(self, UserPoolId, ClientId):
        return {"UserPoolClient": self.clients[ClientId]}

    def update_user_pool_client(self, **kw):
        self.clients[kw["ClientId"]].update(kw)


class _Control:
    def __init__(self, mode: str | None = "ENFORCE"):
        self.providers: dict[str, str] = {}
        self.allowed: dict[str, list[str]] = {"gw-1": ["console"], "kbgw-1": ["console"]}
        self.mode = mode
        self.targets: dict[str, dict[str, Any]] = {}
        self.updates: list[dict[str, Any]] = []

    def list_oauth2_credential_providers(self, **kw):
        return {"credentialProviders": [{"name": n, "credentialProviderArn": a}
                                        for n, a in self.providers.items()]}

    def create_oauth2_credential_provider(self, name, **kw):
        arn = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:token-vault/default/"
               f"oauth2credentialprovider/{name}")
        self.providers[name] = arn
        return {"credentialProviderArn": arn}

    def get_gateway(self, gatewayIdentifier):
        out = {
            "gatewayId": gatewayIdentifier, "gatewayArn": GW_ARN, "name": gatewayIdentifier,
            "roleArn": "arn:role", "protocolType": "MCP", "authorizerType": "CUSTOM_JWT",
            "status": "READY",
            "authorizerConfiguration": {"customJWTAuthorizer": {
                "discoveryUrl": "https://x",
                "allowedClients": list(self.allowed[gatewayIdentifier]),
            }},
        }
        if self.mode:
            out["policyEngineConfiguration"] = {"arn": ENGINE_ARN, "mode": self.mode}
        return out

    def update_gateway(self, **kw):
        self.updates.append(kw)
        self.allowed[kw["gatewayIdentifier"]] = list(
            kw["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"])

    def list_gateway_targets(self, **kw):
        return {"items": [{"targetId": tid, "name": t["name"]} for tid, t in self.targets.items()]}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return self.targets[targetId]


class _Workspace:
    def __init__(self, cognito=None, control=None, resources=None):
        self.id = WS
        self.region = "us-west-2"
        self.resources = dict(RESOURCES if resources is None else resources)
        self.cognito = cognito or _Cognito()
        self.control = control or _Control()

    def client(self, service, **_):
        return {"cognito-idp": self.cognito, "bedrock-agentcore-control": self.control,
                "iam": MagicMock()}[service]


def _openapi_target(tid: str, name: str, ops: list[str]) -> dict[str, Any]:
    import json

    doc = {"openapi": "3.0.0", "paths": {f"/{op}": {"get": {"operationId": op}} for op in ops}}
    return {"targetId": tid, "name": name, "status": "READY",
            "targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": json.dumps(doc)}}}}


def _set_mode(pid: str, mode: str) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(Project, pid)
            row.settings = {**(row.settings or {}), "isolation": {"mode": mode}}
            db.commit()
    finally:
        db.close()


def _own(target_id: str, pid: str) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(ResourceCreator(workspace_id=WS, kind=project_gateway.TARGET_KIND,
                                   resource_id=target_id))
            db.commit()
    finally:
        db.close()


def _rows(pid: str) -> dict[str, ProjectResource]:
    db = SessionLocal()
    try:
        rows = db.scalars(select(ProjectResource).where(ProjectResource.project_id == pid))
        out = {r.kind: r for r in rows}
        for r in out.values():
            db.expunge(r)
        return out
    finally:
        db.close()


class _Scaffold:
    """Stands in for the governance operations: records the plan it was handed."""

    def __init__(self, live: dict[str, str] | None = None):
        self.live = dict(live or {})
        self.applied: list[dict[str, Any]] = []

    def plan(self, control, gateway_id, wanted):
        steps = []
        for name, statement in wanted.items():
            if statement is None:
                if name in self.live:
                    steps.append({"name": name, "action": "delete"})
            elif name not in self.live:
                steps.append({"name": name, "action": "create", "statement": statement})
            elif self.live[name] != statement:
                steps.append({"name": name, "action": "update", "statement": statement})
        return {"gateway_id": gateway_id, "engine_id": "eng-1", "engine_arn": ENGINE_ARN,
                "mode": "ENFORCE", "steps": steps, "acknowledged_gateway_ids": [gateway_id]}

    def apply(self, workspace, gateway_id, steps, **kw):
        self.applied.extend(steps)
        for step in steps:
            if step["action"] == "delete":
                self.live.pop(step["name"], None)
            else:
                self.live[step["name"]] = step["statement"]
        return [{"name": s["name"], "action": s["action"], "status": "succeeded", "error": None}
                for s in steps]


@pytest.fixture
def scaffold(monkeypatch):
    fake = _Scaffold()
    monkeypatch.setattr(governance, "project_cedar_plan", fake.plan)
    monkeypatch.setattr(governance, "apply_mcp_scaffold", fake.apply)
    return fake


@pytest.fixture
def alpha(app):  # noqa: F811
    _project("alpha")
    _set_mode("alpha", svc.MODE_ISOLATED)
    return "alpha"


def _queue(pid: str = "alpha") -> str:
    db = SessionLocal()
    try:
        job, _ = svc.queue_provision(db, pid, WS, actor="admin")
        return job.id
    finally:
        db.close()


def _provisioners() -> svc.Provisioners:
    class NoQuota:
        def check(self, ctx):
            return []

    return svc.Provisioners(iam=svc._PENDING, memory=svc._PENDING, kb=svc._PENDING,
                            gateway=project_gateway.ProjectGatewayProvisioner(),
                            storage=svc._PENDING, quotas=NoQuota())


def _stages(job_id: str) -> dict[str, dict[str, Any]]:
    db = SessionLocal()
    try:
        with project_context(None):
            return {s["name"]: s for s in svc.job_stages(db.get(Job, job_id))}
    finally:
        db.close()


# ── the provisioner ───────────────────────────────────────────────────────────


def test_the_registered_gateway_provisioner_is_the_real_one():
    assert isinstance(svc.get_provisioners().gateway, project_gateway.ProjectGatewayProvisioner)


def test_m2m_and_cedar_stages_provision_the_project_principal(alpha, scaffold):
    aws = _Workspace()
    aws.control.targets = {
        "t-alpha": _openapi_target("t-alpha", "crm-lookup", ["search", "get"]),
        "t-other": _openapi_target("t-other", "office-facts", ["facts"]),
    }
    _own("t-alpha", "alpha")
    job_id = _queue()
    svc.execute_provision_job(job_id, _provisioners(), workspace_factory=lambda _ws: aws)
    stages = _stages(job_id)
    assert stages["m2m"]["status"] == "succeeded", stages["m2m"]
    assert stages["cedar"]["status"] == "succeeded", stages["cedar"]

    names = svc.resource_names("alpha")
    (cid, client), = aws.cognito.clients.items()
    assert client["ClientName"] == names.m2m_client
    assert client["AllowedOAuthScopes"] == ["launchpad-gw/invoke", "launchpad-gw/project.alpha"]
    assert {"ScopeName": "project.alpha"}.items() <= aws.cognito.scopes[-1].items()
    assert aws.cognito.groups == {"lp-project-alpha"}
    assert set(aws.control.providers) == {names.m2m_provider}
    # the client may call both gateways; the update keeps the policy engine attached
    assert cid in aws.control.allowed["gw-1"] and cid in aws.control.allowed["kbgw-1"]
    assert all(u["policyEngineConfiguration"]["arn"] == ENGINE_ARN for u in aws.control.updates)

    # one forbid, for alpha's own target only
    assert [s["name"] for s in scaffold.applied] == ["lp_proj_alpha_crm_lookup"]
    assert '"crm-lookup___search"' in scaffold.applied[0]["statement"]
    assert f'== "{cid}"' in scaffold.applied[0]["statement"]

    rows = _rows("alpha")
    assert {rows[k].status for k in (KIND_M2M_CLIENT, KIND_M2M_PROVIDER, KIND_CEDAR_POLICY)} \
        == {"ready"}
    assert rows[KIND_M2M_CLIENT].resource_id == cid
    assert rows[KIND_CEDAR_POLICY].detail["policies"] == ["lp_proj_alpha_crm_lookup"]

    # a re-run adopts everything and changes nothing
    aws.cognito.creates = 0
    updates = len(aws.control.updates)
    job2 = _queue()
    svc.execute_provision_job(job2, _provisioners(), workspace_factory=lambda _ws: aws)
    assert _stages(job2)["cedar"]["status"] == "succeeded"
    assert aws.cognito.creates == 0 and len(aws.control.updates) == updates
    assert len(scaffold.applied) == 1


def test_cedar_needs_an_enforcing_policy_engine(alpha, scaffold):
    for mode, phrase in ((None, "no Policy Engine"), ("LOG_ONLY", "ENFORCE")):
        aws = _Workspace(control=_Control(mode=mode))
        job_id = _queue()
        svc.execute_provision_job(job_id, _provisioners(),
                                  workspace_factory=lambda _ws, aws=aws: aws)
        cedar = _stages(job_id)["cedar"]
        assert cedar["status"] == "failed" and phrase in cedar["detail"], cedar
    assert scaffold.applied == []


def test_a_removed_target_loses_its_rule_and_an_mcp_target_is_uncovered(alpha, scaffold):
    aws = _Workspace()
    aws.control.targets = {"t1": _openapi_target("t1", "crm", ["search"])}
    _own("t1", "alpha")
    svc.execute_provision_job(_queue(), _provisioners(), workspace_factory=lambda _ws: aws)
    assert set(scaffold.live) == {"lp_proj_alpha_crm"}

    aws.control.targets = {"t2": {"targetId": "t2", "name": "remote", "targetConfiguration": {
        "mcp": {"mcpServer": {"endpoint": "https://mcp.example"}}}}}
    _own("t2", "alpha")
    job_id = _queue()
    svc.execute_provision_job(job_id, _provisioners(), workspace_factory=lambda _ws: aws)
    assert scaffold.live == {}
    assert "no tool list yet: remote" in _stages(job_id)["cedar"]["detail"]
    assert _rows("alpha")[KIND_CEDAR_POLICY].detail["uncovered"] == ["remote"]


def test_the_oauth_provider_lookup_pages():
    control = MagicMock()
    control.list_oauth2_credential_providers.side_effect = [
        {"credentialProviders": [{"name": "x", "credentialProviderArn": "arn:x"}],
         "nextToken": "n"},
        {"credentialProviders": [{"name": "launchpad-gw-m2m-a", "credentialProviderArn": "arn:a"}]},
    ]
    assert gateway_bootstrap.find_oauth_provider(control, "launchpad-gw-m2m-a") == "arn:a"


# ── ready rows for the read paths ─────────────────────────────────────────────


def _ready(pid: str, client_id: str | None = None, *, cedar: str = "ready") -> None:
    names = svc.resource_names(pid)
    db = SessionLocal()
    try:
        for kind, rid, arn, status in (
            (KIND_M2M_CLIENT, client_id or f"client-{pid}", None, "ready"),
            (KIND_M2M_PROVIDER, names.m2m_provider, f"arn:provider/{names.m2m_provider}", "ready"),
            (KIND_CEDAR_POLICY, "eng-1", ENGINE_ARN, cedar),
        ):
            db.merge(ProjectResource(project_id=pid, workspace_id=WS, kind=kind,
                                     resource_id=rid, arn=arn, status=status,
                                     detail={"policies": []}))
        db.commit()
    finally:
        db.close()


def test_isolated_agents_deploy_with_the_project_credentials(alpha):
    base = {"oauth_provider_arn": "arn:provider/launchpad-gw-m2m", "gateway_id": "gw-1"}
    # not provisioned yet, or no project: the workspace credentials, the same map
    assert project_gateway.effective_resources(base, "alpha", WS) is base
    assert project_gateway.effective_resources(base, None, WS) is base
    assert project_gateway.gateway_scopes(base) == ["launchpad-gw/invoke"]
    _ready("alpha")
    out = project_gateway.effective_resources(base, "alpha", WS)
    assert out["oauth_provider_arn"] == "arn:provider/launchpad-gw-m2m-alpha"
    assert project_gateway.gateway_scopes(out) == [
        "launchpad-gw/invoke", "launchpad-gw/project.alpha"]
    # switching isolation off returns the agents to the workspace client
    _set_mode("alpha", svc.MODE_SHARED)
    assert project_gateway.effective_resources(base, "alpha", WS) is base


def test_members_policy_tokens_carry_their_isolated_projects(alpha):
    from app.models.ledger import User

    db = SessionLocal()
    try:
        user = User(username="dana", username_key="dana", email="dana@acme-corp.com",
                    password_hash="x", role="member", status="active")
        db.add(user)
        db.commit()
        uid = user.id
    finally:
        db.close()
    _project("beta")
    _set_mode("beta", svc.MODE_ISOLATED)
    _member_of("alpha", uid)
    _member_of("beta", uid)
    _ready("alpha")  # beta is isolated but not provisioned: its group does not exist yet
    db = SessionLocal()
    try:
        assert project_gateway.project_groups_for(db, "Dana", "member", WS) == [
            "lp-project-alpha"]
        assert project_gateway.project_groups_for(db, "nobody", "member", WS) == []
        with project_context("alpha"):  # an administrator carries the current project
            assert project_gateway.project_groups_for(db, "root", "admin", WS) == [
                "lp-project-alpha"]
    finally:
        db.close()


def test_the_policy_token_reconciles_project_groups():
    cognito = MagicMock()
    cognito.admin_list_groups_for_user.return_value = {"Groups": [
        {"GroupName": "hr-analyst"}, {"GroupName": "lp-project-old"}, {"GroupName": "other"}]}
    policy_identity._reconcile_group(cognito, pool_id="p", username="dana", role="member",
                                     project_groups=frozenset({"lp-project-alpha"}))
    removed = {c.kwargs["GroupName"] for c in cognito.admin_remove_user_from_group.call_args_list}
    added = {c.kwargs["GroupName"] for c in cognito.admin_add_user_to_group.call_args_list}
    assert removed == {"lp-project-old"} and added == {"lp-project-alpha", "lp-role-member"}


# ── targets: ownership, the rule after a change, another project's 404 ───────


def test_a_created_target_is_the_projects_and_gets_its_rule(alpha, monkeypatch):
    _project("beta")
    scheduled: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(project_gateway, "schedule_reconcile",
                        lambda pid, ws, wait_target=None: scheduled.append((pid, ws, wait_target)))
    with project_context("alpha"):
        identity_gateway_targets._owned(WS, "t-new", None)
    db = SessionLocal()
    try:
        assert project_gateway.owned_target_ids(db, WS, "alpha") == {"t-new"}
        assert project_gateway.target_owner(db, "t-new") == (WS, "alpha")
    finally:
        db.close()
    assert scheduled == [("alpha", WS, "t-new")]

    # beta may not delete it; alpha may, and the rule is rewritten
    control = MagicMock()
    control.get_gateway_target.return_value = {"name": "crm", "credentialProviderConfigurations": [
        {"credentialProviderType": "API_KEY", "credentialProvider": {"apiKeyCredentialProvider": {
            "providerArn": "arn:x/apikeycredentialprovider/crm-key"}}}]}
    with project_context("beta"), pytest.raises(NotFoundError):
        identity_gateway_targets.delete_target(control, {"gateway_id": "gw-1"}, "t-new")
    control.delete_gateway_target.assert_not_called()
    with project_context("alpha"):
        identity_gateway_targets.delete_target(control, {"gateway_id": "gw-1"}, "t-new")
    control.delete_gateway_target.assert_called_once()
    db = SessionLocal()
    try:
        assert project_gateway.target_owner(db, "t-new") is None
    finally:
        db.close()
    assert scheduled[-1] == ("alpha", WS, None)


def test_a_failed_reconcile_fails_closed(alpha, monkeypatch):
    _ready("alpha")

    def boom(*a, **kw):
        raise svc.ProvisionError("the workspace gateway runs Cedar in LOG_ONLY mode")

    monkeypatch.setattr(project_gateway, "reconcile_cedar", boom)
    assert project_gateway.reconcile_now("alpha", WS, workspace=_Workspace()) is False
    row = _rows("alpha")[KIND_CEDAR_POLICY]
    assert row.status == "failed" and "LOG_ONLY" in row.error
    db = SessionLocal()
    try:
        assert not project_gateway.cedar_active(db, "alpha", WS)
    finally:
        db.close()

    calls: list[str | None] = []
    monkeypatch.setattr(project_gateway, "reconcile_cedar", lambda ws, pid, **kw: (
        calls.append(current_project()) or {"gateway_id": "gw-1", "engine_id": "eng-1",
                                             "engine_arn": ENGINE_ARN, "mode": "ENFORCE",
                                             "policies": [], "uncovered": [], "principal": {}}))
    assert project_gateway.reconcile_now("alpha", WS, workspace=_Workspace()) is True
    assert calls == ["alpha"]  # runs inside the row's project
    assert _rows("alpha")[KIND_CEDAR_POLICY].status == "ready"


# ── §10.6-6: lowered infra routes ─────────────────────────────────────────────


def _member_role(pid: str, role_key: str = "developer") -> None:
    db = SessionLocal()
    try:
        uid = db.execute(text("SELECT id FROM users WHERE username_key = :u"),
                         {"u": MEMBER_CREDS["username"]}).scalar_one()
        db.execute(text("DELETE FROM project_members WHERE user_id = :u AND project_id = :p"),
                   {"u": uid, "p": pid})
        db.execute(text("INSERT INTO project_members (project_id, user_id, role_key, added_at)"
                        " VALUES (:p, :u, :r, '2026-10-14 00:00:00')"),
                   {"p": pid, "u": uid, "r": role_key})
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def two_projects(member_session, monkeypatch):  # noqa: F811
    from app.routers import identity as identity_router

    _project("alpha")
    _member_role("alpha")
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx: object())
    monkeypatch.setattr(identity_router.gateway_targets, "create_target",
                        lambda *a, **kw: calls.append(("create", current_project()))
                        or {"target_id": "t"})
    monkeypatch.setattr(identity_router.gateway_targets, "delete_target",
                        lambda c, r, tid, *a, **kw: calls.append(("delete", tid)))
    return SimpleNamespace(client=member_session, calls=calls)


_BODY = {"name": "crm", "source": "openapi", "openapi_schema": "{}", "connection": "crm-key",
         "kind": "api_key", "mode": "as_agent"}


def test_target_routes_stay_admin_only_until_every_project_is_isolated(two_projects):
    headers = {"X-Project": "alpha"}
    response = two_projects.client.post("/api/identity/gateway-targets", json=_BODY,
                                        headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "auth.infra_admin_required"
    # alpha isolated with Cedar in force, but the default project is still shared
    _set_mode("alpha", svc.MODE_ISOLATED)
    _ready("alpha")
    response = two_projects.client.post("/api/identity/gateway-targets", json=_BODY,
                                        headers=headers)
    assert response.status_code == 403, response.text
    assert two_projects.calls == []


def test_target_routes_are_the_projects_once_cedar_is_per_project(two_projects):
    for pid in ("alpha", TEST_PROJECT):
        _set_mode(pid, svc.MODE_ISOLATED)
        _ready(pid)
    _own("t-alpha", "alpha")
    headers = {"X-Project": "alpha"}
    response = two_projects.client.post("/api/identity/gateway-targets", json=_BODY,
                                        headers=headers)
    assert response.status_code == 201, response.text
    assert two_projects.calls == [("create", "alpha")]
    ok = two_projects.client.delete("/api/identity/gateway-targets/t-alpha", headers=headers)
    assert ok.status_code == 200, ok.text
    # an unrecorded target is the workspace default project's: not found from alpha
    other = two_projects.client.delete("/api/identity/gateway-targets/t-default",
                                       headers=headers)
    assert other.status_code == 404, other.text
    assert two_projects.calls == [("create", "alpha"), ("delete", "t-alpha")]
    # the project's own key decides: a viewer of alpha is refused
    _member_role("alpha", "viewer")
    response = two_projects.client.post("/api/identity/gateway-targets", json=_BODY,
                                        headers=headers)
    assert response.status_code == 403, response.text
    # and a failed reconcile (rules not in force) turns the lowering off again
    _member_role("alpha")
    _ready("alpha", cedar="failed")
    response = two_projects.client.post("/api/identity/gateway-targets", json=_BODY,
                                        headers=headers)
    assert response.json()["code"] == "auth.infra_admin_required"

