#!/usr/bin/env python
"""Rehearse the projects upgrade on a copy of a ledger (design projects §9.4-4).

    cd backend && uv run python scripts/rehearse_projects_migration.py <copy-of-launchpad.db>
    ... --keep                  # keep the migrated copy (always kept on failure)
    ... --out /tmp/rehearsal.db # write the copy here and keep it (default: a temp dir)

The ledger given is only read (SQLite's online backup API, opened read-only), so a
production ledger — or a copy fetched from one — is safe to pass. On the copy this runs
`init_db`, exactly the startup sequence, and prints:

* per table: rows, rows without a project before and after, orphans (derived rows whose
  parent row is missing, which take their workspace's default project);
* the time `init_db` took;
* the checks: no schema drift, every row has a workspace, every strict row has a
  project, the `migration.projects_v1` marker is set — any failure exits 1.

The copy's `default` workspace keeps its own account / Region / resources: startup
mirrors `launchpad.yaml` onto that row, and the rehearsal host's settings are not the
production box's (a clash with another workspace's identity would fail the run for a
reason the real upgrade never meets). No AWS call is made — boto3 clients are refused.

Exit code:

    0  the upgrade succeeded and every check passed
    1  init_db failed or a check failed (the copy is kept for inspection)
   64  bad command-line usage
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backfill_projects as bp  # noqa: E402 — the sibling script, after the path line

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = bp.EXIT_USAGE


def orphan_counts(bind) -> dict[str, int]:
    """Derived rows whose parent row does not exist, per table (before the upgrade)."""
    from sqlalchemy import inspect, text

    from app.core.db import _PROJECT_BACKFILL_DERIVED

    inspector = inspect(bind)
    live = set(inspector.get_table_names())
    found: dict[str, int] = {}
    with bind.connect() as conn:
        by_table: dict[str, list[tuple[str, str]]] = {}
        for table, column, parent in _PROJECT_BACKFILL_DERIVED:
            if table in live and parent in live:
                by_table.setdefault(table, []).append((column, parent))
        for table, parents in by_table.items():
            # share_links has two parent kinds: an orphan matches neither
            missing = " AND ".join(
                f"{column} IS NOT NULL AND NOT EXISTS"
                f" (SELECT 1 FROM {parent} p WHERE p.id = {table}.{column})"
                for column, parent in parents
            )
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {missing}")  # noqa: S608
            ).scalar_one()
            if count:
                found[table] = int(count)
    return found


@contextlib.contextmanager
def keep_default_identity(bind):
    """Run with settings that mirror the copy's own `default` workspace row."""
    from sqlalchemy import inspect, text

    from app.core import db

    row = None
    if "workspaces" in inspect(bind).get_table_names():
        with bind.connect() as conn:
            row = conn.execute(
                text("SELECT account_id, region, resources FROM workspaces WHERE id = :id"),
                {"id": db.DEFAULT_WORKSPACE_ID},
            ).first()
    if row is None:  # a ledger from before workspaces: the upgrade inserts the row
        yield
        return
    resources = row.resources
    if isinstance(resources, str):
        resources = json.loads(resources or "{}")
    settings = db.get_settings().model_copy(
        update={"account_id": row.account_id or "", "region": row.region,
                "resources": resources or {}}
    )
    original = db.get_settings
    db.get_settings = lambda: settings
    try:
        yield
    finally:
        db.get_settings = original


def check(bind) -> list[str]:
    """Every post-upgrade invariant that failed, as a message (empty = all good)."""
    from sqlalchemy import inspect, text

    from app.core import db

    failures = []
    drift = db.schema_drift(bind)
    if drift:
        failures.append(f"schema drift: {drift}")
    unscoped = db.unscoped_row_counts(bind)
    if unscoped:
        failures.append(f"rows with no workspace: {unscoped}")
    unprojected = db.unprojected_row_counts(bind)
    if unprojected:
        failures.append(f"strict rows with no project: {unprojected}")
    if "platform_settings" not in inspect(bind).get_table_names():
        failures.append("platform_settings is missing")
    else:
        with bind.connect() as conn:
            marker = conn.execute(
                text("SELECT value FROM platform_settings WHERE key = :k"),
                {"k": db.PROJECTS_MIGRATION_MARKER},
            ).scalar()
        if marker != "done":
            failures.append(f"{db.PROJECTS_MIGRATION_MARKER} marker is not set")
    return failures


def rehearse(source: Path, copy: Path, out=None) -> int:
    out = out or sys.stdout
    from app.core import db

    started = time.perf_counter()
    bp.copy_ledger(source, copy)
    copied = time.perf_counter() - started
    engine = bp.ledger_engine(copy)
    try:
        bp.load_models()
        before = bp.table_counts(engine)
        orphans = orphan_counts(engine)
        marker_before = _marker(engine)
        print(f"source {source}\ncopy   {copy} ({copy.stat().st_size:,} bytes, "
              f"copied in {copied:.2f}s)", file=out)
        print(f"projects marker before: {marker_before or 'absent'}", file=out)
        error = None
        started = time.perf_counter()
        try:
            with keep_default_identity(engine):
                db.init_db(engine)
        except Exception as exc:  # noqa: BLE001 — a failed upgrade is the finding
            error = f"{type(exc).__name__}: {exc}"
        took = time.perf_counter() - started
        after = bp.table_counts(engine)
        _print_table(before, after, orphans, out)
        print(f"init_db took {took:.2f}s", file=out)
        failures = [f"init_db failed: {error}"] if error else []
        failures += check(engine)
    finally:
        engine.dispose()
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=out)
        return EXIT_FAILED
    print("OK every row has a workspace, every strict row a project; no drift", file=out)
    return EXIT_OK


def _marker(bind) -> str | None:
    from sqlalchemy import inspect, text

    from app.core.db import PROJECTS_MIGRATION_MARKER

    if "platform_settings" not in inspect(bind).get_table_names():
        return None
    with bind.connect() as conn:
        return conn.execute(text("SELECT value FROM platform_settings WHERE key = :k"),
                            {"k": PROJECTS_MIGRATION_MARKER}).scalar()


def _print_table(before, after, orphans, out) -> None:
    from app.core.db import PROJECT_STRICT_TABLES

    def _null(value: int | None) -> str:
        return "-" if value is None else str(value)

    print(f"{'table':<30} {'kind':<7} {'rows':>9} {'null before':>12} {'null after':>11} "
          f"{'orphans':>8}", file=out)
    for table, (rows, null_after) in after.items():
        null_before = before.get(table, (rows, None))[1]
        kind = "strict" if table in PROJECT_STRICT_TABLES else "shared"
        print(f"{table:<30} {kind:<7} {rows:>9} {_null(null_before):>12} "
              f"{_null(null_after):>11} {orphans.get(table, 0):>8}", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = bp._Parser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("ledger", type=Path, help="ledger file (only read)")
    parser.add_argument("--out", type=Path, help="write the migrated copy here (kept)")
    parser.add_argument("--keep", action="store_true", help="keep the copy after success")
    args = parser.parse_args(argv)
    if not args.ledger.is_file():
        parser.error(f"no ledger at {args.ledger}")
    if args.out is not None and args.out.exists():
        parser.error(f"{args.out} already exists")
    if args.out is not None and args.out.resolve() == args.ledger.resolve():
        parser.error("--out must not be the ledger itself")
    scratch = None
    if args.out is None:
        scratch = Path(tempfile.mkdtemp(prefix="rehearse-projects-"))
        copy = scratch / args.ledger.name
    else:
        copy = args.out
    bp.point_app_at(copy)
    code = EXIT_FAILED
    try:
        with bp.no_aws():
            code = rehearse(args.ledger, copy)
    except (bp.BackfillError, sqlite3.Error) as exc:
        print(f"rehearse_projects_migration: {exc}", file=sys.stderr)
    finally:
        # a copy written to --out is the caller's; only a scratch copy is cleaned up
        if code == EXIT_OK and scratch is not None and not args.keep:
            copy.unlink(missing_ok=True)
            scratch.rmdir()
        elif copy.exists():
            print(f"migrated copy kept at {copy}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
