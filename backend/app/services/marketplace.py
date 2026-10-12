"""Shared agent templates, published from real agents (roadmap T38).

T10's scenario templates are platform-authored and fixed. This is the other half: a team
that built something good publishes it so another team can start from it. The difference
that matters is provenance — a marketplace entry comes from an agent that actually ran, so
what it carries has to be pruned deliberately rather than copied wholesale.

**What is published, and what is stripped.** An entry stores the shape of the agent (its
method, prompt, model, tool *kinds*, memory and guardrail posture) and never the things
that belong to one environment or one team:

* environment-specific ids — knowledge bases, gateway targets, skills, memory resources —
  which are meaningless in another workspace (T23's mapping exists precisely because ids do
  not travel). They are recorded as *requirements* instead, so a consumer is told what to
  supply.
* anything secret-shaped: `env`, BYOC upload ids and image URIs. A template that carried an
  env var would leak one team's configuration into another's account.

**Why templates are hub-global while agents are not.** The point of publishing is that
another workspace can use it. Entries therefore live outside the workspace scope, and the
row records which workspace it came from for attribution.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import Agent, SharedTemplate

# Spec keys that never travel: each is either an environment-specific id or
# secret-shaped. Kept as one list so "what a template may contain" is auditable.
STRIPPED_KEYS = (
    "knowledge_bases",
    "tools",
    "skills",
    "env",
    "byoc",
    "code",
    "code_bundle",
    "studio_flow",
    "source_harness",
    "conversion_notes",
)
# Keys that survive, i.e. the shape a consumer wants.
KEPT_KEYS = (
    "method",
    "system_prompt",
    "model_id",
    "model_source",
    "max_tokens",
    "reasoning_effort",
    "agent_sdk",
    "protocol",
    "toolkits",
    "native_tools",
    "memory",
    "guardrail",
    "max_iterations",
    "timeout_seconds",
)
MAX_PER_WORKSPACE = 50


def _requirements(spec: dict[str, Any]) -> list[dict[str, str]]:
    """What a consumer must supply, derived from what was stripped.

    Named by kind and label rather than by id, so the requirement is readable ("a knowledge
    base holding your HR policies") instead of an opaque identifier from another account.
    """
    needs: list[dict[str, str]] = []
    for ref in spec.get("knowledge_bases") or []:
        if isinstance(ref, dict):
            needs.append(
                {
                    "kind": "knowledge_base",
                    "label": ref.get("name") or "a knowledge base",
                    "detail": (ref.get("description") or "")[:200],
                }
            )
    for ref in spec.get("tools") or []:
        if isinstance(ref, dict):
            needs.append(
                {"kind": "tool", "label": ref.get("name") or ref.get("id") or "a tool",
                 "detail": ref.get("kind") or ""}
            )
    for skill in spec.get("skills") or []:
        needs.append({"kind": "skill", "label": str(skill), "detail": ""})
    memory_id = ((spec.get("memory") or {}).get("memory_id")) if spec.get("memory") else None
    if memory_id:
        needs.append({"kind": "memory", "label": str(memory_id), "detail": ""})
    return needs


def publishable_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The spec an entry carries: kept keys only, with nothing environment-bound.

    The allow-list is not enough on its own — `memory` is kept for its *shape*
    (short/long term) but nests `memory_id`, a resource id belonging to the publishing
    account. Nested ids are pruned here rather than by adding `memory` to the strip list,
    because a consumer does want to know the agent used long-term memory.
    """
    published = {key: spec[key] for key in KEPT_KEYS if key in spec}
    memory = published.get("memory")
    if isinstance(memory, dict) and "memory_id" in memory:
        published["memory"] = {k: v for k, v in memory.items() if k != "memory_id"}
    return published


def publish(
    db: Session,
    agent: Agent,
    *,
    workspace_id: str,
    title: str,
    summary: str,
    published_by: str | None,
) -> SharedTemplate:
    """Publish an active agent as a template. Re-publishing the same agent updates it."""
    if agent.system_key:
        raise AppError(
            "marketplace.system_managed",
            "a system-managed preset is already available to everyone",
            status_code=409,
        )
    if agent.status != "active":
        raise AppError(
            "marketplace.agent_not_active",
            f"only an active agent can be published (this one is {agent.status})",
            status_code=409,
        )
    spec = agent.spec or {}
    existing = db.scalars(
        select(SharedTemplate).where(SharedTemplate.agent_id == agent.id)
    ).first()
    if existing is None:
        count = len(
            db.scalars(
                select(SharedTemplate).where(SharedTemplate.source_workspace_id == workspace_id)
            ).all()
        )
        if count >= MAX_PER_WORKSPACE:
            raise AppError(
                "marketplace.too_many",
                f"a workspace may publish at most {MAX_PER_WORKSPACE} templates",
                status_code=409,
            )
    row = existing or SharedTemplate(
        source_workspace_id=workspace_id, agent_id=agent.id, published_by=published_by
    )
    row.title = title.strip()
    row.summary = summary.strip()
    row.method = agent.method
    row.spec = publishable_spec(spec)
    row.requirements = _requirements(spec)
    row.source_agent_name = agent.name
    row.updated_at = datetime.now(UTC)
    if existing is None:
        db.add(row)
    db.flush()
    return row


def unpublish(db: Session, row: SharedTemplate) -> None:
    db.delete(row)


def entry_out(row: SharedTemplate, *, own_workspace: str | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "summary": row.summary,
        "method": row.method,
        "spec": row.spec or {},
        "requirements": row.requirements or [],
        "source_workspace_id": row.source_workspace_id,
        "source_agent_name": row.source_agent_name,
        "published_by": row.published_by,
        "uses": row.uses or 0,
        # Whether the caller's own workspace published it — the console shows
        # unpublish only for those.
        "own": own_workspace is not None and row.source_workspace_id == own_workspace,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def catalogue(db: Session, *, own_workspace: str | None = None) -> list[dict[str, Any]]:
    rows = db.scalars(select(SharedTemplate).order_by(SharedTemplate.updated_at.desc())).all()
    return [entry_out(row, own_workspace=own_workspace) for row in rows]


def record_use(db: Session, row: SharedTemplate) -> None:
    """Counted when a consumer takes a copy — the only popularity signal there is."""
    row.uses = (row.uses or 0) + 1
    db.flush()
