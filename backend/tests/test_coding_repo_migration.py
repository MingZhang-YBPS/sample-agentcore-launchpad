"""The Agent IDE's "project" → "repo" rename must keep an existing ledger's data.

A deployed box has ``coding_projects`` and ``coding_turns.project_id`` (and, from the
per-person budget release, ``coding_turns.payer_*``). Startup renames them in place
(``core/db._migrate_coding_repo_rename``) before ``create_all`` could add an empty
``coding_repos`` beside them.
"""

import pytest
import sqlalchemy as sa

from app.coding import models as coding_models  # noqa: F401 — registers the tables
from app.core import db as db_module
from app.core.db import Base, assert_no_schema_drift

LEGACY_INDEXES = {
    "coding_projects": {
        "ix_coding_projects_workspace_id": "workspace_id",
        "ix_coding_projects_owner_principal": "owner_principal",
        "ix_coding_projects_created_by": "created_by",
    },
    "coding_turns": {"ix_coding_turns_project_id": "project_id"},
}


def _engine(tmp_path):
    return sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")


def _legacy_ledger(engine) -> None:
    """The current schema rolled back to the pre-rename names, with one repo + turn."""
    Base.metadata.create_all(bind=engine)
    inspector = sa.inspect(engine)
    current = [ix["name"] for table in ("coding_repos", "coding_turns")
               for ix in inspector.get_indexes(table)]
    with engine.begin() as conn:
        for name in current:
            conn.execute(sa.text(f"DROP INDEX {name}"))
        conn.execute(sa.text("ALTER TABLE coding_repos RENAME TO coding_projects"))
        conn.execute(sa.text("ALTER TABLE coding_turns RENAME COLUMN repo_id TO project_id"))
        conn.execute(sa.text("ALTER TABLE coding_turns ADD COLUMN payer_user_id VARCHAR(32)"))
        conn.execute(sa.text("ALTER TABLE coding_turns ADD COLUMN payer_name VARCHAR(64)"))
        for table, indexes in LEGACY_INDEXES.items():
            for name, column in indexes.items():
                conn.execute(sa.text(f"CREATE INDEX {name} ON {table} ({column})"))
        conn.execute(sa.text(
            "INSERT INTO coding_projects (id, workspace_id, owner_principal, name, description,"
            " session_id, status, last_seq, end_seq, created_at, updated_at) VALUES"
            " ('p1', 'default', 'user:u1', 'My agent', '', :sid, 'idle', 7, 7,"
            " '2026-10-01 00:00:00', '2026-10-01 00:00:00')"
        ), {"sid": "s" * 64})
        conn.execute(sa.text(
            "INSERT INTO coding_turns (id, workspace_id, project_id, turn_id, prompt, model,"
            " effort, status, payer_user_id, started_at) VALUES"
            " ('t1', 'default', 'p1', 'turn-1', 'build it', 'm', 'low', 'completed', 'u1',"
            " '2026-10-01 00:00:00')"
        ))


def _index_names(engine, table) -> set[str]:
    return {ix["name"] for ix in sa.inspect(engine).get_indexes(table)}


def test_startup_renames_a_legacy_coding_ledger_and_keeps_its_rows(tmp_path):
    engine = _engine(tmp_path)
    _legacy_ledger(engine)

    db_module.init_db(engine)

    tables = set(sa.inspect(engine).get_table_names())
    assert "coding_projects" not in tables and "coding_repos" in tables
    columns = {c["name"] for c in sa.inspect(engine).get_columns("coding_turns")}
    assert "repo_id" in columns and "project_id" not in columns
    with engine.connect() as conn:
        repo = conn.execute(sa.text("SELECT id, name, last_seq FROM coding_repos")).one()
        turn = conn.execute(sa.text("SELECT id, repo_id, turn_id FROM coding_turns")).one()
    assert tuple(repo) == ("p1", "My agent", 7)
    assert tuple(turn) == ("t1", "p1", "turn-1")
    assert {"ix_coding_repos_workspace_id", "ix_coding_repos_owner_principal",
            "ix_coding_repos_created_by"} <= _index_names(engine, "coding_repos")
    assert "ix_coding_turns_repo_id" in _index_names(engine, "coding_turns")
    assert not any("project" in name for name in _index_names(engine, "coding_repos")
                   | _index_names(engine, "coding_turns"))
    assert_no_schema_drift(engine)

    db_module.init_db(engine)  # idempotent: a renamed ledger is left alone
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM coding_repos")).scalar() == 1


def test_an_empty_coding_repos_built_beside_the_legacy_table_is_replaced(tmp_path):
    engine = _engine(tmp_path)
    _legacy_ledger(engine)
    Base.metadata.create_all(bind=engine)  # what a startup without the rename would do
    assert {"coding_projects", "coding_repos"} <= set(sa.inspect(engine).get_table_names())

    db_module._migrate_coding_repo_rename(engine)

    assert "coding_projects" not in sa.inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT id FROM coding_repos")).scalars().all() == ["p1"]


def test_two_populated_tables_are_refused_not_merged(tmp_path):
    engine = _engine(tmp_path)
    _legacy_ledger(engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO coding_repos (id, workspace_id, owner_principal, name, description,"
            " session_id, status, last_seq, end_seq, created_at, updated_at) VALUES"
            " ('r2', 'default', 'user:u1', 'Other', '', :sid, 'idle', 0, 0,"
            " '2026-10-01 00:00:00', '2026-10-01 00:00:00')"
        ), {"sid": "r" * 64})

    with pytest.raises(RuntimeError, match="coding_projects and coding_repos"):
        db_module._migrate_coding_repo_rename(engine)


def test_a_fresh_ledger_is_untouched(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    before = _index_names(engine, "coding_repos") | _index_names(engine, "coding_turns")

    db_module._migrate_coding_repo_rename(engine)

    assert _index_names(engine, "coding_repos") | _index_names(engine, "coding_turns") == before
    assert "coding_projects" not in sa.inspect(engine).get_table_names()
