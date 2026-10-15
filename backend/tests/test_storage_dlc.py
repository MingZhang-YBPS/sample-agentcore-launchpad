"""Storage mounts in the Agent-DLC (design storage-mounts §8, S4): the release gate's
mount diff and its sign-off, the shared-state write confirmation of gate evaluations,
evaluation replays and canaries in a gated workspace, the promotion's advisory storage
gate, and storage in an architect proposal. No AWS call is made."""

from types import SimpleNamespace

import pytest

from app.assistant import proposal as contract
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.dlc import releases
from app.models.dlc import ReleaseRecord
from app.models.ledger import Agent, AuditEvent, SpecSnapshot, Workspace
from app.models.storage import StorageMount
from app.services import release_gates, storage_dlc
from app.services import storage_mounts as storage_service

WS = DEFAULT_WORKSPACE_ID
S3_AP = ("arn:aws:s3files:us-west-2:111122223333:file-system/fs-0123456789abcdef0/"
         "access-point/fsap-0123456789abcdef0")
EFS_AP = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-0aa"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _entry(db, storage_id, *, data_class="reference", max_mode="rw", arn=S3_AP,
           profile="np-core", status="ok"):
    row = StorageMount(id=storage_id, workspace_id=WS, name=storage_id.title(),
                       kind=storage_service.kind_of_arn(arn), access_point_arn=arn,
                       max_mode=max_mode, data_class=data_class, status=status,
                       network_profile_id=profile)
    db.add(row)
    db.commit()
    return row


def _spec(*mounts):
    return {"name": "st", "method": "container", "network": {"profile_id": "np-core"},
            "filesystem": {"mounts": [
                {"storage_id": sid, "mode": mode, "mount_path": path or f"/mnt/{sid}",
                 "kind": "s3files", "access_point_arn": S3_AP}
                for sid, mode, path in mounts]}}


def _gated(db):
    db.get(Workspace, WS).release_policy = {"release_mode": "gated"}
    db.commit()


def _agent(db, *, version="3", spec=None):
    agent = Agent(workspace_id=WS, name="st-agent", method="container", status="active",
                  spec=spec or _spec(), version=version, resource_id="rt_st-abc",
                  endpoint_mode="live", owner="dev1")
    db.add(agent)
    db.commit()
    return agent


def _snapshot(db, agent, version, spec, seq):
    db.add(SpecSnapshot(workspace_id=WS, agent_id=agent.id, seq=seq, spec=spec,
                        aws_version=str(version)))
    db.commit()


@pytest.fixture
def ep(monkeypatch):
    endpoints = {"live": "2"}

    def point(control, agent, name, version):
        endpoints[name] = str(version)
        return {}

    monkeypatch.setattr(releases, "point_endpoint", point)
    monkeypatch.setattr(releases, "endpoint_version",
                        lambda control, agent, name: endpoints.get(name))
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    return endpoints


def _release(db, agent, live_spec, candidate_spec):
    """A gated agent at v3 with live on v2: the open record and both snapshots."""
    _snapshot(db, agent, "2", live_spec, 1)
    _snapshot(db, agent, "3", candidate_spec, 2)
    releases.after_deploy(db, agent, note=None, actor="dev1")
    db.commit()
    return db.query(ReleaseRecord).one()


# ── the diff (§8.1) ───────────────────────────────────────────────────────────


def _m(sid, mode, path=None, data_class="reference"):
    return {"storage_id": sid, "mode": mode, "mount_path": path or f"/mnt/{sid}",
            "data_class": data_class}


@pytest.mark.parametrize("live, candidate, change, ack", [
    ([], [_m("datasets", "ro")], "added", False),
    ([], [_m("state", "rw")], "added", True),
    ([_m("state", "ro")], [_m("state", "rw")], "mode", True),
    ([_m("state", "rw")], [_m("state", "ro")], "mode", False),
    ([_m("state", "rw")], [], "removed", False),
    ([_m("a", "ro", "/mnt/x")], [_m("b", "ro", "/mnt/x")], "storage", False),
    ([_m("a", "ro", "/mnt/x")], [_m("b", "ro", "/mnt/x", "shared_state")], "storage", True),
    ([_m("a", "rw", "/mnt/x")], [_m("b", "rw", "/mnt/x")], "storage", True),
    ([_m("a", "ro")], [_m("a", "ro", data_class="shared_state")], "data_class", True),
])
def test_the_mount_diff_marks_what_needs_sign_off(live, candidate, change, ack):
    changes = storage_dlc.diff(live, candidate)
    assert [(c["change"], c["requires_ack"]) for c in changes] == [(change, ack)]


def test_identical_mounts_are_no_change():
    assert storage_dlc.diff([_m("a", "rw")], [_m("a", "rw")]) == []


def test_inline_mounts_are_read_write_without_a_class():
    spec = {"filesystem": {"efs": [{"access_point_arn": EFS_AP, "mount_path": "/mnt/legacy"}]}}
    assert storage_dlc.mounts_of(spec) == [{
        "storage_id": None, "mount_path": "/mnt/legacy", "mode": "rw", "kind": "efs",
        "access_point_arn": EFS_AP}]


# ── release records (§8.1) ────────────────────────────────────────────────────


def test_a_new_read_write_mount_needs_the_signers_acknowledgement(db, ep):
    _gated(db)
    _entry(db, "datasets")
    _entry(db, "state", data_class="shared_state")
    agent = _agent(db)
    record = _release(db, agent, _spec(("datasets", "ro", None)),
                      _spec(("datasets", "ro", None), ("state", "rw", None)))
    storage = releases.record_out(record)["storage"]
    assert storage["live_known"] is True and storage["requires_ack"] is True
    assert [(c["change"], c["storage_id"], c["data_class"]) for c in storage["changes"]] == [
        ("added", "state", "shared_state")]
    assert [m["storage_id"] for m in storage["shared_state_writes"]] == ["state"]

    record.gate_report = {"verdict": "PASS"}
    db.commit()
    with pytest.raises(AppError) as err:
        releases.sign(db, record, agent, object(), actor="lead")
    assert err.value.code == "release.storage_ack_required"
    assert ep["live"] == "2"  # production untouched

    releases.sign(db, record, agent, object(), actor="lead", storage_ack=True)
    db.commit()
    assert record.decision == "released" and ep["live"] == "3"
    signed = releases.record_out(record)["storage"]
    assert signed["acknowledged_by"] == "lead" and signed["acknowledged_at"]
    actions = {e.action for e in db.query(AuditEvent).all()}
    assert {"release.sign", "release.storage_ack"} <= actions


def test_read_only_changes_sign_without_an_acknowledgement(db, ep):
    _gated(db)
    _entry(db, "datasets")
    _entry(db, "skills")
    agent = _agent(db)
    record = _release(db, agent, _spec(("datasets", "rw", None)),
                      _spec(("datasets", "ro", None), ("skills", "ro", None)))
    storage = releases.record_out(record)["storage"]
    assert storage["requires_ack"] is False
    assert sorted(c["change"] for c in storage["changes"]) == ["added", "mode"]
    record.gate_report = {"verdict": "PASS"}
    releases.sign(db, record, agent, object(), actor="lead")
    assert record.decision == "released"


def test_an_agent_without_mounts_keeps_an_empty_diff(db, ep):
    _gated(db)
    agent = _agent(db, spec={"name": "st", "method": "container"})
    record = _release(db, agent, {"name": "st"}, {"name": "st"})
    storage = releases.record_out(record)["storage"]
    assert storage["changes"] == [] and storage["requires_ack"] is False
    record.gate_report = {"verdict": "PASS"}
    releases.sign(db, record, agent, object(), actor="lead")
    assert record.decision == "released"


def test_an_unknown_live_spec_reads_every_mount_as_added(db, ep):
    _gated(db)
    _entry(db, "state")
    agent = _agent(db)
    _snapshot(db, agent, "3", _spec(("state", "rw", None)), 1)  # no v2 snapshot
    releases.after_deploy(db, agent, note=None, actor="dev1")
    storage = releases.record_out(db.query(ReleaseRecord).one())["storage"]
    assert storage["live_known"] is False and storage["requires_ack"] is True


def test_a_reclassified_storage_shows_as_a_data_class_change(db, ep):
    _gated(db)
    row = _entry(db, "state", data_class="reference")
    agent = _agent(db, version="2")
    spec = _spec(("state", "ro", None))
    _snapshot(db, agent, "2", spec, 1)
    # v2 was released while the storage was reference data
    signed = ReleaseRecord(workspace_id=WS, agent_id=agent.id, candidate_version="2",
                           candidate_endpoint="candidate", decision="released",
                           requested_by="dev1")
    db.add(signed)
    db.flush()
    releases.refresh_storage(db, signed, agent)
    db.commit()
    row.data_class = "shared_state"
    agent.version = "3"
    db.commit()
    _snapshot(db, agent, "3", spec, 2)
    releases.after_deploy(db, agent, note=None, actor="dev1")
    record = db.query(ReleaseRecord).filter(ReleaseRecord.decision == "pending").one()
    changes = releases.record_out(record)["storage"]["changes"]
    assert [(c["change"], c["from_data_class"], c["data_class"], c["requires_ack"])
            for c in changes] == [("data_class", "reference", "shared_state", True)]


def test_the_gate_report_carries_the_storage_evidence(db, ep, monkeypatch):
    _gated(db)
    _entry(db, "state")
    agent = _agent(db)
    record = _release(db, agent, _spec(), _spec(("state", "rw", None)))
    monkeypatch.setattr(releases.gate_engine, "decide",
                        lambda **kw: {"verdict": "PASS", "criteria": []})
    monkeypatch.setattr(releases.criteria_svc, "criteria_of", lambda db, sid: [])
    monkeypatch.setattr(releases.calibration_svc, "calibrated_keys", lambda *a: {})
    from app.evaluation.models import EvalRun

    run = EvalRun(workspace_id=WS, agent_id=agent.id, agent_name=agent.name,
                  status="completed", evaluators=[], session_ids=[])
    db.add(run)
    db.commit()
    record.run_ids = [run.id]
    out = releases.evaluate(db, record)
    assert out["report"]["storage"]["requires_ack"] is True
    assert out["report"]["storage"]["changes"][0]["storage_id"] == "state"


# ── evaluation write isolation (§8.2) ─────────────────────────────────────────


@pytest.fixture
def gate_plan(monkeypatch):
    """A gate evaluation without criteria or golden sets: one regression item."""
    split = SimpleNamespace(id="ds-1", name="regression", items=[{"input": "hi"}], cloud=None)
    monkeypatch.setattr(releases, "_gate_plan", lambda db, record, agent: (
        [], ["Builtin.Helpfulness"], {"regression": split}))
    monkeypatch.setattr(releases, "gate_estimate", lambda *a, **kw: {})
    submitted = []

    def submit(**kwargs):
        submitted.append(kwargs)
        return SimpleNamespace(id=f"run-{len(submitted)}")

    return submit, submitted


def test_a_gate_evaluation_writing_shared_state_needs_confirmation(db, ep, gate_plan):
    submit, submitted = gate_plan
    _gated(db)
    _entry(db, "state", data_class="shared_state")
    agent = _agent(db)
    record = _release(db, agent, _spec(), _spec(("state", "rw", None)))
    with pytest.raises(AppError) as err:
        releases.start_evaluation(db, record, agent, None, actor="dev1", submit=submit)
    assert err.value.code == "storage.write_confirm_required" and err.value.status_code == 409
    assert [m["storage_id"] for m in err.value.detail["storage"]] == ["state"]
    assert submitted == []

    run_ids = releases.start_evaluation(db, record, agent, None, actor="dev1", submit=submit,
                                        confirm_storage=True)
    db.commit()
    assert run_ids == ["run-1"]
    confirmed = releases._attachment(record, releases.STORAGE_WRITE_CONFIRMATION)
    assert confirmed["confirmed_by"] == "dev1" and confirmed["storage"] == ["state"]
    assert releases.storage_evidence(db, record, agent)["write_confirmation"]["storage"] == [
        "state"]
    assert any(e.action == "storage.write_confirmed" for e in db.query(AuditEvent).all())


@pytest.mark.parametrize("data_class, mode", [
    ("reference", "rw"), ("scratch", "rw"), ("shared_state", "ro")])
def test_other_mounts_need_no_confirmation(db, ep, gate_plan, data_class, mode):
    submit, submitted = gate_plan
    _gated(db)
    _entry(db, "store", data_class=data_class)
    agent = _agent(db)
    record = _release(db, agent, _spec(), _spec(("store", mode, None)))
    releases.start_evaluation(db, record, agent, None, actor="dev1", submit=submit)
    assert len(submitted) == 1
    assert releases._attachment(record, releases.STORAGE_WRITE_CONFIRMATION) is None


def test_only_gated_workspaces_ask(db):
    _entry(db, "state", data_class="shared_state")
    spec = _spec(("state", "rw", None))
    assert storage_dlc.require_write_confirmation(db, WS, spec, confirmed=False,
                                                  action="x") == []
    _gated(db)
    with pytest.raises(AppError):
        storage_dlc.require_write_confirmation(db, WS, spec, confirmed=False, action="x")


def test_an_evaluation_replay_asks_before_the_run_exists(db):
    from app.evaluation import service as eval_service
    from app.evaluation.models import EvalRun

    _gated(db)
    _entry(db, "state", data_class="shared_state")
    agent = _agent(db, spec=_spec(("state", "rw", None)))
    with pytest.raises(AppError) as err:
        eval_service.submit_run(agent=agent, workspace=SimpleNamespace(id=WS),
                                dataset_items=[{"input": "hi"}], dataset_id="d",
                                dataset_name="d", evaluators=["e"], qualifier="live",
                                confirm_storage=False)
    assert err.value.code == "storage.write_confirm_required"
    assert err.value.detail["action"] == "evaluation.run"
    assert db.query(EvalRun).count() == 0
    # confirmed: journaled, then the run proceeds
    eval_service.storage_write_check(agent, confirmed=True, actor="dev1")
    db.expire_all()
    events = [e for e in db.query(AuditEvent).all() if e.action == "storage.write_confirmed"]
    assert events and events[0].target == f"{agent.id}:state" and events[0].actor == "dev1"


def test_a_canary_of_a_shared_state_writer_asks(db):
    from app.optimization import canary_infra
    from app.schemas.agent import AgentSpec

    _gated(db)
    _entry(db, "state", data_class="shared_state")
    agent = _agent(db, spec=_spec(("state", "rw", None)))
    with pytest.raises(AppError) as err:
        canary_infra.storage_write_check(agent, None, confirmed=False)
    assert err.value.detail["action"] == "canary.start"
    # the edited candidate decides, not the running spec
    edited = AgentSpec.model_validate({**_spec(("state", "ro", None)), "name": "st-agent",
                                       "system_prompt": "x"})
    assert canary_infra.storage_write_check(agent, edited, confirmed=False) == []
    writes = canary_infra.storage_write_check(agent, None, confirmed=True, actor="dev1")
    assert [m["storage_id"] for m in writes] == ["state"]


# ── promotion (§8.1, advisory) ────────────────────────────────────────────────


def test_the_promotion_storage_gate_is_advisory_evidence(db, monkeypatch):
    _entry(db, "state", data_class="shared_state")
    target = db.get(Workspace, WS)
    bundle = SimpleNamespace(agent_name="st-agent", project_id=None,
                             spec=_spec(("state", "rw", None)))
    monkeypatch.setattr(release_gates.promotion_service, "target_holder",
                        lambda *a: (None, False))
    gate = release_gates.storage_gate(db, bundle, target)
    assert gate["key"] == "storage" and gate["ok"] is False
    assert gate["changes"][0]["change"] == "added" and "storage" in release_gates.ADVISORY_GATES
    bundle.spec = {"name": "st"}
    assert release_gates.storage_gate(db, bundle, target) is None


# ── architect proposals (§8.3) ────────────────────────────────────────────────

PROPOSAL = {"name": "pipeline-agent", "system_prompt": "You keep the pipeline state.",
            "native_tools": [], "memory": "disabled"}


def _catalog(*entries):
    return {"tools": [], "skills": [], "knowledge_bases": [], "resources": {},
            "storage": list(entries)}


def _cat_entry(sid, *, max_mode="rw", profile="np-core", data_class="shared_state",
               status="ok"):
    return {"storage_id": sid, "name": sid, "kind": "s3files", "access_point_arn": S3_AP,
            "max_mode": max_mode, "data_class": data_class, "network_profile_id": profile,
            "status": status}


def test_a_proposal_without_storage_hashes_as_before():
    content, errors = contract.parse_content(PROPOSAL)
    assert errors == [] and content.storage == []
    assert "storage" not in contract.content_dump(content)
    bindings = contract.resource_bindings(content, _catalog())
    assert "storage" not in bindings["resources"] and bindings["network"] is None
    assert "filesystem" not in contract.bindings_view(bindings)


def test_a_proposal_mounts_catalog_storage_on_its_default_network():
    raw = {**PROPOSAL, "storage": [{"storage_id": "state", "mode": "rw"},
                                   {"storage_id": "datasets"}]}
    catalog = _catalog(_cat_entry("state"), _cat_entry("datasets", data_class="reference"))
    content, errors = contract.parse_content(raw)
    assert errors == [] and contract.reference_errors(content, catalog) == []
    assert contract.content_dump(content)["storage"] == [
        {"storage_id": "state", "mode": "rw"}, {"storage_id": "datasets", "mode": "ro"}]
    spec = contract.to_agent_spec(content, catalog)
    assert spec.network.profile_id == "np-core"
    assert [(m.storage_id, m.mode, m.mount_path, m.resolved) for m in spec.filesystem.mounts] == [
        ("state", "rw", "/mnt/state", True), ("datasets", "ro", "/mnt/datasets", True)]
    bound = contract.resource_bindings(content, catalog)["resources"]["storage"]
    assert bound["state"]["mode"] == "rw" and bound["state"]["data_class"] == "shared_state"
    view = contract.bindings_view(contract.resource_bindings(content, catalog))
    assert view["network"] == {"profile_id": "np-core"}


@pytest.mark.parametrize("storage, entries, message", [
    ([{"storage_id": "other"}], [], "is not a storage entry this project may mount"),
    ([{"storage_id": "ds", "mode": "rw"}], [_cat_entry("ds", max_mode="ro")], "read-only at most"),
    ([{"storage_id": "ds"}], [_cat_entry("ds", profile=None)], "no default network profile"),
    ([{"storage_id": "ds"}], [_cat_entry("ds", status="blocked")], "failed its check"),
    ([{"storage_id": "a1"}, {"storage_id": "b1"}],
     [_cat_entry("a1"), _cat_entry("b1", profile="np-edge")], "different default network"),
])
def test_bad_storage_references_are_refused(storage, entries, message):
    content, errors = contract.parse_content({**PROPOSAL, "storage": storage})
    assert errors == []
    assert any(message in e for e in contract.reference_errors(content, _catalog(*entries)))


def test_a_proposal_never_carries_an_arn_or_a_repeat():
    _, errors = contract.parse_content(
        {**PROPOSAL, "storage": [{"storage_id": "ds", "access_point_arn": S3_AP}]})
    assert errors
    _, errors = contract.parse_content(
        {**PROPOSAL, "storage": [{"storage_id": "ds"}, {"storage_id": "ds", "mode": "rw"}]})
    assert "storage must not repeat an entry" in errors


def test_the_proposal_storage_catalog_is_the_projects_allowed_set(db):
    _entry(db, "mine")
    _entry(db, "theirs", arn=EFS_AP)
    everything = contract.storage_catalog(db, WS, None)
    assert sorted(e["storage_id"] for e in everything) == ["mine", "theirs"]
    project_id = _default_project(db)
    storage_service.set_project_entries(db, WS, project_id, ["mine"])
    allowed = contract.storage_catalog(db, WS, project_id)
    assert [e["storage_id"] for e in allowed] == ["mine"]
    assert allowed[0]["access_point_arn"] == S3_AP and allowed[0]["max_mode"] == "rw"


def _default_project(db):
    from app.models.projects import ProjectWorkspace

    return db.query(ProjectWorkspace).filter(ProjectWorkspace.workspace_id == WS).first().project_id
