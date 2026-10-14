"""Evaluation pre-check (projects §17.3-5): a BYOC code zip deployed without the
ADOT launcher, whose endpoint has written no span, is refused with
`eval.no_telemetry` before a run row exists; every other agent is not probed."""

from unittest.mock import MagicMock

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import log_streams
from app.evaluation import service as svc
from app.evaluation.models import EvalRun
from app.models.ledger import Agent
from tests.conftest import client_error, ws_ctx
from tests.evaluation.test_run_scopes import wait_terminal
from tests.evaluation.test_runs_flow import make_agent, stub_environment


def byoc_agent(db, name="zip-agent", *, kind="code_zip", launcher=None) -> Agent:
    provenance = {"sha256": "abc"}
    if launcher is not None:
        provenance["adot_launcher"] = launcher
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="byoc", status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/rt-1",
        resource_id="rt-1",
        spec={"name": name, "byoc": {"artifact_kind": kind, "provenance": provenance}},
    )
    db.add(agent)
    db.commit()
    return agent


def logs_with(*pages):
    logs = MagicMock()
    logs.filter_log_events.side_effect = list(pages)
    return logs


def stub_logs(monkeypatch, logs):
    from app.services.workspace import WorkspaceContext

    real = WorkspaceContext.client
    monkeypatch.setattr(
        WorkspaceContext, "client",
        lambda self, service, *a, **kw: (
            logs if service == "logs" else real(self, service, *a, **kw)
        ),
    )


# ── log_streams.service_has_spans ──────────────────────────────────────────


def test_probe_hit_on_first_page():
    logs = logs_with({"events": [{"message": "{}"}]})
    assert log_streams.service_has_spans(logs, "rt_a.DEFAULT", since_ms=5) is True
    kwargs = logs.filter_log_events.call_args.kwargs
    assert kwargs["logGroupName"] == "aws/spans"
    assert kwargs["filterPattern"] == '"rt_a.DEFAULT"'
    assert kwargs["startTime"] == 5 and kwargs["limit"] == 1


def test_probe_follows_tokens_until_a_hit():
    logs = logs_with({"events": [], "nextToken": "t1"}, {"events": [{"message": "{}"}]})
    assert log_streams.service_has_spans(logs, "rt_a.DEFAULT", since_ms=0) is True
    assert logs.filter_log_events.call_args.kwargs["nextToken"] == "t1"


def test_probe_finished_scan_is_false():
    logs = logs_with({"events": [], "nextToken": "t1"}, {"events": []})
    assert log_streams.service_has_spans(logs, "rt_a.DEFAULT", since_ms=0) is False


def test_probe_missing_spans_group_is_false():
    logs = MagicMock()
    logs.filter_log_events.side_effect = client_error("ResourceNotFoundException")
    assert log_streams.service_has_spans(logs, "rt_a.DEFAULT", since_ms=0) is False


def test_probe_inconclusive_is_none():
    pages = log_streams.MAX_SPAN_PROBE_PAGES
    endless = [{"events": [], "nextToken": f"t{i}"} for i in range(pages)]
    assert log_streams.service_has_spans(logs_with(*endless), "s", since_ms=0) is None
    denied = MagicMock()
    denied.filter_log_events.side_effect = client_error("AccessDeniedException")
    assert log_streams.service_has_spans(denied, "s", since_ms=0) is None


# ── service.byoc_code_zip_without_adot / require_span_telemetry ────────────


def test_only_code_zip_without_launcher_is_flagged():
    db = SessionLocal()
    try:
        assert svc.byoc_code_zip_without_adot(byoc_agent(db, "a1")) is True
        assert svc.byoc_code_zip_without_adot(byoc_agent(db, "a2", launcher=False)) is True
        assert svc.byoc_code_zip_without_adot(byoc_agent(db, "a3", launcher=True)) is False
        assert svc.byoc_code_zip_without_adot(byoc_agent(db, "a4", kind="container_image")) is False
        assert svc.byoc_code_zip_without_adot(make_agent(db, name="a5")) is False
        assert svc.byoc_code_zip_without_adot(None) is False
    finally:
        db.close()


def test_require_raises_when_no_span():
    db = SessionLocal()
    agent = byoc_agent(db)
    db.close()
    logs = logs_with({"events": []})
    try:
        svc.require_span_telemetry(agent, ws_ctx(), "rt_a.DEFAULT", logs_client=logs)
    except svc.AppError as exc:
        assert exc.code == "eval.no_telemetry"
        assert exc.status_code == 400
    else:
        raise AssertionError("expected eval.no_telemetry")


def test_require_passes_self_instrumented_and_inconclusive():
    db = SessionLocal()
    agent = byoc_agent(db)
    db.close()
    svc.require_span_telemetry(agent, ws_ctx(), "s", logs_client=logs_with({"events": [{}]}))
    denied = MagicMock()
    denied.filter_log_events.side_effect = client_error("AccessDeniedException")
    svc.require_span_telemetry(agent, ws_ctx(), "s", logs_client=denied)


def test_require_does_not_probe_other_agents():
    db = SessionLocal()
    agent = make_agent(db, name="plain")
    db.close()
    logs = MagicMock()
    svc.require_span_telemetry(agent, ws_ctx(), "s", logs_client=logs)
    logs.filter_log_events.assert_not_called()


# ── POST /api/eval/runs ────────────────────────────────────────────────────


def test_create_run_refused_before_row(client, monkeypatch):
    db = SessionLocal()
    agent = byoc_agent(db)
    db.close()
    stub_environment(monkeypatch)
    logs = logs_with({"events": []})
    stub_logs(monkeypatch, logs)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 24,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 400
    assert res.json()["code"] == "eval.no_telemetry"
    assert logs.filter_log_events.call_args.kwargs["filterPattern"] == '"eval_agent_abc123.DEFAULT"'
    db = SessionLocal()
    try:
        assert db.query(EvalRun).count() == 0
    finally:
        db.close()


def test_create_run_with_spans_is_queued(client, monkeypatch):
    db = SessionLocal()
    agent = byoc_agent(db)
    db.close()
    stub_environment(monkeypatch)
    stub_logs(monkeypatch, logs_with({"events": [{"message": "{}"}]}))

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 24,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.json()
    assert wait_terminal(client, res.json()["id"])["status"] == "completed"
