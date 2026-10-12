"""T04 — human display names on top of the immutable slug (hermetic, no AWS)."""

import pytest
from pydantic import ValidationError

import app.routers.agents as agents_router
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec, display_name_of

BASE = {"name": "display-agent", "method": "harness", "system_prompt": "Be brief."}


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)


def _activate(agent_id: str) -> None:
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status = "active"
    agent.resource_id = "harness-xyz"
    agent.arn = "arn:aws:bedrock-agentcore:us-west-2:111:harness/xyz"
    db.commit()
    db.close()


# --- schema ---------------------------------------------------------------


def test_display_name_is_optional_and_defaults_to_none():
    assert AgentSpec(**BASE).display_name is None


def test_display_name_accepts_unicode_and_is_trimmed():
    spec = AgentSpec(**BASE, display_name="  客服助手 Pro  ")
    assert spec.display_name == "客服助手 Pro"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_display_name_reads_back_as_none(blank):
    assert AgentSpec(**BASE, display_name=blank).display_name is None


def test_display_name_max_64_chars():
    assert AgentSpec(**BASE, display_name="字" * 64).display_name == "字" * 64
    with pytest.raises(ValidationError):
        AgentSpec(**BASE, display_name="字" * 65)


def test_slug_rules_are_unchanged_by_display_name():
    with pytest.raises(ValidationError):
        AgentSpec(**{**BASE, "name": "客服助手"}, display_name="客服助手")


def test_display_name_of_tolerates_legacy_and_odd_specs():
    assert display_name_of(None) is None
    assert display_name_of({}) is None
    assert display_name_of({"display_name": "  "}) is None
    assert display_name_of({"display_name": 3}) is None
    assert display_name_of({"display_name": "销售"}) == "销售"


# --- API round-trip -------------------------------------------------------


def test_display_name_round_trips_create_list_detail_and_redeploy(client):
    res = client.post("/api/agents", json={**BASE, "display_name": " 客服助手 "})
    assert res.status_code == 202, res.text
    agent = res.json()["agent"]
    assert agent["name"] == "display-agent"
    assert agent["display_name"] == "客服助手"
    assert agent["spec"]["display_name"] == "客服助手"

    detail = client.get(f"/api/agents/{agent['id']}").json()
    assert detail["display_name"] == "客服助手"
    listed = next(a for a in client.get("/api/agents").json()["agents"]
                  if a["id"] == agent["id"])
    assert listed["display_name"] == "客服助手"

    # Unlike the slug, the display name is editable on redeploy.
    _activate(agent["id"])
    res = client.post(f"/api/agents/{agent['id']}/redeploy",
                      json={**BASE, "display_name": "销售助手"})
    assert res.status_code == 202, res.text
    assert client.get(f"/api/agents/{agent['id']}").json()["display_name"] == "销售助手"

    # ...and clearable: the console then falls back to the slug.
    _activate(agent["id"])
    res = client.post(f"/api/agents/{agent['id']}/redeploy", json=BASE)
    assert res.status_code == 202, res.text
    assert client.get(f"/api/agents/{agent['id']}").json()["display_name"] is None


def test_agent_without_display_name_projects_none(client):
    agent = client.post("/api/agents", json=BASE).json()["agent"]
    assert agent["display_name"] is None


def test_too_long_display_name_is_422(client):
    res = client.post("/api/agents", json={**BASE, "display_name": "x" * 65})
    assert res.status_code == 422


def test_public_v1_list_surfaces_display_name(client):
    agent = client.post("/api/agents", json={**BASE, "display_name": "客服"}).json()["agent"]
    _activate(agent["id"])
    key = client.post("/api/apikeys", json={"name": "dn-key"}).json()["key"]
    body = client.get("/v1/agents", headers={"X-Api-Key": key}).json()
    row = next(a for a in body["agents"] if a["id"] == agent["id"])
    assert row["name"] == "display-agent"
    assert row["display_name"] == "客服"
