"""Application inference profiles per isolated project (projects design §10.8, §10.2).

Model invocations are billed to the model, not to the runtime that made them, so the
runtime's `launchpad:project` tag never reaches the Bedrock line items. An
*application inference profile* is the Bedrock resource that can carry a tag: one per
(project, workspace, model), copied from the model (or the system cross-region profile
the spec names), tagged `launchpad:project` / `launchpad:workspace`, and used as the
model id of the project's agents. Cost Explorer then splits model spend by the tag
(after the payer account activates it as a cost allocation tag — an operator step),
and the console's estimate (`services/costs.py`) maps the profile ARN in a span back
to its project and its model's price.

Behind the `project_inference_profiles` setting (off by default) and only for an
isolated project: a shared project's agents keep their configured model id.

Each profile is one `project_resources` row of kind `inference_profile:<model id>`
(`resource_id` = profile id, `arn`, `detail.model` / `detail.source`). `ensure_profile`
is idempotent like every provisioner: the recorded row, else a profile of the
deterministic name found by ListInferenceProfiles (a run that died after the create),
else CreateInferenceProfile — recorded the moment it exists. The Bedrock client is
injected (`workspace.client("bedrock")` in production, a stub in tests).

UNVERIFIED live: CreateInferenceProfile's `copyFrom` taking a geographic system
profile ARN for every model family the console offers, and the harness accepting an
application profile ARN as `bedrockModelConfig.modelId` (the Converse API does).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.project_isolation import (
    KIND_INFERENCE_PROFILE_PREFIX,
    STATUS_READY,
    ProjectResource,
)

logger = logging.getLogger(__name__)

# `inferenceProfileName`: 1–64 of `([0-9a-zA-Z][ _-]?)+`
_NAME_MAX = 64
_NAME_SEPARATORS = re.compile(r"[^0-9A-Za-z]+")
# `project_resources.kind` is VARCHAR(96)
_KIND_MAX = 96
# the system (cross-region) inference profile prefixes; anything else is a model id
_SYSTEM_PROFILE_PREFIXES = ("us-gov.", "us.", "eu.", "apac.", "jp.", "au.", "ca.", "global.")
_APPLICATION_PROFILE = re.compile(r"application-inference-profile/([A-Za-z0-9]+)")
_CACHE_TTL_S = 60.0


class InferenceProfileError(RuntimeError):
    """A profile could not be made or adopted; the message is for the deploy log."""


def enabled() -> bool:
    """The `project_inference_profiles` setting (LAUNCHPAD_PROJECT_INFERENCE_PROFILES)."""
    return bool(getattr(get_settings(), "project_inference_profiles", False))


def kind_for(model_id: str) -> str:
    kind = f"{KIND_INFERENCE_PROFILE_PREFIX}{model_id}"
    if not model_id or len(kind) > _KIND_MAX:
        raise InferenceProfileError(f"model id {model_id!r} cannot key an inference profile")
    return kind


def profile_name(project_id: str, model_id: str) -> str:
    """`lp-<project>-<model>` with every run of other characters as one `-`; a name
    over the limit keeps its head and gains a digest of the full one."""
    slug = _NAME_SEPARATORS.sub("-", f"lp-{project_id}-{model_id}").strip("-")
    if len(slug) <= _NAME_MAX:
        return slug
    digest = hashlib.sha256(slug.encode()).hexdigest()[:10]
    return f"{slug[:_NAME_MAX - len(digest) - 1].rstrip('-')}-{digest}"


def source_arn(model_id: str, workspace: Any) -> str:
    """`modelSource.copyFrom`: the system profile a geographic id names, else the
    foundation model."""
    partition = getattr(workspace, "partition", "aws")
    if model_id.startswith(_SYSTEM_PROFILE_PREFIXES):
        return (f"arn:{partition}:bedrock:{workspace.region}:{workspace.account_id}:"
                f"inference-profile/{model_id}")
    return f"arn:{partition}:bedrock:{workspace.region}::foundation-model/{model_id}"


@dataclass(frozen=True)
class Profile:
    project_id: str
    workspace_id: str
    model_id: str
    profile_id: str | None
    arn: str


def _row_profile(row: ProjectResource) -> Profile | None:
    if not row.arn or row.status != STATUS_READY:
        return None
    model = (row.detail or {}).get("model") or row.kind.removeprefix(
        KIND_INFERENCE_PROFILE_PREFIX)
    return Profile(row.project_id, row.workspace_id, model, row.resource_id, row.arn)


def _find_by_name(bedrock: Any, name: str) -> dict[str, Any] | None:
    token = None
    while True:
        kwargs: dict[str, Any] = {"typeEquals": "APPLICATION", "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = bedrock.list_inference_profiles(**kwargs)
        for summary in page.get("inferenceProfileSummaries") or []:
            if summary.get("inferenceProfileName") == name:
                return summary
        token = page.get("nextToken")
        if not token:
            return None


def _record(db: Session, *, project_id: str, workspace_id: str, model_id: str,
            arn: str, profile_id: str | None, name: str, source: str) -> Profile:
    kind = kind_for(model_id)
    row = db.get(ProjectResource, (project_id, workspace_id, kind))
    if row is None:
        row = ProjectResource(project_id=project_id, workspace_id=workspace_id, kind=kind)
        db.add(row)
    row.resource_id = profile_id
    row.arn = arn
    row.status = STATUS_READY
    row.error = None
    row.detail = {"model": model_id, "name": name, "source": source}
    db.commit()
    invalidate()
    return Profile(project_id, workspace_id, model_id, profile_id, arn)


def _profile_id(arn: str) -> str | None:
    match = _APPLICATION_PROFILE.search(arn or "")
    return match.group(1) if match else None


def ensure_profile(db: Session, bedrock: Any, *, project_id: str, workspace: Any,
                   model_id: str, log: Callable[[str], None] | None = None) -> Profile:
    """The project's profile for `model_id` in `workspace`: recorded, adopted or made."""
    say = log or (lambda _msg: None)
    kind = kind_for(model_id)
    row = db.get(ProjectResource, (project_id, workspace.id, kind))
    if row is not None and (found := _row_profile(row)) is not None:
        return found
    name = profile_name(project_id, model_id)
    source = source_arn(model_id, workspace)
    from app.services.project_telemetry import project_tags

    try:
        existing = _find_by_name(bedrock, name)
        if existing is not None:
            arn = existing["inferenceProfileArn"]
            say(f"inference profile {name} adopted · {arn}")
        else:
            created = bedrock.create_inference_profile(
                inferenceProfileName=name,
                # `([0-9a-zA-Z:.][ _-]?)+`: single separators only
                description=f"Launchpad project {project_id} model {model_id}"[:200],
                # deterministic: a retried create after a lost response is the same one
                clientRequestToken=hashlib.sha256(
                    f"{workspace.id}:{name}".encode()).hexdigest()[:64],
                modelSource={"copyFrom": source},
                tags=[{"key": k, "value": v} for k, v in project_tags(
                    project_id, workspace_id=workspace.id).items()],
            )
            arn = created["inferenceProfileArn"]
            say(f"inference profile {name} created · {arn}")
    except (ClientError, BotoCoreError, KeyError) as exc:
        code = exc.response.get("Error", {}).get("Code") if isinstance(
            exc, ClientError) else None
        raise InferenceProfileError(
            f"could not create the project's inference profile for {model_id}: "
            f"{code or type(exc).__name__} — check bedrock:CreateInferenceProfile / "
            "TagResource on the workspace role, or turn project_inference_profiles off"
        ) from exc
    return _record(db, project_id=project_id, workspace_id=workspace.id, model_id=model_id,
                   arn=arn, profile_id=existing.get("inferenceProfileId") if existing
                   else _profile_id(arn), name=name, source=source)


def model_id_for(db: Session, *, project_id: str | None, workspace: Any, model_id: str,
                 bedrock: Any = None, log: Callable[[str], None] | None = None) -> str:
    """The model id a project's agent is deployed with: its profile ARN when the
    setting is on and the project isolated; `model_id` unchanged otherwise."""
    if not (enabled() and project_id and model_id):
        return model_id
    from app.services.project_telemetry import attributed

    if not attributed(project_id):
        return model_id
    client = bedrock if bedrock is not None else workspace.client("bedrock")
    return ensure_profile(db, client, project_id=project_id, workspace=workspace,
                          model_id=model_id, log=log).arn


# ── span model → profile (cost attribution) ───────────────────────────────────

_cache_lock = threading.Lock()
_cache: dict[str, Profile] | None = None
_cache_at = 0.0


def invalidate() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _profiles() -> dict[str, Profile]:
    """Every recorded profile by ARN and by profile id, re-read at most once a minute
    (span rows are priced in bulk; the ledger changes on a deploy)."""
    global _cache, _cache_at
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache_at < _CACHE_TTL_S:
            return _cache
    from app.core.db import SessionLocal
    from app.core.project_scope import project_context

    db = SessionLocal()
    try:
        with project_context(None):
            rows = db.scalars(select(ProjectResource).where(
                ProjectResource.kind.startswith(KIND_INFERENCE_PROFILE_PREFIX)
            )).all()
        found: dict[str, Profile] = {}
        for row in rows:
            profile = _row_profile(row)
            if profile is None:
                continue
            found[profile.arn] = profile
            if profile.profile_id:
                found[profile.profile_id] = profile
    finally:
        db.close()
    with _cache_lock:
        _cache, _cache_at = found, time.monotonic()
    return found


def profile_for_model(model: str | None) -> Profile | None:
    """The recorded profile a span's model names (its ARN, or its id), or None."""
    if not model or "application-inference-profile" not in model:
        return None
    profiles = _profiles()
    if model in profiles:
        return profiles[model]
    profile_id = _profile_id(model)
    return profiles.get(profile_id) if profile_id else None


def price_model(model: str | None) -> str | None:
    """The model a span's id is priced as: an application profile's underlying model."""
    profile = profile_for_model(model)
    return profile.model_id if profile is not None else model
