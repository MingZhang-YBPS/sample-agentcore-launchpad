"""The project boundary in evaluation, optimization and Skill Lab (design §5.5): what the
automatic ORM criteria cannot see — account-wide AWS listings, CloudWatch telemetry,
background threads, the shared experiment gateway — and that a single-project
workspace (the workshop shape) behaves exactly as before."""

import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project, project_context
from app.evaluation import online, pipeline_routers
from app.evaluation import routers as eval_routers
from app.evaluation.models import EvalDataset, EvalPipeline, OnlineEvalConfig
from app.evaluation.service import ProjectFence, several_projects
from app.models.ledger import Agent
from app.models.projects import Project, ProjectWorkspace
from app.optimization import routers as opt_routers
from app.optimization import service as opt_service
from app.optimization.models import Experiment
from app.services import access
from app.services.visibility import Viewer
from app.skill_lab import samples, task_assets
from app.skill_lab import tasksets as taskset_svc
from app.skill_lab.models import SkillLabTaskset

WS = SimpleNamespace(id=DEFAULT_WORKSPACE_ID)
RUNTIMES = "/aws/bedrock-agentcore/runtimes/"


def _project(pid: str) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            if db.get(Project, pid) is None:
                db.add(Project(id=pid, name=pid.upper(), status="active"))
            if db.get(ProjectWorkspace, (pid, DEFAULT_WORKSPACE_ID)) is None:
                db.add(ProjectWorkspace(project_id=pid, workspace_id=DEFAULT_WORKSPACE_ID,
                                        is_workspace_default=False))
            access.bump_epoch(db)
            db.commit()
    finally:
        db.close()


def _add(pid: str | None, row):
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _agent(name: str, pid: str | None, resource_id: str | None = None) -> str:
    return _add(pid, Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime",
                           status="active", spec={"name": name}, resource_id=resource_id))


def _cloud_dataset(pid: str, cloud_id: str) -> str:
    return _add(pid, EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name=f"ds-{cloud_id}",
                                 items=[{"prompt": "hi"}], cloud={"dataset_id": cloud_id}))


# ── account-wide AWS listings ──────────────────────────────────────────────────


def test_cloud_datasets_follow_their_rows_across_projects():
    _cloud_dataset("default", "cd-default")
    db = SessionLocal()
    try:
        with project_context("default"):  # one project: every cloud dataset as before
            hidden = eval_routers._cloud_hidden(db, WS, Viewer.ALL)
            assert not hidden("cd-default") and not hidden("cd-console-made")
        _project("alpha")
        _cloud_dataset("alpha", "cd-alpha")
        with project_context("alpha"):
            hidden = eval_routers._cloud_hidden(db, WS, Viewer.ALL)
            assert hidden("cd-default")  # synced by another project: not "unlinked"
            assert not hidden("cd-alpha")
            assert hidden("cd-console-made")  # unattributed: the workspace default's
            with pytest.raises(NotFoundError):
                eval_routers._require_cloud_visible(db, WS, "cd-default", Viewer.ALL)
        with project_context("default"):
            hidden = eval_routers._cloud_hidden(db, WS, Viewer.ALL)
            assert hidden("cd-alpha") and not hidden("cd-console-made")
    finally:
        db.close()


def _online_row(pid: str, agent_id: str, config_id: str) -> None:
    _add(pid, OnlineEvalConfig(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                               agent_name="a", config_id=config_id, config_arn=f"arn:{config_id}",
                               name=f"oe_{config_id}", service_name="s", log_group="g"))


def test_online_configs_belong_to_their_ledger_or_parent_project():
    _project("alpha")
    default_agent = _agent("d", "default")
    alpha_agent = _agent("a", "alpha")
    _online_row("default", default_agent, "cfg-default")
    _online_row("alpha", alpha_agent, "cfg-alpha")
    exp = _add("default", Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="e",
                                     agent_id=default_agent, agent_name="d"))
    db = SessionLocal()
    try:
        with project_context("alpha"):
            fence = online._ProjectConfigs.load(db, WS)
            assert fence is not None
            assert fence.decide("cfg-default", "oe_x") is False
            assert fence.decide("cfg-alpha", "oe_x") is True
            assert fence.decide("other", f"exp_{exp[:8]}_ctl") is False  # default's arm
            assert fence.decide("other", "exp_deadbeef_ctl") is False  # owner gone
            assert fence.decide("other", "made_in_console") is None  # needs a log-group match
            # another project's ledger config is refused before any AWS call
            with pytest.raises(NotFoundError):
                online.get_config(db, WS, "cfg-default", control=object())
        with project_context("default"):
            assert online._ProjectConfigs.load(db, WS).decide(
                "other", f"exp_{exp[:8]}_ctl") is True
    finally:
        db.close()


def test_online_configs_are_unfenced_in_a_single_project_workspace():
    db = SessionLocal()
    try:
        with project_context("default"):
            assert not several_projects(db, DEFAULT_WORKSPACE_ID)
            assert online._ProjectConfigs.load(db, WS) is None
    finally:
        db.close()


def test_online_list_drops_another_projects_and_unmatched_external_configs(monkeypatch):
    _project("alpha")
    default_agent = _agent("d", "default")
    _agent("a", "alpha", resource_id="alpharun-abc123")
    _online_row("default", default_agent, "cfg-default")
    summaries = [
        {"onlineEvaluationConfigId": cid, "onlineEvaluationConfigName": name}
        for cid, name in (("cfg-default", "oe_d"), ("ext-alpha", "console_a"),
                          ("ext-nobody", "console_b"))
    ]
    groups = {"ext-alpha": [f"{RUNTIMES}alpharun-abc123-DEFAULT"],
              "ext-nobody": [f"{RUNTIMES}somebody-else-DEFAULT"]}
    monkeypatch.setattr(online.ac, "list_online_eval_configs", lambda _c: summaries)
    monkeypatch.setattr(
        online.ac, "get_online_eval_config",
        lambda _c, config_id: {
            "onlineEvaluationConfigId": config_id, "onlineEvaluationConfigName": "x",
            "dataSourceConfig": {"cloudWatchLogs": {"logGroupNames": groups.get(config_id, [])}},
        },
    )
    db = SessionLocal()
    try:
        with project_context("alpha"):
            seen = [c["config_id"] for c in online.list_configs(db, WS, control=object())]
        assert seen == ["ext-alpha"]  # matched to alpha's agent by its log group
    finally:
        db.close()


# ── CloudWatch telemetry ───────────────────────────────────────────────────────


def test_the_fence_names_another_projects_telemetry():
    _agent("own", "default", resource_id="ownrun-aaa111")
    db = SessionLocal()
    try:
        with project_context("default"):
            fence = ProjectFence(db, DEFAULT_WORKSPACE_ID)
            assert not fence.closed and fence.allows_service("anything.DEFAULT")
        _project("alpha")
        access.reset_cache_for_tests()
        _agent("theirs", "alpha", resource_id="theirrun-bbb222")
        with project_context("default"):
            fence = ProjectFence(db, DEFAULT_WORKSPACE_ID)
            assert fence.closed
            assert fence.foreign_log_group(f"{RUNTIMES}theirrun-bbb222-DEFAULT")
            assert not fence.foreign_log_group(f"{RUNTIMES}ownrun-aaa111-DEFAULT")
            assert not fence.foreign_log_group("aws/spans")
            assert fence.foreign_service("theirrun.DEFAULT")
            assert fence.allows_service("ownrun.DEFAULT")
            assert not fence.allows_service("unknown.DEFAULT")  # fail closed (§5.9)
            with pytest.raises(NotFoundError):
                eval_routers._assert_log_source_in_project(
                    db, WS, "theirrun.DEFAULT", ["aws/spans"])
            with pytest.raises(AppError) as err:
                eval_routers._assert_log_source_in_project(
                    db, WS, "x.DEFAULT", [f"{RUNTIMES}theirrun-bbb222-DEFAULT"])
            assert err.value.code == "run.log_group_missing"
            eval_routers._assert_log_source_in_project(  # an off-runtime service is fine
                db, WS, "offruntime.DEFAULT", ["aws/spans"])
            assert not pipeline_routers._in_project(
                fence, [{"service": "ownrun.DEFAULT"}, {"service": "theirrun.DEFAULT"}])
            assert pipeline_routers._in_project(fence, [{"service": "ownrun.DEFAULT"}])
            assert not pipeline_routers._in_project(fence, [])
    finally:
        db.close()


# ── background threads carry the row's project ────────────────────────────────


def _pipeline(pid: str) -> tuple[str, pipeline_routers.PipelineBody]:
    config = pipeline_routers.PipelineBody.model_validate({
        "name": "p", "output": {"dataset_name": "from-traces"},
    })
    row_id = _add(pid, EvalPipeline(
        workspace_id=DEFAULT_WORKSPACE_ID, name="p", status="running",
        config=config.model_dump(include={"source", "processing", "output"}),
    ))
    return row_id, config


def test_a_pipeline_run_writes_its_dataset_into_the_pipelines_project(monkeypatch):
    _project("alpha")
    monkeypatch.setattr(pipeline_routers.observability, "list_sessions",
                        lambda *_a, **_k: {"sessions": []})
    monkeypatch.setattr(pipeline_routers, "_read_items",
                        lambda *_a, **_k: ([{"prompt": "hello"}], []))
    ws = SimpleNamespace(id=DEFAULT_WORKSPACE_ID, context=None)
    private = SimpleNamespace(visible=lambda _sid: True)
    row_id, config = _pipeline("alpha")
    pipeline_routers._run_in_project("alpha", row_id, config, ws, private)
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(EvalPipeline, row_id)
            assert row.status == "succeeded", row.last_run
            dataset = db.get(EvalDataset, row.last_run["dataset_id"])
            assert dataset.project_id == "alpha"
    finally:
        db.close()


def test_an_experiment_action_runs_as_the_experiments_project(monkeypatch):
    _project("alpha")
    agent = _agent("a", "alpha")
    exp = _add("alpha", Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="e",
                                   agent_id=agent, agent_name="a"))
    monkeypatch.setattr(opt_service, "_spawn", lambda target: target())
    seen: list[str | None] = []
    with project_context("alpha"):
        opt_service.run_action(exp, "verdict", lambda _progress: seen.append(current_project()))
    seen_outside: list[str | None] = []
    opt_service.run_action(exp, "verdict", lambda _p: seen_outside.append(current_project()))
    assert seen == ["alpha"] and seen_outside == ["alpha"]


# ── the shared experiment gateway ─────────────────────────────────────────────


def test_the_running_experiment_slot_is_workspace_wide_but_unnamed(monkeypatch):
    _project("alpha")
    agent = _agent("d", "default")
    exp = _add("default", Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="secret-exp",
                                     agent_id=agent, agent_name="d", status="running"))
    db = SessionLocal()
    try:
        with project_context("alpha"):
            with pytest.raises(AppError) as err:
                opt_routers.create_experiment(
                    opt_routers.ExperimentCreate(agent_id="whatever"), None, db, WS,
                    Viewer.ALL,
                )
            assert err.value.code == "experiment.already_running"
            assert "secret-exp" not in err.value.message
            assert opt_service._other_project_test(f"exp_{exp[:8]}_bundle")
        with project_context("default"):
            with pytest.raises(AppError) as err:
                opt_routers.create_experiment(
                    opt_routers.ExperimentCreate(agent_id="whatever"), None, db, WS,
                    Viewer.ALL,
                )
            assert "secret-exp" in err.value.message
            assert not opt_service._other_project_test(f"exp_{exp[:8]}_bundle")
        assert not opt_service._other_project_test(f"exp_{exp[:8]}_bundle")  # unscoped
    finally:
        db.close()


def test_gateway_busy_details_hide_another_projects_test_name():
    _project("alpha")
    agent = _agent("d", "default")
    exp = _add("default", Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="e",
                                     agent_id=agent, agent_name="d"))
    tests = [{"abTestId": "t1", "name": f"exp_{exp[:8]}_bundle", "gatewayArn": "gw",
              "executionStatus": "RUNNING"}]
    data = SimpleNamespace(list_ab_tests=lambda **_k: {"abTests": tests})
    with project_context("alpha"), pytest.raises(AppError) as err:
        opt_service.assert_gateway_available("gw", data)
    assert err.value.detail["active_tests"] == [
        {"id": None, "name": None, "execution_status": "RUNNING"}]
    with project_context("default"), pytest.raises(AppError) as err:
        opt_service.assert_gateway_available("gw", data)
    assert err.value.detail["active_tests"][0]["name"] == f"exp_{exp[:8]}_bundle"


# ── Skill Lab ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tasksets_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        taskset_svc, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )


def test_every_project_gets_its_own_sample_task_sets(tasksets_dir):
    _project("alpha")
    db = SessionLocal()
    try:
        assert samples._seed_tasksets(db, DEFAULT_WORKSPACE_ID, lambda _m: None) == 6
        assert samples._seed_tasksets(db, DEFAULT_WORKSPACE_ID, lambda _m: None) == 0
        with project_context(None):
            rows = db.scalars(select(SkillLabTaskset)).all()
        by_project: dict[str, int] = {}
        for row in rows:
            assert row.sample
            by_project[row.project_id] = by_project.get(row.project_id, 0) + 1
        assert by_project == {"default": 3, "alpha": 3}
    finally:
        db.close()


def test_staged_task_assets_are_keyed_by_project_only_with_several(tmp_path, monkeypatch):
    monkeypatch.setattr(task_assets, "STAGING_DIR", tmp_path / "staging")
    with project_context("default"):
        single = task_assets._staging_root(DEFAULT_WORKSPACE_ID)
    assert single == task_assets._staging_root(DEFAULT_WORKSPACE_ID)  # as before
    _project("alpha")
    access.reset_cache_for_tests()
    with project_context("default"):
        default_root = task_assets._staging_root(DEFAULT_WORKSPACE_ID)
    with project_context("alpha"):
        alpha_root = task_assets._staging_root(DEFAULT_WORKSPACE_ID)
    assert default_root != alpha_root and default_root.parent == alpha_root.parent
