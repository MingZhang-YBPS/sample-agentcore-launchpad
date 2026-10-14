"""Projects phase 1 (design §5.5/§5.6/§5.12) for releases and Agent-DLC: the promotion
worker runs as its release's project, agent names and busy targets are judged across
projects, the target workspace must be bound to the request's project, mappings keep
the workspace default apart from a project's own, and DLC writes never land in a
project the caller cannot see."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project, project_context
from app.deployer import pipeline
from app.main import create_app
from app.models.dlc import AnnotationTask, CriteriaSet, WatchConfig
from app.models.ledger import Agent, Job, Promotion, ReleaseBundle, Workspace
from app.models.projects import Project, ProjectMember, ProjectWorkspace
from app.models.resource_mapping import ResourceMapping
from app.services import access, promotion_exec
from app.services import promotion as promotion_service
from app.services import resource_mapping as mapping_service
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
TARGET = "tgt-ws"


# ── ledger helpers (the test_project_scope pattern) ───────────────────────────


def _project(pid: str, *, workspaces=(DEFAULT_WORKSPACE_ID,)) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            if db.get(Project, pid) is None:
                db.add(Project(id=pid, name=pid.upper(), status="active"))
            for ws in workspaces:
                if db.get(ProjectWorkspace, (pid, ws)) is None:
                    db.add(ProjectWorkspace(project_id=pid, workspace_id=ws,
                                            is_workspace_default=False))
            access.bump_epoch(db)
            db.commit()
    finally:
        db.close()


def _target_ws(*, bind=("default",)) -> None:
    db = SessionLocal()
    try:
        db.add(Workspace(id=TARGET, name="Target", account_id="444455556677",
                         region="us-west-1", bootstrap_status="ready", resources={}))
        db.flush()
        for pid in bind:  # registering a workspace may already bind `default`
            if db.get(ProjectWorkspace, (pid, TARGET)) is None:
                db.add(ProjectWorkspace(project_id=pid, workspace_id=TARGET,
                                        is_workspace_default=False))
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()


def _agent(name: str, pid: str, *, ws: str = DEFAULT_WORKSPACE_ID) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=ws, name=name, method="harness", status="active",
                        spec={"name": name})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _bundle(agent_id: str, name: str) -> str:
    db = SessionLocal()
    try:
        row = ReleaseBundle(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                            agent_name=name, method="harness", digest="d" * 64,
                            spec={"name": name, "method": "harness", "system_prompt": "v1",
                                  "model_id": "anthropic.claude"})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _promotion(bundle_id: str, *, target: str = TARGET, status: str = "approved") -> str:
    db = SessionLocal()
    try:
        row = Promotion(workspace_id=DEFAULT_WORKSPACE_ID, bundle_id=bundle_id,
                        target_workspace_id=target, status=status,
                        change_note="c", rollback_note="r")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


# ── the promotion worker (§5.12) ──────────────────────────────────────────────


def test_the_worker_thread_runs_every_stage_as_the_releases_project(monkeypatch):
    _project("alpha")
    agent = _agent("ship", "alpha")
    promotion_id = _promotion(_bundle(agent, "ship"), target=DEFAULT_WORKSPACE_ID)
    db = SessionLocal()
    try:
        with project_context("alpha"):  # the execute route runs as the project
            row = db.get(Promotion, promotion_id)
            assert row.project_id == "alpha"  # from its bundle, from its agent
            job = promotion_exec.admit(db, row, action=promotion_exec.ACTION_EXECUTE,
                                       bundle_id=row.bundle_id, previous_id=None)
            db.commit()
            job_id = job.id
            assert job.project_id == "alpha"
    finally:
        db.close()
    seen: list[str | None] = []
    for name in promotion_exec.STAGE_ORDER:
        monkeypatch.setattr(
            promotion_exec, f"_stage_{name}",
            lambda run: (seen.append(current_project()), pipeline.StageResult(detail="ok"))[1],
        )
    promotion_exec.start_async(job_id).join(30)  # a real thread: no inherited context
    assert seen == ["alpha"] * len(promotion_exec.STAGE_ORDER)
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.get(Promotion, promotion_id).status == promotion_exec.STATUS_SUCCEEDED
            assert db.get(Job, job_id).status == "succeeded"
    finally:
        db.close()


def test_resolve_refuses_a_target_name_held_by_another_project():
    _project("alpha", workspaces=(DEFAULT_WORKSPACE_ID,))
    _target_ws(bind=("default", "alpha"))
    _agent("ship", "default", ws=TARGET)  # the target's `ship` is the default project's
    agent = _agent("ship", "alpha")
    bundle_id = _bundle(agent, "ship")
    promotion_id = _promotion(bundle_id)
    run = SimpleNamespace(
        promotion_id=promotion_id, target_id=TARGET, project_id="alpha",
        bundle=lambda db: db.get(ReleaseBundle, bundle_id),
    )
    with project_context("alpha"), pytest.raises(promotion_exec.StageFailed) as err:
        promotion_exec._stage_resolve(run)
    assert "another project" in str(err.value)


def test_gates_and_holder_flag_another_projects_name_without_showing_it():
    _project("alpha")
    _target_ws(bind=("default", "alpha"))
    _agent("ship", "default", ws=TARGET)
    agent = _agent("ship", "alpha")
    bundle_id = _bundle(agent, "ship")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            bundle = db.get(ReleaseBundle, bundle_id)
            holder, foreign = promotion_service.target_holder(db, TARGET, "ship", "alpha")
            assert holder is None and foreign
            gates = promotion_service.evaluate_gates(db, bundle, db.get(Workspace, TARGET))
            assert "target_agent" in gates["blocking_failures"]
        with project_context("default"):
            holder, foreign = promotion_service.target_holder(db, TARGET, "ship", "default")
            assert holder is not None and not foreign
    finally:
        db.close()


def test_a_busy_target_counts_other_projects_but_names_only_ours():
    _project("alpha")
    _target_ws(bind=("default", "alpha"))
    theirs = _promotion(_bundle(_agent("ship", "default"), "ship"), status="executing")
    mine_bundle = _bundle(_agent("ship-a", "alpha"), "ship")
    mine = _promotion(mine_bundle)
    db = SessionLocal()
    try:
        with project_context("alpha"):
            row = db.get(Promotion, mine)
            with pytest.raises(AppError) as err:
                promotion_exec.assert_target_free(db, row, db.get(ReleaseBundle, mine_bundle))
        assert err.value.code == "promotion.target_busy"
        assert err.value.detail == {"promotion_id": None}
        assert theirs not in err.value.message
    finally:
        db.close()


# ── the target must be bound to the project (§5.6) ────────────────────────────


def _scope(pid: str, *, is_default: bool = False) -> access.ProjectScope:
    return access.ProjectScope(
        id=pid, name=pid, status="active", is_default=is_default,
        workspace_id=DEFAULT_WORKSPACE_ID, permissions=access.ALL_PROJECT_KEYS,
        role_sources=(), bypass=True,
    )


def test_a_target_not_bound_to_the_project_reads_as_not_found():
    _project("alpha")
    _target_ws(bind=("default",))
    admin = SimpleNamespace(user_id=None, role="admin")
    db = SessionLocal()
    try:
        with pytest.raises(NotFoundError) as err:
            promotion_service.assert_project_reaches(db, admin, TARGET, _scope("alpha"), "GET")
        assert err.value.code == "project.not_found"
        promotion_service.assert_project_reaches(
            db, admin, TARGET, _scope("default", is_default=True), "GET")
        # an exempt route (no project) and the request's own workspace are not re-checked
        promotion_service.assert_project_reaches(db, admin, TARGET, None, "GET")
        promotion_service.assert_project_reaches(
            db, admin, DEFAULT_WORKSPACE_ID, _scope("alpha"), "GET")
    finally:
        db.close()


# ── resource mappings: project override vs workspace default (§5.6) ──────────


def _mapping(scope: str, resource_id: str, pid: str | None = "alpha") -> ResourceMapping:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = mapping_service.upsert_mapping(
                db, workspace_id=DEFAULT_WORKSPACE_ID, kind="kb", name="hr",
                resource_id=resource_id, note=None, updated_by="t", scope=scope,
            )
            db.commit()
            return row
    finally:
        db.close()


def test_a_project_mapping_overrides_the_workspace_default_and_never_rewrites_it():
    _project("alpha")
    default = _mapping("workspace", "DEFAULTKB1")
    assert default.project_id is None
    mine = _mapping("project", "ALPHAKB111")
    assert mine.project_id == "alpha" and mine.id != default.id
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.get(ResourceMapping, default.id).resource_id == "DEFAULTKB1"
        spec = {"knowledge_bases": [{"kb_id": "SRCKB", "name": "hr"}]}
        with project_context("alpha"):
            out = mapping_service.resolve_spec(
                db, spec, source_workspace_id=None, target_workspace_id=DEFAULT_WORKSPACE_ID)
            assert out.spec["knowledge_bases"][0]["kb_id"] == "ALPHAKB111"
            scopes = {r["scope"] for r in map(mapping_service.mapping_out,
                                              mapping_service.list_mappings(
                                                  db, DEFAULT_WORKSPACE_ID))}
            assert scopes == {"project", "workspace"}
        with project_context("default"):  # another project falls back to the default
            out = mapping_service.resolve_spec(
                db, spec, source_workspace_id=None, target_workspace_id=DEFAULT_WORKSPACE_ID)
            assert out.spec["knowledge_bases"][0]["kb_id"] == "DEFAULTKB1"
        with project_context("alpha"):
            assert mapping_service.delete_mapping(
                db, workspace_id=DEFAULT_WORKSPACE_ID, kind="kb", name="hr")
            db.commit()
        with project_context(None):
            left = db.scalars(select(ResourceMapping.id)).all()
        assert left == [default.id]
    finally:
        db.close()


# ── through the API ───────────────────────────────────────────────────────────


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _alpha_member(app, username: str, role_key: str = "biz_owner") -> TestClient:
    db = SessionLocal()
    try:
        user, _ = users_service.create_user(
            db, username=username, email=f"{username}@acme-corp.com", role="member",
            password="sufficient-pass", workspaces=[], created_by="test",
        )
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        db.add(ProjectMember(project_id="alpha", user_id=user.id, role_key=role_key))
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()
    client = TestClient(app, client=PEER)
    res = client.post("/api/auth/login",
                      json={"username": username, "password": "sufficient-pass"})
    assert res.status_code == 200, res.text
    return client


def _task(agent_id: str | None, **extra) -> dict:
    return {"agent_id": agent_id, "criterion_key": "c1", "annotators": ["ann1", "ann2"],
            "items": [{"ref": "i1", "prompt": "hi"}], **extra}


def test_an_annotation_task_cannot_be_filed_under_another_projects_agent(app):
    _project("alpha")
    theirs = _agent("their-agent", "default")
    mine = _agent("my-agent", "alpha")
    client = _alpha_member(app, "lena")
    res = client.post("/api/annotation-tasks", json=_task(theirs))
    assert res.status_code == 404, res.text
    assert client.post("/api/annotation-tasks",
                       json=_task(mine, dataset_id="nope")).status_code == 404
    res = client.post("/api/annotation-tasks", json=_task(mine))
    assert res.status_code == 201, res.text
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.scalars(select(AnnotationTask.project_id)).all() == ["alpha"]
    finally:
        db.close()


def test_a_golden_set_cannot_name_another_projects_criteria_lineage(app):
    _project("alpha")
    agent = _agent("their-agent", "default")
    db = SessionLocal()
    try:
        with project_context("default"):
            row = CriteriaSet(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent,
                              lineage_id="lin-theirs", version=1, kind="agent", name="x")
            db.add(row)
            db.commit()
    finally:
        db.close()
    client = _alpha_member(app, "mona")
    res = client.post("/api/golden-sets",
                      json={"name": "g", "criteria_lineage_id": "lin-theirs"})
    assert res.status_code == 404 and res.json()["code"] == "criteria.not_found"


def test_a_project_member_without_the_key_does_not_see_judge_verdicts():
    from app.routers import dlc as dlc_router

    def request(keys, role="member"):
        identity = SimpleNamespace(role=role, can=lambda key: True)  # the global keys say yes
        scope = access.ProjectScope(
            id="alpha", name="A", status="active", is_default=False,
            workspace_id=DEFAULT_WORKSPACE_ID, permissions=frozenset(keys),
            role_sources=(), bypass=False,
        )
        return SimpleNamespace(state=SimpleNamespace(identity=identity, project=scope))

    from app.routers import auth

    original = auth.require_identity
    try:
        dlc_router.require_identity = lambda req: req.state.identity
        assert not dlc_router._sees_verdicts(request({"project.use"}))
        assert dlc_router._sees_verdicts(request({"judge.calibrate"}))
        assert dlc_router._sees_verdicts(request(set(), role="admin"))
    finally:
        dlc_router.require_identity = original


def test_the_audit_trail_filters_by_project(app):
    from app.services.audit import record_audit_event

    _project("alpha")
    db = SessionLocal()
    try:
        for pid, action in (("alpha", "a.one"), ("default", "d.one"), (None, "w.one")):
            with project_context(pid):
                record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor="x",
                                   action=action, target="t", db=db)
                db.commit()  # stamped at flush, from the project then current
        # a workspace-level event has no project at all
        db.execute(text("UPDATE audit_events SET project_id = NULL WHERE action = 'w.one'"))
        db.commit()
    finally:
        db.close()
    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    events = client.get("/api/audit", params={"project": "alpha"}).json()["events"]
    assert [e["action"] for e in events] == ["a.one"] and events[0]["project_id"] == "alpha"
    events = client.get("/api/audit", params={"project": "workspace"}).json()["events"]
    assert "w.one" in [e["action"] for e in events]
    assert all(e["project_id"] is None for e in events)


# ── the scheduler (§5.12) ─────────────────────────────────────────────────────


def test_a_scheduled_watch_of_an_archived_project_is_skipped(monkeypatch):
    from app.dlc import scheduler
    from app.dlc import watch as watch_svc

    _project("alpha")
    agent = _agent("watched", "alpha")
    live = _agent("live", "default")
    db = SessionLocal()
    try:
        for agent_id in (agent, live):
            db.add(WatchConfig(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                               enabled=True, every="daily"))
        db.execute(text("UPDATE projects SET status = 'archived' WHERE id = 'alpha'"))
        db.commit()
    finally:
        db.close()
    ran: list[tuple[str, str | None]] = []
    monkeypatch.setattr(watch_svc, "run_now", lambda db, config, **_: (
        ran.append((config.agent_id, current_project())), "run-1")[1])
    db = SessionLocal()
    try:
        out = scheduler.run_due_watches(db)
    finally:
        db.close()
    assert ran == [(live, "default")]
    assert out["skipped"] == [{"agent_id": agent, "reason": "project.archived"}]


def test_mapping_writes_through_the_api_respect_scope_and_ownership(app):
    from app.models.ledger import ResourceCreator

    _project("alpha")
    db = SessionLocal()
    try:
        for pid, kb in (("default", "THEIRKB001"), ("alpha", "ALPHAKB001")):
            with project_context(pid):
                db.add(ResourceCreator(workspace_id=DEFAULT_WORKSPACE_ID,
                                       kind="knowledge_base", resource_id=kb))
                db.commit()
    finally:
        db.close()
    ops = _alpha_member(app, "nils", role_key="release_ops")
    body = {"resource_id": "ALPHAKB001"}
    # several projects share the workspace: its default is an administrator's call
    res = ops.put("/api/resource-mappings/kb/hr", params={"scope": "workspace"}, json=body)
    assert res.status_code == 403, res.text
    res = ops.put("/api/resource-mappings/kb/hr", json={"resource_id": "THEIRKB001"})
    assert res.status_code == 404 and res.json()["code"] == "kb.not_found"
    res = ops.put("/api/resource-mappings/kb/hr", json=body)
    assert res.status_code == 200, res.text
    assert (res.json()["scope"], res.json()["project_id"]) == ("project", "alpha")
