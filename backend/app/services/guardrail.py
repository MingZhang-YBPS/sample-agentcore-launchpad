"""PII protection on the platform's own invoke chain (roadmap T12).

Why here and not in the agent: a managed Harness runs its model call inside
AgentCore, so the platform cannot pass a `guardrailConfig` down to it. What the
platform *does* own is both ends of the conversation — the prompt it forwards and
the answer it returns — so an opt-in agent has its traffic screened with the
Bedrock Runtime `ApplyGuardrail` API on the way in and on the way out. The result
is the same PII posture for every creation method, including BYOC code the platform
never reads.

One guardrail per workspace, Launchpad-owned and named `launchpad-pii`, created on
first use and remembered in the workspace's `resources` map
(`guardrail_id` / `guardrail_version`). It is created with the PII entity set below
in the mode the agent asked for:

* `anonymize` (default) — entities come back masked (`{EMAIL}`, `{PHONE}`); the
  conversation continues with the masked text.
* `block` — a hit refuses the turn with `guardrail.blocked`, so nothing with PII in
  it ever reaches the model or the caller.

The guardrail resource itself always declares `ANONYMIZE`; blocking is the
platform's reading of the intervention, not a second AWS resource. That keeps one
resource per workspace no matter how many agents opt in with which mode.
"""

from dataclasses import dataclass
from typing import Any, Literal

from botocore.exceptions import ClientError

from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

GUARDRAIL_NAME = "launchpad-pii"
GuardrailMode = Literal["anonymize", "block"]

# The entities a PII preset is expected to cover. Deliberately the contact and
# credential ones rather than every entity Bedrock offers: a preset that masks, say,
# AGE or NAME breaks ordinary HR questions ("how much leave does Mei have left?").
PII_ENTITIES: tuple[str, ...] = (
    "EMAIL",
    "PHONE",
    "CREDIT_DEBIT_CARD_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "US_BANK_ACCOUNT_NUMBER",
    "PASSWORD",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "IP_ADDRESS",
)

_BLOCKED_MESSAGE = (
    "This message was withheld because it contains personal or sensitive data "
    "(the agent has PII protection set to block)."
)


@dataclass(frozen=True)
class Screened:
    """One screening result. ``text`` is what the caller should use downstream."""

    text: str
    intervened: bool
    entities: tuple[str, ...] = ()


def guardrail_config(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """The agent's opt-in, or None. Shape: ``{"enabled": bool, "mode": ...}``."""
    raw = (spec or {}).get("guardrail")
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    mode = raw.get("mode")
    return {"mode": mode if mode in ("anonymize", "block") else "anonymize"}


def _identifier(workspace: WorkspaceContext) -> tuple[str, str] | None:
    resources = workspace.resources or {}
    gid = resources.get("guardrail_id")
    version = resources.get("guardrail_version")
    if gid and version:
        return str(gid), str(version)
    return None


def _pii_entities_config() -> dict[str, Any]:
    return {
        "piiEntitiesConfig": [
            {"type": entity, "action": "ANONYMIZE"} for entity in PII_ENTITIES
        ]
    }


def describe(workspace: WorkspaceContext) -> dict[str, Any]:
    """What the console shows: whether this workspace has the preset, and its state."""
    ident = _identifier(workspace)
    if ident is None:
        return {"provisioned": False, "entities": list(PII_ENTITIES)}
    gid, version = ident
    try:
        info = workspace.client("bedrock").get_guardrail(
            guardrailIdentifier=gid, guardrailVersion=version
        )
    except ClientError as exc:
        return {
            "provisioned": True,
            "id": gid,
            "version": version,
            "status": "unavailable",
            "error": exc.response.get("Error", {}).get("Code", "ClientError"),
            "entities": list(PII_ENTITIES),
        }
    return {
        "provisioned": True,
        "id": gid,
        "version": version,
        "status": info.get("status"),
        "name": info.get("name"),
        "entities": list(PII_ENTITIES),
    }


def ensure_guardrail(workspace: WorkspaceContext, db: Any = None) -> tuple[str, str]:
    """The workspace's PII guardrail, created on first use.

    Idempotent: an existing id in the resource map is returned untouched, and a
    concurrent creation is tolerated by looking the name up before creating. When
    ``db`` is given the resource map is persisted on that session (the caller
    commits); without it the ids live only on the in-memory context, so the next
    process re-reads or re-creates them.
    """
    ident = _identifier(workspace)
    if ident is not None:
        return ident

    client = workspace.client("bedrock")
    found: tuple[str, str] | None = None
    try:
        paginator = client.get_paginator("list_guardrails")
        for page in paginator.paginate():
            for row in page.get("guardrails", []):
                if row.get("name") == GUARDRAIL_NAME:
                    found = (str(row["id"]), str(row.get("version") or "DRAFT"))
                    break
            if found:
                break
    except ClientError:
        found = None

    if found is None:
        try:
            created = client.create_guardrail(
                name=GUARDRAIL_NAME,
                description="Launchpad PII preset — masks contact and credential data.",
                sensitiveInformationPolicyConfig=_pii_entities_config(),
                blockedInputMessaging=_BLOCKED_MESSAGE,
                blockedOutputsMessaging=_BLOCKED_MESSAGE,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            raise AppError(
                "guardrail.create_failed",
                f"could not create the '{GUARDRAIL_NAME}' guardrail ({code})",
                status_code=502,
            ) from exc
        found = (str(created["guardrailId"]), str(created.get("version") or "DRAFT"))

    gid, version = found
    resources = workspace.resources if workspace.resources is not None else {}
    resources["guardrail_id"] = gid
    resources["guardrail_version"] = version
    if db is not None:
        from app.services.workspace import get_workspace_row

        row = get_workspace_row(db, workspace.id)
        if row is not None:
            merged = dict(row.resources or {})
            merged["guardrail_id"] = gid
            merged["guardrail_version"] = version
            row.resources = merged
    return gid, version


def screen(
    text: str,
    *,
    source: Literal["INPUT", "OUTPUT"],
    mode: GuardrailMode,
    workspace: WorkspaceContext,
) -> Screened:
    """Run one side of a turn through the workspace's PII guardrail.

    Fails **open** on an AWS error (the screen is unavailable, not the answer): a
    refusal here would take a working agent offline because a guardrail call
    timed out. A genuine PII hit in `block` mode raises `guardrail.blocked`.
    """
    if not text:
        return Screened(text=text, intervened=False)
    try:
        gid, version = ensure_guardrail(workspace)
    except AppError:
        return Screened(text=text, intervened=False)
    try:
        result = workspace.client("bedrock-runtime").apply_guardrail(
            guardrailIdentifier=gid,
            guardrailVersion=version,
            source=source,
            content=[{"text": {"text": text}}],
        )
    except ClientError:
        return Screened(text=text, intervened=False)

    if result.get("action") != "GUARDRAIL_INTERVENED":
        return Screened(text=text, intervened=False)

    entities = tuple(
        str(match.get("type"))
        for assessment in result.get("assessments", [])
        for match in (assessment.get("sensitiveInformationPolicy", {}) or {}).get(
            "piiEntities", []
        )
        if match.get("type")
    )
    if mode == "block":
        raise AppError(
            "guardrail.blocked",
            _BLOCKED_MESSAGE,
            {"source": source, "entities": sorted(set(entities))},
            status_code=422,
        )
    masked = "".join(
        part.get("text", "") for part in result.get("outputs", []) if isinstance(part, dict)
    )
    return Screened(text=masked or text, intervened=True, entities=entities)
