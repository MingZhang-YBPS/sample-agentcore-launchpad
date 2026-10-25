"""Rows a worker writes off the request thread keep their project (design §5.12):
a deploy job carries its agent's project and its worker runs inside it; a system
preset's deploy job, like the preset, belongs to no project."""

from sqlalchemy import text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project, project_context
from app.deployer import pipeline
from app.models.ledger import Agent, Job
from app.models.projects import Project, ProjectWorkspace
from app.services import access


def _alpha() -> None:
    db = SessionLocal()
    try:
        db.add(Project(id="alpha", name="Alpha", status="active"))
        db.add(ProjectWorkspace(project_id="alpha", workspace_id=DEFAULT_WORKSPACE_ID))
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()


def _agent(name: str, pid: str | None, system_key: str | None = None) -> Agent:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="harness",
                        status="active", spec={"name": name}, system_key=system_key)
            db.add(row)
            db.commit()
            return row
    finally:
        db.close()


def _deploy(agent_id: str) -> Job:
    db = SessionLocal()
    try:
        with project_context(None):  # a promotion worker, a resumed job: no request
            agent = db.get(Agent, agent_id)
            _, job = pipeline.create_deployment(db, agent)
        return job
    finally:
        db.close()


def test_a_deploy_job_carries_its_agents_project_and_runs_in_it(monkeypatch):
    _alpha()  # two projects: the sole-workspace fallback cannot decide any more
    job = _deploy(_agent("alpha-agent", "alpha").id)
    assert job.project_id == "alpha"
    seen = []
    monkeypatch.setattr(pipeline, "_execute_deploy_job",
                        lambda job_id, resume=None: seen.append(current_project()))
    pipeline.execute_deploy_job(job.id)
    assert seen == ["alpha"]


def test_a_presets_deploy_job_is_platform_level():
    _alpha()
    job = _deploy(_agent("preset", None, system_key="aws-agent-solution-architect").id)
    db = SessionLocal()
    try:
        stored = db.execute(text("SELECT project_id FROM jobs WHERE id = :id"),
                            {"id": job.id}).scalar_one()
    finally:
        db.close()
    assert stored is None and job.payload.get("platform") is True


def test_bootstrap_jobs_belong_to_no_project():
    db = SessionLocal()
    try:
        with project_context("default"):
            job = Job(workspace_id=DEFAULT_WORKSPACE_ID, type="bootstrap_workspace", payload={})
            db.add(job)
            db.commit()
        assert job.project_id is None
    finally:
        db.close()
