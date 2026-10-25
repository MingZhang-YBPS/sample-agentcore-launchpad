"""VPC agents after deploy (design projects §12.8, §12.9, §12): the informational
inbound-endpoint and cost entries of the preflight, the observability hint, the
teardown's ENI wait (an injected EC2 stand-in — no AWS call), the profile archive guard
while ENIs are pending, and the cross-workspace promotion of a profiled agent through
`network_profile` resource mappings."""

import json
from typing import Any

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.deployer import pipeline
from app.models.ledger import Agent, Job, Promotion, ReleaseBundle, Workspace
from app.models.network import NetworkProfile, ProjectNetworkProfile
from app.services import network_preflight as preflight
from app.services import network_profiles as service
from app.services import promotion_exec
from app.services import resource_mapping as rm
from tests.test_environments import bind_test_project
from tests.test_network_profile_check import REGION, SG, SUBNETS, FakeEc2, _check, _vpc

TARGET = "prod-ws"


def _profile(db, pid: str = "np-0123456789ab", *, workspace: str = DEFAULT_WORKSPACE_ID,
             name: str = "core-vpc", **fields: Any) -> NetworkProfile:
    row = NetworkProfile(id=pid, workspace_id=workspace, name=name, vpc_id="vpc-0123456789abcdef0",
                         subnet_ids=list(SUBNETS), security_group_ids=[SG], purpose="agents",
                         internet="none", status=fields.pop("status", "ok"), **fields)
    db.add(row)
    db.commit()
    return row


def _agent(db, pid: str | None = "np-0123456789ab", *, name: str = "vpc-agent",
           resource_id: str = "rt-abc123", status: str = "active") -> Agent:
    spec: dict[str, Any] = {"name": name, "method": "zip_runtime", "system_prompt": "p"}
    if pid:
        spec["network"] = {"profile_id": pid}
    row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime",
                status=status, spec=spec, resource_id=resource_id,
                arn=f"arn:aws:bedrock-agentcore:us-west-2:1:runtime/{resource_id}")
    db.add(row)
    db.commit()
    return row


# ── §12.8: the inbound PrivateLink endpoint ───────────────────────────────────


def test_the_inbound_entry_reports_the_endpoint_and_its_policy():
    data = _vpc()
    for ep in data["endpoints"]:
        if ep["ServiceName"].endswith(".bedrock-agentcore"):
            ep["PolicyDocument"] = json.dumps({"Statement": [
                {"Effect": "Allow", "Principal": {"AWS": ["arn:aws:iam::1:role/caller"]},
                 "Action": "*", "Resource": "*"}]})
    inbound = _check(data)["inbound.endpoint"]
    assert inbound["severity"] == "info"
    assert inbound["details"]["private_inbound"] is True
    [ep] = inbound["details"]["endpoints"]
    assert ep["principals"] == ["arn:aws:iam::1:role/caller"] and not ep["any_principal"]
    assert "arn:aws:iam::1:role/caller" in inbound["message"]


def test_no_inbound_endpoint_is_informational_and_oauth_agents_get_the_note():
    data = _vpc()
    data["endpoints"] = [e for e in data["endpoints"]
                         if not e["ServiceName"].endswith(".bedrock-agentcore")]
    results = _check(data, features={"always", "inbound_oauth"})
    inbound = results["inbound.endpoint"]
    assert inbound["severity"] == "info" and inbound["details"]["private_inbound"] is False
    assert "public endpoint" in inbound["message"]
    assert 'Principal "*"' in inbound["message"]
    # informational entries never move the status
    assert preflight.status_of([inbound, results["cost"]]) == "ok"


def test_oauth_inbound_is_a_feature_of_the_spec_but_needs_no_endpoint():
    features = preflight.features_of_spec({"method": "zip_runtime", "inbound_auth": {"x": 1}})
    assert "inbound_oauth" in features
    assert "inbound_oauth" not in {n for _s, _k, needs in preflight.ENDPOINT_MATRIX
                                   for n in needs}


def test_a_default_policy_admits_anyone():
    assert preflight._policy_principals(None) == ([], True)
    assert preflight._policy_principals(json.dumps(
        {"Statement": {"Effect": "Allow", "Principal": "*"}})) == (["*"], True)


# ── §12.9: cost and observability ─────────────────────────────────────────────


def test_the_cost_entry_counts_endpoints_azs_and_nat():
    data = _vpc()
    data["nats"] = [{"NatGatewayId": "nat-1", "State": "available"}]
    data["route_tables"][0]["Routes"].append(
        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"})
    cost = _check(data, internet="nat")["cost"]
    interface = sum(1 for _s, kind, _n in preflight.ENDPOINT_MATRIX if kind == "Interface")
    assert cost["severity"] == "info"
    assert len(cost["details"]["interface_endpoints"]) == interface
    assert cost["details"]["availability_zones"] == 2
    assert cost["details"]["endpoint_az_hours"] == interface * 2
    assert cost["details"]["nat_gateways"] == ["nat-1"]
    assert cost["details"]["pricing"]["privatelink"].startswith("https://aws.amazon.com/")
    assert f"{interface} interface endpoint(s) × 2" in cost["message"]


def test_the_telemetry_hint_names_the_missing_endpoints():
    db = SessionLocal()
    try:
        spec = {"network": {"profile_id": "np-0123456789ab"}}
        assert service.telemetry_hint(db, DEFAULT_WORKSPACE_ID, {}) is None  # PUBLIC
        row = _profile(db, status="unchecked")
        assert service.telemetry_hint(db, DEFAULT_WORKSPACE_ID, spec)["code"] == \
            "network.telemetry_unchecked"
        row.status = "blocked"
        row.check_results = [{"check": "endpoint.logs", "severity": "block"},
                             {"check": "endpoint.xray", "severity": "warn"}]
        db.commit()
        hint = service.telemetry_hint(db, DEFAULT_WORKSPACE_ID, spec)
        assert hint["missing"] == ["logs", "xray"] and "logs" in hint["message"]
        row.status = "ok"
        row.check_results = [{"check": "endpoint.logs", "severity": "ok"},
                             {"check": "endpoint.xray", "severity": "ok", "via": "nat"}]
        db.commit()
        assert service.telemetry_hint(db, DEFAULT_WORKSPACE_ID, spec) is None
        # another workspace's profile is not this agent's
        assert service.telemetry_hint(db, "other", spec) is None
    finally:
        db.close()


# ── §12.9: the teardown waits for the ENIs ────────────────────────────────────


class EniEc2(FakeEc2):
    """DescribeNetworkInterfaces answered from a queue of pages (the last one repeats)."""

    def __init__(self, answers: list[list[dict[str, Any]]]):
        super().__init__(_vpc())
        self.answers = answers
        self.filters: list[Any] = []

    def describe_network_interfaces(self, **kw):
        self._gate("DescribeNetworkInterfaces")
        self.filters.append(kw.get("Filters"))
        rows = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return {"NetworkInterfaces": rows}


def _eni(eid: str, description: str = "") -> dict[str, Any]:
    return {"NetworkInterfaceId": eid, "Description": description, "Status": "in-use"}


@pytest.fixture
def enis(monkeypatch):
    holder: dict[str, EniEc2] = {"fake": EniEc2([[]])}
    previous = pipeline.set_teardown_ec2(lambda _ws: holder["fake"])
    monkeypatch.setattr(pipeline, "_eni_sleep", lambda _s: None)
    yield holder
    pipeline.set_teardown_ec2(previous)


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        db.expunge(job)
        return job
    finally:
        db.close()


def test_a_public_agent_has_nothing_to_wait_for(enis):
    db = SessionLocal()
    try:
        agent = _agent(db, None)
        assert pipeline.start_eni_release_wait(db, agent, None, start=False) is None
    finally:
        db.close()


def test_the_wait_polls_until_the_enis_are_released(enis):
    db = SessionLocal()
    try:
        _profile(db)
        agent = _agent(db)
        enis["fake"] = EniEc2([[_eni("eni-1", "AgentCore runtime rt-abc123")],
                               [_eni("eni-1", "AgentCore runtime rt-abc123")], []])
        job_id = pipeline.start_eni_release_wait(db, agent, None, start=False)
    finally:
        db.close()
    job = _job(job_id)
    assert job.type == pipeline.ENI_JOB_TYPE and job.status == "running"
    assert job.payload["vpc"] == {"subnets": SUBNETS, "security_groups": [SG]}
    assert job.payload["shared"] is False
    pipeline.wait_for_eni_release(job_id, wait_seconds=60, poll_seconds=15)
    job = _job(job_id)
    assert job.status == "succeeded"
    assert "every ENI released" in job.log
    names = {f["Name"]: f["Values"] for f in enis["fake"].filters[0]}
    assert names == {"group-id": [SG], "subnet-id": SUBNETS}


def test_the_wait_hands_over_to_the_sweep_then_fails_at_the_deadline(enis, monkeypatch):
    db = SessionLocal()
    try:
        _profile(db)
        agent = _agent(db)
        enis["fake"] = EniEc2([[_eni("eni-9")]])
        job_id = pipeline.start_eni_release_wait(db, agent, None, start=False)
    finally:
        db.close()
    pipeline.wait_for_eni_release(job_id, wait_seconds=30, poll_seconds=15)
    job = _job(job_id)
    assert job.status == "running" and job.payload["remaining"] == ["eni-9"]
    assert "scheduled teardown keeps checking" in job.log

    db = SessionLocal()
    try:
        swept = pipeline.sweep_eni_releases(db)
        assert swept["eni_releases"] == [{"job_id": job_id, "status": "waiting"}]
        row = db.get(Job, job_id)
        row.payload = {**row.payload, "deadline": 0}
        db.commit()
        swept = pipeline.sweep_eni_releases(db)
        assert swept["eni_releases"] == [{"job_id": job_id, "status": "failed"}]
    finally:
        db.close()
    job = _job(job_id)
    assert "eni-9" in job.error and "EC2 console" in job.error


def test_a_shared_profile_waits_only_for_enis_naming_the_agent(enis):
    db = SessionLocal()
    try:
        _profile(db)
        _agent(db, name="neighbour", resource_id="rt-other")
        agent = _agent(db)
        enis["fake"] = EniEc2([[_eni("eni-n", "runtime rt-other")]])
        job_id = pipeline.start_eni_release_wait(db, agent, None, start=False)
        assert _job(job_id).payload["shared"] is True
    finally:
        db.close()
    # the neighbour's ENI is not this agent's: nothing attributable remains
    pipeline.wait_for_eni_release(job_id, wait_seconds=0)
    assert _job(job_id).status == "succeeded"


def test_a_profile_cannot_be_archived_while_its_enis_are_pending(enis):
    db = SessionLocal()
    try:
        _profile(db)
        agent = _agent(db)
        enis["fake"] = EniEc2([[_eni("eni-1")]])
        job_id = pipeline.start_eni_release_wait(db, agent, None, start=False)
        agent.status = "deleted"
        db.commit()
        with pytest.raises(AppError) as err:
            service.archive_profile(db, DEFAULT_WORKSPACE_ID, "np-0123456789ab")
        assert err.value.code == "network_profile.enis_pending"
        assert err.value.status_code == 409
        assert err.value.detail["agents"] == [agent.id]
        db.get(Job, job_id).status = "succeeded"
        db.commit()
        service.archive_profile(db, DEFAULT_WORKSPACE_ID, "np-0123456789ab")
        assert db.get(NetworkProfile, "np-0123456789ab").archived_at is not None
    finally:
        db.close()


def test_the_eni_wait_resumes_after_a_restart(enis, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(pipeline, "start_eni_release_async", lambda job_id: started.append(job_id))
    db = SessionLocal()
    try:
        _profile(db)
        job_id = pipeline.start_eni_release_wait(db, _agent(db), None, start=False)
    finally:
        db.close()
    assert job_id in pipeline.resume_pending_jobs()
    assert started == [job_id]


# ── promotion: network profiles map like any other environment resource ──────


@pytest.fixture
def target_ws():
    db = SessionLocal()
    try:
        db.add(Workspace(id=TARGET, name="Prod", account_id="444455556677", region=REGION,
                         tier="prod", bootstrap_status="ready", resources={}))
        db.flush()
        bind_test_project(db, TARGET)
        db.commit()
        _profile(db)  # the source's profile
        _profile(db, "np-prod00000001", workspace=TARGET, name="prod-vpc")
        yield db
    finally:
        db.close()


SPEC = {"name": "vpc-agent", "method": "zip_runtime", "system_prompt": "p",
        "network": {"profile_id": "np-0123456789ab"}}


def test_the_profile_is_a_reference_named_after_the_source_profile(target_ws):
    db = target_ws
    refs = rm.extract_references(SPEC)
    assert [(r.kind, r.path_text) for r in refs] == [("network_profile", "network.profile_id")]
    result = rm.resolve_spec(db, SPEC, source_workspace_id=DEFAULT_WORKSPACE_ID,
                             target_workspace_id=TARGET)
    [missing] = result.unmapped
    assert missing["logical"] == "network_profile:core-vpc"
    assert missing["code"] == "network_profile.unmapped"
    with pytest.raises(AppError) as err:
        rm.assert_network_mapped(db, SPEC, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                 target_workspace_id=TARGET, project_id=None)
    assert err.value.code == "network_profile.unmapped" and err.value.status_code == 409
    assert "network_profile:core-vpc" in err.value.message


def test_a_mapping_rewrites_the_profile_for_the_target(target_ws):
    db = target_ws
    rm.upsert_mapping(db, workspace_id=TARGET, kind="network_profile", name="core-vpc",
                      resource_id="np-prod00000001", note=None, updated_by="t",
                      scope=rm.SCOPE_WORKSPACE)
    db.commit()
    result = rm.resolve_spec(db, SPEC, source_workspace_id=DEFAULT_WORKSPACE_ID,
                             target_workspace_id=TARGET)
    assert result.complete
    assert result.spec["network"] == {"profile_id": "np-prod00000001"}
    assert rm.assert_network_mapped(db, SPEC, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                    target_workspace_id=TARGET,
                                    project_id=None) == "np-prod00000001"
    # the releasing project must be allowed to use the target profile
    with pytest.raises(AppError) as err:
        rm.assert_network_mapped(db, SPEC, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                 target_workspace_id=TARGET, project_id="p-other")
    assert err.value.code == "network_profile.unmapped"
    db.add(ProjectNetworkProfile(project_id="p-other", profile_id="np-prod00000001",
                                 workspace_id=TARGET, is_default=False))
    db.commit()
    assert rm.assert_network_mapped(db, SPEC, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                    target_workspace_id=TARGET, project_id="p-other")
    assert rm.assert_network_mapped(db, {"name": "x"}, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                    target_workspace_id=TARGET, project_id=None) is None


def test_a_mapping_must_name_a_live_profile_of_its_workspace(target_ws):
    db = target_ws
    for bad in ("np-0123456789ab", "np-missing00001"):  # the source's, or none at all
        with pytest.raises(NotFoundError):
            rm.upsert_mapping(db, workspace_id=TARGET, kind="network_profile", name="core-vpc",
                              resource_id=bad, note=None, updated_by="t",
                              scope=rm.SCOPE_WORKSPACE)
    with pytest.raises(AppError) as err:
        rm.validate_mapping("network_profile", "core-vpc", "Not An Id")
    assert err.value.code == "mapping.invalid_resource_id"


def test_admission_refuses_an_unmapped_network_profile(target_ws):
    db = target_ws
    bundle = ReleaseBundle(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a1",
                           agent_name="vpc-agent", method="zip_runtime", spec=SPEC,
                           artifact={}, evaluation={}, policy={}, digest="d")
    db.add(bundle)
    db.flush()
    promotion = Promotion(workspace_id=DEFAULT_WORKSPACE_ID, bundle_id=bundle.id,
                          target_workspace_id=TARGET, status="approved", requested_by="a",
                          change_note="c", rollback_note="r")
    db.add(promotion)
    db.commit()
    with pytest.raises(AppError) as err:
        promotion_exec.admit(db, promotion, action=promotion_exec.ACTION_ROLLBACK,
                             bundle_id=bundle.id, previous_id=None)
    assert err.value.code == "network_profile.unmapped"
    db.rollback()
    rm.upsert_mapping(db, workspace_id=TARGET, kind="network_profile", name="core-vpc",
                      resource_id="np-prod00000001", note=None, updated_by="t",
                      scope=rm.SCOPE_WORKSPACE)
    promotion = db.get(Promotion, promotion.id)
    if promotion.project_id:  # the releasing project may use the target profile
        db.add(ProjectNetworkProfile(project_id=promotion.project_id,
                                     profile_id="np-prod00000001", workspace_id=TARGET,
                                     is_default=False))
    db.commit()
    job = promotion_exec.admit(db, promotion, action=promotion_exec.ACTION_ROLLBACK,
                               bundle_id=bundle.id, previous_id=None)
    assert job.type == promotion_exec.JOB_TYPE
    db.rollback()
