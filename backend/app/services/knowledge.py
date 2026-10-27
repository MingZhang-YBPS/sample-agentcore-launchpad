"""Managed Knowledge Base service — CRUD, S3 data sources, ingestion, and the
retrieval Playground.

Only ``type == "MANAGED"`` KBs are in scope; the account also holds VECTOR KBs
that the connector cannot serve, so every read filters on the configuration type
(list summaries omit it — GetKnowledgeBase carries it). Style mirrors
registry_console: thin service over the bedrock-agent / bedrock-agent-runtime
wrappers, with the Agent ledger scanned for attachment relationships.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError, cleanup_error, is_not_found
from app.core.project_scope import current_project, project_context
from app.models.ledger import Agent
from app.services import cost_tags, creators, kb_gateway, project_kb
from app.services.agentcore.client import (
    agent_client,
    agent_runtime_client,
    control_client,
)
from app.services.workspace import WorkspaceContext

# Fallback only: the real name comes from the workspace's resolved kb_role_arn
# (a non-legacy region suffixes it — see infra regional_role_name).
KB_ROLE_NAME = "launchpad-kb-role"
_MISSING_ARTIFACTS = (
    "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
)

logger = logging.getLogger(__name__)


class KBAttachedError(AppError):
    """A KB with agents mounted was deleted without ``force`` — 409 carrying the
    blocking agent names in ``detail.agents`` (same envelope shape as agents.py)."""

    def __init__(self, kb_id: str, agents: list[str]):
        super().__init__(
            "kb.has_attached_agents",
            f"knowledge base is attached to {len(agents)} agent(s); "
            "pass force=true to delete anyway",
            detail={"agents": agents},
            status_code=409,
        )
        self.agents = agents


# ── helpers ────────────────────────────────────────────────────────────────


def _slug(text: str, max_len: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_len].rstrip("-")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _kb_type(detail: dict[str, Any]) -> str:
    return (detail.get("knowledgeBaseConfiguration") or {}).get("type", "")


def _get_kb(client: Any, kb_id: str) -> dict[str, Any]:
    try:
        return client.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
    except client.exceptions.ResourceNotFoundException as exc:
        raise NotFoundError("kb.not_found", "knowledge base not found") from exc


def _require_managed(detail: dict[str, Any]) -> dict[str, Any]:
    # non-MANAGED KBs are out of scope and must never be addressable here
    if _kb_type(detail) != "MANAGED":
        raise NotFoundError("kb.not_found", "knowledge base not found")
    return detail


def _paginate(fn, key: str, **kwargs) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token = None
    while True:
        page = fn(**(kwargs | ({"nextToken": token} if token else {})))
        items.extend(page.get(key, []))
        token = page.get("nextToken")
        if not token:
            break
    return items


def _list_data_sources(client: Any, kb_id: str) -> list[dict[str, Any]]:
    return _paginate(
        client.list_data_sources, "dataSourceSummaries", knowledgeBaseId=kb_id, maxResults=100
    )


def _parse_ds_location(detail: dict[str, Any]) -> tuple[str | None, str | None]:
    """Best-effort (bucket, prefix) from a managed S3 connector's params.
    ``connectorParameters`` is a document member — GetDataSource returns it as a
    JSON *string*, while our create path sends a dict; accept both."""
    try:
        params = detail["dataSourceConfiguration"][
            "managedKnowledgeBaseConnectorConfiguration"
        ]["connectorParameters"]
        if isinstance(params, str):
            params = json.loads(params)
        bucket = (params.get("connectionConfiguration") or {}).get("bucketName")
        prefixes = (params.get("filterConfiguration") or {}).get("inclusionPrefixes") or []
        return bucket, (prefixes[0] if prefixes else None)
    except (KeyError, TypeError, IndexError, ValueError):
        return None, None


def _job_out(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("ingestionJobId"),
        "status": job.get("status"),
        "started_at": _iso(job.get("startedAt")),
        "updated_at": _iso(job.get("updatedAt")),
        "statistics": job.get("statistics") or {},
        "failure_reasons": job.get("failureReasons") or [],
    }


def _recent_ingestion_jobs(
    client: Any, kb_id: str, ds_id: str, cap: int = 10
) -> list[dict[str, Any]]:
    jobs = _paginate(
        client.list_ingestion_jobs,
        "ingestionJobSummaries",
        knowledgeBaseId=kb_id,
        dataSourceId=ds_id,
        maxResults=100,
    )
    jobs.sort(key=lambda j: str(j.get("startedAt") or ""), reverse=True)  # newest first
    out = [_job_out(j) for j in jobs[:cap]]
    if out:
        _fill_failure_reasons(client, kb_id, ds_id, out[0])
    return out


def _fill_failure_reasons(client: Any, kb_id: str, ds_id: str, job: dict[str, Any]) -> None:
    """ListIngestionJobs summaries carry no ``failureReasons`` — only GetIngestionJob
    does. For the newest job, when it failed or left documents failed, read them so
    the console can say why (one extra call, only on failure; best-effort)."""
    failed_docs = (job.get("statistics") or {}).get("numberOfDocumentsFailed") or 0
    if job["failure_reasons"] or not (job.get("status") == "FAILED" or failed_docs):
        return
    try:
        full = client.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job["job_id"]
        )["ingestionJob"]
    except Exception as exc:  # noqa: BLE001 — the summary still renders without it
        logger.info("kb %s: GetIngestionJob %s failed: %s", kb_id, job["job_id"],
                    cleanup_error(exc))
        return
    reasons = full.get("failureReasons")
    if isinstance(reasons, list):
        job["failure_reasons"] = [str(r) for r in reasons]


# ── attachment (Agent ledger) ────────────────────────────────────────────────


def _attached_map(workspace: WorkspaceContext) -> dict[str, list[str]]:
    """kb_id → [agent name] over this workspace's non-deleted agents."""
    db = SessionLocal()
    try:
        out: dict[str, list[str]] = {}
        for agent in _workspace_agents(db, workspace):
            for ref in (agent.spec or {}).get("knowledge_bases") or []:
                if isinstance(ref, dict) and ref.get("kb_id"):
                    out.setdefault(ref["kb_id"], []).append(agent.name)
        return out
    finally:
        db.close()


def attached_agents(workspace: WorkspaceContext, kb_id: str) -> list[str]:
    return _attached_map(workspace).get(kb_id, [])


def attached_system_agents(workspace: WorkspaceContext, kb_id: str) -> list[str]:
    """Names of system-managed presets that mount ``kb_id`` (ledger-only read)."""
    db = SessionLocal()
    try:
        return [
            agent.name
            for agent in _workspace_agents(db, workspace)
            if agent.system_key
            and any(
                isinstance(ref, dict) and ref.get("kb_id") == kb_id
                for ref in (agent.spec or {}).get("knowledge_bases") or []
            )
        ]
    finally:
        db.close()


def refuse_if_attached_to_system_agent(workspace: WorkspaceContext, kb_id: str) -> None:
    """A KB mounted on a system-managed preset is never force-detached here.

    The preset's spec is server-owned: an administrator changes its mounts through
    the preset maintenance route (install/repair without that KB) first. Raised
    before any AWS read or write so a refusal has no side effect at all.
    """
    protected = attached_system_agents(workspace, kb_id)
    if protected:
        raise AppError(
            "kb.attached_to_system_agent",
            f"knowledge base is mounted on the system-managed preset(s) "
            f"{', '.join(protected)}; detach it through the preset's maintenance "
            "route before deleting",
            detail={"agents": protected, "maintenance_route": "/api/system-agents"},
            status_code=409,
        )


def _workspace_agents(db: Session, workspace: WorkspaceContext) -> list[Agent]:
    """Every project's agents: a KB is a workspace resource and its mounts are an
    integrity question (the delete guard, the force strip, the in-use count), so the
    read is unscoped; the router masks names of agents outside the current project."""
    with project_context(None):
        return (
            db.query(Agent)
            .filter(Agent.workspace_id == workspace.id, Agent.status != "deleted")
            .all()
        )


def _strip_kb_from_agents(workspace: WorkspaceContext, kb_id: str) -> list[str]:
    """Force-delete follow-through: drop the KB from every mounted agent's spec
    and re-sync their per-agent agentic targets so retrieval doesn't dangle on a
    deleted KB id (and later re-publishes don't try to recreate its target).
    The deployed harness keeps its stale prompt section until the next
    re-publish — harmless, the tool itself no longer routes to the dead KB.

    Only harness agents have a kb-gw target: zip/container agents retrieve
    directly through bedrock-agent-runtime, so touching the gateway for them
    would CREATE a target nothing ever uses."""
    refuse_if_attached_to_system_agent(workspace, kb_id)  # before any mutation
    gateway_id = workspace.resources.get("kb_gateway_id")
    control = control_client(workspace) if gateway_id else None
    stripped: list[str] = []
    db = SessionLocal()
    try:
        for agent in _workspace_agents(db, workspace):
            refs = (agent.spec or {}).get("knowledge_bases") or []
            remaining = [r for r in refs if isinstance(r, dict) and r.get("kb_id") != kb_id]
            if len(remaining) == len(refs):
                continue
            spec = dict(agent.spec)
            spec["knowledge_bases"] = remaining
            agent.spec = spec
            stripped.append(agent.name)
            if control and gateway_id and spec.get("method") == "harness":
                try:
                    kb_gateway.sync_agentic_target(
                        control,
                        gateway_id,
                        agent.name,
                        [
                            {"kb_id": r["kb_id"], "description": r.get("description", "")}
                            for r in remaining
                        ],
                    )
                except Exception as exc:  # target resync is best-effort during delete
                    # not-found = the gateway/target is gone, so nothing routes to
                    # the dead KB; anything else leaves it offered as a tool
                    if not is_not_found(exc):
                        logger.warning(
                            "kb %s force delete: agent %s target on gateway %s still "
                            "lists the KB: %s",
                            kb_id, agent.name, gateway_id, cleanup_error(exc),
                        )
        db.commit()
    finally:
        db.close()
    return stripped


# ── IAM: per-KB inline read policy for external buckets ───────────────────────


def _kb_policy_name(kb_id: str) -> str:
    return f"launchpad-kb-{kb_id}"


def _kb_role_name(workspace: WorkspaceContext) -> str:
    role_arn = str(workspace.resources.get("kb_role_arn") or "")
    return role_arn.rsplit("/", 1)[-1] or KB_ROLE_NAME


def _kb_policy_document(bucket: str, prefix: str) -> dict[str, Any]:
    list_stmt: dict[str, Any] = {
        "Effect": "Allow",
        "Action": ["s3:ListBucket"],
        "Resource": f"arn:aws:s3:::{bucket}",
    }
    if prefix:
        list_stmt["Condition"] = {"StringLike": {"s3:prefix": [f"{prefix}*"]}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{prefix}*",
            },
            list_stmt,
        ],
    }


def _sync_kb_policy(
    workspace: WorkspaceContext, kb_id: str, bucket: str, prefix: str,
    role_name: str | None = None,
) -> None:
    """`role_name`: the KB's own role — an isolated project's KB role (R10-kb)
    carries its external grants; None = the workspace's shared role."""
    iam = workspace.client("iam")
    iam.put_role_policy(
        RoleName=role_name or _kb_role_name(workspace),
        PolicyName=_kb_policy_name(kb_id),
        PolicyDocument=json.dumps(_kb_policy_document(bucket, prefix)),
    )


def _delete_kb_policy(
    workspace: WorkspaceContext, kb_id: str, role_name: str | None = None
) -> None:
    iam = workspace.client("iam")
    role_name, policy_name = role_name or _kb_role_name(workspace), _kb_policy_name(kb_id)
    try:
        iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
    except Exception as exc:  # NoSuchEntity / role absent — nothing to clean
        if not is_not_found(exc):
            logger.warning(
                "kb %s: inline policy %s left on role %s: %s",
                kb_id, policy_name, role_name, cleanup_error(exc),
            )


# ── data-source construction ─────────────────────────────────────────────────


# S3 general-purpose bucket naming. Beyond matching AWS's own rules, this blocks
# wildcards ('*') and path separators ('/') that would otherwise widen the per-KB
# inline read policy (_kb_policy_document interpolates the name straight into the
# grant ARNs) from one bucket to the whole account.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _validate_external_source(bucket: str, prefix: str) -> None:
    if not _BUCKET_RE.match(bucket):
        raise AppError(
            "kb.invalid_bucket",
            "invalid S3 bucket name — 3–63 chars, lowercase letters, digits, "
            "dots or hyphens only",
            status_code=400,
        )
    if "*" in prefix:  # the prefix is a literal path, not an IAM/S3 wildcard
        raise AppError(
            "kb.invalid_prefix", "S3 prefix must be a literal path, not a wildcard", status_code=400
        )


def _resolve_source(
    workspace: WorkspaceContext,
    kb_id: str,
    source: dict[str, Any],
    placement: project_kb.KbPlacement | None = None,
) -> tuple[str, str]:
    """(bucket, prefix) for a source descriptor. ``upload`` targets the artifacts
    bucket under the KB's prefix — ``kb/{kb_id}/``, or ``projects/<pid>/kb/{kb_id}/``
    for a KB on an isolated project's role (``placement``); ``existing`` uses the
    caller's bucket/prefix."""
    artifacts = workspace.resources.get("artifacts_bucket")
    mode = (source or {}).get("mode") or "upload"
    if mode == "upload":
        if not artifacts:
            raise RuntimeError(_MISSING_ARTIFACTS)
        return artifacts, (placement or project_kb.shared_placement(workspace)).prefix(kb_id)
    if mode == "existing":
        bucket = (source.get("bucket") or "").strip()
        if not bucket:
            raise AppError(
                "kb.bucket_required", "an existing S3 source needs a bucket", status_code=400
            )
        prefix = (source.get("prefix") or "").strip().lstrip("/")
        _validate_external_source(bucket, prefix)
        return bucket, prefix
    raise AppError("kb.invalid_source", f"unsupported source mode '{mode}'", status_code=400)


def _ds_name(bucket: str, prefix: str) -> str:
    base = _slug(bucket, 40) or "s3"
    if prefix:
        base = f"{base}-{_slug(prefix, 20)}"
    return base[:60] or "s3-source"


def _data_source_configuration(bucket: str, prefix: str, account_id: str) -> dict[str, Any]:
    conn: dict[str, Any] = {
        "type": "S3",
        "version": "1",
        "connectionConfiguration": {
            "bucketName": bucket,
            "bucketOwnerAccountId": account_id,
        },
    }
    if prefix:
        conn["filterConfiguration"] = {"inclusionPrefixes": [prefix]}
    return {
        "type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
        "managedKnowledgeBaseConnectorConfiguration": {"connectorParameters": conn},
    }


def _find_data_source_at(client: Any, kb_id: str, bucket: str, prefix: str) -> str | None:
    """Existing data-source id pointing at the same S3 location, if any.

    Creation is attempted from several places that can race — the create fast
    path, the background completion below, a manual POST /data-sources, an old
    client replay — so every caller must be able to no-op instead of adding a
    second connector for the same bucket/prefix."""
    for ds in _list_data_sources(client, kb_id):
        detail = client.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"])[
            "dataSource"
        ]
        ds_bucket, ds_prefix = _parse_ds_location(detail)
        if ds_bucket == bucket and (ds_prefix or "") == (prefix or ""):
            return ds["dataSourceId"]
    return None


def _create_data_source(
    client: Any,
    workspace: WorkspaceContext,
    kb_id: str,
    source: dict[str, Any],
    placement: project_kb.KbPlacement | None = None,
) -> str:
    """``placement``: the KB's role and prefix (``project_kb.placement_of``); None =
    the shared layout."""
    if placement is None or placement.project_id is None:
        bucket, prefix = _resolve_source(workspace, kb_id, source)
    else:
        bucket, prefix = _resolve_source(workspace, kb_id, source, placement)
    existing = _find_data_source_at(client, kb_id, bucket, prefix)
    if existing:
        return existing
    if bucket != workspace.resources.get("artifacts_bucket"):
        # dynamic S3 read grant for BYO buckets — on the role the KB uses
        if placement is not None and placement.project_id:
            _sync_kb_policy(workspace, kb_id, bucket, prefix, role_name=placement.role_name)
        else:
            _sync_kb_policy(workspace, kb_id, bucket, prefix)
    created = client.create_data_source(
        knowledgeBaseId=kb_id,
        name=_ds_name(bucket, prefix),
        dataSourceConfiguration=_data_source_configuration(
            bucket, prefix, workspace.account_id
        ),
        vectorIngestionConfiguration={"parsingConfiguration": {"parsingStrategy": "SMART_PARSING"}},
    )
    return created["dataSource"]["dataSourceId"]


def data_source_locations(client: Any, kb_id: str) -> dict[str, tuple[str | None, str | None]]:
    """ds_id → (bucket, prefix) over the KB's data sources."""
    out: dict[str, tuple[str | None, str | None]] = {}
    for ds in _list_data_sources(client, kb_id):
        detail = client.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"])[
            "dataSource"
        ]
        out[ds["dataSourceId"]] = _parse_ds_location(detail)
    return out


def _artifacts_data_source(
    client: Any, kb_id: str, artifacts: str, prefix: str | None = None
) -> dict[str, Any] | None:
    """The KB's upload source (on the artifacts bucket, under its prefix), or None."""
    prefix = prefix or f"kb/{kb_id}/"
    for ds in _list_data_sources(client, kb_id):
        detail = client.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"])[
            "dataSource"
        ]
        bucket, ds_prefix = _parse_ds_location(detail)
        if bucket == artifacts and (ds_prefix or "").startswith(prefix):
            return {**detail, "dataSourceId": ds["dataSourceId"]}
    return None


def _has_artifacts_data_source(
    client: Any, kb_id: str, artifacts: str, prefix: str | None = None
) -> bool:
    return _artifacts_data_source(client, kb_id, artifacts, prefix) is not None


def _safe_filename(filename: str) -> str:
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    return name if name not in ("", ".", "..") else "file"


# ── public API ────────────────────────────────────────────────────────────────


def list_kbs(workspace: WorkspaceContext) -> list[dict[str, Any]]:
    client = agent_client(workspace)
    attached = _attached_map(workspace)
    items: list[dict[str, Any]] = []
    for summary in _paginate(
        client.list_knowledge_bases, "knowledgeBaseSummaries", maxResults=100
    ):
        kb_id = summary["knowledgeBaseId"]
        try:
            detail = client.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
        except Exception as exc:
            if is_not_found(exc):  # finished deleting between List and Get
                continue
            raise
        if _kb_type(detail) != "MANAGED":
            continue  # VECTOR/KENDRA/SQL KBs are out of scope
        items.append(
            {
                "kb_id": kb_id,
                "name": detail.get("name"),
                "description": detail.get("description", ""),
                "status": detail.get("status"),
                "updated_at": _iso(detail.get("updatedAt")),
                "data_source_count": len(_list_data_sources(client, kb_id)),
                "attached_agents": attached.get(kb_id, []),
            }
        )
    return items


def get_kb_detail(workspace: WorkspaceContext, kb_id: str) -> dict[str, Any]:
    client = agent_client(workspace)
    detail = _require_managed(_get_kb(client, kb_id))
    data_sources: list[dict[str, Any]] = []
    for ds in _list_data_sources(client, kb_id):
        ds_id = ds["dataSourceId"]
        full = client.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)["dataSource"]
        bucket, prefix = _parse_ds_location(full)
        data_sources.append(
            {
                "ds_id": ds_id,
                "name": full.get("name"),
                "status": full.get("status"),
                "bucket": bucket,
                "prefix": prefix,
                "failure_reasons": full.get("failureReasons") or [],
                "ingestion_jobs": _recent_ingestion_jobs(client, kb_id, ds_id),
            }
        )
    return {
        "kb_id": kb_id,
        "name": detail.get("name"),
        "description": detail.get("description", ""),
        "status": detail.get("status"),
        "arn": detail.get("knowledgeBaseArn"),
        "created_at": _iso(detail.get("createdAt")),
        "updated_at": _iso(detail.get("updatedAt")),
        "failure_reasons": detail.get("failureReasons") or [],
        "data_sources": data_sources,
        "attached_agents": attached_agents(workspace, kb_id),
    }


def _s3_object_meta(
    workspace: WorkspaceContext, bucket: str, prefix: str | None
) -> dict[str, tuple[int, str | None]]:
    """key → (size, last_modified ISO) over the source location. Best-effort
    upload-time/size enrichment — external buckets may deny the backend, and
    huge buckets are capped (enrichment, not the source of truth)."""
    s3 = workspace.client("s3")
    out: dict[str, tuple[int, str | None]] = {}
    kwargs: dict[str, Any] = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    for _ in range(5):  # ≤5k objects
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            out[obj["Key"]] = (obj.get("Size", 0), _iso(obj.get("LastModified")))
        if not resp.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]
    return out


def list_documents(
    workspace: WorkspaceContext,
    kb_id: str,
    ds_id: str,
    *,
    page_size: int = 50,
    token: str | None = None,
) -> dict[str, Any]:
    """One page of a data source's documents (ListKnowledgeBaseDocuments,
    token-paginated) with the KB-side index status plus S3-side size and
    upload time joined in by object key."""
    client = agent_client(workspace)
    _require_managed(_get_kb(client, kb_id))
    kwargs: dict[str, Any] = {
        "knowledgeBaseId": kb_id,
        "dataSourceId": ds_id,
        "maxResults": page_size,
    }
    if token:
        kwargs["nextToken"] = token
    try:
        resp = client.list_knowledge_base_documents(**kwargs)
        full = client.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)["dataSource"]
    except client.exceptions.ResourceNotFoundException as exc:
        raise NotFoundError("kb.ds_not_found", "data source not found") from exc
    bucket, prefix = _parse_ds_location(full)
    meta: dict[str, tuple[int, str | None]] = {}
    if bucket:
        try:
            meta = _s3_object_meta(workspace, bucket, prefix)
        except Exception:
            meta = {}
    documents: list[dict[str, Any]] = []
    for doc in resp.get("documentDetails", []):
        uri = ((doc.get("identifier") or {}).get("s3") or {}).get("uri") or ""
        key = uri.removeprefix(f"s3://{bucket}/") if bucket else ""
        size, uploaded = meta.get(key, (None, None)) if key != uri else (None, None)
        documents.append(
            {
                "name": uri.rsplit("/", 1)[-1] or uri or "—",
                "uri": uri,
                "status": doc.get("status"),
                "status_reason": doc.get("statusReason"),
                "indexed_at": _iso(doc.get("updatedAt")),
                "size_bytes": size,
                "uploaded_at": uploaded,
            }
        )
    return {
        "documents": documents,
        "next_token": resp.get("nextToken"),
        "page_size": page_size,
    }


def create_kb(
    workspace: WorkspaceContext,
    name: str,
    description: str,
    source: dict[str, Any],
    *,
    creator: creators.Creator | None = None,
) -> dict[str, Any]:
    # an isolated project's KB takes the project role and prefix (§10.5, R10-kb);
    # every other one the workspace's shared role, as before
    placement = project_kb.placement_for_new_kb(workspace, current_project())
    role_arn = placement.role_arn
    if not role_arn:
        raise RuntimeError(
            "kb_role_arn missing from this workspace's resource map — run its bootstrap"
        )
    client = agent_client(workspace)
    kwargs: dict[str, Any] = {
        "name": name,
        "roleArn": role_arn,
        "knowledgeBaseConfiguration": {
            "type": "MANAGED",
            "managedKnowledgeBaseConfiguration": {"embeddingModelType": "MANAGED"},
        },
    }
    if description:
        kwargs["description"] = description
    # cost tags of the creating project (cost-tags design §1.2), then the isolated
    # project's tag the project boundary authorizes retrieval on (project_iam)
    kwargs["tags"] = {**cost_tags.context_tags(workspace_id=workspace.id), **placement.tags}
    created = client.create_knowledge_base(**kwargs)
    kb_id = created["knowledgeBase"]["knowledgeBaseId"]
    # AWS keeps no ledger row of a KB, so its creator is recorded beside it — now,
    # before the slow tail, so a dying thread cannot lose it
    _record_creator(workspace, kb_id, creator)
    # KB creation takes 1.5–3 min; never wait it out on the request. This used
    # to poll up to 60 s for an ACTIVE fast path, which behind a proxy with a
    # ~60 s origin timeout (CloudFront in the workshop deployment) cut the
    # connection mid-request — the browser then never ran its follow-up file
    # upload and the loss was silent (#19). The background thread owns the
    # whole tail instead: it polls until ACTIVE and creates the data source
    # idempotently (an already-ACTIVE KB is handled on its first pass, no
    # sleep). Uploads may land before the source exists — upload_files allows
    # that — and the detail view polls, auto-starts the first sync, and offers
    # a manual repair if the thread ever dies.
    _start_source_completion(workspace, kb_id, source)
    detail = get_kb_detail(workspace, kb_id)
    detail["source_pending"] = source
    return detail


def _start_source_completion(
    workspace: WorkspaceContext,
    kb_id: str,
    source: dict[str, Any],
    timeout_s: int = 900,
    interval_s: int = 10,
) -> threading.Thread:
    """Wait out the KB's CREATING state off-request, then create its data source.

    Owns its own client (a request's client must not outlive the request) and
    never raises: a failure here leaves the KB ACTIVE with zero data sources,
    which the detail view surfaces with a one-click repair.

    The workspace is captured in the closure: there is no KB ledger row to
    rehydrate it from, so the environment has to travel with the thread — and so
    does the request's project, which the thread runs inside.
    """
    project_id = current_project()

    def run() -> None:
        with project_context(project_id):
            _complete_source(workspace, kb_id, source, timeout_s, interval_s)

    thread = threading.Thread(target=run, name=f"kb-source-{kb_id}", daemon=True)
    thread.start()
    return thread


def _complete_source(
    workspace: WorkspaceContext,
    kb_id: str,
    source: dict[str, Any],
    timeout_s: int,
    interval_s: int,
) -> None:
    deadline = time.time() + timeout_s
    try:
        client = agent_client(workspace)
        while time.time() < deadline:
            detail = client.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
            status = detail.get("status")
            if status == "ACTIVE":
                placement = project_kb.placement_of(workspace, detail)
                _create_data_source(client, workspace, kb_id, source, placement)  # idempotent
                logger.info("kb %s: data source created after CREATING", kb_id)
                return
            if status in {"FAILED", "DELETING"}:
                logger.warning("kb %s: giving up source setup, status %s", kb_id, status)
                return
            # Deliberate polling interval; the surrounding loop owns the deadline.
            time.sleep(interval_s)  # nosemgrep: arbitrary-sleep
        logger.warning(
            "kb %s: still not ACTIVE after %ss — source not created", kb_id, timeout_s
        )
    except Exception:  # background best-effort; the UI repair path is the backstop
        logger.exception("kb %s: background data-source creation failed", kb_id)


def upload_files(
    workspace: WorkspaceContext, kb_id: str, files: list[tuple[str, bytes]]
) -> list[str]:
    artifacts = workspace.resources.get("artifacts_bucket")
    if not artifacts:
        raise RuntimeError(_MISSING_ARTIFACTS)
    client = agent_client(workspace)
    # the KB's prefix follows its role: `projects/<pid>/kb/<id>/` on an isolated
    # project's role, `kb/<id>/` otherwise
    prefix = project_kb.placement_of(workspace, _require_managed(_get_kb(client, kb_id))).prefix(
        kb_id
    )
    # zero data sources = an upload-mode KB whose source setup is still pending
    # (client replays it once the KB is ACTIVE) — files may land ahead of it
    if _list_data_sources(client, kb_id) and not _has_artifacts_data_source(
        client, kb_id, artifacts, prefix
    ):
        raise AppError(
            "kb.no_upload_target",
            "this KB has no data source on the uploads bucket — uploads are only "
            "available for KBs created in upload mode",
            status_code=409,
        )
    s3 = workspace.client("s3")
    keys: list[str] = []
    for filename, data in files:
        key = f"{prefix}{_safe_filename(filename)}"
        s3.put_object(Bucket=artifacts, Key=key, Body=data)
        keys.append(key)
    return keys


_RUNNING_JOB = {"STARTING", "IN_PROGRESS", "STOPPING"}


def upload_and_sync(
    workspace: WorkspaceContext, kb_id: str, files: list[tuple[str, bytes]]
) -> dict[str, Any]:
    """Upload, then start ingestion on the KB's upload source so the new files are
    indexed without a separate "Sync now". The upload stands whatever the sync does:
    ``sync.status`` tells the console what happened —

    - ``started``: a job is running for these files (``job_id``);
    - ``running``: a job was already running and may not see the new files —
      the console re-syncs once it finishes;
    - ``not_ready``: the source is still provisioning — its first sync picks them up;
    - ``pending_source``: the KB has no upload source yet (slow create) — likewise;
    - ``failed``: the sync could not start (``message``); the manual sync remains.
    """
    keys = upload_files(workspace, kb_id, files)
    return {"keys": keys, "sync": _sync_upload_source(workspace, kb_id)}


def _sync_upload_source(workspace: WorkspaceContext, kb_id: str) -> dict[str, Any]:
    artifacts = workspace.resources.get("artifacts_bucket") or ""
    try:
        client = agent_client(workspace)
        prefix = project_kb.placement_of(workspace, _get_kb(client, kb_id)).prefix(kb_id)
        ds = _artifacts_data_source(client, kb_id, artifacts, prefix)
        if ds is None:
            return {"status": "pending_source"}
        ds_id = ds["dataSourceId"]
        if (ds.get("status") or "").upper() != "AVAILABLE":
            return {"status": "not_ready", "ds_id": ds_id}
        if any(j["status"] in _RUNNING_JOB
               for j in _recent_ingestion_jobs(client, kb_id, ds_id, cap=3)):
            return {"status": "running", "ds_id": ds_id}
        try:
            job = client.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
        except client.exceptions.ConflictException:
            return {"status": "running", "ds_id": ds_id}  # a sync won the race
        except client.exceptions.ValidationException:
            return {"status": "not_ready", "ds_id": ds_id}
        return {"status": "started", "ds_id": ds_id,
                "job_id": job["ingestionJob"].get("ingestionJobId")}
    except Exception as exc:  # noqa: BLE001 — the files are stored; never fail the upload
        logger.warning("kb %s: sync after upload failed: %s", kb_id, cleanup_error(exc))
        return {"status": "failed", "message": cleanup_error(exc)}


def add_data_source(
    workspace: WorkspaceContext, kb_id: str, source: dict[str, Any]
) -> dict[str, Any]:
    client = agent_client(workspace)
    placement = project_kb.placement_of(workspace, _require_managed(_get_kb(client, kb_id)))
    _create_data_source(client, workspace, kb_id, source, placement)
    return get_kb_detail(workspace, kb_id)


def delete_data_source(
    workspace: WorkspaceContext, kb_id: str, ds_id: str
) -> dict[str, Any]:
    client = agent_client(workspace)
    _require_managed(_get_kb(client, kb_id))
    client.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    return {"deleted": True, "ds_id": ds_id}


def start_sync(workspace: WorkspaceContext, kb_id: str, ds_id: str) -> dict[str, Any]:
    client = agent_client(workspace)
    try:
        job = client.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    except (
        client.exceptions.ValidationException,
        client.exceptions.ConflictException,
    ) as exc:
        raise AppError(
            "kb.sync_not_ready",
            "cannot start ingestion — the data source may still be provisioning, "
            "or another sync is already running",
            status_code=409,
        ) from exc
    return _job_out(job["ingestionJob"])


def list_ingestion_jobs(
    workspace: WorkspaceContext, kb_id: str, ds_id: str
) -> list[dict[str, Any]]:
    return _recent_ingestion_jobs(agent_client(workspace), kb_id, ds_id, cap=50)


def update_description(
    workspace: WorkspaceContext, kb_id: str, description: str
) -> dict[str, Any]:
    client = agent_client(workspace)
    detail = _require_managed(_get_kb(client, kb_id))
    client.update_knowledge_base(
        knowledgeBaseId=kb_id,
        name=detail["name"],
        description=description or detail["name"],
        roleArn=detail["roleArn"],
        knowledgeBaseConfiguration=detail["knowledgeBaseConfiguration"],
    )
    _tag_kb(workspace, detail)
    return get_kb_detail(workspace, kb_id)


def _tag_kb(workspace: WorkspaceContext, detail: dict[str, Any]) -> None:
    """An update catches a KB up on its cost tags — those of the request's project,
    the one the KB is reachable from. Best-effort: a refusal is logged."""
    arn = detail.get("knowledgeBaseArn")
    if arn:
        cost_tags.tag_quietly(workspace, cost_tags.entry(cost_tags.KNOWLEDGE_BASE, arn=arn),
                              cost_tags.context_tags(workspace_id=workspace.id), logger.info)


def _location_uri(location: dict[str, Any]) -> str | None:
    for sub in location.values():
        if isinstance(sub, dict):
            uri = sub.get("uri") or sub.get("url")
            if uri:
                return uri
    return None


def _result_out(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": (result.get("content") or {}).get("text", ""),
        "score": result.get("score"),
        "location_uri": _location_uri(result.get("location") or {}),
        "metadata": result.get("metadata") or {},
    }


def query(
    workspace: WorkspaceContext, kb_id: str, text: str, number_of_results: int = 8
) -> list[dict[str, Any]]:
    runtime = agent_runtime_client(workspace)
    try:
        resp = runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": text},
            retrievalConfiguration={
                "managedSearchConfiguration": {"numberOfResults": number_of_results}
            },
        )
    except (
        runtime.exceptions.ValidationException,
        runtime.exceptions.AccessDeniedException,
    ) as exc:
        # a KB can be broken for ANY caller (index still building, policy
        # drift) — surface it readably instead of a raw 500
        raise AppError(
            "kb.query_failed",
            f"retrieval failed on the knowledge base side: {exc}",
            status_code=502,
        ) from exc
    return [_result_out(r) for r in resp.get("retrievalResults", [])]


# ── source documents (the links a retrieval result cites) ─────────────────

# Managed KB retrieval cites ``location.s3Location.uri`` — an https URL into the data
# source's (private) bucket — so an agent's "source" link answers AccessDenied in a
# browser. The console resolves it to a short-lived presigned GET instead.
SOURCE_URL_TTL_S = 300
_SOURCE_SIGV4 = "kb-source-s3v4"
# rendered as text in the browser (an .html source must not run on the S3 origin)
_TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".csv", ".json", ".html", ".htm", ".xml")


def parse_source_uri(uri: str) -> tuple[str, str] | None:
    """(bucket, key) of an S3 object address, or None when ``uri`` is not one.

    Accepts ``s3://bucket/key`` and the https forms retrieval results carry:
    virtual-hosted ``bucket.s3[.region].amazonaws.com/key`` and path-style
    ``s3[.region].amazonaws.com/bucket/key``. The https key is percent-decoded."""
    uri = (uri or "").strip()
    if uri.startswith("s3://"):
        bucket, _, key = uri[5:].partition("/")
        return (bucket, key) if bucket and key else None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(".amazonaws.com"):
        return None
    path = unquote(parts.path).lstrip("/")
    if host.startswith(("s3.", "s3-")):  # path-style
        bucket, _, key = path.partition("/")
    else:  # virtual-hosted: <bucket>.s3.<region>.amazonaws.com
        cut = max(host.rfind(".s3."), host.rfind(".s3-"))
        if cut <= 0:
            return None
        bucket, key = host[:cut], path
    return (bucket, key) if bucket and key else None


def _source_owner(
    client: Any, bucket: str, key: str, visible: Callable[[str], bool]
) -> dict[str, Any] | None:
    """The visible managed KB whose S3 data source holds ``bucket/key``."""
    for summary in _paginate(
        client.list_knowledge_bases, "knowledgeBaseSummaries", maxResults=100
    ):
        kb_id = summary["knowledgeBaseId"]
        if not visible(kb_id):
            continue
        detail = client.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
        if _kb_type(detail) != "MANAGED":
            continue
        for ds in _list_data_sources(client, kb_id):
            full = client.get_data_source(
                knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"]
            )["dataSource"]
            ds_bucket, prefix = _parse_ds_location(full)
            if ds_bucket == bucket and key.startswith(prefix or ""):
                return {"kb_id": kb_id, "kb_name": detail.get("name")}
    return None


def source_link(
    workspace: WorkspaceContext, uri: str, visible: Callable[[str], bool]
) -> dict[str, Any]:
    """A presigned GET for a document a retrieval result cites.

    Only objects inside a data source of a managed KB the caller may see are
    signed — the endpoint must never become a reader of arbitrary S3 objects the
    backend's credentials reach. A hidden KB answers like a missing one."""
    parsed = parse_source_uri(uri)
    if parsed is None:
        raise AppError(
            "kb.source_unsupported", "not an S3 object address", status_code=400
        )
    bucket, key = parsed
    owner = _source_owner(agent_client(workspace), bucket, key, visible)
    if owner is None:
        raise NotFoundError(
            "kb.source_not_found", "no visible knowledge base has this document"
        )
    s3 = workspace.client(
        "s3", cache_token=_SOURCE_SIGV4, config=Config(signature_version="s3v4")
    )
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code")
        if status == 404 or code in ("404", "NoSuchKey", "NotFound"):
            raise NotFoundError(
                "kb.source_missing", "the document is no longer in the data source"
            ) from exc
        raise AppError(
            "kb.source_unreadable",
            f"the backend cannot read this document: {exc}",
            status_code=502,
        ) from exc
    name = key.rsplit("/", 1)[-1]
    params: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "ResponseContentDisposition": f"inline; filename*=UTF-8''{quote(name)}",
    }
    if name.lower().endswith(_TEXT_SUFFIXES):
        params["ResponseContentType"] = "text/plain; charset=utf-8"
    url = s3.generate_presigned_url(
        "get_object", Params=params, ExpiresIn=SOURCE_URL_TTL_S
    )
    return {"url": url, "name": name, "expires_in": SOURCE_URL_TTL_S, **owner}


def _delete_uploaded_files(workspace: WorkspaceContext, kb_id: str, prefix: str) -> int:
    """Remove the KB's uploaded files from the artifacts bucket once its delete is
    accepted. Only the platform-owned, KB-id-scoped prefixes are touched — the KB's
    own (``placement.prefix``) and the shared ``kb/<id>/`` a project migration copied
    from; an ``existing``-mode bucket is the caller's and is never touched.
    Best-effort: a failure is logged, the delete stands."""
    artifacts = workspace.resources.get("artifacts_bucket")
    if not artifacts:
        return 0
    removed = 0
    try:
        s3 = workspace.client("s3")
        for scoped in dict.fromkeys((prefix, f"kb/{kb_id}/")):
            token: str | None = None
            while True:
                page = s3.list_objects_v2(
                    Bucket=artifacts, Prefix=scoped,
                    **({"ContinuationToken": token} if token else {}),
                )
                keys = [{"Key": o["Key"]} for o in page.get("Contents") or []
                        if isinstance(o, dict) and o.get("Key")]
                if keys:  # ≤ 1000 per page = DeleteObjects' own cap
                    s3.delete_objects(Bucket=artifacts, Delete={"Objects": keys, "Quiet": True})
                    removed += len(keys)
                token = page.get("NextContinuationToken")
                if page.get("IsTruncated") is not True or not isinstance(token, str):
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb %s: uploaded files under s3://%s/%s left behind: %s",
                       kb_id, artifacts, prefix, cleanup_error(exc))
    if removed:
        logger.info("kb %s: removed %d uploaded file(s)", kb_id, removed)
    return removed


def delete_kb(
    workspace: WorkspaceContext, kb_id: str, force: bool = False
) -> dict[str, Any]:
    # Preflight, ledger-only, before the first AWS read: a preset mount blocks the
    # delete outright (force or not) — see refuse_if_attached_to_system_agent.
    refuse_if_attached_to_system_agent(workspace, kb_id)
    client = agent_client(workspace)
    detail = _require_managed(_get_kb(client, kb_id))
    if (detail.get("status") or "").upper() == "DELETING":
        # an earlier delete was accepted; Bedrock is still tearing the managed store
        # down (minutes) — repeating it would only answer Conflict
        return {"deleted": True, "kb_id": kb_id}
    placement = project_kb.placement_of(workspace, detail)
    agents = attached_agents(workspace, kb_id)
    if agents and not force:
        raise KBAttachedError(kb_id, agents)
    if agents:  # force path — unmount from every agent before tearing down
        _strip_kb_from_agents(workspace, kb_id)

    for ds in _list_data_sources(client, kb_id):
        try:  # async deletion — best-effort; DeleteKnowledgeBase cascades the rest
            client.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"])
        except Exception as exc:
            if not is_not_found(exc):
                logger.warning(
                    "kb %s: data source %s delete failed: %s",
                    kb_id, ds["dataSourceId"], cleanup_error(exc),
                )

    # only touch the gateway target if the gateway already exists — never
    # provision the KB gateway during a delete
    gateway_id = workspace.resources.get("kb_gateway_id")
    if gateway_id:
        kb_gateway.delete_retrieve_target(control_client(workspace), gateway_id, kb_id)

    if placement.project_id:  # the external grants sit on the project's KB role
        _delete_kb_policy(workspace, kb_id, placement.role_name)
    else:
        _delete_kb_policy(workspace, kb_id)

    try:
        client.delete_knowledge_base(knowledgeBaseId=kb_id)
    except client.exceptions.ConflictException as exc:
        if (detail.get("status") or "").upper() == "CREATING":
            raise AppError(
                "kb.delete_conflict",
                "the KB is still being created — wait for it to become ACTIVE before deleting",
                status_code=409,
            ) from exc
        raise AppError(
            "kb.delete_busy",
            "another operation is still running on this KB (an ingestion job, or its data "
            "sources being deleted) — retry in a few minutes",
            status_code=409,
        ) from exc
    _delete_uploaded_files(workspace, kb_id, placement.prefix(kb_id))
    db = SessionLocal()
    try:
        creators.forget(db, workspace.id, creators.KB, kb_id)
    finally:
        db.close()
    return {"deleted": True, "kb_id": kb_id}


def sample_document_names(workspace: WorkspaceContext, kb_id: str, limit: int = 8) -> list[str]:
    """A few document file names from the KB's first data source (best effort).

    Feeds the suggested-questions prompt only, so it stays to one data source and
    one page; any AWS failure is the caller's to swallow.
    """
    sources = _list_data_sources(agent_client(workspace), kb_id)
    if not sources:
        return []
    page = list_documents(workspace, kb_id, sources[0]["dataSourceId"], page_size=limit)
    return [d["name"] for d in page["documents"] if d.get("name") and d["name"] != "—"][:limit]


def _record_creator(
    workspace: WorkspaceContext, kb_id: str, creator: creators.Creator | None
) -> None:
    # also with no creator: the row is the KB's project ownership (creators.record)
    db = SessionLocal()
    try:
        creators.record(db, workspace.id, creators.KB, kb_id, creator)
    finally:
        db.close()
