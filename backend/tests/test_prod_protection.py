"""T05 — workspace tier + prod protection.

Covers: the additive `workspaces.tier` migration (existing rows become `dev`, the
startup mirror never clobbers an admin's tier), tier on register/patch with the
explicit prod-crossing confirmation, the central refusal of member agent mutations
on a `prod` workspace, the admin break-glass journal, reads/invoke staying open,
and the drift rules that keep `PROD_PROTECTED` honest.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, Base, SessionLocal
from app.core.route_policy import (
    ADMIN,
    PROD_PROTECTED,
    PROD_UNPROTECTED_AGENT_ROUTES,
    ROUTE_POLICY,
    WORKSPACE_EXEMPT,
)
from app.main import create_app
from app.models.ledger import AuditEvent, Workspace
from app.services import users as users_service

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "prod-member",
    "email": "prod-member@acme-corp.com",
    "password": "sufficient-pass",
}
NEW = {"id": "acct-prod", "name": "Prod", "account_id": "444455556677", "region": "us-west-1"}


def _concrete(path_format: str) -> str:
    return "/".join(
        "prod-probe" if segment.startswith("{") else segment for segment in path_format.split("/")
    )


def _set_tier(workspace_id: str, tier: str) -> None:
    db = SessionLocal()
    try:
        db.get(Workspace, workspace_id).tier = tier
        db.commit()
    finally:
        db.close()


def _audit_rows(workspace_id: str) -> list[AuditEvent]:
    db = SessionLocal()
    try:
        return list(
            db.scalars(
                select(AuditEvent)
                .where(AuditEvent.workspace_id == workspace_id)
                .order_by(AuditEvent.created_at)
            )
        )
    finally:
        db.close()


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()
    _set_tier(DEFAULT_WORKSPACE_ID, "dev")


@pytest.fixture
def admin(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        yield client


@pytest.fixture
def member(gated_app):
    """An approved member holding a grant on `default` (the prod workspace here)."""
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.commit()
        finally:
            db.close()
        login = client.post(
            "/api/auth/login",
            json={"username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"]},
        )
        assert login.status_code == 200, login.text
        yield client


# ── migration ───────────────────────────────────────────────────────────────


def test_existing_rows_become_dev_and_the_mirror_keeps_an_admin_tier(tmp_path, monkeypatch):
    stub = SimpleNamespace(account_id="111122223333", region="eu-central-1", resources={})
    monkeypatch.setattr(db_module, "get_settings", lambda: stub)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.init_db(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO workspaces (id, name, account_id, region, bootstrap_status,"
                " resources, created_at, updated_at) VALUES ('w2', 'W2', '222233334444',"
                " 'us-east-1', 'ready', '{}', '2026-08-01 00:00:00', '2026-08-01 00:00:00')"
            )
        )
        # a ledger from before T05: no tier column at all
        conn.execute(sa.text("ALTER TABLE workspaces DROP COLUMN tier"))
    assert "workspaces" in db_module.schema_drift(engine)

    db_module.init_db(engine)

    assert db_module.schema_drift(engine) == {}
    with engine.begin() as conn:
        tiers = dict(conn.execute(sa.text("SELECT id, tier FROM workspaces")).all())
        assert tiers == {DEFAULT_WORKSPACE_ID: "dev", "w2": "dev"}
        conn.execute(sa.text("UPDATE workspaces SET tier = 'prod' WHERE id = 'default'"))
    # the default row re-mirrors settings on every startup — but never its tier
    stub.account_id = "999988887777"
    db_module.init_db(engine)
    with engine.begin() as conn:
        row = conn.execute(
            sa.text("SELECT account_id, tier FROM workspaces WHERE id = 'default'")
        ).one()
    assert tuple(row) == ("999988887777", "prod")
    assert Base.metadata.tables["audit_events"] is not None


# ── tier on register / patch ────────────────────────────────────────────────


class TestTierAdministration:
    def test_register_defaults_to_dev_and_accepts_a_tier(self, admin):
        created = admin.post("/api/workspaces", json=NEW)
        assert created.status_code == 201 and created.json()["tier"] == "dev"
        other = {**NEW, "id": "acct-stg", "account_id": "444455556688", "tier": "staging"}
        assert admin.post("/api/workspaces", json=other).json()["tier"] == "staging"
        rows = admin.get("/api/workspaces").json()["workspaces"]
        listed = {row["id"]: row["tier"] for row in rows}
        assert listed[DEFAULT_WORKSPACE_ID] == "dev"
        assert listed["acct-stg"] == "staging"
        bad = {**NEW, "id": "acct-bad", "account_id": "444455556699", "tier": "qa"}
        assert admin.post("/api/workspaces", json=bad).status_code == 422

    def test_crossing_prod_needs_confirmation_and_is_journaled(self, admin):
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        url = f"/api/workspaces/{NEW['id']}"
        refused = admin.patch(url, json={"tier": "prod"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "workspace.tier_change_unconfirmed"
        assert _audit_rows(NEW["id"]) == []

        promoted = admin.patch(url, json={"tier": "prod", "confirm_tier_change": True})
        assert promoted.status_code == 200 and promoted.json()["tier"] == "prod"
        # leaving prod is just as explicit
        assert admin.patch(url, json={"tier": "staging"}).status_code == 409
        assert admin.patch(
            url, json={"tier": "staging", "confirm_tier_change": True}
        ).json()["tier"] == "staging"
        # dev <-> staging does not touch prod, so no confirmation is needed
        assert admin.patch(url, json={"tier": "dev"}).json()["tier"] == "dev"

        journal = [(row.actor, row.action, row.target) for row in _audit_rows(NEW["id"])]
        assert journal == [
            ("operator", "workspace.tier_change", "dev->prod"),
            ("operator", "workspace.tier_change", "prod->staging"),
            ("operator", "workspace.tier_change", "staging->dev"),
        ]

    def test_rename_still_works_and_an_empty_patch_is_400(self, admin):
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        url = f"/api/workspaces/{NEW['id']}"
        renamed = admin.patch(url, json={"name": "Prod One"})
        assert renamed.json()["name"] == "Prod One" and renamed.json()["tier"] == "dev"
        assert admin.patch(url, json={}).json()["code"] == "workspace.empty_patch"

    def test_members_cannot_change_a_tier(self, admin, member):
        response = member.patch(f"/api/workspaces/{DEFAULT_WORKSPACE_ID}", json={"tier": "dev"})
        assert response.status_code == 403


# ── the prod guard ──────────────────────────────────────────────────────────

from app.services.users import default_permissions  # noqa: E402

_MEMBER_PERMISSIONS = {f"perm:{key}" for key in default_permissions("member")}
# Routes a plain member can actually reach: the prod guard runs after the permission
# check, so a route gated on a permission a member lacks answers with that refusal
# instead — the guard itself is then exercised by the admin break-glass test.
MEMBER_REACHABLE_PROTECTED = sorted(
    k for k in PROD_PROTECTED
    if ROUTE_POLICY[k] != ADMIN
    and (not ROUTE_POLICY[k].startswith("perm:") or ROUTE_POLICY[k] in _MEMBER_PERMISSIONS)
)


class TestProdGuard:
    @pytest.mark.parametrize(("method", "path_format"), MEMBER_REACHABLE_PROTECTED)
    def test_a_member_is_refused_on_prod(self, member, method, path_format):
        _set_tier(DEFAULT_WORKSPACE_ID, "prod")
        response = member.request(method, _concrete(path_format), json={})
        assert response.status_code == 403, response.text
        body = response.json()
        assert body["code"] == "workspace.prod_protected"
        assert "promotion" in body["message"]

    def test_the_same_member_is_not_refused_off_prod(self, member):
        _set_tier(DEFAULT_WORKSPACE_ID, "staging")
        response = member.delete(_concrete("/api/agents/{agent_id}"))
        assert response.status_code == 404  # reached the handler: no such agent

    def test_an_admin_is_let_through_and_journaled(self, admin):
        _set_tier(DEFAULT_WORKSPACE_ID, "prod")
        before = len(_audit_rows(DEFAULT_WORKSPACE_ID))
        response = admin.delete("/api/agents/prod-probe")
        assert response.status_code == 404, response.text  # the handler ran
        rows = _audit_rows(DEFAULT_WORKSPACE_ID)[before:]
        assert [(r.actor, r.action, r.target) for r in rows] == [
            ("operator", "DELETE /api/agents/{agent_id}", "/api/agents/prod-probe")
        ]

    def test_reads_and_invoke_stay_open_for_a_member_on_prod(self, member):
        _set_tier(DEFAULT_WORKSPACE_ID, "prod")
        assert member.get("/api/agents").status_code == 200
        assert member.get("/api/workspaces").json()["workspaces"][0]["tier"] == "prod"
        # invoke/chat reach their handlers (404: no such agent), never the guard
        for method, path in (
            ("POST", "/api/agents/prod-probe/invoke"),
            ("POST", "/api/chat/prod-probe"),
            ("GET", "/api/agents/prod-probe"),
        ):
            response = member.request(method, path, json={"prompt": "hi", "message": "hi"})
            assert response.json().get("code") != "workspace.prod_protected", (method, path)


# ── drift ───────────────────────────────────────────────────────────────────


class TestProdProtectionCoverage:
    def test_every_entry_is_a_classified_workspace_scoped_mutation(self):
        """The guard runs only after workspace resolution, so an exempt entry would
        be silently unenforced; a GET entry would block a read."""
        assert PROD_PROTECTED <= set(ROUTE_POLICY)
        assert not PROD_PROTECTED & WORKSPACE_EXEMPT
        assert all(method != "GET" for method, _ in PROD_PROTECTED)

    def test_every_agent_lifecycle_permission_has_a_prod_decision(self):
        lifecycle = {k for k, role in ROUTE_POLICY.items() if role.startswith("perm:agents.")}
        undecided = sorted(lifecycle - PROD_PROTECTED - set(PROD_UNPROTECTED_AGENT_ROUTES))
        assert not undecided, f"classify these for prod (PROD_PROTECTED or exempt): {undecided}"
        assert not PROD_PROTECTED & set(PROD_UNPROTECTED_AGENT_ROUTES)
        assert set(PROD_UNPROTECTED_AGENT_ROUTES) <= set(ROUTE_POLICY)

    def test_every_agent_mutation_route_is_protected(self):
        """Any non-read route under the agent namespaces is protected, except
        invoke (member parity with Chat)."""
        open_by_design = {
            ("POST", "/api/agents/{agent_id}/invoke"),
            # T14: a chat link changes no agent, so a prod workspace may hand one out
            ("POST", "/api/agents/{agent_id}/share-links"),
            # T30: a Slack/Feishu channel link is a share link of another kind
            ("POST", "/api/agents/{agent_id}/channel-links"),
            # T20: bundling freezes a publish that already exists — it writes one ledger
            # row and touches no AWS resource, and refusing it in prod would make a prod
            # agent the one thing that can never be re-released from its own environment
            ("POST", "/api/agents/{agent_id}/release-bundles"),
            # T34: a reviewer link is a share link of another kind
            ("POST", "/api/agents/{agent_id}/review-links"),
            # T35: curated answers are ledger rows, not agent mutations: fixing a wrong
            # answer in production without a redeploy is the feature. Writes are journaled
            # in audit_events by the handlers.
            ("POST", "/api/agents/{agent_id}/rules"),
            ("PATCH", "/api/agents/{agent_id}/rules/{rule_id}"),
            ("DELETE", "/api/agents/{agent_id}/rules/{rule_id}"),
            ("PUT", "/api/agents/{agent_id}/rules-order"),
            ("PUT", "/api/agents/{agent_id}/rules-enabled"),
            ("POST", "/api/agents/{agent_id}/rules/test"),  # read-only dry run
            # creating an experiment writes one ledger row; every step that touches the
            # agent (bundles, gateway, accept, promote) runs through its protected action
            ("POST", "/api/experiments"),
        }
        # canaries and experiments are agent mutations too: a canary's setup rolls the
        # runtime to a member-authored candidate and an experiment's promote redeploys
        # it, so a prod member could otherwise bypass promotion through them
        mutations = {
            (method, path)
            for method, path in ROUTE_POLICY
            if method != "GET"
            and (
                path == "/api/agents"
                or path.startswith((
                    "/api/agents/",
                    "/api/system-agents",
                    "/api/runtime-canaries",
                    "/api/experiments",
                ))
            )
        }
        # a route is decided when it is protected, listed open-by-design here, or carries
        # a written reason in PROD_UNPROTECTED_AGENT_ROUTES
        decided = PROD_PROTECTED | set(PROD_UNPROTECTED_AGENT_ROUTES)
        assert mutations - open_by_design <= decided
