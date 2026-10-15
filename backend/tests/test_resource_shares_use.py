"""Cross-project shares in use (projects design §11.2, SHARE-2): evaluations and online
evaluation accept datasets / evaluators shared at `use` level (and refuse read-only ones),
a share never lets the grantee change the resource here or on AWS, lists label what
another project lent (`shared_from`), workspace isolation does not hide a shared id, the
release evidence names the lending project, and when a share ends the grantee's owners
get a notice and an isolated grantee's boundary is rendered again."""

# ruff: noqa: F811 — the `app` / `world` fixtures are imported, then requested by name

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import forget_shared_rows, project_context
from app.evaluation import online_evaluators
from app.evaluation import routers as eval_routers
from app.evaluation.models import EvalDataset
from app.models.ledger import Job, ResourceCreator
from app.models.projects import Project
from app.services import access, audit, creators, project_isolation, resource_shares
from app.services.visibility import Viewer, hidden_aws_ids
from tests.test_project_scope import _member_of, _user, app  # noqa: F401
from tests.test_resource_shares import _golden, _share, world  # noqa: F401

WS = DEFAULT_WORKSPACE_ID
BETA = {"X-Project": "beta"}
ITEMS = [{"scenario_id": "s1", "turns": [{"input": "hi", "expected_response": "hello"}]}]


def _dataset(pid: str, name: str = "scratch") -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = EvalDataset(workspace_id=WS, name=name, items=ITEMS, kind="predefined")
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _creator(pid: str, kind: str, resource_id: str, created_by: str | None = None) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(ResourceCreator(workspace_id=WS, kind=kind, resource_id=resource_id,
                                   created_by=created_by))
            db.commit()
    finally:
        db.close()


def _in(pid: str, fn, *args, **kwargs):
    db = SessionLocal()
    try:
        with project_context(pid):
            forget_shared_rows()
            return fn(db, *args, **kwargs)
    finally:
        db.close()


# ── the helpers: what a share allows, and its label ──────────────────────────


def test_refused_ids_and_the_label_follow_the_access_level(world):
    _creator("alpha", creators.EVALUATOR, "judge-a")
    _creator("alpha", creators.EVALUATOR, "judge-b")
    _share(world.olga, kind="evaluator", resource_id="judge-a")
    _share(world.olga, kind="evaluator", resource_id="judge-b", access="use")
    ids = ["judge-a", "judge-b", "judge-own"]
    assert _in("beta", resource_shares.refused_ids, creators.EVALUATOR, ids, "use") == {
        "judge-a"}
    assert _in("beta", resource_shares.refused_ids, creators.EVALUATOR, ids, "own") == {
        "judge-a", "judge-b"}
    labels = _in("beta", resource_shares.shared_from, creators.EVALUATOR, ids)
    assert labels == {
        "judge-a": {"project_id": "alpha", "name": "Alpha", "access": "read"},
        "judge-b": {"project_id": "alpha", "name": "Alpha", "access": "use"},
    }
    # nothing shared, or no project: no label, nothing refused
    assert _in("gamma", resource_shares.shared_from, creators.EVALUATOR, ids) == {}
    assert _in(None, resource_shares.refused_ids, creators.EVALUATOR, ids, "own") == set()


def test_workspace_isolation_does_not_hide_a_shared_id(world):
    # created by a member of alpha (not an admin): an isolated beta member's rule
    # would hide it — but it is alpha's creation, lent to beta
    _creator("alpha", creators.KB, "KBALPHA001", created_by="u-someone")
    viewer = Viewer(user_id="u-bob", filters=True, project_id="beta")

    def hidden(db):
        return hidden_aws_ids(viewer, db, WS, creators.KB, ["KBALPHA001"])

    assert _in("beta", hidden) == {"KBALPHA001"}
    _share(world.olga, kind="knowledge_base", resource_id="KBALPHA001")
    assert _in("beta", hidden) == set()
    assert _in("gamma", hidden) == {"KBALPHA001"}  # not the grantee


# ── datasets: runs need `use`, changes are never a share's ───────────────────


def test_an_evaluation_runs_on_a_use_share_only(world):
    dataset = _dataset("alpha")
    ws = SimpleNamespace(id=WS)

    def get(db, need):
        return eval_routers._dataset_in(db, ws, dataset, need=need).id

    with pytest.raises(AppError) as err:
        _in("beta", get, "read")
    assert err.value.code == "dataset.not_found"  # not shared yet: another project's
    read = _share(world.olga, resource_id=dataset).json()["id"]
    assert _in("beta", get, "read") == dataset
    with pytest.raises(AppError) as err:
        _in("beta", get, "use")
    assert err.value.code == "share.read_only" and err.value.status_code == 404
    world.olga.delete(f"/api/projects/alpha/shares/{read}")
    _share(world.olga, resource_id=dataset, access="use")
    assert _in("beta", get, "use") == dataset
    with pytest.raises(AppError) as err:
        _in("beta", get, "own")
    assert err.value.code == "share.read_only"
    assert _in("alpha", get, "own") == dataset  # the owner is untouched


def test_a_shared_dataset_is_never_changed_by_the_grantee(world, monkeypatch):
    dataset = _dataset("alpha")
    _share(world.olga, resource_id=dataset, access="use")

    def no_aws(*_a, **_k):
        raise AssertionError("a shared dataset reached AWS")

    monkeypatch.setattr(eval_routers, "control_client", no_aws)
    for method, path, body in [
        ("PUT", f"/api/eval/datasets/{dataset}", {"name": "renamed"}),
        ("DELETE", f"/api/eval/datasets/{dataset}", None),
        ("POST", f"/api/eval/datasets/{dataset}/sync-to-aws", None),
        ("POST", f"/api/eval/datasets/{dataset}/publish-version", None),
    ]:
        res = world.bob.request(method, path, json=body, headers=BETA)
        assert res.status_code == 404, (path, res.text)
        assert res.json()["code"] == "share.read_only", path
    db = SessionLocal()
    try:
        with project_context(None):
            assert db.get(EvalDataset, dataset).name == "scratch"
    finally:
        db.close()


def test_the_dataset_and_golden_lists_label_what_was_lent(world):
    dataset = _dataset("alpha", "lent")
    golden = _golden("alpha", "lent-golden")
    mine = _dataset("beta", "mine")
    _share(world.olga, resource_id=dataset, access="use")
    _share(world.olga, resource_id=golden)
    rows = {d["id"]: d for d in world.bob.get("/api/eval/datasets", headers=BETA
                                              ).json()["datasets"]}
    assert rows[dataset]["shared_from"] == {"project_id": "alpha", "name": "Alpha",
                                            "access": "use"}
    assert "shared_from" not in rows[mine]
    # a golden set's splits carry their parent's label
    splits = [d for d in rows.values() if d["id"] not in (dataset, mine)]
    assert splits and all(d["shared_from"]["access"] == "read" for d in splits)
    sets = {g["id"]: g for g in world.bob.get("/api/golden-sets", headers=BETA
                                              ).json()["golden_sets"]}
    assert sets[golden]["shared_from"] == {"project_id": "alpha", "name": "Alpha",
                                           "access": "read"}
    # the owner sees its own rows unlabelled
    owner = world.olga.get("/api/golden-sets", headers={"X-Project": "alpha"}).json()
    assert all("shared_from" not in g for g in owner["golden_sets"])


# ── evaluators: scoring needs `use`, changing them is never a share's ────────


def test_evaluators_by_access_level(world):
    _creator("alpha", creators.EVALUATOR, "judge-a")
    share = _share(world.olga, kind="evaluator", resource_id="judge-a").json()["id"]
    viewer = Viewer(user_id=None, filters=False, project_id="beta")

    def check(db, need):
        online_evaluators.require_visible_evaluators(db, WS, viewer, ["judge-a"], need=need)

    def online(db):
        online_evaluators.refuse_hidden_evaluators(db, WS, viewer, ["judge-a"],
                                                   code_prefix="online_eval")

    _in("beta", check, "read")
    for need in ("use", "own"):
        with pytest.raises(ClientError):
            _in("beta", check, need)
    with pytest.raises(AppError) as err:
        _in("beta", online)
    assert err.value.code == "online_eval.evaluator_unsupported"
    assert err.value.detail["share"] == "read"
    world.olga.delete(f"/api/projects/alpha/shares/{share}")
    _share(world.olga, kind="evaluator", resource_id="judge-a", access="use")
    _in("beta", check, "use")
    _in("beta", online)
    with pytest.raises(ClientError):
        _in("beta", check, "own")


def test_a_batch_run_refuses_a_read_only_evaluator(world):
    _creator("alpha", creators.EVALUATOR, "judge-a")
    _share(world.olga, kind="evaluator", resource_id="judge-a")
    ws = SimpleNamespace(id=WS, context=None)

    def assert_refs(db):
        eval_routers._assert_target_references(db, ws, ["judge-a"], ITEMS, True)

    with pytest.raises(AppError) as err:
        _in("beta", assert_refs)
    assert err.value.code == "run.evaluator_share_read_only"


def test_the_evaluator_list_labels_what_was_lent(world, monkeypatch):
    _creator("alpha", creators.EVALUATOR, "judge-a")
    _creator("alpha", creators.EVALUATOR, "judge-kept")
    _share(world.olga, kind="evaluator", resource_id="judge-a", access="use")
    monkeypatch.setattr(eval_routers, "control_client", lambda _ctx: None)
    monkeypatch.setattr(eval_routers.ac, "list_evaluators", lambda _c: [
        {"evaluatorId": "judge-a", "evaluatorName": "judge-a", "evaluatorType": "Custom"},
        {"evaluatorId": "judge-kept", "evaluatorName": "kept", "evaluatorType": "Custom"},
    ])
    rows = world.bob.get("/api/eval/evaluators", headers=BETA).json()["evaluators"]
    custom = {r["id"]: r for r in rows if r.get("source") == "custom"}
    assert set(custom) == {"judge-a"}
    assert custom["judge-a"]["shared_from"]["access"] == "use"


# ── knowledge bases: read here, never changed ────────────────────────────────


def test_a_shared_kb_is_listed_with_its_label_and_never_changed(world, monkeypatch):
    from app.routers import knowledge as kb_router

    _creator("alpha", creators.KB, "KBALPHA001")
    _share(world.olga, kind="knowledge_base", resource_id="KBALPHA001")
    monkeypatch.setattr(kb_router.knowledge, "list_kbs", lambda _ctx: [
        {"kb_id": "KBALPHA001", "name": "policies", "status": "ACTIVE"}])
    items = world.bob.get("/api/knowledge-bases", headers=BETA).json()["items"]
    assert [i["shared_from"] for i in items] == [
        {"project_id": "alpha", "name": "Alpha", "access": "read"}]

    def no_aws(*_a, **_k):
        raise AssertionError("a shared KB reached AWS")

    monkeypatch.setattr(kb_router.knowledge, "update_description", no_aws)
    monkeypatch.setattr(kb_router.knowledge, "delete_kb", no_aws)
    res = world.bob.patch("/api/knowledge-bases/KBALPHA001", json={"description": "x"},
                          headers=BETA)
    assert res.status_code == 404 and res.json()["code"] == "share.read_only", res.text
    res = world.bob.delete("/api/knowledge-bases/KBALPHA001", headers=BETA)
    assert res.json()["code"] == "share.read_only"


# ── the release evidence ─────────────────────────────────────────────────────


def test_the_release_evidence_names_the_lending_project(world):
    from app.dlc import releases

    record = SimpleNamespace(project_id="beta", golden_versions={
        "regression": {"dataset_id": "d1", "version": None, "items": 3},
        "holdout": {"dataset_id": "d2", "version": "3", "items": 2,
                    "shared_from": {"project_id": "alpha", "name": "Alpha"}},
    })
    cset = SimpleNamespace(project_id="alpha", version=2)
    notes = _in(None, releases.shared_evidence, record, cset)
    assert notes == ["used project Alpha's criteria v2",
                     "used project Alpha's golden set holdout v3"]
    own = SimpleNamespace(project_id="beta", version=2)
    record.golden_versions = {"holdout": {"dataset_id": "d2", "version": "3"}}
    assert _in(None, releases.shared_evidence, record, own) == []
    # a split of another project is labelled with its owner, the project's own is not
    split = SimpleNamespace(project_id="alpha")
    assert _in(None, releases._lent_by, split, "beta") == {"project_id": "alpha",
                                                          "name": "Alpha"}
    assert _in(None, releases._lent_by, split, "alpha") is None


# ── when a share ends ────────────────────────────────────────────────────────


@pytest.fixture
def bea(world):
    """beta's owner — the one told when a share it received ends."""
    uid = _user("bea", grant=False)
    _member_of("beta", uid, "project_owner")
    access.reset_cache_for_tests()
    return SimpleNamespace(user_id=uid, username="bea")


def test_revoking_tells_the_grantees_owners(world, bea):
    dataset = _dataset("alpha", "lent")
    share = _share(world.olga, resource_id=dataset).json()["id"]
    assert _in(None, resource_shares.notices_for, bea)["items"] == []
    world.olga.delete(f"/api/projects/alpha/shares/{share}")
    notices = _in(None, resource_shares.notices_for, bea)
    assert notices["unread"] == 1
    item = notices["items"][0]
    assert item["kind"] == "revoked" and item["share_id"] == share
    assert item["owner_project_name"] == "Alpha" and item["project_id"] == "beta"
    assert item["resource_name"] == "lent" and item["actor"] == "olga"
    assert "recipients" not in item
    # only a recipient reads or acknowledges it
    other = SimpleNamespace(user_id="u-other", username="other")
    assert _in(None, resource_shares.notices_for, other)["items"] == []
    with pytest.raises(AppError):
        _in(None, resource_shares.acknowledge, other, item["id"])

    def ack(db):
        resource_shares.acknowledge(db, bea, item["id"])
        db.commit()

    _in(None, ack)
    assert _in(None, resource_shares.notices_for, bea)["unread"] == 0


def test_an_expired_share_tells_them_too_and_a_request_does_not(world, bea):
    from datetime import UTC, datetime, timedelta

    from app.models.shares import ResourceShare

    dataset = _dataset("alpha", "lent")
    share = _share(world.olga, resource_id=dataset).json()["id"]
    db = SessionLocal()
    try:
        db.get(ResourceShare, share).expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
        resource_shares.expire_due(db)
    finally:
        db.close()
    items = _in(None, resource_shares.notices_for, bea)["items"]
    assert [i["kind"] for i in items] == ["expired"] and items[0]["actor"] == "scheduler"


def _isolate(pid: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        row.settings = {**(row.settings or {}), "isolation": {"mode": "isolated"}}
        db.commit()
    finally:
        db.close()


def _provision_jobs(pid: str) -> list[Job]:
    db = SessionLocal()
    try:
        with project_context(None):
            return list(db.scalars(select(Job).where(
                Job.type == project_isolation.JOB_TYPE, Job.project_id == pid)))
    finally:
        db.close()


def test_revoking_a_used_kb_re_renders_an_isolated_grantees_boundary(world, monkeypatch):
    started: list[list[str]] = []
    monkeypatch.setattr(project_isolation, "start_jobs", lambda ids: started.append(list(ids)))
    _creator("alpha", creators.KB, "KBALPHA001")
    _creator("alpha", creators.KB, "KBALPHA002")
    _isolate("beta")
    before = len(_provision_jobs("beta"))
    # a read share never reached the boundary: nothing to render
    read = _share(world.olga, kind="knowledge_base", resource_id="KBALPHA002").json()["id"]
    world.olga.delete(f"/api/projects/alpha/shares/{read}")
    assert len(_provision_jobs("beta")) == before and started == []
    use = _share(world.olga, kind="knowledge_base", resource_id="KBALPHA001",
                 access="use").json()["id"]
    assert world.olga.delete(f"/api/projects/alpha/shares/{use}").status_code == 200
    jobs = _provision_jobs("beta")
    assert len(jobs) == before + 1
    assert started == [[jobs[-1].id]] and jobs[-1].workspace_id == WS
    assert jobs[-1].payload["actor"] == "olga"


def test_a_shared_grantee_gets_no_provisioning_run(world, monkeypatch):
    monkeypatch.setattr(project_isolation, "start_jobs", lambda ids: None)
    _creator("alpha", creators.KB, "KBALPHA001")
    use = _share(world.olga, kind="knowledge_base", resource_id="KBALPHA001",
                 access="use").json()["id"]
    world.olga.delete(f"/api/projects/alpha/shares/{use}")
    assert _provision_jobs("beta") == []


def test_a_rolled_back_revocation_queues_nothing(world, monkeypatch):
    monkeypatch.setattr(project_isolation, "start_jobs", lambda ids: None)
    _creator("alpha", creators.KB, "KBALPHA001")
    _isolate("beta")
    use = _share(world.olga, kind="knowledge_base", resource_id="KBALPHA001",
                 access="use").json()["id"]
    before = len(_provision_jobs("beta"))
    db = SessionLocal()
    try:
        identity = SimpleNamespace(username="olga", user_id=None)
        resource_shares.revoke(db, identity, "alpha", use)
        db.rollback()
        db.commit()  # a later commit of the same session carries nothing over
    finally:
        db.close()
    assert len(_provision_jobs("beta")) == before


# ── the journal ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["project.share.grant", "project.share.revoke",
                                    "project.share.expire"])
def test_share_events_are_structure_events(action):
    assert audit.is_structure_action(action) and audit.steward_visible(action)
    assert not audit.is_structure_action("project.share.")
