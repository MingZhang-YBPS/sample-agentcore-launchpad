"""T37–T39 — the fleet view, governance health and the template marketplace.

The invariants worth pinning: a workspace nobody can read says so instead of reporting
zeroes, a health score is never a number without a listed finding behind it, and a
published template carries the agent's *shape* without carrying another environment's ids
or anybody's secrets.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import (
    Agent,
    AlertRule,
    Deployment,
    Job,
    Promotion,
    ReleaseBundle,
    SharedTemplate,
    Workspace,
)
from app.services import fleet as fleet_service
from app.services import marketplace as marketplace_service
from app.services import users as users_service

ADMIN_CREDS = {"username": "fleet-admin", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "fleet-member",
    "email": "fleet-member@acme-corp.com",
    "password": "sufficient-pass",
}

# An agent carrying exactly the things a template must NOT republish.
RICH_SPEC = {
    "name": "rich-agent",
    "method": "harness",
    "system_prompt": "you are helpful",
    "model_id": "anthropic.claude",
    "memory": {"short_term": True, "long_term": True, "memory_id": "mem-dev-only"},
    "guardrail": {"enabled": True, "mode": "anonymize"},
    "knowledge_bases": [{"kb_id": "KBDEVONLY1", "name": "HR policies", "description": "PDFs"}],
    "tools": [{"id": "tgt-dev-only", "name": "hr-database", "kind": "gateway"}],
    "skills": ["skills/dev-only/expenses"],
    "env": {"SECRET_TOKEN": "do-not-publish"},
    "max_iterations": 12,
}


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _reset() -> None:
    db = SessionLocal()
    try:
        for model in (SharedTemplate, AlertRule, Promotion, ReleaseBundle, Deployment, Job, Agent):
            for row in db.query(model).all():
                db.delete(row)
        for row in db.query(Workspace).filter(Workspace.id != DEFAULT_WORKSPACE_ID).all():
            db.delete(row)
        db.commit()
    finally:
        db.close()


def _agent(
    spec: dict | None = None,
    *,
    workspace: str = DEFAULT_WORKSPACE_ID,
    status: str = "active",
    name: str | None = None,
    deployed_days_ago: int | None = None,
) -> str:
    db = SessionLocal()
    try:
        used = dict(spec or RICH_SPEC)
        agent = Agent(
            workspace_id=workspace,
            name=name or used.get("name", "an-agent"),
            method=used.get("method", "harness"),
            status=status,
            spec=used,
            arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/h",
            version="2",
        )
        db.add(agent)
        db.flush()
        if deployed_days_ago is not None:
            db.add(
                Deployment(
                    workspace_id=workspace,
                    agent_id=agent.id,
                    status="succeeded",
                    started_at=datetime.now(UTC) - timedelta(days=deployed_days_ago),
                )
            )
        db.commit()
        return agent.id
    finally:
        db.close()


# (account, region) is UNIQUE, so each scratch workspace gets its own region.
_REGIONS = iter(
    ["us-east-2", "us-west-1", "eu-central-1", "ap-southeast-1", "eu-west-2", "sa-east-1"]
)


def _workspace(ws_id: str, status: str = "ready", tier: str = "dev") -> None:
    db = SessionLocal()
    try:
        db.add(
            Workspace(
                id=ws_id,
                name=ws_id,
                account_id="444455556677",
                region=next(_REGIONS),
                bootstrap_status=status,
                tier=tier,
                resources={},
            )
        )
        db.commit()
    finally:
        db.close()


# ── T37: the fleet view ──────────────────────────────────────────────────────────


def test_the_fleet_lists_every_workspace_with_its_counts():
    _reset()
    _workspace("staging-ws")
    _agent(name="a-one", workspace=DEFAULT_WORKSPACE_ID)
    _agent(name="a-two", workspace="staging-ws")
    _agent(name="a-bad", workspace="staging-ws", status="failed")
    db = SessionLocal()
    try:
        report = fleet_service.fleet_overview(db)
    finally:
        db.close()
    rows = {row["id"]: row for row in report["workspaces"]}
    assert rows[DEFAULT_WORKSPACE_ID]["agents_active"] == 1
    assert rows["staging-ws"]["agents_active"] == 1
    assert rows["staging-ws"]["agents_failed"] == 1
    assert report["totals"]["agents_active"] == 2
    assert report["totals"]["needs_attention"] == 1
    assert report["source"] == "ledger"


def test_an_unusable_workspace_says_so_instead_of_reporting_zeroes():
    """A `registered` or `failed` row must not read like a healthy empty environment."""
    _reset()
    _workspace("never-finished", status="registered")
    _workspace("broken-ws", status="failed")
    db = SessionLocal()
    try:
        rows = {row["id"]: row for row in fleet_service.fleet_overview(db)["workspaces"]}
    finally:
        db.close()
    for ws_id in ("never-finished", "broken-ws"):
        assert rows[ws_id]["readable"] is False
        assert rows[ws_id]["agents_active"] is None  # not 0
    assert rows[DEFAULT_WORKSPACE_ID]["readable"] is True


def test_the_fleet_counts_pending_releases_and_firing_alerts():
    _reset()
    agent_id = _agent(name="release-me")
    db = SessionLocal()
    try:
        bundle = ReleaseBundle(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name="release-me",
            method="harness", spec={}, artifact={}, digest="d" * 64,
        )
        db.add(bundle)
        db.flush()
        db.add(
            Promotion(
                workspace_id=DEFAULT_WORKSPACE_ID, bundle_id=bundle.id,
                target_workspace_id="staging-ws", status="pending",
                change_note="c", rollback_note="r",
            )
        )
        db.add(
            AlertRule(
                workspace_id=DEFAULT_WORKSPACE_ID, kind="error_rate", comparison="above",
                threshold=0.1, state="firing",
            )
        )
        db.commit()
        report = fleet_service.fleet_overview(db)
    finally:
        db.close()
    row = next(r for r in report["workspaces"] if r["id"] == DEFAULT_WORKSPACE_ID)
    assert row["promotions_pending"] == 1 and row["alerts_firing"] == 1
    assert report["totals"]["alerts_firing"] == 1


def test_the_fleet_is_admin_only(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as admin:
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        assert admin.get("/api/fleet").status_code == 200
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as member:
        assert member.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.commit()
        finally:
            db.close()
        member.post(
            "/api/auth/login",
            json={"username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"]},
        )
        assert member.get("/api/fleet").status_code == 403


# ── T39: governance health ───────────────────────────────────────────────────────


def test_a_clean_workspace_scores_full_marks():
    _reset()
    db = SessionLocal()
    try:
        health = fleet_service.governance_health(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    # no agents ⇒ nothing to find, including no "nobody is watching" finding
    assert health["score"] == 100 and health["grade"] == "good"
    assert health["findings"] == []


def test_every_deducted_point_has_a_finding_behind_it():
    _reset()
    _agent(spec={**RICH_SPEC, "guardrail": {"enabled": False}}, name="unguarded")
    db = SessionLocal()
    try:
        health = fleet_service.governance_health(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    keys = {finding["key"] for finding in health["findings"]}
    # unevaluated + no guardrail + nobody watching
    assert {"agents_unevaluated", "agents_without_guardrail", "no_alert_rules"} <= keys
    assert health["score"] < 100
    assert all(finding["to"] for finding in health["findings"])  # every finding is actionable
    severities = [finding["severity"] for finding in health["findings"]]
    assert severities == sorted(severities, key=lambda s: {"action": 0, "warn": 1, "info": 2}[s])


def test_a_firing_alert_is_the_top_finding():
    _reset()
    _agent(name="watched")
    db = SessionLocal()
    try:
        db.add(
            AlertRule(
                workspace_id=DEFAULT_WORKSPACE_ID, kind="error_rate", name="errors",
                comparison="above", threshold=0.1, state="firing",
            )
        )
        db.commit()
        health = fleet_service.governance_health(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    assert health["findings"][0]["key"] == "alerts_firing"
    assert health["findings"][0]["severity"] == "action"
    assert "no_alert_rules" not in {f["key"] for f in health["findings"]}


def test_a_stale_deployment_is_flagged():
    _reset()
    _agent(name="old-agent", deployed_days_ago=fleet_service.STALE_DEPLOY_DAYS + 10)
    _agent(name="fresh-agent", deployed_days_ago=1)
    db = SessionLocal()
    try:
        health = fleet_service.governance_health(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    stale = next(f for f in health["findings"] if f["key"] == "deployments_stale")
    assert stale["count"] == 1 and stale["sample"] == ["old-agent"]


def test_a_system_preset_is_not_held_against_the_workspace():
    _reset()
    db = SessionLocal()
    try:
        db.add(
            Agent(
                workspace_id=DEFAULT_WORKSPACE_ID, name="a-preset", method="harness",
                status="active", spec={"name": "a-preset"}, system_key="some-preset",
            )
        )
        db.commit()
        health = fleet_service.governance_health(db, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    assert health["agents_considered"] == 0
    assert health["findings"] == []


# ── T38: the marketplace ─────────────────────────────────────────────────────────


def test_publishing_strips_environment_ids_and_secrets(client):
    _reset()
    agent_id = _agent()
    created = client.post(
        "/api/marketplace/templates",
        json={"agent_id": agent_id, "title": "HR helper", "summary": "answers policy questions"},
    )
    assert created.status_code == 201, created.text
    entry = created.json()
    spec = entry["spec"]

    # the shape survives
    assert spec["system_prompt"] == RICH_SPEC["system_prompt"]
    assert spec["model_id"] == RICH_SPEC["model_id"]
    assert spec["guardrail"] == {"enabled": True, "mode": "anonymize"}
    assert spec["max_iterations"] == 12
    # nothing environment-bound or secret-shaped does
    for forbidden in marketplace_service.STRIPPED_KEYS:
        assert forbidden not in spec, forbidden
    assert "do-not-publish" not in str(entry)
    assert "KBDEVONLY1" not in str(entry)
    assert "tgt-dev-only" not in str(entry)


def test_what_a_consumer_must_supply_is_named_not_left_implicit(client):
    _reset()
    agent_id = _agent()
    entry = client.post(
        "/api/marketplace/templates",
        json={"agent_id": agent_id, "title": "HR helper"},
    ).json()
    kinds = {req["kind"] for req in entry["requirements"]}
    assert {"knowledge_base", "tool", "skill", "memory"} == kinds
    labels = {req["label"] for req in entry["requirements"]}
    assert "HR policies" in labels  # readable, not an opaque id
    assert "hr-database" in labels


def test_the_memory_id_is_a_requirement_not_a_carried_value(client):
    _reset()
    agent_id = _agent()
    entry = client.post(
        "/api/marketplace/templates", json={"agent_id": agent_id, "title": "t"}
    ).json()
    assert entry["spec"]["memory"]["long_term"] is True
    # the shape travels; the dev environment's resource id does not
    assert "mem-dev-only" not in str(entry["spec"])
    assert any(req["label"] == "mem-dev-only" for req in entry["requirements"])


def test_republishing_updates_rather_than_duplicating(client):
    _reset()
    agent_id = _agent()
    first = client.post(
        "/api/marketplace/templates", json={"agent_id": agent_id, "title": "v1"}
    ).json()
    second = client.post(
        "/api/marketplace/templates", json={"agent_id": agent_id, "title": "v2"}
    ).json()
    assert first["id"] == second["id"] and second["title"] == "v2"
    assert len(client.get("/api/marketplace/templates").json()["templates"]) == 1


def test_a_draft_agent_and_a_preset_cannot_be_published(client):
    _reset()
    draft = _agent(status="draft", name="draft-agent")
    refused = client.post("/api/marketplace/templates", json={"agent_id": draft, "title": "t"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "marketplace.agent_not_active"

    db = SessionLocal()
    try:
        preset = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="preset-agent", method="harness",
            status="active", spec={"name": "preset-agent"}, system_key="k",
        )
        db.add(preset)
        db.commit()
        preset_id = preset.id
    finally:
        db.close()
    refused = client.post("/api/marketplace/templates", json={"agent_id": preset_id, "title": "t"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "marketplace.system_managed"


def test_using_a_template_counts_but_creates_nothing(client):
    """A template hands over defaults; the consumer still goes through the wizard."""
    _reset()
    agent_id = _agent()
    entry = client.post(
        "/api/marketplace/templates", json={"agent_id": agent_id, "title": "t"}
    ).json()
    before = len(client.get("/api/agents").json()["agents"])
    used = client.post(f"/api/marketplace/templates/{entry['id']}/use")
    assert used.status_code == 200 and used.json()["uses"] == 1
    assert len(client.get("/api/agents").json()["agents"]) == before


def test_templates_are_visible_across_workspaces(client):
    """Cross-workspace reads are the whole point; `own` says who may withdraw it."""
    _reset()
    _workspace("other-ws")
    db = SessionLocal()
    try:
        db.add(
            SharedTemplate(
                source_workspace_id="other-ws", agent_id="x", title="from elsewhere",
                summary="", method="harness", spec={}, requirements=[],
            )
        )
        db.commit()
    finally:
        db.close()
    rows = client.get("/api/marketplace/templates").json()["templates"]
    foreign = next(row for row in rows if row["title"] == "from elsewhere")
    assert foreign["own"] is False
    assert foreign["source_workspace_id"] == "other-ws"


def test_only_the_publisher_withdraws_its_template(gated_app):
    _reset()
    db = SessionLocal()
    try:
        row = SharedTemplate(
            source_workspace_id="someone-else", agent_id="x", title="theirs", summary="",
            method="harness", spec={}, requirements=[],
        )
        db.add(row)
        db.commit()
        template_id = row.id
    finally:
        db.close()
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as member:
        member.post("/api/auth/register", json=MEMBER_CREDS)
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.commit()
        finally:
            db.close()
        member.post(
            "/api/auth/login",
            json={"username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"]},
        )
        refused = member.delete(f"/api/marketplace/templates/{template_id}")
        assert refused.status_code == 403
        assert refused.json()["code"] == "marketplace.not_yours"

    with TestClient(gated_app, client=("127.0.0.1", 4321)) as admin:
        admin.post("/api/auth/login", json=ADMIN_CREDS)
        assert admin.delete(f"/api/marketplace/templates/{template_id}").status_code == 200


def test_an_unknown_template_is_404(client):
    assert client.post("/api/marketplace/templates/nope/use").status_code == 404
    assert client.delete("/api/marketplace/templates/nope").status_code == 404


def test_the_shared_template_table_is_not_workspace_scoped():
    """Scoping it would defeat the point: another workspace could never see it."""
    from app.core.db import WORKSPACE_SCOPED_TABLES

    assert "shared_templates" not in WORKSPACE_SCOPED_TABLES


def test_publishable_spec_keeps_only_the_allow_list():
    spec = {key: "x" for key in marketplace_service.KEPT_KEYS}
    spec.update({key: "secret" for key in marketplace_service.STRIPPED_KEYS})
    spec["something_new"] = "also dropped"
    published = marketplace_service.publishable_spec(spec)
    assert set(published) == set(marketplace_service.KEPT_KEYS)


def test_the_publish_cap_is_enforced(client, monkeypatch):
    _reset()
    monkeypatch.setattr(marketplace_service, "MAX_PER_WORKSPACE", 1)
    first = _agent(name="one")
    second = _agent(name="two")
    assert client.post(
        "/api/marketplace/templates", json={"agent_id": first, "title": "a"}
    ).status_code == 201
    refused = client.post("/api/marketplace/templates", json={"agent_id": second, "title": "b"})
    assert refused.status_code == 409 and refused.json()["code"] == "marketplace.too_many"


def test_health_is_reachable_per_workspace(client):
    _reset()
    body = client.get("/api/governance/health").json()
    assert body["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert "score" in body and "findings" in body


def test_the_marketplace_row_records_its_source_agent(client):
    _reset()
    agent_id = _agent(name="traceable")
    entry = client.post(
        "/api/marketplace/templates", json={"agent_id": agent_id, "title": "t"}
    ).json()
    assert entry["source_agent_name"] == "traceable"
    db = SessionLocal()
    try:
        [row] = db.scalars(select(SharedTemplate)).all()
        assert row.agent_id == agent_id
    finally:
        db.close()
