"""Tool review of hosted MCP servers (design projects §15.6): snapshot, diff, hints,
decisions.

Pure ledger logic — no AWS. ``services/mcp_servers.py`` lists the tools through the
Gateway and asks :func:`record_tool_list` whether the list it sees is the approved
one; when it is not, the server's target is withheld until a review is approved.

* The snapshot is ``[{name, description, input_schema}]`` sorted by name with the
  ``<target>___`` prefix stripped, and its hash is the SHA-256 of its canonical JSON
  — so a re-publish that changes nothing hashes the same and needs no new review.
* The diff is against the server's last **approved** list (what agents saw), not the
  previous open one.
* Hints flag what a reviewer should read twice (injection-style phrases, URLs,
  instructions naming other tools, very long text, destructive / write verbs, missing
  descriptions). They never reject anything by themselves.
* Separation of duties (§3.6): the deployer (``requested_by``) cannot decide, compared
  by ``users.id`` when both sides carry one, else by name — for administrators too,
  like signing a release.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.actor import same_actor, user_id_for
from app.core.errors import AppError
from app.models.ledger import Agent, Deployment, Job
from app.models.mcp_review import McpToolReview
from app.services.audit import record_audit_event

SYSTEM_BASELINE = "system:baseline"
MAX_NOTE = 2000
LONG_DESCRIPTION = 1000

# name tokens: destructive / code-executing verbs, and write-y ones
DANGEROUS_VERBS = frozenset({
    "delete", "drop", "remove", "destroy", "truncate", "purge", "wipe", "erase", "kill",
    "terminate", "shutdown", "exec", "execute", "eval", "shell", "sudo", "format",
})
WRITE_VERBS = frozenset({
    "create", "update", "write", "put", "post", "send", "set", "modify", "insert", "upsert",
    "upload", "export", "transfer", "pay", "approve", "grant", "publish", "patch", "move",
    "rename", "invite", "email", "submit", "charge", "refund", "deploy",
})
_INJECTION = re.compile(
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) "
    r"(?:instructions|messages|prompts?)"
    r"|disregard (?:all |any )?(?:the )?(?:previous|prior|above|earlier|system)"
    r"|system prompt|developer message|you are now|do not (?:tell|inform|mention)"
    r"|without (?:telling|asking|informing) the user|always call|before (?:calling|using) any"
    r"|<\s*(?:system|instructions?)\s*>",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _now() -> datetime:
    return datetime.now(UTC)


# ── snapshot, hash, diff ──────────────────────────────────────────────────────


def normalize_tools(raw: list[dict[str, Any]], target_name: str) -> list[dict[str, Any]]:
    """The target's tools from a Gateway ``tools/list`` (other targets dropped)."""
    prefix = f"{target_name}___"
    out: dict[str, dict[str, Any]] = {}
    for tool in raw:
        name = str(tool.get("name") or "")
        if not name.startswith(prefix):
            continue
        short = name[len(prefix):]
        schema = tool.get("inputSchema")
        out[short] = {
            "name": short,
            "description": str(tool.get("description") or ""),
            "input_schema": schema if isinstance(schema, dict) else {},
        }
    return [out[name] for name in sorted(out)]


def tools_hash(tools: list[dict[str, Any]]) -> str:
    canonical = json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _schema_hash(schema: Any) -> str:
    return tools_hash([schema]) if schema else ""


def diff_tools(base: list[dict[str, Any]] | None, new: list[dict[str, Any]]) -> dict[str, Any]:
    """Added / removed / description- or schema-changed names against ``base``
    (None = the server's first list: everything is added)."""
    before = {t["name"]: t for t in base or []}
    after = {t["name"]: t for t in new}
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "description_changed": sorted(
            n for n in set(after) & set(before)
            if after[n].get("description") != before[n].get("description")
        ),
        "schema_changed": sorted(
            n for n in set(after) & set(before)
            if _schema_hash(after[n].get("input_schema"))
            != _schema_hash(before[n].get("input_schema"))
        ),
    }


# ── static hints ──────────────────────────────────────────────────────────────


def _name_tokens(name: str) -> set[str]:
    return {t.lower() for part in re.split(r"[_\-.\s]+", name) for t in _TOKEN.findall(part)}


def _schema_texts(schema: Any) -> list[str]:
    """Every free text a schema carries into the model's context (descriptions, titles,
    enum / default strings)."""
    out: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in ("description", "title") and isinstance(value, str):
                out.append(value)
            elif key in ("enum", "examples") and isinstance(value, list):
                out.extend(v for v in value if isinstance(v, str))
            elif key == "default" and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_schema_texts(value))
    elif isinstance(schema, list):
        for item in schema:
            out.extend(_schema_texts(item))
    return out


def static_hints(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a reviewer should read twice, per tool. Hints only — never a verdict."""
    names = {t["name"] for t in tools}
    hints: list[dict[str, Any]] = []

    def hint(tool: str, kind: str, detail: str = "") -> None:
        hints.append({"tool": tool, "kind": kind, "detail": detail[:200]})

    for tool in tools:
        name = tool["name"]
        description = tool.get("description") or ""
        tokens = _name_tokens(name)
        if tokens & DANGEROUS_VERBS:
            hint(name, "dangerous_verb", ", ".join(sorted(tokens & DANGEROUS_VERBS)))
        elif tokens & WRITE_VERBS:
            hint(name, "write_name", ", ".join(sorted(tokens & WRITE_VERBS)))
        if not description.strip():
            hint(name, "missing_description")
        texts = [description, *_schema_texts(tool.get("input_schema"))]
        joined = "\n".join(texts)
        found = _INJECTION.search(joined)
        if found:
            hint(name, "injection_phrase", found.group(0))
        url = _URL.search(joined)
        if url:
            hint(name, "url", url.group(0))
        others = sorted(
            other for other in names - {name}
            if re.search(rf"(?<![\w-]){re.escape(other)}(?![\w-])", joined)
        )
        if others:
            hint(name, "cross_tool_reference", ", ".join(others))
        if len(description) > LONG_DESCRIPTION:
            hint(name, "long_description", str(len(description)))
    return hints


# ── reads ─────────────────────────────────────────────────────────────────────


def _latest(db: Session, agent_id: str, status: str) -> McpToolReview | None:
    return db.scalars(
        select(McpToolReview)
        .where(McpToolReview.agent_id == agent_id, McpToolReview.status == status)
        .order_by(McpToolReview.created_at.desc())
        .limit(1)
    ).first()


def latest_approved(db: Session, agent_id: str) -> McpToolReview | None:
    return _latest(db, agent_id, "approved")


def open_review(db: Session, agent_id: str) -> McpToolReview | None:
    return _latest(db, agent_id, "reviewing")


def has_reviews(db: Session, agent_id: str) -> bool:
    return db.scalars(
        select(McpToolReview.id).where(McpToolReview.agent_id == agent_id).limit(1)
    ).first() is not None


def list_reviews(db: Session, agent_id: str, limit: int = 50) -> list[McpToolReview]:
    return list(db.scalars(
        select(McpToolReview)
        .where(McpToolReview.agent_id == agent_id)
        .order_by(McpToolReview.created_at.desc())
        .limit(limit)
    ))


def state(db: Session, agent_id: str) -> dict[str, Any]:
    """The gate as the ledger knows it (no AWS read): ``reviewing`` while a list
    waits, else the latest decision; ``none`` before the first tool list."""
    pending = open_review(db, agent_id)
    approved = latest_approved(db, agent_id)
    latest = next(iter(list_reviews(db, agent_id, limit=1)), None)
    # superseded only ever sits behind a newer review, so `latest` is never one
    current = "reviewing" if pending is not None else latest.status if latest else "none"
    return {
        "state": current,
        "open_review_id": pending.id if pending is not None else None,
        "approved_review_id": approved.id if approved is not None else None,
        "approved_hash": approved.tools_hash if approved is not None else None,
    }


def deployer_of(db: Session, agent: Agent) -> tuple[str, str | None]:
    """Who published the server's latest deployment (the Job's recorded actor), else
    its creator — the person a review must not be approved by."""
    deployment = db.scalars(
        select(Deployment)
        .where(Deployment.agent_id == agent.id)
        .order_by(Deployment.started_at.desc())
        .limit(1)
    ).first()
    job = db.get(Job, deployment.job_id) if deployment and deployment.job_id else None
    payload = (job.payload or {}) if job is not None else {}
    actor = payload.get("actor")
    if actor:
        return str(actor)[:64], payload.get("actor_user_id")
    return (agent.created_by_name or "")[:64], agent.created_by


# ── writes ────────────────────────────────────────────────────────────────────


def record_tool_list(
    db: Session, agent: Agent, tools: list[dict[str, Any]], *, source: str = "deploy",
) -> McpToolReview:
    """The review deciding ``tools``: the latest approved one when the hash matches
    (the gate is open), else the open review for this exact list — created now, with
    any older open review superseded. The caller commits."""
    digest = tools_hash(tools)
    approved = latest_approved(db, agent.id)
    if approved is not None and approved.tools_hash == digest:
        return approved
    pending = open_review(db, agent.id)
    if pending is not None and pending.tools_hash == digest:
        return pending
    if pending is not None:
        pending.status = "superseded"
    requested_by, requested_by_user_id = deployer_of(db, agent)
    target_name = str(((agent.spec or {}).get("mcp") or {}).get("target_name") or "")
    review = McpToolReview(
        workspace_id=agent.workspace_id,
        agent_id=agent.id,
        project_id=agent.project_id,
        version=agent.version,
        target_name=target_name,
        tools=tools,
        tools_hash=digest,
        base_review_id=approved.id if approved is not None else None,
        diff=diff_tools(approved.tools if approved is not None else None, tools),
        hints=static_hints(tools),
        status="reviewing",
        source=source,
        requested_by=requested_by,
        requested_by_user_id=requested_by_user_id,
    )
    db.add(review)
    record_audit_event(workspace_id=agent.workspace_id, actor=requested_by or "system",
                       action=f"mcp.review.{source}", target=agent.id, db=db,
                       project_id=agent.project_id)
    db.flush()
    return review


def record_baseline(db: Session, agent: Agent, tools: list[dict[str, Any]]) -> McpToolReview:
    """§15.15: a server deployed before tool review existed — its live list becomes the
    approved baseline, so its first re-sync does not pull a target agents already use."""
    target_name = str(((agent.spec or {}).get("mcp") or {}).get("target_name") or "")
    review = McpToolReview(
        workspace_id=agent.workspace_id, agent_id=agent.id, project_id=agent.project_id,
        version=agent.version, target_name=target_name, tools=tools,
        tools_hash=tools_hash(tools), diff=diff_tools(None, tools),
        hints=static_hints(tools), status="approved", source="baseline",
        requested_by=SYSTEM_BASELINE, decided_by=SYSTEM_BASELINE, decided_at=_now(),
        note="recorded as approved: the server was live before tool review existed",
        publish_status="published",
    )
    db.add(review)
    db.flush()
    return review


def decide(
    db: Session, review: McpToolReview, *, actor: str, approve: bool, note: str = "",
    actor_user_id: str | None = None,
) -> McpToolReview:
    """Approve or reject an open review. 409 when it is not open, or when the decider
    is its deployer (both decisions: a deployer neither waves through nor buries)."""
    if review.status != "reviewing":
        raise AppError("mcp_review.not_open", "this tool review is already decided",
                       {"status": review.status}, status_code=409)
    actor_user_id = user_id_for(actor, actor_user_id)
    if same_actor(review.requested_by, review.requested_by_user_id, actor, actor_user_id):
        raise AppError(
            "mcp_review.self_decide",
            "the deployer of an MCP server cannot decide its tool review — ask another "
            "member holding mcp.approve",
            status_code=409,
        )
    review.status = "approved" if approve else "rejected"
    review.decided_by = actor[:64]
    review.decided_by_user_id = actor_user_id
    review.decided_at = _now()
    review.note = note[:MAX_NOTE]
    if approve:
        review.publish_status = "publishing"
        review.publish_error = None
    record_audit_event(workspace_id=review.workspace_id, actor=actor,
                       action=f"mcp.review.{'approve' if approve else 'reject'}",
                       target=review.id, db=db)
    db.flush()
    return review


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def review_out(review: McpToolReview, *, full: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": review.id,
        "agent_id": review.agent_id,
        "version": review.version,
        "target_name": review.target_name,
        "tools_hash": review.tools_hash,
        "tool_count": len(review.tools or []),
        "base_review_id": review.base_review_id,
        "diff": review.diff or {},
        "hints": review.hints or [],
        "status": review.status,
        "source": review.source,
        "requested_by": review.requested_by,
        "decided_by": review.decided_by,
        "decided_at": _iso(review.decided_at),
        "note": review.note,
        "publish_status": review.publish_status,
        "publish_error": review.publish_error,
        "created_at": _iso(review.created_at),
    }
    if full:
        out["tools"] = review.tools or []
    return out
