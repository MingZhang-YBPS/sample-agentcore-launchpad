"""Cost tags on every project's resources (cost-tags design §1.1–§1.3, CT-1): the
unified tag set, the project's own `cost_tags` setting, the per-service tag writes and
the resumable `cost_tags_backfill` job, re-tags after a transfer or a project change.
AWS is stubbed throughout."""

from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.ledger import Agent, IdentityProvider, Job, ManagedMemory
from app.models.projects import Department, Project
from app.services import cost_tags as ct
from app.services import project_telemetry as pt
from app.services import projects as projects_service
from app.services.agentcore import runtime as rt
from app.services.users import ROLE_ADMIN
from app.services.workspace import WorkspaceContext
from tests.test_project_scope import _project

WS = DEFAULT_WORKSPACE_ID
ACCT = "111122223333"
REGION = "us-west-2"
ADMIN = SimpleNamespace(role=ROLE_ADMIN, username="root", user_id=None)
MANAGED = {"launchpad:managed-by": "launchpad"}


def _ws(resources: dict[str, Any] | None = None, clients: dict[str, Any] | None = None):
    ctx = WorkspaceContext(account_id=ACCT, region=REGION, resources=dict(resources or {}),
                           id=WS)
    if clients is not None:
        object.__setattr__(ctx, "client", lambda service, **_kw: clients[service])
    return ctx


def _alpha(*, department: str | None = "risk", cost: dict[str, str] | None = None) -> None:
    _project("alpha")
    db = SessionLocal()
    try:
        if department and db.get(Department, department) is None:
            db.add(Department(id=department, name=department.upper()))
        row = db.get(Project, "alpha")
        row.department_id = department
        row.settings = {"cost_tags": cost} if cost else None
        db.commit()
    finally:
        db.close()


def _agent(name: str, pid: str | None, **fields: Any) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=WS, name=name, method=fields.pop("method", "zip_runtime"),
                        status="active", spec={"name": name}, **fields)
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


# ── the tag set ───────────────────────────────────────────────────────────────


def test_the_standard_set_never_lets_a_custom_tag_override_the_platform():
    tags = pt.standard_tags("alpha", workspace_id=WS, agent_id="a1", department_id="risk",
                            custom={"CostCenter": "42", "launchpad:project": "beta",
                                    "AWS:x": "y", "Launchpad:Agent-Id": "z"})
    assert tags == {"launchpad:project": "alpha", "launchpad:workspace": WS,
                    "launchpad:agent-id": "a1", **MANAGED, "launchpad:department": "risk",
                    "CostCenter": "42"}
    assert pt.shared_tags(WS) == {"launchpad:project": "shared", "launchpad:workspace": WS,
                                  **MANAGED}


def test_a_projects_tags_carry_its_department_and_cost_tags():
    _alpha(cost={"CostCenter": "1234"})
    assert ct.tags_for("alpha", workspace_id=WS, agent_id="a1") == {
        "launchpad:project": "alpha", "launchpad:workspace": WS, "launchpad:agent-id": "a1",
        **MANAGED, "launchpad:department": "risk", "CostCenter": "1234"}
    assert ct.tags_for(None)["launchpad:project"] == "shared"
    assert ct.tags_for("gone") == {"launchpad:project": "gone", **MANAGED}


def test_a_system_preset_is_shared_infrastructure():
    preset = SimpleNamespace(id="p1", project_id="alpha", system_key="architect",
                             workspace_id=WS)
    assert ct.agent_tags(preset)["launchpad:project"] == "shared"


def test_a_shared_projects_runtime_is_tagged_from_the_deploys_project():
    """D-1: not only isolated projects — the deploy's project, the deployer's ids."""
    _alpha(cost={"CostCenter": "1234"})

    class Control:
        def __init__(self):
            self.calls = []

        def create_agent_runtime(self, **kw):
            self.calls.append(kw)
            return {}

    control = Control()
    with project_context("alpha"):
        rt.create_code_runtime(control, runtime_name="n", s3_bucket="b", s3_key="k",
                               role_arn="r", environment={"A": "1"},
                               tags={"launchpad:agent-id": "a1", "launchpad:workspace": WS})
    assert control.calls[0]["tags"] == {
        "launchpad:project": "alpha", **MANAGED, "launchpad:department": "risk",
        "CostCenter": "1234", "launchpad:agent-id": "a1", "launchpad:workspace": WS}
    # the span attributes are untouched: only an isolated project sets them
    assert control.calls[0]["environmentVariables"] == {"A": "1"}


# ── the project setting ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [
    {f"k{i}": "v" for i in range(11)},
    {"aws:createdBy": "x"},
    {"Launchpad:project": "beta"},
    {"bad#key": "v"},
    {"ok": "bad\u0000value"},
    {"ok": 3},
    {"": "v"},
    {"x" * 129: "v"},
    {"ok": "v" * 257},
    {"Team": "a", "team": "b"},
    ["not", "a", "map"],
])
def test_cost_tags_follow_the_aws_tag_rules(value):
    with pytest.raises(AppError) as err:
        projects_service.check_cost_tags(value)
    assert err.value.code == "project.setting_invalid"


def test_valid_cost_tags_are_normalized_and_an_empty_map_clears():
    assert projects_service.check_cost_tags({" CostCenter ": " 1234 ",
                                             "team/unit": "a.b:c=d+e-f@g h"}) == {
        "CostCenter": "1234", "team/unit": "a.b:c=d+e-f@g h"}
    assert projects_service.check_cost_tags({}) is None


def test_an_owner_cannot_set_cost_tags_and_an_admin_change_queues_a_retag():
    _alpha()
    owner = SimpleNamespace(role="member", username="olga", user_id="u-olga")
    db = SessionLocal()
    try:
        row = db.get(Project, "alpha")
        with pytest.raises(AppError) as err:
            projects_service.update_project(db, owner, row, {"settings": {
                "cost_tags": {"CostCenter": "1"}}})
        assert err.value.code == "project.setting_forbidden"
        projects_service.update_project(db, ADMIN, row, {"settings": {
            "cost_tags": {"CostCenter": "1"}}})
        db.commit()
        assert db.get(Project, "alpha").settings["cost_tags"] == {"CostCenter": "1"}
        with project_context(None):
            jobs = list(db.query(Job).filter(Job.type == ct.JOB_TYPE))
        assert len(jobs) == 1 and jobs[0].project_id == "alpha"
        assert jobs[0].payload["scope"] == {"project_id": "alpha", "agent_ids": None}
        assert db.info["cost_tags_jobs"] == [jobs[0].id]
        # an unrelated settings change queues nothing new
        projects_service.update_project(db, ADMIN, row, {"description": "x"})
        db.commit()
        with project_context(None):
            assert db.query(Job).filter(Job.type == ct.JOB_TYPE).count() == 1
    finally:
        db.close()


def test_a_transfer_queues_the_agents_retag_in_its_new_project(monkeypatch):
    _alpha()
    _project("beta")
    agent_id = _agent("mover", "alpha", arn="arn:rt/1", resource_id="rt-1")
    started: list[str] = []
    monkeypatch.setattr(ct, "start_backfill_async", started.append)
    db = SessionLocal()
    try:
        source = db.get(Project, "alpha")
        out = projects_service.transfer_agent(db, ADMIN, source, agent_id, "beta")
        db.commit()
        [job_id] = out["retag_job_ids"]
        with project_context(None):
            job = db.get(Job, job_id)
        assert job.project_id == "beta" and job.status == "queued"
        assert job.payload["scope"] == {"project_id": "beta", "agent_ids": [agent_id]}
        assert ct.start_queued(db) == [job_id] and started == [job_id]
        assert ct.start_queued(db) == []  # started once
    finally:
        db.close()


# ── writing tags ──────────────────────────────────────────────────────────────


class _Stub:
    """Records every call; `fail` maps an operation to the exception it raises."""

    def __init__(self, fail: dict[str, Exception] | None = None, **answers: Any):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail or {}
        self.answers = answers

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**kw):
            self.calls.append((name, kw))
            if name in self.fail:
                raise self.fail[name]
            return self.answers.get(name, {})
        return call


def _err(code: str, op: str = "TagResource") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def test_each_service_is_tagged_in_its_own_shape():
    clients = {
        "bedrock-agentcore-control": _Stub(get_oauth2_credential_provider={
            "clientSecretArn": {"secretArn": "arn:secret:gh"}}),
        "bedrock-agent": _Stub(), "iam": _Stub(), "ecr": _Stub(), "logs": _Stub(),
        "secretsmanager": _Stub(),
        "codebuild": _Stub(batch_get_projects={"projects": [
            {"name": "b", "tags": [{"key": "Owner", "value": "ops"}]}]}),
    }
    ws = _ws(clients=clients)
    writer = ct.AwsTagWriter()
    tags = {"launchpad:project": "alpha"}
    cases = [
        ct.entry(ct.RUNTIME, arn="arn:rt"), ct.entry(ct.KNOWLEDGE_BASE, arn="arn:kb"),
        ct.entry(ct.IAM_ROLE, name="launchpad-agent-x"), ct.entry(ct.ECR_REPOSITORY, arn="arn:ecr"),
        ct.entry(ct.LOG_GROUP, arn="arn:lg"), ct.entry(ct.CODEBUILD_PROJECT, name="b"),
        ct.entry(ct.SECRET, name="gh", provider_kind="oauth2"),
    ]
    assert [writer.apply(ws, item, tags)[0] for item in cases] == [ct.TAGGED] * len(cases)
    kv = [{"Key": "launchpad:project", "Value": "alpha"}]
    assert clients["bedrock-agentcore-control"].calls[0] == (
        "tag_resource", {"resourceArn": "arn:rt", "tags": tags})
    assert clients["bedrock-agent"].calls == [("tag_resource",
                                               {"resourceArn": "arn:kb", "tags": tags})]
    assert clients["iam"].calls == [("tag_role", {"RoleName": "launchpad-agent-x", "Tags": kv})]
    assert clients["ecr"].calls == [("tag_resource", {"resourceArn": "arn:ecr", "tags": kv})]
    assert clients["logs"].calls == [("tag_resource", {"resourceArn": "arn:lg", "tags": tags})]
    assert clients["secretsmanager"].calls == [("tag_resource",
                                                {"SecretId": "arn:secret:gh", "Tags": kv})]
    # CodeBuild: UpdateProject replaces the tags, so the existing one is kept
    assert clients["codebuild"].calls[-1] == ("update_project", {"name": "b", "tags": [
        {"key": "Owner", "value": "ops"}, {"key": "launchpad:project", "value": "alpha"}]})
    assert writer.apply(ws, ct.entry(ct.DATA_SOURCE, name="kb/ds"), tags)[0] == ct.UNSUPPORTED


@pytest.mark.parametrize(("kind", "error", "result"), [
    (ct.RUNTIME, _err("AccessDeniedException"), ct.DENIED),
    (ct.RUNTIME, _err("ResourceNotFoundException"), ct.MISSING),
    (ct.RUNTIME, _err("ValidationException"), ct.FAILED),
    (ct.GATEWAY_TARGET, _err("ValidationException"), ct.UNSUPPORTED),
    (ct.RUNTIME, _err("ThrottlingException"), ct.FAILED),
])
def test_a_refusal_is_recorded_not_raised(kind, error, result):
    control = _Stub(fail={"tag_resource": error})
    ws = _ws(clients={"bedrock-agentcore-control": control})
    status, detail = ct.AwsTagWriter().apply(ws, ct.entry(kind, arn="arn:x"), {"a": "b"})
    assert status == result and detail


# ── the back-fill job ─────────────────────────────────────────────────────────


class _Writer:
    def __init__(self, crash_on: str | None = None):
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.crash_on = crash_on

    def apply(self, workspace, item, tags):
        if item["key"] == self.crash_on:
            self.crash_on = None
            raise RuntimeError("process died")
        self.calls.append((item["key"], tags))
        if item["kind"] == ct.LOG_GROUP:
            return ct.DENIED, "AccessDeniedException: logs:TagResource"
        return ct.TAGGED, ""


class _Discovery:
    def runtime_endpoints(self, workspace, runtime_id):
        return [f"arn:rt/{runtime_id}/runtime-endpoint/live"]

    def log_groups(self, workspace, prefix):
        return [f"arn:logs:{prefix}DEFAULT"]

    def knowledge_bases(self, workspace):
        return [("KB1", ["DS1"])]


RESOURCES = {"gateway_arn": "arn:gw/shared", "memory_id": "launchpad_memory-abc",
             "ecr_repo": "launchpad-agents", "codebuild_project": "launchpad-agent-builder",
             "execution_role_arn": f"arn:aws:iam::{ACCT}:role/launchpad-agent-execution-role"}


def _world() -> dict[str, str]:
    _alpha(cost={"CostCenter": "1234"})
    ids = {
        "runtime": _agent("svc", "alpha", arn="arn:rt/rt-1", resource_id="rt-1"),
        "harness": _agent("h", "alpha", method="harness", arn="arn:harness/h-1",
                          resource_id="h-1"),
        "foreign": _agent("found", "alpha", method="discovered", arn="arn:rt/theirs"),
    }
    db = SessionLocal()
    try:
        with project_context("alpha"):
            db.add(ManagedMemory(workspace_id=WS, memory_id="team_mem-1"))
            db.add(IdentityProvider(workspace_id=WS, name="gh", kind="oauth2",
                                    arn="arn:provider/gh"))
            db.commit()
    finally:
        db.close()
    return ids


def _run(job_id: str, writer: _Writer) -> None:
    ct.execute_backfill_job(job_id, ops=ct.BackfillOps(writer=writer, discovery=_Discovery()),
                            workspace_factory=lambda _ws_id: _ws(RESOURCES))


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        with project_context(None):
            job = db.get(Job, job_id)
            db.expunge(job)
            return job
    finally:
        db.close()


def test_the_backfill_tags_every_resource_and_reports_coverage():
    ids = _world()
    db = SessionLocal()
    try:
        job_id = ct.start_backfill(db, ADMIN, WS).id
    finally:
        db.close()
    writer = _Writer()
    _run(job_id, writer)
    job = _job(job_id)
    assert job.status == "succeeded", job.error
    assert job.project_id is None and job.payload["platform"] is True
    keys = {r["key"]: r for r in job.payload["resources"]}
    tagged = dict(writer.calls)
    agent_tags = {"launchpad:project": "alpha", "launchpad:workspace": WS,
                  "launchpad:agent-id": ids["runtime"], **MANAGED,
                  "launchpad:department": "risk", "CostCenter": "1234"}
    assert tagged["runtime:arn:rt/rt-1"] == agent_tags
    assert tagged["runtime_endpoint:arn:rt/rt-1/runtime-endpoint/live"] == agent_tags
    assert tagged["harness:arn:harness/h-1"]["launchpad:agent-id"] == ids["harness"]
    assert "runtime:arn:rt/theirs" not in keys  # a discovered runtime is not ours
    assert any(k.startswith("iam_role:launchpad-agent-svc-") for k in tagged)
    mem = f"memory:arn:aws:bedrock-agentcore:{REGION}:{ACCT}:memory/team_mem-1"
    assert tagged[mem]["launchpad:project"] == "alpha" and "launchpad:agent-id" not in tagged[mem]
    assert tagged["secret:gh"]["launchpad:project"] == "alpha"
    assert tagged["credential_provider:arn:provider/gh"]["CostCenter"] == "1234"
    # the workspace's own infrastructure is shared
    assert tagged["gateway:arn:gw/shared"] == {"launchpad:project": "shared",
                                               "launchpad:workspace": WS, **MANAGED}
    assert tagged["codebuild_project:launchpad-agent-builder"]["launchpad:project"] == "shared"
    assert tagged["iam_role:launchpad-agent-execution-role"]["launchpad:project"] == "shared"
    # a KB without a creator row belongs to the workspace's default project
    kb = f"knowledge_base:arn:aws:bedrock:{REGION}:{ACCT}:knowledge-base/KB1"
    assert keys[kb]["status"] == ct.TAGGED
    assert keys["data_source:KB1/DS1"]["status"] == ct.UNSUPPORTED
    cov = job.payload["coverage"]
    assert cov["by_status"][ct.DENIED] == 1  # the runtime's log group
    assert cov["by_status"][ct.PENDING] == 0
    assert cov["taggable"] == cov["total"] - 1
    assert cov["percent"] == round(100.0 * cov["tagged"] / cov["taggable"], 1) < 100
    view = ct.backfill_view(SessionLocal(), WS)
    assert view["run"]["id"] == job_id
    assert [r["kind"] for r in view["run"]["resources"]] == [ct.LOG_GROUP]  # what to fix


def test_a_restarted_backfill_continues_where_it_stopped():
    _world()
    db = SessionLocal()
    try:
        job_id = ct.start_backfill(db, ADMIN, WS).id
    finally:
        db.close()
    first = _Writer(crash_on="harness:arn:harness/h-1")
    _run(job_id, first)
    job = _job(job_id)
    assert job.status == "failed" and "process died" in job.error
    done = {r["key"] for r in job.payload["resources"] if r["status"] != ct.PENDING}
    assert done and "harness:arn:harness/h-1" not in done
    db = SessionLocal()
    try:
        with project_context(None):
            db.get(Job, job_id).status = "running"  # what a restart finds
            db.commit()
    finally:
        db.close()
    second = _Writer()
    _run(job_id, second)
    job = _job(job_id)
    assert job.status == "succeeded"
    again = {key for key, _ in second.calls}
    assert "harness:arn:harness/h-1" in again
    assert not again & {k for k in done if not k.startswith("data_source:")}  # not re-tagged
    assert job.payload["coverage"]["by_status"][ct.PENDING] == 0


def test_a_transfer_run_tags_only_the_agent_with_its_new_project():
    ids = _world()
    _project("beta")
    db = SessionLocal()
    try:
        source = db.get(Project, "alpha")
        [job_id] = projects_service.transfer_agent(db, ADMIN, source, ids["runtime"],
                                                   "beta")["retag_job_ids"]
        db.commit()
    finally:
        db.close()
    writer = _Writer()
    _run(job_id, writer)
    assert _job(job_id).status == "succeeded"
    tagged = dict(writer.calls)
    assert tagged["runtime:arn:rt/rt-1"]["launchpad:project"] == "beta"
    assert "CostCenter" not in tagged["runtime:arn:rt/rt-1"]  # alpha's cost tag is gone
    assert not any(k.startswith(("harness:", "memory:", "gateway:")) for k in tagged)


def test_an_identical_queued_run_is_reused():
    db = SessionLocal()
    try:
        first = ct.start_backfill(db, ADMIN, WS)
        second = ct.start_backfill(db, ADMIN, WS)
        assert first.id == second.id
    finally:
        db.close()


def test_the_backfill_is_a_resumable_job_type():
    from app.services import inflight

    assert ct.JOB_TYPE in inflight.resumable_job_types()


# ── stale tags (E2E-projects: a transferred runtime kept its old cost center) ──


class _UntagWriter(_Writer):
    def __init__(self):
        super().__init__()
        self.removed: dict[str, list[str]] = {}

    def untag(self, workspace, item, keys):
        self.removed[item["key"]] = list(keys)
        return ct.TAGGED, ""


def test_a_transfer_run_removes_the_old_projects_cost_tags():
    """TagResource merges: writing beta's set leaves alpha's `CostCenter` and
    department on the runtime, still billing it to alpha's cost center — the run
    must take them off."""
    ids = _world()  # alpha: department risk, CostCenter 1234
    _project("beta", department_id=None)  # no department: alpha's comes off
    db = SessionLocal()
    try:
        source = db.get(Project, "alpha")
        [job_id] = projects_service.transfer_agent(db, ADMIN, source, ids["runtime"],
                                                   "beta")["retag_job_ids"]
        db.commit()
    finally:
        db.close()
    assert _job(job_id).payload["remove_keys"] == ["CostCenter", pt.TAG_DEPARTMENT]
    writer = _UntagWriter()
    _run(job_id, writer)
    job = _job(job_id)
    assert job.status == "succeeded"
    assert writer.removed["runtime:arn:rt/rt-1"] == ["CostCenter", pt.TAG_DEPARTMENT]
    row = next(r for r in job.payload["resources"] if r["key"] == "runtime:arn:rt/rt-1")
    assert row["status"] == ct.TAGGED and row["untagged"] == ["CostCenter", pt.TAG_DEPARTMENT]


def test_a_key_the_target_also_carries_is_rewritten_not_removed():
    ids = _world()
    _project("beta", department_id=None)
    db = SessionLocal()
    try:
        db.get(Project, "beta").settings = {"cost_tags": {"CostCenter": "9999"}}
        db.commit()
        [job_id] = projects_service.transfer_agent(
            db, ADMIN, db.get(Project, "alpha"), ids["runtime"], "beta")["retag_job_ids"]
        db.commit()
    finally:
        db.close()
    writer = _UntagWriter()
    _run(job_id, writer)
    assert dict(writer.calls)["runtime:arn:rt/rt-1"]["CostCenter"] == "9999"
    assert writer.removed["runtime:arn:rt/rt-1"] == [pt.TAG_DEPARTMENT]


def test_dropping_a_custom_tag_queues_its_removal():
    from app.services import billing_costs

    _alpha(cost={"CostCenter": "1234", "Team": "risk"})
    db = SessionLocal()
    try:
        row = db.get(Project, "alpha")
        billing_costs.update_custom_tags(db, ADMIN, row, {"CostCenter": "1234"})
        db.commit()
        with project_context(None):
            [job] = [j for j in db.query(Job).filter(Job.type == ct.JOB_TYPE)
                     if (j.payload or {}).get("reason") == "project_update"]
        assert job.payload["remove_keys"] == ["Team"]
    finally:
        db.close()


def test_untag_uses_each_services_own_call():
    clients = {
        "bedrock-agentcore-control": _Stub(), "iam": _Stub(), "ecr": _Stub(),
        "logs": _Stub(),
        "codebuild": _Stub(batch_get_projects={"projects": [{"name": "b", "tags": [
            {"key": "Owner", "value": "ops"}, {"key": "CostCenter", "value": "1"}]}]}),
    }
    ws = _ws(clients=clients)
    writer = ct.AwsTagWriter()
    keys = ["CostCenter"]
    for item in (ct.entry(ct.RUNTIME, arn="arn:rt"), ct.entry(ct.IAM_ROLE, name="r"),
                 ct.entry(ct.ECR_REPOSITORY, arn="arn:ecr"), ct.entry(ct.LOG_GROUP, arn="arn:lg"),
                 ct.entry(ct.CODEBUILD_PROJECT, name="b")):
        assert writer.untag(ws, item, keys) == (ct.TAGGED, "")
    assert clients["bedrock-agentcore-control"].calls == [
        ("untag_resource", {"resourceArn": "arn:rt", "tagKeys": keys})]
    assert clients["iam"].calls == [("untag_role", {"RoleName": "r", "TagKeys": keys})]
    assert clients["ecr"].calls == [("untag_resource", {"resourceArn": "arn:ecr",
                                                        "tagKeys": keys})]
    assert clients["logs"].calls == [("untag_resource", {"resourceArn": "arn:lg",
                                                         "tagKeys": keys})]
    assert clients["codebuild"].calls[-1] == ("update_project", {"name": "b", "tags": [
        {"key": "Owner", "value": "ops"}]})
    denied = _ws(clients={"bedrock-agentcore-control": _Stub(
        fail={"untag_resource": _err("AccessDeniedException", "UntagResource")})})
    assert writer.untag(denied, ct.entry(ct.RUNTIME, arn="arn:rt"), keys)[0] == ct.DENIED
