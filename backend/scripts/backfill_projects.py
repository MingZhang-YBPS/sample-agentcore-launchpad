#!/usr/bin/env python
"""File a ledger's rows into projects, offline (design projects §8.3 / §8.4).

    cd backend && uv run python scripts/backfill_projects.py                 # the service's ledger
    uv run python scripts/backfill_projects.py --db copy-of-launchpad.db     # any ledger file
    ... --workspace prod --project payments    # adopt prod's leftover rows into `payments`
    ... --dry-run                              # run on a throwaway copy, report only

Startup refuses a ledger holding strict rows with no `project_id` in a workspace bound
to several projects (`assert_every_row_has_a_project`): an older build wrote them after
a rollback, and which project owns them is an operator's call. This script is that call.
Each run, in order — every step only touches `project_id IS NULL`, so a second run
changes nothing:

1. schema: `create_all` + `_migrate` (additive, the same steps as startup), so a ledger
   from before projects gains the tables and `project_id` columns;
2. `_seed_default_projects`: the `default` project, its workspace bindings, and — only
   while the `migration.projects_v1` marker is absent — the one-time backfill;
3. derived rows take their parent's project (a chat message its agent's), which is
   unambiguous even in a workspace with several projects;
4. a row in a workspace bound to one project takes that project (startup's own rule);
5. with `--workspace W --project P`: every strict row of W still without a project goes
   to P, which must be bound to W.

Then it prints, per table, the rows / rows without a project before and after and how
many it stamped, and the strict rows still unprojected per (table, workspace) with the
projects each workspace binds. Nothing else is written: no audit row, no epoch bump (a
row's project changes no one's access), no AWS call. Stop the backend first — the
service's own writes would race step 5.

Exit code:

    0  every strict row has a project (startup will accept the ledger)
    1  strict rows are still unprojected, or the ledger could not be processed
   64  bad command-line usage
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 64

# Every module that declares tables, so `create_all` and the drift check see the
# metadata the running app does (init_db imports only part of it; routers do the rest).
MODEL_MODULES = (
    "app.models.ledger",
    "app.models.projects",
    "app.models.dlc",
    "app.models.platform_settings",
    "app.models.resource_mapping",
    "app.models.selfservice",
    "app.models.video",
    "app.models.assistant",
    "app.models.announcement",
    "app.evaluation.models",
    "app.optimization.models",
    "app.skill_lab.models",
)


class BackfillError(Exception):
    """The ledger could not be processed; the message says why."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def point_app_at(ledger: Path) -> None:
    """Make the process engine name `ledger` too, before `app` is imported.

    The scripts always pass their own engine, so this is belt and braces: should
    anything reach for `app.core.db.engine`, it hits the same file, never
    `data/launchpad.db`. A no-op once `app` is loaded (tests import it first).
    """
    if "app.core.db" not in sys.modules:
        os.environ["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{ledger.resolve()}"


@contextlib.contextmanager
def no_aws():
    """Fail any boto3 client creation while inside: these scripts never reach AWS."""
    import boto3.session

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("the offline projects migration scripts make no AWS calls")

    saved = boto3.session.Session.client, boto3.session.Session.resource
    boto3.session.Session.client = _refuse  # type: ignore[method-assign]
    boto3.session.Session.resource = _refuse  # type: ignore[method-assign]
    try:
        yield
    finally:
        boto3.session.Session.client, boto3.session.Session.resource = saved


def load_models() -> None:
    import importlib

    for name in MODEL_MODULES:
        importlib.import_module(name)


def ledger_engine(ledger: Path):
    from sqlalchemy import create_engine

    if not ledger.is_file():
        # create_engine would happily create an empty ledger and "migrate" that
        raise BackfillError(f"no ledger at {ledger}")
    return create_engine(f"sqlite:///{ledger.resolve()}")


def ledger_from_settings() -> Path:
    from sqlalchemy.engine import make_url

    from app.core.config import get_settings

    url = make_url(get_settings().database_url)
    shown = url.render_as_string(hide_password=True)
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise BackfillError(f"{shown} does not name a SQLite ledger file — pass --db PATH")
    return Path(url.database)


def copy_ledger(src: Path, dst: Path) -> None:
    """Consistent copy with SQLite's online backup API; the source is opened read-only."""
    if not src.is_file():
        raise BackfillError(f"no ledger at {src}")
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target, pages=-1)
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise BackfillError(f"copying {src} failed: {exc}") from exc
    finally:
        source.close()


# ── counts ────────────────────────────────────────────────────────────────────


def project_tables() -> tuple[str, ...]:
    from app.core.db import PROJECT_SHARED_TABLES, PROJECT_STRICT_TABLES

    return (*PROJECT_STRICT_TABLES, *PROJECT_SHARED_TABLES)


def table_counts(bind) -> dict[str, tuple[int, int | None]]:
    """(rows, rows with NULL project_id) per live project table; None = no column yet."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    live = set(inspector.get_table_names())
    counts: dict[str, tuple[int, int | None]] = {}
    with bind.connect() as conn:
        for table in project_tables():
            if table not in live:
                continue
            total = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608
            columns = {c["name"] for c in inspector.get_columns(table)}
            null = None
            if "project_id" in columns:
                null = conn.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE project_id IS NULL")  # noqa: S608
                ).scalar_one()
            counts[table] = (int(total), None if null is None else int(null))
    return counts


def unprojected_by_workspace(bind) -> dict[tuple[str, str | None], int]:
    """Strict rows (non-preset agents included) with no project, per (table, workspace)."""
    from sqlalchemy import text

    from app.core.db import unprojected_row_counts

    found: dict[tuple[str, str | None], int] = {}
    with bind.connect() as conn:
        for table in unprojected_row_counts(bind):
            extra = " AND system_key IS NULL" if table == "agents" else ""
            rows = conn.execute(
                text(
                    f"SELECT workspace_id, COUNT(*) FROM {table}"  # noqa: S608
                    f" WHERE project_id IS NULL{extra} GROUP BY workspace_id"
                )
            ).all()
            for workspace_id, count in rows:
                found[(table, workspace_id)] = int(count)
    return found


def workspace_projects(bind) -> dict[str, list[str]]:
    from sqlalchemy import inspect, text

    if "project_workspaces" not in inspect(bind).get_table_names():
        return {}
    bound: dict[str, list[str]] = {}
    with bind.connect() as conn:
        for workspace_id, project_id in conn.execute(
            text("SELECT workspace_id, project_id FROM project_workspaces"
                 " ORDER BY workspace_id, is_workspace_default DESC, project_id")
        ):
            bound.setdefault(workspace_id, []).append(project_id)
    return bound


# ── the steps ─────────────────────────────────────────────────────────────────


def bring_schema_up(bind) -> None:
    from app.core import db

    db.Base.metadata.create_all(bind=bind)
    db._migrate(bind)
    db.assert_no_schema_drift(bind)
    unscoped = db.unscoped_row_counts(bind)
    if unscoped:
        listed = ", ".join(f"{t}={n}" for t, n in sorted(unscoped.items()))
        raise BackfillError(
            f"rows with no workspace_id ({listed}): this ledger predates workspaces — "
            "start the backend on it once (or run scripts/rehearse_projects_migration.py)"
        )


def derive_from_parents(bind) -> dict[str, int]:
    """Unprojected derived rows take their parent's project, where it has one."""
    from sqlalchemy import inspect, text

    from app.core.db import _PROJECT_BACKFILL_DERIVED

    live = set(inspect(bind).get_table_names())
    stamped: dict[str, int] = {}
    with bind.begin() as conn:
        for table, column, parent in _PROJECT_BACKFILL_DERIVED:
            if table not in live or parent not in live:
                continue
            parent_project = (
                f"(SELECT p.project_id FROM {parent} p WHERE p.id = {table}.{column})"
            )
            changed = conn.execute(
                text(
                    f"UPDATE {table} SET project_id = {parent_project}"  # noqa: S608
                    f" WHERE project_id IS NULL AND {parent_project} IS NOT NULL"
                )
            ).rowcount
            if changed:
                stamped[table] = stamped.get(table, 0) + int(changed)
    return stamped


def assign_workspace(bind, workspace_id: str, project_id: str) -> dict[str, int]:
    """Every strict row of `workspace_id` still without a project goes to `project_id`."""
    from sqlalchemy import inspect, text

    from app.core.db import PROJECT_STRICT_TABLES

    with bind.connect() as conn:
        if conn.execute(text("SELECT 1 FROM workspaces WHERE id = :w"),
                        {"w": workspace_id}).first() is None:
            raise BackfillError(f"no workspace {workspace_id!r}")
        if conn.execute(text("SELECT 1 FROM projects WHERE id = :p"),
                        {"p": project_id}).first() is None:
            raise BackfillError(f"no project {project_id!r}")
        if conn.execute(
            text("SELECT 1 FROM project_workspaces WHERE workspace_id = :w AND project_id = :p"),
            {"w": workspace_id, "p": project_id},
        ).first() is None:
            # its rows would be unreachable: no request in this workspace resolves to it
            bound = ", ".join(workspace_projects(bind).get(workspace_id, [])) or "none"
            raise BackfillError(
                f"project {project_id!r} is not bound to workspace {workspace_id!r}"
                f" (bound there: {bound})"
            )
    live = set(inspect(bind).get_table_names())
    stamped: dict[str, int] = {}
    with bind.begin() as conn:
        for table in (*PROJECT_STRICT_TABLES, "agents"):
            if table not in live:
                continue
            platform = " AND system_key IS NULL" if table == "agents" else ""
            changed = conn.execute(
                text(
                    f"UPDATE {table} SET project_id = :p"  # noqa: S608
                    f" WHERE project_id IS NULL AND workspace_id = :w{platform}"
                ),
                {"p": project_id, "w": workspace_id},
            ).rowcount
            if changed:
                stamped[table] = int(changed)
    return stamped


def backfill(bind, *, workspace_id: str | None = None,
             project_id: str | None = None) -> dict:
    """Run every step on `bind`; returns the report (see `print_report`)."""
    from app.core.db import _seed_default_projects, claim_sole_project_rows

    load_models()
    before = table_counts(bind)
    bring_schema_up(bind)
    _seed_default_projects(bind)
    steps = {
        "derived from parent": derive_from_parents(bind),
        "workspace's only project": claim_sole_project_rows(bind),
    }
    if workspace_id is not None and project_id is not None:
        steps[f"--workspace {workspace_id} --project {project_id}"] = assign_workspace(
            bind, workspace_id, project_id
        )
    return {
        "before": before,
        "after": table_counts(bind),
        "steps": steps,
        "unprojected": unprojected_by_workspace(bind),
        "bindings": workspace_projects(bind),
    }


def print_report(report: dict, out=None) -> None:
    out = out or sys.stdout
    before, after = report["before"], report["after"]

    def _null(value: int | None) -> str:
        return "-" if value is None else str(value)

    print(f"{'table':<30} {'rows':>9} {'null before':>12} {'null after':>11} "
          f"{'stamped':>8}", file=out)
    for table, (rows, null_after) in after.items():
        null_before = before.get(table, (rows, None))[1]
        # a column the schema step just added was all-NULL a moment ago
        start = rows if null_before is None else null_before
        stamped = start - (null_after or 0)
        print(f"{table:<30} {rows:>9} {_null(null_before):>12} {_null(null_after):>11} "
              f"{stamped:>8}", file=out)
    for step, stamped in report["steps"].items():
        total = sum(stamped.values())
        detail = ", ".join(f"{t}={n}" for t, n in sorted(stamped.items()))
        print(f"step {step}: {total} rows{f' ({detail})' if detail else ''}", file=out)
    unprojected = report["unprojected"]
    if not unprojected:
        print("every strict row has a project", file=out)
        return
    print("strict rows still without a project:", file=out)
    for (table, workspace_id), count in sorted(unprojected.items(),
                                               key=lambda item: (str(item[0][1]), item[0][0])):
        bound = ", ".join(report["bindings"].get(workspace_id or "", [])) or "none"
        print(f"  {table} workspace={workspace_id} rows={count} (projects bound: {bound})",
              file=out)
    print("assign them with --workspace <id> --project <id> (one bound project per run)",
          file=out)


def run(ledger: Path, *, workspace_id: str | None, project_id: str | None,
        dry_run: bool, out=None) -> int:
    out = out or sys.stdout
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="backfill-projects-") as scratch:
            copy = Path(scratch) / ledger.name
            copy_ledger(ledger, copy)
            print(f"dry run on a copy of {ledger}; the ledger is not modified", file=out)
            return run(copy, workspace_id=workspace_id, project_id=project_id,
                       dry_run=False, out=out)
    engine = ledger_engine(ledger)
    try:
        report = backfill(engine, workspace_id=workspace_id, project_id=project_id)
    finally:
        engine.dispose()
    print(f"ledger {ledger}", file=out)
    print_report(report, out=out)
    return EXIT_FAILED if report["unprojected"] else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--db", type=Path, help="ledger file (default: settings.database_url)")
    parser.add_argument("--workspace", help="workspace whose leftover rows to assign")
    parser.add_argument("--project", help="project (bound to --workspace) that takes them")
    parser.add_argument("--dry-run", action="store_true",
                        help="run on a temporary copy and only report")
    args = parser.parse_args(argv)
    if (args.workspace is None) != (args.project is None):
        parser.error("--workspace and --project go together")
    try:
        if args.db is not None:
            point_app_at(args.db)
        ledger = args.db if args.db is not None else ledger_from_settings()
        with no_aws():
            return run(ledger, workspace_id=args.workspace, project_id=args.project,
                       dry_run=args.dry_run)
    except (BackfillError, RuntimeError, sqlite3.Error) as exc:
        print(f"backfill_projects: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — e.g. SQLAlchemy errors: report, exit 1
        print(f"backfill_projects: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
