"""Projects phase 2 (design §10.7): /v1 per-project rate limit and the project's own
inbound JWT default.

* `settings.quotas.v1_rate_per_minute` caps all of a project's keys together, beside
  each key's own limit; the default project (no quotas) is unchanged.
* `settings.inbound_auth_default` is an isolated project's own inbound JWT default:
  the spec still wins, and a shared project resolves to the workspace default.
* Both are administrator settings on `PATCH /api/projects/{id}`, shape-validated.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.ledger import ApiKey, Workspace
from app.models.projects import Project
from app.services import api_keys as api_keys_service
from app.services import inbound_auth
from tests.test_project_scope import _login, app  # noqa: F401
from tests.test_projects_api_keys import (  # noqa: F401
    ALPHA,
    _admin,
    _mint,
    _v1,
    stub_invoke,
    world,
)

WS = DEFAULT_WORKSPACE_ID
JWT = {
    "mode": "jwt",
    "jwt": {
        "discovery_url": "https://idp.example.com/alpha/.well-known/openid-configuration",
        "allowed_audience": ["alpha-api"],
    },
}
WS_JWT = {
    "mode": "jwt",
    "jwt": {
        "discovery_url": "https://idp.example.com/ws/.well-known/openid-configuration",
        "allowed_clients": ["ws-client"],
    },
}


def _set_settings(pid: str, settings: dict | None) -> None:
    db = SessionLocal()
    try:
        db.get(Project, pid).settings = settings
        db.commit()
    finally:
        db.close()


def _settings(pid: str) -> dict:
    db = SessionLocal()
    try:
        return dict(db.get(Project, pid).settings or {})
    finally:
        db.close()


# ── the project window ───────────────────────────────────────────────────────


def test_check_rate_counts_every_key_of_the_project_together():
    a = ApiKey(id="k-a", project_id="alpha", rate_per_minute=None)
    b = ApiKey(id="k-b", project_id="alpha", rate_per_minute=None)
    other = ApiKey(id="k-c", project_id="beta", rate_per_minute=None)
    api_keys_service.check_rate(a, project_limit=2, now=100.0)
    api_keys_service.check_rate(b, project_limit=2, now=101.0)
    with pytest.raises(AppError) as exc:
        api_keys_service.check_rate(a, project_limit=2, now=102.0)
    assert exc.value.status_code == 429
    assert exc.value.detail["scope"] == "project"
    assert exc.value.detail["limit_per_minute"] == 2
    assert 1 <= int(exc.value.headers["Retry-After"]) <= 61
    api_keys_service.check_rate(other, project_limit=2, now=102.0)  # another project
    api_keys_service.check_rate(a, project_limit=2, now=160.5)  # the window slid
    # no project limit: the project window is neither checked nor fed
    for i in range(5):
        api_keys_service.check_rate(a, now=161.0 + i)


def test_a_refusal_by_one_window_does_not_use_the_others_room():
    key = ApiKey(id="k-a", project_id="alpha", rate_per_minute=1)
    api_keys_service.check_rate(key, project_limit=2, now=100.0)
    with pytest.raises(AppError) as exc:
        api_keys_service.check_rate(key, project_limit=2, now=101.0)
    assert "scope" not in exc.value.detail  # the key's own limit, as before
    sibling = ApiKey(id="k-b", project_id="alpha", rate_per_minute=None)
    api_keys_service.check_rate(sibling, project_limit=2, now=102.0)  # 2nd project slot
    with pytest.raises(AppError):
        api_keys_service.check_rate(sibling, project_limit=2, now=103.0)


def test_project_rate_limit_reads_the_quota(app, world):  # noqa: F811
    key = ApiKey(id="k", project_id="alpha")
    db = SessionLocal()
    try:
        assert api_keys_service.project_rate_limit(db, key) is None
        db.get(Project, "alpha").settings = {"quotas": {"v1_rate_per_minute": 7}}
        db.commit()
        assert api_keys_service.project_rate_limit(db, key) == 7
        for bad in (0, -1, True, "7", None):
            db.get(Project, "alpha").settings = {"quotas": {"v1_rate_per_minute": bad}}
            db.commit()
            assert api_keys_service.project_rate_limit(db, key) is None
        assert api_keys_service.project_rate_limit(db, ApiKey(id="k", project_id=None)) is None
    finally:
        db.close()


def test_v1_enforces_the_project_window_across_keys(app, world):  # noqa: F811
    mei = _login(app, "mei")
    first, second = _mint(mei, ALPHA), _mint(mei, ALPHA)
    default_key = _mint(mei, {"X-Project": "default"})
    _set_settings("alpha", {"quotas": {"v1_rate_per_minute": 2}})

    assert _v1(app, first["key"]).status_code == 200
    assert _v1(app, second["key"]).status_code == 200
    limited = _v1(app, first["key"])
    assert limited.status_code == 429
    assert limited.json()["code"] == "auth.rate_limited"
    assert limited.headers["Retry-After"]
    assert _v1(app, second["key"]).status_code == 429
    # the default project has no quota: its key is untouched by alpha's window
    for _ in range(4):
        assert _v1(app, default_key["key"]).status_code == 200
    # a refused call is not counted
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.get(ApiKey, first["id"]).use_count == 1
    finally:
        db.close()
    # lifting the quota applies on the next call
    _set_settings("alpha", None)
    assert _v1(app, first["key"]).status_code == 200


# ── the project's inbound JWT default ────────────────────────────────────────


def _agent_stub(project_id: str | None, spec: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(spec=spec or {}, workspace_id=WS, project_id=project_id,
                           method="zip_runtime")


def _set_workspace_default(value: dict | None) -> None:
    db = SessionLocal()
    try:
        row = db.get(Workspace, WS)
        settings = dict(row.settings or {})
        settings.pop(inbound_auth.SETTINGS_KEY, None)
        if value is not None:
            settings[inbound_auth.SETTINGS_KEY] = value
        row.settings = settings
        db.commit()
    finally:
        db.close()


def test_an_isolated_projects_default_stands_in_for_the_workspaces(app, world):  # noqa: F811
    _set_workspace_default(WS_JWT)
    try:
        db = SessionLocal()
        try:
            # stored but shared: ignored
            _set_settings("alpha", {"inbound_auth_default": JWT})
            resolved = inbound_auth.resolve_for_agent(_agent_stub("alpha"), db)
            assert resolved.jwt.discovery_url == WS_JWT["jwt"]["discovery_url"]

            _set_settings("alpha", {"inbound_auth_default": JWT,
                                    "isolation": {"mode": "isolated"}})
            db.expire_all()
            resolved = inbound_auth.resolve_for_agent(_agent_stub("alpha"), db)
            assert resolved.mode == "jwt"
            assert resolved.jwt.allowed_audience == ["alpha-api"]
            # the spec still wins
            spec = {"inbound_auth": {"mode": "iam"}}
            assert inbound_auth.resolve_for_agent(_agent_stub("alpha", spec), db).mode == "iam"
            # another project, the default one, and a stand-in without a project
            for pid in ("default", None):
                resolved = inbound_auth.resolve_for_agent(_agent_stub(pid), db)
                assert resolved.jwt.discovery_url == WS_JWT["jwt"]["discovery_url"]
            # isolated with no own default: the workspace's
            _set_settings("alpha", {"isolation": {"mode": "isolated"}})
            db.expire_all()
            resolved = inbound_auth.resolve_for_agent(_agent_stub("alpha"), db)
            assert resolved.jwt.discovery_url == WS_JWT["jwt"]["discovery_url"]
        finally:
            db.close()
    finally:
        _set_workspace_default(None)


def test_project_default_reads_only_an_isolated_project():
    assert inbound_auth.project_default(None) is None
    shared = Project(id="p", settings={"inbound_auth_default": JWT})
    assert inbound_auth.project_default(shared) is None
    isolated = Project(id="p", settings={"inbound_auth_default": JWT,
                                         "isolation": {"mode": "isolated"}})
    assert inbound_auth.project_default(isolated).mode == "jwt"
    garbled = Project(id="p", settings={"inbound_auth_default": {"mode": "jwt"},
                                        "isolation": {"mode": "isolated"}})
    assert inbound_auth.project_default(garbled) is None


# ── PATCH /api/projects/{id} settings ────────────────────────────────────────


def test_admin_sets_and_clears_the_new_settings(app, world):  # noqa: F811
    admin: TestClient = _admin(app)
    res = admin.patch("/api/projects/alpha", json={"settings": {
        "quotas": {"v1_rate_per_minute": 600}, "inbound_auth_default": JWT}})
    assert res.status_code == 200, res.text
    stored = res.json()["settings"]
    assert stored["quotas"] == {"v1_rate_per_minute": 600}
    assert stored["inbound_auth_default"]["mode"] == "jwt"
    assert stored["inbound_auth_default"]["jwt"]["allowed_audience"] == ["alpha-api"]
    # normalized to the full model
    assert stored["inbound_auth_default"]["jwt"]["custom_claims"] == []

    # another quota-less PATCH keeps the quota (quotas merge one level deep)
    assert admin.patch("/api/projects/alpha", json={
        "settings": {"sod_strict": False}}).status_code == 200
    assert _settings("alpha")["quotas"] == {"v1_rate_per_minute": 600}

    res = admin.patch("/api/projects/alpha", json={"settings": {
        "quotas": {"v1_rate_per_minute": None}, "inbound_auth_default": None}})
    assert res.status_code == 200, res.text
    assert "quotas" not in _settings("alpha")
    assert "inbound_auth_default" not in _settings("alpha")


@pytest.mark.parametrize("settings", [
    {"quotas": {"v1_rate_per_minute": 0}},
    {"quotas": {"v1_rate_per_minute": api_keys_service.MAX_RATE_PER_MINUTE + 1}},
    {"quotas": {"v1_rate_per_minute": "600"}},
    {"quotas": {"v1_rate_per_minute": True}},
    {"quotas": {"max_agents": 3}},
    {"quotas": [600]},
    {"inbound_auth_default": {"mode": "jwt"}},
    {"inbound_auth_default": {"mode": "jwt", "jwt": {
        "discovery_url": "http://idp.example.com/.well-known/openid-configuration",
        "allowed_audience": ["a"]}}},
    {"inbound_auth_default": {"mode": "jwt", "jwt": {
        "discovery_url": "https://idp.example.com/.well-known/openid-configuration"}}},
    {"inbound_auth_default": "jwt"},
])
def test_invalid_shapes_are_refused(app, world, settings):  # noqa: F811
    res = _admin(app).patch("/api/projects/alpha", json={"settings": settings})
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "project.setting_invalid"
    assert _settings("alpha") == {}


def test_an_owner_may_not_set_them(app, world):  # noqa: F811
    from tests.test_project_scope import _member_of, _user

    olga = _user("olga")
    _member_of("alpha", olga, "project_owner")
    client = _login(app, "olga")
    for settings in ({"quotas": {"v1_rate_per_minute": 5}}, {"inbound_auth_default": JWT}):
        res = client.patch("/api/projects/alpha", json={"settings": settings})
        assert res.status_code == 403, res.text
        assert res.json()["code"] == "project.setting_forbidden"
    assert _settings("alpha") == {}


def test_create_accepts_them(app, world):  # noqa: F811
    res = _admin(app).post("/api/projects", json={
        "id": "gamma", "name": "Gamma", "workspaces": [WS],
        "settings": {"quotas": {"v1_rate_per_minute": 30}},
    })
    assert res.status_code in (200, 201), res.text
    assert _settings("gamma") == {"quotas": {"v1_rate_per_minute": 30}}

