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

Tool review (§15.6) is the gate: creating or re-syncing the target is how the
server's tools are listed, and right after that the list is compared with the
last approved one (``services/mcp_reviews.py``). A list nobody approved yet —
a first deploy, a re-publish that changed a tool, a drift — opens a review and
the target is deleted again, so agents cannot call (or even read) the new tools
until a member holding ``mcp.approve`` who is not the deployer approves; the
approval re-creates the target in the background (``publish_approved``).
Withholding the whole target is the gate; the Cedar scaffold (§15.7,
``scaffold_inputs`` + ``governance.apply_mcp_scaffold``) adds the per-tool
forbid / per-project permit on the Gateway's policy engine on request.

Endpoint, health, rollback (§15.8): the target calls the runtime's ``DEFAULT``
endpoint (it follows every new version) unless the server is pinned — a rollback
points the named endpoint ``live`` at an earlier version, restores that version's
approved tool list and re-points the target there; the next deploy un-pins. The
hourly scheduler task ``mcp.health`` (``check_health``) re-syncs each live
target, compares the list with the approved one (``check_drift``) and records
the outcome in ``mcp_servers.health`` (`models/lambda_tools.McpServerState`).

Design: docs/design/projects.zh-CN.md §15 (PUBLIC or a VPC network profile, §15.10:
the profile changes the runtime's outbound path only; the Gateway reaches it through
the AgentCore data plane). Teardown runs in reverse and is idempotent
(``teardown_target`` before the runtime, ``release_identity`` after it).
"""

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from app.core.db import SessionLocal
from app.core.errors import AppError, is_not_found
from app.core.project_scope import project_context
from app.models.lambda_tools import McpServerState
from app.models.ledger import Agent
from app.models.mcp_review import McpToolReview
from app.services import mcp_client, mcp_reviews
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
# the named endpoint a rollback pins (the same name Agent-DLC's releases use)
LIVE = "live"
TARGET_TIMEOUT_S = 300
# consecutive failed health checks before the check counts as an alert
HEALTH_ALERT_AFTER = 3

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


def endpoint_url(
    workspace: WorkspaceContext, runtime_arn: str, qualifier: str = QUALIFIER
) -> str:
    """The streamable-HTTP MCP endpoint the Gateway target calls."""
    return rt.bearer_invoke_url(workspace.region, runtime_arn, qualifier)


# ── platform-side state (models/lambda_tools.McpServerState) ───────────────────


def load_state(db: Any, agent: Agent) -> McpServerState:
    """The server's state row (created on first use; the caller commits)."""
    row = db.get(McpServerState, agent.id)
    if row is None:
        row = McpServerState(agent_id=agent.id, workspace_id=agent.workspace_id,
                             project_id=agent.project_id, qualifier=QUALIFIER,
                             health={}, operation={})
        db.add(row)
        db.flush()
    return row


def _update_state(agent: Agent, **values: Any) -> None:
    with project_context(agent.project_id):
        db = SessionLocal()
        try:
            row = load_state(db, agent)
            for key, value in values.items():
                setattr(row, key, value)
            db.commit()
        finally:
            db.close()


def current_qualifier(agent: Agent) -> str:
    """The endpoint the server's target should call: ``live`` for a gated agent or a
    server a rollback pinned, else ``DEFAULT``."""
    if getattr(agent, "endpoint_mode", None) == LIVE:
        return LIVE
    with project_context(agent.project_id):
        db = SessionLocal()
        try:
            row = db.get(McpServerState, agent.id)
            return row.qualifier if row is not None and row.qualifier else QUALIFIER
        finally:
            db.close()


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


def _same_runtime(endpoint: str, url: str) -> bool:
    """Both URLs invoke the same runtime (they may differ in the qualifier only)."""
    return bool(endpoint) and endpoint.split("?", 1)[0] == url.split("?", 1)[0]


def _repoint_target(
    control: Any, gateway_id: str, target: dict[str, Any], url: str, log: Log
) -> None:
    """Move an MCP-server target to another endpoint of its runtime (UpdateGatewayTarget
    replaces the whole definition, so name and credentials are sent back unchanged)."""
    target_id = str(target["targetId"])
    detail = policy_api.get_gateway_target(control, gateway_id, target_id)
    kwargs: dict[str, Any] = {
        "gatewayIdentifier": gateway_id,
        "targetId": target_id,
        "name": detail.get("name") or target.get("name"),
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": url}}},
        "credentialProviderConfigurations": detail.get("credentialProviderConfigurations")
        or target.get("credentialProviderConfigurations") or [],
    }
    if detail.get("description"):
        kwargs["description"] = detail["description"]
    control.update_gateway_target(**kwargs)
    log(f"gateway target {target_id} re-pointed → {url}")


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
    qualifier: str | None = None,
) -> tuple[str | None, str]:
    """Create the MCP-server target, or re-sync the one this server already owns,
    then pass it through the tool-review gate.

    ``qualifier`` is the runtime endpoint to call; None (a deploy) un-pins: the
    target follows ``DEFAULT`` again (``live`` for a gated agent), so a re-publish
    after a rollback rolls forward. A target of this runtime calling another
    endpoint is re-pointed, not refused.

    Returns ``(target_id, outcome)`` with outcome ``created`` | ``synchronized``;
    ``(None, "… withheld …")`` when the listed tools are not the approved ones (the
    target was deleted again and a review is open). A target of the same name that
    points elsewhere is somebody else's: refused (``409 mcp.target_name_taken``)
    rather than repointed. A renamed server's previous target is deleted first.
    """
    control = control_client(workspace)
    gateway_id = _gateway_id(workspace)
    if qualifier is None:
        qualifier = LIVE if getattr(agent, "endpoint_mode", None) == LIVE else QUALIFIER
        _update_state(agent, qualifier=qualifier)
    url = endpoint_url(workspace, agent.arn or "", qualifier)
    targets = policy_api.list_gateway_target_details(control, gateway_id)
    if previous_target_id:
        previous = next((t for t in targets if t.get("targetId") == previous_target_id), None)
        if previous is not None and previous.get("name") != target_name:
            log(f"target renamed {previous.get('name')} → {target_name}; deleting the old one")
            _delete_target_and_wait(control, gateway_id, previous_target_id)
            targets = [t for t in targets if t.get("targetId") != previous_target_id]
    same_name = next((t for t in targets if t.get("name") == target_name), None)
    if same_name is not None:
        if _target_endpoint(same_name) != url and _same_runtime(_target_endpoint(same_name), url):
            _wait_target(control, gateway_id, str(same_name["targetId"]), log)
            _repoint_target(control, gateway_id, same_name, url, log)
        elif _target_endpoint(same_name) != url:
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
        if previous_target_id == target_id:
            # only a target a finished deploy stamped: one left by a deploy that died
            # before the gate must not become the baseline unread
            _baseline_if_unreviewed(workspace, agent, target_name, log)
        policy_api.synchronize_gateway_target(control, gateway_id, target_id)
        _wait_target(control, gateway_id, target_id, log)
        return _through_gate(
            workspace, agent, control, gateway_id, target_id, target_name, "synchronized", log
        )
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
    return _through_gate(
        workspace, agent, control, gateway_id, target_id, target_name, "created", log
    )


# ── the tool-review gate (§15.6) ───────────────────────────────────────────────


def listed_tools(workspace: WorkspaceContext, target_name: str) -> list[dict[str, Any]]:
    """The target's tools through the Gateway, as a review snapshot (prefix stripped)."""
    return mcp_reviews.normalize_tools(mcp_client.tools_list(workspace), target_name)


def _baseline_if_unreviewed(
    workspace: WorkspaceContext, agent: Agent, target_name: str, log: Log
) -> None:
    """A server whose target predates tool review: record what agents use today as
    the approved baseline (read BEFORE the re-sync), so only a change needs review."""
    with project_context(agent.project_id):
        db = SessionLocal()
        try:
            if mcp_reviews.has_reviews(db, agent.id):
                return
            try:
                tools = listed_tools(workspace, target_name)
            except (AppError, httpx.HTTPError) as exc:
                log(f"tool-review baseline skipped (tools/list failed: {exc})")
                return
            mcp_reviews.record_baseline(db, agent, tools)
            db.commit()
            log(f"tool review: recorded the live {len(tools)} tool(s) as the approved baseline")
        finally:
            db.close()


def _withhold(control: Any, gateway_id: str, target_id: str, *, wait: bool = True) -> None:
    if wait:
        _delete_target_and_wait(control, gateway_id, target_id)
        return
    try:
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except ClientError as exc:
        if not is_not_found(exc):
            raise


def gate(agent: Agent, tools: list[dict[str, Any]], *, source: str) -> McpToolReview:
    """The review deciding ``tools`` (committed): approved = the gate is open."""
    with project_context(agent.project_id):
        db = SessionLocal()
        try:
            review = mcp_reviews.record_tool_list(db, agent, tools, source=source)
            db.commit()
            db.refresh(review)
            db.expunge(review)
            return review
        finally:
            db.close()


def _through_gate(
    workspace: WorkspaceContext,
    agent: Agent,
    control: Any,
    gateway_id: str,
    target_id: str,
    target_name: str,
    outcome: str,
    log: Log,
) -> tuple[str | None, str]:
    try:
        tools = listed_tools(workspace, target_name)
    except (AppError, httpx.HTTPError) as exc:
        # fail closed: an unread list cannot be reviewed, so it is not served either
        _withhold(control, gateway_id, target_id)
        raise RuntimeError(
            f"could not read the MCP server's tools through the Gateway for review ({exc}); "
            "the target was withheld — re-run the deploy once tools/list works"
        ) from exc
    review = gate(agent, tools, source="deploy")
    if review.status == "approved":
        log(f"tool review: {len(tools)} tool(s) match the approved list — target live")
        return target_id, outcome
    _withhold(control, gateway_id, target_id)
    log(
        f"tool review {review.id} opened for {len(tools)} tool(s) "
        f"(+{len(review.diff.get('added', []))} −{len(review.diff.get('removed', []))} "
        f"~{len(review.diff.get('description_changed', []))} described "
        f"~{len(review.diff.get('schema_changed', []))} schema, {len(review.hints)} hint(s)); "
        "the target is withheld until a member with mcp.approve other than the deployer "
        "approves it"
    )
    return None, f"{outcome}, then withheld for tool review"


def _stamp_target_id(agent_id: str, target_id: str | None) -> None:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        if row is None:
            return
        spec = dict(row.spec or {})
        spec["mcp"] = {**(spec.get("mcp") or {}), "target_id": target_id}
        row.spec = spec
        db.commit()
    finally:
        db.close()


def check_drift(workspace: WorkspaceContext, agent: Agent) -> dict[str, Any]:
    """Re-sync the live target and compare its tool list with the approved one (a
    server can change its tools without a re-deploy); a difference opens a ``drift``
    review and withholds the target. Returns what was found."""
    mcp = (agent.spec or {}).get("mcp") or {}
    target_id = mcp.get("target_id")
    target_name = str(mcp.get("target_name") or "")
    if not target_id or not target_name:
        return {"checked": False, "reason": "no_target", "drift": False}
    control = control_client(workspace)
    gateway_id = _gateway_id(workspace)
    noop: Log = lambda _msg: None  # noqa: E731
    _wait_target(control, gateway_id, str(target_id), noop)
    policy_api.synchronize_gateway_target(control, gateway_id, str(target_id))
    _wait_target(control, gateway_id, str(target_id), noop)
    try:
        tools = listed_tools(workspace, target_name)
    except (AppError, httpx.HTTPError):
        _withhold(control, gateway_id, str(target_id), wait=False)  # fail closed
        _stamp_target_id(agent.id, None)
        raise
    review = gate(agent, tools, source="drift")
    if review.status == "approved":
        return {"checked": True, "drift": False, "review_id": review.id,
                "tool_count": len(tools)}
    _withhold(control, gateway_id, str(target_id), wait=False)
    _stamp_target_id(agent.id, None)
    logger.warning("agent %s: MCP tool list drifted; target %s withheld (review %s)",
                   agent.id, target_id, review.id)
    return {"checked": True, "drift": True, "review_id": review.id, "tool_count": len(tools)}


def publish_approved(workspace: WorkspaceContext, agent_id: str, review_id: str) -> None:
    """After an approval: re-create the withheld target (through the gate again —
    the server must still list exactly the approved tools) and stamp its id."""
    db = SessionLocal()
    try:
        review = db.get(McpToolReview, review_id)
        agent = db.get(Agent, agent_id)
        if review is None or agent is None:
            return
        mcp = (agent.spec or {}).get("mcp") or {}
        error: str | None = None
        try:
            if agent.status == "deleted" or not agent.arn:
                raise RuntimeError("the MCP server has no runtime — re-deploy it")
            if not mcp.get("provider_arn"):
                raise RuntimeError("the MCP server has no OAuth2 provider — re-deploy it")
            target_id, _outcome = ensure_target(
                workspace, agent, target_name=str(mcp.get("target_name") or ""),
                provider_arn=str(mcp["provider_arn"]),
                previous_target_id=mcp.get("target_id"),
                log=lambda msg: logger.info("agent %s: %s", agent_id, msg),
                qualifier=current_qualifier(agent),
            )
            _stamp_target_id(agent_id, target_id)
            if target_id is None:
                error = "the server now lists different tools — a new review is open"
        except (AppError, ClientError, BotoCoreError, RuntimeError) as exc:
            error = f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"
        db.refresh(review)
        review.publish_status = "failed" if error else "published"
        review.publish_error = error[:2000] if error else None
        db.commit()
    finally:
        db.close()


def start_publish(workspace: WorkspaceContext, agent: Agent, review_id: str) -> None:
    """Run ``publish_approved`` on a daemon thread, inside the server's project."""
    project_id, agent_id = agent.project_id, agent.id

    def run() -> None:
        with project_context(project_id):
            try:
                publish_approved(workspace, agent_id, review_id)
            except Exception:  # a crashed publish must not kill the process
                logger.exception("agent %s: publishing approved MCP tools failed", agent_id)

    threading.Thread(target=run, daemon=True, name=f"mcp-publish-{agent_id[:8]}").start()


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
    """Delete the server's Gateway target — before the runtime it points at — and the
    ``live`` endpoint a rollback made (AgentCore will not delete a runtime that still
    has one; a gated agent's endpoints are Agent-DLC's to delete)."""
    target_id = ((agent.spec or {}).get("mcp") or {}).get("target_id")
    if target_id:
        _delete_target_and_wait(control_client(workspace), _gateway_id(workspace), target_id)
    with project_context(agent.project_id):
        db = SessionLocal()
        try:
            row = db.get(McpServerState, agent.id)
            pinned = row is not None and row.live_version is not None
        finally:
            db.close()
    if pinned and agent.resource_id and getattr(agent, "endpoint_mode", None) != LIVE:
        _delete_live_endpoint(control_client(workspace), agent.resource_id)


def _delete_live_endpoint(control: Any, runtime_id: str) -> None:
    try:
        rt.delete_runtime_endpoint(control, runtime_id=runtime_id, endpoint_name=LIVE)
    except ClientError as exc:
        if is_not_found(exc):
            return
        raise
    deadline = time.monotonic() + TARGET_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            rt.get_runtime_endpoint(control, runtime_id=runtime_id, endpoint_name=LIVE)
        except ClientError as exc:
            if is_not_found(exc):
                return
            raise
        time.sleep(3)  # nosemgrep: arbitrary-sleep
    raise RuntimeError(f"runtime endpoint {LIVE} still present after {TARGET_TIMEOUT_S}s")


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
        "review": None,
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
    live_hash: str | None = None
    if out["target_status"] == "READY" and target_name:
        try:
            raw = mcp_client.tools_list(workspace)
            prefix = f"{target_name}___"
            out["tools"] = [
                {"name": str(t.get("name") or ""), "description": str(t.get("description") or "")}
                for t in raw if str(t.get("name") or "").startswith(prefix)
            ]
            live_hash = mcp_reviews.tools_hash(mcp_reviews.normalize_tools(raw, target_name))
        except AppError as exc:
            out["tools_error"] = exc.message
    db = SessionLocal()
    try:
        gate_state = mcp_reviews.state(db, agent.id)
        row = db.get(McpServerState, agent.id)
        out["qualifier"] = current_qualifier(agent)
        out["live_version"] = row.live_version if row is not None else None
        out["health"] = (row.health or {}) if row is not None else {}
        out["operation"] = (row.operation or {}) if row is not None else {}
        out["rollback_versions"] = [
            {"version": r.version, "review_id": r.id, "decided_at":
             r.decided_at.isoformat() if r.decided_at else None}
            for r in mcp_reviews.approved_versions(db, agent.id)
        ]
    finally:
        db.close()
    if agent.arn:
        out["endpoint"] = endpoint_url(workspace, agent.arn, out["qualifier"])
    approved_hash = gate_state["approved_hash"]
    out["review"] = {
        **gate_state,
        "live_hash": live_hash,
        # read-only: a mismatch is recorded (and the target withheld) by
        # POST /api/mcp-servers/{agent_id}/reviews/check or the next deploy
        "drift": bool(live_hash and approved_hash and live_hash != approved_hash),
    }
    return out


# ── health (§15.8): the hourly `mcp.health` task and POST …/health ─────────────


def follow_endpoint(workspace: WorkspaceContext, agent: Agent) -> bool:
    """Re-point the live target when it calls another endpoint than the server's
    current one (``DEFAULT`` / pinned ``live``). True when it moved."""
    target_id = ((agent.spec or {}).get("mcp") or {}).get("target_id")
    if not target_id or not agent.arn:
        return False
    control = control_client(workspace)
    gateway_id = _gateway_id(workspace)
    target = policy_api.get_gateway_target(control, gateway_id, str(target_id))
    url = endpoint_url(workspace, agent.arn, current_qualifier(agent))
    current = _target_endpoint(target)
    if current == url or not _same_runtime(current, url):
        return False
    noop: Log = lambda _msg: None  # noqa: E731
    _wait_target(control, gateway_id, str(target_id), noop)
    _repoint_target(control, gateway_id, {**target, "targetId": target_id}, url, noop)
    _wait_target(control, gateway_id, str(target_id), noop)
    return True


def check_health(workspace: WorkspaceContext, agent: Agent) -> dict[str, Any]:
    """One health check: follow the live endpoint, re-sync, compare the tool list with
    the approved one (``check_drift`` — a drift opens a review and withholds the
    target), and record the outcome in ``mcp_servers.health``. Never raises for an
    AWS / Gateway failure: that is the ``failing`` outcome."""
    with project_context(agent.project_id):
        db = SessionLocal()
        try:
            previous = dict(load_state(db, agent).health or {})
            db.commit()
        finally:
            db.close()
    now = time.time()
    health: dict[str, Any] = {
        "checked_at": _iso_now(), "consecutive_failures": 0, "error": None,
        "tool_count": None, "review_id": None, "repointed": False,
    }
    try:
        health["repointed"] = follow_endpoint(workspace, agent)
        found = check_drift(workspace, agent)
        health["review_id"] = found.get("review_id")
        health["tool_count"] = found.get("tool_count")
        if not found.get("checked"):
            health["status"] = "withheld"  # no live target: a review is open or rejected
        elif found.get("drift"):
            health["status"] = "drift"
        else:
            health["status"] = "healthy"
    except (AppError, ClientError, BotoCoreError, RuntimeError, httpx.HTTPError) as exc:
        failures = int(previous.get("consecutive_failures") or 0) + 1
        health.update(
            status="failing", consecutive_failures=failures,
            error=f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:500],
        )
        if failures >= HEALTH_ALERT_AFTER:
            logger.warning("agent %s: MCP health check failed %d times in a row: %s",
                           agent.id, failures, health["error"])
    health["alert"] = health["consecutive_failures"] >= HEALTH_ALERT_AFTER
    health["duration_ms"] = int((time.time() - now) * 1000)
    _update_state(agent, health=health)
    return health


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def health_candidates(db: Any) -> list[Agent]:
    """Every active hosted MCP server with a deployed runtime (unscoped scan)."""
    from sqlalchemy import select

    rows = db.scalars(
        select(Agent).where(Agent.status == "active", Agent.method == "byoc",
                            Agent.arn.isnot(None))
    ).all()
    return [a for a in rows if (a.spec or {}).get("protocol") == "mcp"]


# ── rollback (§15.8) ───────────────────────────────────────────────────────────


def plan_rollback(db: Any, agent: Agent, version: str | None) -> McpToolReview:
    """The approved review a rollback returns to: ``version``'s, else the newest
    approved version other than the one the target serves now. 409 when none."""
    row = db.get(McpServerState, agent.id)
    serving = (row.live_version if row is not None and row.qualifier == LIVE else None) or (
        agent.version
    )
    candidates = mcp_reviews.approved_versions(db, agent.id)
    if version:
        chosen = next((r for r in candidates if r.version == str(version)), None)
    else:
        chosen = next((r for r in candidates if r.version != serving), None)
    if chosen is None:
        raise AppError(
            "mcp.rollback_unavailable",
            "no earlier version of this MCP server has an approved tool list to roll back to",
            {"version": version, "serving": serving,
             "approved_versions": [r.version for r in candidates]},
            status_code=409,
        )
    if chosen.version == serving:
        raise AppError("mcp.rollback_noop", f"the target already serves version {serving}",
                       {"version": serving}, status_code=409)
    if row is not None and (row.operation or {}).get("status") == "running":
        raise AppError("mcp.operation_in_flight",
                       f"a {row.operation.get('kind')} of this server is still running",
                       {"operation": row.operation}, status_code=409)
    return chosen


def run_rollback(
    workspace: WorkspaceContext, agent_id: str, review_id: str, *, actor: str,
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    """Point ``live`` at the review's version, make its approved list the current one,
    and re-point (or re-create) the target there — through the gate again. Recorded in
    ``mcp_servers.operation``."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        review = db.get(McpToolReview, review_id)
        if agent is None or review is None:
            return {"status": "failed", "error": "server or review not found"}
        db.expunge(agent)
    finally:
        db.close()
    version = str(review.version)
    operation: dict[str, Any] = {"kind": "rollback", "status": "running", "version": version,
                                 "actor": actor, "started_at": _iso_now(), "error": None}
    _update_state(agent, operation=operation)
    mcp = (agent.spec or {}).get("mcp") or {}
    try:
        if not agent.resource_id or not agent.arn:
            raise RuntimeError("the MCP server has no runtime — re-deploy it")
        if not mcp.get("provider_arn"):
            raise RuntimeError("the MCP server has no OAuth2 provider — re-deploy it")
        rt.ensure_runtime_endpoint(control_client(workspace), runtime_id=agent.resource_id,
                                   endpoint_name=LIVE, version=version)
        with project_context(agent.project_id):
            db = SessionLocal()
            try:
                old = db.get(McpToolReview, review_id)
                mcp_reviews.restore_approved(db, old, actor=actor, actor_user_id=actor_user_id)
                state_row = load_state(db, agent)
                state_row.qualifier, state_row.live_version = LIVE, version
                db.commit()
            finally:
                db.close()
        target_id, _outcome = ensure_target(
            workspace, agent, target_name=str(mcp.get("target_name") or ""),
            provider_arn=str(mcp["provider_arn"]), previous_target_id=mcp.get("target_id"),
            log=lambda msg: logger.info("agent %s rollback: %s", agent_id, msg),
            qualifier=LIVE,
        )
        _stamp_target_id(agent_id, target_id)
        if target_id is None:
            operation.update(status="failed", error=(
                f"version {version} now lists different tools than were approved for it — "
                "the target is withheld and a review is open"))
        else:
            operation.update(status="succeeded", target_id=target_id)
    except (AppError, ClientError, BotoCoreError, RuntimeError) as exc:
        operation.update(status="failed", error=(
            f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:2000]))
    operation["completed_at"] = _iso_now()
    _update_state(agent, operation=operation)
    return operation


def _background(agent: Agent, name: str, fn: Callable[[], Any]) -> None:
    project_id = agent.project_id

    def run() -> None:
        with project_context(project_id):
            try:
                fn()
            except Exception:  # a crashed operation must not kill the process
                logger.exception("agent %s: MCP %s failed", agent.id, name)

    threading.Thread(target=run, daemon=True, name=f"mcp-{name}-{agent.id[:8]}").start()


def start_rollback(
    workspace: WorkspaceContext, agent: Agent, review_id: str, *, actor: str,
    actor_user_id: str | None = None,
) -> None:
    _update_state(agent, operation={"kind": "rollback", "status": "running",
                                    "actor": actor, "started_at": _iso_now()})
    _background(agent, "rollback", lambda: run_rollback(
        workspace, agent.id, review_id, actor=actor, actor_user_id=actor_user_id))


# ── Cedar scaffold (§15.7) ─────────────────────────────────────────────────────


def scaffold_inputs(db: Any, agent_id: str) -> dict[str, Any]:
    """The tool names the scaffold forbids (an open review's) and permits (the
    approved list's)."""
    approved = mcp_reviews.latest_approved(db, agent_id)
    pending = mcp_reviews.open_review(db, agent_id)
    if approved is None and pending is None:
        raise AppError(
            "mcp.scaffold_nothing_reviewed",
            "this server has no tool list yet — deploy it so its tools can be reviewed",
            status_code=409,
        )
    return {
        "approved_tools": [t["name"] for t in (approved.tools or [])] if approved else None,
        "open_tools": [t["name"] for t in (pending.tools or [])] if pending else None,
        "approved_review_id": approved.id if approved else None,
        "open_review_id": pending.id if pending else None,
    }


def start_scaffold(
    workspace: WorkspaceContext, agent: Agent, plan: dict[str, Any], *, target_name: str,
    acknowledged_gateway_ids: list[str], mode: dict[str, Any] | None, evidence_count: int,
    actor: str,
) -> None:
    """Apply a scaffold plan in the background (one governance operation at a time),
    recording progress and the operation ids in ``mcp_servers.operation``."""
    from app.services import governance

    operation: dict[str, Any] = {"kind": "scaffold", "status": "running", "actor": actor,
                                 "started_at": _iso_now(), "plan": plan, "results": []}
    _update_state(agent, operation=operation)

    def run() -> None:
        try:
            results = governance.apply_mcp_scaffold(
                workspace, plan["gateway_id"], plan["steps"], target_name=target_name,
                acknowledged_gateway_ids=acknowledged_gateway_ids, mode=mode,
                evidence_count=evidence_count,
            )
            failed = next((r for r in results if r["status"] != "succeeded"), None)
            operation.update(results=results, status="failed" if failed else "succeeded",
                             error=failed["error"] if failed else None)
        except (AppError, ClientError, BotoCoreError, RuntimeError) as exc:
            operation.update(status="failed", error=(
                f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:2000]))
        operation["completed_at"] = _iso_now()
        _update_state(agent, operation=operation)

    _background(agent, "scaffold", run)
