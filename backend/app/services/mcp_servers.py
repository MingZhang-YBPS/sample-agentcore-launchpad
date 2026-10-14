"""Hosted MCP servers: a BYOC runtime (``protocol="mcp"``) wired to the Gateway.

An MCP server is a tool, not an agent. The platform deploys the member's
artifact as an AgentCore Runtime with ``serverProtocol=MCP`` and makes the
workspace Gateway its only caller:

    inbound   a Cognito app client of its own (``lp-mcp-<agent id12>``,
              client_credentials) in the workspace pool; the runtime's
              ``customJWTAuthorizer`` allows exactly that client
    provider  an OAuth2 credential provider of the same name holding that
              client, so the Gateway can mint the token
    target    an MCP-server target on the workspace Gateway whose endpoint is
              the runtime's data-plane URL (DEFAULT endpoint); creating it, or
              synchronizing it after a re-publish, lists the server's tools

Agents then reach the tools as ``<target_name>___<tool>`` through the Gateway,
under its Cedar policy. The Gateway-level Registry record is NOT rewritten
here: an update resets an approved record to DRAFT for every agent that
attaches it, so refreshing the catalog stays an explicit Governance action.

Design: docs/design/projects.zh-CN.md §15 (minimal version: public network,
no tool review, no Cedar scaffolding). Teardown runs in reverse and is
idempotent (``teardown_target`` before the runtime, ``release_identity``
after it).
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import AppError, is_not_found
from app.models.ledger import Agent
from app.services import mcp_client
from app.services.agentcore import policy as policy_api
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.workspace import WorkspaceContext

logger = logging.getLogger(__name__)

NAME_PREFIX = "lp-mcp-"
# The workspace pool's one resource-server scope (workspace_bootstrap); the MCP
# client's token is only ever accepted by its own runtime's authorizer, which
# checks allowedClients — the Gateway's own authorizer does not list it.
TOKEN_SCOPE = "launchpad-gw/invoke"
# the DEFAULT endpoint follows every new version, so a re-publish needs only a
# tool re-sync, never an endpoint change on the target
QUALIFIER = "DEFAULT"
TARGET_TIMEOUT_S = 300

Log = Callable[[str], None]


def resource_name(agent: Agent) -> str:
    """Name of the server's Cognito client and OAuth2 provider (both per agent)."""
    return f"{NAME_PREFIX}{agent.id[:12]}"


def _pool_id(workspace: WorkspaceContext) -> str:
    pool_id = str(workspace.resources.get("user_pool_id") or "")
    if not pool_id:
        raise RuntimeError(
            "user_pool_id missing from this workspace's resource map — an MCP server "
            "needs the workspace Cognito pool; run its bootstrap"
        )
    return pool_id


def _gateway_id(workspace: WorkspaceContext) -> str:
    gateway_id = str(workspace.resources.get("gateway_id") or "")
    if not gateway_id:
        raise RuntimeError(
            "gateway_id missing from this workspace's resource map — run its bootstrap"
        )
    return gateway_id


def discovery_url(workspace: WorkspaceContext) -> str:
    return (
        f"https://cognito-idp.{workspace.region}.amazonaws.com/{_pool_id(workspace)}"
        "/.well-known/openid-configuration"
    )


def endpoint_url(workspace: WorkspaceContext, runtime_arn: str) -> str:
    """The streamable-HTTP MCP endpoint the Gateway target calls."""
    return rt.bearer_invoke_url(workspace.region, runtime_arn, QUALIFIER)


# ── inbound: the server's own Cognito client ───────────────────────────────────


def _find_client(cognito: Any, pool_id: str, name: str) -> str | None:
    kwargs: dict[str, Any] = {"UserPoolId": pool_id, "MaxResults": 60}
    while True:
        page = cognito.list_user_pool_clients(**kwargs)
        for client in page.get("UserPoolClients", []):
            if client.get("ClientName") == name:
                return str(client["ClientId"])
        token = page.get("NextToken")
        if not token:
            return None
        kwargs["NextToken"] = token


def ensure_inbound_client(workspace: WorkspaceContext, agent: Agent, log: Log) -> str:
    """The server's client_credentials app client (found by name, else created)."""
    cognito = workspace.client("cognito-idp")
    pool_id = _pool_id(workspace)
    name = resource_name(agent)
    existing = _find_client(cognito, pool_id, name)
    if existing:
        return existing
    created = cognito.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=name,
        GenerateSecret=True,  # the OAuth2 provider reads this secret
        AllowedOAuthFlows=["client_credentials"],
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthScopes=[TOKEN_SCOPE],
        SupportedIdentityProviders=["COGNITO"],
        AccessTokenValidity=1,
        TokenValidityUnits={"AccessToken": "hours"},
    )["UserPoolClient"]
    log(f"created Cognito client {name} for the MCP server's inbound auth")
    return str(created["ClientId"])


def inbound_auth(workspace: WorkspaceContext, client_id: str) -> dict[str, Any]:
    """The JWT authorizer the runtime carries: the workspace pool, this client only."""
    return {
        "mode": "jwt",
        "jwt": {
            "discovery_url": discovery_url(workspace),
            "allowed_clients": [client_id],
        },
    }


# ── outbound: the Gateway's OAuth2 provider for that client ────────────────────


def _find_provider(control: Any, name: str) -> str | None:
    kwargs: dict[str, Any] = {"maxResults": 20}
    while True:
        page = control.list_oauth2_credential_providers(**kwargs)
        for provider in page.get("credentialProviders", []):
            if provider.get("name") == name:
                return str(provider["credentialProviderArn"])
        token = page.get("nextToken")
        if not token:
            return None
        kwargs["nextToken"] = token


def ensure_provider(
    workspace: WorkspaceContext, agent: Agent, client_id: str, log: Log
) -> str:
    """OAuth2 provider (CustomOauth2 against the pool) holding the server's client."""
    control = control_client(workspace)
    name = resource_name(agent)
    existing = _find_provider(control, name)
    if existing:
        return existing
    secret = workspace.client("cognito-idp").describe_user_pool_client(
        UserPoolId=_pool_id(workspace), ClientId=client_id
    )["UserPoolClient"]["ClientSecret"]
    created = control.create_oauth2_credential_provider(
        name=name,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "oauthDiscovery": {"discoveryUrl": discovery_url(workspace)},
                "clientId": client_id,
                "clientSecret": secret,
            }
        },
    )
    log(f"created OAuth2 provider {name} for the Gateway's outbound auth")
    return str(created["credentialProviderArn"])


# ── the Gateway target ─────────────────────────────────────────────────────────


def _target_endpoint(target: dict[str, Any]) -> str:
    mcp = ((target.get("targetConfiguration") or {}).get("mcp") or {})
    return str((mcp.get("mcpServer") or {}).get("endpoint") or "")


def _wait_target(control: Any, gateway_id: str, target_id: str, log: Log) -> dict[str, Any]:
    deadline = time.monotonic() + TARGET_TIMEOUT_S
    last = ""
    while True:
        target = policy_api.get_gateway_target(control, gateway_id, target_id)
        status = str(target.get("status") or "").upper()
        if status != last:
            log(f"gateway target status: {status}")
            last = status
        if status == "READY":
            return target
        if status in ("FAILED", "CREATE_FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"):
            reasons = "; ".join(str(r) for r in target.get("statusReasons") or []) or status
            raise RuntimeError(
                f"gateway target {target_id} {status}: {reasons} — the Gateway could not "
                "list the server's tools; check that it serves streamable HTTP on "
                "0.0.0.0:8000/mcp"
            )
        if time.monotonic() > deadline:
            raise RuntimeError(f"gateway target {target_id} not READY after {TARGET_TIMEOUT_S}s")
        # Deliberate polling interval; the deadline above owns the loop.
        time.sleep(5)  # nosemgrep: arbitrary-sleep


def _delete_target_and_wait(control: Any, gateway_id: str, target_id: str) -> None:
    try:
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except ClientError as exc:
        if is_not_found(exc):
            return
        raise
    deadline = time.monotonic() + TARGET_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            policy_api.get_gateway_target(control, gateway_id, target_id)
        except ClientError as exc:
            if is_not_found(exc):
                return
            raise
        time.sleep(3)  # nosemgrep: arbitrary-sleep
    raise RuntimeError(f"gateway target {target_id} still present after {TARGET_TIMEOUT_S}s")


def ensure_target(
    workspace: WorkspaceContext,
    agent: Agent,
    *,
    target_name: str,
    provider_arn: str,
    previous_target_id: str | None,
    log: Log,
) -> tuple[str, str]:
    """Create the MCP-server target, or re-sync the one this server already owns.

    Returns ``(target_id, outcome)`` with outcome ``created`` | ``synchronized``.
    A target of the same name that points elsewhere is somebody else's: refused
    (``409 mcp.target_name_taken``) rather than repointed. A renamed server's
    previous target is deleted first.
    """
    control = control_client(workspace)
    gateway_id = _gateway_id(workspace)
    url = endpoint_url(workspace, agent.arn or "")
    targets = policy_api.list_gateway_target_details(control, gateway_id)
    if previous_target_id:
        previous = next((t for t in targets if t.get("targetId") == previous_target_id), None)
        if previous is not None and previous.get("name") != target_name:
            log(f"target renamed {previous.get('name')} → {target_name}; deleting the old one")
            _delete_target_and_wait(control, gateway_id, previous_target_id)
            targets = [t for t in targets if t.get("targetId") != previous_target_id]
    same_name = next((t for t in targets if t.get("name") == target_name), None)
    if same_name is not None:
        if _target_endpoint(same_name) != url:
            raise AppError(
                "mcp.target_name_taken",
                f"the Gateway already has a target named {target_name!r} that points "
                "at another server — pick another target name",
                {"target_name": target_name, "target_id": same_name.get("targetId")},
                status_code=409,
            )
        target_id = str(same_name["targetId"])
        # a re-publish keeps the URL (DEFAULT follows the new version): re-list tools
        _wait_target(control, gateway_id, target_id, log)
        policy_api.synchronize_gateway_target(control, gateway_id, target_id)
        _wait_target(control, gateway_id, target_id, log)
        return target_id, "synchronized"
    created = control.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=target_name,
        description=f"Launchpad MCP server {agent.name}",
        targetConfiguration={"mcp": {"mcpServer": {"endpoint": url}}},
        credentialProviderConfigurations=[
            {
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": provider_arn,
                        "scopes": [TOKEN_SCOPE],
                        "grantType": "CLIENT_CREDENTIALS",
                    }
                },
            }
        ],
    )
    target_id = str(created["targetId"])
    log(f"created gateway target {target_name} ({target_id}) → {url}")
    _wait_target(control, gateway_id, target_id, log)
    return target_id, "created"


def gateway_tools(workspace: WorkspaceContext, target_name: str) -> list[dict[str, Any]]:
    """The server's tools as agents see them through the Gateway (``tools/list``)."""
    prefix = f"{target_name}___"
    return [
        {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
        }
        for tool in mcp_client.tools_list(workspace)
        if str(tool.get("name") or "").startswith(prefix)
    ]


# ── teardown (reverse order, idempotent) ───────────────────────────────────────


def teardown_target(workspace: WorkspaceContext, agent: Agent) -> None:
    """Delete the server's Gateway target — before the runtime it points at."""
    target_id = ((agent.spec or {}).get("mcp") or {}).get("target_id")
    if not target_id:
        return
    _delete_target_and_wait(control_client(workspace), _gateway_id(workspace), target_id)


def release_identity(workspace: WorkspaceContext, agent: Agent, log: Log) -> None:
    """Delete the OAuth2 provider and the Cognito client (best-effort, after the runtime)."""
    name = resource_name(agent)
    try:
        control_client(workspace).delete_oauth2_credential_provider(name=name)
    except (ClientError, BotoCoreError) as exc:
        if not (isinstance(exc, ClientError) and is_not_found(exc)):
            log(f"OAuth2 provider {name} not deleted: {type(exc).__name__}: {exc}")
    try:
        cognito = workspace.client("cognito-idp")
        pool_id = _pool_id(workspace)
        client_id = _find_client(cognito, pool_id, name)
        if client_id:
            cognito.delete_user_pool_client(UserPoolId=pool_id, ClientId=client_id)
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        if not (isinstance(exc, ClientError) and is_not_found(exc)):
            log(f"Cognito client {name} not deleted: {type(exc).__name__}: {exc}")


# ── read-back for the console ──────────────────────────────────────────────────


def server_status(workspace: WorkspaceContext, agent: Agent) -> dict[str, Any]:
    """Live state of one MCP server's Gateway wiring (never secrets)."""
    mcp = (agent.spec or {}).get("mcp") or {}
    target_name = str(mcp.get("target_name") or "")
    out: dict[str, Any] = {
        "target_name": target_name,
        "target_id": mcp.get("target_id"),
        "client_id": mcp.get("client_id"),
        "gateway_id": workspace.resources.get("gateway_id"),
        "endpoint": endpoint_url(workspace, agent.arn) if agent.arn else None,
        "target_status": None,
        "status_reasons": [],
        "tools": [],
        "tools_error": None,
    }
    target_id = mcp.get("target_id")
    if target_id:
        try:
            target = policy_api.get_gateway_target(
                control_client(workspace), _gateway_id(workspace), str(target_id)
            )
            out["target_status"] = target.get("status")
            out["status_reasons"] = [str(r) for r in target.get("statusReasons") or []]
        except ClientError as exc:
            out["target_status"] = "MISSING" if is_not_found(exc) else "UNKNOWN"
        except RuntimeError as exc:  # the workspace map lost its gateway
            out["target_status"] = "UNKNOWN"
            out["status_reasons"] = [str(exc)]
    if out["target_status"] == "READY" and target_name:
        try:
            out["tools"] = gateway_tools(workspace, target_name)
        except AppError as exc:
            out["tools_error"] = exc.message
    return out
