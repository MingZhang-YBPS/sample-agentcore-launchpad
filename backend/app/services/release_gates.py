"""Blocking release gates, the deploy window and the execution plan (roadmap T26).

`promotion.evaluate_gates` is the approver's view: DB-only, advisory, recorded as evidence
at approval time. This module is the *executor's* view. It re-reads the world at the moment
someone presses Execute (and for the plan preview), adds the gates that need a live read
(the target gateway's Policy Engine mode) or a clock (the deploy window), and decides which
failures refuse the run.

Separation on purpose: approving a flagged bundle is a human accepting a risk and is
recorded; executing one is the platform refusing to act on a check that failed. So an
approver may wave a bundle through with, say, an unmapped reference, but `execute` will not
ship it.

**Which gates block.** Everything except `ADVISORY_GATES`. A gate added later (the resource
mapping gate from T23 is one) blocks by default — the safe direction for a check nobody
classified.

**Policy** lives on the *target* workspace (`workspaces.release_policy`), edited by an
administrator, because the target's owner decides what may land there. Unset keys fall back
to tier defaults: `prod` demands ENFORCE and a five-minute observation window, `dev` demands
neither.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import Agent, Promotion, ReleaseBundle, Workspace
from app.services import promotion as promotion_service

# Gates that inform but never refuse an execution.
ADVISORY_GATES = frozenset({"guardrail", "target_agent"})

TIER_DEFAULTS: dict[str, dict[str, Any]] = {
    "prod": {"require_policy_enforce": True, "observe_seconds": 300},
    "staging": {"require_policy_enforce": False, "observe_seconds": 60},
    "dev": {"require_policy_enforce": False, "observe_seconds": 0},
}
# Methods whose target build is reproducible from the spec alone.
SPEC_BUILT_METHODS = frozenset({"harness", "zip_runtime", "studio"})
DEFAULT_MIN_EVAL_SCORE = 0.7
MAX_OBSERVE_SECONDS = 6 * 3600
DEFAULT_SMOKE_PROMPTS = (
    "Hello! Introduce yourself in one short sentence.",
    "What kinds of questions can you help me with?",
    "Reply with the single word: ready.",
)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EVAL_RUN_OK = ("completed", "succeeded")


# ── policy ───────────────────────────────────────────────────────────────────────


def _parse_instant(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(
            "release_policy.invalid", f"{field} is not an ISO timestamp", status_code=422
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_policy(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate an administrator's policy and return only the keys they set."""
    out: dict[str, Any] = {}
    if raw.get("min_eval_score") is not None:
        score = raw["min_eval_score"]
        if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= 1:
            raise AppError(
                "release_policy.invalid", "min_eval_score must be between 0 and 1",
                status_code=422,
            )
        out["min_eval_score"] = float(score)
    if raw.get("require_policy_enforce") is not None:
        out["require_policy_enforce"] = bool(raw["require_policy_enforce"])
    if raw.get("observe_seconds") is not None:
        seconds = raw["observe_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not (
            0 <= seconds <= MAX_OBSERVE_SECONDS
        ):
            raise AppError(
                "release_policy.invalid",
                f"observe_seconds must be a whole number between 0 and {MAX_OBSERVE_SECONDS}",
                status_code=422,
            )
        out["observe_seconds"] = seconds
    prompts = raw.get("smoke_prompts")
    if prompts:
        if (
            not isinstance(prompts, list)
            or len(prompts) > 10
            or not all(isinstance(p, str) and p.strip() and len(p) <= 2000 for p in prompts)
        ):
            raise AppError(
                "release_policy.invalid",
                "smoke_prompts is at most 10 non-empty strings",
                status_code=422,
            )
        out["smoke_prompts"] = [p.strip() for p in prompts]
    window = raw.get("window")
    if window:
        tz = str(window.get("timezone") or "UTC")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise AppError(
                "release_policy.invalid", f"unknown timezone '{tz}'", status_code=422
            ) from exc
        days = window.get("days")
        days = list(range(7)) if days in (None, []) else days
        if not isinstance(days, list) or not all(
            isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days
        ):
            raise AppError(
                "release_policy.invalid", "window.days are integers 0 (Mon) to 6 (Sun)",
                status_code=422,
            )
        start, end = str(window.get("start") or ""), str(window.get("end") or "")
        if not (_TIME_RE.match(start) and _TIME_RE.match(end)) or start >= end:
            raise AppError(
                "release_policy.invalid",
                "window.start and window.end are HH:MM with start before end",
                status_code=422,
            )
        out["window"] = {"timezone": tz, "days": sorted(set(days)), "start": start, "end": end}
    freezes = raw.get("freezes")
    if freezes:
        cleaned = []
        for entry in freezes:
            begin = _parse_instant(entry.get("start"), "freeze.start")
            finish = _parse_instant(entry.get("end"), "freeze.end")
            if finish <= begin:
                raise AppError(
                    "release_policy.invalid", "a freeze must end after it starts",
                    status_code=422,
                )
            cleaned.append(
                {
                    "start": begin.isoformat(),
                    "end": finish.isoformat(),
                    "reason": str(entry.get("reason") or "")[:200],
                }
            )
        out["freezes"] = cleaned
    # Agent-DLC (docs/agent-dlc-design.md §6.2, §4.4)
    if raw.get("release_mode") is not None:
        if raw["release_mode"] not in ("direct", "gated"):
            raise AppError(
                "release_policy.invalid", "release_mode must be 'direct' or 'gated'",
                status_code=422,
            )
        out["release_mode"] = raw["release_mode"]
    if raw.get("calibration") is not None:
        from app.dlc.calibration import normalize_policy as normalize_calibration

        out["calibration"] = normalize_calibration(raw["calibration"])
    if raw.get("eval_cost_confirm_usd") is not None or raw.get("eval_cost_max_usd") is not None:
        for key in ("eval_cost_confirm_usd", "eval_cost_max_usd"):
            value = raw.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise AppError("release_policy.invalid", f"{key} must be ≥ 0", status_code=422)
            out[key] = float(value)
    return out


def effective_policy(target: Workspace) -> dict[str, Any]:
    """The stored policy laid over the target tier's defaults."""
    tier = (target.tier or "dev") if target.tier in TIER_DEFAULTS else "dev"
    policy: dict[str, Any] = {
        "min_eval_score": DEFAULT_MIN_EVAL_SCORE,
        "window": None,
        "freezes": [],
        "smoke_prompts": list(DEFAULT_SMOKE_PROMPTS),
        **TIER_DEFAULTS[tier],
    }
    policy.update(getattr(target, "release_policy", None) or {})
    return policy


# ── deploy window ────────────────────────────────────────────────────────────────


def _in_window(window: dict[str, Any], instant: datetime) -> bool:
    local = instant.astimezone(ZoneInfo(window["timezone"]))
    clock = local.strftime("%H:%M")
    return local.weekday() in window["days"] and window["start"] <= clock < window["end"]


def _active_freeze(freezes: list[dict[str, Any]], instant: datetime) -> dict[str, Any] | None:
    for freeze in freezes:
        begin = _parse_instant(freeze["start"], "start")
        if begin <= instant < _parse_instant(freeze["end"], "end"):
            return freeze
    return None


def _allowed_at(policy: dict[str, Any], instant: datetime) -> bool:
    window = policy.get("window")
    if window and not _in_window(window, instant):
        return False
    return _active_freeze(policy.get("freezes") or [], instant) is None


def next_allowed_time(policy: dict[str, Any], now: datetime) -> datetime | None:
    """The earliest instant at or after `now` the policy allows a release, or None.

    Candidate instants are `now`, the end of every freeze and the start of the window on
    each of the next 15 days; the answer is the first candidate that is itself allowed.
    Exact (no minute stepping) because both edges of any allowed interval are candidates.
    """
    candidates = [now]
    for freeze in policy.get("freezes") or []:
        candidates.append(_parse_instant(freeze["end"], "end"))
    window = policy.get("window")
    if window:
        zone = ZoneInfo(window["timezone"])
        local_now = now.astimezone(zone)
        hour, minute = (int(part) for part in window["start"].split(":"))
        for offset in range(0, 15):
            day = (local_now + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            candidates.append(day.astimezone(UTC))
    for candidate in sorted(c for c in candidates if c >= now):
        if _allowed_at(policy, candidate):
            return candidate
    return None


def deploy_window_check(policy: dict[str, Any], now: datetime) -> dict[str, Any]:
    window, freezes = policy.get("window"), policy.get("freezes") or []
    if not window and not freezes:
        return {"key": "deploy_window", "ok": True, "detail": "no deploy window configured"}
    freeze = _active_freeze(freezes, now)
    if _allowed_at(policy, now):
        return {"key": "deploy_window", "ok": True, "detail": "inside the deploy window"}
    upcoming = next_allowed_time(policy, now)
    why = (
        f"change freeze{f' ({freeze['reason']})' if freeze and freeze.get('reason') else ''}"
        if freeze
        else "outside the deploy window "
        f"({window['start']}-{window['end']} {window['timezone']})"
    )
    return {
        "key": "deploy_window",
        "ok": False,
        "detail": (
            f"{why}; next allowed {upcoming.isoformat(timespec='minutes')}"
            if upcoming
            else f"{why}; no allowed time in the next two weeks"
        ),
        "next_allowed_at": upcoming.isoformat() if upcoming else None,
    }


# ── live reads (module-level so tests substitute them) ───────────────────────────


def read_policy_mode(target_workspace_id: str) -> dict[str, Any]:
    """The target gateway's Policy Engine attachment: `{"engine": arn|None, "mode": ...}`.

    One `GetGateway` against the TARGET workspace's credentials. A workspace with no
    gateway reports no engine rather than raising.
    """
    from app.services.agentcore.client import control_client
    from app.services.workspace import context_for_workspace

    ctx = context_for_workspace(target_workspace_id)
    gateway_id = ctx.resources.get("gateway_id")
    if not gateway_id:
        return {"engine": None, "mode": None}
    gateway = control_client(ctx).get_gateway(gatewayIdentifier=gateway_id)
    attachment = gateway.get("policyEngineConfiguration") or {}
    return {"engine": attachment.get("arn"), "mode": attachment.get("mode")}


def eval_gate(db: Session, bundle: ReleaseBundle, policy: dict[str, Any]) -> dict[str, Any]:
    """A passing evaluation run pinned to the bundle, above the configured threshold."""
    from app.evaluation.models import EvalRun

    evidence = bundle.evaluation or {}
    threshold = float(policy["min_eval_score"])
    run_id = evidence.get("run_id")
    base = {
        "key": "eval_score",
        "threshold": threshold,
        "run_id": run_id,
        "dataset_id": evidence.get("dataset_id"),
        "dataset_version": evidence.get("dataset_version"),
    }
    if not run_id:
        return {**base, "ok": False, "detail": "no evaluation run is pinned to this bundle"}
    run = db.get(EvalRun, run_id)
    if run is None or run.status not in _EVAL_RUN_OK:
        return {
            **base,
            "ok": False,
            "detail": f"pinned run {run_id} is "
            f"{'gone' if run is None else run.status}, not completed",
        }
    values = [
        float(s["score"]) for s in (evidence.get("scores") or []) if s.get("score") is not None
    ]
    if not values:
        return {**base, "ok": False, "detail": f"pinned run {run_id} recorded no scores"}
    mean = sum(values) / len(values)
    dataset = evidence.get("dataset_id") or "no dataset"
    version = evidence.get("dataset_version")
    where = f"dataset {dataset}" + (f" v{version}" if version else " (draft)")
    return {
        **base,
        "ok": mean >= threshold,
        "score": round(mean, 4),
        "detail": f"run {run_id} on {where}: mean {mean:.2f} "
        f"{'meets' if mean >= threshold else 'is below'} the {threshold:.2f} threshold",
    }


def policy_enforce_gate(target: Workspace, policy: dict[str, Any]) -> dict[str, Any]:
    if not policy.get("require_policy_enforce"):
        return {
            "key": "policy_enforce",
            "ok": True,
            "detail": f"not required for a {target.tier} workspace",
        }
    try:
        state = read_policy_mode(target.id)
    except Exception as exc:  # unreadable is not the same as enforced
        return {
            "key": "policy_enforce",
            "ok": False,
            "detail": f"could not read the gateway's Policy Engine: {type(exc).__name__}",
        }
    ok = bool(state.get("engine")) and state.get("mode") == "ENFORCE"
    return {
        "key": "policy_enforce",
        "ok": ok,
        "detail": (
            "Policy Engine attached in ENFORCE"
            if ok
            else "no Policy Engine attached to the target gateway"
            if not state.get("engine")
            else f"Policy Engine is in {state.get('mode')}, ENFORCE is required"
        ),
    }


def execution_gates(
    db: Session,
    bundle: ReleaseBundle,
    target: Workspace,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Every gate, freshly evaluated, each marked `blocking`."""
    now = now or datetime.now(UTC)
    policy = effective_policy(target)
    base = promotion_service.evaluate_gates(db, bundle, target)
    checks = list(base["checks"])
    checks.append(eval_gate(db, bundle, policy))
    checks.append(policy_enforce_gate(target, policy))
    window = deploy_window_check(policy, now)
    checks.append(window)
    for check in checks:
        check["blocking"] = check["key"] not in ADVISORY_GATES
        # A zip agent is built from its frozen spec (hashed lock), so "no stored artifact"
        # is not a defect there; it is one for container/byoc, which must not rebuild.
        if check["key"] == "artifact" and bundle.method in SPEC_BUILT_METHODS:
            check["blocking"] = False
    failures = [c["key"] for c in checks if c["blocking"] and not c["ok"]]
    return {
        "checks": checks,
        "blocking_failures": failures,
        "next_allowed_at": window.get("next_allowed_at"),
        "evaluated_at": now.isoformat(),
    }


def assert_executable(gates: dict[str, Any]) -> None:
    """Refuse with a code that names the first failing gate."""
    failures = gates["blocking_failures"]
    if not failures:
        return
    first = next(c for c in gates["checks"] if c["key"] == failures[0])
    raise AppError(
        f"promotion.gate_failed.{first['key']}",
        f"release gate '{first['key']}' failed: {first['detail']}",
        {
            "gate": first["key"],
            "failed_gates": failures,
            "next_allowed_at": first.get("next_allowed_at"),
            "checks": gates["checks"],
        },
        status_code=409,
    )


# ── the plan preview ─────────────────────────────────────────────────────────────


def build_plan(
    db: Session,
    promotion: Promotion,
    bundle: ReleaseBundle,
    target: Workspace,
    source: Workspace | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What executing this promotion would create or change in the target. Read-only.

    No AWS mutation and no ledger write: the one live read is the Policy Engine mode (via
    the gates), which is a `GetGateway`.
    """
    from app.services import resource_mapping

    existing = db.query(Agent).filter(
        Agent.workspace_id == target.id,
        Agent.name == bundle.agent_name,
        Agent.status != "deleted",
    ).first()
    artifact = bundle.artifact or {}
    needs_copy = any(artifact.get(k) for k in ("image_digest", "upload_id", "image_uri"))
    items: list[dict[str, Any]] = []
    if existing is not None:
        items.append(
            {
                "key": "agent",
                "action": "replace",
                "detail": f"publishes a new version of '{existing.name}' ({existing.status}, "
                f"v{existing.version or '?'}) in place; the previous spec stays in its history",
            }
        )
    else:
        items.append(
            {"key": "agent", "action": "create", "detail": f"creates '{bundle.agent_name}'"}
        )
    per_agent_role = bundle.method == "byoc"
    if per_agent_role:
        items.append(
            {
                "key": "iam_role",
                "action": "reuse" if existing is not None else "create",
                "detail": "a per-agent least-privilege execution role",
            }
        )
    else:
        items.append(
            {
                "key": "iam_role",
                "action": "reuse",
                "detail": "the target workspace's shared execution role",
            }
        )
    items.append(
        {
            "key": "artifact_copy",
            "action": "copy" if needs_copy else "none",
            "detail": (
                "copies the tested artifact into the target account (never rebuilt)"
                if needs_copy
                else "no stored artifact: the target builds from the frozen spec"
            ),
        }
    )
    has_record = bool(existing and existing.registry_record_id)
    registry = bool((target.resources or {}).get("registry_id"))
    items.append(
        {
            "key": "registry_record",
            "action": "keep" if has_record else "create" if registry else "none",
            "detail": (
                "keeps the existing registry record"
                if has_record
                else "publishes an A2A registry record"
                if registry
                else "the target has no registry: nothing is published"
            ),
        }
    )
    from app.optimization.service import canary_capability

    canary_ok = (
        existing is not None
        and existing.status == "active"
        and existing.method == bundle.method
        and canary_capability(existing)["eligible"]
    )
    items.append(
        {
            "key": "canary",
            "action": "ramp" if canary_ok else "skip",
            "detail": (
                "traffic ramp 90/10 -> 50/50 -> 1/99 against the running version"
                if canary_ok
                else "no canary: "
                + (
                    "there is no running agent to compare against"
                    if existing is None
                    else "this agent cannot host a runtime canary"
                )
            ),
        }
    )
    resolution = resource_mapping.resolve_spec(
        db,
        bundle.spec or {},
        source_workspace_id=bundle.workspace_id,
        target_workspace_id=target.id,
        source_resources=getattr(source, "resources", None),
        target_resources=target.resources,
    )
    items.append(
        {
            "key": "mapping",
            "action": "rewrite" if resolution.resolved else "none",
            "detail": f"{len(resolution.resolved)} reference(s) rewritten, "
            f"{len(resolution.unmapped)} unmapped",
        }
    )
    gates = execution_gates(db, bundle, target, now=now)
    policy = effective_policy(target)
    return {
        "promotion_id": promotion.id,
        "target_workspace_id": target.id,
        "target_tier": target.tier,
        "mode": "replace" if existing is not None else "create",
        "items": items,
        "stages": [] if promotion.stages is None else list(promotion.stages),
        "observe_seconds": policy["observe_seconds"],
        "gates": gates,
        "can_execute": (
            promotion.status in ("approved", "failed") and not gates["blocking_failures"]
        ),
        "blocked_by": gates["blocking_failures"],
    }
