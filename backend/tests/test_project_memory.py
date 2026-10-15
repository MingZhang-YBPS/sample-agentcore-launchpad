"""Per-project Memory for isolated projects (projects design §10.4, R10-mem): the
`memory` stage of `project_provision`, the per-project default memory (deploys, the
chat rail, observability, the console), and the dual-read of the old shared memory —
for the project's own agents' actors only. No AWS: every client is a stub."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.memory_console as mc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.models.project_isolation import (
    KIND_MEMORY,
    STATUS_PROVISIONING,
    STATUS_READY,
    ProjectResource,
)
from app.models.projects import Project
from app.services import bootstrap, memory, project_memory
from app.services import project_isolation as svc
from app.services.workspace import WorkspaceContext
from tests.conftest import client_error, set_default_resources
from tests.test_project_scope import (  # noqa: F401
    _agent,
    _login,
    _member_of,
    _project,
    _user,
    app,
)

WS = DEFAULT_WORKSPACE_ID
SHARED = "launchpad_memory-SHARED01"
OWN = "launchpad_mem_alpha-OWN0000001"
OTHER = "launchpad_mem_beta-OTHER00001"


# ── helpers ───────────────────────────────────────────────────────────────────


def _isolate(pid: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        row.settings = {**(row.settings or {}), "isolation": {"mode": svc.MODE_ISOLATED}}
        db.commit()
    finally:
        db.close()


def _memory_row(pid: str, memory_id: str, *, status: str = STATUS_READY,
                created_at: datetime | None = None, detail: dict | None = None) -> None:
    db = SessionLocal()
    try:
        db.add(ProjectResource(
            project_id=pid, workspace_id=WS, kind=KIND_MEMORY, resource_id=memory_id,
            arn=f"arn:aws:bedrock-agentcore:us-west-2:1:memory/{memory_id}", status=status,
            detail=detail, created_at=created_at or datetime.now(UTC),
        ))
        db.commit()
    finally:
        db.close()


def _ws(**resources: Any) -> WorkspaceContext:
    return WorkspaceContext(account_id="111122223333", region="us-west-2",
                            resources={"memory_id": SHARED, **resources}, id=WS)


class StubControl:
    def __init__(self, memories: list[dict] | None = None):
        self.memories = memories or []
        self.calls: list[tuple[str, dict]] = []

    def list_memories(self, **kw):
        self.calls.append(("list_memories", kw))
        return {"memories": self.memories}

    def create_memory(self, **kw):
        self.calls.append(("create_memory", kw))
        mem_id = f"{kw['name']}-NEW0000001"
        self.memories.append({"id": mem_id, "arn": f"arn:mem/{mem_id}"})
        return {"memory": {"id": mem_id, "arn": f"arn:mem/{mem_id}"}}

    def get_memory(self, **kw):
        self.calls.append(("get_memory", kw))
        return {"memory": {"id": kw["memoryId"], "status": "ACTIVE",
                           "strategies": [dict(s) for s in bootstrap.DEFAULT_STRATEGIES]}}

    def update_memory(self, **kw):
        self.calls.append(("update_memory", kw))
        return {}


class StubData:
    """Answers per memory id: `by_memory[memory_id][operation]`."""

    def __init__(self, by_memory: dict[str, dict[str, Any]]):
        self.by_memory = by_memory
        self.calls: list[tuple[str, str]] = []

    def _reply(self, op: str, kw: dict) -> Any:
        self.calls.append((op, kw["memoryId"]))
        value = self.by_memory.get(kw["memoryId"], {}).get(op, {})
        if isinstance(value, Exception):
            raise value
        return value(kw) if callable(value) else value

    def __getattr__(self, op: str):
        if op.startswith("list_") or op.startswith("retrieve_"):
            return lambda **kw: self._reply(op, kw)
        raise AttributeError(op)


@pytest.fixture
def alpha(app):  # noqa: F811
    _project("alpha")
    _isolate("alpha")
    return "alpha"


# ── the provisioner ───────────────────────────────────────────────────────────


def test_the_real_provisioner_is_registered():
    assert isinstance(svc.get_provisioners().memory, project_memory.ProjectMemoryProvisioner)


def _ctx(control: StubControl, recorded: list, resources: dict | None = None):
    ctx = svc.ProvisionContext(
        project_id="risk-desk", workspace_id=WS, job_id="job1", settings={},
        names=svc.resource_names("risk-desk"), resources=resources or {},
        _workspace=SimpleNamespace(
            resources={"execution_role_arn": "arn:aws:iam::1:role/exec"},
            client=lambda _service: control,
        ),
    )
    ctx._record = recorded.append
    return ctx


def test_provisioner_creates_the_project_memory_tagged_and_records_it_first():
    control, recorded, waited = StubControl(), [], []
    prov = project_memory.ProjectMemoryProvisioner(
        wait=lambda _c, mem_id, timeout_s: waited.append((mem_id, len(recorded))))
    detail = prov.ensure_memory(_ctx(control, recorded))
    create = next(kw for op, kw in control.calls if op == "create_memory")
    assert create["name"] == "launchpad_mem_risk_desk"
    assert create["tags"] == {"launchpad:project": "risk-desk", "launchpad:workspace": WS}
    assert create["memoryExecutionRoleArn"] == "arn:aws:iam::1:role/exec"
    assert create["eventExpiryDuration"] == bootstrap.MEMORY_EVENT_EXPIRY_DAYS
    assert len(create["memoryStrategies"]) == len(bootstrap.DEFAULT_STRATEGIES)
    assert "risk-desk" in create["description"]
    [row] = recorded
    assert row.kind == KIND_MEMORY and row.resource_id == "launchpad_mem_risk_desk-NEW0000001"
    # recorded before the ACTIVE wait, so a killed job's resume adopts it
    assert waited == [(row.resource_id, 1)]
    assert detail == row.resource_id


def test_provisioner_adopts_an_existing_memory_and_keeps_the_dual_read_deadline():
    existing = {"id": "launchpad_mem_risk_desk-OLD0000001", "arn": "arn:old"}
    # a look-alike of another project is never adopted
    other = {"id": "launchpad_mem_risk_desk_2-XYZ0000001", "arn": "arn:x"}
    control, recorded = StubControl([other, existing]), []
    prov = project_memory.ProjectMemoryProvisioner(wait=lambda *_a, **_k: None)
    previous = {KIND_MEMORY: {"detail": {"dual_read_until": "2026-12-01T00:00:00+00:00"}}}
    assert prov.ensure_memory(_ctx(control, recorded, previous)).endswith("(adopted)")
    assert not [op for op, _ in control.calls if op == "create_memory"]
    [row] = recorded
    assert row.resource_id == existing["id"]
    assert row.detail["dual_read_until"] == "2026-12-01T00:00:00+00:00"


def test_bootstrap_memory_sends_no_tags():
    control = StubControl()
    bootstrap.ensure_memory(control, wait=False)
    create = next(kw for op, kw in control.calls if op == "create_memory")
    assert "tags" not in create and create["name"] == bootstrap.MEMORY_NAME
    assert create["description"].startswith("Launchpad shared memory")


def test_provision_job_runs_the_memory_stage_and_finalizes_the_row(alpha):
    control = StubControl()
    db = SessionLocal()
    try:
        job, _ = svc.queue_provision(db, "alpha", WS, actor="test")
        job_id = job.id
    finally:
        db.close()

    class NoQuota:
        def check(self, ctx):
            return []

    provisioners = svc.Provisioners(
        iam=svc._PENDING, kb=svc._PENDING, gateway=svc._PENDING, storage=svc._PENDING,
        quotas=NoQuota(),
        memory=project_memory.ProjectMemoryProvisioner(wait=lambda *_a, **_k: None),
    )
    workspace = SimpleNamespace(id=WS, resources={}, client=lambda _s: control)
    svc.execute_provision_job(job_id, provisioners, workspace_factory=lambda _ws: workspace)
    db = SessionLocal()
    try:
        with project_context(None):
            stages = {e["name"]: e["status"] for e in svc.job_stages(db.get(svc.Job, job_id))}
        row = db.get(ProjectResource, ("alpha", WS, KIND_MEMORY))
        assert stages["memory"] == "succeeded"
        assert row.status == STATUS_READY and row.resource_id == "launchpad_mem_alpha-NEW0000001"
    finally:
        db.close()


# ── resolution ────────────────────────────────────────────────────────────────


def test_a_shared_project_keeps_the_workspace_memory(app):  # noqa: F811
    _project("alpha")
    _memory_row("alpha", OWN)  # a leftover row does not switch a shared project
    ws = _ws()
    with project_context("alpha"):
        assert memory.memory_id_or_none(ws) == SHARED
        assert project_memory.dual_read(ws) is None
    assert project_memory.memory_resources({"memory_id": SHARED}, WS, "alpha") == {
        "memory_id": SHARED}


def test_an_isolated_project_uses_its_memory_once_ready(alpha):
    ws = _ws()
    with project_context("alpha"):
        assert memory.memory_id_or_none(ws) == SHARED  # nothing provisioned yet
    _memory_row("alpha", OWN, status=STATUS_PROVISIONING)
    with project_context("alpha"):
        assert memory.memory_id_or_none(ws) == SHARED  # the run has not finished
    db = SessionLocal()
    try:
        db.get(ProjectResource, ("alpha", WS, KIND_MEMORY)).status = STATUS_READY
        db.commit()
    finally:
        db.close()
    with project_context("alpha"):
        assert memory.memory_id_or_none(ws) == OWN
    # another project, and a hub-global context, keep the workspace memory
    with project_context("default"):
        assert memory.memory_id_or_none(ws) == SHARED
    with project_context(None):
        assert memory.memory_id_or_none(ws) == SHARED
    # the deploy paths' overlay (LAUNCHPAD_MEMORY_ID, a harness's memory ARN)
    resources = {"memory_id": SHARED, "memory_arn": "arn:shared", "gateway_id": "gw"}
    with project_context("alpha"):
        out = project_memory.memory_resources(resources, WS)
    assert out["memory_id"] == OWN and out["memory_arn"].endswith(f"memory/{OWN}")
    assert out["gateway_id"] == "gw" and resources["memory_id"] == SHARED


def test_dual_read_admits_only_the_projects_own_agents(alpha):
    _memory_row("alpha", OWN)
    mine = _agent("alpha-agent", "alpha")
    theirs = _agent("default-agent", "default")
    with project_context("alpha"):
        dual = project_memory.dual_read(_ws())
    assert dual is not None and dual.memory_id == SHARED
    assert dual.allows_actor(f"{mine}__joan")
    assert not dual.allows_actor(f"{theirs}__joan")
    assert not dual.allows_actor("default") and not dual.allows_actor(f"{mine}__")
    assert dual.allows_namespace(f"/facts/{mine}__joan")
    assert dual.allows_namespace(f"/summaries/{mine}__joan/sess-1")
    assert not dual.allows_namespace(f"/facts/{theirs}__joan")
    assert not dual.allows_namespace("/facts")


@pytest.mark.parametrize("detail, created_days_ago", [
    (None, project_memory.DUAL_READ_DAYS + 1),  # the event expiry passed
    ({"dual_read_until": "2020-01-01T00:00:00+00:00"}, 0),  # the migration closed it
    ({"dual_read_until": False}, 0),  # switched off
])
def test_dual_read_window_closes(alpha, detail, created_days_ago):
    _memory_row("alpha", OWN, detail=detail,
                created_at=datetime.now(UTC) - timedelta(days=created_days_ago))
    _agent("alpha-agent", "alpha")
    with project_context("alpha"):
        assert project_memory.dual_read(_ws()) is None
        assert memory.memory_id_or_none(_ws()) == OWN


# ── services/memory.py: chat rail, observability ──────────────────────────────


def test_memory_reads_fall_back_to_the_shared_memory_for_own_actors(alpha, monkeypatch):
    _memory_row("alpha", OWN)
    mine = _agent("alpha-agent", "alpha")
    theirs = _agent("default-agent", "default")
    old_event = {"eventId": "e-old"}
    data = StubData({
        OWN: {"list_events": lambda kw: {"events": []},
              "list_actors": {"actorSummaries": [{"actorId": f"{mine}__new"}]},
              "list_memory_records": {"memoryRecordSummaries": [{"memoryRecordId": "r1"}]}},
        SHARED: {"list_events": {"events": [old_event]},
                 "list_actors": {"actorSummaries": [{"actorId": f"{mine}__joan"},
                                                    {"actorId": f"{theirs}__joan"}]},
                 "list_memory_records": {"memoryRecordSummaries": [
                     {"memoryRecordId": "r1"}, {"memoryRecordId": "r0"}]}},
    })
    monkeypatch.setattr(memory, "data_client", lambda _ws: data)
    ws = _ws()
    with project_context("alpha"):
        assert memory.list_events(ws, f"{mine}__joan", "s1") == [old_event]
        # another project's actor is never read from the shared memory
        assert memory.list_events(ws, f"{theirs}__joan", "s1") == []
        assert ("list_events", SHARED) in data.calls
        data.calls.clear()
        memory.list_events(ws, f"{theirs}__joan", "s1")
        assert data.calls == [("list_events", OWN)]
        assert memory.list_actor_ids(ws) == [f"{mine}__new", f"{mine}__joan"]
        ids = [r["memoryRecordId"] for r in memory.list_records(ws, f"/facts/{mine}__joan")]
        assert ids == ["r1", "r0"]
        theirs_ids = memory.list_records(ws, f"/facts/{theirs}__joan")
        assert [r["memoryRecordId"] for r in theirs_ids] == ["r1"]
        # a pinned memory is read alone
        data.calls.clear()
        memory.list_events(ws, f"{mine}__joan", "s1", memory_id="pinned-1")
        assert data.calls == [("list_events", "pinned-1")]


# ── the Memory console ────────────────────────────────────────────────────────


@pytest.fixture
def console(alpha, monkeypatch):
    set_default_resources({"memory_id": SHARED})
    _memory_row("alpha", OWN)
    _project("beta")
    _isolate("beta")
    _memory_row("beta", OTHER)
    mine = _agent("alpha-agent", "alpha")
    theirs = _agent("beta-agent", "beta")
    uid = _user("joan", grant=False)
    _member_of("alpha", uid)
    return SimpleNamespace(mine=mine, theirs=theirs)


def _wire(monkeypatch, data: StubData, memories: list[dict] | None = None):
    control = SimpleNamespace(
        get_memory=lambda **kw: {"memory": {"id": kw["memoryId"], "strategies": [
            {"strategyId": "facts", "name": "facts", "type": "SEMANTIC",
             "namespaces": ["/facts/{actorId}"], "namespaceTemplates": ["/facts/{actorId}"]},
        ]}},
        list_memories=lambda **kw: {"memories": memories or []},
    )
    monkeypatch.setattr(mc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(mc, "control_client", lambda _ws=None: control)


@pytest.fixture
def joan(app, console):  # noqa: F811
    return _login(app, "joan")


def test_console_reads_the_project_memory_and_merges_own_shared_actors(
    joan, console, monkeypatch
):
    mine, theirs = console.mine, console.theirs
    data = StubData({
        OWN: {"list_actors": {"actorSummaries": [{"actorId": f"{mine}__new"}]}},
        SHARED: {"list_actors": {"actorSummaries": [
            {"actorId": f"{mine}__joan"}, {"actorId": f"{mine}__new"},
            {"actorId": f"{theirs}__joan"}, {"actorId": "default"}]}},
    })
    _wire(monkeypatch, data)
    res = joan.get("/api/memory/actors")
    assert res.status_code == 200, res.text
    items = {(a["actor_id"], a["source"]) for a in res.json()["items"]}
    assert items == {(f"{mine}__new", "project"), (f"{mine}__joan", "shared")}
    # a later page continues in the project memory only
    data.calls.clear()
    joan.get("/api/memory/actors", params={"next_token": "t2"})
    assert data.calls == [("list_actors", OWN)]


def test_console_sessions_events_and_records_dual_read(joan, console, monkeypatch):
    mine, theirs = console.mine, console.theirs
    actor = f"{mine}__joan"
    data = StubData({
        OWN: {
            "list_sessions": client_error("ResourceNotFoundException", "ListSessions"),
            "list_events": {"events": []},
            "list_memory_records": {"memoryRecordSummaries": []},
        },
        SHARED: {
            "list_sessions": {"sessionSummaries": [{"sessionId": "s-old", "actorId": actor}]},
            "list_events": {"events": [{"eventId": "e-old", "payload": []}]},
            "list_memory_records": {"memoryRecordSummaries": [
                {"memoryRecordId": "r-old", "content": {"text": "likes tea"}}]},
        },
    })
    _wire(monkeypatch, data)
    sessions = joan.get("/api/memory/sessions", params={"actor_id": actor})
    assert sessions.status_code == 200, sessions.text
    assert [(s["session_id"], s["source"]) for s in sessions.json()["items"]] == [
        ("s-old", "shared")]
    events = joan.get("/api/memory/events", params={"actor_id": actor, "session_id": "s-old"})
    assert events.json()["source"] == "shared"
    assert [e["event_id"] for e in events.json()["items"]] == ["e-old"]
    records = joan.get("/api/memory/records", params={"actor_id": actor})
    assert [(r["record_id"], r["source"]) for r in records.json()["items"]] == [
        ("r-old", "shared")]
    # another project's actor in the shared memory is not reachable
    foreign = f"{theirs}__joan"
    res = joan.get("/api/memory/sessions", params={"actor_id": foreign, "source": "shared"})
    assert res.status_code == 404
    res = joan.get("/api/memory/records",
                   params={"namespace": f"/facts/{foreign}", "source": "shared"})
    assert res.status_code == 404


def test_console_overview_names_the_project_memory_and_hides_other_projects(
    joan, console, monkeypatch
):
    data = StubData({OWN: {"list_actors": {"actorSummaries": []}}})
    _wire(monkeypatch, data, memories=[{"id": OWN}, {"id": OTHER}, {"id": SHARED}])
    res = joan.get("/api/memory/overview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["memory"]["id"] == OWN
    assert body["project_memory"]["memory_id"] == OWN
    assert body["project_memory"]["shared_memory_id"] == SHARED
    ids = {m["id"] for m in body["other_memories"]}
    assert ids == {OWN, SHARED}
    assert [m["id"] for m in body["other_memories"] if m["is_platform"]] == [OWN]


def test_shared_project_console_is_unchanged(app, monkeypatch):  # noqa: F811
    set_default_resources({"memory_id": SHARED})
    _user("kim")
    kim = _login(app, "kim")
    data = StubData({SHARED: {"list_actors": {"actorSummaries": [{"actorId": "x__y"}]}}})
    _wire(monkeypatch, data, memories=[{"id": SHARED}])
    body = kim.get("/api/memory/actors").json()
    assert [a["actor_id"] for a in body["items"]] == ["x__y"]
    assert "source" not in body["items"][0]
    assert "project_memory" not in kim.get("/api/memory/overview").json()
    assert {m for _op, m in data.calls} == {SHARED}
