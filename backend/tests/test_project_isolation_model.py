"""Phase 2 isolation model (projects design §10.2, §10.12, §10.13, R10-model): the
`project_resources` ledger and `agents.isolation_applied`, the resumable
`project_provision` job with injectable provisioners, the Service Quotas preflight,
and the isolation API. No AWS: every provisioner and client here is a fake."""

from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.core import db as db_module
from app.core.db import DEFAULT_WORKSPACE_ID, Base, SessionLocal
from app.core.project_scope import current_project, project_context
from app.models.ledger import Job
from app.models.project_isolation import (
    KIND_BOUNDARY_POLICY,
    KIND_MEMORY,
    ProjectResource,
    valid_kind,
)
from app.models.projects import Project
from app.services import inflight
from app.services import project_isolation as svc
from tests.conftest import client_error
from tests.test_project_scope import (  # noqa: F401
    ADMIN,
    PEER,
    _login,
    _member_of,
    _project,
    _user,
    app,
)

WS = DEFAULT_WORKSPACE_ID


# ── helpers ───────────────────────────────────────────────────────────────────


def _set_mode(pid: str, mode: str, **extra: Any) -> None:
    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        row.settings = {**(row.settings or {}), "isolation": {"mode": mode, **extra}}
        db.commit()
    finally:
        db.close()


def _queue(pid: str = "alpha", ws: str = WS) -> str:
    db = SessionLocal()
    try:
        job, created = svc.queue_provision(db, pid, ws, actor="test")
        assert created
        return job.id
    finally:
        db.close()


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        with project_context(None):
            job = db.get(Job, job_id)
            db.expunge(job)
            return job
    finally:
        db.close()


def _stages(job_id: str) -> dict[str, str]:
    return {e["name"]: e["status"] for e in svc.job_stages(_job(job_id))}


def _rows(pid: str = "alpha") -> dict[str, ProjectResource]:
    db = SessionLocal()
    try:
        rows = db.scalars(select(ProjectResource).where(ProjectResource.project_id == pid))
        out = {r.kind: r for r in rows}
        for r in out.values():
            db.expunge(r)
        return out
    finally:
        db.close()


class _NoQuota:
    def check(self, ctx):
        return []


def _provisioners(**changes: Any) -> svc.Provisioners:
    base = svc.Provisioners(iam=svc._PENDING, memory=svc._PENDING, kb=svc._PENDING,
                            gateway=svc._PENDING, storage=svc._PENDING, quotas=_NoQuota())
    from dataclasses import replace

    return replace(base, **changes)


class _Boundary:
    def __init__(self):
        self.calls = 0
        self.projects: list[str | None] = []

    def ensure_boundary(self, ctx):
        self.calls += 1
        self.projects.append(current_project())
        ctx.record(svc.ProvisionedResource(
            KIND_BOUNDARY_POLICY, resource_id=ctx.names.boundary_policy,
            arn=f"arn:aws:iam::111122223333:policy/{ctx.names.boundary_policy}",
            detail={"version": 1},
        ))
        return "boundary v1 attached"


class _Memory:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def ensure_memory(self, ctx):
        ctx.record(svc.ProvisionedResource(KIND_MEMORY, resource_id=f"{ctx.names.memory}-x1"))
        if self.fail:
            raise RuntimeError("CreateMemory throttled")
        return "memory ready"


@pytest.fixture
def alpha(app):  # noqa: F811
    _project("alpha")
    _set_mode("alpha", svc.MODE_ISOLATED)
    return "alpha"


# ── model and migration ───────────────────────────────────────────────────────


def test_kinds_and_names():
    assert valid_kind("memory") and valid_kind("inference_profile:anthropic.claude")
    assert not valid_kind("inference_profile:") and not valid_kind("bogus")
    names = svc.resource_names("risk-desk")
    assert names.memory == "launchpad_mem_risk_desk"
    assert names.kb_role == "launchpad-kb-role-risk-desk"
    assert names.m2m_provider == "launchpad-gw-m2m-risk-desk"
    assert names.ecr_repo == "launchpad-agents/risk-desk"
    assert names.s3_prefix == "projects/risk-desk/"


def test_isolation_applied_is_migrated_onto_an_old_agents_table(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE agents DROP COLUMN isolation_applied"))
    assert "isolation_applied" in db_module.schema_drift(engine).get("agents", [])
    db_module._migrate(engine)
    assert db_module.schema_drift(engine) == {}
    assert "project_resources" in sa.inspect(engine).get_table_names()


def test_settings_default_to_shared(app):  # noqa: F811
    _project("beta")
    db = SessionLocal()
    try:
        row = db.get(Project, "beta")
        assert svc.isolation_settings(row) == {
            "mode": "shared", "enabled": False, "gateway": "shared",
            "updated_by": None, "updated_at": None,
        }
        row.settings = {"isolation": {"mode": "weird", "gateway": "dedicated"}}
        assert svc.isolation_settings(row)["mode"] == "shared"
        assert svc.isolation_settings(row)["gateway"] == "dedicated"
    finally:
        db.close()


# ── the job ───────────────────────────────────────────────────────────────────


def test_placeholder_stages_are_skipped_and_the_state_is_partial(alpha):
    job_id = _queue()
    svc.execute_provision_job(job_id, _provisioners())
    job = _job(job_id)
    assert job.status == "succeeded" and job.project_id == "alpha"
    stages = _stages(job_id)
    assert stages["preflight"] == stages["finalize"] == "succeeded"
    assert {stages[s] for s in svc.PENDING_NODE} == {"skipped"}
    db = SessionLocal()
    try:
        state = svc.workspace_state(db, "alpha", WS)
    finally:
        db.close()
    assert state["state"] == svc.STATE_PARTIAL
    assert set(state["skipped_stages"]) == set(svc.PENDING_NODE)


def test_real_provisioners_record_rows_inside_the_project(alpha):
    boundary = _Boundary()
    job_id = _queue()
    svc.execute_provision_job(job_id, _provisioners(iam=boundary, memory=_Memory()))
    assert boundary.projects == ["alpha"]  # the worker runs inside project_context
    rows = _rows()
    assert set(rows) == {KIND_BOUNDARY_POLICY, KIND_MEMORY}
    assert {r.status for r in rows.values()} == {"ready"}
    assert rows[KIND_BOUNDARY_POLICY].detail == {"version": 1}
    assert rows[KIND_MEMORY].job_id == job_id


def test_a_resume_skips_finished_stages(alpha):
    boundary = _Boundary()
    job_id = _queue()
    db = SessionLocal()
    try:
        with project_context(None):
            job = db.get(Job, job_id)
            payload = dict(job.payload)
            stages = [dict(s) for s in payload["stages"]]
            for s in stages:
                if s["name"] in ("preflight", "boundary"):
                    s["status"] = "succeeded"
            job.payload = {**payload, "stages": stages}
            job.status = "running"  # what a dead process leaves behind
            db.commit()
    finally:
        db.close()
    svc.execute_provision_job(job_id, _provisioners(iam=boundary, memory=_Memory()))
    assert boundary.calls == 0
    assert _job(job_id).status == "succeeded"
    assert "memory" in _rows()


def test_a_failed_stage_marks_its_rows_and_a_retry_is_a_fresh_run(alpha):
    job_id = _queue()
    svc.execute_provision_job(job_id, _provisioners(iam=_Boundary(), memory=_Memory(fail=True)))
    job = _job(job_id)
    assert job.status == "failed" and "throttled" in job.error
    stages = _stages(job_id)
    assert stages["boundary"] == "succeeded" and stages["memory"] == "failed"
    assert stages["kb-role"] == "pending"
    rows = _rows()
    assert rows[KIND_MEMORY].status == "failed" and "throttled" in rows[KIND_MEMORY].error
    assert rows[KIND_BOUNDARY_POLICY].status == "provisioning"  # never finalized

    boundary = _Boundary()
    retry = _queue()
    assert retry != job_id
    svc.execute_provision_job(retry, _provisioners(iam=boundary, memory=_Memory()))
    assert boundary.calls == 1  # a new job re-runs every stage (all idempotent)
    rows = _rows()
    assert {r.status for r in rows.values()} == {"ready"}
    assert rows[KIND_MEMORY].error is None


def test_a_run_refuses_when_isolation_was_switched_off(alpha):
    job_id = _queue()
    _set_mode("alpha", svc.MODE_SHARED)
    boundary = _Boundary()
    svc.execute_provision_job(job_id, _provisioners(iam=boundary))
    assert boundary.calls == 0
    job = _job(job_id)
    assert job.status == "failed" and "switched off" in job.error


def test_queue_is_idempotent_while_a_run_is_in_flight(alpha):
    first = _queue()
    db = SessionLocal()
    try:
        job, created = svc.queue_provision(db, "alpha", WS, actor="test")
    finally:
        db.close()
    assert not created and job.id == first


def test_binding_an_isolated_project_queues_a_run(app, monkeypatch):  # noqa: F811
    started: list[str] = []
    monkeypatch.setattr(svc, "start_provision_async", started.append)
    _project("beta")
    db = SessionLocal()
    try:
        assert svc.on_workspaces_bound(db, "beta", [WS], actor="admin") == []  # shared
        _set_mode("beta", svc.MODE_ISOLATED)
        db.expire_all()
        queued = svc.on_workspaces_bound(db, "beta", [WS], actor="admin")
    finally:
        db.close()
    assert len(queued) == 1 and started == queued


def test_provision_jobs_resume_at_startup(alpha, monkeypatch):
    assert svc.JOB_TYPE in inflight.resumable_job_types()
    job_id = _queue()
    started: list[str] = []
    monkeypatch.setattr(svc, "start_provision_async", started.append)
    from app.deployer import pipeline

    assert job_id in pipeline.resume_pending_jobs()
    assert started == [job_id]


# ── §10.12 quota preflight ────────────────────────────────────────────────────


def test_a_blocked_quota_fails_the_preflight_and_keeps_the_findings(alpha):
    class Short:
        def check(self, ctx):
            return [svc.QuotaFinding("agentcore.memory", "AgentCore memories", 1,
                                     limit=10, used=10)]

    boundary = _Boundary()
    job_id = _queue()
    svc.execute_provision_job(job_id, _provisioners(iam=boundary, quotas=Short()))
    job = _job(job_id)
    assert job.status == "failed" and "AgentCore memories 10/10" in job.error
    assert boundary.calls == 0
    assert job.payload["preflight"][0]["blocked"] is True


class _Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **_kw):
        return iter(self.pages)


class _FakeAws:
    def __init__(self, *, summary=None, quotas=None, memories=0, quotas_error=None):
        self.summary, self.quotas, self.memories = summary, quotas, memories
        self.quotas_error = quotas_error

    def client(self, service, **_kw):
        if service == "iam":
            return SimpleNamespace(get_account_summary=lambda: {"SummaryMap": self.summary})
        if service == "service-quotas":
            def paginator(_name):
                if self.quotas_error:
                    raise self.quotas_error
                return _Paginator([{"Quotas": self.quotas}])
            return SimpleNamespace(get_paginator=paginator)
        if service == "bedrock-agentcore-control":
            return SimpleNamespace(
                list_memories=lambda **_kw: {"memories": [{}] * self.memories},
                list_gateways=lambda **_kw: {"items": [{}]},
            )
        raise AssertionError(service)


def _ctx(aws: _FakeAws, resources: dict | None = None, gateway: str = "shared"):
    return svc.ProvisionContext(
        project_id="alpha", workspace_id=WS, job_id="j", names=svc.resource_names("alpha"),
        settings={"gateway": gateway}, resources=resources or {}, _workspace=aws,
    )


def test_service_quotas_checker_reads_limits_and_usage():
    aws = _FakeAws(
        summary={"Roles": 799, "RolesQuota": 1000, "Policies": 1500, "PoliciesQuota": 1500},
        quotas=[{"QuotaName": "Gateways per account", "Value": 10},
                {"QuotaName": "Memory resources per account", "Value": 50}],
        memories=12,
    )
    checker = svc.ServiceQuotasChecker(shared_boundary_exists=lambda _ctx: False)
    found = {f.key: f for f in checker.check(_ctx(aws))}
    assert (found["agentcore.memory"].limit, found["agentcore.memory"].used) == (50, 12)
    assert not found["agentcore.memory"].blocked and not found["iam.roles"].blocked
    assert found["iam.policies"].blocked  # 1500/1500 and the boundary policy is new
    assert "agentcore.gateway" not in found  # only for a dedicated gateway

    # adopted resources need nothing; a workspace boundary is shared by every project
    checker = svc.ServiceQuotasChecker(shared_boundary_exists=lambda _ctx: True)
    found = {f.key: f for f in checker.check(_ctx(aws, {"memory": {}}))}
    assert found["iam.policies"].needed == 0 and found["agentcore.memory"].needed == 0


def test_unreadable_quotas_are_unknown_not_blocking():
    aws = _FakeAws(summary={"Roles": 1, "RolesQuota": 1000},
                   quotas_error=client_error("AccessDeniedException"))
    checker = svc.ServiceQuotasChecker(shared_boundary_exists=lambda _ctx: True)
    found = {f.key: f for f in checker.check(_ctx(aws, gateway="dedicated"))}
    memory = found["agentcore.memory"]
    assert not memory.known and not memory.blocked
    assert "AccessDeniedException" in memory.note
    assert found["agentcore.gateway"].used == 1 and not found["agentcore.gateway"].known
    assert not found["iam.policies"].known  # no PoliciesQuota in the summary


# ── §10.13 API ────────────────────────────────────────────────────────────────


def _admin(app):  # noqa: F811
    from fastapi.testclient import TestClient

    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


@pytest.fixture
def api(app, monkeypatch):  # noqa: F811
    started: list[str] = []
    monkeypatch.setattr(svc, "start_provision_async", started.append)
    _project("alpha")
    _project("beta")
    dev = _user("dev", grant=False)
    _member_of("alpha", dev)
    return SimpleNamespace(admin=_admin(app), dev=_login(app, "dev"), started=started)


def test_reads_are_for_members_and_another_project_is_404(api):
    res = api.dev.get("/api/projects/alpha/isolation")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "shared" and body["enabled"] is False
    assert [w["workspace_id"] for w in body["workspaces"]] == [WS]
    assert body["workspaces"][0]["state"] == svc.STATE_NONE
    for path in ("/api/projects/beta/isolation", "/api/projects/beta/resources",
                 f"/api/projects/beta/provisioning?workspace={WS}"):
        res = api.dev.get(path)
        assert res.status_code == 404 and res.json()["code"] == "project.not_found", path
    res = api.dev.get("/api/projects/alpha/provisioning?workspace=elsewhere")
    assert res.status_code == 404 and res.json()["code"] == "project.not_bound"


def test_only_administrators_switch_isolation_or_retry(api):
    assert api.dev.put("/api/projects/alpha/isolation",
                       json={"enabled": True}).status_code == 403
    assert api.dev.post("/api/projects/alpha/provision",
                        json={"workspace": WS}).status_code == 403
    assert api.admin.put("/api/projects/alpha/isolation", json={}).status_code == 422
    assert api.admin.put("/api/projects/alpha/isolation",
                         json={"mode": "shared", "enabled": True}).status_code == 422


def test_enabling_queues_a_run_per_bound_workspace(api):
    res = api.admin.put("/api/projects/alpha/isolation", json={"enabled": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "isolated" and body["updated_by"] == ADMIN["username"]
    assert len(body["queued"]) == 1 and api.started == body["queued"]
    assert body["workspaces"][0]["state"] == svc.STATE_PROVISIONING
    # the same mode again is a no-op; a retry while in flight returns the same job
    again = api.admin.put("/api/projects/alpha/isolation", json={"mode": "isolated"})
    assert again.json()["queued"] == []
    res = api.admin.post("/api/projects/alpha/provision", json={"workspace": WS})
    assert res.status_code == 202 and res.json()["created"] is False
    assert res.json()["job"]["id"] == body["queued"][0]

    svc.execute_provision_job(body["queued"][0], _provisioners(iam=_Boundary()))
    res = api.dev.get(f"/api/projects/alpha/provisioning?workspace={WS}")
    assert res.json()["state"] == svc.STATE_PARTIAL
    assert res.json()["job"]["status"] == "succeeded"
    resources = api.dev.get(f"/api/projects/alpha/resources?workspace={WS}").json()
    assert [r["kind"] for r in resources["resources"]] == [KIND_BOUNDARY_POLICY]

    res = api.admin.post("/api/projects/alpha/provision", json={"workspace": WS})
    assert res.status_code == 202 and res.json()["created"] is True
    assert len(api.started) == 2


def test_retry_needs_an_isolated_active_bound_project(api):
    res = api.admin.post("/api/projects/alpha/provision", json={"workspace": WS})
    assert res.status_code == 409 and res.json()["code"] == "project.isolation_disabled"
    _set_mode("alpha", svc.MODE_ISOLATED)
    res = api.admin.post("/api/projects/alpha/provision", json={"workspace": "elsewhere"})
    assert res.status_code == 404
    db = SessionLocal()
    try:
        db.get(Project, "alpha").status = "archived"
        db.commit()
    finally:
        db.close()
    res = api.admin.put("/api/projects/alpha/isolation", json={"enabled": False})
    assert res.status_code == 409 and res.json()["code"] == "project.archived"
