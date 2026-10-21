"""Cost tags on every platform-created resource (cost-tags design §1.1–§1.3, CT-1).

`project_telemetry.standard_tags` is the tag set; this module resolves it for a
project (its department and `settings.cost_tags`, read hub-global), writes it with each
service's own TagResource shape, and back-fills what predates it:

* **create / update** — the runtime and harness deployers, memory, knowledge base,
  workspace bootstrap and agent role code ask `tags_for` / `agent_tags` /
  `context_tags` / `runtime_tags` for the map they send on create, and tag an
  existing resource best-effort (`tag_quietly`) — a refusal never fails a deploy.
* **back-fill** — the resumable Job `cost_tags_backfill` (one per workspace, or per
  project / agents after a settings change or a transfer): `plan` records every
  resource of the ledger's agents and the workspace's infrastructure in the job
  payload, `tag` writes each one and records its result (`tagged` / `unsupported` /
  `denied` / `missing` / `failed`), so a restart continues with the first resource not
  yet written and `coverage` shows what still lacks a permission. TagResource is
  idempotent, so a re-run is too.

Unverified live (marked so the result reads `unsupported` rather than `failed` on a
validation error): TagResource on gateway targets and credential providers. Bedrock
data sources carry no tags at all — their cost reports under the knowledge base. S3
objects are not tagged (cost is per bucket, design §1.4). A key removed from
`cost_tags` is not untagged from resources already carrying it.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import NOT_FOUND_CODES, aws_error_code, aws_error_message
from app.core.project_scope import current_project, project_context
from app.services import project_telemetry as pt

logger = logging.getLogger(__name__)

JOB_TYPE = "cost_tags_backfill"
STAGES = ("plan", "tag")

# per-resource results
PENDING = "pending"
TAGGED = "tagged"
UNSUPPORTED = "unsupported"
DENIED = "denied"
MISSING = "missing"
FAILED = "failed"
RESULTS = (TAGGED, UNSUPPORTED, DENIED, MISSING, FAILED)

# resource kinds
RUNTIME = "runtime"
RUNTIME_ENDPOINT = "runtime_endpoint"
HARNESS = "harness"
MEMORY = "memory"
GATEWAY = "gateway"
GATEWAY_TARGET = "gateway_target"
CREDENTIAL_PROVIDER = "credential_provider"
KNOWLEDGE_BASE = "knowledge_base"
DATA_SOURCE = "data_source"
IAM_ROLE = "iam_role"
ECR_REPOSITORY = "ecr_repository"
CODEBUILD_PROJECT = "codebuild_project"
LOG_GROUP = "log_group"
SECRET = "secret"
AGENTCORE_KINDS = frozenset({RUNTIME, RUNTIME_ENDPOINT, HARNESS, MEMORY, GATEWAY,
                             GATEWAY_TARGET, CREDENTIAL_PROVIDER})
# TagResource support not yet seen live: a validation error reads as unsupported
UNVERIFIED_KINDS = frozenset({GATEWAY_TARGET, CREDENTIAL_PROVIDER})

RUNTIME_LOG_PREFIX = "/aws/bedrock-agentcore/runtimes/"
_DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                           "UnauthorizedException", "AuthorizationError"})
_UNSUPPORTED_CODES = frozenset({"UnsupportedOperation", "UnsupportedOperationException",
                                "OperationNotSupportedException", "InvalidAction",
                                "UnknownOperationException"})
_VALIDATION_CODES = frozenset({"ValidationException", "InvalidParameterException",
                               "InvalidRequestException", "InvalidInputException"})
_ACTIVE = ("queued", "running")


# ── the tag set ───────────────────────────────────────────────────────────────


def _project_inputs(project_id: str, db: Session | None = None) -> tuple[str | None,
                                                                         dict[str, Any]]:
    """(department id, `settings.cost_tags`) of a project, hub-global; fail-soft —
    a read error leaves the optional tags out rather than failing a create."""
    from app.models.projects import Project

    own = db is None
    session = SessionLocal() if own else db
    try:
        with project_context(None):
            row = session.get(Project, project_id)
            if row is None:
                return None, {}
            custom = (row.settings or {}).get("cost_tags")
            return row.department_id, dict(custom) if isinstance(custom, dict) else {}
    except Exception as exc:  # noqa: BLE001 — tags are attribution, never a blocker
        logger.debug("cost tags of project %s unreadable: %s", project_id, exc)
        return None, {}
    finally:
        if own:
            session.close()


def tags_for(project_id: str | None, *, workspace_id: str | None = None,
             agent_id: str | None = None, db: Session | None = None) -> dict[str, str]:
    """The standard tags of a resource of `project_id` (None = shared infrastructure)."""
    if not project_id or project_id == pt.SHARED_PROJECT:
        return pt.standard_tags(None, workspace_id=workspace_id, agent_id=agent_id)
    department, custom = _project_inputs(project_id, db)
    return pt.standard_tags(project_id, workspace_id=workspace_id, agent_id=agent_id,
                            department_id=department, custom=custom)


def agent_project(agent: Any) -> str | None:
    """The project an agent's resources are filed under: a system preset is platform
    level (`shared`), every other agent its own project."""
    if getattr(agent, "system_key", None):
        return None
    return getattr(agent, "project_id", None)


def agent_tags(agent: Any, workspace_id: str | None = None,
               db: Session | None = None) -> dict[str, str]:
    """The tags of an agent-level resource (runtime, harness, role, log group…)."""
    return tags_for(agent_project(agent),
                    workspace_id=workspace_id or getattr(agent, "workspace_id", None),
                    agent_id=getattr(agent, "id", None), db=db)


def context_tags(workspace_id: str | None = None,
                 agent_id: str | None = None) -> dict[str, str]:
    """The tags of a resource created now, for the current project (a request's, or
    the job's `project_context`); none current = shared infrastructure."""
    return tags_for(current_project(), workspace_id=workspace_id, agent_id=agent_id)


def runtime_tags(environment: Mapping[str, Any] | None,
                 explicit: Mapping[str, str] | None = None) -> dict[str, str]:
    """CreateAgentRuntime / TagResource tags of a runtime: the standard set of the
    project the environment's span attributes name (an isolated project's agent) or
    else of the deploy's current project, the attribute-derived ids, then the
    caller's explicit tags (platform code: the deployer's `agent_tags`)."""
    derived = pt.tags_from_environment(environment)
    base = tags_for(derived.get(pt.TAG_PROJECT) or current_project(),
                    workspace_id=derived.get(pt.TAG_WORKSPACE),
                    agent_id=derived.get(pt.TAG_AGENT))
    return {**base, **derived, **dict(explicit or {})}


def tag_list(tags: Mapping[str, str], key: str = "Key", value: str = "Value",
             ) -> list[dict[str, str]]:
    """The `[{Key, Value}]` shape IAM / ECR / Secrets Manager (`key`/`value`: CodeBuild,
    Bedrock inference profiles) take."""
    return [{key: k, value: v} for k, v in tags.items()]


# ── writing tags ──────────────────────────────────────────────────────────────


def classify(exc: BaseException, kind: str = "") -> tuple[str, str]:
    """(result, detail) of a failed tag call."""
    if isinstance(exc, ClientError):
        code, message = aws_error_code(exc), aws_error_message(exc)
        detail = f"{code or 'ClientError'}: {message}"
        if code in _DENIED_CODES or "not authorized" in message.lower():
            return DENIED, detail
        if code in NOT_FOUND_CODES or code == "NoSuchEntityException":
            return MISSING, detail
        if code in _UNSUPPORTED_CODES or (kind in UNVERIFIED_KINDS
                                          and code in _VALIDATION_CODES):
            return UNSUPPORTED, detail
        return FAILED, detail
    if isinstance(exc, AttributeError):  # a client without the operation
        return UNSUPPORTED, f"{type(exc).__name__}: {exc}"
    return FAILED, f"{type(exc).__name__}: {exc}"


def _resolve_secret(workspace: Any, entry: dict[str, Any]) -> str:
    """The token-vault secret behind a Connection (its provider's `*SecretArn`)."""
    control = workspace.client("bedrock-agentcore-control")
    name = entry["name"]
    if entry.get("provider_kind") == "api_key":
        found = control.get_api_key_credential_provider(name=name)
        return str((found.get("apiKeySecretArn") or {}).get("secretArn") or "")
    found = control.get_oauth2_credential_provider(name=name)
    return str((found.get("clientSecretArn") or {}).get("secretArn") or "")


class TagWriter(Protocol):
    def apply(self, workspace: Any, entry: dict[str, Any],
              tags: dict[str, str]) -> tuple[str, str]: ...


class AwsTagWriter:
    """One resource's tags through its service's own call; never raises."""

    def apply(self, workspace: Any, entry: dict[str, Any],
              tags: dict[str, str]) -> tuple[str, str]:
        kind = entry["kind"]
        try:
            return self._apply(workspace, entry, tags)
        except (ClientError, BotoCoreError, AttributeError) as exc:
            return classify(exc, kind)
        except Exception as exc:  # noqa: BLE001 — recorded per resource, never raised
            return classify(exc, kind)

    def _apply(self, workspace: Any, entry: dict[str, Any],
               tags: dict[str, str]) -> tuple[str, str]:
        kind = entry["kind"]
        arn = entry.get("arn") or ""
        if kind in AGENTCORE_KINDS:
            workspace.client("bedrock-agentcore-control").tag_resource(
                resourceArn=arn, tags=tags)
        elif kind == KNOWLEDGE_BASE:
            workspace.client("bedrock-agent").tag_resource(resourceArn=arn, tags=tags)
        elif kind == DATA_SOURCE:
            return UNSUPPORTED, ("Bedrock data sources carry no tags — their cost "
                                 "reports under the knowledge base")
        elif kind == IAM_ROLE:
            workspace.client("iam").tag_role(RoleName=entry["name"], Tags=tag_list(tags))
        elif kind == ECR_REPOSITORY:
            workspace.client("ecr").tag_resource(resourceArn=arn, tags=tag_list(tags))
        elif kind == LOG_GROUP:
            workspace.client("logs").tag_resource(resourceArn=arn, tags=tags)
        elif kind == CODEBUILD_PROJECT:
            return self._codebuild(workspace, entry["name"], tags)
        elif kind == SECRET:
            arn = arn or _resolve_secret(workspace, entry)
            if not arn:
                return MISSING, "the Connection has no secret in the token vault"
            entry["arn"] = arn
            workspace.client("secretsmanager").tag_resource(SecretId=arn,
                                                            Tags=tag_list(tags))
        else:
            return UNSUPPORTED, f"no tagging for resource kind '{kind}'"
        return TAGGED, ""

    def untag(self, workspace: Any, entry: dict[str, Any],
              keys: list[str]) -> tuple[str, str]:
        """Remove `keys` from one resource (tags a project change made stale: the old
        project's custom cost tags after a transfer, a key dropped from the project's
        set, a department cleared); never raises. A key the resource does not carry
        is not an error on any of these services."""
        try:
            return self._untag(workspace, entry, keys)
        except Exception as exc:  # noqa: BLE001 — recorded per resource, never raised
            return classify(exc, entry["kind"])

    @staticmethod
    def _untag(workspace: Any, entry: dict[str, Any], keys: list[str]) -> tuple[str, str]:
        kind = entry["kind"]
        arn = entry.get("arn") or ""
        if kind in AGENTCORE_KINDS:
            workspace.client("bedrock-agentcore-control").untag_resource(
                resourceArn=arn, tagKeys=keys)
        elif kind == KNOWLEDGE_BASE:
            workspace.client("bedrock-agent").untag_resource(resourceArn=arn, tagKeys=keys)
        elif kind == IAM_ROLE:
            workspace.client("iam").untag_role(RoleName=entry["name"], TagKeys=keys)
        elif kind == ECR_REPOSITORY:
            workspace.client("ecr").untag_resource(resourceArn=arn, tagKeys=keys)
        elif kind == LOG_GROUP:
            workspace.client("logs").untag_resource(resourceArn=arn, tagKeys=keys)
        elif kind == CODEBUILD_PROJECT:
            codebuild = workspace.client("codebuild")
            found = codebuild.batch_get_projects(names=[entry["name"]]).get("projects") or []
            if not found:
                return MISSING, f"CodeBuild project {entry['name']} not found"
            current = {t["key"]: t.get("value", "") for t in found[0].get("tags") or []}
            kept = {k: v for k, v in current.items() if k not in keys}
            if kept != current:
                codebuild.update_project(name=entry["name"],
                                         tags=tag_list(kept, "key", "value"))
        elif kind == SECRET and arn:
            workspace.client("secretsmanager").untag_resource(SecretId=arn, TagKeys=keys)
        else:
            return UNSUPPORTED, f"no untagging for resource kind '{kind}'"
        return TAGGED, ""

    @staticmethod
    def _codebuild(workspace: Any, name: str, tags: dict[str, str]) -> tuple[str, str]:
        """CodeBuild has no TagResource: UpdateProject replaces the project's tags, so
        the current ones are read and merged first."""
        codebuild = workspace.client("codebuild")
        found = codebuild.batch_get_projects(names=[name]).get("projects") or []
        if not found:
            return MISSING, f"CodeBuild project {name} not found"
        current = {t["key"]: t.get("value", "") for t in found[0].get("tags") or []}
        merged = {**current, **tags}
        if merged != current:
            codebuild.update_project(name=name, tags=tag_list(merged, "key", "value"))
        return TAGGED, ""


def tag_quietly(workspace: Any, entry: dict[str, Any], tags: dict[str, str],
                log: Callable[[str], None] = lambda _m: None,
                writer: TagWriter | None = None) -> str:
    """Best-effort tags on one resource at create / update time: the result, logged
    when it is not `tagged`; never raises (attribution must not fail the work)."""
    result, detail = (writer or AwsTagWriter()).apply(workspace, entry, tags)
    if result != TAGGED:
        log(f"cost tags on {entry.get('arn') or entry.get('name')}: {result}"
            f"{' — ' + detail if detail else ''}")
    return result


def entry(kind: str, *, arn: str | None = None, name: str | None = None,
          project_id: str | None = None, agent_id: str | None = None,
          status: str = PENDING, detail: str = "", **extra: Any) -> dict[str, Any]:
    """One resource of a back-fill run (also the shape `tag_quietly` takes)."""
    ident = arn or name or ""
    return {"key": f"{kind}:{ident}", "kind": kind, "arn": arn or "", "name": name or "",
            "project_id": project_id, "agent_id": agent_id, "status": status,
            "detail": detail, **extra}


# ── the inventory (plan) ──────────────────────────────────────────────────────


class Discovery(Protocol):
    def runtime_endpoints(self, workspace: Any, runtime_id: str) -> list[str]: ...

    def log_groups(self, workspace: Any, prefix: str) -> list[str]: ...

    def knowledge_bases(self, workspace: Any) -> list[tuple[str, list[str]]]: ...


def _pages(call: Callable[..., dict[str, Any]], key: str, token_key: str = "nextToken",
           **kwargs: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        page = call(**kwargs)
        out.extend(page.get(key) or [])
        token = page.get(token_key)
        if not token:
            return out
        kwargs[token_key] = token


class AwsDiscovery:
    """The resources the ledger does not name: a runtime's endpoints and log groups,
    the workspace's managed knowledge bases and their data sources."""

    def runtime_endpoints(self, workspace: Any, runtime_id: str) -> list[str]:
        control = workspace.client("bedrock-agentcore-control")
        found = _pages(control.list_agent_runtime_endpoints, "runtimeEndpoints",
                       agentRuntimeId=runtime_id)
        return [str(e["agentRuntimeEndpointArn"]) for e in found
                if e.get("agentRuntimeEndpointArn")]

    def log_groups(self, workspace: Any, prefix: str) -> list[str]:
        logs = workspace.client("logs")
        found = _pages(logs.describe_log_groups, "logGroups", logGroupNamePrefix=prefix)
        return [str(g.get("logGroupArn") or "") or _log_group_arn(workspace, g["logGroupName"])
                for g in found if g.get("logGroupName")]

    def knowledge_bases(self, workspace: Any) -> list[tuple[str, list[str]]]:
        client = workspace.client("bedrock-agent")
        out = []
        for summary in _pages(client.list_knowledge_bases, "knowledgeBaseSummaries",
                              maxResults=100):
            kb_id = summary["knowledgeBaseId"]
            detail = client.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
            if (detail.get("knowledgeBaseConfiguration") or {}).get("type") != "MANAGED":
                continue  # the console manages MANAGED KBs only (services/knowledge.py)
            sources = _pages(client.list_data_sources, "dataSourceSummaries",
                             knowledgeBaseId=kb_id)
            out.append((kb_id, [str(s["dataSourceId"]) for s in sources]))
        return out


def _arn(workspace: Any, service: str, resource: str, *, region: bool = True) -> str:
    return (f"arn:{workspace.partition}:{service}:{workspace.region if region else ''}:"
            f"{workspace.account_id}:{resource}")


def _log_group_arn(workspace: Any, name: str) -> str:
    return _arn(workspace, "logs", f"log-group:{name}")


def _role_name(arn_or_name: str) -> str:
    return str(arn_or_name).rsplit("/", 1)[-1]


@dataclass
class Scope:
    """What a run covers: the whole workspace (both None), one project's resources,
    or some agents' resources (a transfer)."""

    project_id: str | None = None
    agent_ids: list[str] | None = None

    @property
    def workspace_wide(self) -> bool:
        return self.project_id is None and not self.agent_ids

    def covers_project(self, project_id: str | None) -> bool:
        if self.agent_ids:
            return False  # agent runs cover the agents' own resources only
        return self.project_id is None or project_id == self.project_id

    def as_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "agent_ids": self.agent_ids}


def _discovered(add: Callable[[dict[str, Any]], None], kind: str, what: str,
                project_id: str | None, agent_id: str | None,
                call: Callable[[], Iterable[str]]) -> None:
    """Record what a discovery call found; a refused call becomes one entry with its
    result, so the coverage names the missing permission."""
    try:
        for arn in call():
            add(entry(kind, arn=arn, project_id=project_id, agent_id=agent_id))
    except Exception as exc:  # noqa: BLE001 — recorded, the run goes on
        result, detail = classify(exc, kind)
        add(entry(kind, name=f"{what} (listing)", project_id=project_id, agent_id=agent_id,
                  status=result, detail=detail))


def _default_project(db: Session, workspace_id: str) -> str | None:
    from app.models.projects import ProjectWorkspace

    return db.scalar(select(ProjectWorkspace.project_id).where(
        ProjectWorkspace.workspace_id == workspace_id,
        ProjectWorkspace.is_workspace_default.is_(True)))


def plan_resources(db: Session, workspace: Any, scope: Scope,
                   discovery: Discovery | None = None) -> list[dict[str, Any]]:
    """Every resource the run tags, from the ledger plus `discovery`."""
    from app.core.config import get_settings
    from app.models.lambda_tools import ExternalMcpTarget, LambdaToolTarget
    from app.models.ledger import Agent, IdentityProvider, ManagedMemory, ResourceCreator
    from app.models.project_isolation import (
        KIND_ECR_REPO,
        KIND_GATEWAY,
        KIND_KB_ROLE,
        KIND_MEMORY,
        ProjectResource,
    )
    from app.services import creators
    from app.services.agent_iam import role_name_for

    discovery = discovery or AwsDiscovery()
    ws_id = workspace.id
    found: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any]) -> None:
        found.setdefault(item["key"], item)

    with project_context(None):
        query = select(Agent).where(Agent.workspace_id == ws_id, Agent.status != "deleted",
                                    Agent.method != "discovered")
        if scope.agent_ids:
            query = query.where(Agent.id.in_(list(scope.agent_ids)))
        elif scope.project_id:
            query = query.where(Agent.project_id == scope.project_id,
                                Agent.system_key.is_(None))
        agents = list(db.scalars(query.order_by(Agent.name)))
        per_agent_roles = get_settings().per_agent_execution_roles
        for agent in agents:
            pid, aid = agent_project(agent), agent.id
            if agent.arn:
                add(entry(HARNESS if agent.method == "harness" else RUNTIME, arn=agent.arn,
                          project_id=pid, agent_id=aid))
            if agent.method != "harness" and agent.resource_id:
                rid = agent.resource_id
                _discovered(add, RUNTIME_ENDPOINT, f"endpoints of {rid}", pid, aid,
                            lambda rid=rid: discovery.runtime_endpoints(workspace, rid))
                _discovered(add, LOG_GROUP, f"{RUNTIME_LOG_PREFIX}{rid}-*", pid, aid,
                            lambda rid=rid: discovery.log_groups(
                                workspace, f"{RUNTIME_LOG_PREFIX}{rid}-"))
            if per_agent_roles:
                add(entry(IAM_ROLE, name=role_name_for(agent.name, agent.id),
                          project_id=pid, agent_id=aid))
        if scope.agent_ids:
            return list(found.values())

        rows = db.scalars(select(ProjectResource).where(ProjectResource.workspace_id == ws_id))
        for row in rows:
            if not scope.covers_project(row.project_id):
                continue
            if row.kind == KIND_MEMORY and (row.arn or row.resource_id):
                add(entry(MEMORY, arn=row.arn or _arn(
                    workspace, "bedrock-agentcore", f"memory/{row.resource_id}"),
                    project_id=row.project_id))
            elif row.kind == KIND_GATEWAY and row.arn:
                add(entry(GATEWAY, arn=row.arn, project_id=row.project_id))
            elif row.kind == KIND_ECR_REPO and row.arn:
                add(entry(ECR_REPOSITORY, arn=row.arn, project_id=row.project_id))
            elif row.kind == KIND_KB_ROLE and row.resource_id:
                add(entry(IAM_ROLE, name=row.resource_id, project_id=row.project_id))
        for mem in db.scalars(select(ManagedMemory).where(ManagedMemory.workspace_id == ws_id)):
            if scope.covers_project(mem.project_id):
                add(entry(MEMORY, arn=_arn(workspace, "bedrock-agentcore",
                                           f"memory/{mem.memory_id}"),
                          project_id=mem.project_id))
        for model in (LambdaToolTarget, ExternalMcpTarget):
            for target in db.scalars(select(model).where(model.workspace_id == ws_id)):
                if target.target_id and target.gateway_id and scope.covers_project(
                        target.project_id):
                    add(entry(GATEWAY_TARGET, arn=_arn(
                        workspace, "bedrock-agentcore",
                        f"gateway/{target.gateway_id}/target/{target.target_id}"),
                        project_id=target.project_id))
        for conn in db.scalars(select(IdentityProvider).where(
                IdentityProvider.workspace_id == ws_id)):
            if not scope.covers_project(conn.project_id):
                continue
            if conn.arn:
                add(entry(CREDENTIAL_PROVIDER, arn=conn.arn, project_id=conn.project_id))
            add(entry(SECRET, name=conn.name, project_id=conn.project_id,
                      provider_kind=conn.kind))
        owners = {r.resource_id: r.project_id for r in db.scalars(select(ResourceCreator).where(
            ResourceCreator.workspace_id == ws_id, ResourceCreator.kind == creators.KB))}
        default_project = _default_project(db, ws_id)

    def kbs() -> list[str]:
        for kb_id, sources in discovery.knowledge_bases(workspace):
            pid = owners.get(kb_id, default_project)
            if not scope.covers_project(pid):
                continue
            arn = _arn(workspace, "bedrock", f"knowledge-base/{kb_id}")
            add(entry(KNOWLEDGE_BASE, arn=arn, project_id=pid))
            for ds in sources:
                add(entry(DATA_SOURCE, name=f"{kb_id}/{ds}", project_id=pid,
                          status=UNSUPPORTED, detail="Bedrock data sources carry no tags"))
        return []  # entries added above, each with its own project

    _discovered(add, KNOWLEDGE_BASE, "knowledge bases", None, None, kbs)
    if scope.workspace_wide:
        for item in workspace_infra(workspace):
            add(item)
        for item in context_layer_infra(db, workspace):
            add(item)
    return list(found.values())


def context_layer_infra(db: Session, workspace: Any) -> list[dict[str, Any]]:
    """The managed COA gateway (`launchpad-coa-gw`) is shared by every project's
    gateway-mode turns: platform infrastructure (`launchpad:project=shared`), so its
    fixed and per-request fees land on the billing view's shared row — the platform
    line — rather than untagged. COA's own runtimes are a connected deployment
    Launchpad never tags (`services/context_layer.py`)."""
    from app.models.ledger import ContextLayer

    with project_context(None):
        layer = db.scalars(select(ContextLayer).where(
            ContextLayer.workspace_id == workspace.id)).first()
    gateway = ((layer.resources or {}).get("gateway") if layer is not None else None) or {}
    arn = gateway.get("gateway_arn") if isinstance(gateway, dict) else None
    return [entry(GATEWAY, arn=str(arn))] if arn else []


def workspace_infra(workspace: Any) -> list[dict[str, Any]]:
    """The workspace infrastructure no project owns (`launchpad:project=shared`)."""
    from app.services import workspace_iam

    res = workspace.resources or {}
    out: list[dict[str, Any]] = []
    if res.get("gateway_arn"):
        out.append(entry(GATEWAY, arn=str(res["gateway_arn"])))
    if res.get("memory_arn") or res.get("memory_id"):
        out.append(entry(MEMORY, arn=str(res.get("memory_arn") or _arn(
            workspace, "bedrock-agentcore", f"memory/{res['memory_id']}"))))
    if res.get("oauth_provider_arn"):
        out.append(entry(CREDENTIAL_PROVIDER, arn=str(res["oauth_provider_arn"])))
    if res.get("ecr_repo"):
        out.append(entry(ECR_REPOSITORY, arn=_arn(workspace, "ecr",
                                                  f"repository/{res['ecr_repo']}")))
    if res.get("codebuild_project"):
        out.append(entry(CODEBUILD_PROJECT, name=str(res["codebuild_project"])))
        out.append(entry(IAM_ROLE, name=workspace_iam.regional_role_name(
            workspace_iam.CODEBUILD_ROLE_BASE, workspace.region)))
    for key in ("execution_role_arn", "gateway_role_arn", "kb_role_arn"):
        if res.get(key):
            out.append(entry(IAM_ROLE, name=_role_name(str(res[key]))))
    return out


# ── the back-fill job ─────────────────────────────────────────────────────────


@dataclass
class BackfillOps:
    """Every AWS touch of a run, so tests swap them."""

    writer: TagWriter = field(default_factory=AwsTagWriter)
    discovery: Discovery = field(default_factory=AwsDiscovery)


_OPS = BackfillOps()


def get_ops() -> BackfillOps:
    return _OPS


def set_ops(**changes: Any) -> BackfillOps:
    """Replace pieces of the ops (tests); returns the previous ops."""
    global _OPS
    previous = _OPS
    _OPS = BackfillOps(**{**previous.__dict__, **changes})
    return previous


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _job(db: Session, job_id: str) -> Any:
    from app.models.ledger import Job

    return db.get(Job, job_id)


def _save(db: Session, job_id: str, payload: dict[str, Any]) -> None:
    job = _job(db, job_id)
    if job is not None:
        job.payload = payload  # reassigned: in-place JSON edits are not tracked
        db.commit()


def _log(db: Session, job_id: str, stage: str, message: str, level: str = "info") -> None:
    job = _job(db, job_id)
    if job is None:
        return
    line = json.dumps({"ts": _now_iso(), "stage": stage, "level": level, "msg": message},
                      ensure_ascii=False)
    job.log = (job.log + "\n" + line) if job.log else line
    db.commit()


def _set_stage(db: Session, job_id: str, stage: str, status: str, detail: str = "") -> None:
    job = _job(db, job_id)
    payload = dict(job.payload or {})
    stages = [dict(s) for s in payload.get("stages") or []]
    for item in stages:
        if item["name"] == stage:
            item["status"] = status
            item["detail"] = detail or item.get("detail", "")
            item["started_at" if status == "running" else "ended_at"] = _now_iso()
    payload["stages"] = stages
    _save(db, job_id, payload)


def coverage(resources: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """How much of a run's inventory carries the tags: counts by result and by kind,
    and the tagged share of what can carry tags at all (`unsupported` left out)."""
    by_status = dict.fromkeys((PENDING, *RESULTS), 0)
    by_kind: dict[str, dict[str, int]] = {}
    total = 0
    for item in resources:
        status = item.get("status") or PENDING
        by_status[status] = by_status.get(status, 0) + 1
        kind = by_kind.setdefault(item.get("kind") or "?", {})
        kind[status] = kind.get(status, 0) + 1
        total += 1
    taggable = total - by_status[UNSUPPORTED]
    percent = round(100.0 * by_status[TAGGED] / taggable, 1) if taggable else None
    return {"total": total, "taggable": taggable, "tagged": by_status[TAGGED],
            "percent": percent, "by_status": by_status, "by_kind": by_kind}


def _desired_tags(db: Session, workspace: Any, item: Mapping[str, Any]) -> dict[str, str]:
    """The tags a resource should carry now: an agent-level one follows its agent's
    current project (a transfer moved it), anything else the project it was planned
    under."""
    from app.models.ledger import Agent

    agent_id = item.get("agent_id")
    if agent_id:
        with project_context(None):
            agent = db.get(Agent, agent_id)
        if agent is not None:
            return agent_tags(agent, workspace.id, db=db)
    return tags_for(item.get("project_id"), workspace_id=workspace.id, db=db)


def _stage_plan(db: Session, job_id: str, workspace: Any, ops: BackfillOps) -> str:
    payload = dict(_job(db, job_id).payload or {})
    scope = Scope(**(payload.get("scope") or {}))
    resources = plan_resources(db, workspace, scope, ops.discovery)
    payload["resources"] = resources
    payload["coverage"] = coverage(resources)
    _save(db, job_id, payload)
    return f"{len(resources)} resource(s) to tag"


def _stage_tag(db: Session, job_id: str, workspace: Any, ops: BackfillOps) -> str:
    """Write each pending resource and persist its result before the next one — a
    restart continues with the first resource still pending."""
    payload = dict(_job(db, job_id).payload or {})
    resources = [dict(r) for r in payload.get("resources") or []]
    for index, item in enumerate(resources):
        if item.get("status") != PENDING:
            continue
        desired = _desired_tags(db, workspace, item)
        result, detail = ops.writer.apply(workspace, item, desired)
        stale = sorted(set(payload.get("remove_keys") or []) - set(desired))
        untag = getattr(ops.writer, "untag", None)
        if result == TAGGED and stale and untag is not None:
            # tags the change made stale (an old project's cost center must not keep
            # attributing the resource to it); a failure here is the resource's result
            result, detail = untag(workspace, item, stale)
            if result == TAGGED:
                item["untagged"] = stale
        item.update(status=result, detail=detail, tagged_at=_now_iso())
        resources[index] = item
        payload = dict(_job(db, job_id).payload or {})
        payload["resources"] = resources
        payload["coverage"] = coverage(resources)
        _save(db, job_id, payload)
    summary = coverage(resources)
    counts = ", ".join(f"{n} {s}" for s, n in summary["by_status"].items() if n)
    return counts or "nothing to tag"


def execute_backfill_job(job_id: str, ops: BackfillOps | None = None,
                         workspace_factory: Callable[[str], Any] | None = None) -> None:
    """Run (or resume) one back-fill run inside the job's project; never raises."""
    from app.services.workspace import context_for_workspace

    ops = ops or get_ops()
    db = SessionLocal()
    try:
        with project_context(None):
            job = _job(db, job_id)
            if job is None or job.type != JOB_TYPE or job.status not in _ACTIVE:
                return
            project_id, workspace_id = job.project_id, job.workspace_id
        with project_context(project_id):
            job = _job(db, job_id)
            job.status, job.error = "running", None
            db.commit()
            workspace = (workspace_factory or context_for_workspace)(workspace_id)
            done = {s["name"] for s in (job.payload or {}).get("stages") or []
                    if s.get("status") == "succeeded"}
            for stage, run in (("plan", _stage_plan), ("tag", _stage_tag)):
                if stage in done:
                    continue
                _set_stage(db, job_id, stage, "running")
                try:
                    detail = run(db, job_id, workspace, ops)
                except Exception as exc:
                    db.rollback()
                    detail = f"{type(exc).__name__}: {exc}"
                    _set_stage(db, job_id, stage, "failed", detail)
                    _log(db, job_id, stage, detail, "error")
                    _log(db, job_id, stage, traceback.format_exc(limit=3), "debug")
                    _finish(db, job_id, "failed", detail)
                    return
                _set_stage(db, job_id, stage, "succeeded", detail)
                _log(db, job_id, stage, detail)
            _finish(db, job_id, "succeeded")
    except Exception as exc:  # job-level failure — never crash the worker thread
        db.rollback()
        logger.exception("%s %s failed outside a stage", JOB_TYPE, job_id)
        with project_context(None):
            _finish(db, job_id, "failed", f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


def _finish(db: Session, job_id: str, status: str, error: str | None = None) -> None:
    job = _job(db, job_id)
    if job is not None:
        job.status, job.error = status, error
        db.commit()


# ── starting runs ─────────────────────────────────────────────────────────────

_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()
_QUEUE_LOCK = threading.Lock()
_PENDING_KEY = "cost_tags_jobs"


def start_backfill_async(job_id: str) -> threading.Thread:
    """One live worker per run in this process; also the resume starter."""
    with _LIVE_LOCK:
        existing = _LIVE.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def run() -> None:
            try:
                execute_backfill_job(job_id)
            finally:
                with _LIVE_LOCK:
                    if _LIVE.get(job_id) is threading.current_thread():
                        del _LIVE[job_id]

        thread = threading.Thread(target=run, daemon=True, name=f"cost-tags-{job_id[:8]}")
        _LIVE[job_id] = thread
        try:
            thread.start()
        except Exception:
            _LIVE.pop(job_id, None)
            raise
        return thread


def _new_job(db: Session, workspace_id: str, scope: Scope, *, reason: str, actor: str,
             actor_user_id: str | None, remove_keys: Iterable[str] | None = None) -> Any:
    """A run of `scope` in the workspace, unless an identical one is still queued (it
    will read the ledger as it is when it runs; it takes this run's `remove_keys` too).
    Flushed, not committed."""
    from app.models.ledger import Job

    remove = sorted(set(remove_keys or ()))
    with project_context(None):
        for job in db.scalars(select(Job).where(
                Job.type == JOB_TYPE, Job.workspace_id == workspace_id,
                Job.status == "queued")):
            if (job.payload or {}).get("scope") == scope.as_dict():
                if remove:
                    payload = dict(job.payload or {})
                    payload["remove_keys"] = sorted(
                        set(payload.get("remove_keys") or []) | set(remove))
                    job.payload = payload
                    db.flush()
                return job
        payload: dict[str, Any] = {
            "workspace_id": workspace_id, "scope": scope.as_dict(), "reason": reason,
            "actor": actor, "actor_user_id": actor_user_id,
            "stages": [{"name": s, "status": "pending", "detail": ""} for s in STAGES],
            "resources": [],
        }
        if remove:
            # keys to take off each resource unless its desired set has them
            payload["remove_keys"] = remove
        if scope.project_id is None and not scope.agent_ids:
            payload["platform"] = True  # workspace-wide: no project owns the run
        job = Job(workspace_id=workspace_id, type=JOB_TYPE, payload=payload,
                  project_id=None if payload.get("platform") else scope.project_id)
        db.add(job)
        db.flush()
        return job


def start_backfill(db: Session, identity: Any, workspace_id: str) -> Any:
    """The administrator's workspace-wide run (`配置 → 成本标签`): queued and committed;
    the caller starts it (`start_backfill_async`)."""
    with _QUEUE_LOCK:
        job = _new_job(db, workspace_id, Scope(), reason="admin",
                       actor=getattr(identity, "username", "admin"),
                       actor_user_id=getattr(identity, "user_id", None))
        db.commit()
    return job


def schedule_retag(db: Session, *, project_id: str, reason: str,
                   workspace_ids: Iterable[str] | None = None,
                   agent_ids: list[str] | None = None, actor: str = "system",
                   actor_user_id: str | None = None,
                   remove_keys: Iterable[str] | None = None) -> list[str]:
    """Queue the re-tag a project change needs (a transfer: the agents; a department
    or `cost_tags` change, a rename: the project) in the caller's transaction, one run
    per workspace (default: every workspace the project is bound to). The ids wait in
    `db.info` until `start_queued` runs after the commit. `remove_keys`: tags the
    change made stale (the old project's custom keys, a dropped key, a cleared
    department) — removed from each resource whose desired set lacks them."""
    from app.models.projects import ProjectWorkspace

    if workspace_ids is None:
        with project_context(None):
            workspace_ids = list(db.scalars(select(ProjectWorkspace.workspace_id).where(
                ProjectWorkspace.project_id == project_id)))
    scope = Scope(project_id=project_id, agent_ids=sorted(agent_ids) if agent_ids else None)
    ids = []
    for ws in workspace_ids:
        job = _new_job(db, ws, scope, reason=reason, actor=actor,
                       actor_user_id=actor_user_id, remove_keys=remove_keys)
        ids.append(job.id)
    db.info.setdefault(_PENDING_KEY, []).extend(i for i in ids
                                                if i not in db.info[_PENDING_KEY])
    return ids


def start_queued(db: Session) -> list[str]:
    """Start the runs `schedule_retag` queued in this session — call after commit."""
    ids = list(db.info.pop(_PENDING_KEY, None) or [])
    for job_id in ids:
        start_backfill_async(job_id)
    return ids


# ── views ─────────────────────────────────────────────────────────────────────


def latest_run(db: Session, workspace_id: str, *, workspace_wide: bool = True) -> Any:
    from app.models.ledger import Job

    with project_context(None):
        jobs = db.scalars(select(Job).where(Job.type == JOB_TYPE,
                                            Job.workspace_id == workspace_id)
                          .order_by(Job.created_at.desc(), Job.id.desc()))
        for job in jobs:
            if not workspace_wide or (job.payload or {}).get("platform"):
                return job
    return None


def run_view(job: Any, *, all_resources: bool = False) -> dict[str, Any]:
    """A run for the console: stages, coverage and the resources not tagged (all of
    them with `all_resources`)."""
    payload = job.payload or {}
    resources = payload.get("resources") or []
    return {
        "id": job.id, "status": job.status, "error": job.error,
        "workspace_id": job.workspace_id, "project_id": job.project_id,
        "scope": payload.get("scope") or {}, "reason": payload.get("reason"),
        "actor": payload.get("actor"),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "stages": payload.get("stages") or [],
        "coverage": coverage(resources),
        "resources": [r for r in resources
                      if all_resources or r.get("status") not in (TAGGED, UNSUPPORTED)],
    }


def backfill_view(db: Session, workspace_id: str) -> dict[str, Any]:
    """The tag set and the workspace's latest workspace-wide run."""
    job = latest_run(db, workspace_id)
    return {
        "tag_keys": [pt.TAG_PROJECT, pt.TAG_WORKSPACE, pt.TAG_AGENT, pt.TAG_MANAGED_BY,
                     pt.TAG_DEPARTMENT],
        "shared_project": pt.SHARED_PROJECT,
        "max_cost_tags": pt.MAX_COST_TAGS,
        "run": run_view(job) if job is not None else None,
    }
