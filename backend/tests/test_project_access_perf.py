"""Performance of the per-request project resolution (design §5.11, §9.3-6).

Synthetic ledger at the design's size: 1,000 users × 200 projects, every user with
10 memberships (9 direct + 1 group that bundles 10 projects), a few custom roles,
projects bound to 1–2 of 4 workspaces. Rows go in with bulk SQL, not the ORM.

Budget (§5.11): a cached ``resolve_project`` costs the epoch PK reads only — p95
under 2 ms on SQLite. An uncached one (two queries per snapshot plus the bindings
read) only has to stay "reasonable"; the bound here is generous so a busy CI box
does not flake, while still catching an accidental full scan or N+1.

The autouse table wipe runs after every test, so each test seeds its own copy
(~50 ms); the whole file runs in about a second. ``LAUNCHPAD_PERF_SAMPLES`` raises
the number of timed calls for a local, steadier measurement (default 300).
"""

import os
import random
import statistics
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.db import SessionLocal, engine
from app.services import access

USERS = 1000
PROJECTS = 200
WORKSPACES = 4
GROUP_SIZE = 10  # projects per group → 20 groups
DIRECT_PER_USER = 9  # + 1 group membership = 10 memberships each
SAMPLES = int(os.environ.get("LAUNCHPAD_PERF_SAMPLES", "300"))

CACHED_P95_MS = 2.0
UNCACHED_P95_MS = 25.0

_NOW = "2026-01-01 00:00:00.000000"
_ROLES = ("developer", "viewer", "release_ops", "biz_owner", "sme", "custom-a")


def _ws(i: int) -> str:
    return f"perf-ws-{i}"


def _project_id(i: int) -> str:
    return f"perf-p{i:03d}"


def _user_id(i: int) -> str:
    return f"perf-u{i:04d}"


def _seed() -> None:
    rnd = random.Random(1469)
    projects, binds, roles, members, group_members, user_ws = [], [], [], [], [], []
    groups = [{"id": f"perf-g{g:02d}", "name": f"G{g}", "now": _NOW}
              for g in range(PROJECTS // GROUP_SIZE)]
    for i in range(PROJECTS):
        pid = _project_id(i)
        projects.append({"id": pid, "name": pid.upper(), "group_id": f"perf-g{i // GROUP_SIZE:02d}",
                         "status": "archived" if i % 25 == 0 else "active", "now": _NOW})
        first = i % WORKSPACES
        binds.append({"p": pid, "ws": _ws(first), "d": 0, "now": _NOW})
        if i % 3 == 0:
            binds.append({"p": pid, "ws": _ws((first + 1) % WORKSPACES), "d": 0, "now": _NOW})
        # two custom roles per project → 400 rows in project_roles
        for key in ("custom-a", "custom-b"):
            roles.append({"p": pid, "k": key, "n": key,
                          "perms": '["project.use", "agents.deploy"]', "now": _NOW})
    for u in range(USERS):
        uid = _user_id(u)
        for i in rnd.sample(range(PROJECTS), DIRECT_PER_USER):
            members.append({"p": _project_id(i), "u": uid, "r": rnd.choice(_ROLES),
                            "o": '{"agents.delete": false}' if u % 7 == 0 else None,
                            "now": _NOW})
        group_members.append({"g": groups[u % len(groups)]["id"], "u": uid, "r": "developer",
                              "now": _NOW})
        for w in rnd.sample(range(WORKSPACES), 2):
            user_ws.append({"u": uid, "ws": _ws(w), "now": _NOW})
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO project_groups (id, name, description, created_at,"
                          " updated_at) VALUES (:id, :name, '', :now, :now)"), groups)
        conn.execute(text("INSERT INTO projects (id, name, description, group_id, status,"
                          " is_default, created_at, updated_at)"
                          " VALUES (:id, :name, '', :group_id, :status, 0, :now, :now)"), projects)
        conn.execute(text("INSERT INTO project_workspaces (project_id, workspace_id,"
                          " is_workspace_default, enabled_at) VALUES (:p, :ws, :d, :now)"), binds)
        conn.execute(text("INSERT INTO project_roles (project_id, key, name, permissions,"
                          " created_at, updated_at) VALUES (:p, :k, :n, :perms, :now, :now)"),
                     roles)
        conn.execute(text("INSERT INTO project_members (project_id, user_id, role_key,"
                          " overrides, added_at) VALUES (:p, :u, :r, :o, :now)"), members)
        conn.execute(text("INSERT INTO group_members (group_id, user_id, role_key, added_at)"
                          " VALUES (:g, :u, :r, :now)"), group_members)
        conn.execute(text("INSERT INTO user_workspaces (user_id, workspace_id, created_at)"
                          " VALUES (:u, :ws, :now)"), user_ws)
        conn.execute(text("UPDATE access_epoch SET value = value + 1 WHERE id = 1"))
        conn.execute(text("ANALYZE"))


def _identity(u: int) -> SimpleNamespace:
    # permission_overrides given → snapshot_for skips its users-table read, as the
    # request boundary's identity does
    return SimpleNamespace(user_id=_user_id(u), role="member", permission_overrides={})


def _p95_ms(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[-1] * 1000


def _target(db, u: int) -> tuple[str, str]:
    """A (workspace, project) this user reaches, from their own snapshot."""
    snapshot = access.snapshot_for(db, _identity(u))
    for m in sorted(snapshot.memberships.values(), key=lambda m: m.project_id):
        if m.status == "active":
            return sorted(m.workspaces)[0], m.project_id
    raise AssertionError(f"user {u} reaches no active project")


@pytest.fixture
def ledger():
    _seed()
    access.reset_cache_for_tests()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def test_the_synthetic_ledger_has_the_design_shape(ledger):
    snapshot = access.snapshot_for(ledger, _identity(0))
    # 9 direct + a 10-project group, overlapping at most 9 → 10..19 projects
    assert 10 <= len(snapshot.memberships) <= DIRECT_PER_USER + GROUP_SIZE
    counts = ledger.execute(text(
        "SELECT (SELECT COUNT(*) FROM project_members WHERE user_id LIKE 'perf-%'),"
        " (SELECT COUNT(*) FROM projects WHERE id LIKE 'perf-%')"
    )).one()
    assert tuple(counts) == (USERS * DIRECT_PER_USER, PROJECTS)


def test_cached_resolve_project_p95_within_budget(ledger):
    users = [u for u in range(0, USERS, max(1, USERS // 50))]
    targets = {u: _target(ledger, u) for u in users}  # warms every snapshot + binding
    samples: list[float] = []
    for n in range(SAMPLES):
        u = users[n % len(users)]
        ws, pid = targets[u]
        start = time.perf_counter()
        scope = access.resolve_project(ledger, _identity(u), ws, pid, "GET")
        samples.append(time.perf_counter() - start)
        assert scope.id == pid
    p95 = _p95_ms(samples)
    assert p95 < CACHED_P95_MS, f"cached resolve_project p95 {p95:.3f} ms"


def test_uncached_snapshot_p95_is_reasonable(ledger):
    # every call is a different user's first touch → a real snapshot load
    count = min(SAMPLES, USERS)
    samples: list[float] = []
    for u in range(count):
        start = time.perf_counter()
        snapshot = access.snapshot_for(ledger, _identity(u))
        samples.append(time.perf_counter() - start)
        assert snapshot.memberships
    p95 = _p95_ms(samples)
    assert p95 < UNCACHED_P95_MS, f"uncached snapshot_for p95 {p95:.3f} ms"


def test_uncached_resolve_project_p95_is_reasonable(ledger):
    users = list(range(0, USERS, max(1, USERS // 100)))
    targets = {u: _target(ledger, u) for u in users}
    samples: list[float] = []
    for n in range(min(SAMPLES, 200)):
        u = users[n % len(users)]
        ws, pid = targets[u]
        access.reset_cache_for_tests()  # cold: bindings and snapshot both reload
        start = time.perf_counter()
        scope = access.resolve_project(ledger, _identity(u), ws, pid, "GET")
        samples.append(time.perf_counter() - start)
        assert scope.id == pid
    p95 = _p95_ms(samples)
    assert p95 < UNCACHED_P95_MS, f"uncached resolve_project p95 {p95:.3f} ms"


def test_a_bumped_epoch_is_seen_on_the_next_call_at_scale(ledger):
    ws, pid = _target(ledger, 3)
    access.resolve_project(ledger, _identity(3), ws, pid, "GET")
    ledger.execute(text("DELETE FROM project_members WHERE user_id = :u AND project_id = :p"),
                   {"u": _user_id(3), "p": pid})
    access.bump_epoch(ledger)
    ledger.commit()
    still = access.snapshot_for(ledger, _identity(3)).memberships.get(pid)
    # the project may still come through the user's group; never through the dropped row
    assert still is None or still.direct_role is None
