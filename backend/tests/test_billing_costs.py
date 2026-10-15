"""Cost allocation tags and the billing-basis cost view (cost-tags design §1.4, §1.6,
CT-2), against a stubbed Cost Explorer: activation state and one-click activation
(standalone vs. organization member), `GetCostAndUsage` grouped by
`launchpad:project` and service with the shared row allocated by invocations, the
6-hour cache, a project's custom cost tags, the budget basis, and the routes."""

from datetime import date
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.core.route_policy import (
    ADMIN_SPLIT,
    DEPARTMENT_ADMIN_ROUTES,
    PROJECT_EXEMPT,
    ROUTE_POLICY,
    WORKSPACE_EXEMPT,
)
from app.models.projects import Project
from app.services import billing_costs as svc
from app.services import project_quotas
from app.services.workspace import WorkspaceContext
from tests.test_project_scope import _login, _member_of, _user, app  # noqa: F401
from tests.test_projects_api import _admin, _create


def _err(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code}!"}}, op)


def _group(tag: str, service: str, usd: float) -> dict:
    return {"Keys": [f"launchpad:project${tag}", service],
            "Metrics": {"UnblendedCost": {"Amount": str(usd), "Unit": "USD"}}}


class FakeCE:
    def __init__(self, groups=None, statuses=None, list_error=None, update_error=None):
        self.groups = groups if groups is not None else []
        self.statuses = statuses if statuses is not None else {}
        self.list_error, self.update_error = list_error, update_error
        self.calls: list[tuple[str, dict]] = []

    def get_cost_and_usage(self, **kw):
        self.calls.append(("get_cost_and_usage", kw))
        return {"ResultsByTime": [{"TimePeriod": {"Start": kw["TimePeriod"]["Start"],
                                                  "End": kw["TimePeriod"]["End"]},
                                   "Estimated": True, "Groups": self.groups}]}

    def list_cost_allocation_tags(self, **kw):
        self.calls.append(("list_cost_allocation_tags", kw))
        if self.list_error:
            raise self.list_error
        return {"CostAllocationTags": [
            {"TagKey": k, "Type": "UserDefined", "Status": s}
            for k, s in self.statuses.items() if k in kw["TagKeys"]]}

    def update_cost_allocation_tags_status(self, **kw):
        self.calls.append(("update_cost_allocation_tags_status", kw))
        if self.update_error:
            raise self.update_error
        for entry in kw["CostAllocationTagsStatus"]:
            self.statuses[entry["TagKey"]] = entry["Status"]
        return {"Errors": []}


class FakeOrg:
    def __init__(self, management: str | None):
        self.management = management

    def describe_organization(self):
        if self.management is None:
            raise _err("AWSOrganizationsNotInUseException", "DescribeOrganization")
        return {"Organization": {"MasterAccountId": self.management}}


@pytest.fixture
def stub(monkeypatch):
    svc.reset_cache()
    state = SimpleNamespace(ce=FakeCE(), org=FakeOrg(None), weights={})
    monkeypatch.setattr(svc, "ce_client", lambda ws: state.ce)
    monkeypatch.setattr(svc, "org_client", lambda ws: state.org)
    monkeypatch.setattr(svc, "invocation_weights", lambda db, ws: dict(state.weights))
    yield state
    svc.reset_cache()


@pytest.fixture
def ctx():
    return WorkspaceContext(account_id="111122223333", region="us-west-2", resources={})


def _db():
    return SessionLocal()


# ── routes ────────────────────────────────────────────────────────────────────


def test_routes_are_classified():
    assert ROUTE_POLICY[("GET", "/api/cost-tags")] == "admin"
    assert ADMIN_SPLIT[("POST", "/api/cost-tags/activate")] == "admin:data"
    assert ADMIN_SPLIT[("GET", "/api/costs/billing")] == "admin:data"
    assert ("GET", "/api/costs/billing") in PROJECT_EXEMPT
    assert ROUTE_POLICY[("GET", "/api/projects/{project_id}/cost-tags")] == "member"
    assert ROUTE_POLICY[("PUT", "/api/projects/{project_id}/cost-tags")] == (
        "pperm:project.members")
    assert DEPARTMENT_ADMIN_ROUTES[("PUT", "/api/projects/{project_id}/budget-basis")] == (
        "project")
    assert ("PUT", "/api/projects/{project_id}/cost-tags") in WORKSPACE_EXEMPT


# ── the billing view ──────────────────────────────────────────────────────────


def test_period_windows():
    assert svc.period("daily", date(2026, 3, 10)) == ("2026-02-09", "2026-03-11")
    assert svc.period("monthly", date(2026, 3, 10)) == ("2025-10-01", "2026-03-11")


def test_groups_by_project_and_service_and_allocates_shared_by_invocations(stub, ctx):
    stub.ce.groups = [
        _group("alpha", "Amazon Bedrock AgentCore", 6.0),
        _group("alpha", "Amazon S3", 1.0),
        _group("beta", "Amazon Bedrock AgentCore", 2.0),
        _group("shared", "Amazon Cognito", 4.0),
        _group("", "Amazon EC2", 10.0),
    ]
    stub.weights = {"alpha": 3, "beta": 1, "idle": 0}
    db = _db()
    try:
        report = svc.billing_report(db, ctx, granularity="daily")
    finally:
        db.close()
    call = stub.ce.calls[0][1]
    assert call["Granularity"] == "DAILY"
    assert call["GroupBy"] == [{"Type": "TAG", "Key": "launchpad:project"},
                               {"Type": "DIMENSION", "Key": "SERVICE"}]
    assert call["Filter"] == {"Dimensions": {"Key": "REGION", "Values": ["us-west-2"]}}
    rows = {r["project_id"]: r for r in report["by_project"]}
    assert set(rows) == {"alpha", "beta"}
    assert rows["alpha"]["tagged_usd"] == 7.0
    assert rows["alpha"]["allocated_shared_usd"] == 3.0
    assert rows["alpha"]["total_usd"] == 10.0
    assert rows["alpha"]["services"][0] == {"service": "Amazon Bedrock AgentCore", "usd": 6.0}
    assert rows["beta"]["total_usd"] == 3.0
    assert report["shared"]["usd"] == 4.0 and report["shared"]["allocated"]
    assert report["shared"]["weights"] == {"alpha": 3, "beta": 1}
    assert report["untagged"]["usd"] == 10.0
    assert report["total_usd"] == 23.0  # every billed dollar, once
    assert report["estimated"] is True
    assert report["series"][0]["by_project"] == {"alpha": 7.0, "beta": 2.0}


def test_no_invocations_leaves_the_shared_row_unallocated(stub, ctx):
    stub.ce.groups = [_group("shared", "Amazon Cognito", 4.0)]
    db = _db()
    try:
        report = svc.billing_report(db, ctx)
    finally:
        db.close()
    assert report["by_project"] == []
    assert report["shared"]["allocated"] is False
    assert report["total_usd"] == 4.0


def test_narrowed_to_a_project(stub, ctx):
    stub.ce.groups = [_group("alpha", "S", 1.0), _group("beta", "S", 2.0),
                      _group("shared", "S", 2.0), _group("", "S", 5.0)]
    stub.weights = {"alpha": 1, "beta": 1}
    db = _db()
    try:
        report = svc.billing_report(db, ctx, project="alpha")
    finally:
        db.close()
    assert [r["project_id"] for r in report["by_project"]] == ["alpha"]
    assert report["total_usd"] == 2.0
    assert report["untagged"] is None
    assert report["series"][0]["by_project"] == {"alpha": 1.0}


def test_cached_six_hours_and_force_refreshes(stub, ctx, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(svc, "_now", lambda: clock[0])
    db = _db()
    try:
        first = svc.billing_report(db, ctx)
        clock[0] += 5 * 3600
        second = svc.billing_report(db, ctx)
        assert first["cache"]["hit"] is False and second["cache"]["hit"] is True
        assert len(stub.ce.calls) == 1
        clock[0] += 2 * 3600  # past 6 h
        svc.billing_report(db, ctx)
        assert len(stub.ce.calls) == 2
        svc.billing_report(db, ctx, force=True)
        assert len(stub.ce.calls) == 3
        svc.billing_report(db, ctx, granularity="monthly")  # its own entry
        assert stub.ce.calls[-1][1]["Granularity"] == "MONTHLY"
    finally:
        db.close()


def test_bad_granularity_and_unreadable_ce(stub, ctx):
    db = _db()
    try:
        with pytest.raises(AppError) as bad:
            svc.billing_report(db, ctx, granularity="hourly")
        assert bad.value.status_code == 422

        def denied(**kw):
            raise _err("AccessDeniedException", "GetCostAndUsage")

        stub.ce.get_cost_and_usage = denied
        with pytest.raises(AppError) as err:
            svc.billing_report(db, ctx)
        assert err.value.code == "costs.billing_unavailable" and err.value.status_code == 403
    finally:
        db.close()


def test_project_month_to_date(stub, ctx):
    stub.ce.groups = [_group("alpha", "S", 3.0), _group("shared", "S", 2.0),
                      _group("", "S", 9.0)]
    stub.weights = {"alpha": 1, "beta": 1}
    db = _db()
    try:
        assert svc.project_month_to_date_usd(db, ctx, "alpha") == 4.0
        assert svc.project_month_to_date_usd(db, ctx, "beta") == 1.0
        # the single-project workspace: all tagged and shared spend, never untagged
        assert svc.project_month_to_date_usd(db, ctx, None) == 5.0
    finally:
        db.close()
    assert stub.ce.calls[0][1]["Granularity"] == "MONTHLY"
    assert len(stub.ce.calls) == 1  # cached


# ── activation ────────────────────────────────────────────────────────────────


def test_status_reports_each_key(stub, ctx):
    stub.ce.statuses = {"launchpad:project": "Inactive", "launchpad:workspace": "Active"}
    db = _db()
    try:
        status = svc.activation_status(db, ctx)
    finally:
        db.close()
    states = {t["key"]: t["status"] for t in status["tags"]}
    assert states["launchpad:project"] == "inactive"
    assert states["launchpad:workspace"] == "active"
    assert states["launchpad:agent-id"] == "not_found"
    assert status["account_type"] == "standalone" and status["can_activate"]
    assert status["project_tag_active"] is False


def test_standalone_activates_inactive_keys(stub, ctx):
    stub.ce.statuses = {"launchpad:project": "Inactive", "launchpad:workspace": "Active"}
    db = _db()
    try:
        result = svc.activate(db, ctx)
    finally:
        db.close()
    assert result["activated"] == ["launchpad:project"]
    assert result["already_active"] == ["launchpad:workspace"]
    assert "launchpad:agent-id" in result["not_found"]
    update = next(kw for name, kw in stub.ce.calls
                  if name == "update_cost_allocation_tags_status")
    assert update["CostAllocationTagsStatus"] == [
        {"TagKey": "launchpad:project", "Status": "Active"}]
    assert result["status"]["project_tag_active"] is True


def test_member_account_gets_instructions_not_a_call(stub, ctx):
    stub.org = FakeOrg("999988887777")
    stub.ce.statuses = {"launchpad:project": "Inactive"}
    db = _db()
    try:
        status = svc.activation_status(db, ctx)
        assert status["account_type"] == "member" and not status["can_activate"]
        assert status["management_account_id"] == "999988887777"
        with pytest.raises(AppError) as err:
            svc.activate(db, ctx)
    finally:
        db.close()
    assert err.value.code == "cost_tags.org_member" and err.value.status_code == 409
    assert err.value.detail["management_account_id"] == "999988887777"
    assert not any(n == "update_cost_allocation_tags_status" for n, _ in stub.ce.calls)


def test_management_account_can_activate(stub, ctx):
    stub.org = FakeOrg("111122223333")
    db = _db()
    try:
        status = svc.activation_status(db, ctx)
    finally:
        db.close()
    assert status["account_type"] == "management" and status["can_activate"]


def test_denied_activation_and_unreadable_status(stub, ctx):
    stub.ce.statuses = {"launchpad:project": "Inactive"}
    stub.ce.update_error = _err("AccessDeniedException")
    db = _db()
    try:
        with pytest.raises(AppError) as err:
            svc.activate(db, ctx)
        assert err.value.code == "cost_tags.activation_denied"
        svc.reset_cache()
        stub.ce.list_error = _err("AccessDeniedException")
        status = svc.activation_status(db, ctx)
    finally:
        db.close()
    assert status["error"]["code"] == "AccessDeniedException"
    assert not status["can_activate"]
    assert {t["status"] for t in status["tags"]} == {"unknown"}


# ── custom cost tags ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, fragment", [
    ({"aws:x": "1"}, "reserved"),
    ({"Launchpad:project": "x"}, "reserved"),
    ({f"k{i}": "v" for i in range(11)}, "at most 10"),
    ({"": "v"}, "1–128"),
    ({"k" * 129: "v"}, "1–128"),
    ({"k": "v" * 257}, "256"),
    ({"bad#key": "v"}, "letters"),
    ({"k": "bad*value"}, "letters"),
    ({"Cost": "1", "cost": "2"}, "twice"),
    ({"k": 1}, "string"),
    (["k"], "object"),
])
def test_custom_tags_are_validated(value, fragment):
    with pytest.raises(AppError) as err:
        svc.check_custom_tags(value)
    assert err.value.status_code == 422 and fragment in err.value.message


def test_custom_tags_are_cleaned():
    assert svc.check_custom_tags({" CostCenter ": " 1234 ", "team": "a/b@c"}) == {
        "CostCenter": "1234", "team": "a/b@c"}


# ── API ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def world(app, stub):  # noqa: F811
    admin = _admin(app)
    dev = _user("dev", grant=False)
    _user("out", grant=False)
    _create(admin, "alpha")
    _member_of("alpha", dev)
    return SimpleNamespace(admin=admin, dev=_login(app, "dev"), out=_login(app, "out"),
                           stub=stub)


def test_project_cost_tags_api(world):
    res = world.admin.get("/api/projects/alpha/cost-tags")
    assert res.status_code == 200, res.text
    assert res.json()["cost_tags"] == {} and res.json()["budget_basis"] == "estimate"
    assert world.out.get("/api/projects/alpha/cost-tags").status_code == 404
    assert world.dev.get("/api/projects/alpha/cost-tags").status_code == 200
    assert world.dev.put("/api/projects/alpha/cost-tags",
                         json={"cost_tags": {"CostCenter": "1"}}).status_code == 403
    bad = world.admin.put("/api/projects/alpha/cost-tags",
                          json={"cost_tags": {"launchpad:project": "x"}})
    assert bad.status_code == 422 and bad.json()["code"] == "cost_tags.invalid"
    ok = world.admin.put("/api/projects/alpha/cost-tags",
                         json={"cost_tags": {"CostCenter": "1234"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["cost_tags"] == {"CostCenter": "1234"}
    # the custom key joins the activation list
    status = world.admin.get("/api/cost-tags").json()["activation"]
    assert {"key": "CostCenter", "custom": True}.items() <= next(
        t for t in status["tags"] if t["key"] == "CostCenter").items()
    cleared = world.admin.put("/api/projects/alpha/cost-tags", json={"cost_tags": {}})
    assert cleared.json()["cost_tags"] == {}
    db = SessionLocal()
    try:
        with project_context(None):
            assert "cost_tags" not in (db.get(Project, "alpha").settings or {})
    finally:
        db.close()


def test_budget_basis_api_and_quotas_view(world):
    assert world.dev.put("/api/projects/alpha/budget-basis",
                         json={"basis": "billing"}).status_code == 403
    assert world.admin.put("/api/projects/alpha/budget-basis",
                           json={"basis": "nope"}).status_code == 422
    res = world.admin.put("/api/projects/alpha/budget-basis", json={"basis": "billing"})
    assert res.status_code == 200, res.text
    assert res.json()["budget_basis"] == "billing"
    view = world.dev.get("/api/projects/alpha/quotas").json()
    assert view["budget_basis"] == "billing"
    item = next(i for i in view["items"] if i["key"] == "monthly_cost_alert_usd")
    assert item["basis"] == "billing"
    world.admin.put("/api/projects/alpha/budget-basis", json={"basis": "estimate"})
    db = SessionLocal()
    try:
        with project_context(None):
            assert "budget_basis" not in (db.get(Project, "alpha").settings or {})
    finally:
        db.close()


def test_budget_spend_follows_the_basis(world, monkeypatch, ctx):
    from app.services import costs

    world.stub.ce.groups = [_group("alpha", "S", 7.0)]
    monkeypatch.setattr(costs, "month_to_date_usd", lambda db, ws, project=None: 1.5)
    db = SessionLocal()
    try:
        assert project_quotas.budget_spend(db, ctx, "alpha", several=True)[0] == 1.5
        world.admin.put("/api/projects/alpha/budget-basis", json={"basis": "billing"})
        db.expire_all()
        usd, detail = project_quotas.budget_spend(db, ctx, "alpha", several=True)
        assert usd == 7.0 and "billed" in detail

        def boom(**kw):
            raise _err("AccessDeniedException")

        svc.reset_cache()
        world.stub.ce.get_cost_and_usage = boom
        usd, detail = project_quotas.budget_spend(db, ctx, "alpha", several=True)
        assert usd is None and "billed" in detail
    finally:
        db.close()


def test_workspace_cost_tag_api(world):
    world.stub.ce.statuses = {"launchpad:project": "Inactive"}
    world.stub.ce.groups = [_group("alpha", "S", 2.0), _group("", "S", 1.0)]
    assert world.dev.get("/api/cost-tags").status_code == 403
    assert world.dev.get("/api/costs/billing").status_code == 403
    view = world.admin.get("/api/cost-tags")
    assert view.status_code == 200, view.text
    assert [t["key"] for t in view.json()["tag_set"]][0] == "launchpad:project"
    bill = world.admin.get("/api/costs/billing")
    assert bill.status_code == 200, bill.text
    assert bill.json()["project_tag_active"] is False
    assert bill.json()["total_usd"] == 3.0
    narrowed = world.admin.get("/api/costs/billing?project=alpha").json()
    assert narrowed["total_usd"] == 2.0
    assert world.admin.get("/api/costs/billing?project=ghost").status_code == 404
    act = world.admin.post("/api/cost-tags/activate", json={})
    assert act.status_code == 200, act.text
    assert act.json()["activated"] == ["launchpad:project"]
    world.stub.org = FakeOrg("999988887777")
    svc.reset_cache()
    member = world.admin.post("/api/cost-tags/activate", json={})
    assert member.status_code == 409 and member.json()["code"] == "cost_tags.org_member"
