"""An agent's network as it stands (design projects §12.11 `GET /api/agents/{id}/network`,
§12.12 "network" card): the mode, the resolved profile with its last preflight, and the
ENIs AgentCore holds in the profile's subnets.

ENIs are read through an injectable reader (`set_eni_reader`; the default lists
`ec2:DescribeNetworkInterfaces` filtered by the profile's security groups and
subnets — the spoke role's read-only grant for teardown checks, §12.4). The service
managing them (`AWSServiceRoleForBedrockAgentCoreNetwork`) does not stamp the
agent on them in any documented way, so the list is per profile: every agent placed
in the same subnets + security groups shares it. A read failure (an older spoke
template without `ec2:Describe*`) reports `unavailable` instead of failing the route.
"""

from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.models.ledger import Agent
from app.models.network import NetworkProfile
from app.services import network_profiles

EniReader = Callable[[Any, dict[str, list[str]]], list[dict[str, Any]]]


def _describe_enis(workspace: Any, vpc: dict[str, list[str]]) -> list[dict[str, Any]]:
    ec2 = workspace.client("ec2")
    kwargs: dict[str, Any] = {"Filters": [
        {"Name": "group-id", "Values": list(vpc["security_groups"])},
        {"Name": "subnet-id", "Values": list(vpc["subnets"])},
    ]}
    out: list[dict[str, Any]] = []
    while True:
        page = ec2.describe_network_interfaces(**kwargs)
        out.extend(page.get("NetworkInterfaces") or [])
        token = page.get("NextToken")
        if not token:
            return out
        kwargs["NextToken"] = token


_eni_reader: EniReader = _describe_enis


def set_eni_reader(reader: EniReader) -> EniReader:
    """Swap the ENI reader (tests); returns the previous one."""
    global _eni_reader
    previous, _eni_reader = _eni_reader, reader
    return previous


def _eni_out(eni: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": eni.get("NetworkInterfaceId"),
        "subnet_id": eni.get("SubnetId"),
        "availability_zone": eni.get("AvailabilityZone"),
        "status": eni.get("Status"),
        "private_ip": eni.get("PrivateIpAddress"),
        "description": eni.get("Description"),
    }


def _enis(workspace: Any, vpc: dict[str, list[str]]) -> dict[str, Any]:
    try:
        rows = _eni_reader(workspace, vpc)
    except (ClientError, BotoCoreError) as exc:
        code = (exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError)
                else type(exc).__name__)
        return {"status": "unavailable", "error": code, "interfaces": []}
    interfaces = [_eni_out(e) for e in rows]
    in_use = sum(1 for e in interfaces if e["status"] == "in-use")
    return {"status": "ok", "scope": "profile", "count": len(interfaces),
            "in_use": in_use, "interfaces": interfaces}


def _profile_out(row: NetworkProfile | None, profile_id: str) -> dict[str, Any]:
    if row is None:
        return {"id": profile_id, "missing": True}
    results = row.check_results or []
    return {
        "id": row.id,
        "name": row.name,
        "vpc_id": row.vpc_id,
        "subnet_ids": list(row.subnet_ids or []),
        "security_group_ids": list(row.security_group_ids or []),
        "internet": row.internet,
        "status": row.status,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "archived": row.archived_at is not None,
        "blocking": sum(1 for r in results if r.get("severity") == "block"),
        "warnings": sum(1 for r in results if r.get("severity") == "warn"),
    }


def agent_network(db: Session, workspace: Any, agent: Agent) -> dict[str, Any]:
    """The route body: `mode` PUBLIC | VPC, the profile (None for PUBLIC and for a
    pre-migration inline network), the resolved placement and the ENI read."""
    network = (agent.spec or {}).get("network") or None
    if network is None:
        return {"agent_id": agent.id, "mode": "PUBLIC", "profile": None, "vpc": None,
                "enis": None}
    profile_id = network_profiles.network_profile_id(network)
    profile = None
    if profile_id is not None:
        row = db.get(NetworkProfile, profile_id)
        if row is not None and row.workspace_id != agent.workspace_id:
            row = None
        profile = _profile_out(row, profile_id)
        if row is None:
            return {"agent_id": agent.id, "mode": "VPC", "profile": profile, "vpc": None,
                    "enis": None}
    vpc = network_profiles.resolve_vpc(db, agent.workspace_id or "", network)
    return {"agent_id": agent.id, "mode": "VPC", "profile": profile, "vpc": vpc,
            "enis": _enis(workspace, vpc) if vpc else None}
