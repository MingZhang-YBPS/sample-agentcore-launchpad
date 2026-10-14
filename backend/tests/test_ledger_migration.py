"""The ledger must survive an in-place upgrade, not just a fresh create.

`conftest.py` points every test at a brand-new SQLite file, so `create_all` builds
each table with the current model columns and a missing `_migrate` entry is invisible.
These tests build a database the way a *deployed* box has one — tables created by an
older release — and assert startup upgrades it instead of 500-ing later.
"""

import sqlalchemy as sa

from app.core import db as db_module
from app.core.db import Base, assert_no_schema_drift, schema_drift


def _engine(tmp_path, name="ledger.db"):
    return sa.create_engine(f"sqlite:///{tmp_path / name}")


def _added_columns() -> set[tuple[str, str]]:
    """(table, column) pairs `_migrate` knows how to add, read off its own DDL.

    `_migrate` delegates a step to a `_migrate_*` helper when the DDL is long, so
    every such function is scanned, not just `_migrate` itself.
    """
    import inspect as py_inspect
    import re

    sources = [
        py_inspect.getsource(member)
        for name, member in vars(db_module).items()
        if name.startswith("_migrate") and py_inspect.isfunction(member)
    ]
    return set(re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", "\n".join(sources)))


def _indexes_on(engine, table, column) -> list[str]:
    return [
        index["name"]
        for index in sa.inspect(engine).get_indexes(table)
        if column in index["column_names"]
    ]


def test_migrate_declares_every_column_missing_from_an_older_table(tmp_path):
    """Drop each column `_migrate` handles, then prove `_migrate` puts it back.

    This is the round trip that a deployed upgrade performs.
    """
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    pairs = _added_columns()
    assert pairs, "no ALTER statements found — did _migrate change shape?"

    # SQLite refuses to drop an indexed column, and a database predating the
    # column has neither it nor its index — so drop both.
    drops = [
        (table, column, _indexes_on(engine, table, column)) for table, column in sorted(pairs)
    ]
    # A composite index (agents.uq_agents_workspace_system_key spans workspace_id
    # and system_key) is reported for each of its columns — drop it once.
    dropped_indexes: set[str] = set()
    with engine.begin() as conn:
        for table, column, indexes in drops:
            for index in indexes:
                if index not in dropped_indexes:
                    conn.execute(sa.text(f"DROP INDEX {index}"))
                    dropped_indexes.add(index)
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))

    stale = schema_drift(engine)
    for table, column in pairs:
        assert column in stale.get(table, []), f"{table}.{column} was not dropped"

    db_module._migrate(engine)
    assert schema_drift(engine) == {}


def test_image_digest_is_added_to_an_existing_deployments_table(tmp_path):
    """Regression: the supply-chain work added this column with no migration entry,
    which made `/api/agents` 500 on every pre-existing ledger."""
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE deployments DROP COLUMN image_digest"))

    db_module._migrate(engine)

    with engine.begin() as conn:
        # The failing request was a SELECT over the mapped columns.
        conn.execute(sa.select(Base.metadata.tables["deployments"]))


def test_schema_drift_names_the_missing_column(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE agents DROP COLUMN registry_record_id"))

    assert schema_drift(engine) == {"agents": ["registry_record_id"]}

    try:
        assert_no_schema_drift(engine)
    except RuntimeError as exc:
        assert "agents" in str(exc) and "registry_record_id" in str(exc)
        assert "_migrate()" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("assert_no_schema_drift did not raise")


def test_a_fresh_database_has_no_drift(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    db_module._migrate(engine)
    assert_no_schema_drift(engine)


def test_migrate_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    db_module._migrate(engine)
    db_module._migrate(engine)
    assert_no_schema_drift(engine)


# ── P1A-cols: actor ids (design §8.2-4, §8.3-7) ─────────────────────────────────

_ACTOR_ID_COLUMNS = {
    "users": ["session_epoch"],
    "audit_events": ["actor_user_id"],
    "share_links": ["created_by_user_id"],
    "promotions": ["requested_by_user_id", "reviewed_by_user_id"],
    "release_records": ["requested_by_user_id", "decided_by_user_id"],
    "waivers": ["requested_by_user_id", "approved_by_user_id"],
    "criteria_sets": ["created_by_user_id", "updated_by_user_id", "signed_by_user_id"],
}


def _old_ledger_with_actors(tmp_path):
    """A ledger from before the actor-id columns, holding history to backfill.

    `alice` existed when she acted; the `bob` account was registered after the rows
    naming "bob" were written (the original was deleted), so it must not inherit them;
    `admin` is the built-in admin, which has no `users` row.
    """
    engine = _engine(tmp_path)
    db_module.init_db(engine)
    ts = "2026-03-01 00:00:00.000000"
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, username, username_key, email, password_hash, role,"
            " status, created_by, login_count, session_epoch, created_at, updated_at)"
            " VALUES (:id, :name, :key, :email, 'h', 'member', 'active', 'test', 0, 0,"
            " :at, :at)"),
            [
                {"id": "u-alice", "name": "Alice", "key": "alice", "email": "a@x.io",
                 "at": "2026-01-01 00:00:00.000000"},
                {"id": "u-bob", "name": "bob", "key": "bob", "email": "b@x.io",
                 "at": "2026-09-01 00:00:00.000000"},
            ],
        )
        conn.execute(sa.text(
            "INSERT INTO waivers (id, workspace_id, project_id, agent_id, criterion_key,"
            " reason, risk_owner, compensating_control, expires_on, status, requested_by,"
            " approved_by, approved_at, note, created_at) VALUES ('w1', 'default',"
            " 'default', 'a1', 'G1', 'r', 'o', '', :at, 'approved', 'alice', 'bob', :at,"
            " '', :at)"), {"at": ts})
        conn.execute(sa.text(
            "INSERT INTO criteria_sets (id, workspace_id, project_id, lineage_id, kind,"
            " name, description, scenario, version, status, source, signed_by, signed_at,"
            " sign_note, removals, created_by, updated_by, created_at, updated_at)"
            " VALUES ('c1', 'default', 'default', 'l1', 'template', 'n', '', '', 1,"
            " 'published', 'manual', 'admin', :at, '', '[]', 'alice', 'ALICE', :at, :at)"),
            {"at": ts})
        for table, columns in _ACTOR_ID_COLUMNS.items():
            for column in columns:
                conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(sa.text("DELETE FROM platform_settings WHERE key = :k"),
                     {"k": db_module.SOD_USER_IDS_MARKER})
    return engine


def test_an_old_ledger_gains_the_actor_id_columns_and_backfills_sod_once(tmp_path):
    engine = _old_ledger_with_actors(tmp_path)
    stale = schema_drift(engine)
    for table, columns in _ACTOR_ID_COLUMNS.items():
        assert set(columns) <= set(stale.get(table, [])), table

    db_module.init_db(engine)
    assert schema_drift(engine) == {}
    with engine.begin() as conn:
        # every existing session keeps its stamp: epoch 0
        assert conn.execute(sa.text(
            "SELECT DISTINCT session_epoch FROM users")).scalars().all() == [0]
        waiver = conn.execute(sa.text(
            "SELECT requested_by_user_id, approved_by_user_id FROM waivers")).one()
        # alice matched case-insensitively; "bob" was someone else at the time
        assert tuple(waiver) == ("u-alice", None)
        cset = conn.execute(sa.text(
            "SELECT created_by_user_id, updated_by_user_id, signed_by_user_id"
            " FROM criteria_sets")).one()
        assert tuple(cset) == ("u-alice", "u-alice", None)  # the built-in admin: no row
        marker = conn.execute(sa.text(
            "SELECT value FROM platform_settings WHERE key = :k"),
            {"k": db_module.SOD_USER_IDS_MARKER}).scalar()
        assert marker == "done"
        # a row a write path leaves without an id after the backfill keeps NULL
        conn.execute(sa.text("UPDATE criteria_sets SET created_by_user_id = NULL"))

    db_module.init_db(engine)  # idempotent: no re-guessing, nothing re-added
    assert schema_drift(engine) == {}
    with engine.begin() as conn:
        assert conn.execute(sa.text(
            "SELECT created_by_user_id, updated_by_user_id FROM criteria_sets")).one() == (
            None, "u-alice")
        assert tuple(conn.execute(sa.text(
            "SELECT requested_by_user_id, approved_by_user_id FROM waivers")).one()) == (
            "u-alice", None)


def test_the_actor_id_columns_are_declared_to_migrate():
    pairs = _added_columns()
    for table, columns in _ACTOR_ID_COLUMNS.items():
        for column in columns:
            assert (table, column) in pairs, f"{table}.{column}"
