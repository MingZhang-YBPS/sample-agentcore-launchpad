"""Console account service — registration, credentials, and admin management.

The built-in admin lives in settings (`auth_username`/`auth_password`); every
*other* console account is a `users` row created here. Passwords are hashed with
a stdlib KDF (no passlib/bcrypt dependency) and accounts are time-boxed:
`expires_at` is authoritative and re-checked on every guarded request, so an
expired or disabled account loses access immediately.
"""

import base64
import hashlib
import hmac
import re
import secrets
import string
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import DEFAULT_WORKSPACE_ID
from app.core.errors import AppError, NotFoundError
from app.models.ledger import User, UserWorkspace, Workspace

# Member-grantable agent-management capabilities. Defined here (not in
# routers.auth, which imports this module) so both auth and the users console
# share one list. `users.permissions` stores only explicit False entries; a
# missing key or a None column means granted.
AGENT_PERMISSIONS = (
    "agents.deploy",
    "agents.import",
    "agents.delete",
    "agents.convert",
    "eval.run",
    # T19: the promotion surface. Unlike the keys above these are NOT granted to a
    # plain member by default — approving and executing a release is the operator's
    # job — so they carry their own default table (`DEFAULT_BY_ROLE`).
    "promotion.request",
    "promotion.approve",
    "identity.manage",
    "identity.grant",
    "memory.manage",
    # Agent-DLC (docs/agent-dlc-design.md §8). The three that decide what "good" means
    # — signing criteria, admitting golden samples, declaring a judge calibrated — are
    # granted to named people, never by role: they are the standard's ownership.
    "criteria.manage",
    "criteria.sign",
    "golden.admit",
    "judge.calibrate",
    "waiver.approve",
    "release.sign",
)

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_MEMBER = "member"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_MEMBER)

# Which permissions each role holds unless an administrator overrode one. Admins are
# absent on purpose: they hold everything by role, never by row.
#
# The split is separation of duties (T19): a member builds and asks for a release, an
# operator approves and runs it. An operator deliberately does NOT hold `agents.deploy`
# / `agents.convert` — they release what a member built rather than editing prompts or
# code themselves — and a member does not hold `promotion.approve`, so nobody can
# approve their own release without an administrator saying so explicitly.
#
# The identity / memory keys follow the build side: a member creates Connections and
# memories for the agents they build. `identity.grant` (consenting one's OWN token for
# an as_user tool) is also an operator's, who invokes agents while running releases.
DEFAULT_BY_ROLE: dict[str, frozenset[str]] = {
    ROLE_OPERATOR: frozenset(
        {"eval.run", "promotion.request", "promotion.approve", "identity.grant",
         "criteria.manage", "waiver.approve", "release.sign"}
    ),
    ROLE_MEMBER: frozenset(
        {"agents.deploy", "agents.import", "agents.delete", "agents.convert", "eval.run",
         "promotion.request", "identity.manage", "identity.grant", "memory.manage",
         "criteria.manage"}
    ),
}


def default_permissions(role: str) -> frozenset[str]:
    """The keys a role holds before any per-user override."""
    if role == ROLE_ADMIN:
        return frozenset(AGENT_PERMISSIONS)
    return DEFAULT_BY_ROLE.get(role, DEFAULT_BY_ROLE[ROLE_MEMBER])

# --- password hashing -------------------------------------------------------

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 390_000
_SALT_BYTES = 16
_DK_BYTES = 32
# A stable hash of a value nobody can submit: used to burn the same PBKDF2 work
# on unknown usernames so response timing does not disclose account existence.
_DUMMY_HASH_PASSWORD = "launchpad-dummy-verify-target"


def hash_password(password: str) -> str:
    """Return `pbkdf2_sha256$<iters>$<salt_b64>$<dk_b64>`."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS, _DK_BYTES)
    return "$".join(
        [
            _ALGO,
            str(_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(dk).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a stored hash string."""
    parts = (stored or "").split("$")
    if len(parts) != 4 or parts[0] != _ALGO or not parts[1].isdigit():
        return False
    try:
        salt = base64.urlsafe_b64decode(parts[2].encode("ascii"))
        expected = base64.urlsafe_b64decode(parts[3].encode("ascii"))
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(parts[1]), len(expected) or _DK_BYTES
    )
    return hmac.compare_digest(dk, expected)


def _burn_verify(password: str) -> None:
    """Spend one PBKDF2 verification on a throwaway hash (timing equalizer)."""
    verify_password(password, hash_password(_DUMMY_HASH_PASSWORD))


def credential_stamp(password_hash: str, epoch: int = 0) -> str:
    """Short, non-reversible tag of the current password hash and session epoch.

    A session cookie carries it, so changing the password (an admin reset) or
    bumping `users.session_epoch` (a forced logout, design §7.4) ends every session
    that was opened before. Epoch 0 stamps the hash alone, exactly as before the
    column existed, so a deploy ends no session.
    """
    material = password_hash if epoch <= 0 else f"{password_hash}:{epoch}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def generate_password(length: int = 14) -> str:
    """Admin password reset: a readable-but-random password shown once."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- validation -------------------------------------------------------------

_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9]([A-Za-z0-9.-]{0,251})\.[A-Za-z]{2,24}$"
)
MIN_PASSWORD_LENGTH = 8


def normalize_username(username: str) -> str:
    return (username or "").strip()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _domain_matches(domain: str, entry: str) -> bool:
    entry = entry.strip().lower().lstrip("@").lstrip(".")
    if not entry:
        return False
    # exact domain or any subdomain of it, so regional variants of a blocked
    # base domain (mail.gmail.com) cannot slip through
    return domain == entry or domain.endswith(f".{entry}")


def validate_username(username: str, settings: Settings) -> str:
    name = normalize_username(username)
    if not _USERNAME_RE.match(name):
        raise AppError(
            "auth.invalid_username",
            "Username must be 3-32 characters of letters, digits, dot, underscore or hyphen",
        )
    if name.lower() == settings.auth_username.strip().lower():
        raise AppError("auth.username_taken", "This username is reserved", status_code=409)
    return name


def validate_email(email: str, settings: Settings) -> str:
    address = normalize_email(email)
    if not _EMAIL_RE.match(address):
        raise AppError("auth.invalid_email", "Enter a valid email address")
    domain = address.rpartition("@")[2]
    allowed = [d for d in settings.auth_allowed_email_domains if d.strip()]
    if allowed:
        if not any(_domain_matches(domain, entry) for entry in allowed):
            raise AppError(
                "auth.email_domain_blocked",
                "Registration is limited to approved company email domains",
            )
        return address
    if any(_domain_matches(domain, entry) for entry in settings.auth_blocked_email_domains):
        raise AppError(
            "auth.email_domain_blocked",
            "Use your company email address — public mailbox providers are not accepted",
        )
    return address


def validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AppError(
            "auth.weak_password",
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    return password


# --- state helpers ----------------------------------------------------------


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; re-attach UTC before comparing."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_expired(user: User, now: datetime | None = None) -> bool:
    expires_at = as_utc(user.expires_at)
    if expires_at is None:
        return False
    return expires_at <= (now or datetime.now(UTC))


STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUSES = (STATUS_PENDING, STATUS_ACTIVE, STATUS_DISABLED)


def account_state(user: User, now: datetime | None = None) -> str:
    """`pending` | `disabled` | `expired` | `active` — derived, never stored."""
    if user.status == STATUS_PENDING:
        return STATUS_PENDING
    if user.status != STATUS_ACTIVE:
        return STATUS_DISABLED
    return "expired" if is_expired(user, now) else STATUS_ACTIVE


def days_remaining(user: User, now: datetime | None = None) -> int | None:
    """Whole days left before expiry (0 once lapsed); None = never expires."""
    expires_at = as_utc(user.expires_at)
    if expires_at is None:
        return None
    seconds = (expires_at - (now or datetime.now(UTC))).total_seconds()
    return max(0, int(seconds // 86400))


def serialize(
    user: User, now: datetime | None = None, workspaces: list[str] | None = None
) -> dict[str, Any]:
    """Console-facing shape. The password hash never leaves the backend.

    `workspaces` is the account's grant list; the caller passes it because it
    needs a ledger session this function does not take. Admins reach every
    workspace by role, so their (usually empty) grant list is not a capability
    statement.
    """
    moment = now or datetime.now(UTC)
    expires_at = as_utc(user.expires_at)
    last_login = as_utc(user.last_login_at)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "state": account_state(user, moment),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "days_remaining": days_remaining(user, moment),
        "created_at": (as_utc(user.created_at) or moment).isoformat(),
        "last_login_at": last_login.isoformat() if last_login else None,
        "login_count": user.login_count,
        "first_login_at": (
            first_login.isoformat() if (first_login := as_utc(user.first_login_at)) else None
        ),
        "created_by": user.created_by,
        "permissions": effective_permissions(user),
        "workspaces": workspaces or [],
    }


def effective_permissions(user: User) -> dict[str, bool]:
    """Full permission map as it will be enforced (admins hold everything).

    A key the role does not hold by default is granted only by an explicit `True`
    override, which is what lets an administrator give one member the approval right
    without making them an operator.
    """
    overrides = user.permissions or {}
    defaults = default_permissions(user.role)
    return {
        key: (
            user.role == ROLE_ADMIN
            or (overrides[key] if key in overrides else key in defaults)
        )
        for key in AGENT_PERMISSIONS
    }


# --- lookups ----------------------------------------------------------------


def find_by_username(db: Session, username: str) -> User | None:
    key = normalize_username(username).lower()
    if not key:
        return None
    return db.scalars(select(User).where(User.username_key == key)).first()


def find_by_email(db: Session, email: str) -> User | None:
    address = normalize_email(email)
    if not address:
        return None
    return db.scalars(select(User).where(User.email == address)).first()


def get_user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("users.not_found", "User not found")
    return user


# --- registration / authentication ------------------------------------------


def register_user(
    db: Session,
    username: str,
    email: str,
    password: str,
    settings: Settings | None = None,
    created_by: str = "self",
    role: str = "member",
    valid_days: int | None = None,
) -> User:
    current = settings or get_settings()
    name = validate_username(username, current)
    address = validate_email(email, current)
    validate_password(password)

    if find_by_username(db, name) is not None:
        raise AppError("auth.username_taken", "This username is already taken", status_code=409)
    if find_by_email(db, address) is not None:
        raise AppError("auth.email_taken", "This email is already registered", status_code=409)

    days = current.auth_registration_valid_days if valid_days is None else valid_days
    # With approval required the clock only starts once an admin approves, so the
    # pending account carries no validity window yet.
    pending = current.auth_registration_require_approval and created_by == "self"
    user = User(
        username=name,
        username_key=name.lower(),
        email=address,
        password_hash=hash_password(password),
        role=role,
        status=STATUS_PENDING if pending else STATUS_ACTIVE,
        expires_at=None if pending or not days else datetime.now(UTC) + timedelta(days=days),
        created_by=created_by,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_user(
    db: Session,
    *,
    username: str,
    email: str,
    password: str | None,
    role: str = ROLE_MEMBER,
    permissions: dict[str, bool] | None = None,
    workspaces: list[str] | None = None,
    valid_days: int | None = None,
    expires_at: datetime | None = None,
    never_expires: bool = False,
    created_by: str,
    settings: Settings | None = None,
) -> tuple[User, str | None]:
    """An administrator creates an account that is active immediately.

    Same username / email / password rules as self-registration (`register_user`),
    but no pending state: the admin who creates it is the approval. Returns the user
    and the generated password when none was provided (shown once by the caller;
    only its hash is stored). Everything is validated before anything is written, and
    the account + its workspace grants commit as one transaction.

    Expiry: `valid_days` from now, an explicit future `expires_at`, `never_expires`,
    or — none of them — the same `auth_registration_valid_days` an approval starts.
    Workspaces: omitted means `default`, the grant an approval preselects; an
    administrator holds none (it reaches every workspace by role).
    """
    current = settings or get_settings()
    name = validate_username(username, current)
    address = validate_email(email, current)
    generated = None if password else generate_password()
    chosen = password or generated or ""
    validate_password(chosen)
    if role not in ROLES:
        raise AppError("users.invalid_role", f"Role must be one of {', '.join(ROLES)}")
    overrides = _normalized_permissions(permissions, role)

    if role == ROLE_ADMIN:
        if workspaces:
            raise AppError(
                "users.admin_no_grants",
                "An administrator reaches every workspace by role and holds no grant",
            )
        requested: list[str] = []
    else:
        requested = [DEFAULT_WORKSPACE_ID] if workspaces is None else workspaces
    _validated_workspace_ids(db, requested)

    chosen_expiry = sum(
        1 for flag in (valid_days is not None, expires_at is not None, never_expires) if flag
    )
    if chosen_expiry > 1:
        raise AppError(
            "users.invalid_expiry",
            "Choose one of valid_days, expires_at or never expiring",
            status_code=422,
        )
    now = datetime.now(UTC)
    if valid_days is not None:
        if not 1 <= int(valid_days) <= 3650:
            raise AppError("users.invalid_expiry", "valid_days must be 1-3650", status_code=422)
        expiry: datetime | None = now + timedelta(days=int(valid_days))
    elif expires_at is not None:
        expiry = as_utc(expires_at)
        if expiry is None or expiry <= now:
            raise AppError(
                "users.invalid_expiry", "expires_at must be in the future", status_code=422
            )
    elif never_expires:
        expiry = None
    else:
        expiry = now + timedelta(days=current.auth_registration_valid_days)

    if find_by_username(db, name) is not None:
        raise AppError("auth.username_taken", "This username is already taken", status_code=409)
    if find_by_email(db, address) is not None:
        raise AppError("auth.email_taken", "This email is already registered", status_code=409)

    user = User(
        id=uuid.uuid4().hex,
        username=name,
        username_key=name.lower(),
        email=address,
        password_hash=hash_password(chosen),
        role=role,
        permissions=overrides,
        status=STATUS_ACTIVE,
        expires_at=expiry,
        created_by=created_by[:64],
    )
    db.add(user)
    for workspace_id in sorted(set(requested)):
        db.add(UserWorkspace(user_id=user.id, workspace_id=workspace_id))
    db.flush()
    from app.services.access import sync_default_membership

    sync_default_membership(db, user.id, set(requested), previous=set(), role=role)
    try:
        db.commit()
    except IntegrityError as exc:
        # a concurrent create/registration won the unique username_key / email
        db.rollback()
        raise AppError(
            "auth.username_taken", "This username or email is already taken", status_code=409
        ) from exc
    db.refresh(user)
    return user, generated


def _validated_workspace_ids(db: Session, value: Any) -> set[str]:
    """The set of workspace ids `value` names; 400 unless every one exists."""
    if value is None:
        return set()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AppError(
            "users.invalid_workspaces", "workspaces must be a list of workspace ids"
        )
    requested = {item.strip() for item in value if item.strip()}
    if requested:
        known = set(
            db.scalars(select(Workspace.id).where(Workspace.id.in_(sorted(requested))))
        )
        unknown = sorted(requested - known)
        if unknown:
            raise AppError(
                "users.invalid_workspaces",
                f"Unknown workspace id(s): {', '.join(unknown)}",
                {"unknown": unknown},
            )
    return requested


def builtin_admin_enabled(settings: Settings | None = None) -> bool:
    """Whether the config-driven admin exists (the login gate is on)."""
    secret = (settings or get_settings()).auth_password
    return secret is not None and bool(secret.get_secret_value())


def admins_who_can_sign_in(
    db: Session, settings: Settings | None = None, excluding: str | None = None
) -> int:
    """Administrators able to sign in right now: the built-in one (while the gate is
    on) plus every active, unexpired admin-role account other than `excluding`."""
    now = datetime.now(UTC)
    rows = db.scalars(
        select(User).where(User.role == ROLE_ADMIN, User.status == STATUS_ACTIVE)
    )
    registered = sum(1 for row in rows if row.id != excluding and not is_expired(row, now))
    return registered + (1 if builtin_admin_enabled(settings) else 0)


def assert_not_last_admin(db: Session, user: User, settings: Settings | None = None) -> None:
    """Refuse a change that would leave no administrator able to sign in.

    The built-in admin counts and can never be demoted or disabled, so while the
    login gate is on this is a backstop rather than a rule an admin meets in
    practice; it exists so a future way of disabling the built-in admin cannot
    leave the console without one.
    """
    if user.role != ROLE_ADMIN:
        return
    if admins_who_can_sign_in(db, settings, excluding=user.id) == 0:
        raise AppError(
            "users.last_admin",
            "This is the last administrator who can sign in — make another account "
            "an administrator first",
            status_code=409,
        )


def authenticate(db: Session, username: str, password: str) -> User:
    """Return the account for valid credentials.

    Raises `auth.invalid_credentials` for unknown user / wrong password, and the
    distinct `auth.account_disabled` / `auth.account_expired` once the
    credentials themselves check out (the caller proved account ownership, so
    telling them *why* they cannot sign in discloses nothing new).
    """
    user = find_by_username(db, username)
    if user is None:
        _burn_verify(password)
        raise AppError(
            "auth.invalid_credentials", "Invalid username or password", status_code=401
        )
    if not verify_password(password, user.password_hash):
        raise AppError(
            "auth.invalid_credentials", "Invalid username or password", status_code=401
        )
    if user.status == STATUS_PENDING:
        raise AppError(
            "auth.account_pending",
            "This account is waiting for administrator approval",
            status_code=401,
        )
    if user.status != STATUS_ACTIVE:
        raise AppError("auth.account_disabled", "This account is disabled", status_code=401)
    if is_expired(user):
        raise AppError("auth.account_expired", "This account has expired", status_code=401)
    return user


def record_login(db: Session, user: User) -> None:
    now = datetime.now(UTC)
    if user.first_login_at is None and not user.login_count:
        # only a genuinely first login starts the TTFA clock; an account that
        # logged in before the column existed keeps NULL (→ created_at fallback)
        user.first_login_at = now
    user.last_login_at = now
    user.login_count = (user.login_count or 0) + 1
    db.commit()


# --- admin management -------------------------------------------------------

STATUS_FILTERS = ("all", "pending", "active", "expired", "disabled")


@dataclass(frozen=True)
class UserPage:
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


def list_users(
    db: Session,
    q: str | None = None,
    status: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> UserPage:
    now = datetime.now(UTC)
    stmt = select(User)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(func.lower(User.username).like(needle), User.email.like(needle))
        )
    rows = list(db.scalars(stmt.order_by(User.created_at.desc())))
    # expiry is time-derived, so status filtering happens in Python against the
    # same helper the auth path uses (one definition of "expired")
    if status in (STATUS_PENDING, STATUS_ACTIVE, "expired", STATUS_DISABLED):
        rows = [row for row in rows if account_state(row, now) == status]
    total = len(rows)
    window = rows[offset : offset + limit] if limit > 0 else rows[offset:]
    grants = workspace_grants(db, [row.id for row in window])
    return UserPage(
        items=[serialize(row, now, grants.get(row.id, [])) for row in window],
        total=total,
        limit=limit,
        offset=offset,
    )


def compute_stats(db: Session, settings: Settings | None = None) -> dict[str, Any]:
    current = settings or get_settings()
    now = datetime.now(UTC)
    rows: Sequence[User] = list(db.scalars(select(User)))
    states = [account_state(row, now) for row in rows]

    def _created(row: User) -> datetime:
        return as_utc(row.created_at) or now

    week_ago = now - timedelta(days=7)
    expiring_soon = 0
    for row, state in zip(rows, states, strict=True):
        expires_at = as_utc(row.expires_at)
        if (
            state == STATUS_ACTIVE
            and expires_at is not None
            and expires_at - now <= timedelta(days=3)
        ):
            expiring_soon += 1

    day_keys = [(now - timedelta(days=offset)).date() for offset in range(13, -1, -1)]
    counts = dict.fromkeys(day_keys, 0)
    for row in rows:
        day = _created(row).date()
        if day in counts:
            counts[day] += 1

    domains: dict[str, int] = {}
    for row in rows:
        domain = row.email.rpartition("@")[2]
        if domain:
            domains[domain] = domains.get(domain, 0) + 1
    top_domains = sorted(domains.items(), key=lambda item: (-item[1], item[0]))[:5]

    return {
        "total": len(rows),
        "pending": states.count(STATUS_PENDING),
        "active": states.count(STATUS_ACTIVE),
        "expired": states.count("expired"),
        "disabled": states.count(STATUS_DISABLED),
        "expiring_soon": expiring_soon,
        "registered_last_7d": sum(1 for row in rows if _created(row) >= week_ago),
        "active_last_7d": sum(
            1
            for row in rows
            if row.last_login_at is not None and (as_utc(row.last_login_at) or now) >= week_ago
        ),
        "registrations": [
            {"date": day.isoformat(), "count": counts[day]} for day in day_keys
        ],
        "top_domains": [{"domain": domain, "count": count} for domain, count in top_domains],
        "valid_days": current.auth_registration_valid_days,
    }


def extend_validity(user: User, days: int) -> None:
    """Add `days` on top of the remaining validity (revive from now if lapsed)."""
    now = datetime.now(UTC)
    base = as_utc(user.expires_at) or now
    user.expires_at = max(base, now) + timedelta(days=days)


def apply_patch(
    db: Session,
    user: User,
    patch: dict[str, Any],
    settings: Settings | None = None,
) -> str | None:
    """Apply an admin patch in place. Returns a generated password, if any.

    `patch` only carries keys the caller actually sent, so `{"expires_at": None}`
    means "never expires" while an omitted key leaves the field alone.
    """
    current = settings or get_settings()
    generated: str | None = None
    was_signable_admin = _signable_admin(user)
    if "status" in patch:
        status = patch["status"]
        if status not in STATUSES:
            raise AppError(
                "users.invalid_status", f"Status must be one of {', '.join(STATUSES)}"
            )
        approving = user.status == STATUS_PENDING and status == STATUS_ACTIVE
        user.status = status
        # approving a pending account starts its validity window, unless the same
        # patch sets the expiry explicitly
        if approving and user.expires_at is None and not {"expires_at", "extend_days"} & set(patch):
            user.expires_at = datetime.now(UTC) + timedelta(
                days=current.auth_registration_valid_days
            )
    if "role" in patch:
        role = patch["role"]
        if role not in ROLES:
            raise AppError(
                "users.invalid_role", f"Role must be one of {', '.join(ROLES)}"
            )
        demoted = user.role == ROLE_ADMIN and role != ROLE_ADMIN
        user.role = role
        if demoted:
            # admins hold no project rows (they bypass): give the account back the
            # default-project memberships its workspace grants imply (design §8.5)
            from app.services.access import sync_default_membership

            grants = set(db.scalars(
                select(UserWorkspace.workspace_id).where(UserWorkspace.user_id == user.id)
            ))
            db.flush()
            sync_default_membership(db, user.id, grants, previous=set(), role=role)
    if "extend_days" in patch and patch["extend_days"] is not None:
        days = int(patch["extend_days"])
        if not 1 <= days <= 3650:
            raise AppError("users.invalid_extension", "Extension must be 1-3650 days")
        extend_validity(user, days)
    if "expires_at" in patch:
        value = patch["expires_at"]
        user.expires_at = as_utc(value) if isinstance(value, datetime) else None
    if "password" in patch:
        # an explicit null asks the platform to generate one, which is the only
        # case the caller gets a password back (shown once in the console)
        chosen = patch["password"]
        password = chosen or generate_password()
        validate_password(password)
        user.password_hash = hash_password(password)
        generated = None if chosen else password
    if "permissions" in patch:
        # after the role patch above, so an override is measured against the new role
        user.permissions = _normalized_permissions(patch["permissions"], user.role)
    if "workspaces" in patch:
        set_workspace_grants(db, user, patch["workspaces"])
    if was_signable_admin and not _signable_admin(user):
        # a demotion, disable or lapsed expiry of an admin: never the last one
        if admins_who_can_sign_in(db, current, excluding=user.id) == 0:
            db.rollback()
            raise AppError(
                "users.last_admin",
                "This is the last administrator who can sign in — make another "
                "account an administrator first",
                status_code=409,
            )
    # A disable, a lapsed expiry, a demotion or a revoked grant (and with it the
    # default project's `inherit` membership) can each offboard the account from
    # keys it minted; same transaction as the change itself.
    _disable_offboarded_api_keys(db, user.id)
    db.commit()
    db.refresh(user)
    return generated


def revoke_sessions(db: Session, user: User) -> int:
    """Force a logout (design §7.4): bump the account's session epoch, which changes
    its credential stamp, so every cookie issued before answers 401 on its next
    request. Returns the new epoch."""
    user.session_epoch = int(user.session_epoch or 0) + 1
    db.commit()
    db.refresh(user)
    return user.session_epoch


def list_admins(db: Session, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every admin-role account (design §7.3), oldest first, each marked with
    `can_sign_in` and `last_signable` — the one whose demotion, disable or expiry
    `users.last_admin` would refuse. The built-in admin is the router's to add."""
    current = settings or get_settings()
    now = datetime.now(UTC)
    rows = list(
        db.scalars(select(User).where(User.role == ROLE_ADMIN).order_by(User.created_at))
    )
    signable = admins_who_can_sign_in(db, current)
    grants = workspace_grants(db, [row.id for row in rows])
    items = []
    for row in rows:
        payload = serialize(row, now, grants.get(row.id, []))
        can_sign_in = _signable_admin(row)
        payload["builtin"] = False
        payload["can_sign_in"] = can_sign_in
        payload["last_signable"] = can_sign_in and signable <= 1
        items.append(payload)
    return items


def _signable_admin(user: User) -> bool:
    return user.role == ROLE_ADMIN and user.status == STATUS_ACTIVE and not is_expired(user)


def _disable_offboarded_api_keys(db: Session, user_id: str) -> None:
    # Every project's keys of this account (design projects §5.7): the predicate
    # reads the pending membership rows, so call it after the write it follows.
    # Imported here: services.api_keys reads this module's account-state helpers.
    from app.services import api_keys as api_keys_service

    api_keys_service.disable_offboarded_keys(db, user_id)


def workspace_grants(db: Session, user_ids: Sequence[str]) -> dict[str, list[str]]:
    """Granted workspace ids per user id, for the ids asked about."""
    if not user_ids:
        return {}
    rows = db.execute(
        select(UserWorkspace.user_id, UserWorkspace.workspace_id).where(
            UserWorkspace.user_id.in_(list(user_ids))
        )
    ).all()
    grants: dict[str, list[str]] = {}
    for user_id, workspace_id in rows:
        grants.setdefault(user_id, []).append(workspace_id)
    return {user_id: sorted(ids) for user_id, ids in grants.items()}


def set_workspace_grants(db: Session, user: User, value: Any) -> None:
    """Replace this account's workspace grants with `value` (None clears them).

    A full replacement rather than a per-workspace toggle: the console edits the
    whole set, and two concurrent toggles against a replacement list cannot
    resurrect a grant the other one just removed. Staged on the session; the
    caller commits.
    """
    requested = _validated_workspace_ids(db, value)
    current = set(
        db.scalars(
            select(UserWorkspace.workspace_id).where(UserWorkspace.user_id == user.id)
        )
    )
    for workspace_id in current - requested:
        db.query(UserWorkspace).filter(
            UserWorkspace.user_id == user.id,
            UserWorkspace.workspace_id == workspace_id,
        ).delete()
    for workspace_id in requested - current:
        db.add(UserWorkspace(user_id=user.id, workspace_id=workspace_id))
    db.flush()
    # the default project follows the grants (design §8.5): granted → `inherit`
    # member; revoked → the `inherit` row goes (an explicit project role stays)
    from app.services.access import sync_default_membership

    sync_default_membership(db, user.id, requested, previous=current, role=user.role)


def _normalized_permissions(value: Any, role: str = ROLE_MEMBER) -> dict[str, bool] | None:
    """Validate a full/partial permission map; keep only what differs from the role.

    Unsent keys fall back to the role's defaults, so a map that matches them
    normalizes to None and permission keys added later follow their role default for
    every existing account.

    Amended by T19: an explicit **grant** is stored too, not just a denial. The defaults
    are per role now (`DEFAULT_BY_ROLE`), so `{"promotion.approve": true}` on a member is
    a real change — dropping it, as the denial-only form did, silently discarded the one
    way to give a single member the approval right without making them an operator.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AppError("users.invalid_permissions", "permissions must be an object")
    unknown = sorted(set(value) - set(AGENT_PERMISSIONS))
    if unknown:
        raise AppError(
            "users.invalid_permissions",
            f"Unknown permission key(s): {', '.join(unknown)}",
        )
    if not all(isinstance(flag, bool) for flag in value.values()):
        raise AppError("users.invalid_permissions", "permission values must be booleans")
    defaults = default_permissions(role)
    overrides = {
        key: flag for key, flag in value.items() if flag is not (key in defaults)
    }
    return overrides or None


def delete_user(db: Session, user: User, settings: Settings | None = None) -> None:
    if _signable_admin(user):
        assert_not_last_admin(db, user, settings)
    # grants first: SQLite does not enforce the FK, so the rows would linger and
    # a recycled user id would inherit them
    db.query(UserWorkspace).filter(UserWorkspace.user_id == user.id).delete()
    # ...and so do project and group memberships (the user's after_delete listener
    # bumps the access epoch), which would otherwise linger in member lists and counts
    for table in ("project_members", "group_members"):
        db.execute(text(f"DELETE FROM {table} WHERE user_id = :id"),  # noqa: S608
                   {"id": user.id})
    db.delete(user)
    # The keys it minted are disabled, never deleted: they stay listed for audit,
    # and their dangling `created_by` keeps them from being re-enabled.
    _disable_offboarded_api_keys(db, user.id)
    db.commit()
