"""Hosted MCP servers (protocol="mcp" on byoc): spec rules, the deploy stages'
inbound / Gateway wiring, the server-owned ids, the guards that keep an MCP
server out of agent-only paths, and the reverse teardown — AWS stubbed."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer import byoc as byoc_dep
from app.dlc import releases
from app.models.ledger import Agent
from app.routers import agents as agents_router
from app.schemas.agent import AgentSpec
from app.services import agent_iam, mcp_servers, runtime_discovery
from app.services.agentcore import runtime as rt
from tests.conftest import client_error, set_default_resources, ws_ctx
from tests.test_byoc import (
    ECR_IMAGE,
    SDK_MAIN,
    StubRuntimeControl,
    StubS3,
    _client_router,
    _get_agent,
    _mk_agent,
    _seed_upload,
    _stage_ctx,
)

RESOURCES = {
    "artifacts_bucket": "bkt",
    "execution_role_arn": "arn:role",
    "codebuild_project": "launchpad-agent-builder",
    "ecr_repo": "launchpad-agents",
    "user_pool_id": "us-west-2_POOL",
    "gateway_id": "gw-1",
}
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/crm-abc"


def _mcp_spec(**over) -> dict:
    return {
        "name": "crm-mcp",
        "method": "byoc",
        "protocol": "mcp",
        "byoc": {"artifact_kind": "code_zip", "upload_id": "u1"},
        "mcp": {"target_name": "crm-lookup"},
        **over,
    }


# ── spec rules ────────────────────────────────────────────────────────────────


def test_mcp_spec_forces_memory_off_and_keeps_the_target_name():
    spec = AgentSpec(**_mcp_spec())
    assert spec.protocol == "mcp"
    assert spec.memory.short_term is False and spec.memory.long_term is False
    assert spec.mcp.target_name == "crm-lookup"


@pytest.mark.parametrize(
    "over, message",
    [
        ({"mcp": None}, "requires the mcp settings block"),
        ({"method": "zip_runtime", "byoc": None, "system_prompt": "x"}, "byoc method only"),
        ({"tools": [{"type": "rest", "name": "t", "auth": {"connection": "c", "kind": "oauth2"}}]},
         "protocol=mcp"),
        ({"inbound_auth": {"mode": "iam"}}, "platform-managed JWT"),
        ({"mcp": {"target_name": "bad_name"}}, "target_name"),
        ({"mcp": {"target_name": "trailing-"}}, "target_name"),
    ],
)
def test_mcp_spec_refusals(over, message):
    with pytest.raises(ValidationError) as err:
        AgentSpec(**_mcp_spec(**over))
    assert message in str(err.value)


def test_mcp_block_requires_the_mcp_protocol():
    with pytest.raises(ValidationError) as err:
        AgentSpec(**_mcp_spec(protocol="http"))
    assert "mcp settings require protocol='mcp'" in str(err.value)


def test_mcp_server_role_is_granted_no_model():
    spec = AgentSpec(**_mcp_spec())
    ctx = agent_iam.role_context(ws_ctx(RESOURCES))
    sids = {s["Sid"] for s in agent_iam.policy_document(spec, ctx, inbound_jwt=True)["Statement"]}
    assert "BedrockModels" not in sids
    assert "InboundJwtWorkloadToken" in sids


def test_mcp_server_is_not_invocable_or_gateable():
    agent = Agent(method="byoc", status="active", arn=RUNTIME_ARN, resource_id="rt-1",
                  version="1", spec=AgentSpec(**_mcp_spec()).model_dump())
    capability = runtime_discovery.invoke_capability(agent)
    assert capability["eligible"] is False
    assert capability["reason_code"] == "not-agent-protocol"
    ok, why = releases.gateable(agent)
    assert ok is False and "Gateway" in why


def test_server_owned_ids_are_cleared_on_create_and_restored_on_republish():
    sent = AgentSpec(**_mcp_spec(mcp={"target_name": "crm-lookup", "target_id": "FOREIGN",
                                      "client_id": "evil", "provider_arn": "arn:evil"}))
    created = agents_router._server_owned_mcp(sent, None)
    assert (created.mcp.target_id, created.mcp.client_id, created.mcp.provider_arn) == (
        None, None, None,
    )
    stored = {"mcp": {"target_name": "crm-lookup", "target_id": "T1", "client_id": "C1",
                      "provider_arn": "arn:p"}}
    republished = agents_router._server_owned_mcp(sent, stored)
    assert (republished.mcp.target_id, republished.mcp.client_id) == ("T1", "C1")


# ── AWS stubs ─────────────────────────────────────────────────────────────────


class StubCognito:
    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.deleted: list[str] = []

    def list_user_pool_clients(self, **kwargs):
        return {"UserPoolClients": [
            {"ClientId": cid, "ClientName": c["ClientName"]} for cid, c in self.clients.items()
        ]}

    def create_user_pool_client(self, **kwargs):
        cid = f"client-{len(self.clients) + 1}"
        self.clients[cid] = {**kwargs, "ClientSecret": "s3cr3t"}
        return {"UserPoolClient": {"ClientId": cid}}

    def describe_user_pool_client(self, UserPoolId, ClientId):
        return {"UserPoolClient": {"ClientId": ClientId, **self.clients[ClientId]}}

    def delete_user_pool_client(self, UserPoolId, ClientId):
        self.deleted.append(ClientId)
        self.clients.pop(ClientId, None)


class StubGatewayControl(StubRuntimeControl):
    def __init__(self):
        super().__init__()
        self.providers: dict[str, dict] = {}
        self.targets: dict[str, dict] = {}
        self.calls: list[str] = []

    def list_oauth2_credential_providers(self, **kwargs):
        return {"credentialProviders": [
            {"name": n, "credentialProviderArn": p["arn"]} for n, p in self.providers.items()
        ]}

    def create_oauth2_credential_provider(self, name, **kwargs):
        self.calls.append(f"create_provider:{name}")
        self.providers[name] = {"arn": f"arn:provider/{name}", **kwargs}
        return {"credentialProviderArn": f"arn:provider/{name}"}

    def delete_oauth2_credential_provider(self, name):
        self.calls.append(f"delete_provider:{name}")
        if name not in self.providers:
            raise client_error("ResourceNotFoundException")
        del self.providers[name]

    def list_gateway_targets(self, **kwargs):
        return {"items": [{"targetId": tid} for tid in self.targets]}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        if targetId not in self.targets:
            raise client_error("ResourceNotFoundException")
        return {"targetId": targetId, "status": "READY", **self.targets[targetId]}

    def create_gateway_target(self, **kwargs):
        tid = f"T{len(self.targets) + 1}"
        self.calls.append(f"create_target:{kwargs['name']}")
        self.targets[tid] = {"name": kwargs["name"],
                             "targetConfiguration": kwargs["targetConfiguration"],
                             "credentialProviderConfigurations":
                                 kwargs["credentialProviderConfigurations"]}
        return {"targetId": tid, "status": "CREATING"}

    def synchronize_gateway_targets(self, gatewayIdentifier, targetIdList):
        self.calls.append(f"sync:{targetIdList[0]}")
        return {"targets": [self.get_gateway_target(gatewayIdentifier, targetIdList[0])]}

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.calls.append(f"delete_target:{targetId}")
        self.targets.pop(targetId, None)

    def delete_agent_runtime(self, agentRuntimeId):
        self.calls.append(f"delete_runtime:{agentRuntimeId}")
        super().delete_agent_runtime(agentRuntimeId)


@pytest.fixture
def aws(monkeypatch):
    control = StubGatewayControl()
    cognito = StubCognito()
    s3 = StubS3()
    _client_router(monkeypatch, {"cognito-idp": cognito, "s3": s3})
    monkeypatch.setattr(mcp_servers, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(mcp_servers.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        mcp_servers.mcp_client, "tools_list",
        lambda _ws: [{"name": "crm-lookup___find_order", "description": "Find"},
                     {"name": "other___x"}],
    )
    from app.dlc import releases as releases_mod

    monkeypatch.setattr(releases_mod, "delete_endpoints", lambda agent, client: None)
    return SimpleNamespace(control=control, cognito=cognito, s3=s3)


def _active(agent_id: str) -> Agent:
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    row.arn, row.resource_id, row.version = RUNTIME_ARN, "rt-1", "1"
    db.commit()
    db.refresh(row)
    db.close()
    return _get_agent(agent_id)


# ── deploy stages ─────────────────────────────────────────────────────────────


def test_generate_stamps_the_servers_own_client_and_jwt_inbound(aws):
    _seed_upload(aws.s3, "u1", {"main.py": SDK_MAIN})
    agent_id, dep_id = _mk_agent(AgentSpec(**_mcp_spec()))
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))

    byoc_dep._stage_generate(ctx, _get_agent(agent_id))
    byoc_dep._stage_generate(ctx, _get_agent(agent_id))  # idempotent: one client

    spec = _get_agent(agent_id).spec
    [(client_id, client)] = aws.cognito.clients.items()
    assert client["ClientName"] == f"lp-mcp-{agent_id[:12]}"
    assert client["AllowedOAuthFlows"] == ["client_credentials"]
    assert spec["mcp"]["client_id"] == client_id
    assert spec["inbound_auth"]["mode"] == "jwt"
    assert spec["inbound_auth"]["jwt"]["allowed_clients"] == [client_id]
    assert spec["inbound_auth"]["jwt"]["discovery_url"] == (
        "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_POOL"
        "/.well-known/openid-configuration"
    )


@pytest.mark.parametrize("kind", ["code_zip", "container_image"])
def test_deploy_sends_the_mcp_protocol_and_no_model_env(aws, kind):
    byoc = ({"artifact_kind": "code_zip", "upload_id": "u1"} if kind == "code_zip"
            else {"artifact_kind": "container_image", "image_uri": ECR_IMAGE})
    spec = AgentSpec(**_mcp_spec(byoc=byoc))
    spec.mcp.client_id = "client-1"
    spec.inbound_auth = mcp_servers.inbound_auth(ws_ctx(RESOURCES), "client-1")
    agent_id, dep_id = _mk_agent(AgentSpec(**spec.model_dump()))
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    ctx.scratch.update({"s3_bucket": "bkt", "s3_key": "k", "execution_role_arn": "arn:r"})

    byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    sent = aws.control.created_with
    assert sent["protocolConfiguration"] == {"serverProtocol": "MCP"}
    assert sent["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == [
        "client-1"
    ]
    assert "MODEL_ID" not in (sent.get("environmentVariables") or {})


def test_update_container_runtime_echoes_the_protocol():
    stub = StubRuntimeControl()
    rt.update_container_runtime(stub, runtime_id="rt-1", container_uri=ECR_IMAGE,
                                role_arn="arn:r", protocol="mcp")
    assert stub.updated_with["protocolConfiguration"] == {"serverProtocol": "MCP"}


def _registered(aws) -> str:
    spec = AgentSpec(**_mcp_spec())
    spec.mcp.client_id = "client-1"
    agent_id, dep_id = _mk_agent(spec)
    aws.cognito.clients["client-1"] = {
        "ClientName": f"lp-mcp-{agent_id[:12]}", "ClientSecret": "s",
    }
    _active(agent_id)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    result = byoc_dep._stage_register(ctx, _get_agent(agent_id))
    assert "crm-lookup created" in result.detail and "1 tool(s)" in result.detail
    return agent_id


def test_register_wires_provider_and_gateway_target(aws):
    agent_id = _registered(aws)
    name = f"lp-mcp-{agent_id[:12]}"
    provider = aws.control.providers[name]
    assert provider["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]["clientId"] == (
        "client-1"
    )
    [(target_id, target)] = aws.control.targets.items()
    assert target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == (
        rt.bearer_invoke_url("us-west-2", RUNTIME_ARN, "DEFAULT")
    )
    oauth = target["credentialProviderConfigurations"][0]["credentialProvider"][
        "oauthCredentialProvider"
    ]
    assert oauth == {"providerArn": f"arn:provider/{name}", "scopes": ["launchpad-gw/invoke"],
                     "grantType": "CLIENT_CREDENTIALS"}
    stored = _get_agent(agent_id).spec["mcp"]
    assert stored["target_id"] == target_id
    assert stored["provider_arn"] == f"arn:provider/{name}"


def test_republish_resyncs_the_same_target(aws):
    agent_id = _registered(aws)
    ctx = _stage_ctx(agent_id, "d", ws_ctx(RESOURCES))
    result = byoc_dep._stage_register(ctx, _get_agent(agent_id))
    assert "synchronized" in result.detail
    assert len(aws.control.targets) == 1
    assert aws.control.calls[-1] == "sync:T1"


def test_a_foreign_target_with_the_same_name_is_refused(aws):
    aws.control.targets["X1"] = {
        "name": "crm-lookup",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": "https://elsewhere/mcp"}}},
    }
    spec = AgentSpec(**_mcp_spec())
    spec.mcp.client_id = "client-1"
    aws.cognito.clients["client-1"] = {"ClientName": "c", "ClientSecret": "s"}
    agent_id, dep_id = _mk_agent(spec)
    _active(agent_id)
    with pytest.raises(AppError) as err:
        byoc_dep._stage_register(_stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES)),
                                 _get_agent(agent_id))
    assert err.value.code == "mcp.target_name_taken"
    assert "X1" in aws.control.targets  # never repointed or deleted


def test_a_renamed_server_drops_its_previous_target(aws):
    agent_id = _registered(aws)
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    row.spec = {**row.spec, "mcp": {**row.spec["mcp"], "target_name": "crm-v2"}}
    db.commit()
    db.close()
    byoc_dep._stage_register(_stage_ctx(agent_id, "d", ws_ctx(RESOURCES)), _get_agent(agent_id))
    assert [t["name"] for t in aws.control.targets.values()] == ["crm-v2"]
    assert "delete_target:T1" in aws.control.calls


# ── teardown ──────────────────────────────────────────────────────────────────


def test_delete_removes_target_then_runtime_then_identity(aws):
    agent_id = _registered(aws)
    agent = _get_agent(agent_id)
    byoc_dep.delete_agent_resources(agent, ws_ctx(RESOURCES))
    calls = aws.control.calls
    order = [c.split(":")[0] for c in calls
             if c.split(":")[0] in ("delete_target", "delete_runtime", "delete_provider")]
    assert order == ["delete_target", "delete_runtime", "delete_provider"]
    assert aws.control.targets == {}
    assert aws.cognito.deleted  # the server's own client is gone too
    # idempotent: a second teardown finds nothing and does not raise
    byoc_dep.delete_agent_resources(agent, ws_ctx(RESOURCES))


# ── routes ────────────────────────────────────────────────────────────────────


def test_mcp_route_and_guards(client, aws, monkeypatch):
    set_default_resources(RESOURCES)
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    res = client.post("/api/agents", json=_mcp_spec())
    assert res.status_code == 202, res.text
    agent_id = res.json()["agent"]["id"]
    _active(agent_id)
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    row.status = "active"
    row.spec = {**row.spec, "mcp": {**row.spec["mcp"], "target_id": "T9"}}
    db.commit()
    db.close()
    aws.control.targets["T9"] = {"name": "crm-lookup"}

    status = client.get(f"/api/agents/{agent_id}/mcp").json()
    assert status["target_status"] == "READY"
    assert [t["name"] for t in status["tools"]] == ["crm-lookup___find_order"]

    switched = client.post(f"/api/agents/{agent_id}/inbound-auth", json={"inbound_auth": None})
    assert switched.status_code == 422
    assert switched.json()["code"] == "agent.inbound_auth_unsupported"

    to_agent = client.post(f"/api/agents/{agent_id}/redeploy", json={
        **_mcp_spec(), "protocol": "http", "mcp": None,
    })
    assert to_agent.status_code == 400
    assert to_agent.json()["code"] == "agent.redeploy_immutable"

    chat = client.post(f"/api/agents/{agent_id}/invoke", json={"prompt": "hi"})
    assert chat.status_code == 409


def test_mcp_route_refuses_an_ordinary_agent(client, monkeypatch):
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="plain", method="zip_runtime",
                  status="active", spec={"name": "plain", "method": "zip_runtime"})
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    res = client.get(f"/api/agents/{agent_id}/mcp")
    assert res.status_code == 409
    assert res.json()["code"] == "agent.not_mcp_server"
