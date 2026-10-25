"""Share links (T13): the account-free way to reach exactly one thing.

A link is a bearer token. Only its sha256 is stored (the raw value is shown once,
like an API key), and the *row* resolves everything a request needs — the
workspace, the target, whether it is still usable — so the public routes never
read an `X-Workspace` header or a console session.

Every unusable state (unknown, disabled, revoked, expired, target gone, workspace
gone, target moved to another project, project archived) collapses into the same 404
`share.not_found`: an outsider must not be able to tell a revoked link from one that
never existed.

A link carries its project (design §5.8): `project_id` is stamped from the target row
(the agent or annotation task, `ShareLink.__project_parent__`). The public routes run
as that project (`bind_link_project`), so a visitor's session, messages and feedback
are filed where the link's agent lives; the token lookup itself is unscoped, since
nothing but the token names the project.
"""

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.actor import user_id_for
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context, set_current_project
from app.models.ledger import Agent, ShareLink
from app.models.projects import Project
from app.services.runtime_discovery import invoke_capability
from app.services.workspace import get_workspace_row

KIND_CHAT = "chat"
# Channel links (T30) reuse the row: `kind` is the platform, the token is the secret
# in the webhook URL, and `channel_config` / `channel_secrets` carry the adapter's
# settings. They never open the web chat page (`resolve` only accepts KIND_CHAT).
CHANNEL_KINDS = ("slack", "feishu")
TOKEN_PREFIX = "shr_"
# base64url of 32 random bytes is 43 chars; anything far outside that is not ours
_MAX_TOKEN_LEN = 96
MAX_EXPIRY_DAYS = 365

# What a visitor's memory actor looks like — distinct from any console username
# (`admin`, `river`, …) so share traffic can never read or write a console user's
# Memory partition. See `share_actor`.
ACTOR_PREFIX = "share_"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mint_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def share_actor(link_id: str, session_id: str) -> str:
    """The Memory/ledger actor of one anonymous conversation.

    Per *session*, not per link: visitors of one link are strangers to each other,
    so a link-wide actor would let one outsider's long-term memory surface in the
    next one's answers. Fits `ChatSession.actor_id` (64) and the actorId charset.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:10]
    return f"{ACTOR_PREFIX}{link_id}_{digest}"


def owns_session_actor(link_id: str, actor_id: str | None) -> bool:
    """Whether a stored `ChatSession.actor_id` was minted for this link."""
    return bool(actor_id) and str(actor_id).startswith(f"{ACTOR_PREFIX}{link_id}_")


def _aware(value: datetime | None) -> datetime | None:
    # SQLite hands timezone-aware columns back naive; the values are stored UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def not_found() -> NotFoundError:
    return NotFoundError("share.not_found", "share link not found")


def link_state(link: ShareLink, now: datetime | None = None) -> str:
    """active | revoked | disabled | expired — for the console list."""
    now = now or datetime.now(UTC)
    if link.revoked_at is not None:
        return "revoked"
    if not link.enabled:
        return "disabled"
    expires = _aware(link.expires_at)
    if expires is not None and expires <= now:
        return "expired"
    return "active"


def create_link(
    db: Session,
    *,
    workspace_id: str,
    agent: Agent | None,
    label: str,
    created_by: str,
    expires_in_days: int | None,
    kind: str = KIND_CHAT,
    channel_config: dict | None = None,
    channel_secrets: dict | None = None,
    target_id: str | None = None,
    created_by_user_id: str | None = None,
) -> tuple[ShareLink, str]:
    """`target_id` overrides the agent id: what a link points at is kind-dependent
    (an agent for `chat` / `review`, an annotation task for `annotate`).

    `created_by_user_id` is the creator's `users.id` (design §5.8); omitted, it is
    the request caller's when `created_by` is the caller (`app.core.actor`)."""
    target = target_id or (agent.id if agent is not None else "")
    if not target:
        raise AppError("share.no_target", "a share link must point at something")
    raw = mint_token()
    expires_at = (
        datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
    )
    link = ShareLink(
        workspace_id=workspace_id,
        kind=kind,
        target_id=target,
        token_hash=hash_token(raw),
        prefix=raw[: len(TOKEN_PREFIX) + 4] + "…",
        label=label.strip(),
        created_by=created_by,
        created_by_user_id=user_id_for(created_by, created_by_user_id),
        expires_at=expires_at,
        channel_config=channel_config,
        channel_secrets=channel_secrets,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link, raw


def shareable_agent(agent: Agent | None) -> Agent:
    """The agent a chat link may point at; 409 otherwise (console side, so the
    creator learns why). System presets are platform-owned — never exposed."""
    if agent is None or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    if getattr(agent, "system_key", None):
        raise AppError(
            "share.agent_not_shareable",
            "system-managed presets cannot be shared",
            status_code=409,
        )
    return agent


def create_channel_link(
    db: Session,
    *,
    workspace_id: str,
    agent: Agent,
    platform: str,
    label: str,
    created_by: str,
    expires_in_days: int | None,
    config: dict,
    secrets_: dict,
    created_by_user_id: str | None = None,
) -> tuple[ShareLink, str]:
    """A webhook link for one IM platform. Same token/hash/expiry mechanics as a
    chat link; the secrets ride on the row and are never returned by any route."""
    link, raw = create_link(
        db,
        workspace_id=workspace_id,
        agent=agent,
        label=label,
        created_by=created_by,
        expires_in_days=expires_in_days,
        kind=platform,
        channel_config=config,
        channel_secrets=secrets_,
        created_by_user_id=created_by_user_id,
    )
    return link, raw


def resolve_link(
    db: Session, raw_token: str, kinds: tuple[str, ...] = (KIND_CHAT,)
) -> ShareLink:
    """Token -> the live link, or 404 for every unusable state.

    Kinds whose target is not an agent (`annotate`) resolve the target themselves.
    """
    if not _well_formed(raw_token):
        raise not_found()
    # unscoped: the token alone names the link, and through it the project
    with project_context(None):
        link = db.query(ShareLink).filter(ShareLink.token_hash == hash_token(raw_token)).first()
        if (
            link is None
            or link_state(link) != "active"
            or link.kind not in kinds
            or not _project_active(db, link.project_id)
        ):
            raise not_found()
    return link


def _well_formed(raw_token: str) -> bool:
    return bool(raw_token) and len(raw_token) <= _MAX_TOKEN_LEN and raw_token.startswith(
        TOKEN_PREFIX
    )


def _project_active(db: Session, project_id: str | None) -> bool:
    """A link answers only while its project does: an archived (or missing) project
    closes every link into it, before P1F's archive has revoked them."""
    if not project_id:
        return False  # an owned row without a project is unreachable by design
    project = db.get(Project, project_id)
    return project is not None and project.status == "active"


def link_project(raw_token: str) -> str | None:
    """The project a token's row belongs to, or None when nothing matches. Only picks
    the request's project; whether the link is usable is still `resolve_link`'s call."""
    if not _well_formed(raw_token):
        return None
    db = SessionLocal()
    try:
        with project_context(None):
            row = (
                db.query(ShareLink.project_id)
                .filter(ShareLink.token_hash == hash_token(raw_token))
                .first()
            )
        return row[0] if row else None
    finally:
        db.close()


async def bind_link_project(request: Request) -> None:
    """Router-level dependency of the public `/share/*` routers: run the request as the
    link's project. Async on purpose, like `route_policy.bind_request_project` (which
    has already set None here — `/share` resolves no console project): it runs in the
    request's own task, so the sync handler, a streaming response's generator and
    BackgroundTasks all inherit it."""
    pid = await run_in_threadpool(link_project, str(request.path_params.get("token") or ""))
    set_current_project(pid)


def resolve(
    db: Session, raw_token: str, kinds: tuple[str, ...] = (KIND_CHAT,)
) -> tuple[ShareLink, Agent]:
    """Token -> (live link, its agent), or 404 for every unusable state."""
    link = resolve_link(db, raw_token, kinds)
    return link, live_agent(db, link)


def live_agent(db: Session, link: ShareLink) -> Agent:
    """The link's agent, or the same opaque 404 when it can no longer be reached."""
    workspace = get_workspace_row(db, link.workspace_id or "")
    if workspace is None:
        raise not_found()
    agent = db.get(Agent, link.target_id)
    if (
        agent is None
        or agent.status == "deleted"
        or agent.workspace_id != link.workspace_id
        # the agent moved (or the link was minted elsewhere): the link stays with its project
        or agent.project_id != link.project_id
        or getattr(agent, "system_key", None)
        or not invoke_capability(agent)["eligible"]
    ):
        raise not_found()
    return agent


def record_use(link_id: str) -> None:
    """Bookkeeping for one conversation turn (own session: the stream outlives the
    request's)."""
    db = SessionLocal()
    try:
        with project_context(None):  # the id alone names the link
            link = db.get(ShareLink, link_id)
            if link is not None:
                link.use_count = (link.use_count or 0) + 1
                link.last_used_at = datetime.now(UTC)
                db.commit()
    finally:
        db.close()


def revoke_project_links(db: Session, project_id: str) -> int:
    """Revoke every live link into one project — share, review, annotate and channel
    alike (design §5.8: archiving a project revokes its links). Flushes but does not
    commit, so the caller's archive stays one transaction. Returns how many it revoked;
    already-revoked links keep their first revocation time."""
    now = datetime.now(UTC)
    with project_context(None):
        rows = (
            db.query(ShareLink)
            .filter(ShareLink.project_id == project_id, ShareLink.revoked_at.is_(None))
            .all()
        )
        for row in rows:
            row.revoked_at = now
            row.enabled = False
        db.flush()
    for row in rows:
        limiter.forget(row.id)
    return len(rows)


class TokenBucket:
    """Per-key token bucket: `capacity` burst, refilled at `refill_per_sec`.

    In-process and per worker: with N uvicorn workers the effective ceiling is N
    times the configured rate. Launchpad runs a single backend process (see the
    process-topology section), so that is the documented deployment; a
    multi-worker deployment needs a shared store instead.
    """

    def __init__(
        self,
        capacity: float,
        refill_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float:
        """Spend one token. Returns 0.0 when allowed, else seconds until one is."""
        with self._lock:
            now = self._clock()
            tokens, stamp = self._state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - stamp) * self.refill_per_sec)
            if tokens >= 1:
                self._state[key] = (tokens - 1, now)
                return 0.0
            self._state[key] = (tokens, now)
            return (1 - tokens) / self.refill_per_sec

    def forget(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


# 12-message burst, then one every 5 s (12 per minute) per link.
limiter = TokenBucket(capacity=12, refill_per_sec=0.2)


def enforce_rate_limit(link_id: str) -> None:
    wait = limiter.take(link_id)
    if wait:
        retry_after = max(1, int(wait) + 1)
        raise AppError(
            "share.rate_limited",
            "too many messages — please slow down",
            {"retry_after_seconds": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
