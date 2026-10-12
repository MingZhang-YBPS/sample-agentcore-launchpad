"""`/v1` API key policy: scope, expiry, rate limit and usage (T16), and the key
lifecycle — who minted a key, and whether they still may.

Design notes (T16):

* **Rate limit is in-process.** A sliding 60 s window per key id held in memory:
  the backend is a single process (same assumption as the deploy-worker registry),
  the check must not add a ledger write per *rejected* call, and a restart merely
  forgives one window. It is a guard against runaway integrations, not billing.
* **Usage is in the ledger.** `api_keys.last_used_at`/`use_count` plus one
  `api_key_usage` row per (key, UTC day) so the console can chart it and a restart
  loses nothing. Only *admitted* calls are counted.

Lifecycle:

A key carries its creator (`ApiKey.created_by`, a `users.id`). Once that account
is offboarded the key stops working — `creator_offboarded` is the ONE definition
of "offboarded", used in three places:

* `/v1` auth (`routers.public_api.require_api_key`) refuses the key with the same
  401 as any unusable key and flips it to disabled in that request;
* `POST /api/apikeys/{id}/enable` refuses with 409 `apikey.creator_offboarded`;
* `GET /api/apikeys` marks such keys for the console.

Offboarding has several paths and one of them never writes: account expiry is
checked lazily per request, nothing fires when `expires_at` passes. So the check
at use is what guarantees the rule; the explicit write paths (admin disable /
delete, grant removal) additionally call `disable_offboarded_keys` so the console
shows those keys disabled at once. Keys are only ever disabled here, never
deleted, and a key whose creator has no `users` row (the config admin, the
auth-disabled operator, or a legacy NULL creator) is never offboarded.
"""

import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import ApiKey, ApiKeyUsage, User, UserWorkspace
from app.services import users as users_service

WINDOW_SECONDS = 60.0
MAX_RATE_PER_MINUTE = 100_000
# A hot key writes `last_used_at` at most this often.
LAST_USED_RESOLUTION = timedelta(minutes=1)

_hits: dict[str, deque[float]] = {}
_lock = threading.Lock()


def reset_rate_limits() -> None:
    with _lock:
        _hits.clear()


def _aware(value: datetime | None) -> datetime | None:
    # SQLite hands timezone-aware columns back naive; every stored value is UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def is_expired(key: ApiKey, now: datetime | None = None) -> bool:
    expires = _aware(key.expires_at)
    return expires is not None and (now or datetime.now(UTC)) >= expires


def allows_agent(key: ApiKey, agent_id: str) -> bool:
    """Empty/NULL scope means every agent in the key's workspace (legacy keys)."""
    return not key.agent_ids or agent_id in key.agent_ids


def check_rate(key: ApiKey, *, now: float | None = None) -> None:
    """Admit or refuse one call; refusal is 429 with `Retry-After` (whole seconds)."""
    limit = key.rate_per_minute
    if not limit:
        return
    moment = time.monotonic() if now is None else now
    with _lock:
        window = _hits.setdefault(key.id, deque())
        while window and moment - window[0] >= WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            retry = max(1, int(WINDOW_SECONDS - (moment - window[0])) + 1)
            raise AppError(
                "auth.rate_limited",
                f"rate limit of {limit} requests per minute exceeded",
                {"limit_per_minute": limit, "retry_after_seconds": retry},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        window.append(moment)


def record_use(db: Session, key: ApiKey) -> None:
    """Count one admitted call: `use_count` and today's counter, atomically in SQL (no
    lost updates). `last_used_at` follows `touch_last_used`'s resolution, so a hot key
    does not rewrite it on every call — the counters are what stay exact."""
    now = datetime.now(UTC)
    day = now.strftime("%Y-%m-%d")
    values: dict[str, object] = {"use_count": ApiKey.use_count + 1}
    last = _aware(key.last_used_at)
    if last is None or now - last >= LAST_USED_RESOLUTION:
        values["last_used_at"] = now
    db.execute(update(ApiKey).where(ApiKey.id == key.id).values(**values))
    bumped = db.execute(
        update(ApiKeyUsage)
        .where(ApiKeyUsage.key_id == key.id, ApiKeyUsage.day == day)
        .values(count=ApiKeyUsage.count + 1)
    ).rowcount
    if not bumped:
        db.add(ApiKeyUsage(workspace_id=key.workspace_id, key_id=key.id, day=day, count=1))
    db.commit()
    db.refresh(key)


def usage_by_day(db: Session, key_id: str, days: int = 14) -> list[dict[str, object]]:
    """Dense per-day counts for the last `days` UTC days, oldest first."""
    today = datetime.now(UTC).date()
    wanted = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    rows = {
        r.day: r.count
        for r in db.query(ApiKeyUsage)
        .filter(ApiKeyUsage.key_id == key_id, ApiKeyUsage.day.in_(wanted))
        .all()
    }
    return [{"day": d, "count": rows.get(d, 0)} for d in wanted]



# `creator_offboarded` reasons, surfaced to the console (never on /v1).
REASON_DELETED = "deleted"
REASON_DISABLED = "disabled"
REASON_EXPIRED = "expired"
REASON_NO_GRANT = "no_workspace_grant"


def creator_offboarded(
    db: Session, key: ApiKey, now: datetime | None = None
) -> str | None:
    """Why this key's creator may no longer hold it, or None.

    Mirrors what the creator's own console session would be refused for: a
    missing, non-active or expired account, or — for a member — no grant to the
    key's workspace. Admins reach every workspace by role, so for them only the
    account state counts.
    """
    if key.created_by is None:
        return None
    user = db.get(User, key.created_by)
    if user is None:
        return REASON_DELETED
    if user.status != users_service.STATUS_ACTIVE:
        return REASON_DISABLED
    if users_service.is_expired(user, now):
        return REASON_EXPIRED
    if user.role != "admin" and not _granted(db, user.id, key.workspace_id):
        return REASON_NO_GRANT
    return None


def _granted(db: Session, user_id: str, workspace_id: str | None) -> bool:
    if workspace_id is None:
        return False
    return (
        db.execute(
            select(UserWorkspace.workspace_id).where(
                UserWorkspace.user_id == user_id,
                UserWorkspace.workspace_id == workspace_id,
            )
        ).first()
        is not None
    )


def disable_offboarded_keys(db: Session, user_id: str) -> int:
    """Disable every enabled key `user_id` minted that `creator_offboarded` now
    refuses. Staged on the session (flushed first, so pending grant/status
    changes are what the predicate sees); the caller commits."""
    db.flush()
    keys = db.scalars(
        select(ApiKey).where(ApiKey.created_by == user_id, ApiKey.enabled.is_(True))
    ).all()
    disabled = 0
    for key in keys:
        if creator_offboarded(db, key) is not None:
            key.enabled = False
            disabled += 1
    return disabled


def touch_last_used(key: ApiKey, now: datetime | None = None) -> bool:
    """Stamp `last_used_at` unless it was stamped within `LAST_USED_RESOLUTION`.
    Returns whether the row changed (the caller commits)."""
    moment = now or datetime.now(UTC)
    last = users_service.as_utc(key.last_used_at)
    if last is not None and moment - last < LAST_USED_RESOLUTION:
        return False
    key.last_used_at = moment
    return True
