"""The offline projects migration tools (scripts/backfill_projects.py and
scripts/rehearse_projects_migration.py, design projects §8.4 / §9.4-4).

Every test works on a throwaway ledger file under `tmp_path`, shaped like a deployed
pre-projects box (`tests/test_ledger_migration.py`): the backfill files rows the same
way startup does, is idempotent, assigns an ambiguous workspace's leftovers only to a
project bound there, and leaves the ledger untouched on `--dry-run`; the rehearsal only
reads its input, migrates a copy, checks it, and fails loudly on a ledger startup would
refuse. Neither reaches AWS.
"""

import hashlib
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
import sqlalchemy as sa

from app.core import db as db_module
from tests.test_ledger_migration import _insert, _project_of, pre_projects_ledger

BACKEND = Path(__file__).resolve().parents[1]
SCRIPTS = BACKEND / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def backfill():
    return _load("backfill_projects")


@pytest.fixture(scope="module")
def rehearse():
    return _load("rehearse_projects_migration")


def _ledger_path(engine) -> Path:
    path = Path(engine.url.database)
    engine.dispose()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ambiguous_ledger(tmp_path):
    """An upgraded ledger where `prod` binds two projects and an older build (after a
    rollback) wrote an agent and a chat message with no project."""
    engine = pre_projects_ledger(tmp_path)
    db_module.init_db(engine)
    with engine.begin() as conn:
        _insert(conn, "projects", id="payments", name="Payments")
        _insert(conn, "project_workspaces", project_id="payments", workspace_id="prod",
                is_workspace_default=False)
        _insert(conn, "projects", id="elsewhere", name="Elsewhere")
        _insert(conn, "agents", id="a-old", workspace_id="prod", name="a-old",
                method="harness", project_id=None)
        # its agent (a2) is in `default`: the parent decides, not --project
        _insert(conn, "chat_messages", id=201, workspace_id="prod", agent_id="a2",
                session_id="rt-2", role="user", project_id=None)
    return engine


# ── backfill_projects.py ──────────────────────────────────────────────────────


def test_backfill_files_a_pre_projects_ledger_and_a_second_run_changes_nothing(
        tmp_path, backfill, capsys):
    path = _ledger_path(pre_projects_ledger(tmp_path))

    assert backfill.main(["--db", str(path)]) == backfill.EXIT_OK
    first = capsys.readouterr().out
    assert "every strict row has a project" in first
    # the agents line: 3 rows, no column before, the preset left NULL, 2 stamped
    agents_line = next(line for line in first.splitlines() if line.startswith("agents "))
    assert agents_line.split() == ["agents", "3", "-", "1", "2"]
    engine = sa.create_engine(f"sqlite:///{path}")
    assert _project_of(engine, "agents", "a2") == "default"
    assert _project_of(engine, "chat_messages", 102) == "default"  # the orphan
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT value FROM platform_settings WHERE key = :k"),
                            {"k": db_module.PROJECTS_MIGRATION_MARKER}).scalar() == "done"
    engine.dispose()
    digest = _digest(path)

    assert backfill.main(["--db", str(path)]) == backfill.EXIT_OK
    second = capsys.readouterr().out
    for line in second.splitlines():
        if line.startswith("step "):
            assert ": 0 rows" in line, line
    assert _digest(path) == digest  # nothing written the second time


def test_backfill_reports_and_then_assigns_an_ambiguous_workspace(
        tmp_path, backfill, capsys):
    path = _ledger_path(_ambiguous_ledger(tmp_path))

    assert backfill.main(["--db", str(path)]) == backfill.EXIT_FAILED
    out = capsys.readouterr().out
    assert "agents workspace=prod rows=1 (projects bound: default, payments)" in out
    engine = sa.create_engine(f"sqlite:///{path}")
    # unambiguous rows were still filed: the message took its agent's project
    assert _project_of(engine, "chat_messages", 201) == "default"
    assert _project_of(engine, "agents", "a-old") is None

    assert backfill.main(["--db", str(path), "--workspace", "prod",
                          "--project", "payments"]) == backfill.EXIT_OK
    assert "--workspace prod --project payments: 1 rows (agents=1)" in capsys.readouterr().out
    assert _project_of(engine, "agents", "a-old") == "payments"
    assert _project_of(engine, "chat_messages", 201) == "default"
    engine.dispose()
    db_module.init_db(sa.create_engine(f"sqlite:///{path}"))  # startup accepts it now


def test_backfill_refuses_a_project_not_bound_to_the_workspace(tmp_path, backfill, capsys):
    path = _ledger_path(_ambiguous_ledger(tmp_path))

    code = backfill.main(["--db", str(path), "--workspace", "prod", "--project", "elsewhere"])

    assert code == backfill.EXIT_FAILED
    err = capsys.readouterr().err
    assert "'elsewhere' is not bound to workspace 'prod'" in err
    assert "default, payments" in err
    engine = sa.create_engine(f"sqlite:///{path}")
    assert _project_of(engine, "agents", "a-old") is None
    engine.dispose()


def test_backfill_dry_run_leaves_the_ledger_untouched(tmp_path, backfill, capsys):
    path = _ledger_path(_ambiguous_ledger(tmp_path))
    digest = _digest(path)

    code = backfill.main(["--db", str(path), "--workspace", "prod", "--project", "payments",
                          "--dry-run"])

    assert code == backfill.EXIT_OK
    assert "dry run on a copy" in capsys.readouterr().out
    assert _digest(path) == digest


def test_backfill_usage_and_a_missing_ledger(tmp_path, backfill, capsys):
    with pytest.raises(SystemExit) as exc:
        backfill.main(["--db", str(tmp_path / "x.db"), "--workspace", "prod"])
    assert exc.value.code == backfill.EXIT_USAGE

    missing = tmp_path / "missing.db"
    assert backfill.main(["--db", str(missing)]) == backfill.EXIT_FAILED
    assert "no ledger at" in capsys.readouterr().err
    assert not missing.exists()  # never creates an empty ledger to "migrate"


def test_the_scripts_refuse_aws_and_restore_boto3_afterwards(backfill):
    with backfill.no_aws(), pytest.raises(RuntimeError, match="no AWS calls"):
        boto3.client("sts", region_name="us-east-1")
    boto3.client("sts", region_name="us-east-1")  # creating a client makes no request


# ── rehearse_projects_migration.py ────────────────────────────────────────────


def test_rehearsal_migrates_a_copy_and_only_reads_the_ledger(tmp_path, rehearse, capsys):
    path = _ledger_path(pre_projects_ledger(tmp_path))
    with sa.create_engine(f"sqlite:///{path}").begin() as conn:
        # the production box's identity, which the rehearsal host's settings do not share
        conn.execute(sa.text("UPDATE workspaces SET account_id = '999988887777',"
                             " region = 'eu-west-1' WHERE id = 'default'"))
    digest = _digest(path)
    out_path = tmp_path / "rehearsed.db"

    assert rehearse.main([str(path), "--out", str(out_path)]) == rehearse.EXIT_OK

    out = capsys.readouterr().out
    assert "projects marker before: absent" in out
    assert "init_db took" in out and out.rstrip().endswith("no drift")
    messages = next(line for line in out.splitlines() if line.startswith("chat_messages "))
    assert messages.split() == ["chat_messages", "strict", "2", "-", "0", "1"]  # 1 orphan
    assert _digest(path) == digest
    copy = sa.create_engine(f"sqlite:///{out_path}")
    assert _project_of(copy, "agents", "a1") == "default"
    with copy.connect() as conn:
        assert tuple(conn.execute(sa.text(
            "SELECT account_id, region FROM workspaces WHERE id = 'default'")).one()) == (
            "999988887777", "eu-west-1")
    copy.dispose()


def test_rehearsal_removes_its_copy_after_success(tmp_path, rehearse, monkeypatch, capsys):
    path = _ledger_path(pre_projects_ledger(tmp_path))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(rehearse.tempfile, "mkdtemp", lambda prefix: str(scratch / "r"))
    (scratch / "r").mkdir()

    assert rehearse.main([str(path)]) == rehearse.EXIT_OK
    assert not (scratch / "r").exists()
    capsys.readouterr()


def test_rehearsal_fails_on_a_ledger_startup_would_refuse(tmp_path, rehearse, capsys):
    path = _ledger_path(_ambiguous_ledger(tmp_path))
    out_path = tmp_path / "rehearsed.db"

    assert rehearse.main([str(path), "--out", str(out_path)]) == rehearse.EXIT_FAILED

    captured = capsys.readouterr()
    assert "FAIL init_db failed: RuntimeError: ledger rows with no project_id" in captured.out
    # startup does not derive from parents: the message is refused too (the backfill
    # script files it under its agent's project)
    assert "agents=1, chat_messages=1" in captured.out
    assert "FAIL strict rows with no project:" in captured.out
    assert f"migrated copy kept at {out_path}" in captured.err
    assert out_path.exists()


def test_rehearsal_usage(tmp_path, rehearse):
    with pytest.raises(SystemExit) as exc:
        rehearse.main([str(tmp_path / "missing.db")])
    assert exc.value.code == rehearse.EXIT_USAGE
    path = _ledger_path(pre_projects_ledger(tmp_path))
    with pytest.raises(SystemExit) as exc:
        rehearse.main([str(path), "--out", str(path)])
    assert exc.value.code == rehearse.EXIT_USAGE


# ── as commands ───────────────────────────────────────────────────────────────


def _run(script: str, *args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("LAUNCHPAD_DATABASE_URL", None)  # the script points `app` at its ledger itself
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def test_both_scripts_run_as_commands(tmp_path):
    path = _ledger_path(pre_projects_ledger(tmp_path))

    rehearsed = _run("rehearse_projects_migration.py", str(path), tmp_path=tmp_path)
    assert rehearsed.returncode == 0, rehearsed.stdout + rehearsed.stderr
    assert "OK every row has a workspace" in rehearsed.stdout

    filed = _run("backfill_projects.py", "--db", str(path), tmp_path=tmp_path)
    assert filed.returncode == 0, filed.stdout + filed.stderr
    assert "every strict row has a project" in filed.stdout


def test_print_report_lists_no_leftovers_on_a_clean_ledger(backfill):
    out = io.StringIO()
    backfill.print_report({"before": {}, "after": {"agents": (2, 0)}, "steps": {},
                           "unprojected": {}, "bindings": {}}, out=out)
    assert out.getvalue().splitlines()[-1] == "every strict row has a project"
