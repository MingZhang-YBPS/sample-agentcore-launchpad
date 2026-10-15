"""§10.11 migration of existing agents into an isolated project (R10-migrate): the
resumable `project_isolate` run (grant-new → republish → verify → switch per agent,
tag-backfill, revert, retire), its API, and IAM-simulation-style checks that no probe
call is denied while a republish rolls and that one project's role cannot reach
another's resources. No AWS: IAM, the republish and the invoker are fakes."""

import fnmatch
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import current_project, project_context
from app.models.ledger import Agent, Job, ManagedMemory
from app.models.project_isolation import (
    KIND_BOUNDARY_POLICY,
    KIND_MEMORY,
    STATUS_READY,
    ProjectResource,
)
from app.models.projects import Project
from app.schemas.agent import AgentSpec
from app.services import agent_iam, inflight, project_iam
from app.services import project_isolation as isolation
from app.services import project_migration as svc
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
ACCT = "111122223333"
REGION = "us-west-2"
LEGACY_MEM = "launchpad_mem-OLD123"
PROVIDER = (f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:token-vault/default/"
            "oauth2credentialprovider/launchpad-kb-gw")


def _mem_id(pid: str) -> str:
    return f"{isolation.resource_names(pid).memory}-abc123"


def _mem_arn(memory_id: str) -> str:
    return f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:memory/{memory_id}"


# ── a small IAM simulator ─────────────────────────────────────────────────────


def _match(patterns: Any, value: str) -> bool:
    items = patterns if isinstance(patterns, list) else [patterns]
    return any(fnmatch.fnmatchcase(value.lower(), str(p).lower()) for p in items)


def _condition_holds(condition: dict[str, Any] | None, context: dict[str, str]) -> bool:
    for op, pairs in (condition or {}).items():
        for key, want in pairs.items():
            wants = want if isinstance(want, list) else [want]
            have = context.get(key)
            if have is None:
                if op.endswith("IfExists"):
                    continue
                return False
            if op.startswith("StringLike"):
                if not any(fnmatch.fnmatchcase(have, w) for w in wants):
                    return False
            elif have not in wants:
                return False
    return True


def allows(document: dict[str, Any], action: str, resource: str,
           context: dict[str, str] | None = None) -> bool:
    """Whether any Allow statement of `document` covers the request (no Deny in the
    documents under test)."""
    for st in document.get("Statement", []):
        if st.get("Effect") != "Allow":
            continue
        if not _match(st.get("Action"), action) or not _match(st.get("Resource"), resource):
            continue
        if _condition_holds(st.get("Condition"), context or {}):
            return True
    return False


def decision(iam: "FakeIam", role: str, action: str, resource: str,
             context: dict[str, str] | None = None) -> str:
    """`iam:SimulatePrincipalPolicy`'s answer: identity ∩ boundary."""
    identity = any(allows(doc, action, resource, context)
                   for doc in iam.inline.get(role, {}).values())
    boundary_arn = iam.boundaries.get(role)
    bounded = (boundary_arn is None
               or allows(iam.policies[boundary_arn]["doc"], action, resource, context))
    return "allowed" if identity and bounded else "implicitDeny"


class NoSuchEntity(Exception):
    pass


class FakeIam:
    def __init__(self):
        self.roles: set[str] = set()
        self.tags: dict[str, dict[str, str]] = {}
        self.inline: dict[str, dict[str, dict[str, Any]]] = {}
        self.boundaries: dict[str, str] = {}
        self.policies: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def _role(self, name):
        if name not in self.roles:
            raise NoSuchEntity(f"NoSuchEntity: role {name}")

    def get_role(self, RoleName):  # noqa: N803 — boto3's casing
        self._role(RoleName)
        return {"Role": {"Arn": f"arn:aws:iam::{ACCT}:role/{RoleName}"}}

    def tag_role(self, RoleName, Tags):  # noqa: N803
        self.calls.append(f"tag_role {RoleName}")
        self.tags.setdefault(RoleName, {}).update({t["Key"]: t["Value"] for t in Tags})

    def untag_role(self, RoleName, TagKeys):  # noqa: N803
        for key in TagKeys:
            self.tags.get(RoleName, {}).pop(key, None)

    def put_role_permissions_boundary(self, RoleName, PermissionsBoundary):  # noqa: N803
        self.calls.append(f"boundary {RoleName}")
        self.boundaries[RoleName] = PermissionsBoundary

    def delete_role_permissions_boundary(self, RoleName):  # noqa: N803
        if RoleName not in self.boundaries:
            raise NoSuchEntity("NoSuchEntity")
        del self.boundaries[RoleName]

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):  # noqa: N803
        self.calls.append(f"put {RoleName}/{PolicyName}")
        self.inline.setdefault(RoleName, {})[PolicyName] = json.loads(PolicyDocument)

    def delete_role_policy(self, RoleName, PolicyName):  # noqa: N803
        if PolicyName not in self.inline.get(RoleName, {}):
            raise NoSuchEntity("NoSuchEntity")
        self.calls.append(f"delete {RoleName}/{PolicyName}")
        del self.inline[RoleName][PolicyName]

    # managed policies (the project boundary)
    def get_policy(self, PolicyArn):  # noqa: N803
        if PolicyArn not in self.policies:
            raise NoSuchEntity("NoSuchEntity")
        p = self.policies[PolicyArn]
        return {"Policy": {"Arn": PolicyArn, "DefaultVersionId": f"v{p['version']}"}}

    def create_policy(self, PolicyName, PolicyDocument, Description, Tags):  # noqa: N803
        arn = f"arn:aws:iam::{ACCT}:policy/{PolicyName}"
        self.policies[arn] = {"doc": json.loads(PolicyDocument), "version": 1,
                              "tags": Tags}
        return {"Policy": {"Arn": arn, "DefaultVersionId": "v1"}}

    def list_policy_tags(self, PolicyArn):  # noqa: N803
        return {"Tags": self.policies[PolicyArn]["tags"]}

    def get_policy_version(self, PolicyArn, VersionId):  # noqa: N803
        return {"PolicyVersion": {"Document": self.policies[PolicyArn]["doc"]}}

    def list_policy_versions(self, PolicyArn):  # noqa: N803
        p = self.policies[PolicyArn]
        return {"Versions": [{"VersionId": f"v{p['version']}", "IsDefaultVersion": True}]}

    def create_policy_version(self, PolicyArn, PolicyDocument, SetAsDefault):  # noqa: N803
        p = self.policies[PolicyArn]
        p["version"] += 1
        p["doc"] = json.loads(PolicyDocument)
        return {"PolicyVersion": {"VersionId": f"v{p['version']}"}}

    def delete_policy_version(self, PolicyArn, VersionId):  # noqa: N803
        pass


class FakeControl:
    def __init__(self):
        self.tagged: dict[str, dict[str, str]] = {}

    def tag_resource(self, resourceArn, tags):  # noqa: N803
        self.tagged[resourceArn] = tags


def _workspace(iam: FakeIam, control: FakeControl | None = None):
    clients = {"iam": iam, "bedrock-agentcore-control": control or FakeControl()}
    return SimpleNamespace(
        id=WS, account_id=ACCT, region=REGION, partition="aws",
        resources={"memory_id": LEGACY_MEM, "artifacts_bucket": "lp-artifacts",
                   "oauth_provider_arn": PROVIDER, "ecr_repo": "launchpad-agents"},
        client=lambda name, **_kw: clients[name],
    )


# ── the fake runtime: which version serves, what memory it needs ─────────────


SPEC = {"name": "risk", "system_prompt": "p", "method": "zip_runtime",
        "memory": {"short_term": True}}


def _role_ctx(memory_id: str) -> agent_iam.RoleContext:
    return agent_iam.RoleContext(
        account_id=ACCT, region=REGION, artifacts_bucket="lp-artifacts",
        ecr_repo_arn=f"arn:aws:ecr:{REGION}:{ACCT}:repository/launchpad-agents",
        memory_id=memory_id)


class Runtime:
    """A runtime per agent: the serving version and the memory each version uses. A
    probe is one invoke: 200 when the role may write the version's memory, else the
    500 an AccessDenied inside the agent turns into."""

    def __init__(self, iam: FakeIam):
        self.iam = iam
        self.serving: dict[str, str] = {}  # agent id → memory id the version uses
        self.probes: list[tuple[str, str, int]] = []

    def probe(self, agent: Agent, when: str) -> int:
        role = agent_iam.role_name_for(agent.name, agent.id)
        memory = self.serving[agent.id]
        ok = decision(self.iam, role, "bedrock-agentcore:CreateEvent", _mem_arn(memory))
        status = 200 if ok == "allowed" else 500
        self.probes.append((agent.name, when, status))
        return status


class Republisher:
    """Plays the deploy pipeline's provision + deploy stages, probing in between."""

    def __init__(self, runtime: Runtime, target_memory, fail: set[str] | None = None):
        self.runtime, self.target_memory = runtime, target_memory
        self.fail = fail or set()
        self.started: list[str] = []
        self.jobs: dict[str, str] = {}

    def start(self, db, agent, *, actor, note):
        assert current_project() == agent.project_id
        job_id = f"deploy-{agent.name}-{len(self.started)}"
        self.started.append(agent.name)
        if agent.name in self.fail:
            self.jobs[job_id] = "failed"
            return job_id
        role = agent_iam.role_name_for(agent.name, agent.id)
        self.runtime.probe(agent, "before")
        # provision stage: the capability policy now names the target memory
        new_memory = self.target_memory(agent)
        self.runtime.iam.put_role_policy(
            RoleName=role, PolicyName=agent_iam.capability_policy_name(agent.name),
            PolicyDocument=json.dumps(agent_iam.policy_document(
                AgentSpec(**agent.spec), _role_ctx(new_memory))))
        self.runtime.probe(agent, "provisioned (old version serving)")
        self.runtime.serving[agent.id] = new_memory
        self.runtime.probe(agent, "rolled")
        agent.version = str(int(agent.version or "1") + 1)
        self.jobs[job_id] = "succeeded"
        return job_id

    def status(self, job_id):
        return self.jobs.get(job_id, "running"), "deploy failed" if self.jobs.get(
            job_id) == "failed" else None


class Invoker:
    def __init__(self, runtime: Runtime, broken: set[str] | None = None):
        self.runtime, self.broken = runtime, broken or set()
        self.calls: list[str] = []

    def health(self, ctx, agent):
        pass

    def invoke(self, ctx, agent, prompt):
        self.calls.append(agent.name)
        if agent.name in self.broken or self.runtime.probe(agent, "verify") != 200:
            raise svc.MigrationError("invoke answered 500")
        return "pong"


class Releases:
    def __init__(self, waiting: set[str] | None = None):
        self.waiting = waiting or set()

    def awaiting(self, db, agent):
        return agent.name in self.waiting


# ── fixtures ──────────────────────────────────────────────────────────────────


def _set_isolation(pid: str, **values: Any) -> None:
    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        raw = dict((row.settings or {}).get("isolation") or {})
        raw.update(values)
        row.settings = {**(row.settings or {}), "isolation": raw}
        db.commit()
    finally:
        db.close()


def _provisioned(pid: str) -> None:
    """What a finished `project_provision` run leaves: a succeeded job and the rows."""
    db = SessionLocal()
    try:
        with project_context(None):
            db.add(Job(workspace_id=WS, project_id=pid, type=isolation.JOB_TYPE,
                       status="succeeded",
                       payload={"stages": [{"name": s, "status": "succeeded"}
                                           for s in isolation.STAGE_ORDER]}))
            db.add(ProjectResource(project_id=pid, workspace_id=WS, kind=KIND_MEMORY,
                                   resource_id=_mem_id(pid), arn=_mem_arn(_mem_id(pid)),
                                   status=STATUS_READY))
            db.commit()
    finally:
        db.close()


def _agent(name: str, pid: str, *, status: str = "active", method: str = "zip_runtime",
           endpoint_mode: str = "default") -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=WS, name=name, method=method, status=status,
                        spec={**SPEC, "name": name}, version="1", endpoint_mode=endpoint_mode,
                        arn=f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:runtime/{name}-x")
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _load(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(Agent, agent_id)
            db.expunge(row)
            return row
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


def _agents(job_id: str) -> dict[str, dict[str, Any]]:
    return {a["name"]: a for a in _job(job_id).payload["agents"]}


class World(SimpleNamespace):
    pass


@pytest.fixture
def world(app, monkeypatch):  # noqa: F811
    _project("alpha")
    _set_isolation("alpha", mode="isolated")
    _provisioned("alpha")
    iam, control = FakeIam(), FakeControl()
    ws = _workspace(iam, control)
    runtime = Runtime(iam)
    monkeypatch.setattr(isolation, "get_provisioners", lambda: replace(
        isolation._provisioners, quotas=SimpleNamespace(check=lambda ctx: [])))

    def target(agent):
        return _mem_id(agent.project_id) if isolation.is_isolated(_project_row(
            agent.project_id)) else LEGACY_MEM

    ops = svc.MigrationOps(republisher=Republisher(runtime, target),
                           invoker=Invoker(runtime), releases=Releases(),
                           tagger=svc.ControlTagger(), poll_seconds=0,
                           republish_timeout_s=0.5, sleep=lambda _s: None)

    def add(name: str, pid: str = "alpha", **kw: Any) -> str:
        agent_id = _agent(name, pid, **kw)
        agent = _load(agent_id)
        role = agent_iam.role_name_for(agent.name, agent.id)
        iam.roles.add(role)
        iam.put_role_policy(RoleName=role, PolicyName=agent_iam.capability_policy_name(name),
                            PolicyDocument=json.dumps(agent_iam.policy_document(
                                AgentSpec(**agent.spec), _role_ctx(LEGACY_MEM))))
        runtime.serving[agent_id] = LEGACY_MEM
        return agent_id

    def queue(direction: str = svc.DIRECTION_ISOLATE, pid: str = "alpha") -> str:
        db = SessionLocal()
        try:
            job, created = svc._queue(db, pid, WS, direction, actor="admin",
                                      actor_user_id=None)
            assert created
            return job.id
        finally:
            db.close()

    def run(job_id: str, **changes: Any) -> Job:
        svc.execute_migration_job(job_id, replace(ops, **changes), lambda _ws: ws)
        return _job(job_id)

    return World(iam=iam, control=control, ws=ws, runtime=runtime, ops=ops, add=add,
                 queue=queue, run=run)


def _boundary_row(version: int, pid: str = "alpha") -> None:
    db = SessionLocal()
    try:
        row = db.get(ProjectResource, (pid, WS, KIND_BOUNDARY_POLICY))
        if row is None:
            row = ProjectResource(project_id=pid, workspace_id=WS, kind=KIND_BOUNDARY_POLICY)
            db.add(row)
        row.resource_id, row.status = f"launchpad-project-{pid}", STATUS_READY
        row.detail = {**(row.detail or {}), "version": version}
        db.commit()
    finally:
        db.close()


def _project_row(pid: str) -> Project:
    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        db.expunge(row)
        return row
    finally:
        db.close()


# ── isolate ───────────────────────────────────────────────────────────────────


def test_isolate_moves_every_running_agent_without_a_denied_call(world):
    risk = world.add("risk")
    world.add("fraud")
    world.add("draft-one", status="draft")
    job = world.run(world.queue())
    assert job.status == "succeeded", job.error
    agents = _agents(job.id)
    assert agents["risk"]["status"] == agents["fraud"]["status"] == svc.AGENT_DONE
    assert agents["draft-one"]["status"] == svc.AGENT_SKIPPED
    assert [s for s, v in agents["risk"]["steps"].items() if v["status"] == "succeeded"] == \
        svc.AGENT_STEPS[svc.DIRECTION_ISOLATE]
    # §10.15: the probe never saw a 5xx — before, while the old version served on the
    # new role, after the roll, and at verify
    assert world.runtime.probes and {s for *_x, s in world.runtime.probes} == {200}
    assert world.runtime.serving[risk] == _mem_id("alpha")
    # the role carries the boundary, its tags and the label
    role = agent_iam.role_name_for("risk", risk)
    assert world.iam.boundaries[role].endswith("policy/launchpad-project-alpha")
    assert world.iam.tags[role][project_iam.ISOLATION_TAG_KEY] == "isolated"
    assert _load(risk).isolation_applied == "boundary-v1"
    assert svc.BRIDGE_POLICY in world.iam.inline[role]


def test_tag_backfill_tags_runtimes_and_managed_memories(world):
    risk = world.add("risk")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            db.add(ManagedMemory(workspace_id=WS, memory_id="team-mem-1"))
            db.commit()
    finally:
        db.close()
    world.run(world.queue())
    tagged = world.control.tagged
    runtime_arn = _load(risk).arn
    assert tagged[runtime_arn] == {"launchpad:project": "alpha", "launchpad:workspace": WS,
                                   "launchpad:agent-id": risk}
    assert tagged[_mem_arn("team-mem-1")]["launchpad:project"] == "alpha"


def test_a_failed_verify_fails_the_run_and_a_retry_resumes_at_that_step(world, monkeypatch):
    world.add("risk")
    world.add("fraud")
    job = world.run(world.queue(), invoker=Invoker(world.runtime, broken={"fraud"}))
    assert job.status == "failed" and "fraud" in job.error
    agents = _agents(job.id)
    assert agents["risk"]["status"] == svc.AGENT_DONE
    assert agents["fraud"]["status"] == svc.AGENT_FAILED
    assert agents["fraud"]["steps"]["verify"]["status"] == svc.AGENT_FAILED

    monkeypatch.setattr(svc, "get_ops", lambda: world.ops)
    db = SessionLocal()
    try:
        svc.retry(db, SimpleNamespace(username="admin"), db.get(Project, "alpha"), job.id)
    finally:
        db.close()
    republished = list(world.ops.republisher.started)
    job = world.run(job.id)
    assert job.status == "succeeded", job.error
    # neither agent was republished again: risk was done, fraud resumed at verify
    assert world.ops.republisher.started == republished
    assert _agents(job.id)["fraud"]["status"] == svc.AGENT_DONE


def test_a_live_agent_waits_for_its_release_then_a_retry_switches_it(world, monkeypatch):
    world.add("live-one", endpoint_mode="live")
    releases = Releases(waiting={"live-one"})
    job = world.run(world.queue(), releases=releases)
    assert job.status == "succeeded"
    assert _agents(job.id)["live-one"]["status"] == svc.AGENT_AWAITING
    assert svc.run_state(job) == svc.STATE_AWAITING

    releases.waiting.clear()  # the candidate was signed
    monkeypatch.setattr(svc, "get_ops", lambda: world.ops)
    db = SessionLocal()
    try:
        svc.retry(db, SimpleNamespace(username="admin"), db.get(Project, "alpha"), job.id)
    finally:
        db.close()
    job = world.run(job.id, releases=releases)
    assert _agents(job.id)["live-one"]["status"] == svc.AGENT_DONE
    assert svc.run_state(job) == svc.STATE_DONE
    assert world.ops.republisher.started == ["live-one"]


def test_a_restart_resumes_and_waits_on_the_recorded_republish(world, monkeypatch):
    risk = world.add("risk")
    fraud = world.add("fraud")
    job_id = world.queue()
    # a dead process: preflight and tags done, risk moved, fraud's republish started
    db = SessionLocal()
    try:
        with project_context(None):
            job = db.get(Job, job_id)
            payload = dict(job.payload)
            payload["stages"] = [{**s, "status": "succeeded" if s["name"] in (
                "preflight", "tag-backfill") else s["status"]} for s in payload["stages"]]
            done = {s: {"status": "succeeded"} for s in svc.AGENT_STEPS["isolate"]}
            payload["agents"] = [
                {**svc._snapshot(_load(risk), "isolate"), "status": "succeeded",
                 "steps": done},
                {**svc._snapshot(_load(fraud), "isolate"), "status": "running",
                 "step": "republish", "deploy_job_id": "deploy-fraud-earlier",
                 "steps": {"grant-new": {"status": "succeeded"},
                           "republish": {"status": "running"},
                           "verify": {"status": "pending"},
                           "switch": {"status": "pending"}}},
            ]
            job.payload = payload
            job.status = "running"
            db.commit()
    finally:
        db.close()

    assert svc.JOB_TYPE in inflight.resumable_job_types()
    started: list[str] = []
    monkeypatch.setattr(svc, "start_migration_async", started.append)
    from app.deployer import pipeline

    assert job_id in pipeline.resume_pending_jobs() and started == [job_id]

    # the earlier process's deploy finished on its own: new policy, new version
    world.ops.republisher.jobs["deploy-fraud-earlier"] = "succeeded"
    world.iam.put_role_policy(
        RoleName=agent_iam.role_name_for("fraud", fraud),
        PolicyName=agent_iam.capability_policy_name("fraud"),
        PolicyDocument=json.dumps(agent_iam.policy_document(
            AgentSpec(**_load(fraud).spec), _role_ctx(_mem_id("alpha")))))
    world.runtime.serving[fraud] = _mem_id("alpha")
    job = world.run(job_id)
    assert job.status == "succeeded", job.error
    assert world.ops.republisher.started == []  # waited on, never started twice
    assert _agents(job_id)["fraud"]["steps"]["republish"]["detail"].startswith(
        "deploy deploy-fraud-earlier")


def test_a_stuck_republish_times_out_and_keeps_its_job_for_the_retry(world):
    world.add("risk")

    class Stuck(Republisher):
        def status(self, job_id):
            return "running", None

    job = world.run(world.queue(), republisher=Stuck(world.runtime, lambda a: LEGACY_MEM))
    assert job.status == "failed"
    entry = _agents(job.id)["risk"]
    assert entry["deploy_job_id"] and "still running" in entry["error"]


def test_preflight_needs_finished_provisioning_and_reads_quotas(world, monkeypatch):
    _project("beta")
    _set_isolation("beta", mode="isolated")
    job = world.run(world.queue(pid="beta"))
    assert job.status == "failed" and "finish provisioning" in job.error

    short = isolation.QuotaFinding("iam.roles", "IAM roles", 1, limit=10, used=10)
    monkeypatch.setattr(isolation, "get_provisioners", lambda: replace(
        isolation._provisioners, quotas=SimpleNamespace(check=lambda ctx: [short])))
    job = world.run(world.queue())
    assert job.status == "failed" and "IAM roles 10/10" in job.error
    assert job.payload["preflight"][0]["blocked"] is True


def test_a_run_refuses_once_isolation_was_switched_off(world):
    world.add("risk")
    job_id = world.queue()
    _set_isolation("alpha", mode="shared")
    job = world.run(job_id)
    assert job.status == "failed" and "switched off" in job.error
    assert world.ops.republisher.started == []


# ── IAM simulation (§10.15) ───────────────────────────────────────────────────


def test_a_migrated_role_cannot_reach_another_project(world):
    _project("beta")
    _set_isolation("beta", mode="isolated")
    _provisioned("beta")
    risk = world.add("risk")
    beta_agent = world.add("beta-agent", pid="beta")
    assert world.run(world.queue()).status == "succeeded"
    assert world.run(world.queue(pid="beta")).status == "succeeded"
    role_a = agent_iam.role_name_for("risk", risk)
    role_b = agent_iam.role_name_for("beta-agent", beta_agent)
    vault = f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:token-vault/default"
    # the matrix: A's role on B's memory / S3 prefix / runtime / M2M provider
    star = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*",
                                                    "Resource": "*"}]}
    world.iam.inline[role_a]["probe-star"] = star  # identity grants all: boundary decides
    beta_runtime = {"aws:ResourceTag/launchpad:project": "beta"}
    for action, resource, context in [
        ("bedrock-agentcore:CreateEvent", _mem_arn(_mem_id("beta")), {}),
        ("s3:GetObject", "arn:aws:s3:::lp-artifacts/projects/beta/agents/x.zip", {}),
        ("bedrock-agentcore:InvokeAgentRuntime",
         f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:runtime/beta-agent-x", beta_runtime),
        ("bedrock-agentcore:GetResourceOauth2Token",
         f"{vault}/oauth2credentialprovider/launchpad-gw-m2m-beta", {}),
    ]:
        assert decision(world.iam, role_a, action, resource, context) == "implicitDeny", action
    # and its own project's resources stay reachable
    assert decision(world.iam, role_a, "bedrock-agentcore:CreateEvent",
                    _mem_arn(_mem_id("alpha"))) == "allowed"
    assert decision(world.iam, role_a, "bedrock-agentcore:GetResourceOauth2Token",
                    f"{vault}/oauth2credentialprovider/launchpad-gw-m2m-alpha") == "allowed"
    # until retire, the shared memory stays inside both (dual-read)
    assert decision(world.iam, role_b, "bedrock-agentcore:CreateEvent",
                    _mem_arn(LEGACY_MEM)) == "allowed"


# ── revert and retire ─────────────────────────────────────────────────────────


def test_revert_moves_agents_back_and_removes_the_boundary(world):
    risk = world.add("risk")
    assert world.run(world.queue()).status == "succeeded"
    role = agent_iam.role_name_for("risk", risk)
    assert role in world.iam.boundaries
    db = SessionLocal()
    try:
        queued = svc.migrate(db, SimpleNamespace(username="admin"), db.get(Project, "alpha"),
                             direction=svc.DIRECTION_REVERT)
    finally:
        db.close()
    assert not isolation.is_isolated(_project_row("alpha"))
    job = world.run(queued[0])
    assert job.status == "succeeded", job.error
    assert world.runtime.serving[risk] == LEGACY_MEM
    assert {s for *_x, s in world.runtime.probes} == {200}
    assert role not in world.iam.boundaries
    assert svc.BRIDGE_POLICY not in world.iam.inline[role]
    assert project_iam.ISOLATION_TAG_KEY not in world.iam.tags[role]
    assert _load(risk).isolation_applied is None


def test_retire_drops_the_bridge_narrows_the_boundary_and_blocks_revert(world, monkeypatch):
    risk = world.add("risk")
    assert world.run(world.queue()).status == "succeeded"
    role = agent_iam.role_name_for("risk", risk)
    policy = world.iam.boundaries[role]
    assert _mem_arn(LEGACY_MEM) in json.dumps(world.iam.policies[policy]["doc"])

    class Boundary:
        def run(self, ctx):
            state = project_iam.ensure_boundary_policy(
                world.iam, project_iam.inputs_for(ctx.project_id, world.ws), WS)
            _boundary_row(state.version)
            return f"v{state.version}"

    db = SessionLocal()
    try:
        queued = svc.retire(db, SimpleNamespace(username="admin"), db.get(Project, "alpha"))
    finally:
        db.close()
    assert isolation.legacy_retired(_project_row("alpha"))
    job = world.run(queued[0], boundary=Boundary())
    assert job.status == "succeeded", job.error
    assert svc.BRIDGE_POLICY not in world.iam.inline[role]
    assert world.iam.policies[policy]["version"] == 2
    assert _mem_arn(LEGACY_MEM) not in json.dumps(world.iam.policies[policy]["doc"])
    assert decision(world.iam, role, "bedrock-agentcore:CreateEvent",
                    _mem_arn(LEGACY_MEM)) == "implicitDeny"
    assert decision(world.iam, role, "bedrock-agentcore:CreateEvent",
                    _mem_arn(_mem_id("alpha"))) == "allowed"
    assert _load(risk).isolation_applied == "boundary-v2"
    db = SessionLocal()
    try:
        memory = db.get(ProjectResource, ("alpha", WS, KIND_MEMORY))
        assert memory.detail["dual_read_until"]
        row = db.get(Project, "alpha")
        with pytest.raises(Exception) as refused:
            svc.migrate(db, SimpleNamespace(username="admin"), row,
                        direction=svc.DIRECTION_REVERT)
        assert refused.value.code == "project.isolation_retired"
        with pytest.raises(Exception) as refused:
            isolation.set_isolation(db, SimpleNamespace(username="admin"), row, mode="shared")
        assert refused.value.code == "project.isolation_retired"
    finally:
        db.close()


def test_retire_waits_for_every_workspace_and_every_release(world):
    world.add("live-one", endpoint_mode="live")
    db = SessionLocal()
    try:
        with pytest.raises(Exception) as refused:
            svc.retire(db, SimpleNamespace(username="admin"), db.get(Project, "alpha"))
        assert refused.value.code == "project.migration_unfinished"
    finally:
        db.close()
    world.run(world.queue(), releases=Releases(waiting={"live-one"}))
    db = SessionLocal()
    try:
        with pytest.raises(Exception) as refused:
            svc.retire(db, SimpleNamespace(username="admin"), db.get(Project, "alpha"))
        assert refused.value.code == "project.migration_awaiting_release"
    finally:
        db.close()


# ── §10.13 API ────────────────────────────────────────────────────────────────


@pytest.fixture
def api(world, app, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    started: list[str] = []
    monkeypatch.setattr(svc, "start_migration_async", started.append)
    _project("beta")
    dev = _user("dev", grant=False)
    _member_of("alpha", dev)
    admin = TestClient(app, client=PEER)
    assert admin.post("/api/auth/login", json=ADMIN).status_code == 200
    return SimpleNamespace(admin=admin, dev=_login(app, "dev"), started=started)


def test_migration_routes_are_admin_writes_and_member_reads(api, world):
    risk = world.add("risk")
    beta_agent = world.add("beta-agent", pid="beta")
    for path in ("/api/projects/alpha/isolation/migrate", "/api/projects/alpha/isolation/retire",
                 "/api/projects/alpha/isolation/migration/x/retry"):
        assert api.dev.post(path, json={}).status_code == 403, path
    res = api.admin.post("/api/projects/alpha/isolation/migrate", json={"direction": "sideways"})
    assert res.status_code == 400 and res.json()["code"] == "project.migration_invalid"

    res = api.admin.post("/api/projects/alpha/isolation/migrate", json={})
    assert res.status_code == 202, res.text
    queued = res.json()["queued"]
    assert len(queued) == 1 and api.started == queued
    assert res.json()["workspaces"][0]["state"] == svc.STATE_RUNNING
    # a second request while it runs queues nothing
    again = api.admin.post("/api/projects/alpha/isolation/migrate", json={"workspace": WS})
    assert again.json()["queued"] == []

    world.run(queued[0])
    body = api.dev.get("/api/projects/alpha/isolation/migration").json()
    run = body["workspaces"][0]["run"]
    assert run["state"] == svc.STATE_DONE and run["agents"][0]["name"] == "risk"
    res = api.admin.post(f"/api/projects/alpha/isolation/migration/{run['id']}/retry")
    assert res.status_code == 409 and res.json()["code"] == "project.migration_finished"

    line = api.dev.get(f"/api/projects/alpha/isolation/agents/{risk}").json()
    assert line["applied"] == "boundary-v1" and line["migration"]["status"] == "succeeded"

    # another project is 404 for a member, also through its agent
    assert api.dev.get("/api/projects/beta/isolation/migration").status_code == 404
    assert api.dev.get(f"/api/projects/alpha/isolation/agents/{beta_agent}").status_code == 404
    assert api.dev.get(f"/api/projects/beta/isolation/agents/{beta_agent}").status_code == 404
    res = api.admin.post("/api/projects/beta/isolation/migration/" + run["id"] + "/retry")
    assert res.status_code == 404 and res.json()["code"] == "project.migration_not_found"


def test_isolate_needs_isolation_and_provisioning(api):
    res = api.admin.post("/api/projects/beta/isolation/migrate", json={})
    assert res.status_code == 409 and res.json()["code"] == "project.isolation_disabled"
    _set_isolation("beta", mode="isolated")
    res = api.admin.post("/api/projects/beta/isolation/migrate", json={})
    assert res.status_code == 409 and res.json()["code"] == "project.not_provisioned"
    res = api.admin.post("/api/projects/beta/isolation/migrate", json={"workspace": "nowhere"})
    assert res.status_code == 404


def test_the_boundary_line_shows_drift(api, world):
    risk = world.add("risk")
    line = api.dev.get(f"/api/projects/alpha/isolation/agents/{risk}").json()
    assert line["mode"] == "isolated" and line["applied"] is None and line["drift"] is True
    world.run(world.queue())
    _boundary_row(3)
    line = api.dev.get(f"/api/projects/alpha/isolation/agents/{risk}").json()
    assert line["current"] == "boundary-v3" and line["applied"] == "boundary-v1"
    assert line["drift"] is True and line["policy_name"] == "launchpad-project-alpha"


def test_a_shared_project_is_untouched(api, world):
    """Single-project workspaces: shared mode, no boundary, no drift, nothing queued."""
    agent = world.add("plain", pid="beta")
    line = api.dev.get("/api/projects/alpha/isolation/migration").json()
    assert line["workspaces"][0]["state"] == svc.STATE_NONE
    admin_line = api.admin.get(f"/api/projects/beta/isolation/agents/{agent}").json()
    assert admin_line["mode"] == "shared" and admin_line["drift"] is False
