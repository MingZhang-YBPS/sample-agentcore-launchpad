"""Phase 2 isolation, knowledge bases (projects design §10.5, R10-kb): the project KB
service role and its S3 prefix, the `kb-role` provisioner, new KBs of an isolated
project, the explicit move of an existing KB, and the KB gateway's per-project view.

Everything AWS is stubbed (IAM, bedrock-agent, S3, the gateway control plane). The
policy checks reuse R10-iam's offline evaluator (`tests/test_project_iam._allowed`).
"""

import json
from typing import Any

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project, project_context
from app.models.project_isolation import (
    KIND_KB_ROLE,
    KIND_M2M_CLIENT,
    STATUS_FAILED,
    STATUS_READY,
    ProjectResource,
)
from app.services import creators, kb_gateway, knowledge, project_kb
from app.services import project_isolation as svc
from app.services.workspace_iam import ForeignResourceError
from tests.conftest import ws_ctx
from tests.test_project_iam import StubIam, _allowed, _isolate
from tests.test_project_scope import _project, app  # noqa: F401

ACCT = "111122223333"
REGION = "us-west-2"
WS = DEFAULT_WORKSPACE_ID
BUCKET = "lp-artifacts"
SHARED_ROLE = f"arn:aws:iam::{ACCT}:role/launchpad-kb-role"
ALPHA_ROLE = f"arn:aws:iam::{ACCT}:role/launchpad-kb-role-alpha"


# ── names and the policy ──────────────────────────────────────────────────────


def test_role_name_follows_the_regional_rule_and_fits_iam():
    assert project_kb.kb_role_name("alpha", "us-west-2") == "launchpad-kb-role-alpha"
    assert project_kb.kb_role_name("alpha", "us-east-1") == "launchpad-kb-role-alpha-us-east-1"
    long_id = "a" * 32
    name = project_kb.kb_role_name(long_id, "ap-southeast-2")
    assert len(name) <= 64 and name.endswith("-ap-southeast-2")
    assert name == project_kb.kb_role_name(long_id, "ap-southeast-2")  # deterministic
    assert name != project_kb.kb_role_name("a" * 31 + "b", "ap-southeast-2")


def test_the_role_reads_only_the_projects_kb_prefix():
    doc = project_kb.kb_role_policy("alpha", BUCKET, REGION)
    obj = f"arn:aws:s3:::{BUCKET}/"
    assert _allowed(doc, "s3:GetObject", obj + "projects/alpha/kb/KB1/a.pdf")
    for other in ("kb/KB1/a.pdf", "projects/beta/kb/KB1/a.pdf", "projects/alpha/agents/x",
                  "projects/alpha-two/kb/KB1/a.pdf"):
        assert not _allowed(doc, "s3:GetObject", obj + other), other
    bucket = f"arn:aws:s3:::{BUCKET}"
    assert _allowed(doc, "s3:ListBucket", bucket, {"s3:prefix": "projects/alpha/kb/KB1/"})
    assert not _allowed(doc, "s3:ListBucket", bucket, {"s3:prefix": "kb/"})
    assert not _allowed(doc, "s3:ListBucket", bucket, {"s3:prefix": "projects/beta/kb/"})


def test_the_kb_provisioner_is_registered():
    assert isinstance(svc.get_provisioners().kb, project_kb.KbRoleProvisioner)


# ── the role ──────────────────────────────────────────────────────────────────


def _ensure(iam: StubIam, pid: str = "alpha", **kw: Any) -> project_kb.KbRole:
    return project_kb.ensure_kb_role(iam, project_id=pid, workspace_id=WS, account_id=ACCT,
                                     region=REGION, artifacts_bucket=BUCKET, **kw)


def test_ensure_role_creates_tags_and_converges():
    iam = StubIam()
    seen: list[str] = []
    role = _ensure(iam, on_exists=lambda r: seen.append(r.name))
    assert role.created and role.arn == ALPHA_ROLE and seen == ["launchpad-kb-role-alpha"]
    stored = iam.roles["launchpad-kb-role-alpha"]
    tags = {t["Key"]: t["Value"] for t in stored["tags"]}
    assert tags == {"launchpad:project": "alpha", "launchpad:workspace": WS,
                    "launchpad:managed": "true"}
    assert "launchpad:isolation" not in tags  # the agent-role CreateRole condition
    assert stored["policies"]["launchpad-kb"] == project_kb.kb_role_policy("alpha", BUCKET,
                                                                           REGION)
    iam.list_role_tags = lambda RoleName: {"Tags": iam.roles[RoleName]["tags"]}  # noqa: N803
    again = _ensure(iam)
    assert not again.created and again.arn == ALPHA_ROLE


def test_a_role_not_tagged_for_the_project_is_refused():
    iam = StubIam()
    iam.roles["launchpad-kb-role-alpha"] = {"tags": [{"Key": "launchpad:project",
                                                      "Value": "beta"}], "policies": {}}
    iam.list_role_tags = lambda RoleName: {"Tags": iam.roles[RoleName]["tags"]}  # noqa: N803
    with pytest.raises(ForeignResourceError):
        _ensure(iam)
    assert iam.roles["launchpad-kb-role-alpha"]["policies"] == {}


# ── the stage ─────────────────────────────────────────────────────────────────


class _Workspace:
    """A workspace context whose clients are stubs (WorkspaceContext is frozen)."""

    def __init__(self, clients: dict[str, Any], resources: dict[str, Any] | None = None):
        ctx = ws_ctx(resources if resources is not None
                     else {"artifacts_bucket": BUCKET, "kb_role_arn": SHARED_ROLE},
                     account_id=ACCT, region=REGION)
        self.account_id, self.region, self.id = ctx.account_id, ctx.region, ctx.id
        self.partition, self.resources = ctx.partition, ctx.resources
        self._clients = clients

    def client(self, service: str, *args: Any, **kwargs: Any) -> Any:
        return self._clients[service]


class _NoQuota:
    def check(self, ctx):
        return []


@pytest.fixture
def alpha(app):  # noqa: F811
    _project("alpha")
    _isolate("alpha")
    return "alpha"


def _run_stage(iam: StubIam, resources: dict[str, Any] | None = None) -> dict[str, Any]:
    db = SessionLocal()
    try:
        job, _ = svc.queue_provision(db, "alpha", WS, actor="test")
        job_id = job.id
    finally:
        db.close()
    provisioners = svc.Provisioners(iam=svc._PENDING, memory=svc._PENDING,
                                    kb=project_kb.KbRoleProvisioner(), gateway=svc._PENDING,
                                    storage=svc._PENDING, quotas=_NoQuota())
    svc.execute_provision_job(job_id, provisioners,
                              workspace_factory=lambda _ws: _Workspace({"iam": iam}, resources))
    db = SessionLocal()
    try:
        with project_context(None):
            return {e["name"]: e for e in svc.job_stages(db.get(svc.Job, job_id))}
    finally:
        db.close()


def _row(pid: str = "alpha", kind: str = KIND_KB_ROLE) -> ProjectResource | None:
    db = SessionLocal()
    try:
        return db.get(ProjectResource, (pid, WS, kind))
    finally:
        db.close()


def test_the_kb_role_stage_provisions_and_records(alpha):
    iam = StubIam()
    stages = _run_stage(iam)
    assert stages["kb-role"]["status"] == "succeeded", stages["kb-role"]
    assert "projects/alpha/kb/" in stages["kb-role"]["detail"]
    row = _row()
    assert row.status == STATUS_READY and row.arn == ALPHA_ROLE
    assert row.resource_id == "launchpad-kb-role-alpha"
    assert row.detail == {"bucket": BUCKET, "s3_prefix": "projects/alpha/kb/"}


def test_the_kb_role_stage_fails_without_an_artifacts_bucket(alpha):
    stages = _run_stage(StubIam(), resources={})
    assert stages["kb-role"]["status"] == "failed"
    assert "artifacts_bucket" in stages["kb-role"]["detail"]
    assert _row() is None


def test_the_stage_allows_the_projects_m2m_client_on_the_kb_gateway(alpha, monkeypatch):
    allowed: list[tuple[str, str]] = []
    monkeypatch.setattr(kb_gateway, "ensure_gateway_allows_client",
                        lambda _c, gw, cid: allowed.append((gw, cid)))
    import app.services.agentcore.client as ac_client

    monkeypatch.setattr(ac_client, "control_client", lambda _ws: object())
    ctx = svc.ProvisionContext(
        project_id="alpha", workspace_id=WS, job_id="j", settings={},
        names=svc.resource_names("alpha"),
        resources={KIND_M2M_CLIENT: {"resource_id": "client-alpha"}},
        _workspace=_Workspace({"iam": StubIam()}, {"artifacts_bucket": BUCKET,
                                                   "kb_gateway_id": "kbgw-1"}),
    )
    detail = project_kb.KbRoleProvisioner().ensure_kb_role(ctx)
    assert allowed == [("kbgw-1", "client-alpha")]
    assert "M2M client allowed" in detail


# ── new KBs ───────────────────────────────────────────────────────────────────


def _record_role(pid: str = "alpha", status: str = STATUS_READY,
                 arn: str = ALPHA_ROLE) -> None:
    db = SessionLocal()
    try:
        db.add(ProjectResource(project_id=pid, workspace_id=WS, kind=KIND_KB_ROLE,
                               resource_id=arn.rsplit("/", 1)[-1], arn=arn, status=status))
        db.commit()
    finally:
        db.close()


def test_a_shared_project_keeps_the_shared_role_and_path(app):  # noqa: F811
    _project("beta")
    workspace = _Workspace({})
    placement = project_kb.placement_for_new_kb(workspace, "beta")
    assert placement == project_kb.KbPlacement(role_arn=SHARED_ROLE)
    assert placement.prefix("KB1") == "kb/KB1/" and placement.tags == {}
    assert project_kb.placement_for_new_kb(workspace, None).role_arn == SHARED_ROLE


@pytest.mark.parametrize("status", [None, STATUS_FAILED])
def test_an_isolated_project_without_its_role_is_refused(alpha, status):
    if status:
        _record_role(status=status)
    with pytest.raises(AppError) as ei:
        project_kb.placement_for_new_kb(_Workspace({}), "alpha")
    assert ei.value.code == "project.kb_role_unavailable" and ei.value.status_code == 409


class _Bedrock:
    """bedrock-agent stub: one KB, its data sources, ingestion jobs."""

    class ResourceNotFoundException(Exception):
        pass

    class ValidationException(Exception):
        response = {"Error": {"Code": "ValidationException"}}

    class ConflictException(Exception):
        pass

    def __init__(self, role_arn: str = SHARED_ROLE, sources: dict[str, tuple] | None = None):
        self.exceptions = self
        self.kb = {"knowledgeBaseId": "KB1", "name": "docs", "status": "ACTIVE",
                   "roleArn": role_arn, "knowledgeBaseArn": f"arn:aws:bedrock:{REGION}:{ACCT}:"
                   "knowledge-base/KB1", "knowledgeBaseConfiguration": {"type": "MANAGED"}}
        self.sources = dict(sources or {})  # ds id → (bucket, prefix)
        self.jobs: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.refuse_role_update = False
        self.project_seen: list[str | None] = []

    def get_knowledge_base(self, knowledgeBaseId):  # noqa: N803
        self.project_seen.append(current_project())
        return {"knowledgeBase": dict(self.kb)}

    def create_knowledge_base(self, **kw):
        self.calls.append(("create_knowledge_base", kw))
        self.kb["roleArn"] = kw["roleArn"]
        return {"knowledgeBase": {"knowledgeBaseId": "KB1"}}

    def update_knowledge_base(self, **kw):
        if self.refuse_role_update and kw["roleArn"] != self.kb["roleArn"]:
            raise self.ValidationException("roleArn cannot be changed")
        self.calls.append(("update_knowledge_base", kw))
        self.kb["roleArn"] = kw["roleArn"]

    def tag_resource(self, **kw):
        self.calls.append(("tag_resource", kw))

    def list_data_sources(self, **_kw):
        return {"dataSourceSummaries": [{"dataSourceId": ds} for ds in self.sources]}

    def get_data_source(self, knowledgeBaseId, dataSourceId):  # noqa: N803
        bucket, prefix = self.sources[dataSourceId]
        conn = {"connectionConfiguration": {"bucketName": bucket},
                "filterConfiguration": {"inclusionPrefixes": [prefix]}}
        return {"dataSource": {"name": dataSourceId, "status": "AVAILABLE",
                               "dataSourceConfiguration": {
                                   "managedKnowledgeBaseConnectorConfiguration": {
                                       "connectorParameters": json.dumps(conn)}}}}

    def create_data_source(self, **kw):
        self.calls.append(("create_data_source", kw))
        ds = f"ds-{len(self.sources) + 1}"
        conn = kw["dataSourceConfiguration"]["managedKnowledgeBaseConnectorConfiguration"][
            "connectorParameters"]
        prefixes = (conn.get("filterConfiguration") or {}).get("inclusionPrefixes") or [""]
        self.sources[ds] = (conn["connectionConfiguration"]["bucketName"], prefixes[0])
        return {"dataSource": {"dataSourceId": ds}}

    def delete_data_source(self, knowledgeBaseId, dataSourceId):  # noqa: N803
        self.calls.append(("delete_data_source", {"ds": dataSourceId}))
        self.sources.pop(dataSourceId, None)

    def delete_knowledge_base(self, knowledgeBaseId):  # noqa: N803
        self.calls.append(("delete_knowledge_base", {}))

    def start_ingestion_job(self, knowledgeBaseId, dataSourceId):  # noqa: N803
        self.calls.append(("start_ingestion_job", {"ds": dataSourceId}))
        job = {"ingestionJobId": "job-1", "status": "STARTING"}
        self.jobs.setdefault(dataSourceId, []).append(job)
        return {"ingestionJob": job}

    def list_ingestion_jobs(self, knowledgeBaseId, dataSourceId, **_kw):  # noqa: N803
        return {"ingestionJobSummaries": self.jobs.get(dataSourceId, [])}

    def called(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


class _S3:
    def __init__(self, keys: list[str] | None = None):
        self.objects = {k: b"x" for k in keys or []}
        self.put: list[str] = []

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.put.append(Key)
        self.objects[Key] = Body

    def list_objects_v2(self, Bucket, Prefix="", **_kw):  # noqa: N803
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def copy_object(self, Bucket, Key, CopySource):  # noqa: N803
        self.objects[Key] = self.objects[CopySource["Key"]]


class _PolicyIam:
    def __init__(self):
        self.put: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):  # noqa: N803
        self.put.append((RoleName, PolicyName))

    def delete_role_policy(self, RoleName, PolicyName):  # noqa: N803
        self.deleted.append((RoleName, PolicyName))


def _wire(monkeypatch, bedrock: _Bedrock, s3: _S3 | None = None,
          iam: _PolicyIam | None = None) -> _Workspace:
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: bedrock)
    return _Workspace({"s3": s3 or _S3(), "iam": iam or _PolicyIam()})


def test_a_new_kb_of_an_isolated_project_uses_the_project_role_and_tag(alpha, monkeypatch):
    _record_role()
    bedrock = _Bedrock()
    workspace = _wire(monkeypatch, bedrock)
    started: list[str] = []
    monkeypatch.setattr(knowledge, "_start_source_completion",
                        lambda _ws, kb_id, _src: started.append(kb_id))
    with project_context("alpha"):
        knowledge.create_kb(workspace, "docs", "", {"mode": "upload"})
    (kw,) = bedrock.called("create_knowledge_base")
    assert kw["roleArn"] == ALPHA_ROLE and kw["tags"]["launchpad:project"] == "alpha"
    # cost tags (cost-tags design §1.2) ride along
    assert kw["tags"]["launchpad:managed-by"] == "launchpad"
    assert started == ["KB1"]


def test_a_new_kb_of_a_shared_project_keeps_the_shared_role(app, monkeypatch):  # noqa: F811
    bedrock = _Bedrock()
    workspace = _wire(monkeypatch, bedrock)
    monkeypatch.setattr(knowledge, "_start_source_completion", lambda *_a: None)
    with project_context("default"):
        knowledge.create_kb(workspace, "docs", "", {"mode": "upload"})
    (kw,) = bedrock.called("create_knowledge_base")
    # the shared role and layout; only the cost tags (cost-tags design §1.1) are new
    assert kw["roleArn"] == SHARED_ROLE
    assert kw["tags"] == {"launchpad:project": "default", "launchpad:workspace": WS,
                          "launchpad:managed-by": "launchpad"}


@pytest.mark.parametrize(("role", "key"), [
    (ALPHA_ROLE, "projects/alpha/kb/KB1/a.txt"),
    (SHARED_ROLE, "kb/KB1/a.txt"),  # an alpha KB from before isolation keeps its path
])
def test_uploads_follow_the_kbs_role(alpha, monkeypatch, role, key):
    _record_role()
    s3 = _S3()
    workspace = _wire(monkeypatch, _Bedrock(role_arn=role), s3)
    assert knowledge.upload_files(workspace, "KB1", [("a.txt", b"hi")]) == [key]
    assert s3.put == [key]


def test_the_source_completion_uses_the_project_prefix_inside_the_project(alpha, monkeypatch):
    _record_role()
    bedrock = _Bedrock(role_arn=ALPHA_ROLE)
    workspace = _wire(monkeypatch, bedrock)
    with project_context("alpha"):
        thread = knowledge._start_source_completion(workspace, "KB1", {"mode": "upload"},
                                                    interval_s=0)
    thread.join(timeout=5)
    (kw,) = bedrock.called("create_data_source")
    conn = kw["dataSourceConfiguration"]["managedKnowledgeBaseConnectorConfiguration"][
        "connectorParameters"]
    assert conn["filterConfiguration"]["inclusionPrefixes"] == ["projects/alpha/kb/KB1/"]
    assert bedrock.project_seen == ["alpha"]  # the thread ran inside the project


def test_external_sources_are_granted_on_the_kbs_own_role(alpha, monkeypatch):
    _record_role()
    iam = _PolicyIam()
    workspace = _wire(monkeypatch, _Bedrock(role_arn=ALPHA_ROLE), iam=iam)
    knowledge.add_data_source(workspace, "KB1", {"mode": "existing", "bucket": "corp-docs"})
    assert iam.put == [("launchpad-kb-role-alpha", "launchpad-kb-KB1")]
    monkeypatch.setattr(knowledge, "get_kb_detail", lambda *_a: {})
    knowledge.delete_kb(workspace, "KB1")
    assert iam.deleted == [("launchpad-kb-role-alpha", "launchpad-kb-KB1")]


def test_a_shared_kbs_external_grant_stays_on_the_shared_role(app, monkeypatch):  # noqa: F811
    iam = _PolicyIam()
    workspace = _wire(monkeypatch, _Bedrock(), iam=iam)
    knowledge.add_data_source(workspace, "KB1", {"mode": "existing", "bucket": "corp-docs"})
    assert iam.put == [("launchpad-kb-role", "launchpad-kb-KB1")]


# ── moving an existing KB ─────────────────────────────────────────────────────


def _own_kb(pid: str, kb_id: str = "KB1") -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            creators.record(db, WS, creators.KB, kb_id, creators.Creator("u1", "ann"))
    finally:
        db.close()


def test_migrate_copies_switches_the_role_tags_and_adds_a_source(alpha, monkeypatch):
    _record_role()
    _own_kb("alpha")
    bedrock = _Bedrock(sources={"ds-old": (BUCKET, "kb/KB1/"),
                                "ds-ext": ("corp-docs", "manuals/")})
    s3 = _S3(["kb/KB1/a.pdf", "kb/KB1/b.pdf", "kb/KB2/other.pdf"])
    iam = _PolicyIam()
    workspace = _wire(monkeypatch, bedrock, s3, iam)

    out = project_kb.migrate_kb(workspace, "KB1", "alpha")

    assert out["copied"] == 2 and out["state"] == "syncing"
    assert {"projects/alpha/kb/KB1/a.pdf", "projects/alpha/kb/KB1/b.pdf"} <= set(s3.objects)
    assert "projects/alpha/kb/KB2/other.pdf" not in s3.objects
    assert iam.put == [("launchpad-kb-role-alpha", "launchpad-kb-KB1")]  # before the switch
    (update,) = bedrock.called("update_knowledge_base")
    assert update["roleArn"] == ALPHA_ROLE
    (tag,) = bedrock.called("tag_resource")
    assert tag["tags"] == {"launchpad:project": "alpha"}
    assert bedrock.sources[out["data_source"]] == (BUCKET, "projects/alpha/kb/KB1/")
    assert "ds-old" in bedrock.sources  # 先加后减: the old source stays until finish
    assert bedrock.called("start_ingestion_job") == [{"ds": out["data_source"]}]

    # a re-run is harmless: no second source, no second role update
    again = project_kb.migrate_kb(workspace, "KB1", "alpha")
    assert again["data_source"] == out["data_source"]
    assert len(bedrock.called("update_knowledge_base")) == 1

    with pytest.raises(AppError) as ei:
        project_kb.finish_kb_migration(workspace, "KB1", "alpha")
    assert ei.value.code == "kb.migration_syncing"
    bedrock.jobs[out["data_source"]] = [{"ingestionJobId": "job-2", "status": "COMPLETE",
                                         "startedAt": "2026-10-15T00:00:00"}]
    done = project_kb.finish_kb_migration(workspace, "KB1", "alpha")
    assert done["deleted_data_sources"] == ["ds-old"] and "ds-old" not in bedrock.sources
    assert ("launchpad-kb-role", "launchpad-kb-KB1") in iam.deleted


def test_migrate_reports_a_refused_role_update(alpha, monkeypatch):
    _record_role()
    _own_kb("alpha")
    bedrock = _Bedrock(sources={"ds-old": (BUCKET, "kb/KB1/")})
    bedrock.refuse_role_update = True
    workspace = _wire(monkeypatch, bedrock, _S3(["kb/KB1/a.pdf"]))
    with pytest.raises(AppError) as ei:
        project_kb.migrate_kb(workspace, "KB1", "alpha")
    assert ei.value.code == "kb.role_update_unsupported" and ei.value.status_code == 409
    assert bedrock.kb["roleArn"] == SHARED_ROLE
    assert bedrock.called("create_data_source") == []


def test_another_projects_kb_answers_404(alpha, monkeypatch):
    _record_role()
    _project("beta")
    _own_kb("beta")
    workspace = _wire(monkeypatch, _Bedrock())
    with pytest.raises(NotFoundError):
        project_kb.migrate_kb(workspace, "KB1", "alpha")
    with pytest.raises(NotFoundError):
        project_kb.finish_kb_migration(workspace, "KB1", "alpha")


# ── the KB gateway per project ────────────────────────────────────────────────


class _Control:
    def __init__(self, names: list[str]):
        self.names = names

    def list_gateway_targets(self, **_kw):
        return {"items": [{"name": n, "targetId": n} for n in self.names]}


def test_project_targets_split_the_shared_kb_gateway(alpha):
    from app.models.ledger import Agent

    _project("beta")
    _own_kb("alpha", "KBA")
    _own_kb("beta", "KBB")
    db = SessionLocal()
    try:
        for pid, name in (("alpha", "Alpha Bot"), ("beta", "Beta Bot")):
            with project_context(pid):
                db.add(Agent(workspace_id=WS, project_id=pid, name=name, method="harness",
                             spec={"method": "harness"}, version="1", status="active"))
                db.commit()
    finally:
        db.close()
    control = _Control([
        kb_gateway.retrieve_target_name("KBA", "Agentic docs"),  # starts with agentic-
        kb_gateway.retrieve_target_name("KBB", "beta docs"),
        kb_gateway.agentic_target_name("Alpha Bot"),
        kb_gateway.agentic_target_name("Beta Bot"),
    ])
    assert kb_gateway.project_targets(control, "gw", WS, "alpha") == [
        "agentic-alpha-bot", "agentic-docs-kba"]
    assert kb_gateway.project_targets(control, "gw", WS, "beta") == [
        "agentic-beta-bot", "beta-docs-kbb"]


def test_the_kb_gateway_allows_the_projects_m2m_clients(alpha, monkeypatch):
    db = SessionLocal()
    try:
        db.add(ProjectResource(project_id="alpha", workspace_id=WS, kind=KIND_M2M_CLIENT,
                               resource_id="client-alpha", status=STATUS_READY))
        db.commit()
    finally:
        db.close()
    assert kb_gateway.project_client_ids(WS) == ["client-alpha"]

    class _Gw:
        created: dict = {}

        def list_gateways(self, **_kw):
            return {"items": []}

        def create_gateway(self, **kw):
            self.created = kw
            return {"gatewayId": "gw-1"}

        def get_gateway(self, gatewayIdentifier):  # noqa: N803
            return {"gatewayId": "gw-1", "gatewayArn": "arn:gw", "gatewayUrl": "https://gw"}

    monkeypatch.setattr(kb_gateway, "_wait_gateway_ready", lambda *_a: None)
    gw = _Gw()
    resources = {"user_pool_client_id": "console", "m2m_client_id": "m2m",
                 "user_pool_id": "pool", "gateway_role_arn": "arn:role"}
    kb_gateway.ensure_kb_gateway(gw, resources, REGION, kb_gateway.project_client_ids(WS))
    assert gw.created["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == [
        "console", "m2m", "client-alpha"]
    assert not kb_gateway.allow_project_client(object(), ws_ctx({}), "client-alpha")
