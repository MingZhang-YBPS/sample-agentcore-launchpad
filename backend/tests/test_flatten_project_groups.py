"""Retiring project groups (department → project → member): the startup migration
`core/db.flatten_project_groups` folds an active group's members into direct project
rows (a direct row wins; an archived group is not migrated), clears `projects.group_id`,
rewrites console-SSO `group` mappings and records into `project` ones, and is
idempotent. The legacy tables are no longer mapped, so this test builds them the way a
pre-retirement ledger has them."""

import json

import sqlalchemy as sa

from app.core import db as db_module
from app.core.db import Base

_NOW = "2026-01-01 00:00:00.000000"


def _insert(conn, table, **values):
    conn.execute(Base.metadata.tables[table].insert(), values)


def _legacy_ledger(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.init_db(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE projects ADD COLUMN group_id VARCHAR(32)"))
        conn.execute(sa.text(
            "CREATE TABLE project_groups (id VARCHAR(32) PRIMARY KEY, name VARCHAR(64),"
            " description TEXT, department_id VARCHAR(32), created_by VARCHAR(32),"
            " created_at DATETIME, updated_at DATETIME, archived_at DATETIME)"))
        conn.execute(sa.text(
            "CREATE TABLE group_members (group_id VARCHAR(32), user_id VARCHAR(32),"
            " role_key VARCHAR(32), added_by VARCHAR(32), added_at DATETIME,"
            " PRIMARY KEY (group_id, user_id))"))
        for uid in ("u-ann", "u-bob", "u-cat"):
            _insert(conn, "users", id=uid, username=uid[2:], username_key=uid[2:],
                    email=f"{uid}@x.io", password_hash="h", role="member")
        for pid, gid in (("alpha", "ops"), ("beta", "ops"), ("gamma", "old"), ("delta", None)):
            _insert(conn, "projects", id=pid, name=pid, status="active")
            conn.execute(sa.text("UPDATE projects SET group_id = :g WHERE id = :p"),
                         {"g": gid, "p": pid})
        conn.execute(sa.text(
            "INSERT INTO project_groups (id, name, description, created_at, updated_at,"
            " archived_at) VALUES ('ops', 'Ops', '', :n, :n, NULL),"
            " ('old', 'Old', '', :n, :n, :n)"), {"n": _NOW})
        for gid, uid, role in (("ops", "u-ann", "developer"), ("ops", "u-bob", "sme"),
                               ("old", "u-cat", "developer")):
            conn.execute(sa.text(
                "INSERT INTO group_members (group_id, user_id, role_key, added_by, added_at)"
                " VALUES (:g, :u, :r, 'u-root', :n)"), {"g": gid, "u": uid, "r": role, "n": _NOW})
        # ann already holds a direct row in alpha: it wins over the group's developer
        _insert(conn, "project_members", project_id="alpha", user_id="u-ann", role_key="viewer")
        _insert(conn, "identity_providers_console", id="kc", name="KC", mappings=[
            {"group": "admins", "kind": "role", "target_id": "", "role_key": "operator"},
            {"group": "eng", "kind": "group", "target_id": "ops", "role_key": "developer"},
            {"group": "legacy", "kind": "group", "target_id": "old", "role_key": "viewer"},
        ])
        # a login put bob into ops (a mapping's record), and ann into ops
        for uid, role in (("u-bob", "sme"), ("u-ann", "developer")):
            _insert(conn, "sso_memberships", idp_id="kc", user_id=uid, kind="group",
                    target_id="ops", role_key=role)
    return engine


def _state(engine):
    with engine.begin() as conn:
        members = {(p, u): r for p, u, r in conn.execute(sa.text(
            "SELECT project_id, user_id, role_key FROM project_members"
            " WHERE user_id LIKE 'u-%'")).fetchall()}
        grouped = conn.execute(sa.text(
            "SELECT COUNT(*) FROM projects WHERE group_id IS NOT NULL")).scalar_one()
        raw = conn.execute(sa.text(
            "SELECT mappings FROM identity_providers_console WHERE id = 'kc'")).scalar_one()
        records = {(u, k, t): r for u, k, t, r in conn.execute(sa.text(
            "SELECT user_id, kind, target_id, role_key FROM sso_memberships")).fetchall()}
        epoch = conn.execute(sa.text("SELECT value FROM access_epoch WHERE id = 1")).scalar_one()
    return members, grouped, json.loads(raw) if isinstance(raw, str) else raw, records, epoch


def test_groups_fold_into_direct_memberships_once(tmp_path):
    engine = _legacy_ledger(tmp_path)
    _, _, _, _, epoch_before = _state(engine)
    result = db_module.flatten_project_groups(engine)
    assert result["projects"] == 3 and result["members"] == 3
    members, grouped, mappings, records, epoch = _state(engine)
    assert members == {
        ("alpha", "u-ann"): "viewer",  # the direct row wins
        ("beta", "u-ann"): "developer",
        ("alpha", "u-bob"): "sme",
        ("beta", "u-bob"): "sme",
        # cat was only in the archived group `old`: nothing migrated
    }
    assert grouped == 0
    assert epoch > epoch_before
    # SSO: the active group's mapping becomes one project mapping per project, the
    # archived group's is dropped; records follow only the rows this migration made
    assert mappings == [
        {"group": "admins", "kind": "role", "target_id": "", "role_key": "operator"},
        {"group": "eng", "kind": "project", "target_id": "alpha", "role_key": "developer"},
        {"group": "eng", "kind": "project", "target_id": "beta", "role_key": "developer"},
    ]
    assert records == {
        ("u-bob", "project", "alpha"): "sme",
        ("u-bob", "project", "beta"): "sme",
        ("u-ann", "project", "beta"): "developer",  # alpha's row is the administrator's
    }
    # the legacy tables stay (rollback safety), unread
    assert {"project_groups", "group_members"} <= set(sa.inspect(engine).get_table_names())

    # idempotent: a second run (and a full startup) changes nothing, not even the epoch
    assert db_module.flatten_project_groups(engine) == {
        "members": 0, "projects": 0, "mappings": 0, "sso_records": 0}
    db_module.init_db(engine)
    assert _state(engine) == (members, 0, mappings, records, epoch)


def test_a_fresh_ledger_has_nothing_to_flatten(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    db_module.init_db(engine)
    tables = set(sa.inspect(engine).get_table_names())
    assert "project_groups" not in tables and "group_members" not in tables
    assert "group_id" not in {c["name"] for c in sa.inspect(engine).get_columns("projects")}
    assert db_module.flatten_project_groups(engine)["members"] == 0
