"""GET /api/agents/{id}/telemetry — the detail page's telemetry status (projects §17.3-4).

Hermetic: the span scan (`log_streams.service_has_spans`) and the service-name
resolver are stubbed. Asserted: the narrowest window with a span is reported, a
BYOC code zip without ADOT that wrote nothing names that cause, an inconclusive scan
reads "unknown", the answer is cached per agent version (``refresh`` probes again),
and a row of another workspace or project answers 404 without probing.
"""

import pytest

import app.routers.agent_telemetry as telemetry
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.core.route_policy import PERM_PROJECT_USE, required_role
from app.models.ledger import Agent
from tests.conftest import TEST_PROJECT
from tests.test_project_scope import _project

DEFAULT = {"X-Project": TEST_PROJECT}
HOUR_MS = 3_600_000


def _seed(
    *,
    method: str = "zip_runtime",
    resource_id: str | None = "rt-1",
    spec: dict | None = None,
    version: str = "2",
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    project: str | None = TEST_PROJECT,
) -> str:
    db = SessionLocal()
    try:
        with project_context(project):
            row = Agent(workspace_id=workspace_id, name=f"a-{method}", method=method,
                        status="active", resource_id=resource_id, version=version,
                        spec=spec or {})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


@pytest.fixture
def probe(monkeypatch):
    """Scripted scan: ``answers[label]`` per window (default False); records calls."""
    telemetry.reset_cache_for_tests()
    state: dict = {"answers": {}, "calls": [], "resolved": 0, "resolve_error": None}
    monkeypatch.setattr(telemetry, "_logs_client", lambda _ws: object())

    def resolve(agent, _ws, _logs, qualifier=None):
        state["resolved"] += 1
        if state["resolve_error"]:
            raise state["resolve_error"]
        return f"{agent.name}.{qualifier}", f"/aws/x-{qualifier}"

    def has_spans(_logs, service_name, *, since_ms):
        import time

        hours = round((time.time() * 1000 - since_ms) / HOUR_MS)
        label = {1: "1h", 24: "24h", 168: "7d", 336: "14d"}[hours]
        state["calls"].append((service_name, label))
        return state["answers"].get(label, False)

    monkeypatch.setattr(telemetry, "resolve_telemetry", resolve)
    monkeypatch.setattr(telemetry.log_streams, "service_has_spans", has_spans)
    yield state
    telemetry.reset_cache_for_tests()


def test_route_is_classified_project_use():
    assert required_role("GET", "/api/agents/{agent_id}/telemetry") == PERM_PROJECT_USE


def test_reports_the_narrowest_window_with_a_span(client, probe):
    probe["answers"] = {"7d": True, "14d": True}
    agent_id = _seed()
    res = client.get(f"/api/agents/{agent_id}/telemetry", headers=DEFAULT)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "receiving"
    assert body["last_span_within"] == "7d"
    assert body["service_name"] == "a-zip_runtime.DEFAULT"
    assert body["endpoint"] == "DEFAULT"
    assert body["cached"] is False
    assert [label for _, label in probe["calls"]] == ["1h", "24h", "7d"]


def test_never_received_names_the_byoc_code_zip_cause(client, probe):
    agent_id = _seed(method="byoc", spec={"byoc": {"artifact_kind": "code_zip"}})
    body = client.get(f"/api/agents/{agent_id}/telemetry", headers=DEFAULT).json()
    assert body["status"] == "none"
    assert body["last_span_within"] is None
    assert body["byoc_no_adot"] is True
    assert body["reason"] == "byoc_code_zip_no_adot"
    assert body["lookback_days"] == 14


def test_a_code_zip_with_the_launcher_is_not_blamed(client, probe):
    spec = {"byoc": {"artifact_kind": "code_zip", "provenance": {"adot_launcher": True}}}
    body = client.get(f"/api/agents/{_seed(method='byoc', spec=spec)}/telemetry",
                      headers=DEFAULT).json()
    assert body["status"] == "none"
    assert body["byoc_no_adot"] is False
    assert body["reason"] is None


def test_an_inconclusive_scan_is_unknown_not_none(client, probe):
    probe["answers"] = {"1h": None, "24h": None, "7d": False, "14d": False}
    body = client.get(f"/api/agents/{_seed()}/telemetry", headers=DEFAULT).json()
    assert body["status"] == "unknown"
    assert body["reason"] == "probe_failed"


def test_a_harness_without_a_log_group_has_never_sent_telemetry(client, probe):
    probe["resolve_error"] = AppError("eval.harness_no_telemetry", "none yet", status_code=400)
    body = client.get(f"/api/agents/{_seed(method='harness')}/telemetry", headers=DEFAULT).json()
    assert body["status"] == "none"
    assert body["reason"] == "no_log_group"
    assert probe["calls"] == []


def test_an_undeployed_agent_is_not_probed(client, probe):
    body = client.get(f"/api/agents/{_seed(resource_id=None)}/telemetry", headers=DEFAULT).json()
    assert body["status"] == "not_deployed"
    assert probe["resolved"] == 0 and probe["calls"] == []


def test_an_imported_runtime_resolves_its_runtime_name(client, probe, monkeypatch):
    monkeypatch.setattr(telemetry, "control_client", lambda _ws: object())
    monkeypatch.setattr(telemetry.rt, "get_runtime",
                        lambda _c, rid: {"agentRuntimeName": f"name_{rid}"})
    probe["answers"] = {"1h": True}
    body = client.get(f"/api/agents/{_seed(method='discovered_runtime')}/telemetry",
                      headers=DEFAULT).json()
    assert body["status"] == "receiving"
    assert body["service_name"] == "name_rt-1.DEFAULT"
    assert probe["resolved"] == 0  # not the evaluation resolver (method unsupported there)


def test_an_imported_harness_is_unsupported(client, probe):
    spec = {"discovery": {"resource_type": "harness"}}
    body = client.get(f"/api/agents/{_seed(method='discovered_runtime', spec=spec)}/telemetry",
                      headers=DEFAULT).json()
    assert body["status"] == "unsupported"


def test_cached_per_version_and_refresh_probes_again(client, probe):
    agent_id = _seed()
    first = client.get(f"/api/agents/{agent_id}/telemetry", headers=DEFAULT).json()
    second = client.get(f"/api/agents/{agent_id}/telemetry", headers=DEFAULT).json()
    assert first["cached"] is False and second["cached"] is True
    assert probe["resolved"] == 1
    client.get(f"/api/agents/{agent_id}/telemetry?refresh=true", headers=DEFAULT)
    assert probe["resolved"] == 2
    # a new deploy (version) is a new key
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            db.get(Agent, agent_id).version = "3"
            db.commit()
    finally:
        db.close()
    res = client.get(f"/api/agents/{agent_id}/telemetry", headers=DEFAULT)
    assert res.json()["cached"] is False
    assert probe["resolved"] == 3


def test_another_workspace_or_project_answers_404_without_probing(client, probe):
    foreign = _seed(workspace_id="acct-other")
    res = client.get(f"/api/agents/{foreign}/telemetry", headers=DEFAULT)
    assert res.status_code == 404 and res.json()["code"] == "agent.not_found"
    _project("alpha")
    theirs = _seed(project="alpha")
    res = client.get(f"/api/agents/{theirs}/telemetry", headers=DEFAULT)
    assert res.status_code == 404 and res.json()["code"] == "agent.not_found"
    res = client.get(f"/api/agents/{theirs}/telemetry", headers={"X-Project": "alpha"})
    assert res.status_code == 200
    assert client.get("/api/agents/nope/telemetry", headers=DEFAULT).status_code == 404
    assert probe["resolved"] == 1  # only the alpha read probed


def _profile(*, status: str = "ok", check_results: list | None = None) -> str:
    import uuid

    from app.models.network import NetworkProfile

    db = SessionLocal()
    try:
        row = NetworkProfile(id=f"np-{uuid.uuid4().hex[:8]}", workspace_id=DEFAULT_WORKSPACE_ID,
                             name="vpc-a", status=status, check_results=check_results)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_a_silent_vpc_agent_names_its_missing_telemetry_endpoint(client, probe):
    pid = _profile(check_results=[{"check": "endpoint.logs", "severity": "error"},
                                  {"check": "endpoint.xray", "severity": "ok"}])
    agent_id = _seed(spec={"network": {"profile_id": pid}})
    body = client.get(f"/api/agents/{agent_id}/telemetry", headers=DEFAULT).json()
    assert body["status"] == "none"
    assert body["hint"]["code"] == "network.telemetry_endpoint_missing"
    assert body["hint"]["missing"] == ["logs"]
    assert "vpc-a" in body["hint"]["message"]
    # the hint is read outside the cache too
    again = client.get(f"/api/agents/{agent_id}/telemetry", headers=DEFAULT).json()
    assert again["cached"] is True
    assert again["hint"]["code"] == "network.telemetry_endpoint_missing"


def test_no_hint_while_spans_arrive_or_for_public_agents(client, probe):
    pid = _profile(status="unchecked")
    probe["answers"] = {"1h": True}
    body = client.get(f"/api/agents/{_seed(spec={'network': {'profile_id': pid}})}/telemetry",
                      headers=DEFAULT).json()
    assert body["status"] == "receiving"
    assert body["hint"] is None
    probe["answers"] = {}
    public = client.get(f"/api/agents/{_seed(version='3')}/telemetry", headers=DEFAULT).json()
    assert public["status"] == "none"
    assert public["hint"] is None
