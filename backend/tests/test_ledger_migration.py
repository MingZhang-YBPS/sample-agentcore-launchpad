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


# ── P1A-mig: the projects upgrade (design §8.2-§8.4, §9.4-4) ─────────────────────

# (the retired `project_groups` / `group_members` are no longer mapped, so no ledger
# gets them created any more)
PROJECT_TABLES = (
    "departments", "projects", "project_workspaces", "project_roles", "project_members",
    "access_epoch",
)
M1, M_ORPHAN, M_OLD = 101, 102, 103  # chat_messages ids are integers


def _insert(conn, table, **values):
    """Core insert: the model's Python-side defaults fill the columns not named."""
    conn.execute(Base.metadata.tables[table].insert(), values)


def _project_columned(engine) -> list[str]:
    inspector = sa.inspect(engine)
    live = set(inspector.get_table_names())
    return [
        table for table in (*db_module.PROJECT_STRICT_TABLES, *db_module.PROJECT_SHARED_TABLES)
        if table in live and "project_id" in {c["name"] for c in inspector.get_columns(table)}
    ]


def strip_projects(engine) -> None:
    """Give an upgraded ledger the shape a pre-projects build has: no project tables,
    no `project_id` columns (nor their indexes), no migration marker."""
    columned = _project_columned(engine)
    dropped: set[str] = set()
    with engine.begin() as conn:
        for table in PROJECT_TABLES:
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
        for table in columned:
            for index in _indexes_on(engine, table, "project_id"):
                if index not in dropped:
                    conn.execute(sa.text(f"DROP INDEX {index}"))
                    dropped.add(index)
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN project_id"))
        conn.execute(sa.text("DELETE FROM platform_settings WHERE key = :k"),
                     {"k": db_module.PROJECTS_MIGRATION_MARKER})


def seed_history(engine) -> None:
    """Two workspaces of history, written before projects existed.

    `default`: agent a1 with a session + message, a system preset, a key and a job;
    `prod`: agent a2, and a chat message whose agent is gone (an orphan). `alice`
    (member) is granted both workspaces; `root` is an admin account.
    """
    with engine.begin() as conn:
        _insert(conn, "workspaces", id="prod", name="Prod", account_id="444455556666",
                region="us-east-1")
        for uid, role in (("u-alice", "member"), ("u-root", "admin")):
            _insert(conn, "users", id=uid, username=uid[2:], username_key=uid[2:],
                    email=f"{uid}@x.io", password_hash="h", role=role)
        for uid, ws in (("u-alice", "default"), ("u-alice", "prod"), ("u-root", "default")):
            _insert(conn, "user_workspaces", user_id=uid, workspace_id=ws)
        _insert(conn, "agents", id="a1", workspace_id="default", name="a1", method="harness")
        _insert(conn, "agents", id="a2", workspace_id="prod", name="a2", method="harness")
        _insert(conn, "agents", id="preset", workspace_id="default", name="preset",
                method="harness", system_key="frontdesk")
        _insert(conn, "chat_sessions", id="s1", workspace_id="default", agent_id="a1",
                session_id="rt-s1")
        _insert(conn, "chat_messages", id=M1, workspace_id="default", agent_id="a1",
                session_id="rt-s1", role="user")
        _insert(conn, "chat_messages", id=M_ORPHAN, workspace_id="prod", agent_id="gone",
                session_id="rt-x", role="user")
        _insert(conn, "api_keys", id="k1", workspace_id="default", name="k", prefix="lp_k1",
                key_hash="h1")
        _insert(conn, "jobs", id="j1", workspace_id="default", type="deploy")


def pre_projects_ledger(tmp_path, name="ledger.db"):
    """A ledger as a deployed pre-projects box has it, holding history to file."""
    engine = _engine(tmp_path, name)
    db_module.init_db(engine)
    seed_history(engine)
    strip_projects(engine)
    return engine


def _project_of(engine, table, row_id):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT project_id FROM {table} WHERE id = :id"),
                            {"id": row_id}).scalar_one()


def _project_snapshot(engine) -> dict:
    """Every row's project, every binding, membership and the epoch."""
    snapshot: dict = {}
    with engine.connect() as conn:
        for table in _project_columned(engine):
            snapshot[table] = sorted(
                (str(r[0]), r[1]) for r in conn.execute(
                    sa.text(f"SELECT id, project_id FROM {table}")).all()
            ) if "id" in {c["name"] for c in sa.inspect(engine).get_columns(table)} else None
        for table in ("project_workspaces", "project_members", "projects", "access_epoch"):
            snapshot[table] = sorted(
                tuple(str(v) for v in row) for row in conn.execute(
                    sa.text(f"SELECT * FROM {table}")).all()
            )
    return snapshot


def test_a_pre_projects_ledger_upgrades(tmp_path):
    engine = pre_projects_ledger(tmp_path)
    assert not set(PROJECT_TABLES) & set(sa.inspect(engine).get_table_names())
    assert _project_columned(engine) == []

    db_module.init_db(engine)

    assert schema_drift(engine) == {}
    live = set(sa.inspect(engine).get_table_names())
    assert set(PROJECT_TABLES) <= live
    every = {*db_module.PROJECT_STRICT_TABLES, *db_module.PROJECT_SHARED_TABLES} & live
    assert set(_project_columned(engine)) == every
    assert "ix_agents_ws_project" in _indexes_on(engine, "agents", "project_id")
    assert "ix_chat_messages_ws_project" in _indexes_on(engine, "chat_messages", "project_id")
    assert "ix_criteria_project_id" in _indexes_on(engine, "criteria", "project_id")
    with engine.connect() as conn:
        # D-9: one `default` project, every workspace's default
        assert conn.execute(sa.text("SELECT id, is_default FROM projects")).all() == [
            ("default", 1)]
        assert sorted(conn.execute(sa.text(
            "SELECT workspace_id, project_id, is_workspace_default FROM project_workspaces"
        )).all()) == [("default", "default", 1), ("prod", "default", 1)]
        # an `inherit` member once (granted two workspaces of one project); admins none
        assert conn.execute(sa.text(
            "SELECT project_id, user_id, role_key FROM project_members")).all() == [
            ("default", "u-alice", "inherit")]
        assert conn.execute(sa.text("SELECT value FROM access_epoch")).scalar() == 1
        assert conn.execute(sa.text("SELECT value FROM platform_settings WHERE key = :k"),
                            {"k": db_module.PROJECTS_MIGRATION_MARKER}).scalar() == "done"
    for table, row_id in (("agents", "a1"), ("agents", "a2"), ("chat_sessions", "s1"),
                          ("chat_messages", M1), ("chat_messages", M_ORPHAN),
                          ("api_keys", "k1"), ("jobs", "j1")):
        assert _project_of(engine, table, row_id) == "default", (table, row_id)
    assert _project_of(engine, "agents", "preset") is None  # platform rows stay NULL
    assert db_module.unprojected_row_counts(engine) == {}


def test_a_second_upgrade_changes_nothing(tmp_path):
    engine = pre_projects_ledger(tmp_path)
    db_module.init_db(engine)
    with engine.begin() as conn:
        # an administrator removes alice from the default project after the upgrade
        conn.execute(sa.text("DELETE FROM project_members WHERE user_id = 'u-alice'"))
    snapshot = _project_snapshot(engine)

    db_module.init_db(engine)
    db_module.init_db(engine)

    # the membership stays removed, no row re-filed, the epoch not bumped again
    assert _project_snapshot(engine) == snapshot
    assert schema_drift(engine) == {}


def test_rows_an_older_build_wrote_after_a_rollback_are_adopted_when_unambiguous(
        tmp_path, caplog):
    """Rolled back: the old binary ignores `project_id`, so its new rows carry NULL."""
    engine = pre_projects_ledger(tmp_path)
    db_module.init_db(engine)
    with engine.begin() as conn:
        _insert(conn, "agents", id="a-old", workspace_id="prod", name="a-old",
                method="harness", project_id=None)
        _insert(conn, "chat_messages", id=M_OLD, workspace_id="default", agent_id="a1",
                session_id="rt-s1", role="user", project_id=None)

    with caplog.at_level("WARNING", logger="app.core.db"):
        db_module.init_db(engine)

    assert _project_of(engine, "agents", "a-old") == "default"
    assert _project_of(engine, "chat_messages", M_OLD) == "default"
    assert "adopted 1 agents rows" in caplog.text


def test_rows_an_older_build_wrote_in_a_multi_project_workspace_stop_startup(
        tmp_path, caplog):
    engine = pre_projects_ledger(tmp_path)
    db_module.init_db(engine)
    with engine.begin() as conn:
        _insert(conn, "projects", id="payments", name="Payments")
        _insert(conn, "project_workspaces", project_id="payments", workspace_id="prod",
                is_workspace_default=False)
        _insert(conn, "agents", id="a-old", workspace_id="prod", name="a-old",
                method="harness", project_id=None)

    assert db_module.claim_sole_project_rows(engine) == {}  # nothing unambiguous
    try:
        with caplog.at_level("WARNING", logger="app.core.db"):
            db_module.init_db(engine)
    except RuntimeError as exc:
        assert "agents=1" in str(exc) and "scripts/backfill_projects.py" in str(exc)
        assert "adopted" not in caplog.text  # a row left NULL is not reported adopted
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("an ambiguous unprojected row did not stop startup")
    assert _project_of(engine, "agents", "a-old") is None  # nothing guessed


def test_a_ledger_rolled_back_to_the_pre_projects_shape_upgrades_again(tmp_path):
    """Drop the project tables and columns as an older ledger lacks them, add history
    written meanwhile, and upgrade again: the same result as the first upgrade."""
    engine = pre_projects_ledger(tmp_path)
    db_module.init_db(engine)
    first = _project_snapshot(engine)
    strip_projects(engine)
    with engine.begin() as conn:
        _insert(conn, "agents", id="a-new", workspace_id="prod", name="a-new",
                method="harness")

    db_module.init_db(engine)

    assert schema_drift(engine) == {}
    assert _project_of(engine, "agents", "a-new") == "default"
    again = _project_snapshot(engine)
    again["agents"] = [row for row in again["agents"] if row[0] != "a-new"]
    # timestamps differ (rows re-seeded); compare the shapes that carry meaning
    for table in _project_columned(engine):
        assert again[table] == first[table], table
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT project_id, user_id, role_key FROM project_members")).all() == [
            ("default", "u-alice", "inherit")]
        assert conn.execute(sa.text("SELECT value FROM access_epoch")).scalar() == 1
