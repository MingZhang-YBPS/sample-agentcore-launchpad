"""strands-builder SB-2 wiring (W11-BE): the model provider's Connection in the IAM
grant, the workload token and the save-time check; no inference profile for a direct
provider; approval events from the runtime, `tool_approvals` sent by console turns
only and the named /v1 refusal; the chat ledger keeping a turn's `structured` answer
and its approval card; agents as a share kind, and an isolated grantee's boundary
invoking a `use`-shared agent."""

# ruff: noqa: F811 — the `app` / `world` fixtures are imported, then requested by name

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.services.chat as chat_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.deployer import model_profile
from app.deployer.environment import ENV_WORKLOAD, runtime_environment
from app.models.ledger import Agent, ChatMessage
from app.routers import agents as agents_router
from app.schemas.agent import AgentSpec
from app.services import agent_iam, chat_ledger, invoke, project_iam, resource_shares
from app.services.agentcore import runtime
from app.services.agentcore.runtime import _invoke_payload, _runtime_payload_events
from app.templates import gateway_support
from tests.test_agent_iam_policy import CTX
from tests.test_chat_api import delta_events, make_active_agent
from tests.test_project_scope import app  # noqa: F401
from tests.test_resource_shares import _share, world  # noqa: F401

CLAUDE = "claude-sonnet-4-5"
PROVIDER = {"kind": "anthropic", "connection": "anthropic-key"}
WS = DEFAULT_WORKSPACE_ID


def _spec(**over) -> AgentSpec:
    base = {"name": "sb2-wire", "method": "zip_runtime", "system_prompt": "p",
            "model_id": CLAUDE}
    return AgentSpec(**{**base, **over})


# ── the model provider's Connection ─────────────────────────────────────────────


def test_the_provider_connection_is_granted_like_an_api_key_tool():
    assert agent_iam._tool_auth_statements(_spec(), CTX) == []
    statements = {s["Sid"]: s for s in agent_iam._tool_auth_statements(
        _spec(model_provider=PROVIDER), CTX)}
    assert any(r.endswith("apikeycredentialprovider/anthropic-key")
               for r in statements["ToolAuthApiKey"]["Resource"])
    assert any("/apikey/anthropic-key-" in r
               for r in statements["ToolAuthVaultSecrets"]["Resource"])


def test_the_provider_needs_the_workload_token():
    assert gateway_support.runtime_user_id({"tools": []}, "ann") is None
    assert gateway_support.runtime_user_id({"model_provider": PROVIDER}, "ann") == "ann"
    env = runtime_environment(_spec(model_provider=PROVIDER), {}, workload_name="wl-1")
    assert env[ENV_WORKLOAD] == "wl-1"
    assert ENV_WORKLOAD not in runtime_environment(_spec(), {}, workload_name="wl-1")


def test_a_direct_provider_gets_no_inference_profile():
    bedrock = _spec(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert model_profile.profiled_model_ids(bedrock) != []
    assert model_profile.profiled_model_ids(_spec(model_provider=PROVIDER)) == []


def test_save_time_check_of_the_provider_connection(monkeypatch):
    names = {"api_key": {"anthropic-key"}, "oauth2": {"github"}}
    monkeypatch.setattr(agents_router.identity_providers, "provider_names",
                        lambda control, kind: names[kind])
    agents_router._validate_model_provider(object(), _spec(model_provider=PROVIDER))
    for connection, code in (("github", "identity.kind_mismatch"),
                             ("missing", "identity.connection_unknown")):
        spec = _spec(model_provider={**PROVIDER, "connection": connection})
        with pytest.raises(AppError) as err:
            agents_router._validate_model_provider(object(), spec)
        assert err.value.code == code and err.value.status_code == 422


# ── approval events and the console-only payload ────────────────────────────────


def test_the_runtime_approval_events_are_recognised():
    payload = {"event": "approval_required", "tool": "send_email", "tool_use_id": "t1",
               "input": {"to": "***"}}
    assert list(_runtime_payload_events(payload)) == [{"event": "approval_required", "data": {
        "tool": "send_email", "tool_use_id": "t1", "input": {"to": "***"}}}]
    refused = list(_runtime_payload_events({"event": "approval_refused", "tool": "x",
                                            "input": "not-a-dict"}))
    assert refused[0]["data"] == {"tool": "x", "tool_use_id": "", "input": {}}


def test_the_payload_carries_tool_approvals_only_when_given():
    assert "tool_approvals" not in json.loads(_invoke_payload("hi", "a"))
    body = json.loads(_invoke_payload("hi", "a", tool_approvals={
        "interactive": True, "approved": ["send_email", ""]}))
    assert body["tool_approvals"] == {"interactive": True, "approved": ["send_email"]}


def test_the_text_invoke_collects_approval_notices():
    stream = (b'data: {"event": "approval_required", "tool": "send_email", "input": {}}\n\n'
              b'data: {"event": "complete", "result": "Waiting for approval."}\n\n')
    client = MagicMock()
    client.invoke_agent_runtime.return_value = {
        "response": SimpleNamespace(iter_chunks=lambda: iter([stream]), read=lambda: stream),
        "contentType": "text/event-stream",
    }
    result = runtime.invoke_runtime_text(client, "arn", "hi",
                                         tool_approvals={"interactive": True, "approved": []})
    assert result["approval_required"][0]["tool"] == "send_email"
    sent = json.loads(client.invoke_agent_runtime.call_args.kwargs["payload"])
    assert sent["tool_approvals"] == {"interactive": True, "approved": []}


def _agent(spec: dict | None = None):
    return SimpleNamespace(id="a1", name="a", method="zip_runtime", arn="arn:rt",
                           spec=spec or {}, workspace_id="default", endpoint_mode=None,
                           inbound_auth_config=None)


def _direct(monkeypatch, answer: dict) -> list[dict]:
    calls: list[dict] = []

    def fake_text(client, arn, prompt, **kwargs):
        calls.append(kwargs)
        return dict(answer)

    monkeypatch.setattr(invoke, "require_invoke_capability", lambda agent: None)
    monkeypatch.setattr(invoke, "refuse_assistant_session", lambda *a: None)
    monkeypatch.setattr(invoke, "_agent_workspace", lambda agent, ws: SimpleNamespace())
    monkeypatch.setattr(invoke, "data_client", lambda ws: object())
    monkeypatch.setattr(invoke.canary_service, "active_canary_route", lambda agent_id: None)
    monkeypatch.setattr(invoke.inbound_auth_service, "is_jwt_mode", lambda agent: False)
    monkeypatch.setattr(invoke.answer_rules, "match_for_agent", lambda agent, prompt: None)
    monkeypatch.setattr(invoke, "_pending_force_reauth", lambda *a: [])
    monkeypatch.setattr(invoke.rt, "invoke_runtime_text", fake_text)
    return calls


def test_console_turns_send_approvals_and_v1_never_does(monkeypatch):
    calls = _direct(monkeypatch, {"text": "ok", "session_id": "s"})
    token = invoke.CONSOLE_TOOL_APPROVALS.set(invoke.console_tool_approvals(["send_email"]))
    try:
        invoke.invoke_agent_text(_agent(), "hi")
        invoke.invoke_agent_text(_agent(), "hi", console_user=False)
        invoke.invoke_agent_text(_agent(), "hi", console_user=False,
                                 tool_approvals={"interactive": True, "approved": ["x"]})
    finally:
        invoke.CONSOLE_TOOL_APPROVALS.reset(token)
    invoke.invoke_agent_text(_agent(), "hi")  # outside a console turn: nothing
    assert calls[0]["tool_approvals"] == {"interactive": True, "approved": ["send_email"]}
    assert all("tool_approvals" not in c for c in calls[1:])


def test_v1_gets_the_named_refusal(monkeypatch):
    _direct(monkeypatch, {"text": "", "session_id": "s",
                          "approval_refused": [{"tool": "send_email"}]})
    with pytest.raises(AppError) as err:
        invoke.invoke_agent_text(_agent(), "hi", console_user=False)
    assert err.value.code == invoke.TOOL_APPROVAL_REQUIRES_CONSOLE
    assert err.value.status_code == 409 and "send_email" in err.value.message
    # the console caller gets the answer (its stream shows the card)
    assert invoke.invoke_agent_text(_agent(), "hi")["approval_refused"]


def test_the_stream_refuses_v1_on_the_first_approval_event():
    event = {"event": "approval_refused", "data": {"tool": "send_email"}}
    invoke._refuse_unapprovable_event(_agent(), event, console_user=True)
    with pytest.raises(AppError) as err:
        invoke._refuse_unapprovable_event(_agent(), event, console_user=False)
    assert err.value.code == invoke.TOOL_APPROVAL_REQUIRES_CONSOLE


# ── the console chat: approvals out, structured + the card kept ─────────────────


def _hooks_agent(name: str) -> str:
    agent_id = make_active_agent(name=name)
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        row.spec = {"name": name, "tool_hooks": {"approval_tools": ["send_email"]}}
        db.commit()
    finally:
        db.close()
    return agent_id


def test_a_console_turn_sends_the_approved_tools(client, monkeypatch):
    seen: list = []

    def fake_events(agent, prompt, **_kw):
        seen.append(invoke.CONSOLE_TOOL_APPROVALS.get())
        return delta_events("ok")

    monkeypatch.setattr(chat_service, "invoke_agent_events", fake_events)
    agent_id = _hooks_agent("sb2-approve")
    plain = make_active_agent(name="sb2-plain")
    res = client.post(f"/api/chat/{agent_id}",
                      json={"prompt": "go", "approved_tools": ["send_email"]})
    assert res.status_code == 200, res.text
    client.post(f"/api/chat/{plain}", json={"prompt": "go", "approved_tools": ["x"]})
    assert seen == [{"interactive": True, "approved": ["send_email"]}, None]


def test_history_keeps_structured_and_the_approval_card(client, monkeypatch):
    events = [
        {"event": "delta", "data": {"text": "Oslo is sunny."}},
        {"event": "structured", "data": {"value": {"city": "Oslo"}}},
        {"event": "approval_required", "data": {"tool": "send_email", "tool_use_id": "t1",
                                                "input": {"to": "***"}}},
    ]
    monkeypatch.setattr(chat_service, "invoke_agent_events",
                        lambda agent, prompt, **_kw: iter(events))
    agent_id = _hooks_agent("sb2-history")
    assert client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"}).status_code == 200
    sid = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["session_id"]
    history = client.get(f"/api/chat/{agent_id}/history",
                         params={"session_id": sid}).json()["messages"]
    assert [m["role"] for m in history] == ["user", "agent", "approval"]
    assert history[1]["structured"] == {"city": "Oslo"}
    assert history[2]["name"] == "send_email"
    assert json.loads(history[2]["text"]) == {"to": "***"}
    assert "structured" not in history[0]


def test_fold_structured_drops_a_stray_row():
    rows = [ChatMessage(id=1, role="user", text="q"),
            ChatMessage(id=2, role=chat_ledger.ROLE_STRUCTURED, text='{"a": 1}'),
            ChatMessage(id=3, role="agent", text="a"),
            ChatMessage(id=4, role=chat_ledger.ROLE_STRUCTURED, text="not json")]
    folded = chat_ledger.fold_structured(rows)
    assert [(r.id, s) for r, s in folded] == [(1, None), (3, None)]


# ── agents as a share kind; the isolated grantee's boundary ─────────────────────


def _runtime_agent(pid: str, name: str, arn: str) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=WS, name=name, method="zip_runtime", status="active",
                        arn=arn, spec={"protocol": "http"})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _account_region() -> tuple[str, str]:
    from app.models.ledger import Workspace

    db = SessionLocal()
    try:
        ws = db.get(Workspace, WS)
        return ws.account_id, ws.region
    finally:
        db.close()


def test_a_use_shared_agent_is_invocable_from_an_isolated_grantee(world):
    account, region = _account_region()
    arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/helper_ab12cd-XyZ0123456"
    agent_id = _runtime_agent("alpha", "helper", arn)
    options = world.olga.get("/api/projects/alpha/shares/options",
                             params={"workspace_id": WS}).json()["resources"]
    assert {"kind": "agent", "id": agent_id, "name": "helper",
            "detail": "zip_runtime"} in options
    inp = project_iam.BoundaryInputs(project_id="beta", account_id=account, region=region)

    def agent_sid():
        return next((s for s in project_iam.boundary_policy_document(inp)["Statement"]
                     if s.get("Sid") == "AgentShared"), None)

    assert agent_sid() is None
    res = _share(world.olga, kind="agent", resource_id=agent_id, access="read")
    assert res.status_code == 201, res.text
    assert agent_sid() is None  # read only: no invoke
    world.olga.delete(f"/api/projects/alpha/shares/{res.json()['id']}")
    res = _share(world.olga, kind="agent", resource_id=agent_id, access="use")
    assert res.status_code == 201, res.text
    statement = agent_sid()
    assert statement["Resource"] == [arn, f"{arn}/runtime-endpoint/*"]
    assert statement["Action"] == resource_shares.AGENT_INVOKE_ACTIONS
    gamma = project_iam.BoundaryInputs(project_id="gamma", account_id=account, region=region)
    assert not any(s.get("Sid") == "AgentShared"
                   for s in project_iam.boundary_policy_document(gamma)["Statement"])


def test_another_projects_agent_cannot_be_lent(world):
    agent_id = _runtime_agent("beta", "theirs", "arn:aws:bedrock-agentcore:r:1:runtime/x")
    res = _share(world.olga, kind="agent", resource_id=agent_id, access="use")
    assert res.status_code == 404, res.text
