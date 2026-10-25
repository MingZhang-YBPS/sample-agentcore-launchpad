"""Project attribution of what an agent emits and what it costs (projects design §10.8).

Two marks, set at deploy time and only for an agent of an **isolated** project (a
`shared` project — the `default` one, every single-project workspace — keeps today's
requests exactly, and its spans are attributed through the ledger as before):

* **span resource attributes** — `OTEL_RESOURCE_ATTRIBUTES` on the runtime / harness
  carries `launchpad.project`, `launchpad.agent` and `launchpad.workspace`, so every
  span names its project and the observability queries can filter on it instead of a
  list of runtime service names (`observability.ProjectAgents.service_filter`). The
  platform's entries go first; the spec's own entries follow, minus any `launchpad.*`
  key (a spec cannot claim another project). UNVERIFIED live: whether AgentCore merges
  its own resource attributes (`service.name`) with a user-set
  `OTEL_RESOURCE_ATTRIBUTES` or lets it replace them; BYOC code may replace the
  variable with its own OTEL setup, and its spans then fall back to the resolver.
* **resource tags** `launchpad:project`, `launchpad:workspace`, `launchpad:agent-id`
  on CreateAgentRuntime / CreateHarness (and CreateMemory for the project memory) —
  the `tags` map is in the bedrock-agentcore-control service model (botocore 1.43),
  not yet exercised live. The tags are derived from the same attributes
  (`tags_from_environment`), so the two marks can never disagree.

Cost tags (cost-tags design §1.1, CT-1) widen the *tags* — not the span attributes —
to every project: `standard_tags` is the one tag set a platform-created resource
carries, isolated or not, plus `launchpad:managed-by`, the project's department and
its own `settings.cost_tags`. Workspace infrastructure no project owns is tagged
`launchpad:project=shared`. A `launchpad:` or `aws:` key is never taken from a user —
not from a spec's `OTEL_RESOURCE_ATTRIBUTES` (above) and not from `cost_tags`
(`services/projects.py` refuses them; `standard_tags` drops them again). The lookups
and the per-service tagging live in `services/cost_tags.py`.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any

ENV_OTEL_RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"

ATTR_PROJECT = "launchpad.project"
ATTR_AGENT = "launchpad.agent"
ATTR_WORKSPACE = "launchpad.workspace"
_ATTR_PREFIX = "launchpad."

TAG_PROJECT = "launchpad:project"
TAG_WORKSPACE = "launchpad:workspace"
TAG_AGENT = "launchpad:agent-id"
TAG_MANAGED_BY = "launchpad:managed-by"
TAG_DEPARTMENT = "launchpad:department"
MANAGED_BY = "launchpad"
# `launchpad:project` of workspace infrastructure no project owns (the shared gateway,
# the workspace memory, the service roles, the shared ECR repository…)
SHARED_PROJECT = "shared"
# keys only the platform writes: a user's cost tag may not start with either (AWS
# itself refuses `aws:`; `launchpad:` would let a project file its cost elsewhere)
RESERVED_TAG_PREFIXES = ("aws:", "launchpad:")
MAX_COST_TAGS = 10
_TAG_FOR_ATTR = {ATTR_PROJECT: TAG_PROJECT, ATTR_AGENT: TAG_AGENT, ATTR_WORKSPACE: TAG_WORKSPACE}


def attributed(project_id: str | None) -> bool:
    """Whether an agent of `project_id` carries the project marks: the project
    exists and is isolated. Hub-global read through its own session, so a deployer
    can ask without threading one through."""
    if not project_id:
        return False
    from app.core.db import SessionLocal
    from app.core.project_scope import project_context
    from app.models.projects import Project
    from app.services.project_isolation import is_isolated

    db = SessionLocal()
    try:
        with project_context(None):
            return is_isolated(db.get(Project, project_id))
    finally:
        db.close()


def resource_attributes(project_id: str, agent_id: str | None = None,
                        workspace_id: str | None = None) -> dict[str, str]:
    """The platform's span resource attributes; an unknown id is left out."""
    out = {ATTR_PROJECT: project_id}
    if agent_id:
        out[ATTR_AGENT] = agent_id
    if workspace_id:
        out[ATTR_WORKSPACE] = workspace_id
    return out


def parse_otel_resource_attributes(value: str | None) -> dict[str, str]:
    """`k=v,k2=v2` (W3C-baggage style, values percent-encoded) → dict; malformed
    entries are skipped, a later duplicate wins (what the OTel SDK does)."""
    out: dict[str, str] = {}
    for entry in (value or "").split(","):
        key, sep, raw = entry.partition("=")
        key = key.strip()
        if sep and key:
            out[key] = urllib.parse.unquote(raw.strip())
    return out


def merge_otel_resource_attributes(existing: str | None, platform: Mapping[str, str]) -> str:
    """The platform's entries first, then the spec's own minus any `launchpad.*` key."""
    user = {
        k: v for k, v in parse_otel_resource_attributes(existing).items()
        if not k.startswith(_ATTR_PREFIX)
    }
    return ",".join(
        f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in {**platform, **user}.items()
    )


def with_project_attributes(environment: Mapping[str, str], *, project_id: str | None,
                            agent_id: str | None = None,
                            workspace_id: str | None = None) -> dict[str, str]:
    """`environment` plus the project's `OTEL_RESOURCE_ATTRIBUTES` when the project
    is attributed (`attributed`); unchanged otherwise."""
    out = dict(environment)
    if not attributed(project_id):
        return out
    out[ENV_OTEL_RESOURCE_ATTRIBUTES] = merge_otel_resource_attributes(
        out.get(ENV_OTEL_RESOURCE_ATTRIBUTES),
        resource_attributes(str(project_id), agent_id, workspace_id),
    )
    return out


def tags_from_environment(environment: Mapping[str, Any] | None) -> dict[str, str]:
    """The `launchpad:*` resource tags matching the platform's span attributes in a
    runtime environment ({} when it carries none — every non-isolated deploy)."""
    attrs = parse_otel_resource_attributes(
        str((environment or {}).get(ENV_OTEL_RESOURCE_ATTRIBUTES) or "")
    )
    return {tag: attrs[attr] for attr, tag in _TAG_FOR_ATTR.items() if attrs.get(attr)}


def project_tags(project_id: str, *, workspace_id: str | None = None,
                 agent_id: str | None = None) -> dict[str, str]:
    """The tag map for a resource created for a project (memory, KB, profile, …)."""
    attrs = resource_attributes(project_id, agent_id, workspace_id)
    return {_TAG_FOR_ATTR[attr]: value for attr, value in attrs.items()}


def is_reserved_tag_key(key: str) -> bool:
    """A key only the platform may write (`aws:` / `launchpad:`, any case)."""
    return str(key).strip().lower().startswith(RESERVED_TAG_PREFIXES)


def standard_tags(project_id: str | None, *, workspace_id: str | None = None,
                  agent_id: str | None = None, department_id: str | None = None,
                  custom: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The unified tag set of a platform-created resource (cost-tags design §1.1):
    `launchpad:project` (`shared` for workspace infrastructure — no project),
    `launchpad:workspace` / `launchpad:agent-id` when known, `launchpad:managed-by`,
    `launchpad:department` when the project has one, then the project's own cost tags
    minus any reserved key — the platform's keys can never be overridden."""
    out = {TAG_PROJECT: str(project_id or SHARED_PROJECT)}
    if workspace_id:
        out[TAG_WORKSPACE] = str(workspace_id)
    if agent_id:
        out[TAG_AGENT] = str(agent_id)
    out[TAG_MANAGED_BY] = MANAGED_BY
    if department_id:
        out[TAG_DEPARTMENT] = str(department_id)
    for key, value in (custom or {}).items():
        if value is None or is_reserved_tag_key(key) or key in out:
            continue
        out[str(key)] = str(value)
    return out


def shared_tags(workspace_id: str | None = None) -> dict[str, str]:
    """The tags of workspace infrastructure no project owns."""
    return standard_tags(None, workspace_id=workspace_id)
