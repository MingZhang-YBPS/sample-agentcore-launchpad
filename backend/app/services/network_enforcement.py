"""Network settings of a workspace and the condition-key enforcement option
(design projects §12.5).

Two switches in `Workspace.settings`:

* ``network_enforcement`` — the workspace means to hold AgentCore to its approved
  subnets / security groups at the IAM layer. The platform computes the approved
  sets (every live, non-blocked profile) and the Deny statements below; the
  operator applies them by updating the spoke stack (`ApprovedSubnetIds` /
  `ApprovedSecurityGroupIds` parameters of `infra/spoke/launchpad-workspace-role.yaml`)
  or as an organization SCP. The platform deliberately does NOT write the policy
  onto its own role: a role allowed to put inline policies on itself can grant
  itself anything, which is a larger hole than the one this closes.
* ``network_public_allowed`` (default true) — false means every agent must name a
  profile. Enforced by the platform only (`assert_public_allowed`): IAM has no
  condition key for `networkMode`.

Condition keys, as published in the Service Authorization Reference for
`bedrock-agentcore` (checked 2026-10-15): ``bedrock-agentcore:subnets`` and
``bedrock-agentcore:securityGroups`` (ArrayOfString) on CreateAgentRuntime,
UpdateAgentRuntime, CreateCodeInterpreter and CreateBrowser. The Harness actions
carry neither key, so a harness's network is enforced by the platform alone, and no
key expresses `networkMode`. The keys are lower camel case — the design's
`Subnets` / `SecurityGroups` spelling would never match.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import Workspace
from app.models.network import NetworkProfile

ENFORCEMENT_KEY = "network_enforcement"
PUBLIC_ALLOWED_KEY = "network_public_allowed"
SUBNETS_KEY = "bedrock-agentcore:subnets"
SECURITY_GROUPS_KEY = "bedrock-agentcore:securityGroups"
ENFORCED_ACTIONS = (
    "bedrock-agentcore:CreateAgentRuntime",
    "bedrock-agentcore:CreateBrowser",
    "bedrock-agentcore:CreateCodeInterpreter",
    "bedrock-agentcore:UpdateAgentRuntime",
)
# (stable code, English text): the console localizes by code, the text stays for API callers
NOT_ENFORCEABLE = (
    ("harness_no_keys",
     "bedrock-agentcore:CreateHarness and UpdateHarness carry no network condition keys"),
    ("public_not_refusable",
     "no condition key expresses networkMode, so PUBLIC cannot be refused at the IAM layer"),
)


def network_settings(db: Session, workspace_id: str) -> dict[str, bool]:
    row = db.get(Workspace, workspace_id)
    settings = (row.settings if row is not None else None) or {}
    return {
        ENFORCEMENT_KEY: bool(settings.get(ENFORCEMENT_KEY, False)),
        PUBLIC_ALLOWED_KEY: bool(settings.get(PUBLIC_ALLOWED_KEY, True)),
    }


def set_network_settings(db: Session, workspace_id: str,
                         changes: dict[str, bool | None]) -> dict[str, bool]:
    row = db.get(Workspace, workspace_id)
    if row is None:
        raise AppError("workspace.not_found", f"workspace '{workspace_id}' not found",
                       status_code=404)
    clean = {k: bool(v) for k, v in changes.items()
             if k in (ENFORCEMENT_KEY, PUBLIC_ALLOWED_KEY) and v is not None}
    if clean:
        row.settings = {**(row.settings or {}), **clean}
        db.commit()
    return network_settings(db, workspace_id)


def assert_public_allowed(db: Session, workspace_id: str, network: Any) -> None:
    """For the deploy path (V12-deploy): a PUBLIC agent in a workspace that forbids it."""
    if network:
        return
    if not network_settings(db, workspace_id)[PUBLIC_ALLOWED_KEY]:
        raise AppError("network.public_not_allowed",
                       "this workspace requires every agent to run in a network profile — "
                       "choose one", status_code=400)


def approved_sets(db: Session, workspace_id: str) -> tuple[list[str], list[str]]:
    """Subnets and SGs of the live profiles not blocked by their last preflight."""
    subnets: set[str] = set()
    groups: set[str] = set()
    rows = db.scalars(select(NetworkProfile).where(
        NetworkProfile.workspace_id == workspace_id, NetworkProfile.archived_at.is_(None),
        NetworkProfile.status != "blocked"))
    for row in rows:
        subnets.update(row.subnet_ids or [])
        groups.update(row.security_group_ids or [])
    return sorted(subnets), sorted(groups)


def policy_document(subnets: list[str], groups: list[str]) -> dict[str, Any]:
    """Deny the network-placing actions when a requested subnet / SG is outside the
    approved sets. `ForAnyValue:StringNotEquals` is false when the key is absent, so a
    PUBLIC request passes (see `network_public_allowed`); an empty approved set
    denies every VPC placement (`Null: false` = the key is present)."""

    def statement(sid: str, key: str, values: list[str]) -> dict[str, Any]:
        condition = ({"ForAnyValue:StringNotEquals": {key: values}} if values
                     else {"Null": {key: "false"}})
        return {"Sid": sid, "Effect": "Deny", "Action": list(ENFORCED_ACTIONS),
                "Resource": "*", "Condition": condition}

    return {"Version": "2012-10-17", "Statement": [
        statement("LaunchpadApprovedSubnetsOnly", SUBNETS_KEY, subnets),
        statement("LaunchpadApprovedSecurityGroupsOnly", SECURITY_GROUPS_KEY, groups),
    ]}


def enforcement(db: Session, workspace_id: str) -> dict[str, Any]:
    """What the "network → enforcement" panel shows: the switches, the approved sets
    as the spoke stack parameters, and the equivalent policy (SCP / inline)."""
    subnets, groups = approved_sets(db, workspace_id)
    return {
        "settings": network_settings(db, workspace_id),
        "approved_subnet_ids": subnets,
        "approved_security_group_ids": groups,
        "stack_parameters": {"ApprovedSubnetIds": ",".join(subnets),
                             "ApprovedSecurityGroupIds": ",".join(groups)},
        "policy": policy_document(subnets, groups),
        "actions": list(ENFORCED_ACTIONS),
        "not_enforceable": [text for _, text in NOT_ENFORCEABLE],
        "not_enforceable_codes": [code for code, _ in NOT_ENFORCEABLE],
    }
