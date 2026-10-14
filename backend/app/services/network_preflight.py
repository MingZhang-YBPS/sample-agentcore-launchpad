"""Network-profile preflight (design projects §12.4, §12.7): read the profile's VPC
through the workspace's own EC2 API and say, check by check, whether AgentCore can
place a runtime there and reach what it needs.

Two halves, kept apart so the rules are testable without AWS:

* ``gather`` makes the read-only `ec2:Describe*` calls (the permissions the spoke
  role template grants for exactly this) and returns plain facts;
* ``evaluate`` turns facts into results — ``{check, severity, message, details}``
  with severity ``ok`` / ``warn`` / ``block`` — and ``status_of`` folds them into the
  profile status (``blocked`` > ``warn`` > ``ok``).

What an agent needs to reach depends on what it uses (the endpoint requirement
matrix below). A profile is checked before any agent names it, so a profile-level
check blocks only on what every agent needs (``BASELINE_FEATURES``) and warns about
the rest; the deploy path re-runs it with the features of one agent's spec
(``features_of_spec``), where every needed endpoint blocks.

An endpoint counts as reachable through an interface / gateway endpoint in the VPC,
or through NAT when every subnet's default route goes to an available NAT gateway.
Routes to the declared private targets (`reachable`, §12.7) are checked for presence
and next-hop type only; the far side (peering acceptance, TGW propagation, remote SGs,
DNS) is the customer's network and reported as "not verified by the platform".

Two entries are informational (severity ``info``, never part of the status, §12.8 /
§12.9): ``inbound.endpoint`` — whether private callers can reach AgentCore through a
PrivateLink endpoint in this VPC, and what its endpoint policy admits — and ``cost`` —
how many interface endpoints × Availability Zones and NAT gateways the profile leans on
(the platform links the regional price lists; it does not price them itself).
"""

import ipaddress
import json
from collections.abc import Callable, Iterable
from typing import Any

from app.core.regions import agentcore_vpc_az_ids

MIN_FREE_IPS = 32
HTTPS = 443
EPHEMERAL = (1024, 65535)

# (service suffix, endpoint type, features needing it). A feature is a word
# `features_of_spec` derives from a spec; "always" is every agent.
ENDPOINT_MATRIX: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("ecr.api", "Interface", frozenset({"container"})),
    ("ecr.dkr", "Interface", frozenset({"container"})),
    # zip artifacts and ECR image layers both come from S3
    ("s3", "Gateway", frozenset({"always"})),
    ("logs", "Interface", frozenset({"always"})),
    ("bedrock-runtime", "Interface", frozenset({"bedrock_models"})),
    ("bedrock-agentcore", "Interface", frozenset({"memory", "identity", "gateway", "a2a"})),
    ("bedrock-agent-runtime", "Interface", frozenset({"knowledge_bases"})),
    ("xray", "Interface", frozenset({"xray"})),
    ("secretsmanager", "Interface", frozenset({"connections"})),
    ("sts", "Interface", frozenset({"connections"})),
)
# what a profile is held to before any agent names it: every agent writes logs,
# loads code / layers from S3 and (all platform templates) calls Bedrock models
BASELINE_FEATURES = frozenset({"always", "bedrock_models"})


def required_endpoints(features: Iterable[str]) -> list[str]:
    """The endpoint services (``ecr.api``, ``s3``, …) the given features need."""
    wanted = set(features) | {"always"}
    return [svc for svc, _kind, needs in ENDPOINT_MATRIX if needs & wanted]


def features_of_spec(spec: dict[str, Any]) -> set[str]:
    """The matrix features a stored agent spec uses (best effort, from the spec alone)."""
    features = {"always"}
    method = spec.get("method")
    byoc = spec.get("byoc") or {}
    if method in ("container", "harness") or (
        method == "byoc" and str(byoc.get("artifact_kind", "")).startswith("container")
    ):
        features.add("container")
    if (spec.get("model_source") or "bedrock") == "bedrock" and method != "byoc":
        features.add("bedrock_models")
    memory = spec.get("memory") or {}
    if memory.get("short_term", True) or memory.get("long_term") or memory.get("memory_id"):
        features.add("memory")
    if spec.get("tools") or spec.get("toolkits"):
        features.add("gateway")
    if spec.get("inbound_auth") or spec.get("tools"):
        features.add("identity")
    if spec.get("protocol") == "a2a":
        features.add("a2a")
    if spec.get("knowledge_bases"):
        features.add("knowledge_bases")
    if spec.get("inbound_auth"):
        # not an endpoint need: tells the inbound-endpoint entry that OAuth callers
        # reach this agent (an endpoint policy cannot restrict them, §12.2)
        features.add("inbound_oauth")
    return features


# ── facts ─────────────────────────────────────────────────────────────────────


def _pages(call: Callable[..., dict[str, Any]], key: str, **kwargs: Any) -> list[dict]:
    out: list[dict] = []
    while True:
        page = call(**kwargs)
        out.extend(page.get(key) or [])
        token = page.get("NextToken")
        if not token:
            return out
        kwargs["NextToken"] = token


def gather(ec2: Any, *, vpc_id: str | None, subnet_ids: list[str],
           security_group_ids: list[str]) -> dict[str, Any]:
    """Every fact `evaluate` reads, from read-only EC2 calls. Lookups by id use
    filters, so a missing id comes back as absent rather than as an error."""
    subnets = _pages(ec2.describe_subnets, "Subnets",
                     Filters=[{"Name": "subnet-id", "Values": list(subnet_ids)}])
    if vpc_id is None:  # a legacy profile: its spec never named the VPC
        vpcs_seen = sorted({s["VpcId"] for s in subnets if s.get("VpcId")})
        vpc_id = vpcs_seen[0] if len(vpcs_seen) == 1 else None
    groups = _pages(ec2.describe_security_groups, "SecurityGroups",
                    Filters=[{"Name": "group-id", "Values": list(security_group_ids)}])
    facts: dict[str, Any] = {
        "vpc_id": vpc_id, "subnets": subnets, "security_groups": groups,
        "vpc": None, "dns_support": None, "dns_hostnames": None,
        "route_tables": [], "endpoints": [], "nat_gateways": [], "network_acls": [],
        "endpoint_subnets": [], "endpoint_groups": [],
    }
    if vpc_id is None:
        return facts
    by_vpc = [{"Name": "vpc-id", "Values": [vpc_id]}]
    vpcs = _pages(ec2.describe_vpcs, "Vpcs", Filters=by_vpc)
    facts["vpc"] = vpcs[0] if vpcs else None
    if facts["vpc"] is None:
        return facts
    for attribute, key, fact in (("enableDnsSupport", "EnableDnsSupport", "dns_support"),
                                 ("enableDnsHostnames", "EnableDnsHostnames", "dns_hostnames")):
        reply = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute=attribute)
        facts[fact] = bool((reply.get(key) or {}).get("Value"))
    facts["route_tables"] = _pages(ec2.describe_route_tables, "RouteTables", Filters=by_vpc)
    facts["endpoints"] = [
        e for e in _pages(ec2.describe_vpc_endpoints, "VpcEndpoints", Filters=by_vpc)
        if str(e.get("State", "")).lower() == "available"
    ]
    # DescribeNatGateways names its filter parameter `Filter`, not `Filters`
    facts["nat_gateways"] = [
        n for n in _pages(ec2.describe_nat_gateways, "NatGateways", Filter=by_vpc)
        if n.get("State") == "available"
    ]
    facts["network_acls"] = _pages(ec2.describe_network_acls, "NetworkAcls", Filters=by_vpc)
    # the interface endpoints' own subnets (their AZs) and security groups (their
    # inbound 443), which the profile's lists do not name
    ep_subnets = sorted({s for e in facts["endpoints"] for s in e.get("SubnetIds") or []}
                        - {s["SubnetId"] for s in subnets})
    if ep_subnets:
        facts["endpoint_subnets"] = _pages(
            ec2.describe_subnets, "Subnets",
            Filters=[{"Name": "subnet-id", "Values": ep_subnets}])
    known = {g["GroupId"] for g in groups}
    ep_groups = sorted({g["GroupId"] for e in facts["endpoints"] for g in e.get("Groups") or []}
                       - known)
    if ep_groups:
        facts["endpoint_groups"] = _pages(
            ec2.describe_security_groups, "SecurityGroups",
            Filters=[{"Name": "group-id", "Values": ep_groups}])
    return facts


# ── rules ─────────────────────────────────────────────────────────────────────


def _result(check: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"check": check, "severity": severity, "message": message}
    if details:
        out["details"] = details
    return out


def _covers_port(rule: dict[str, Any], port: int) -> bool:
    proto = str(rule.get("IpProtocol", ""))
    if proto == "-1":
        return True
    if proto not in ("tcp", "6"):
        return False
    low, high = rule.get("FromPort"), rule.get("ToPort")
    return low is None or high is None or (low <= port <= high)


def _cidr_covers(cidr: str, inner: str) -> bool:
    try:
        return ipaddress.ip_network(inner, strict=False).subnet_of(
            ipaddress.ip_network(cidr, strict=False))
    except (TypeError, ValueError):
        return False


def _egress_443(groups: list[dict[str, Any]], vpc_cidrs: list[str]) -> bool:
    """Some profile SG lets 443 out to the internet / NAT, the VPC (interface
    endpoints), a prefix list (gateway endpoints) or an endpoint's SG."""
    for group in groups:
        for rule in group.get("IpPermissionsEgress") or []:
            if not _covers_port(rule, HTTPS):
                continue
            if rule.get("PrefixListIds") or rule.get("UserIdGroupPairs"):
                return True
            if any(r.get("CidrIpv6") == "::/0" for r in rule.get("Ipv6Ranges") or []):
                return True
            for item in rule.get("IpRanges") or []:
                cidr = item.get("CidrIp", "")
                if cidr == "0.0.0.0/0" or any(_cidr_covers(cidr, v) for v in vpc_cidrs):
                    return True
    return False


def _ingress_443_from(groups: list[dict[str, Any]], sources: set[str],
                      vpc_cidrs: list[str]) -> bool:
    for group in groups:
        for rule in group.get("IpPermissions") or []:
            if not _covers_port(rule, HTTPS):
                continue
            if any(p.get("GroupId") in sources for p in rule.get("UserIdGroupPairs") or []):
                return True
            for item in rule.get("IpRanges") or []:
                cidr = item.get("CidrIp", "")
                if any(_cidr_covers(cidr, v) for v in vpc_cidrs):
                    return True
    return False


def _route_table_of(subnet_id: str, tables: list[dict[str, Any]]) -> dict[str, Any] | None:
    main = None
    for table in tables:
        for assoc in table.get("Associations") or []:
            if assoc.get("SubnetId") == subnet_id:
                return table
            if assoc.get("Main"):
                main = table
    return main


def _acl_of(subnet_id: str, acls: list[dict[str, Any]]) -> dict[str, Any] | None:
    default = None
    for acl in acls:
        if any(a.get("SubnetId") == subnet_id for a in acl.get("Associations") or []):
            return acl
        if acl.get("IsDefault"):
            default = acl
    return default


def _acl_allows(acl: dict[str, Any], *, egress: bool, ports: tuple[int, int]) -> bool:
    """First matching 0.0.0.0/0 rule (in rule-number order) for TCP over ``ports``
    decides; rules naming narrower CIDRs are not judged (a warning check)."""
    rules = sorted((e for e in acl.get("Entries") or [] if bool(e.get("Egress")) == egress),
                   key=lambda e: e.get("RuleNumber", 32767))
    for entry in rules:
        if entry.get("CidrBlock") != "0.0.0.0/0":
            continue
        proto = str(entry.get("Protocol", ""))
        if proto not in ("-1", "6"):
            continue
        span = entry.get("PortRange") or {}
        low, high = span.get("From", 0), span.get("To", 65535)
        if proto != "-1" and not (low <= ports[0] and ports[1] <= high):
            continue
        return entry.get("RuleAction") == "allow"
    return False


def _default_route(table: dict[str, Any] | None) -> dict[str, Any] | None:
    for route in (table or {}).get("Routes") or []:
        if route.get("DestinationCidrBlock") == "0.0.0.0/0" and route.get("State") != "blackhole":
            return route
    return None


_HOP_KEYS = (("VpcPeeringConnectionId", "peering"), ("TransitGatewayId", "transit_gateway"),
             ("GatewayId", "gateway"), ("NatGatewayId", "nat"),
             ("NetworkInterfaceId", "network_interface"), ("LocalGatewayId", "local_gateway"),
             ("CoreNetworkArn", "core_network"))


def _hop(route: dict[str, Any]) -> tuple[str, str]:
    for key, kind in _HOP_KEYS:
        if route.get(key):
            value = str(route[key])
            if kind == "gateway":
                kind = ("virtual_private_gateway" if value.startswith("vgw-")
                        else "local" if value == "local" else "internet_gateway"
                        if value.startswith("igw-") else "gateway")
            return kind, value
    return "unknown", ""


def evaluate(
    facts: dict[str, Any], *, region: str, subnet_ids: list[str],
    security_group_ids: list[str], vpc_id: str | None, internet: str,
    reachable: list[dict[str, Any]] | None = None, features: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-check results for one profile. ``features`` None = a profile-level check
    (block on the baseline, warn on the rest); a set = one agent's deploy check."""
    out: list[dict[str, Any]] = []
    found = {s["SubnetId"]: s for s in facts.get("subnets") or []}
    missing = [s for s in subnet_ids if s not in found]
    out.append(_result("subnets.exist", "block" if missing else "ok",
                       f"subnet(s) not found in this account and region: {', '.join(missing)}"
                       if missing else "every subnet exists", missing=missing))
    vpc = facts.get("vpc_id")
    if vpc_id is None:
        vpcs_seen = sorted({s.get("VpcId") for s in found.values()})
        if vpc:
            out.append(_result("vpc.recorded", "warn",
                               f"the profile records no VPC; its subnets are in {vpc} — "
                               "save the VPC id on the profile", vpc_id=vpc))
        else:
            out.append(_result("vpc.recorded", "block",
                               "the profile records no VPC and its subnets are not in exactly "
                               "one VPC", vpcs=vpcs_seen))
    foreign = [s for s, row in found.items() if vpc and row.get("VpcId") != vpc]
    out.append(_result("subnets.vpc", "block" if foreign else "ok",
                       "; ".join(f"{s} is not in {vpc} (it is in {found[s].get('VpcId')})"
                                 for s in foreign) if foreign
                       else "every subnet is in the profile's VPC", subnets=foreign))

    az_of = {s: row.get("AvailabilityZoneId") for s, row in found.items()}
    azs = sorted({a for a in az_of.values() if a})
    out.append(_result("subnets.azs", "block" if len(azs) < 2 else "ok",
                       f"only {len(azs)} Availability Zone ({', '.join(azs) or 'none'}) — add a "
                       "subnet in another supported Availability Zone" if len(azs) < 2
                       else f"subnets span {len(azs)} Availability Zones", azs=azs))
    supported = agentcore_vpc_az_ids(region)
    if supported is None:
        out.append(_result("subnets.supported_az", "warn",
                           f"the platform does not know AgentCore's VPC Availability Zones in "
                           f"{region}; check the AgentCore VPC documentation", azs=azs))
    else:
        bad = {s: a for s, a in az_of.items() if a and a not in supported}
        out.append(_result(
            "subnets.supported_az", "block" if bad else "ok",
            "; ".join(f"{s} is in {a}, where AgentCore does not support VPC mode"
                      for s, a in bad.items()) + f" — supported in {region}: "
            f"{', '.join(supported)}" if bad
            else "every subnet is in a supported Availability Zone",
            unsupported=bad, supported=list(supported)))
    low = {s: row.get("AvailableIpAddressCount", 0) for s, row in found.items()
           if row.get("AvailableIpAddressCount", 0) < MIN_FREE_IPS}
    out.append(_result("subnets.free_ips", "warn" if low else "ok",
                       "; ".join(f"{s} has only {n} free addresses" for s, n in low.items())
                       + " — every session's ENI takes an address" if low
                       else "every subnet has address room", low=low))

    groups = {g["GroupId"]: g for g in facts.get("security_groups") or []}
    missing_sg = [g for g in security_group_ids if g not in groups]
    foreign_sg = [g for g, row in groups.items() if vpc and row.get("VpcId") != vpc]
    bad_sg = missing_sg + foreign_sg
    out.append(_result("security_groups.exist", "block" if bad_sg else "ok",
                       (f"not found: {', '.join(missing_sg)}. " if missing_sg else "")
                       + (f"not in {vpc}: {', '.join(foreign_sg)}" if foreign_sg else "")
                       if bad_sg else "every security group exists in the profile's VPC",
                       missing=missing_sg, foreign=foreign_sg))

    vpc_row = facts.get("vpc")
    if vpc and vpc_row is None:
        out.append(_result("vpc.exists", "block", f"{vpc} not found in this account and region"))
        return out
    if vpc_row is None:
        return out
    vpc_cidrs = [c.get("CidrBlock") for c in vpc_row.get("CidrBlockAssociationSet") or []
                 if c.get("CidrBlock")] or [vpc_row.get("CidrBlock", "")]
    egress_ok = _egress_443(list(groups.values()), vpc_cidrs)
    out.append(_result("security_groups.egress_443", "ok" if egress_ok else "block",
                       "outbound 443 is allowed" if egress_ok else
                       f"{', '.join(security_group_ids)} allow no outbound 443 to 0.0.0.0/0, the "
                       "VPC or an endpoint prefix list"))
    dns_ok = bool(facts.get("dns_support")) and bool(facts.get("dns_hostnames"))
    out.append(_result("vpc.dns", "ok" if dns_ok else "block",
                       "DNS resolution and DNS hostnames are on" if dns_ok else
                       f"{vpc} needs both DNS resolution and DNS hostnames on — interface "
                       "endpoints' private DNS depends on them",
                       dns_support=facts.get("dns_support"),
                       dns_hostnames=facts.get("dns_hostnames")))

    tables = facts.get("route_tables") or []
    table_of = {s: _route_table_of(s, tables) for s in found}
    nats = {n["NatGatewayId"] for n in facts.get("nat_gateways") or []}
    no_nat = [s for s, t in table_of.items()
              if (_default_route(t) or {}).get("NatGatewayId") not in nats]
    via_nat = bool(found) and not no_nat
    if internet == "nat":
        out.append(_result("routes.nat", "block" if no_nat else "ok",
                           "; ".join(f"{s}'s route table has no default route to an available "
                                     "NAT gateway" for s in no_nat) if no_nat
                           else "every subnet routes 0.0.0.0/0 to an available NAT gateway",
                           subnets=no_nat))
    out.extend(_endpoint_results(facts, region=region, found=found, table_of=table_of,
                                 groups=set(groups), vpc_cidrs=vpc_cidrs, via_nat=via_nat,
                                 features=features))
    acl_bad = [s for s in found
               if (acl := _acl_of(s, facts.get("network_acls") or [])) is not None
               and not (_acl_allows(acl, egress=True, ports=(HTTPS, HTTPS))
                        and _acl_allows(acl, egress=False, ports=EPHEMERAL))]
    out.append(_result("network_acls", "warn" if acl_bad else "ok",
                       f"the network ACL of {', '.join(acl_bad)} may block outbound 443 or "
                       "the inbound ephemeral ports (1024-65535) replies use" if acl_bad
                       else "network ACLs allow 443 out and replies in", subnets=acl_bad))
    if reachable:
        out.append(_reachable_result(reachable, table_of))
    out.append(_inbound_result(facts, region=region, features=features))
    out.append(_cost_result(facts, found=found, table_of=table_of, nats=nats))
    return out


# ── informational entries (§12.8, §12.9) ──────────────────────────────────────

# the data plane (InvokeAgentRuntime …) and the control plane
INBOUND_SERVICES = ("bedrock-agentcore", "bedrock-agentcore-control")
PRICING_LINKS = {
    "privatelink": "https://aws.amazon.com/privatelink/pricing/",
    "nat_gateway": "https://aws.amazon.com/vpc/pricing/",
}


def _policy_principals(document: Any) -> tuple[list[str], bool]:
    """The principals an endpoint policy's Allow statements name, and whether any of
    them is everyone (`*` — also how an OAuth caller appears to the policy)."""
    if not document:
        return [], True  # no policy = the default full-access policy
    try:
        policy = json.loads(document) if isinstance(document, str) else dict(document)
    except (TypeError, ValueError):
        return [], False
    statements = policy.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    names: list[str] = []
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        principal = statement.get("Principal", "*")
        if principal == "*":
            names.append("*")
            continue
        if isinstance(principal, dict):
            for value in principal.values():
                names.extend([value] if isinstance(value, str) else list(value or []))
    ordered = sorted(set(names))
    return ordered, "*" in ordered


def _inbound_result(facts: dict[str, Any], *, region: str,
                    features: Iterable[str] | None) -> dict[str, Any]:
    endpoints = []
    for svc in INBOUND_SERVICES:
        name = f"com.amazonaws.{region}.{svc}"
        for ep in facts.get("endpoints") or []:
            if ep.get("ServiceName") != name or \
                    str(ep.get("VpcEndpointType", "")).lower() != "interface":
                continue
            principals, anyone = _policy_principals(ep.get("PolicyDocument"))
            endpoints.append({"service": svc, "endpoint_id": ep.get("VpcEndpointId"),
                              "private_dns": bool(ep.get("PrivateDnsEnabled")),
                              "subnets": list(ep.get("SubnetIds") or []),
                              "principals": principals, "any_principal": anyone})
    data_plane = [e for e in endpoints if e["service"] == "bedrock-agentcore"]
    oauth = features is not None and "inbound_oauth" in set(features)
    note = ("callers authenticating with OAuth appear as Principal \"*\" to an endpoint "
            "policy, so the agent's inbound JWT authorizer is what restricts them")
    if not data_plane:
        message = (f"no PrivateLink endpoint com.amazonaws.{region}.bedrock-agentcore in "
                   "this VPC — private callers invoke agents over the public endpoint")
    elif any(e["any_principal"] for e in data_plane):
        message = ("private callers can invoke agents through "
                   f"{data_plane[0]['endpoint_id']}; its policy admits any principal")
    else:
        message = (f"private callers can invoke agents through {data_plane[0]['endpoint_id']}; "
                   f"its policy admits {', '.join(data_plane[0]['principals']) or 'nobody'}")
    if oauth:
        message += f" — {note}"
    return _result("inbound.endpoint", "info", message, endpoints=endpoints,
                   private_inbound=bool(data_plane), oauth_note=note)


def _cost_result(facts: dict[str, Any], *, found: dict[str, dict], table_of: dict[str, Any],
                 nats: set[str]) -> dict[str, Any]:
    endpoints = facts.get("endpoints") or []
    interface = sorted({str(e.get("ServiceName", "")) for e in endpoints
                        if str(e.get("VpcEndpointType", "")).lower() == "interface"})
    gateway = sorted({str(e.get("ServiceName", "")) for e in endpoints
                      if str(e.get("VpcEndpointType", "")).lower() == "gateway"})
    azs = sorted({s.get("AvailabilityZoneId") for s in found.values()
                  if s.get("AvailabilityZoneId")})
    used_nats = sorted({nat for t in table_of.values()
                        if (nat := (_default_route(t) or {}).get("NatGatewayId")) in nats})
    message = (f"this profile depends on {len(interface)} interface endpoint(s) × "
               f"{len(azs)} Availability Zone(s) and {len(used_nats)} NAT gateway(s) — "
               "both are billed per hour plus data processed; see the regional price lists")
    return _result("cost", "info", message, interface_endpoints=interface,
                   gateway_endpoints=gateway, availability_zones=len(azs),
                   endpoint_az_hours=len(interface) * len(azs), nat_gateways=used_nats,
                   pricing=PRICING_LINKS)


def _endpoint_results(
    facts: dict[str, Any], *, region: str, found: dict[str, dict], table_of: dict[str, Any],
    groups: set[str], vpc_cidrs: list[str], via_nat: bool, features: Iterable[str] | None,
) -> list[dict[str, Any]]:
    wanted = set(BASELINE_FEATURES if features is None else features) | {"always"}
    endpoints = facts.get("endpoints") or []
    ep_subnet_az = {s["SubnetId"]: s.get("AvailabilityZoneId")
                    for s in [*found.values(), *(facts.get("endpoint_subnets") or [])]}
    all_groups = {g["GroupId"]: g for g in
                  [*(facts.get("security_groups") or []), *(facts.get("endpoint_groups") or [])]}
    profile_azs = {s.get("AvailabilityZoneId") for s in found.values()}
    out: list[dict[str, Any]] = []
    for svc, kind, needs in ENDPOINT_MATRIX:
        needed = bool(needs & wanted)
        if features is not None and not needed:
            continue
        name = f"com.amazonaws.{region}.{svc}"
        matches = [e for e in endpoints if e.get("ServiceName") == name
                   and str(e.get("VpcEndpointType", "")).lower() == kind.lower()]
        problems: list[str] = []
        if not matches:
            problems.append(f"no {kind.lower()} endpoint {name}")
        elif kind == "Gateway":
            routed = {t for e in matches for t in e.get("RouteTableIds") or []}
            unrouted = [s for s, t in table_of.items()
                        if t is None or t.get("RouteTableId") not in routed]
            if unrouted:
                problems.append(f"{name} is not associated with the route table of "
                                f"{', '.join(unrouted)}")
        else:
            ep = matches[0]
            if not ep.get("PrivateDnsEnabled"):
                problems.append(f"{name} has private DNS off")
            covered = {ep_subnet_az.get(s) for s in ep.get("SubnetIds") or []}
            gaps = sorted(a for a in profile_azs - covered if a)
            if gaps:
                problems.append(f"{name} has no subnet in {', '.join(gaps)}")
            ep_groups = [all_groups[g["GroupId"]] for g in ep.get("Groups") or []
                         if g.get("GroupId") in all_groups]
            if not _ingress_443_from(ep_groups, groups, vpc_cidrs):
                problems.append(f"{name}'s security group does not allow 443 in from the "
                                "profile's security groups")
        uses = ", ".join(sorted(n for n in needs if n != "always")) or "every agent"
        check = f"endpoint.{svc}"
        if not problems:
            out.append(_result(check, "ok", f"{name} is ready", needed_by=uses))
        elif via_nat:
            out.append(_result(check, "ok", f"reached through NAT ({problems[0]}); an endpoint "
                               "avoids NAT data charges", needed_by=uses, via="nat"))
        else:
            severity = "block" if needed else "warn"
            out.append(_result(check, severity, "; ".join(problems) + f" — needed by {uses}",
                               needed_by=uses))
    return out


def _reachable_result(reachable: list[dict[str, Any]],
                      table_of: dict[str, Any]) -> dict[str, Any]:
    """Routes to the declared private CIDRs: present, and through which next hop."""
    targets = []
    for item in reachable:
        cidr = item.get("cidr")
        if not cidr:  # a host name: only the probe can resolve it
            targets.append({"target": item.get("host"), "port": item.get("port"),
                            "route": "not_checked"})
            continue
        missing, hops = [], set()
        for subnet, table in table_of.items():
            best = None
            for route in (table or {}).get("Routes") or []:
                dest = route.get("DestinationCidrBlock")
                if dest and route.get("State") != "blackhole" and _cidr_covers(dest, cidr):
                    prefix = ipaddress.ip_network(dest, strict=False).prefixlen
                    if best is None or prefix > best[0]:
                        best = (prefix, route)
            if best is None:
                missing.append(subnet)
            else:
                hops.add(_hop(best[1]))
        targets.append({"target": cidr, "port": item.get("port"),
                        "route": "missing" if missing else "present",
                        "missing_in": missing,
                        "next_hops": sorted(f"{k}:{v}" for k, v in hops)})
    gaps = [t for t in targets if t["route"] == "missing"]
    return _result("routes.reachable", "warn" if gaps else "ok",
                   "no route to " + ", ".join(t["target"] for t in gaps) if gaps
                   else "a route exists to every declared CIDR (the far side — peering "
                   "acceptance, transit gateway propagation, remote security groups, DNS — is "
                   "not verified by the platform)", targets=targets)


def status_of(results: list[dict[str, Any]]) -> str:
    severities = {r.get("severity") for r in results}
    if "block" in severities:
        return "blocked"
    if "warn" in severities:
        return "warn"
    return "ok"
