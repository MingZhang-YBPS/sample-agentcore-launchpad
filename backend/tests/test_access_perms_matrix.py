"""perms(U, P) in every combination (projects design §3.4, §3.5, §9.3-1).

Two layers, both exhaustive over the inputs that decide a member's keys:

* the pure functions — ``inherit_keys`` / ``template_keys`` / ``apply_overrides`` —
  parametrized over every built-in template × every account role × a set of override
  shapes (none, empty, all-deny, all-grant, approver grant, single deny, project keys,
  unknown keys);
* the snapshot (``access.load_snapshot``) on a real ledger: direct role × group role
  (every template, ``inherit``, a custom role, or absent), then the direct row's
  overrides, custom roles per project, archived groups and projects, the ``inherit``
  row's workspace rule, and the epoch-checked cache.

Expected values are derived here from the role defaults and the template table rather
than read back from the code under test, so a change to the union / override order
fails loudly.
"""

import itertools
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.db import SessionLocal
from app.models.ledger import User
from app.services import access
from app.services.access import (
    ALL_PROJECT_KEYS,
    INHERIT_ROLE,
    PROJECT_PERMISSIONS,
    ROLE_TEMPLATES,
    ProjectScope,
    apply_overrides,
    inherit_keys,
    template_keys,
)
from app.services.users import AGENT_PERMISSIONS, DEFAULT_BY_ROLE, ROLES

TEMPLATES = sorted(ROLE_TEMPLATES)
ACCOUNT_ROLES = sorted(ROLES)
# an organization admin (steward, §11.4) holds no project rows: structure only
MEMBER_ROLES = sorted(r for r in ROLES if r != "steward")
CUSTOM_KEY = "custom-reviewer"
CUSTOM_KEYS = frozenset({"project.use", "eval.run", "golden.admit"})

# account-level overrides (users.permissions): what inherit reads
ACCOUNT_OVERRIDES: dict[str, dict[str, bool] | None] = {
    "none": None,
    "empty": {},
    "deny_all": dict.fromkeys(AGENT_PERMISSIONS, False),
    "grant_all": dict.fromkeys(AGENT_PERMISSIONS, True),
    "grant_approver": {"promotion.approve": True},
    "deny_approver": {"promotion.approve": False},
    "deny_deploy": {"agents.deploy": False},
    "grant_signer": {"criteria.sign": True, "release.sign": True},
    # project keys and junk are not account keys: inherit must ignore them
    "project_keys": {"project.members": True, "project.audit": True, "project.use": False},
    "unknown": {"no.such.key": True},
}

# project-level overrides (project_members.overrides): applied after the union
PROJECT_OVERRIDES: dict[str, dict[str, bool] | None] = {
    "none": None,
    "empty": {},
    "grant_members": {"project.members": True},
    "deny_use": {"project.use": False},
    "deny_write": {"project.write": False},
    "grant_approve": {"promotion.approve": True, "mcp.approve": True},
    "deny_all": dict.fromkeys(ALL_PROJECT_KEYS, False),
    "grant_all": dict.fromkeys(ALL_PROJECT_KEYS, True),
    "mixed": {"project.audit": True, "eval.run": False, "criteria.sign": True},
    "unknown": {"no.such.key": True, "users.manage": True},
}


def _account_defaults(role: str) -> frozenset[str]:
    if role == "admin":
        return frozenset(AGENT_PERMISSIONS)
    return DEFAULT_BY_ROLE.get(role, DEFAULT_BY_ROLE["member"])


def _expected_inherit(role: str, overrides: dict[str, bool] | None) -> frozenset[str]:
    ovr = overrides or {}
    granted = {k for k in AGENT_PERMISSIONS if ovr.get(k, k in _account_defaults(role))}
    if "promotion.approve" in granted:
        granted.add("mcp.approve")
    return frozenset(granted | {"project.use", "project.write"})


def _expected_overrides(base: frozenset[str], overrides: dict[str, bool] | None) -> frozenset[str]:
    ovr = {k: v for k, v in (overrides or {}).items() if k in ALL_PROJECT_KEYS}
    return frozenset((set(base) | {k for k, v in ovr.items() if v})
                     - {k for k, v in ovr.items() if not v})


def _role_keys(role_key: str | None, account_role: str,
               account_overrides: dict[str, bool] | None) -> frozenset[str]:
    """The expected keys one role source contributes."""
    if role_key is None:
        return frozenset()
    if role_key == INHERIT_ROLE:
        return _expected_inherit(account_role, account_overrides)
    if role_key == CUSTOM_KEY:
        return CUSTOM_KEYS
    return ROLE_TEMPLATES[role_key].keys


# ── the templates themselves ─────────────────────────────────────────────────


class TestTemplates:
    @pytest.mark.parametrize("template", TEMPLATES)
    def test_every_key_is_a_project_key(self, template):
        assert ROLE_TEMPLATES[template].keys <= ALL_PROJECT_KEYS

    def test_viewer_holds_nothing(self):
        assert ROLE_TEMPLATES["viewer"].keys == frozenset()

    @pytest.mark.parametrize("template", sorted(set(TEMPLATES) - {"viewer"}))
    def test_every_other_template_participates(self, template):
        assert "project.use" in ROLE_TEMPLATES[template].keys

    def test_the_owner_holds_no_approval_or_signing_key(self):
        """Separation of duties: the person managing members does not approve releases."""
        owner = ROLE_TEMPLATES["project_owner"].keys
        assert not owner & {"promotion.approve", "release.sign", "waiver.approve",
                            "criteria.sign", "golden.admit", "judge.calibrate", "mcp.approve"}

    def test_only_the_owner_manages_members(self):
        holders = {t for t in TEMPLATES if "project.members" in ROLE_TEMPLATES[t].keys}
        assert holders == {"project_owner"}

    def test_suggested_templates_exist(self):
        assert set(access.SUGGESTED_TEMPLATE.values()) <= set(ROLE_TEMPLATES)

    def test_project_permissions_are_never_account_keys(self):
        assert not set(PROJECT_PERMISSIONS) & set(AGENT_PERMISSIONS)


# ── inherit_keys ─────────────────────────────────────────────────────────────


class TestInheritKeys:
    @pytest.mark.parametrize("shape", sorted(ACCOUNT_OVERRIDES))
    @pytest.mark.parametrize("role", ACCOUNT_ROLES)
    def test_matches_the_account_computation(self, role, shape):
        overrides = ACCOUNT_OVERRIDES[shape]
        assert inherit_keys(role, overrides) == _expected_inherit(role, overrides)

    @pytest.mark.parametrize("shape", sorted(ACCOUNT_OVERRIDES))
    @pytest.mark.parametrize("role", ACCOUNT_ROLES)
    def test_always_participates_and_writes_but_never_manages(self, role, shape):
        keys = inherit_keys(role, ACCOUNT_OVERRIDES[shape])
        assert {"project.use", "project.write"} <= keys
        assert not keys & {"project.members", "project.audit", "project.share"}

    @pytest.mark.parametrize("role", ACCOUNT_ROLES)
    def test_mcp_approve_follows_the_approver_key(self, role):
        assert "mcp.approve" in inherit_keys(role, {"promotion.approve": True})
        assert "mcp.approve" not in inherit_keys(role, {"promotion.approve": False})

    def test_member_and_operator_defaults(self):
        assert inherit_keys("member", None) == (
            DEFAULT_BY_ROLE["member"] | {"project.use", "project.write"}
        )
        assert inherit_keys("operator", None) == (
            DEFAULT_BY_ROLE["operator"] | {"project.use", "project.write", "mcp.approve"}
        )

    def test_an_unknown_role_reads_as_member(self):
        assert inherit_keys("someone", None) == inherit_keys("member", None)


# ── template_keys ────────────────────────────────────────────────────────────


class TestTemplateKeys:
    @pytest.mark.parametrize("shape", sorted(ACCOUNT_OVERRIDES))
    @pytest.mark.parametrize("role", ACCOUNT_ROLES)
    @pytest.mark.parametrize("template", TEMPLATES)
    def test_a_builtin_ignores_the_account(self, template, role, shape):
        got = template_keys(template, {CUSTOM_KEY: CUSTOM_KEYS}, account_role=role,
                            account_overrides=ACCOUNT_OVERRIDES[shape])
        assert got == ROLE_TEMPLATES[template].keys

    @pytest.mark.parametrize("shape", sorted(ACCOUNT_OVERRIDES))
    @pytest.mark.parametrize("role", ACCOUNT_ROLES)
    def test_inherit_reads_the_account(self, role, shape):
        overrides = ACCOUNT_OVERRIDES[shape]
        got = template_keys(INHERIT_ROLE, {}, account_role=role, account_overrides=overrides)
        assert got == _expected_inherit(role, overrides)

    @pytest.mark.parametrize("role", ACCOUNT_ROLES)
    def test_a_custom_role_is_its_stored_keys(self, role):
        got = template_keys(CUSTOM_KEY, {CUSTOM_KEY: CUSTOM_KEYS, "other": frozenset({"x"})},
                            account_role=role, account_overrides=None)
        assert got == CUSTOM_KEYS

    def test_an_unknown_role_grants_nothing(self):
        assert template_keys("ghost", {CUSTOM_KEY: CUSTOM_KEYS}, account_role="member",
                             account_overrides=None) == frozenset()

    @pytest.mark.parametrize("template", TEMPLATES)
    def test_a_custom_row_cannot_shadow_a_builtin(self, template):
        shadow = {template: frozenset(ALL_PROJECT_KEYS)}
        assert template_keys(template, shadow, account_role="member",
                             account_overrides=None) == ROLE_TEMPLATES[template].keys

    def test_a_custom_row_cannot_shadow_inherit(self):
        shadow = {INHERIT_ROLE: frozenset()}
        assert template_keys(INHERIT_ROLE, shadow, account_role="member",
                             account_overrides=None) == inherit_keys("member", None)


# ── apply_overrides ──────────────────────────────────────────────────────────


class TestApplyOverrides:
    @pytest.mark.parametrize("shape", sorted(PROJECT_OVERRIDES))
    @pytest.mark.parametrize("template", [*TEMPLATES, INHERIT_ROLE])
    def test_grants_add_and_denials_remove(self, template, shape):
        base = _role_keys(template, "member", None)
        overrides = PROJECT_OVERRIDES[shape]
        got = apply_overrides(base, overrides)
        assert got == _expected_overrides(base, overrides)
        assert got <= ALL_PROJECT_KEYS | base

    @pytest.mark.parametrize("template", TEMPLATES)
    def test_unknown_keys_change_nothing(self, template):
        base = ROLE_TEMPLATES[template].keys
        assert apply_overrides(base, {"no.such.key": True, "users.manage": True}) == base

    def test_deny_all_empties_and_grant_all_fills(self):
        base = ROLE_TEMPLATES["developer"].keys
        assert apply_overrides(base, PROJECT_OVERRIDES["deny_all"]) == frozenset()
        assert apply_overrides(frozenset(), PROJECT_OVERRIDES["grant_all"]) == ALL_PROJECT_KEYS

    def test_returns_a_new_frozenset(self):
        base = ROLE_TEMPLATES["sme"].keys
        out = apply_overrides(base, {"project.write": True})
        assert isinstance(out, frozenset) and "project.write" not in base


# ── the snapshot: direct ∪ group, then overrides (real ledger) ───────────────

_NOW = "2026-10-15 00:00:00.000000"
_ROLE_SOURCES = [None, *TEMPLATES, INHERIT_ROLE, CUSTOM_KEY]
_UNION_CASES = [
    (direct, group) for direct, group in itertools.product(_ROLE_SOURCES, _ROLE_SOURCES)
    if direct or group
]


def _user(db, uid: str, role: str = "member", permissions: dict | None = None) -> None:
    db.add(User(id=uid, username=uid, username_key=uid, email=f"{uid}@example.com",
                password_hash="x", role=role, permissions=permissions, status="active"))
    db.flush()


def _project(db, pid: str, *, workspace: str = "default", status: str = "active",
             group: str | None = None) -> None:
    db.execute(text("INSERT INTO projects (id, name, description, status, is_default, group_id,"
                    " created_at, updated_at) VALUES (:p, :p, '', :s, 0, :g, :n, :n)"),
               {"p": pid, "s": status, "g": group, "n": _NOW})
    db.execute(text("INSERT INTO project_workspaces (project_id, workspace_id,"
                    " is_workspace_default, enabled_at) VALUES (:p, :w, 0, :n)"),
               {"p": pid, "w": workspace, "n": _NOW})


def _group(db, gid: str, *, archived: bool = False) -> None:
    db.execute(text("INSERT INTO project_groups (id, name, description, created_at, updated_at,"
                    " archived_at) VALUES (:g, :g, '', :n, :n, :a)"),
               {"g": gid, "n": _NOW, "a": _NOW if archived else None})


def _direct(db, pid: str, uid: str, role: str, overrides: dict | None = None) -> None:
    db.execute(text("INSERT INTO project_members (project_id, user_id, role_key, overrides,"
                    " added_at) VALUES (:p, :u, :r, :o, :n)"),
               {"p": pid, "u": uid, "r": role,
                "o": json.dumps(overrides) if overrides is not None else None, "n": _NOW})


def _in_group(db, gid: str, uid: str, role: str) -> None:
    db.execute(text("INSERT INTO group_members (group_id, user_id, role_key, added_at)"
                    " VALUES (:g, :u, :r, :n)"), {"g": gid, "u": uid, "r": role, "n": _NOW})


def _custom_role(db, pid: str, key: str, keys) -> None:
    db.execute(text("INSERT INTO project_roles (project_id, key, name, permissions, created_at,"
                    " updated_at) VALUES (:p, :k, :k, :perms, :n, :n)"),
               {"p": pid, "k": key, "perms": json.dumps(sorted(keys)), "n": _NOW})


def _grant(db, uid: str, workspace: str = "default") -> None:
    db.execute(text("INSERT INTO user_workspaces (user_id, workspace_id, created_at)"
                    " VALUES (:u, :w, :n)"),
               {"u": uid, "w": workspace, "n": _NOW})


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class TestSnapshotUnion:
    @pytest.mark.parametrize("project_shape", sorted(PROJECT_OVERRIDES))
    def test_direct_union_group_then_overrides(self, db, project_shape):
        """Every (direct, group) pair at once: one project + group per pair, one
        snapshot, so the whole product costs a single ledger seed."""
        overrides = PROJECT_OVERRIDES[project_shape]
        _user(db, "u1")
        _grant(db, "u1")  # an inherit-only row reaches only granted workspaces
        cases = {}
        for i, (direct, group) in enumerate(_UNION_CASES):
            pid, gid = f"p{i}", f"g{i}"
            _group(db, gid)
            _project(db, pid, group=gid)
            _custom_role(db, pid, CUSTOM_KEY, CUSTOM_KEYS)
            if direct:
                _direct(db, pid, "u1", direct, overrides)
            if group:
                _in_group(db, gid, "u1", group)
            cases[pid] = (direct, group, gid)
        snap = access.load_snapshot(db, "u1", "member", None)
        assert set(snap.memberships) == set(cases)
        for pid, (direct, group, gid) in cases.items():
            membership = snap.memberships[pid]
            base = _role_keys(direct, "member", None) | _role_keys(group, "member", None)
            # overrides live on the direct row only
            expected = _expected_overrides(base, overrides if direct else None)
            assert membership.permissions == expected, (direct, group)
            assert (membership.direct_role, membership.group_role) == (direct, group)
            sources = dict(membership.role_sources)
            assert sources.get("direct") == direct and sources.get(f"group:{gid}") == group

    @pytest.mark.parametrize("role", MEMBER_ROLES)
    @pytest.mark.parametrize("shape", sorted(ACCOUNT_OVERRIDES))
    def test_inherit_uses_the_account_role_and_overrides(self, db, role, shape):
        overrides = ACCOUNT_OVERRIDES[shape]
        _user(db, "u1", role=role, permissions=overrides)
        _grant(db, "u1")
        _project(db, "alpha")
        _direct(db, "alpha", "u1", INHERIT_ROLE)
        snap = access.load_snapshot(db, "u1", role, overrides)
        assert snap.memberships["alpha"].permissions == _expected_inherit(role, overrides)

    def test_a_group_key_denied_on_the_direct_row_is_gone(self, db):
        _user(db, "u1")
        _group(db, "g1")
        _project(db, "alpha", group="g1")
        _direct(db, "alpha", "u1", "sme", {"mcp.approve": False})
        _in_group(db, "g1", "u1", "tool_steward")  # the group brings mcp.approve
        perms = access.load_snapshot(db, "u1", "member", None).memberships["alpha"].permissions
        assert "mcp.approve" not in perms and "project.use" in perms

    def test_custom_roles_are_per_project(self, db):
        _user(db, "u1")
        _project(db, "alpha")
        _project(db, "beta")
        _custom_role(db, "alpha", CUSTOM_KEY, CUSTOM_KEYS)
        _custom_role(db, "beta", CUSTOM_KEY, {"project.use"})
        _direct(db, "alpha", "u1", CUSTOM_KEY)
        _direct(db, "beta", "u1", CUSTOM_KEY)
        snap = access.load_snapshot(db, "u1", "member", None)
        assert snap.memberships["alpha"].permissions == CUSTOM_KEYS
        assert snap.memberships["beta"].permissions == frozenset({"project.use"})

    def test_a_custom_role_missing_in_this_project_grants_nothing(self, db):
        _user(db, "u1")
        _project(db, "alpha")
        _project(db, "beta")
        _custom_role(db, "beta", CUSTOM_KEY, CUSTOM_KEYS)
        _direct(db, "alpha", "u1", CUSTOM_KEY)
        snap = access.load_snapshot(db, "u1", "member", None)
        assert snap.memberships["alpha"].permissions == frozenset()

    def test_a_custom_role_keeps_only_project_keys(self, db):
        _user(db, "u1")
        _project(db, "alpha")
        _custom_role(db, "alpha", CUSTOM_KEY, {"project.use", "users.manage", "no.such.key"})
        _direct(db, "alpha", "u1", CUSTOM_KEY)
        perms = access.load_snapshot(db, "u1", "member", None).memberships["alpha"].permissions
        assert perms == frozenset({"project.use"})

    def test_an_archived_group_confers_nothing(self, db):
        _user(db, "u1")
        _group(db, "g1", archived=True)
        _project(db, "alpha", group="g1")
        _project(db, "beta", group="g1")
        _direct(db, "beta", "u1", "viewer")
        _in_group(db, "g1", "u1", "developer")
        snap = access.load_snapshot(db, "u1", "member", None)
        assert "alpha" not in snap.memberships
        assert snap.memberships["beta"].permissions == frozenset()
        assert snap.memberships["beta"].group_role is None

    def test_an_archived_project_keeps_its_keys_and_says_so(self, db):
        _user(db, "u1")
        _project(db, "alpha", status="archived")
        _direct(db, "alpha", "u1", "developer")
        membership = access.load_snapshot(db, "u1", "member", None).memberships["alpha"]
        assert membership.status == "archived"
        assert membership.permissions == ROLE_TEMPLATES["developer"].keys

    def test_inherit_alone_reaches_only_granted_workspaces(self, db):
        _user(db, "u1")
        _project(db, "alpha")
        _direct(db, "alpha", "u1", INHERIT_ROLE)
        assert "alpha" not in access.load_snapshot(db, "u1", "member", None).memberships
        _grant(db, "u1")
        snap = access.load_snapshot(db, "u1", "member", None)
        assert snap.memberships["alpha"].workspaces == frozenset({"default"})

    def test_inherit_with_a_group_reaches_without_a_grant(self, db):
        _user(db, "u1")
        _group(db, "g1")
        _project(db, "alpha", group="g1")
        _direct(db, "alpha", "u1", INHERIT_ROLE)
        _in_group(db, "g1", "u1", "sme")
        perms = access.load_snapshot(db, "u1", "member", None).memberships["alpha"].permissions
        assert perms == inherit_keys("member", None) | ROLE_TEMPLATES["sme"].keys

    def test_no_membership_no_entry(self, db):
        _user(db, "u1")
        _project(db, "alpha")
        assert access.load_snapshot(db, "u1", "member", None).memberships == {}


# ── admins, ProjectScope.can, the cache ──────────────────────────────────────


class TestBypassAndCache:
    @pytest.mark.parametrize("role", ACCOUNT_ROLES)
    def test_only_admins_bypass(self, role):
        identity = SimpleNamespace(role=role)
        assert access.has_data_bypass(identity) is (role == "admin")
        assert access.can_manage_structure(identity) is (role in ("admin", "steward"))

    @pytest.mark.parametrize("key", sorted(ALL_PROJECT_KEYS))
    def test_scope_can(self, key):
        common = dict(id="p", name="p", status="active", is_default=False,
                      workspace_id="default", role_sources=())
        assert ProjectScope(permissions=frozenset(), bypass=True, **common).can(key)
        assert not ProjectScope(permissions=frozenset(), bypass=False, **common).can(key)
        assert ProjectScope(permissions=frozenset({key}), bypass=False, **common).can(key)

    def test_an_admin_resolves_with_every_key(self, db):
        _project(db, "alpha")
        db.commit()
        scope = access.resolve_project(db, SimpleNamespace(role="admin", user_id=None),
                                       "default", "alpha", "GET")
        assert scope.bypass and scope.permissions == ALL_PROJECT_KEYS

    def test_the_cache_drops_on_an_epoch_bump(self, db):
        _user(db, "u1")
        _project(db, "alpha")
        _direct(db, "alpha", "u1", "viewer")
        db.commit()
        identity = SimpleNamespace(user_id="u1", role="member", permission_overrides={})
        first = access.snapshot_for(db, identity)
        assert first.memberships["alpha"].permissions == frozenset()
        db.execute(text("UPDATE project_members SET role_key = 'developer' WHERE user_id = 'u1'"))
        db.commit()
        # no bump: the cached snapshot is still served
        assert access.snapshot_for(db, identity) is first
        access.bump_epoch(db)
        db.commit()
        fresh = access.snapshot_for(db, identity)
        assert fresh.memberships["alpha"].permissions == ROLE_TEMPLATES["developer"].keys

    def test_the_account_overrides_are_read_when_the_identity_has_none(self, db):
        _user(db, "u1", permissions={"promotion.approve": True})
        _grant(db, "u1")
        _project(db, "alpha")
        _direct(db, "alpha", "u1", INHERIT_ROLE)
        db.commit()
        snap = access.snapshot_for(db, SimpleNamespace(user_id="u1", role="member"))
        assert {"promotion.approve", "mcp.approve"} <= snap.memberships["alpha"].permissions
