"""W12-BE: the backend hooks the B12r nodes left open — cost tags at create (CT-1),
the back-fill routes, the budget basis in the budget rule (CT-2), the SB-3 save gate
and Code Interpreter grant, the evaluation cost enforcement (EC) and the console
chat's approval field (W11-FE)."""

from types import SimpleNamespace

import pytest

import app.services.chat as chat_service
from app.core.config import Settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.dlc import cost
from app.evaluation import online, run_cost
from app.evaluation.models import EvalRun
from app.models.ledger import Agent, Job, Workspace
from app.models.projects import Project
from app.schemas.agent import AgentSpec
from app.services import (
    agent_iam,
    alerts,
    billing_costs,
    cost_tags,
    invoke,
    project_iam,
    project_quotas,
)
from app.services import costs as cost_service
from tests.conftest import TEST_PROJECT
from tests.test_chat_api import delta_events, make_active_agent
from tests.test_project_iam import StubIam, _agent_row, _probe_spec, _settings, _workspace
from tests.test_strands_builder_sb3 import RISKY, _tool

WS = DEFAULT_WORKSPACE_ID


# ── CT-1: tags at create ──────────────────────────────────────────────────────


def test_a_shared_projects_execution_role_carries_the_agents_cost_tags():
    iam = StubIam()
    agent = _agent_row("default")
    agent_iam.provision_execution_role(agent, _probe_spec(), _settings(), _workspace(iam))
    role = iam.roles[agent_iam.role_name_for("probe", "abcdef1234")]
    tags = {t["Key"]: t["Value"] for t in role["tags"]}
    assert tags["launchpad:project"] == "default"
    assert tags["launchpad:agent-id"] == "abcdef1234"
    assert tags["launchpad:workspace"] == WS and tags["launchpad:managed-by"] == "launchpad"


def test_a_role_that_cannot_be_tagged_still_provisions():
    class NoTags(StubIam):
        def tag_role(self, RoleName, Tags):
            raise RuntimeError("denied")

    logs: list[str] = []
    arn, _detail = agent_iam.provision_execution_role(
        _agent_row("default"), _probe_spec(), _settings(), _workspace(NoTags()), logs.append)
    assert arn.endswith(agent_iam.role_name_for("probe", "abcdef1234"))
    assert any("cost tags on" in line and "failed" in line for line in logs)


def test_the_deployers_pass_the_agents_tags_on_create_and_update():
    import inspect

    from app.deployer import byoc, container, zip_runtime

    for module in (zip_runtime, container, byoc):
        source = inspect.getsource(module)
        assert "cost_tags.agent_tags(row, row.workspace_id)" in source, module.__name__


def test_the_shared_gateway_and_for_user_invokes_are_in_the_boundary():
    doc = project_iam.boundary_policy_document(_boundary_inputs())
    gateways = next(s for s in doc["Statement"] if s["Sid"] == "Gateways")
    values = next(iter(gateways["Condition"].values()))
    assert "shared" in next(iter(values.values()))
    tagged = next(s for s in doc["Statement"] if s["Sid"] == "ProjectTagged")
    assert "bedrock-agentcore:InvokeAgentRuntimeForUser" in tagged["Action"]


def _boundary_inputs():
    from tests.test_project_iam import _inputs

    return _inputs()


# ── CT-1: the back-fill routes and the retag start ───────────────────────────


def test_the_backfill_routes_start_and_show_a_run(client, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(cost_tags, "start_backfill_async", started.append)
    assert client.get("/api/cost-tags/backfill").json()["run"] is None
    res = client.post("/api/cost-tags/backfill")
    assert res.status_code == 200, res.text
    run = res.json()
    assert run["status"] == "queued" and started == [run["id"]]
    assert run["reason"] == "admin" and [s["name"] for s in run["stages"]] == ["plan", "tag"]
    view = client.get("/api/cost-tags/backfill").json()
    assert view["run"]["id"] == run["id"] and "launchpad:project" in view["tag_keys"]
    # an identical queued run is reused, not doubled
    assert client.post("/api/cost-tags/backfill").json()["id"] == run["id"]


def test_a_rename_starts_its_retag_after_the_commit(client, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(cost_tags, "start_backfill_async", started.append)
    res = client.patch(f"/api/projects/{TEST_PROJECT}", json={"name": "Test renamed"})
    assert res.status_code == 200, res.text
    db = SessionLocal()
    try:
        with project_context(None):
            jobs = db.query(Job).filter(Job.type == cost_tags.JOB_TYPE).all()
    finally:
        db.close()
    assert [j.id for j in jobs] == started and len(started) == 1


def test_a_custom_tag_change_starts_its_retag(client, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(cost_tags, "start_backfill_async", started.append)
    res = client.put(f"/api/projects/{TEST_PROJECT}/cost-tags",
                     json={"cost_tags": {"CostCenter": "1234"}})
    assert res.status_code == 200, res.text
    assert len(started) == 1
    # unchanged: nothing to re-tag
    client.put(f"/api/projects/{TEST_PROJECT}/cost-tags",
               json={"cost_tags": {"CostCenter": "1234"}})
    assert len(started) == 1


# ── CT-2: the budget rule reads the project's basis ──────────────────────────


def test_the_budget_rule_reads_billed_cost_when_the_project_chose_it(monkeypatch):
    monkeypatch.setattr(cost_service, "month_to_date_usd", lambda *a, **k: 1.0)
    monkeypatch.setattr(billing_costs, "project_month_to_date_usd", lambda *a, **k: 7.5)
    from tests.conftest import ws_ctx

    db = SessionLocal()
    try:
        ctx = ws_ctx(id=WS)
        assert alerts._project_cost_value(db, ctx, TEST_PROJECT) == (
            1.0, "month-to-date estimate from the price map")
        with project_context(None):
            row = db.get(Project, TEST_PROJECT)
            admin = SimpleNamespace(username="admin", role="admin")
            project_quotas.update_budget_basis(db, admin, row, "billing")
            db.commit()
        usd, detail = alerts._project_cost_value(db, ctx, TEST_PROJECT)
        assert usd == 7.5 and detail.startswith("month-to-date billed cost")
    finally:
        db.close()


# ── SB-3: the save gate and the Code Interpreter grant ───────────────────────


def test_sandbox_tools_get_the_code_interpreter_grant():
    from tests.test_agent_iam_policy import CTX

    plain = AgentSpec(name="probe", method="zip_runtime", system_prompt="p")
    sandboxed = AgentSpec(name="probe", method="zip_runtime", system_prompt="p",
                          sandbox_tools=["python_repl"])
    sids = lambda spec: {s["Sid"] for s in agent_iam.policy_document(spec, CTX)["Statement"]}  # noqa: E731
    assert "AgentCoreCodeInterpreter" not in sids(plain)
    assert "AgentCoreCodeInterpreter" in sids(sandboxed)


def test_saving_an_unapproved_custom_tool_is_refused(client):
    res = client.post("/api/agents", json={
        "name": "w12-risky", "method": "zip_runtime", "system_prompt": "p",
        "custom_tools": [_tool(RISKY, "disk_usage")]})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "custom_tool.approval_required"
    res = client.post("/api/agents", json={
        "name": "w12-sandbox", "method": "zip_runtime", "system_prompt": "p",
        "sandbox_tools": ["shell"]})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "custom_tool.sandbox_tool_not_allowed"


# ── EC: the measured total, the run payload, passive and online limits ───────


def test_a_final_run_counts_its_measured_judges():
    # the agent traffic is in the price-map estimate already: a run adds its judges
    run = SimpleNamespace(status="completed", cost_estimate={"judge_usd": 9.0},
                          cost_actual={"final": True, "agent_usd": 1.0, "judge_usd": 0.5,
                                       "managed_evaluations_usd": 0.1, "total_usd": 1.6})
    assert project_quotas.run_judge_usd(run) == pytest.approx(0.6)
    live = SimpleNamespace(status="running", cost_estimate={"judge_usd": 2.0, "managed_usd": 1.0},
                           cost_actual={"final": False, "live": {"total_usd": 0.4}})
    assert project_quotas.run_judge_usd(live) == 3.0
    failed = SimpleNamespace(status="failed", cost_estimate={"judge_usd": 2.0}, cost_actual={})
    assert project_quotas.run_judge_usd(failed) == 0.0


def test_the_run_payload_carries_its_cost_summary():
    from app.evaluation.routers import _run_out

    run = EvalRun(id="r1", workspace_id=WS, agent_name="a", status="completed",
                  mode="evaluators", cost_actual={"final": True, "total_usd": 0.42,
                                                  "estimate_total_usd": 0.5})
    assert _run_out(run)["cost"] == run_cost.summary(run) == {
        "usd": 0.42, "kind": "actual", "estimate_usd": 0.5}


@pytest.fixture
def priced(monkeypatch):
    """Every judged session costs something, and any cost asks for a confirmation."""
    monkeypatch.setattr(cost, "window_sessions", lambda *a, hours, **k: 500)
    db = SessionLocal()
    try:
        db.get(Workspace, WS).release_policy = {"eval_cost_confirm_usd": 0.0}
        db.commit()
    finally:
        db.close()
    yield


def test_a_passive_run_over_the_threshold_asks_for_confirmation(client, priced):
    agent_id = make_active_agent(name="w12-passive")
    res = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "evaluators": ["Builtin.Helpfulness"], "lookback_hours": 24})
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "run.cost_confirm_required"
    assert body["detail"]["estimate"]["scope"] == "passive"
    assert body["detail"]["estimate"]["sessions"] == 500


def test_an_online_config_over_the_threshold_asks_for_confirmation(client, priced,
                                                                   monkeypatch):
    agent_id = make_active_agent(name="w12-online")
    created: list = []
    monkeypatch.setattr(online, "create_config",
                        lambda *a, **k: created.append(k) or {"config_id": "oe-1"})
    body = {"agent_id": agent_id, "evaluators": ["Builtin.Helpfulness"],
            "sampling_percentage": 100}
    res = client.post("/api/eval/online", json=body)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["estimate"]["period"] == "day"
    assert created == []
    res = client.post("/api/eval/online", json={**body, "confirm_cost": True})
    assert res.status_code == 201, res.text
    assert created and created[0]["sampling_percentage"] == 100


def test_eval_cost_prices_is_a_setting():
    assert Settings.model_fields["eval_cost_prices"].default == {}
    assert cost.eval_prices()["runtime_vcpu"] == cost.DEFAULT_EVAL_PRICES["runtime_vcpu"]


# ── W11-FE: the console chat's approval field ────────────────────────────────


def test_the_console_chats_tool_approvals_field_is_accepted(client, monkeypatch):
    seen: list = []

    def fake_events(agent, prompt, **_kw):
        seen.append(invoke.CONSOLE_TOOL_APPROVALS.get())
        return delta_events("ok")

    monkeypatch.setattr(chat_service, "invoke_agent_events", fake_events)
    agent_id = make_active_agent(name="w12-approve")
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        row.spec = {"name": "w12-approve", "tool_hooks": {"approval_tools": ["send_email"]}}
        db.commit()
    finally:
        db.close()
    res = client.post(f"/api/chat/{agent_id}", json={
        "prompt": "Approved — go ahead with send_email.",
        "tool_approvals": {"approved": ["send_email"]}})
    assert res.status_code == 200, res.text
    assert seen == [{"interactive": True, "approved": ["send_email"]}]
    assert client.post(f"/api/chat/{agent_id}", json={
        "prompt": "x", "tool_approvals": {"approved": ["t"] * 51}}).status_code == 422
