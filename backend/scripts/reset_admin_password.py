#!/usr/bin/env python
"""Break-glass: forget the built-in admin's console-set password.

    cd backend && uv run python scripts/reset_admin_password.py          # clear it
    cd backend && uv run python scripts/reset_admin_password.py --status # just report

A password the built-in admin set on the console (Change password, or another admin's
reset in Users) is stored as a hash override in the ledger (`platform_settings` row
`auth.admin_password_hash`, app/services/admin_credential.py) and takes precedence over
the configured `auth_password` (on a deployed instance: /etc/launchpad-auth.env, the
stack's `ConsolePassword` output). Clearing it makes the configured password the one
that signs in again, and ends every session opened with the cleared one. No restart
is needed. Prints no password and changes nothing else.

The other break-glass is the setting `LAUNCHPAD_AUTH_ADMIN_PASSWORD_OVERRIDE_DISABLED=true`
(then restart the backend), which ignores the override without deleting it.
"""

import argparse
import sys

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.services import admin_credential
from app.services.audit import record_audit_event


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--status", action="store_true", help="report only, change nothing")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        source = admin_credential.password_source(db)
        if args.status:
            print(f"built-in admin password source: {source}")
            return 0
        cleared = admin_credential.clear(db)
    finally:
        db.close()
    if cleared:
        record_audit_event(
            workspace_id=DEFAULT_WORKSPACE_ID,
            actor="break-glass",
            action="admin.password_reset_to_config",
            target="scripts/reset_admin_password.py",
        )
        print("cleared: the configured auth_password signs the built-in admin in again")
    else:
        print("nothing to clear: the built-in admin already uses the configured auth_password")
    return 0


if __name__ == "__main__":
    sys.exit(main())
