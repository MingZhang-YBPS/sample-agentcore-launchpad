"""Network-profile preflight, probe and enforcement (design projects §12.4, §12.5, §12.7):
every blocking / warning branch of the preflight against an EC2 stand-in, the endpoint
requirement matrix, the routes (admin only, another workspace's profile reads as
missing), the background probe through an injected runner, and the condition-key
policy. No AWS call is made: EC2, the probe runtime and IAM are all fakes."""

import copy
import socket
from typing import Any

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.regions import agentcore_vpc_az_ids
from app.models.ledger import Workspace
from app.models.network import NetworkProfile
from app.services import network_enforcement as enforcement
from app.services import network_preflight as preflight
from app.services import network_probe
from app.services import network_profiles as service
from tests.conftest import client_error, ws_ctx
from tests.test_project_scope import (
    _login,
    _member_of,
    _project,
    _user,
    app,  # noqa: F401 — the fixture
)

REGION = "us-west-2"
VPC = "vpc-0123456789abcdef0"
SUBNETS = ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"]
SG = "sg-0ccccccccccccccc3"
EP_SG = "sg-0eeeeeeeeeeeeeee5"
RT = "rtb-0000000000000001"
NAT = "nat-0000000000000001"
BODY = {"name": "core-vpc", "vpc_id": VPC, "subnet_ids": SUBNETS, "security_group_ids": [SG]}


def _vpc(region: str = REGION) -> dict[str, Any]:
    """A VPC that passes everything: two supported AZs, 443 out, DNS on, every
    interface endpoint (private DNS, both AZs, 443 in from the profile SG), the S3
    gateway on the subnets' route table, and permissive NACLs."""
    prefix = {"us-west-2": "usw2", "us-east-1": "use1"}.get(region, "xx")
    endpoints = [
        {"VpcEndpointId": f"vpce-{i}", "ServiceName": f"com.amazonaws.{region}.{svc}",
         "VpcEndpointType": kind, "State": "available", "PrivateDnsEnabled": kind == "Interface",
         "SubnetIds": list(SUBNETS) if kind == "Interface" else [],
         "Groups": [{"GroupId": EP_SG}] if kind == "Interface" else [],
         "RouteTableIds": [RT] if kind == "Gateway" else []}
        for i, (svc, kind, _needs) in enumerate(preflight.ENDPOINT_MATRIX)
    ]
    return {
        "subnets": [
            {"SubnetId": SUBNETS[0], "VpcId": VPC, "AvailabilityZoneId": f"{prefix}-az1",
             "AvailableIpAddressCount": 250},
            {"SubnetId": SUBNETS[1], "VpcId": VPC, "AvailabilityZoneId": f"{prefix}-az2",
             "AvailableIpAddressCount": 250},
        ],
        "groups": [
            {"GroupId": SG, "VpcId": VPC, "IpPermissions": [],
             "IpPermissionsEgress": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                                      "IpRanges": [{"CidrIp": "10.0.0.0/16"}]}]},
            {"GroupId": EP_SG, "VpcId": VPC, "IpPermissionsEgress": [],
             "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                                "UserIdGroupPairs": [{"GroupId": SG}]}]},
        ],
        "vpcs": [{"VpcId": VPC, "CidrBlock": "10.0.0.0/16",
                  "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16"}]}],
        "attributes": {"enableDnsSupport": True, "enableDnsHostnames": True},
        "route_tables": [{"RouteTableId": RT, "Associations": [{"Main": True}],
                          "Routes": [{"DestinationCidrBlock": "10.0.0.0/16",
                                      "GatewayId": "local", "State": "active"}]}],
        "endpoints": endpoints,
        "nats": [],
        "acls": [{"NetworkAclId": "acl-1", "IsDefault": True, "Associations": [],
                  "Entries": [
                      {"RuleNumber": 100, "Egress": True, "Protocol": "-1",
                       "CidrBlock": "0.0.0.0/0", "RuleAction": "allow"},
                      {"RuleNumber": 100, "Egress": False, "Protocol": "-1",
                       "CidrBlock": "0.0.0.0/0", "RuleAction": "allow"},
                  ]}],
    }


class FakeEc2:
    """The Describe* calls preflight makes, answered from a `_vpc()`-shaped dict;
    records each call. `deny` makes every call fail like a missing permission."""

    def __init__(self, data: dict[str, Any], deny: bool = False):
        self.data = data
        self.deny = deny
        self.calls: list[str] = []

    def _gate(self, name: str) -> None:
        self.calls.append(name)
        if self.deny:
            raise client_error("UnauthorizedOperation", "not authorized", name)

    @staticmethod
    def _values(kwargs: dict[str, Any], key: str = "Filters") -> list[str]:
        return [v for f in kwargs.get(key) or [] for v in f["Values"]]

    def describe_subnets(self, **kw):
        self._gate("DescribeSubnets")
        ids = self._values(kw)
        return {"Subnets": [s for s in self.data["subnets"] if s["SubnetId"] in ids]}

    def describe_security_groups(self, **kw):
        self._gate("DescribeSecurityGroups")
        ids = self._values(kw)
        return {"SecurityGroups": [g for g in self.data["groups"] if g["GroupId"] in ids]}

    def describe_vpcs(self, **kw):
        self._gate("DescribeVpcs")
        ids = self._values(kw)
        return {"Vpcs": [v for v in self.data["vpcs"] if v["VpcId"] in ids]}

    def describe_vpc_attribute(self, VpcId, Attribute):  # noqa: N803 — boto3's names
        self._gate("DescribeVpcAttribute")
        key = Attribute[0].upper() + Attribute[1:]
        return {key: {"Value": self.data["attributes"][Attribute]}}

    def describe_route_tables(self, **kw):
        self._gate("DescribeRouteTables")
        return {"RouteTables": self.data["route_tables"]}

    def describe_vpc_endpoints(self, **kw):
        self._gate("DescribeVpcEndpoints")
        return {"VpcEndpoints": self.data["endpoints"]}

    def describe_nat_gateways(self, **kw):
        self._gate("DescribeNatGateways")
        assert "Filter" in kw and "Filters" not in kw  # this API's own spelling
        return {"NatGateways": self.data["nats"]}

    def describe_network_acls(self, **kw):
        self._gate("DescribeNetworkAcls")
        return {"NetworkAcls": self.data["acls"]}


def _check(data: dict[str, Any], *, vpc_id: str | None = VPC, internet: str = "none",
           reachable=None, features=None, region: str = REGION,
           subnets=None, groups=None) -> dict[str, dict[str, Any]]:
    subnets = list(SUBNETS if subnets is None else subnets)
    groups = list([SG] if groups is None else groups)
    facts = preflight.gather(FakeEc2(data), vpc_id=vpc_id, subnet_ids=subnets,
                             security_group_ids=groups)
    results = preflight.evaluate(facts, region=region, subnet_ids=subnets,
                                 security_group_ids=groups, vpc_id=vpc_id, internet=internet,
                                 reachable=reachable, features=features)
    return {r["check"]: r for r in results}


def _sev(results: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {k: r["severity"] for k, r in results.items()}


# ── the region table ──────────────────────────────────────────────────────────


def test_the_az_table_knows_the_documented_regions_only():
    assert agentcore_vpc_az_ids("us-east-1") == ("use1-az1", "use1-az2", "use1-az4")
    assert "usw1-az2" not in agentcore_vpc_az_ids("us-west-1")
    assert agentcore_vpc_az_ids("mars-north-1") is None


# ── preflight rules (§12.4) ───────────────────────────────────────────────────


def test_a_complete_vpc_passes_every_check():
    results = _check(_vpc())
    # the inbound-endpoint and cost entries are informational (§12.8, §12.9)
    assert set(_sev(results).values()) == {"ok", "info"}, _sev(results)
    assert {k for k, v in _sev(results).items() if v == "info"} == {"inbound.endpoint", "cost"}
    assert preflight.status_of(list(results.values())) == "ok"
    for svc in ("ecr.api", "ecr.dkr", "s3", "logs", "bedrock-runtime", "bedrock-agentcore"):
        assert results[f"endpoint.{svc}"]["severity"] == "ok"


def test_missing_and_foreign_subnets_block():
    data = _vpc()
    data["subnets"][1]["VpcId"] = "vpc-0fffffffffffffff9"
    results = _check(data, subnets=[*SUBNETS, "subnet-0ddddddddddddddd4"])
    assert results["subnets.exist"]["severity"] == "block"
    assert "subnet-0ddddddddddddddd4" in results["subnets.exist"]["message"]
    assert results["subnets.vpc"]["severity"] == "block"
    assert f"{SUBNETS[1]} is not in {VPC}" in results["subnets.vpc"]["message"]


def test_one_az_and_an_unsupported_az_block_and_name_the_alternatives():
    data = _vpc("us-east-1")
    data["subnets"][0]["AvailabilityZoneId"] = "use1-az3"
    data["subnets"][1]["AvailabilityZoneId"] = "use1-az3"
    results = _check(data, region="us-east-1")
    assert results["subnets.azs"]["severity"] == "block"
    assert "only 1 Availability Zone (use1-az3)" in results["subnets.azs"]["message"]
    supported = results["subnets.supported_az"]
    assert supported["severity"] == "block"
    assert "use1-az1, use1-az2, use1-az4" in supported["message"]


def test_an_unknown_region_warns_instead_of_blocking():
    results = _check(_vpc("xx-east-9"), region="xx-east-9")
    assert results["subnets.supported_az"]["severity"] == "warn"


def test_few_free_addresses_warn():
    data = _vpc()
    data["subnets"][0]["AvailableIpAddressCount"] = 12
    results = _check(data)
    assert results["subnets.free_ips"]["severity"] == "warn"
    assert "only 12 free addresses" in results["subnets.free_ips"]["message"]
    assert preflight.status_of(list(results.values())) == "warn"


def test_security_groups_must_exist_in_the_vpc_and_allow_443_out():
    data = _vpc()
    results = _check(data, groups=[SG, "sg-0999999999999999a"])
    assert results["security_groups.exist"]["severity"] == "block"
    data["groups"][0]["IpPermissionsEgress"] = [
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
         "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
    results = _check(data)
    assert results["security_groups.egress_443"]["severity"] == "block"
    # all traffic to anywhere covers 443
    data["groups"][0]["IpPermissionsEgress"] = [
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
    assert _check(data)["security_groups.egress_443"]["severity"] == "ok"


def test_dns_attributes_must_both_be_on():
    data = _vpc()
    data["attributes"]["enableDnsHostnames"] = False
    assert _check(data)["vpc.dns"]["severity"] == "block"


def test_a_missing_vpc_blocks():
    data = _vpc()
    data["vpcs"] = []
    results = _check(data)
    assert results["vpc.exists"]["severity"] == "block"


def test_baseline_endpoints_block_and_feature_endpoints_warn_on_a_profile():
    data = _vpc()
    data["endpoints"] = [e for e in data["endpoints"]
                         if not e["ServiceName"].endswith((".logs", ".ecr.dkr"))]
    results = _check(data)
    assert results["endpoint.logs"]["severity"] == "block"
    assert "no interface endpoint com.amazonaws.us-west-2.logs" in results["endpoint.logs"][
        "message"]
    # only containers need ECR: a warning before any agent names the profile …
    assert results["endpoint.ecr.dkr"]["severity"] == "warn"
    # … and a block for a container agent's deploy-time check
    deploy = _check(data, features={"always", "container"})
    assert deploy["endpoint.ecr.dkr"]["severity"] == "block"
    assert "endpoint.bedrock-agent-runtime" not in deploy  # not needed: not reported


def test_interface_endpoint_misconfigurations_are_named():
    data = _vpc()
    logs = next(e for e in data["endpoints"] if e["ServiceName"].endswith(".logs"))
    logs["PrivateDnsEnabled"] = False
    logs["SubnetIds"] = SUBNETS[:1]
    logs["Groups"] = [{"GroupId": "sg-0777777777777777b"}]
    data["groups"].append({"GroupId": "sg-0777777777777777b", "VpcId": VPC,
                           "IpPermissions": [], "IpPermissionsEgress": []})
    message = _check(data)["endpoint.logs"]["message"]
    assert "private DNS off" in message
    assert "no subnet in usw2-az2" in message
    assert "does not allow 443 in" in message


def test_the_s3_gateway_must_be_on_the_subnets_route_table():
    data = _vpc()
    s3 = next(e for e in data["endpoints"] if e["ServiceName"].endswith(".s3"))
    s3["RouteTableIds"] = ["rtb-other"]
    result = _check(data)["endpoint.s3"]
    assert result["severity"] == "block" and "not associated with the route table" in result[
        "message"]


def _with_nat(data: dict[str, Any], state: str = "available") -> dict[str, Any]:
    data["route_tables"][0]["Routes"].append(
        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": NAT, "State": "active"})
    data["nats"] = [{"NatGatewayId": NAT, "State": state}]
    return data


def test_internet_nat_needs_a_default_route_to_an_available_nat():
    results = _check(_vpc(), internet="nat")
    assert results["routes.nat"]["severity"] == "block"
    assert "has no default route to an available NAT" in results["routes.nat"]["message"]
    nat = _vpc()
    nat["route_tables"][0]["Routes"].append(
        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": NAT, "State": "active"})
    nat["nats"] = []  # the NAT is not available (DescribeNatGateways filters it out)
    assert _check(nat, internet="nat")["routes.nat"]["severity"] == "block"
    assert _check(_with_nat(_vpc()), internet="nat")["routes.nat"]["severity"] == "ok"


def test_nat_stands_in_for_missing_endpoints():
    data = _with_nat(_vpc())
    data["endpoints"] = []
    results = _check(data, internet="nat")
    assert results["endpoint.logs"]["severity"] == "ok"
    assert results["endpoint.logs"]["details"]["via"] == "nat"
    assert preflight.status_of(list(results.values())) == "ok"


def test_an_explicit_subnet_route_table_wins_over_the_main_one():
    data = _vpc()
    data["route_tables"].append({"RouteTableId": "rtb-own",
                                 "Associations": [{"SubnetId": SUBNETS[0]}], "Routes": []})
    result = _check(data)["endpoint.s3"]
    assert result["severity"] == "block" and SUBNETS[0] in result["message"]


def test_a_restrictive_network_acl_warns():
    data = _vpc()
    data["acls"][0]["Entries"] = [
        {"RuleNumber": 50, "Egress": True, "Protocol": "6", "CidrBlock": "0.0.0.0/0",
         "PortRange": {"From": 443, "To": 443}, "RuleAction": "deny"},
        {"RuleNumber": 100, "Egress": True, "Protocol": "-1", "CidrBlock": "0.0.0.0/0",
         "RuleAction": "allow"},
        {"RuleNumber": 100, "Egress": False, "Protocol": "-1", "CidrBlock": "0.0.0.0/0",
         "RuleAction": "allow"},
    ]
    assert _check(data)["network_acls"]["severity"] == "warn"


def test_a_legacy_profile_borrows_its_subnets_vpc():
    results = _check(_vpc(), vpc_id=None)
    assert results["vpc.recorded"]["severity"] == "warn"
    assert VPC in results["vpc.recorded"]["message"]
    assert results["vpc.dns"]["severity"] == "ok"


def test_routes_to_declared_cidrs_report_the_next_hop():
    data = _vpc()
    data["route_tables"][0]["Routes"].append(
        {"DestinationCidrBlock": "172.16.0.0/12", "TransitGatewayId": "tgw-0abc",
         "State": "active"})
    result = _check(data, reachable=[{"cidr": "172.16.4.0/24", "port": 5432},
                                     {"cidr": "192.168.0.0/24", "port": 443},
                                     {"host": "db.internal", "port": 5432}])["routes.reachable"]
    assert result["severity"] == "warn"
    targets = {t["target"]: t for t in result["details"]["targets"]}
    assert targets["172.16.4.0/24"]["next_hops"] == ["transit_gateway:tgw-0abc"]
    assert targets["192.168.0.0/24"]["route"] == "missing"
    assert targets["db.internal"]["route"] == "not_checked"


def test_the_endpoint_matrix_follows_the_spec():
    harness = preflight.features_of_spec({"method": "harness", "knowledge_bases": [{"id": "k"}]})
    assert {"container", "bedrock_models", "memory", "knowledge_bases"} <= harness
    assert "ecr.dkr" in preflight.required_endpoints(harness)
    assert "bedrock-agent-runtime" in preflight.required_endpoints(harness)
    zip_agent = preflight.features_of_spec(
        {"method": "zip_runtime", "memory": {"short_term": False}})
    needed = preflight.required_endpoints(zip_agent)
    assert "ecr.api" not in needed and {"s3", "logs", "bedrock-runtime"} <= set(needed)
    assert "bedrock-agentcore" not in needed
    assert "a2a" in preflight.features_of_spec({"method": "zip_runtime", "protocol": "a2a"})


# ── the routes ────────────────────────────────────────────────────────────────


@pytest.fixture
def ec2(monkeypatch):
    holder: dict[str, FakeEc2] = {}

    def factory(ctx):
        holder.setdefault("fake", FakeEc2(_vpc(ctx.region)))
        return holder["fake"]

    monkeypatch.setattr(service, "_ec2_client", factory)
    return holder


def test_the_check_stores_its_results_on_the_profile(client, ec2):
    pid = client.post("/api/network-profiles", json=BODY).json()["id"]
    db = SessionLocal()
    try:
        region = db.get(Workspace, DEFAULT_WORKSPACE_ID).region
    finally:
        db.close()
    ec2["fake"] = FakeEc2(_vpc(region))
    res = client.post(f"/api/network-profiles/{pid}/check")
    assert res.status_code == 200, res.text
    body = res.json()
    supported = agentcore_vpc_az_ids(region)
    assert body["checked_at"] is not None
    assert body["status"] == ("ok" if supported else "warn")
    assert {r["check"] for r in body["check_results"]} >= {"subnets.exist", "vpc.dns",
                                                           "endpoint.logs"}
    assert "DescribeNatGateways" in ec2["fake"].calls
    # a network edit makes it stale again
    moved = client.patch(f"/api/network-profiles/{pid}", json={"internet": "nat"}).json()
    assert moved["status"] == "unchecked" and moved["check_results"] is None
    # and a re-check under the new declaration blocks: no NAT route
    rechecked = client.post(f"/api/network-profiles/{pid}/check").json()
    assert rechecked["status"] == "blocked"
    db = SessionLocal()
    try:
        assert db.get(NetworkProfile, pid).status == "blocked"
    finally:
        db.close()


def test_a_blocked_profile_cannot_be_chosen_after_its_check(client, ec2):
    pid = client.post("/api/network-profiles", json=BODY).json()["id"]
    ec2["fake"] = FakeEc2({**_vpc(), "vpcs": []})
    assert client.post(f"/api/network-profiles/{pid}/check").json()["status"] == "blocked"
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as err:
            service.usable_profile(db, DEFAULT_WORKSPACE_ID, None, pid)
        assert err.value.code == "network_profile.blocked"
    finally:
        db.close()


def test_missing_ec2_permissions_answer_502_and_leave_the_profile_alone(client, ec2):
    pid = client.post("/api/network-profiles", json=BODY).json()["id"]
    ec2["fake"] = FakeEc2(_vpc(), deny=True)
    res = client.post(f"/api/network-profiles/{pid}/check")
    assert res.status_code == 502
    assert res.json()["code"] == "network_profile.check_failed"
    assert "infra/spoke" in res.json()["message"]
    assert client.get(f"/api/network-profiles/{pid}").json()["status"] == "unchecked"


def test_check_and_probe_of_another_workspaces_profile_read_as_missing(client, ec2):
    pid = client.post("/api/network-profiles", json=BODY).json()["id"]
    db = SessionLocal()
    try:
        db.get(NetworkProfile, pid).workspace_id = "elsewhere"
        db.commit()
    finally:
        db.close()
    for path in (f"/api/network-profiles/{pid}/check", f"/api/network-profiles/{pid}/probe",
                 "/api/network-profiles/np-000000000000/check"):
        res = client.post(path)
        assert res.status_code == 404 and res.json()["code"] == "network_profile.not_found"
    assert "fake" not in ec2  # nothing was read for it


def test_members_cannot_check_probe_or_change_settings(app, ec2):  # noqa: F811
    _project("alpha")
    db = SessionLocal()
    try:
        pid = service.create_profile(db, DEFAULT_WORKSPACE_ID, BODY, "operator")["id"]
        service.set_project_profiles(db, DEFAULT_WORKSPACE_ID, "alpha", [pid], None)
    finally:
        db.close()
    uid = _user("joan", grant=False)
    _member_of("alpha", uid)
    jo = _login(app, "joan")
    assert jo.post(f"/api/network-profiles/{pid}/check").status_code == 403
    assert jo.post(f"/api/network-profiles/{pid}/probe").status_code == 403
    assert jo.get("/api/network-profiles/settings").status_code == 403
    assert jo.put("/api/network-profiles/settings",
                  json={"network_enforcement": True}).status_code == 403
    assert jo.get("/api/network-profiles/enforcement").status_code == 403


# ── the probe (§12.7) ─────────────────────────────────────────────────────────


class FakeRunner:
    def __init__(self, outcomes=None, error: Exception | None = None):
        self.outcomes = outcomes or []
        self.error = error
        self.calls: list[tuple[dict, list]] = []

    def run(self, ctx, profile, targets):
        self.calls.append((copy.deepcopy(profile), copy.deepcopy(targets)))
        if self.error:
            raise self.error
        return self.outcomes


@pytest.fixture
def inline_probe(monkeypatch):
    """Run the probe's background work inline and give it a fake runner."""
    pending: list = []
    monkeypatch.setattr(service, "_spawn", lambda work, name: pending.append(work))

    def use(runner):
        previous = service.set_probe_runner(runner)
        monkeypatch.setattr(service, "_probe_runner", runner)
        assert previous is not None
        return pending

    return use


REACHABLE = [{"cidr": "10.1.0.0/16", "port": 5432}, {"host": "api.internal", "port": 443}]


def test_the_probe_runs_in_the_background_and_lands_on_the_profile(client, inline_probe, ec2):
    pid = client.post("/api/network-profiles", json={**BODY, "reachable": REACHABLE}).json()["id"]
    runner = FakeRunner([
        {"target": "10.1.0.0/16", "port": 5432, "dns": "skipped", "tcp": "ok"},
        {"target": "api.internal", "port": 443, "dns": "failed", "tcp": "skipped"},
    ])
    pending = inline_probe(runner)
    res = client.post(f"/api/network-profiles/{pid}/probe")
    assert res.status_code == 202, res.text
    running = next(r for r in res.json()["check_results"] if r["check"] == "probe")
    assert running["status"] == "running"
    again = client.post(f"/api/network-profiles/{pid}/probe")
    assert again.status_code == 409 and again.json()["code"] == "network_profile.probe_running"

    pending.pop()()
    profile, targets = runner.calls[0]
    assert profile["subnet_ids"] == SUBNETS and targets == REACHABLE
    done = client.get(f"/api/network-profiles/{pid}").json()
    probe = next(r for r in done["check_results"] if r["check"] == "probe")
    assert probe["status"] == "done" and probe["severity"] == "warn"
    assert "1 of 2" in probe["message"]
    # an unchecked profile stays unchecked: the probe is not the preflight
    assert done["status"] == "unchecked"
    # a later preflight keeps the probe's entry and counts it
    checked = client.post(f"/api/network-profiles/{pid}/check").json()
    assert any(r["check"] == "probe" for r in checked["check_results"])


def test_a_failed_probe_is_recorded_not_raised(client, inline_probe):
    pid = client.post("/api/network-profiles", json={**BODY, "reachable": REACHABLE}).json()["id"]
    pending = inline_probe(FakeRunner(error=RuntimeError("no execution role")))
    client.post(f"/api/network-profiles/{pid}/probe")
    pending.pop()()
    probe = next(r for r in client.get(f"/api/network-profiles/{pid}").json()["check_results"]
                 if r["check"] == "probe")
    assert probe["status"] == "failed" and "no execution role" in probe["message"]


def test_a_late_probe_answer_after_a_network_edit_is_dropped(client, inline_probe):
    pid = client.post("/api/network-profiles", json={**BODY, "reachable": REACHABLE}).json()["id"]
    pending = inline_probe(FakeRunner([{"target": "x", "port": 1, "tcp": "ok"}]))
    client.post(f"/api/network-profiles/{pid}/probe")
    client.patch(f"/api/network-profiles/{pid}", json={"security_group_ids": [EP_SG]})
    pending.pop()()
    assert client.get(f"/api/network-profiles/{pid}").json()["check_results"] is None


def test_a_probe_needs_targets_and_an_unblocked_profile(client, inline_probe):
    inline_probe(FakeRunner())
    pid = client.post("/api/network-profiles", json=BODY).json()["id"]
    res = client.post(f"/api/network-profiles/{pid}/probe")
    assert res.status_code == 400 and res.json()["code"] == "network_profile.nothing_to_probe"
    other = client.post("/api/network-profiles",
                        json={**BODY, "name": "b", "reachable": REACHABLE}).json()["id"]
    db = SessionLocal()
    try:
        db.get(NetworkProfile, other).status = "blocked"
        db.commit()
    finally:
        db.close()
    assert client.post(f"/api/network-profiles/{other}/probe").status_code == 409


def test_probe_target_resolves_connects_and_reports_each_step():
    class Sock:
        def close(self):
            pass

    ok = network_probe.probe_target(
        {"host": "db.internal", "port": 5432},
        resolver=lambda *a, **k: [(None, None, None, None, ("10.1.2.3", 5432))],
        connector=lambda addr, timeout: Sock())
    assert ok["dns"] == "ok" and ok["address"] == "10.1.2.3" and ok["tcp"] == "ok"
    assert "tls" not in ok

    def no_dns(*a, **k):
        raise socket.gaierror("Name or service not known")

    failed = network_probe.probe_target({"host": "nowhere", "port": 443}, resolver=no_dns)
    assert failed["dns"] == "failed" and failed["tcp"] == "skipped"

    def refused(addr, timeout):
        raise ConnectionRefusedError("refused")

    cidr = network_probe.probe_target({"cidr": "10.9.0.0/24", "port": 22}, connector=refused)
    assert cidr["address"] == "10.9.0.1" and cidr["dns"] == "skipped"
    assert cidr["tcp"] == "failed"


def test_the_probe_zip_carries_a_dependency_free_main():
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(network_probe.probe_zip())) as zf:
        source = zf.read("main.py").decode()
    compile(source, "main.py", "exec")
    assert "def probe_target" in source and "/invocations" in source and "/ping" in source
    assert "from app" not in source


def test_the_default_runner_creates_invokes_and_always_deletes():
    """The live runner against fakes: shapes only (UNVERIFIED against AWS)."""
    calls: list[str] = []

    class Control:
        def create_agent_runtime(self, **kw):
            calls.append("create")
            assert kw["networkConfiguration"]["networkMode"] == "VPC"
            assert kw["networkConfiguration"]["networkModeConfig"]["subnets"] == SUBNETS
            assert kw["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"] == ["main.py"]
            return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1"}

        def get_agent_runtime(self, agentRuntimeId):  # noqa: N803
            return {"status": "READY", "agentRuntimeArn": "arn:rt-1"}

        def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
            calls.append("delete")

    class Data:
        def invoke_agent_runtime(self, **kw):
            calls.append("invoke")
            raise RuntimeError("invoke failed")

    class S3:
        def put_object(self, **kw):
            calls.append("put")

        def delete_object(self, **kw):
            calls.append("delete_object")

    clients = {"bedrock-agentcore-control": Control(), "bedrock-agentcore": Data(), "s3": S3()}

    class Ctx:
        resources = {"artifacts_bucket": "launchpad-artifacts", "execution_role_arn": "arn:role"}

        def client(self, name):
            return clients[name]

    runner = network_probe.AgentCoreProbeRunner(sleeper=lambda s: None)
    with pytest.raises(RuntimeError, match="invoke failed"):
        runner.run(Ctx(), {"id": "np-1", "subnet_ids": SUBNETS, "security_group_ids": [SG]},
                   REACHABLE)
    assert calls == ["put", "create", "invoke", "delete", "delete_object"]


# ── settings and condition-key enforcement (§12.5) ────────────────────────────


def test_settings_default_to_public_allowed_without_enforcement(client):
    assert client.get("/api/network-profiles/settings").json() == {
        "network_enforcement": False, "network_public_allowed": True}
    res = client.put("/api/network-profiles/settings",
                     json={"network_enforcement": True, "network_public_allowed": False})
    assert res.status_code == 200
    assert res.json() == {"network_enforcement": True, "network_public_allowed": False}
    # a partial update leaves the other switch alone
    res = client.put("/api/network-profiles/settings", json={"network_public_allowed": True})
    assert res.json() == {"network_enforcement": True, "network_public_allowed": True}
    assert client.put("/api/network-profiles/settings", json={"other": 1}).status_code == 422


def test_public_agents_are_refused_when_the_workspace_requires_a_profile():
    db = SessionLocal()
    try:
        enforcement.assert_public_allowed(db, DEFAULT_WORKSPACE_ID, None)
        enforcement.set_network_settings(db, DEFAULT_WORKSPACE_ID,
                                         {"network_public_allowed": False})
        enforcement.assert_public_allowed(db, DEFAULT_WORKSPACE_ID, {"profile_id": "np-1"})
        with pytest.raises(AppError) as err:
            enforcement.assert_public_allowed(db, DEFAULT_WORKSPACE_ID, None)
        assert err.value.code == "network.public_not_allowed"
    finally:
        db.close()


def test_the_enforcement_policy_lists_the_approved_networks(client):
    empty = client.get("/api/network-profiles/enforcement").json()
    deny_all = empty["policy"]["Statement"][0]
    assert deny_all["Condition"] == {"Null": {"bedrock-agentcore:subnets": "false"}}

    client.post("/api/network-profiles", json=BODY)
    blocked = client.post("/api/network-profiles", json={
        **BODY, "name": "bad", "subnet_ids": ["subnet-0ddddddddddddddd4", SUBNETS[0]]}).json()
    db = SessionLocal()
    try:
        db.get(NetworkProfile, blocked["id"]).status = "blocked"
        db.commit()
    finally:
        db.close()
    body = client.get("/api/network-profiles/enforcement").json()
    assert body["approved_subnet_ids"] == sorted(SUBNETS)  # the blocked one's extra is out
    assert body["stack_parameters"]["ApprovedSecurityGroupIds"] == SG
    subnets, groups = body["policy"]["Statement"]
    assert subnets["Effect"] == "Deny"
    assert subnets["Condition"] == {
        "ForAnyValue:StringNotEquals": {"bedrock-agentcore:subnets": sorted(SUBNETS)}}
    assert groups["Condition"] == {
        "ForAnyValue:StringNotEquals": {"bedrock-agentcore:securityGroups": [SG]}}
    assert "bedrock-agentcore:CreateAgentRuntime" in subnets["Action"]
    assert not any("Harness" in a for a in subnets["Action"])  # no keys on harness actions
    assert body["not_enforceable"]


def test_run_check_with_features_reports_without_storing(ec2):
    db = SessionLocal()
    try:
        pid = service.create_profile(db, DEFAULT_WORKSPACE_ID, BODY, "operator")["id"]
        data = _vpc()
        data["endpoints"] = [e for e in data["endpoints"]
                             if not e["ServiceName"].endswith(".ecr.api")]
        out = service.run_check(db, ws_ctx(), pid, features={"always", "container"},
                                ec2=FakeEc2(data))
        assert out["status"] == "blocked"
        assert db.get(NetworkProfile, pid).status == "unchecked"
    finally:
        db.close()
