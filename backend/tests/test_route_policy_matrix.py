"""The route policy matrix: every ROUTE_POLICY entry × every kind of caller (projects
design §5.1, §5.3, §9.3-2).

``tests/test_route_policy.py`` pins the table's shape (no drift, no loose workspace
writes, the exemptions agree). This file pins its *decisions*: for each principal
the dependency (``enforce_route_policy``) is run against a fake, already-routed
request carrying that principal's identity, and the outcome — allowed, or the
``(status, code)`` it raised — must equal what the table says it should be.

The expectation is derived from the entry's role string and the principal's facts
(account keys, workspace reach, project memberships), never hand-written per route,
so a new route is covered the moment it is classified. No HTTP, no handler, no AWS:
one test per principal walks every route (~500 dependency calls on the test ledger).

Ledger: workspace ``default`` holds two active projects, ``default`` (the workspace
default) and ``alpha``; the ``single`` layout leaves only ``default``, which is where
``infra`` falls back to the project's own key instead of admin-only.
"""

from dataclasses import dataclass, field

import pytest
from fastapi import Request
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.core.route_policy import (
    ADMIN,
    INFRA,
    MEMBER,
    PPERM_PREFIX,
    PROJECT_EXEMPT,
    PUBLIC,
    ROUTE_POLICY,
    WORKSPACE_EXEMPT,
    enforce_route_policy,
    infra_key,
)
from app.models.ledger import User
from app.routers import auth
from app.routers.auth import Identity, granted_permissions
from app.services import access

PATH_PROJECT = "alpha"  # what `{project_id}` names in a hub-global project route
_PROBE = "route-policy-probe"
_NOW = "2026-10-15 00:00:00.000000"
ROUTES = sorted(ROUTE_POLICY)

Outcome = tuple[int, str] | None  # None = allowed


@dataclass(frozen=True)
class Principal:
    name: str
    identity: Identity | None
    # project id -> effective keys, for every project the caller is a member of
    memberships: dict[str, frozenset[str]] = field(default_factory=dict)
    reaches_workspace: bool = False
    # the project a project-scoped request resolves to with no header (None: none)
    fallback_project: str | None = None
    header: str = ""  # X-Project

    @property
    def is_admin(self) -> bool:
        return self.identity is not None and self.identity.is_admin

    @property
    def account_keys(self) -> frozenset[str]:
        return self.identity.permissions if self.identity else frozenset()


def _member_identity(uid: str) -> Identity:
    return Identity(username=uid, role="member", user_id=uid,
                    permissions=granted_permissions(None, "member"))


def _user(db, uid: str, role: str = "member") -> None:
    db.add(User(id=uid, username=uid, username_key=uid, email=f"{uid}@example.com",
                password_hash="x", role=role, status="active"))
    db.flush()


def _member(db, pid: str, uid: str, role: str) -> None:
    db.execute(text("INSERT INTO project_members (project_id, user_id, role_key, added_at)"
                    " VALUES (:p, :u, :r, :n)"), {"p": pid, "u": uid, "r": role, "n": _NOW})


def _seed(layout: str) -> dict[str, Principal]:
    """The ledger and the principals of one layout (`shared`: default + alpha)."""
    db = SessionLocal()
    try:
        if layout == "shared":
            db.execute(text("INSERT INTO projects (id, name, description, status, is_default,"
                            " created_at, updated_at) VALUES (:p, 'Alpha', '', 'active', 0,"
                            " :n, :n)"), {"p": PATH_PROJECT, "n": _NOW})
            db.execute(text("INSERT INTO project_workspaces (project_id, workspace_id,"
                            " is_workspace_default, enabled_at) VALUES (:p, 'default', 0, :n)"),
                       {"p": PATH_PROJECT, "n": _NOW})
        for uid in ("u-admin", "u-inherit", "u-dev", "u-viewer", "u-owner", "u-none"):
            _user(db, uid, role="admin" if uid == "u-admin" else "member")
        # the default project's compatibility member: workspace grant + inherit row
        db.execute(text("INSERT INTO user_workspaces (user_id, workspace_id, created_at)"
                        " VALUES ('u-inherit', 'default', :n)"), {"n": _NOW})
        _member(db, "default", "u-inherit", access.INHERIT_ROLE)
        if layout == "shared":
            # reach `default` through alpha's binding alone (design D-6), no grant
            _member(db, PATH_PROJECT, "u-dev", "developer")
            _member(db, PATH_PROJECT, "u-viewer", "viewer")
            _member(db, PATH_PROJECT, "u-owner", "project_owner")
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()

    inherit = access.inherit_keys("member", None)
    templates = access.ROLE_TEMPLATES
    principals = [
        Principal("anonymous", None),
        Principal("builtin_admin", Identity(username="operator", role="admin")),
        Principal("registered_admin", Identity(
            username="u-admin", role="admin", user_id="u-admin",
            permissions=granted_permissions(None, "admin"))),
        Principal("inherit_member", _member_identity("u-inherit"), {"default": inherit},
                  reaches_workspace=True, fallback_project="default"),
        Principal("no_project", _member_identity("u-none")),
    ]
    if layout == "shared":
        dev = {PATH_PROJECT: templates["developer"].keys}
        viewer = {PATH_PROJECT: templates["viewer"].keys}
        owner = {PATH_PROJECT: templates["project_owner"].keys}
        principals += [
            Principal("developer_a", _member_identity("u-dev"), dev,
                      reaches_workspace=True, fallback_project=PATH_PROJECT),
            Principal("viewer_a", _member_identity("u-viewer"), viewer,
                      reaches_workspace=True, fallback_project=PATH_PROJECT),
            Principal("owner_a", _member_identity("u-owner"), owner,
                      reaches_workspace=True, fallback_project=PATH_PROJECT),
            # addressing a project of the workspace they are not a member of
            Principal("developer_a_sends_default", _member_identity("u-dev"), dev,
                      reaches_workspace=True, fallback_project=PATH_PROJECT,
                      header="default"),
            Principal("inherit_member_sends_alpha", _member_identity("u-inherit"),
                      {"default": inherit}, reaches_workspace=True,
                      fallback_project="default", header=PATH_PROJECT),
        ]
    return {p.name: p for p in principals}


SHARED = ["anonymous", "builtin_admin", "registered_admin", "inherit_member", "developer_a",
          "viewer_a", "no_project", "owner_a", "developer_a_sends_default",
          "inherit_member_sends_alpha"]
SINGLE = ["builtin_admin", "registered_admin", "inherit_member", "no_project"]
CASES = [("shared", name) for name in SHARED] + [("single", name) for name in SINGLE]


# ── the expectation, from the table ──────────────────────────────────────────


def _role_key(role: str) -> str:
    """The key a role string names (`perm:<k>` / `pperm:<k>` / `infra[+perm:<k>]`)."""
    if role.startswith(INFRA):
        return infra_key(role)
    if role.startswith(PPERM_PREFIX):
        return role.removeprefix(PPERM_PREFIX)
    return role.removeprefix("perm:") if role.startswith("perm:") else ""


def expected(p: Principal, layout: str, method: str, path: str) -> Outcome:
    role = ROUTE_POLICY[(method, path)]
    if p.identity is None:
        # auth_middleware: every /api path but the login surface needs a session —
        # including the PUBLIC docs, which are only public with the gate off
        guarded = auth._is_guarded_api_path(_concrete(path)[0])
        return (401, "auth.required") if guarded else None
    if role == PUBLIC:
        return None
    if p.is_admin:
        return None  # every route, every workspace, every project (dev tier)
    if role == ADMIN:
        return (403, "auth.forbidden")
    key = _role_key(role)
    if role.startswith(PPERM_PREFIX):
        held = p.memberships.get(PATH_PROJECT)
        if held is None:
            return (404, "project.not_found")
        return None if key in held else (403, "auth.permission_required")
    denied = (403, "auth.permission_required")
    if (method, path) in WORKSPACE_EXEMPT:
        return None if role == MEMBER or key in p.account_keys else denied
    project_scoped = (method, path) not in PROJECT_EXEMPT
    failure: Outcome = None
    if not p.reaches_workspace:
        failure = (403, "workspace.forbidden")
    elif project_scoped:
        wanted = p.header or p.fallback_project
        if wanted is None:
            failure = (403, "project.none")
        elif wanted not in p.memberships:
            failure = (404, "project.not_found")
    if failure is not None:
        # precedence: lacking an account key outright says so before naming scopes
        if key and not key.startswith("project.") and key not in p.account_keys:
            return denied
        return failure
    keys = p.memberships[p.header or p.fallback_project] if project_scoped else p.account_keys
    if role == MEMBER:
        return None
    if role.startswith(INFRA) and layout == "shared":
        return (403, "auth.infra_admin_required")
    return None if key in keys else denied


# ── the decision, from the dependency ────────────────────────────────────────


def _concrete(path_format: str) -> tuple[str, dict[str, str]]:
    params: dict[str, str] = {}
    out = []
    for segment in path_format.split("/"):
        if segment.startswith("{"):
            name = segment.strip("{}").split(":")[0]
            params[name] = PATH_PROJECT if name == "project_id" else _PROBE
            out.append(params[name])
        else:
            out.append(segment)
    return "/".join(out), params


class _Route:
    def __init__(self, path_format: str) -> None:
        self.path_format = path_format


def _request(p: Principal, method: str, path_format: str) -> Request:
    path, params = _concrete(path_format)
    headers = [(access.PROJECT_HEADER.lower().encode(), p.header.encode())] if p.header else []
    return Request({
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": b"",
        "client": ("127.0.0.1", 4321),
        "route": _Route(path_format),
        "path_params": params,
        # what auth_middleware leaves behind for a live session
        "state": {"identity": p.identity} if p.identity is not None else {},
    })


def decide(p: Principal, method: str, path_format: str) -> Outcome:
    """The pipeline's answer: the middleware's 401, then the policy dependency."""
    if p.identity is None and auth._is_guarded_api_path(_concrete(path_format)[0]):
        return (401, "auth.required")
    return _dependency(p, method, path_format)


def _dependency(p: Principal, method: str, path_format: str) -> Outcome:
    try:
        enforce_route_policy(_request(p, method, path_format))
    except AppError as exc:
        return (exc.status_code, exc.code)
    return None


@pytest.fixture
def gated(monkeypatch):
    """Login gate on: identities come from `request.state`, not the open console."""
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _mismatches(p: Principal, layout: str) -> list[str]:
    out = []
    for method, path in ROUTES:
        want, got = expected(p, layout, method, path), decide(p, method, path)
        if want != got:
            out.append(f"{method} {path} [{ROUTE_POLICY[(method, path)]}]: "
                       f"expected {want or 'allowed'}, got {got or 'allowed'}")
    return out


class TestRoutePolicyMatrix:
    @pytest.mark.parametrize(("layout", "name"), CASES)
    def test_every_route_decides_as_the_table_says(self, gated, layout, name):
        principal = _seed(layout)[name]
        wrong = _mismatches(principal, layout)
        assert not wrong, f"{name} ({layout}): {len(wrong)} routes\n" + "\n".join(wrong[:40])


class TestTheMatrixIsNotVacuous:
    """Guard the derivation itself: each principal sees the outcomes it should, so a
    model that collapsed to "always allowed" (or always 403) could not pass."""

    def test_the_shared_layout_exercises_every_outcome(self, gated):
        principals = _seed("shared")
        seen: dict[str, set] = {
            name: {expected(p, "shared", m, path) for m, path in ROUTES}
            for name, p in principals.items()
        }
        assert seen["builtin_admin"] == {None} and seen["registered_admin"] == {None}
        assert seen["anonymous"] == {None, (401, "auth.required")}
        assert {None, (403, "auth.forbidden"), (403, "auth.infra_admin_required"),
                (404, "project.not_found")} <= seen["inherit_member"]
        assert (403, "auth.permission_required") in seen["viewer_a"]
        assert (403, "workspace.forbidden") in seen["no_project"]
        assert (404, "project.not_found") in seen["developer_a_sends_default"]
        # a project owner passes the path-project routes a developer cannot
        pperm = [k for k in ROUTES if ROUTE_POLICY[k].startswith(PPERM_PREFIX)]
        assert pperm
        assert {expected(principals["owner_a"], "shared", *k) for k in pperm} == {None}
        assert {expected(principals["developer_a"], "shared", *k) for k in pperm} == {
            (403, "auth.permission_required")}

    def test_a_viewer_can_read_but_not_write(self, gated):
        viewer = _seed("shared")["viewer_a"]
        assert decide(viewer, "GET", "/api/agents") is None
        assert decide(viewer, "POST", "/api/agents") == (403, "auth.permission_required")

    def test_single_project_infra_is_the_projects_write_key(self, gated):
        member = _seed("single")["inherit_member"]
        infra = [k for k in ROUTES if ROUTE_POLICY[k] == INFRA]
        assert infra and all(decide(member, *k) is None for k in infra)


class TestAnonymousAtTheDependency:
    """Without the middleware in front: the dependency alone refuses an anonymous
    caller everywhere except PUBLIC and hub-global MEMBER routes (whose only guard
    is the middleware's live-session check)."""

    def test_the_dependency_refuses_anonymous_on_its_own(self, gated):
        anonymous = Principal("anonymous", None)
        wrong = []
        for method, path in ROUTES:
            role = ROUTE_POLICY[(method, path)]
            open_here = role == PUBLIC or (role == MEMBER and (method, path) in WORKSPACE_EXEMPT)
            want = None if open_here else (401, "auth.required")
            got = _dependency(anonymous, method, path)
            if want != got:
                wrong.append(f"{method} {path}: expected {want}, got {got}")
        assert not wrong, "\n".join(wrong[:40])
