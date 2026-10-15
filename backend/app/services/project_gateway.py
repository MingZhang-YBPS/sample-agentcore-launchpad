"""Phase 2 isolation, gateway tools (projects design §10.6, R10-gw): who may call a
project's gateway tools, and the provisioner of `project_provision`'s `m2m` and
`cedar` stages.

Before this, every agent took its gateway token from one workspace M2M client
(`launchpad-gw-m2m`), so a JWT said nothing about the project behind it. An isolated
project gets, in each workspace it is bound to:

* **its own M2M client** `launchpad-agent-m2m-<pid>` (Cognito app client, client
  credentials, scopes `launchpad-gw/invoke` + `launchpad-gw/project.<pid>`) and the
  token-vault provider `launchpad-gw-m2m-<pid>` over it. The project boundary lets an
  agent read only its own project's provider (`project_iam`, §10.3 "identity"), so the
  token's `client_id` — and its project scope — cannot be forged by another project.
  The client joins `allowedClients` of the workspace gateway and of the KB gateway.
* **its Cognito group** `lp-project-<pid>`: `policy_identity` puts a member's shadow
  user in it, so the user token a chat turn carries names the project as well.
* **one Cedar forbid per target the project owns** (`governance.project_forbid_statement`,
  `lp_proj_<pid>_<target>`): outside principals are refused that target's tools. A
  target is the project's when `resource_creators` (kind `gateway_target`) says so —
  the Connection-bound targets `identity_gateway_targets` creates; samples and
  bootstrap targets stay platform-wide. The Gateway must run ENFORCE with a Policy
  Engine attached (§10.6-5): otherwise the stage fails with the step to take.

Once every active project of a workspace is isolated and its Cedar is in place, the
Connection-bound target routes are lowered from administrator-only to the project's
own key (§10.6-6, `route_policy.require_infra` → `infra_lowered`).

Agents pick the project's credentials up through `effective_resources`: the
resource map a deploy of an isolated project reads, with `oauth_provider_arn`
replaced by the project's provider and `gateway_scopes` carrying both scopes.

TO VERIFY live (each has its fallback built in): `client_id` and `scope` as Cedar
principal tags — the forbid admits the project by client id, by scope *or* by group,
so whichever the gateway surfaces works; tools of an MCP-server target that carries
no static tool schema cannot be listed from the control plane, so such a target is
reported `uncovered` (its tools reach the forbid once the §15 review scaffold lists
them).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project, project_context
from app.models.ledger import ResourceCreator
from app.models.project_isolation import (
    KIND_CEDAR_POLICY,
    KIND_M2M_CLIENT,
    KIND_M2M_PROVIDER,
    STATUS_FAILED,
    STATUS_READY,
    ProjectResource,
)
from app.models.projects import Project
from app.services import gateway_bootstrap
from app.services import project_isolation as isolation

logger = logging.getLogger(__name__)

# `resource_creators.kind` of a Connection-bound gateway target (§10.6-3)
TARGET_KIND = "gateway_target"
PROJECT_SCOPE_PREFIX = "project."


def project_scope_name(project_id: str) -> str:
    return f"{PROJECT_SCOPE_PREFIX}{project_id}"


def project_scope(project_id: str) -> str:
    """`launchpad-gw/project.<pid>` — the scope fallback for `client_id` (§10.6-4)."""
    return f"{gateway_bootstrap.GATEWAY_RESOURCE_SERVER}/{project_scope_name(project_id)}"


# ── what the ledger says ──────────────────────────────────────────────────────


def _rows(db: Session, project_id: str, workspace_id: str) -> dict[str, ProjectResource]:
    with project_context(None):
        rows = db.scalars(select(ProjectResource).where(
            ProjectResource.project_id == project_id,
            ProjectResource.workspace_id == workspace_id,
            ProjectResource.kind.in_((KIND_M2M_CLIENT, KIND_M2M_PROVIDER, KIND_CEDAR_POLICY)),
        ))
        return {row.kind: row for row in rows}


def _isolated(db: Session, project_id: str | None) -> bool:
    if not project_id:
        return False
    with project_context(None):
        return isolation.is_isolated(db.get(Project, project_id))


def _principal_of(rows: dict[str, ProjectResource], project_id: str) -> dict[str, str] | None:
    client = rows.get(KIND_M2M_CLIENT)
    provider = rows.get(KIND_M2M_PROVIDER)
    if not (client and provider and client.status == STATUS_READY
            and provider.status == STATUS_READY and client.resource_id and provider.arn):
        return None
    return {
        "client_id": client.resource_id,
        "scope": project_scope(project_id),
        "group": isolation.resource_names(project_id).cognito_group,
        "provider_arn": provider.arn,
    }


def project_principal(project_id: str, workspace_id: str,
                      db: Session | None = None) -> dict[str, str] | None:
    """The isolated project's gateway principal in the workspace — `client_id`,
    `scope`, `group`, `provider_arn` — once its `m2m` stage is ready; else None (a
    shared project, or one not provisioned yet: its agents use the workspace client)."""
    own = db is None
    db = db or SessionLocal()
    try:
        if not _isolated(db, project_id):
            return None
        return _principal_of(_rows(db, project_id, workspace_id), project_id)
    finally:
        if own:
            db.close()


def cedar_active(db: Session, project_id: str, workspace_id: str) -> bool:
    """The project's per-target Cedar rules are in force in the workspace: isolated,
    principal ready, and the last `cedar` run (stage or reconcile) succeeded."""
    if not _isolated(db, project_id):
        return False
    rows = _rows(db, project_id, workspace_id)
    cedar = rows.get(KIND_CEDAR_POLICY)
    return (_principal_of(rows, project_id) is not None and cedar is not None
            and cedar.status == STATUS_READY)


def infra_lowered(db: Session, workspace_id: str) -> bool:
    """§10.6-6: every active project of the workspace is isolated with its Cedar in
    force, so a project's own gateway targets no longer reshape what another runs on.
    False for a workspace with one project (it never needed lowering) or none."""
    from app.services import access

    active = [b.project_id for b in access.workspace_bindings(db, workspace_id)
              if b.status == "active"]
    return len(active) > 1 and all(cedar_active(db, pid, workspace_id) for pid in active)


def owned_target_ids(db: Session, workspace_id: str, project_id: str) -> set[str]:
    with project_context(None):
        return set(db.scalars(select(ResourceCreator.resource_id).where(
            ResourceCreator.workspace_id == workspace_id,
            ResourceCreator.kind == TARGET_KIND,
            ResourceCreator.project_id == project_id,
        )))


def target_owner(db: Session, target_id: str) -> tuple[str, str | None] | None:
    """(workspace, project) of a console-created target, or None. Target ids are
    unique per gateway and random, so the id alone finds the row."""
    with project_context(None):
        row = db.execute(select(ResourceCreator.workspace_id, ResourceCreator.project_id).where(
            ResourceCreator.kind == TARGET_KIND, ResourceCreator.resource_id == target_id,
        )).first()
    return (row[0], row[1]) if row else None


def require_own_target(db: Session, workspace_id: str, target_id: str) -> None:
    """404 for a target of another project (an id with no row is the workspace default
    project's) — the guard of a lowered delete (§10.6-6)."""
    from app.services import creators

    if creators.outside_current_project(db, workspace_id, TARGET_KIND, [target_id]):
        raise NotFoundError("identity.target_not_found", f"no gateway target {target_id!r}")


# ── what a deploy reads ───────────────────────────────────────────────────────


def effective_resources(resources: dict[str, Any], project_id: str | None,
                        workspace_id: str | None) -> dict[str, Any]:
    """The workspace resource map as an agent of `project_id` sees it: an isolated,
    provisioned project's agents take gateway tokens from the project's provider
    with both scopes (`gateway_scopes`). Any other project gets `resources` itself —
    single-project workspaces are unchanged."""
    if not (project_id and workspace_id):
        return resources
    principal = project_principal(project_id, workspace_id)
    if principal is None:
        return resources
    return {**resources, "oauth_provider_arn": principal["provider_arn"],
            "gateway_scopes": [gateway_bootstrap.GATEWAY_SCOPE, principal["scope"]],
            "gateway_project": project_id}


def gateway_scopes(resources: dict[str, Any]) -> list[str]:
    """The scopes an agent asks its gateway token for (`effective_resources`)."""
    scopes = resources.get("gateway_scopes")
    return list(scopes) if scopes else [gateway_bootstrap.GATEWAY_SCOPE]


def project_groups_for(db: Session, username: str, role: str, workspace_id: str,
                       ) -> list[str]:
    """The `lp-project-<pid>` groups a console user's policy token carries in the
    workspace: their memberships among the isolated projects whose principal is
    provisioned there (the group exists only then). An administrator — who passes
    every project's data — carries the current project's group as well."""
    from app.services import access

    found = db.execute(text("SELECT id FROM users WHERE username_key = :u"),
                       {"u": username.lower()}).first()
    projects: set[str] = set()
    if found:
        snapshot = access.load_snapshot(db, found[0], role, None)
        projects = {pid for pid, m in snapshot.memberships.items()
                    if m.status == "active" and workspace_id in m.workspaces}
    if role == "admin" and current_project():
        projects.add(str(current_project()))
    return sorted(
        isolation.resource_names(pid).cognito_group for pid in projects
        if project_principal(pid, workspace_id, db) is not None
    )


# ── Cognito (the m2m stage) ───────────────────────────────────────────────────


def _code(exc: Exception) -> str:
    if not isinstance(exc, ClientError):
        return ""
    return str(exc.response.get("Error", {}).get("Code", ""))


def ensure_project_scope(cognito: Any, pool_id: str, project_id: str) -> bool:
    """Add `project.<pid>` to the gateway resource server. UpdateResourceServer
    replaces the scope list, so the existing ones are echoed. Returns changed."""
    server = cognito.describe_resource_server(
        UserPoolId=pool_id, Identifier=gateway_bootstrap.GATEWAY_RESOURCE_SERVER,
    )["ResourceServer"]
    scopes = list(server.get("Scopes") or [])
    name = project_scope_name(project_id)
    if any(s.get("ScopeName") == name for s in scopes):
        return False
    scopes.append({"ScopeName": name,
                   "ScopeDescription": f"Launchpad project {project_id} gateway tools"})
    cognito.update_resource_server(
        UserPoolId=pool_id, Identifier=server.get("Identifier")
        or gateway_bootstrap.GATEWAY_RESOURCE_SERVER,
        Name=server.get("Name") or gateway_bootstrap.GATEWAY_RESOURCE_SERVER, Scopes=scopes,
    )
    return True


def ensure_project_group(cognito: Any, pool_id: str, group: str, project_id: str) -> bool:
    try:
        cognito.create_group(UserPoolId=pool_id, GroupName=group,
                             Description=f"Launchpad project: {project_id}")
    except ClientError as exc:
        if _code(exc) != "GroupExistsException":
            raise
        return False
    return True


def _find_client(cognito: Any, pool_id: str, name: str) -> str | None:
    token = None
    while True:
        kwargs = {"UserPoolId": pool_id, "MaxResults": 60} | (
            {"NextToken": token} if token else {})
        page = cognito.list_user_pool_clients(**kwargs)
        for client in page.get("UserPoolClients", []):
            if client.get("ClientName") == name:
                return str(client["ClientId"])
        token = page.get("NextToken")
        if not token:
            return None


def ensure_project_client(cognito: Any, pool_id: str, name: str, scopes: list[str],
                          ) -> tuple[str, bool]:
    """The project's client-credentials app client (create-if-missing by name, the
    workspace M2M client's settings); an existing one missing a scope gets it back."""
    existing = _find_client(cognito, pool_id, name)
    if existing is None:
        created = cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=name,
            GenerateSecret=True,  # the OAuth provider reads this secret
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthScopes=scopes,
            SupportedIdentityProviders=["COGNITO"],
            AccessTokenValidity=1,
            TokenValidityUnits={"AccessToken": "hours"},
        )["UserPoolClient"]
        return str(created["ClientId"]), True
    client = cognito.describe_user_pool_client(UserPoolId=pool_id,
                                               ClientId=existing)["UserPoolClient"]
    have = list(client.get("AllowedOAuthScopes") or [])
    if not set(scopes) <= set(have):
        # UpdateUserPoolClient resets what it is not given: echo the client's settings
        cognito.update_user_pool_client(
            UserPoolId=pool_id, ClientId=existing, ClientName=name,
            AllowedOAuthFlows=client.get("AllowedOAuthFlows") or ["client_credentials"],
            AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthScopes=sorted(set(have) | set(scopes)),
            SupportedIdentityProviders=client.get("SupportedIdentityProviders") or ["COGNITO"],
            AccessTokenValidity=client.get("AccessTokenValidity") or 1,
            TokenValidityUnits=client.get("TokenValidityUnits") or {"AccessToken": "hours"},
        )
    return existing, False


# ── Cedar (the cedar stage and its reconcile) ─────────────────────────────────


def _cedar_refusal(gateway: dict[str, Any]) -> str | None:
    from app.services import governance

    if gateway.get("authorizerType") != "CUSTOM_JWT":
        return (f"the workspace gateway authorizes with {gateway.get('authorizerType')} — "
                "per-project tool rules need its Cognito JWT authorizer")
    engine_arn, mode = governance.gateway_enforcement(gateway)
    if not engine_arn:
        return ("the workspace gateway has no Policy Engine — attach one on the Governance "
                "page and switch the gateway to ENFORCE, then retry")
    if mode != "ENFORCE":
        return (f"the workspace gateway runs Cedar in {mode or 'no'} mode — switch it to "
                "ENFORCE on the Governance page (per-project tool rules are not enforced "
                "otherwise), then retry")
    return None


def reconcile_cedar(workspace: Any, project_id: str, *, client_id: str, scope: str,
                    previous: Iterable[str] = (), control: Any = None, iam: Any = None,
                    log: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Bring the project's `lp_proj_*` forbids on the workspace gateway to the targets
    it owns now: one per target with known tools, updated when its tools changed,
    deleted when the target is gone (`previous` = the names the last run wrote).
    Raises `ProvisionError` with the operator's next step."""
    from app.services import governance
    from app.services.agentcore import policy as policy_api
    from app.services.agentcore.client import control_client

    control = control or control_client(workspace)
    gateway_id = str(workspace.resources.get("gateway_id") or "")
    if not gateway_id:
        raise isolation.ProvisionError("the workspace has no gateway (gateway_id) — "
                                       "run its bootstrap first")
    gateway = policy_api.get_gateway(control, gateway_id)
    refusal = _cedar_refusal(gateway)
    if refusal:
        raise isolation.ProvisionError(refusal)
    db = SessionLocal()
    try:
        owned = owned_target_ids(db, workspace.id, project_id)
    finally:
        db.close()
    wanted: dict[str, str | None] = {}
    uncovered: list[str] = []
    targets = [t for t in policy_api.list_gateway_targets(control, gateway_id)
               if t.get("targetId") in owned]
    for summary in targets:
        detail = policy_api.get_gateway_target(control, gateway_id, summary["targetId"])
        name = str(detail.get("name") or summary.get("name") or "")
        tools = [a["name"].split("___", 1)[1] for a in governance.discover_actions([detail])]
        if not tools:
            uncovered.append(name)
            continue
        wanted[governance.project_policy_name(project_id, name)] = (
            governance.project_forbid_statement(
                gateway, name, tools, project_id=project_id, client_id=client_id, scope=scope,
            )
        )
    for stale in previous:
        wanted.setdefault(stale, None)
    try:
        plan = governance.project_cedar_plan(control, gateway_id, wanted)
        results = governance.apply_mcp_scaffold(
            workspace, gateway_id, plan["steps"], target_name=f"project {project_id}",
            acknowledged_gateway_ids=plan["acknowledged_gateway_ids"], control=control,
            iam=iam, description=f"Launchpad project {project_id}: tools of its gateway "
                                 "targets are refused to other projects (projects §10.6)",
        )
    except AppError as exc:
        raise isolation.ProvisionError(f"{exc.code}: {exc.message}") from exc
    failed = next((r for r in results if r["status"] != "succeeded"), None)
    if failed:
        raise isolation.ProvisionError(
            f"Cedar policy {failed['name']} ({failed['action']}) failed: {failed['error']}")
    for r in results:
        log(f"{r['action']} {r['name']}")
    if uncovered:
        log("no tool list for " + ", ".join(sorted(uncovered)) + " — not covered yet")
    return {
        "gateway_id": gateway_id,
        "engine_id": plan["engine_id"],
        "engine_arn": plan["engine_arn"],
        "mode": plan["mode"],
        "policies": sorted(n for n, s in wanted.items() if s is not None),
        "uncovered": sorted(uncovered),
        "principal": {"client_id": client_id, "scope": scope,
                      "group": isolation.resource_names(project_id).cognito_group},
    }


def _cedar_detail(result: dict[str, Any]) -> str:
    out = f"{len(result['policies'])} project rule(s) on {result['gateway_id']} (ENFORCE)"
    if result["uncovered"]:
        out += f"; no tool list yet: {', '.join(result['uncovered'])}"
    return out


# One reconcile at a time per (project, workspace): a create and a delete in quick
# succession must not diff against the same `previous`.
_RECONCILE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(project_id: str, workspace_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _RECONCILE_LOCKS.setdefault((project_id, workspace_id), threading.Lock())


def reconcile_now(project_id: str, workspace_id: str, *, workspace: Any = None,
                  control: Any = None, wait_target: str | None = None) -> bool:
    """Re-run the project's Cedar after one of its targets was created or deleted,
    inside `project_context(project_id)`. A failure marks the `cedar_policy` row
    failed — so `infra_lowered` turns off again (fail closed) until a provisioning
    retry succeeds. Returns whether the rules are in force. Never raises."""
    with project_context(project_id), _lock_for(project_id, workspace_id):
        db = SessionLocal()
        try:
            rows = _rows(db, project_id, workspace_id)
            principal = _principal_of(rows, project_id)
            row = rows.get(KIND_CEDAR_POLICY)
            if principal is None or row is None or not _isolated(db, project_id):
                return False
            previous = list((row.detail or {}).get("policies") or [])
            try:
                if workspace is None:
                    from app.services.workspace import context_for_workspace

                    workspace = context_for_workspace(workspace_id)
                if wait_target:
                    _wait_target(workspace, control, wait_target)
                result = reconcile_cedar(workspace, project_id,
                                         client_id=principal["client_id"],
                                         scope=principal["scope"], previous=previous,
                                         control=control)
            except Exception as exc:  # fail closed, never crash the caller's thread
                logger.warning("project %s: Cedar reconcile in %s failed: %s",
                               project_id, workspace_id, exc)
                row.status = STATUS_FAILED
                row.error = f"{type(exc).__name__}: {exc}"[:2000]
                db.commit()
                return False
            row.detail = result
            row.resource_id = result["engine_id"]
            row.arn = result["engine_arn"]
            row.status = STATUS_READY
            row.error = None
            db.commit()
            return True
        finally:
            db.close()


def _wait_target(workspace: Any, control: Any, target_id: str) -> None:
    """Cedar validates actions against the gateway's synced schema, so a new target's
    rule waits for it to be READY (a failed target simply gets no rule)."""
    from app.services.agentcore.client import control_client

    control = control or control_client(workspace)
    try:
        gateway_bootstrap._wait_target_ready(
            control, str(workspace.resources.get("gateway_id") or ""), target_id)
    except (RuntimeError, TimeoutError, ClientError, BotoCoreError) as exc:
        logger.info("target %s not READY (%s); reconciling without waiting", target_id, exc)


def schedule_reconcile(project_id: str, workspace_id: str, *,
                       wait_target: str | None = None) -> threading.Thread | None:
    """`reconcile_now` on a daemon thread; None when the project has no Cedar to keep."""
    db = SessionLocal()
    try:
        if not _isolated(db, project_id) or KIND_CEDAR_POLICY not in _rows(
                db, project_id, workspace_id):
            return None
    finally:
        db.close()
    thread = threading.Thread(
        target=reconcile_now, args=(project_id, workspace_id),
        kwargs={"wait_target": wait_target}, daemon=True,
        name=f"proj-cedar-{project_id[:16]}",
    )
    thread.start()
    return thread


# ── the provisioner ───────────────────────────────────────────────────────────


class ProjectGatewayProvisioner:
    """`m2m` and `cedar` of `project_provision` (§10.6). Idempotent: every piece is
    found by name before it is made, and recorded the moment it exists."""

    def ensure_m2m(self, ctx: isolation.ProvisionContext) -> str:
        resources = ctx.workspace.resources
        pool_id = str(resources.get("user_pool_id") or "")
        gateway_id = str(resources.get("gateway_id") or "")
        if not (pool_id and gateway_id):
            raise isolation.ProvisionError(
                "the workspace has no user pool or gateway (user_pool_id / gateway_id) — "
                "run its bootstrap first")
        cognito = ctx.workspace.client("cognito-idp")
        control = ctx.workspace.client("bedrock-agentcore-control")
        names, pid = ctx.names, ctx.project_id
        scopes = [gateway_bootstrap.GATEWAY_SCOPE, project_scope(pid)]
        if ensure_project_scope(cognito, pool_id, pid):
            ctx.log(f"scope {scopes[1]} added")
        if ensure_project_group(cognito, pool_id, names.cognito_group, pid):
            ctx.log(f"group {names.cognito_group} created")
        client_id, created = ensure_project_client(cognito, pool_id, names.m2m_client, scopes)
        ctx.record(isolation.ProvisionedResource(
            KIND_M2M_CLIENT, resource_id=client_id,
            detail={"name": names.m2m_client, "user_pool_id": pool_id, "scopes": scopes,
                    "group": names.cognito_group},
        ))
        ctx.log(f"client {names.m2m_client} {'created' if created else 'reused'}")
        arn, provider_created = gateway_bootstrap.ensure_oauth_provider(
            control, cognito, user_pool_id=pool_id, m2m_client_id=client_id,
            region=ctx.workspace.region, name=names.m2m_provider,
        )
        ctx.record(isolation.ProvisionedResource(
            KIND_M2M_PROVIDER, resource_id=names.m2m_provider, arn=arn,
            detail={"client_id": client_id},
        ))
        ctx.log(f"provider {names.m2m_provider} {'created' if provider_created else 'reused'}")
        gateways = [gateway_id] + ([str(resources["kb_gateway_id"])]
                                   if resources.get("kb_gateway_id") else [])
        for gid in gateways:
            if gateway_bootstrap.ensure_gateway_allows_client(control, gid, client_id):
                ctx.log(f"client allowed on gateway {gid}")
        return f"{client_id} · {names.m2m_provider} · allowed on {len(gateways)} gateway(s)"

    def ensure_cedar(self, ctx: isolation.ProvisionContext) -> str:
        client = ctx.resources.get(KIND_M2M_CLIENT) or {}
        if not client.get("resource_id"):
            raise isolation.ProvisionError("the project's M2M client is not recorded — "
                                           "the m2m stage must succeed first")
        previous = ((ctx.resources.get(KIND_CEDAR_POLICY) or {}).get("detail") or {}).get(
            "policies") or []
        with _lock_for(ctx.project_id, ctx.workspace_id):
            result = reconcile_cedar(
                ctx.workspace, ctx.project_id, client_id=str(client["resource_id"]),
                scope=project_scope(ctx.project_id), previous=previous, log=ctx.log,
                control=ctx.workspace.client("bedrock-agentcore-control"),
                iam=ctx.workspace.client("iam"),
            )
        ctx.record(isolation.ProvisionedResource(
            KIND_CEDAR_POLICY, resource_id=result["engine_id"], arn=result["engine_arn"],
            detail=result,
        ))
        return _cedar_detail(result)


isolation.set_provisioners(gateway=ProjectGatewayProvisioner())
