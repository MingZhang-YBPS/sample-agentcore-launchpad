"""The built-in admin's console-set password (an override of `auth_password`).

The built-in admin is config-driven (`auth_username` / `auth_password`, on a
deployed instance `/etc/launchpad-auth.env`) and has no `users` row, so without
this module its password could only be changed by editing that file. A password
set from the console is stored here as a **hash** in the `platform_settings` row
`auth.admin_password_hash`; while the row holds a well-formed hash it takes
precedence over the configured password, which then stops working.

Why it cannot lock the console out:

* a row that is not a well-formed hash is ignored (the configured password works);
* **break-glass** — on the instance, either clear the row
  (`cd backend && uv run python scripts/reset_admin_password.py`) or start the
  backend with `LAUNCHPAD_AUTH_ADMIN_PASSWORD_OVERRIDE_DISABLED=true`; both make
  the configured password the one that signs in again.

The session cookie of the built-in admin carries a stamp of the override hash
(empty while there is none), so changing the password — or clearing it — ends
every session opened with the previous one.
"""

import base64
import hmac
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.platform_settings import PlatformSetting
from app.services import users as users_service

OVERRIDE_KEY = "auth.admin_password_hash"
SOURCE_CONFIG = "config"
SOURCE_CONSOLE = "console"


def is_password_hash(value: str | None) -> bool:
    """Whether `value` is a hash `users.hash_password` could have produced."""
    parts = (value or "").split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256" or not parts[1].isdigit():
        return False
    try:
        salt = base64.urlsafe_b64decode(parts[2].encode("ascii"))
        digest = base64.urlsafe_b64decode(parts[3].encode("ascii"))
    except (ValueError, TypeError):
        return False
    return bool(salt) and bool(digest) and int(parts[1]) > 0


def override_hash(db: Session, settings: Settings | None = None) -> str | None:
    """The active console-set hash, or None when the configured password rules."""
    current = settings or get_settings()
    if current.auth_admin_password_override_disabled:
        return None
    row = db.get(PlatformSetting, OVERRIDE_KEY)
    if row is None or not is_password_hash(row.value):
        return None
    return row.value


def session_stamp(db: Session, settings: Settings | None = None) -> str:
    """What the built-in admin's cookie must carry: '' while no override is active."""
    active = override_hash(db, settings)
    return users_service.credential_stamp(active) if active else ""


def password_source(db: Session, settings: Settings | None = None) -> str:
    return SOURCE_CONSOLE if override_hash(db, settings) else SOURCE_CONFIG


def configured_password(settings: Settings | None = None) -> str | None:
    secret = (settings or get_settings()).auth_password
    if secret is None:
        return None
    return secret.get_secret_value() or None


def verify(db: Session, password: str, settings: Settings | None = None) -> bool:
    """Check a password for the built-in admin against whichever source rules."""
    current = settings or get_settings()
    active = override_hash(db, current)
    if active:
        return users_service.verify_password(password, active)
    configured = configured_password(current)
    if configured is None:
        return False
    return hmac.compare_digest(password.encode("utf-8"), configured.encode("utf-8"))


def set_password(db: Session, password: str, actor: str) -> None:
    """Store a new override hash (staged; the caller commits)."""
    users_service.validate_password(password)
    row = db.get(PlatformSetting, OVERRIDE_KEY)
    if row is None:
        row = PlatformSetting(key=OVERRIDE_KEY)
        db.add(row)
    row.value = users_service.hash_password(password)
    row.updated_by = actor[:64]
    row.updated_at = datetime.now(UTC)


def clear(db: Session) -> bool:
    """Drop the override (break-glass). Returns whether there was one. Commits."""
    row = db.get(PlatformSetting, OVERRIDE_KEY)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def updated_at(db: Session) -> datetime | None:
    row = db.get(PlatformSetting, OVERRIDE_KEY)
    return users_service.as_utc(row.updated_at) if row is not None else None


# The built-in admin's row in the user-management list. Not a `users` id (those
# are 32 hex characters), so it can never collide with a registered account.
BUILTIN_ADMIN_ID = "builtin-admin"


def serialize_builtin(db: Session, settings: Settings | None = None) -> dict:
    """The console shape of the built-in admin, marked `builtin`.

    Role, status and validity are fixed: it cannot be demoted, disabled, expired or
    deleted — the only thing user management can change on it is its password.
    """
    current = settings or get_settings()
    changed = updated_at(db) if override_hash(db, current) else None
    return {
        "id": BUILTIN_ADMIN_ID,
        "username": current.auth_username,
        "email": "",
        "role": users_service.ROLE_ADMIN,
        "status": users_service.STATUS_ACTIVE,
        "state": users_service.STATUS_ACTIVE,
        "expires_at": None,
        "days_remaining": None,
        "created_at": None,
        "last_login_at": None,
        "login_count": 0,
        "first_login_at": None,
        "created_by": "config",
        "permissions": dict.fromkeys(users_service.ACCOUNT_PERMISSIONS, True),
        "workspaces": [],
        "builtin": True,
        "password_source": password_source(db, current),
        "password_changed_at": changed.isoformat() if changed else None,
    }
