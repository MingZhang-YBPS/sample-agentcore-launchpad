"""Region facts shared by the backend and (through the workspaces list) the console.

Everything here is a pure function or a constant: nothing calls AWS. The platform is
region-parametric by construction (a workspace is one ``(account, region)`` pair and
every client is built from it), so this module exists only for the places where a
*name* depends on the region: the inference-profile geography prefix, the IAM
partition, the suggestions the registration form offers, and the Availability Zones
AgentCore accepts VPC subnets in.
"""

import re

# Regions offered as suggestions in the workspace registration form. NOT an
# authority and never used to refuse input: the bootstrap job's `validate-access`
# stage probes the target region for real, and the form keeps free-text entry.
# KEEP IN STEP with AWS's published list of Amazon Bedrock AgentCore regions
# (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html);
# a stale entry only costs a wasted click, a missing one is reachable by typing it.
#
# Last checked 2026-10-01 by a read-only probe from this repo's dev account: the regions
# below answered ListAgentRuntimes, ListHarnesses and ListMemories. us-west-1 and
# ap-southeast-5 answered the runtime call but refused Harness and Memory, so they are
# left out until those services are offered there; ap-east-1, ap-northeast-3,
# ap-southeast-3 and ap-southeast-4 exposed no AgentCore endpoint at all.
AGENTCORE_SUGGESTED_REGIONS: tuple[str, ...] = (
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "ca-central-1",
    "sa-east-1",
    "eu-central-1",
    "eu-north-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
)

_PARTITIONS: tuple[tuple[str, str], ...] = (
    (r"^cn-", "aws-cn"),
    (r"^us-gov-", "aws-us-gov"),
    (r"^us-isob-", "aws-iso-b"),
    (r"^us-iso-", "aws-iso"),
)


def partition_for_region(region: str) -> str:
    """IAM partition of a region (``aws`` unless the name says otherwise)."""
    for pattern, partition in _PARTITIONS:
        if re.match(pattern, region or ""):
            return partition
    return "aws"


# Geographic cross-region inference-profile prefixes. ``global.`` works from any
# region and is never rewritten. Only the geographies whose prefix is certain are
# mapped; any other region (ca-, sa-, me-, af-, il-, ...) returns None and callers
# leave the id alone rather than guess.
_GEO_PREFIX_BY_REGION: tuple[tuple[str, str], ...] = (
    (r"^us-gov-", "us-gov"),
    (r"^us-", "us"),
    (r"^eu-", "eu"),
    (r"^ap-", "apac"),
)
_GEO_PREFIXES = ("us-gov", "us", "eu", "apac")
# Only families whose geographic profiles are issued for every supported region of a
# geography are re-prefixed. Others (openai.*, ...) are left exactly as configured:
# re-prefixing them would invent an id that may not exist.
_LOCALISED_FAMILIES = ("anthropic.", "amazon.")


def inference_profile_prefix(region: str) -> str | None:
    """Geographic inference-profile prefix for ``region`` (no trailing dot).

    ``us-*`` -> ``us``, ``eu-*`` -> ``eu``, ``ap-*`` -> ``apac``, ``us-gov-*`` ->
    ``us-gov``; ``None`` for any other region.
    """
    for pattern, prefix in _GEO_PREFIX_BY_REGION:
        if re.match(pattern, region or ""):
            return prefix
    return None


def localize_model_id(model_id: str, region: str) -> str:
    """Re-prefix a geographic Claude/Nova profile id so it matches ``region``.

    ``global.*`` ids, bare ids, ids of other families and regions without a known
    prefix come back unchanged.
    """
    prefix = inference_profile_prefix(region)
    if prefix is None:
        return model_id
    head, dot, rest = model_id.partition(".")
    if not dot or head not in _GEO_PREFIXES or head == prefix:
        return model_id
    if not rest.startswith(_LOCALISED_FAMILIES):
        return model_id
    return f"{prefix}.{rest}"


# Availability Zone ids (not names: names are shuffled per account) in which AgentCore
# places VPC-mode runtimes and built-in tools. A subnet elsewhere fails at resource
# creation, so network-profile preflight blocks on it (design projects §12.4).
# KEEP IN STEP with the "Supported Availability Zones" table of
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html —
# copied from it on 2026-10-15. A region missing here is "unknown", not "unsupported":
# preflight warns instead of blocking.
AGENTCORE_VPC_AZ_IDS: dict[str, tuple[str, ...]] = {
    "us-east-1": ("use1-az1", "use1-az2", "use1-az4"),
    "us-east-2": ("use2-az1", "use2-az2", "use2-az3"),
    "us-west-1": ("usw1-az1", "usw1-az3"),
    "us-west-2": ("usw2-az1", "usw2-az2", "usw2-az3"),
    "ap-southeast-5": ("apse5-az1", "apse5-az2", "apse5-az3"),
    "ap-south-1": ("aps1-az1", "aps1-az2", "aps1-az3"),
    "ap-south-2": ("aps2-az1", "aps2-az2", "aps2-az3"),
    "ap-northeast-2": ("apne2-az1", "apne2-az2", "apne2-az3"),
    "ap-southeast-1": ("apse1-az1", "apse1-az2", "apse1-az3"),
    "ap-southeast-2": ("apse2-az1", "apse2-az2", "apse2-az3"),
    "ap-southeast-7": ("apse7-az1", "apse7-az2", "apse7-az3"),
    "ap-northeast-1": ("apne1-az1", "apne1-az2", "apne1-az4"),
    "ca-central-1": ("cac1-az1", "cac1-az2", "cac1-az4"),
    "eu-central-1": ("euc1-az1", "euc1-az2", "euc1-az3"),
    "eu-west-1": ("euw1-az1", "euw1-az2", "euw1-az3"),
    "eu-west-2": ("euw2-az1", "euw2-az2", "euw2-az3"),
    "eu-south-1": ("eus1-az1", "eus1-az2", "eus1-az3"),
    "eu-west-3": ("euw3-az1", "euw3-az2", "euw3-az3"),
    "eu-south-2": ("eus2-az1", "eus2-az2", "eus2-az3"),
    "eu-north-1": ("eun1-az1", "eun1-az2", "eun1-az3"),
    "sa-east-1": ("sae1-az1", "sae1-az2", "sae1-az3"),
    "us-gov-west-1": ("usgw1-az1", "usgw1-az2", "usgw1-az3"),
}


def agentcore_vpc_az_ids(region: str) -> tuple[str, ...] | None:
    """The AZ ids AgentCore supports VPC mode in for ``region``; None when this
    table does not know the region (callers warn rather than refuse)."""
    return AGENTCORE_VPC_AZ_IDS.get(region or "")
