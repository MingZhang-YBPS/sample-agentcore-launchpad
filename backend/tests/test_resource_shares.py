"""Cross-project sharing grants (projects design §11.2, G11-share): the routes and their
classification, who may grant / list / revoke / approve, the ORM criteria widening reads
(and only reads) to exactly the shared rows, revocation and expiry taking effect on the
next read, the `use`-level pin rule for knowledge bases (and the ended-share refusal),
the isolated grantee's boundary statement, and the journal. A project nothing is shared
with sees exactly what it saw before; another project's ids stay 404."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import forget_shared_rows, owned_get, project_context
from app.core.route_policy import (
    ADMIN_DATA,
    ADMIN_SPLIT,
    DEPARTMENT_ADMIN_ROUTES,
    ROUTE_POLICY,
    WORKSPACE_EXEMPT,
)
from app.dlc import golden
from app.evaluation.models import EvalDataset
from app.models.ledger import AuditEvent, ResourceCreator
from app.models.projects import Project
from app.models.shares import ResourceShare
from app.services import access, agent_access, creators, project_iam, resource_shares
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext
from tests.test_project_scope import _login, _member_of, _user, app  # noqa: F401
from tests.test_projects_api import _admin, _create

WS = DEFAULT_WORKSPACE_ID


def _golden(pid: str, name: str = "refunds") -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            parent = golden.create(db, WS, name=name, criteria_lineage_id=None, actor="t")
            db.commit()
            return parent.id
    finally:
        db.close()


def _kb(pid: str, kb_id: str) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(ResourceCreator(workspace_id=WS, kind=creators.KB, resource_id=kb_id))
            db.commit()
    finally:
        db.close()


def _visible(pid: str, dataset_id: str) -> bool:
    db = SessionLocal()
    try:
        with project_context(pid):
            listed = dataset_id in set(db.scalars(select(EvalDataset.id)))
            got = owned_get(db, EvalDataset, dataset_id) is not None
            assert listed == got
            return listed
    finally:
        db.close()


def _share(client, owner="alpha", **body):
    payload = {"workspace_id": WS, "kind": "eval_dataset", "grantee_project_id": "beta",
               "access": "read", **body}
    return client.post(f"/api/projects/{owner}/shares", json=payload)


@pytest.fixture
def world(app):  # noqa: F811
    """alpha (olga its owner, dev a developer), beta (bob a developer), gamma (gus)."""
    admin = _admin(app)
    for pid in ("alpha", "beta", "gamma"):
        _create(admin, pid)
    olga, dev, bob, gus, out = (_user(n, grant=False) for n in ("olga", "dev", "bob", "gus",
                                                                "out"))
    _member_of("alpha", olga, "project_owner")
    _member_of("alpha", dev)
    _member_of("beta", bob)
    _member_of("gamma", gus)
    access.reset_cache_for_tests()
    return SimpleNamespace(
        app=app, admin=admin, olga=_login(app, "olga"), dev=_login(app, "dev"),
        bob=_login(app, "bob"), gus=_login(app, "gus"), out=_login(app, "out"),
    )


# ── routes ────────────────────────────────────────────────────────────────────


def test_routes_are_classified():
    share = "pperm:project.share"
    assert ROUTE_POLICY[("GET", "/api/projects/{project_id}/shares")] == "member"
    assert ROUTE_POLICY[("POST", "/api/projects/{project_id}/shares")] == share
    assert ROUTE_POLICY[("GET", "/api/projects/{project_id}/shares/options")] == share
    assert ROUTE_POLICY[("DELETE", "/api/projects/{project_id}/shares/{share_id}")] == share
    approve = ("POST", "/api/projects/{project_id}/shares/{share_id}/approve")
    assert ROUTE_POLICY[approve] == "admin"
    assert DEPARTMENT_ADMIN_ROUTES[approve] == "project"
    assert ADMIN_SPLIT[approve] == ADMIN_DATA  # a steward does not open data (§11.1)
    assert ("POST", "/api/projects/{project_id}/shares") in WORKSPACE_EXEMPT


def test_the_owner_side_grants_and_nobody_else(world):
    dataset = _golden("alpha")
    # a member without project.share; a non-member of the owner; the grantee itself
    assert _share(world.dev, resource_id=dataset).status_code == 403
    assert _share(world.out, resource_id=dataset).status_code == 404
    assert _share(world.bob, owner="beta", resource_id=dataset).status_code == 403
    # even an administrator lends a resource only from the project that holds it
    res = _share(world.admin, owner="beta", resource_id=dataset, grantee_project_id="gamma")
    assert res.json()["code"] == "share.resource_not_found"
    res = _share(world.olga, resource_id=dataset, note="for the refunds pilot")
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "active" and body["access"] == "read"
    assert body["resource_name"] == "refunds" and body["grantee_project_name"] == "Beta"
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=89) < expires - datetime.now(UTC) <= timedelta(days=90)
    # one open grant per (resource, grantee)
    assert _share(world.olga, resource_id=dataset).json()["code"] == "share.exists"


@pytest.mark.parametrize(("body", "code"), [
    ({"grantee_project_id": "alpha"}, "share.self"),
    ({"grantee_project_id": "nope"}, "project.not_found"),
    ({"kind": "memory"}, "share.invalid_kind"),
    ({"access": "write"}, "share.invalid_access"),
    ({"expires_in_days": 400}, "share.invalid_expiry"),
    ({"workspace_id": "elsewhere"}, "workspace.not_found"),
])
def test_a_grant_is_validated(world, body, code):
    dataset = _golden("alpha")
    res = _share(world.olga, resource_id=dataset, **body)
    assert res.status_code in (404, 409, 422), res.text
    assert res.json()["code"] == code


def test_only_the_owners_own_resources_can_be_shared(world):
    theirs = _golden("gamma", "gamma-set")
    res = _share(world.olga, resource_id=theirs)
    assert res.status_code == 404 and res.json()["code"] == "share.resource_not_found"
    # a split is part of its golden set, not a resource of its own
    parent = _golden("alpha")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            split = db.scalar(select(EvalDataset.id).where(EvalDataset.split_of == parent))
    finally:
        db.close()
    assert _share(world.olga, resource_id=split).status_code == 404


def test_options_list_the_owners_resources_and_the_workspaces_projects(world):
    dataset = _golden("alpha")
    _golden("gamma", "gamma-set")
    _kb("alpha", "KBALPHA001")
    res = world.olga.get(f"/api/projects/alpha/shares/options?workspace_id={WS}")
    assert res.status_code == 200, res.text
    body = res.json()
    ids = {(r["kind"], r["id"]) for r in body["resources"]}
    assert ("eval_dataset", dataset) in ids and ("knowledge_base", "KBALPHA001") in ids
    assert all(r["name"] != "gamma-set" for r in body["resources"])
    assert {g["id"] for g in body["grantees"]} >= {"beta", "gamma"}
    assert "alpha" not in {g["id"] for g in body["grantees"]}
    assert world.dev.get(f"/api/projects/alpha/shares/options?workspace_id={WS}"
                         ).status_code == 403


# ── the boundary: exactly the shared rows, read-only ─────────────────────────


def test_a_share_opens_exactly_the_shared_rows_to_the_grantee(world):
    shared = _golden("alpha", "shared-set")
    kept = _golden("alpha", "kept-set")
    assert not _visible("beta", shared)
    assert _share(world.olga, resource_id=shared).status_code == 201
    assert _visible("beta", shared) and not _visible("beta", kept)
    assert not _visible("gamma", shared)  # not the grantee: still another project's
    db = SessionLocal()
    try:
        with project_context("beta"):
            splits = set(db.scalars(select(EvalDataset.split).where(
                EvalDataset.split_of == shared)))
            assert splits == {"dev", "regression", "holdout"}  # the set comes whole
    finally:
        db.close()
    # over HTTP: the grantee reads it, a third project gets the missing answer
    beta = {"X-Project": "beta"}
    res = world.bob.get(f"/api/golden-sets/{shared}", headers=beta)
    assert res.status_code == 200, res.text
    assert res.json()["id"] == shared
    listed = {g["id"] for g in world.bob.get("/api/golden-sets", headers=beta
                                             ).json()["golden_sets"]}
    assert shared in listed and kept not in listed
    assert world.gus.get(f"/api/golden-sets/{shared}",
                         headers={"X-Project": "gamma"}).status_code == 404
    assert world.bob.get(f"/api/golden-sets/{kept}", headers=beta).status_code == 404


def test_a_share_never_grants_a_write(world):
    shared = _golden("alpha")
    assert _share(world.olga, resource_id=shared, access="use").status_code == 201
    res = world.bob.post(f"/api/golden-sets/{shared}/items", headers={"X-Project": "beta"},
                         json={"split": "dev", "items": [{"scenario_id": "s1", "turns": [
                             {"input": "hi", "expected_response": "hello"}]}]})
    assert res.status_code == 404, res.text
    assert res.json()["code"] == "share.read_only"
    db = SessionLocal()
    try:
        with project_context("beta"):
            row = db.get(EvalDataset, shared)
            row.name = "renamed"
            with pytest.raises(AppError) as err:
                db.commit()
            assert err.value.code == "share.read_only"
            db.rollback()
            # bulk statements stay on the project's own rows
            db.query(EvalDataset).filter(EvalDataset.id == shared).update({"name": "x"})
            db.commit()
        with project_context(None):
            assert db.get(EvalDataset, shared).name == "refunds"
    finally:
        db.close()


def test_revocation_takes_effect_on_the_next_read(world):
    shared = _golden("alpha")
    share_id = _share(world.olga, resource_id=shared).json()["id"]
    assert _visible("beta", shared)
    # only the owner side revokes: through the grantee's path the share does not exist
    assert world.admin.delete(f"/api/projects/beta/shares/{share_id}"
                              ).json()["code"] == "share.not_found"
    res = world.olga.delete(f"/api/projects/alpha/shares/{share_id}")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "revoked" and res.json()["revoked_by"] == "olga"
    forget_shared_rows()
    assert not _visible("beta", shared)
    assert world.bob.get(f"/api/golden-sets/{shared}",
                         headers={"X-Project": "beta"}).status_code == 404
    assert world.olga.delete(f"/api/projects/alpha/shares/{share_id}"
                             ).json()["code"] == "share.not_open"


def test_both_sides_list_the_share_and_outsiders_get_404(world):
    shared = _golden("alpha")
    share_id = _share(world.olga, resource_id=shared).json()["id"]
    alpha = world.dev.get("/api/projects/alpha/shares").json()
    assert [s["id"] for s in alpha["outgoing"]] == [share_id] and alpha["incoming"] == []
    beta = world.bob.get("/api/projects/beta/shares?direction=incoming").json()
    assert [s["owner_project_name"] for s in beta["incoming"]] == ["Alpha"]
    assert "outgoing" not in beta
    assert world.gus.get("/api/projects/alpha/shares").status_code == 404
    world.olga.delete(f"/api/projects/alpha/shares/{share_id}")
    assert world.dev.get("/api/projects/alpha/shares").json()["outgoing"] == []
    closed = world.dev.get("/api/projects/alpha/shares?include_closed=true").json()
    assert [s["status"] for s in closed["outgoing"]] == ["revoked"]


def test_expiry_is_swept_and_stops_counting(world):
    shared = _golden("alpha")
    share_id = _share(world.olga, resource_id=shared, expires_in_days=1).json()["id"]
    assert _visible("beta", shared)
    db = SessionLocal()
    try:
        row = db.get(ResourceShare, share_id)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        access.bump_epoch(db)  # stands in for the cached value reaching its expiry
        db.commit()
        forget_shared_rows()
        # past its date it no longer opens anything, even before the sweep runs
        assert not _visible("beta", shared)
        assert resource_shares.expire_due(db) == {"expired": [share_id]}
        db.expire_all()
        assert db.get(ResourceShare, share_id).status == "expired"
        with project_context(None):
            actions = list(db.scalars(select(AuditEvent.action).where(
                AuditEvent.project_id == "alpha", AuditEvent.action.like("project.share.%"))))
        assert sorted(actions) == ["project.share.expire", "project.share.grant"]
    finally:
        db.close()


def test_the_scheduler_runs_the_expiry_sweep():
    from app.dlc import scheduler

    assert ("project.share_expiry", scheduler._expire_shares, scheduler.TICK_SECONDS
            ) in scheduler.TASKS


def test_a_cross_department_share_waits_for_the_grantee_department(world):
    admin = world.admin
    for did in ("retail", "ops"):
        assert admin.post("/api/departments", json={"id": did, "name": did}).status_code == 201
    db = SessionLocal()
    try:
        db.get(Project, "alpha").department_id = "retail"
        db.get(Project, "beta").department_id = "ops"
        db.commit()
    finally:
        db.close()
    dana = _user("dana", grant=False)
    assert admin.put("/api/departments/ops/admins", json={"add": [dana]}).status_code == 200
    dana_client = _login(world.app, "dana")
    shared = _golden("alpha")
    res = _share(world.olga, resource_id=shared)
    assert res.json()["status"] == "requested", res.text
    share_id = res.json()["id"]
    assert not _visible("beta", shared)
    # the grantee's member cannot approve; the owner's department is not the approver
    assert world.bob.post(f"/api/projects/beta/shares/{share_id}/approve").status_code == 403
    res = dana_client.post(f"/api/projects/beta/shares/{share_id}/approve")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "active" and res.json()["approved_by"] == "dana"
    forget_shared_rows()
    assert _visible("beta", shared)
    # an administrator granting across departments is the approver already
    other = _golden("alpha", "second")
    assert _share(admin, resource_id=other).json()["status"] == "active"


# ── knowledge bases: pins, revocation, the isolated boundary ─────────────────


def _pin_check(spec, previous=None) -> None:
    db = SessionLocal()
    try:
        with project_context("beta"):
            forget_shared_rows()
            workspace = WorkspaceContext(account_id="111122223333", region="us-west-2")
            agent_access.check_pins(db, Viewer.ALL, workspace, spec, previous_spec=previous)
    finally:
        db.close()


def test_a_use_share_lets_the_grantee_pin_a_knowledge_base(world):
    _kb("alpha", "KBALPHA001")
    spec = {"knowledge_bases": [{"kb_id": "KBALPHA001"}]}
    with pytest.raises(AppError) as err:
        _pin_check(spec)
    assert err.value.code == "kb.not_found"
    # read level is not enough to pin
    read = _share(world.olga, kind="knowledge_base", resource_id="KBALPHA001")
    assert read.status_code == 201, read.text
    with pytest.raises(AppError):
        _pin_check(spec)
    world.olga.delete(f"/api/projects/alpha/shares/{read.json()['id']}")
    use = _share(world.olga, kind="knowledge_base", resource_id="KBALPHA001", access="use")
    _pin_check(spec)
    db = SessionLocal()
    try:
        assert agent_access.foreign_kb_ids(db, WS, {"KBALPHA001"}, project_id="beta") == set()
        assert agent_access.foreign_kb_ids(db, WS, {"KBALPHA001"}, project_id="gamma") == {
            "KBALPHA001"}
    finally:
        db.close()
    # revoked: an agent that kept the pin is refused on its next publish
    res = world.olga.delete(f"/api/projects/alpha/shares/{use.json()['id']}")
    assert res.status_code == 200
    with pytest.raises(AppError) as err:
        _pin_check(spec, previous=spec)
    assert err.value.code == "kb.share_revoked"
    # a kept pin of the project's own KB is untouched by the rule
    _kb("beta", "KBBETA0001")
    own = {"knowledge_bases": [{"kb_id": "KBBETA0001"}]}
    _pin_check(own, previous=own)


def test_the_isolated_grantees_boundary_reads_the_shared_kb(world):
    _kb("alpha", "KBALPHA001")
    db = SessionLocal()
    try:
        from app.models.ledger import Workspace

        ws = db.get(Workspace, WS)
        account, region = ws.account_id, ws.region
    finally:
        db.close()
    inp = project_iam.BoundaryInputs(project_id="beta", account_id=account, region=region)
    assert _shared_sid(project_iam.boundary_policy_document(inp)) is None
    _share(world.olga, kind="knowledge_base", resource_id="KBALPHA001", access="use")
    statement = _shared_sid(project_iam.boundary_policy_document(inp))
    assert statement is not None
    assert statement["Resource"] == [
        f"arn:aws:bedrock:{region}:{account}:knowledge-base/KBALPHA001"]
    assert set(statement["Action"]) == {"bedrock:GetKnowledgeBase", "bedrock:Retrieve*"}
    # a pinned KB is already listed by the boundary itself; gamma gets nothing
    pinned = project_iam.BoundaryInputs(project_id="beta", account_id=account, region=region,
                                        kb_ids=("KBALPHA001",))
    assert _shared_sid(project_iam.boundary_policy_document(pinned)) is None
    gamma = project_iam.BoundaryInputs(project_id="gamma", account_id=account, region=region)
    assert _shared_sid(project_iam.boundary_policy_document(gamma)) is None


def _shared_sid(document):
    return next((s for s in document["Statement"] if s.get("Sid") == "KbShared"), None)


def test_the_journal_names_the_owner_and_the_grantee(world):
    shared = _golden("alpha")
    share_id = _share(world.olga, resource_id=shared).json()["id"]
    world.olga.delete(f"/api/projects/alpha/shares/{share_id}")
    db = SessionLocal()
    try:
        with project_context(None):
            rows = list(db.scalars(select(AuditEvent).where(
                AuditEvent.action.like("project.share.%")).order_by(AuditEvent.created_at)))
    finally:
        db.close()
    assert [r.action for r in rows] == ["project.share.grant", "project.share.revoke"]
    assert {r.project_id for r in rows} == {"alpha"}
    assert all(r.target == f"eval_dataset:{shared} -> project:beta" for r in rows)
    assert rows[0].actor == "olga"


def test_a_cached_share_set_goes_stale_at_its_first_expiry():
    past = datetime.now(UTC) - timedelta(seconds=1)
    assert access._expired(access.SharedResources(valid_until=past), datetime.now(UTC))
    future = datetime.now(UTC) + timedelta(days=1)
    assert not access._expired(access.SharedResources(valid_until=future), datetime.now(UTC))
    assert not access._expired(access.SharedResources(), datetime.now(UTC))


def test_nothing_shared_reads_exactly_as_before(world):
    """A project that receives nothing: the widened criterion is `project_id = :pid`."""
    mine = _golden("beta", "beta-set")
    theirs = _golden("alpha")
    assert _visible("beta", mine) and not _visible("beta", theirs)
    db = SessionLocal()
    try:
        assert access.shared_for(db, "beta") == access.SharedResources()
    finally:
        db.close()
