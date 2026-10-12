"""Region facts shared by the backend and (through the workspaces list) the console.

Everything here is a pure function or a constant: nothing calls AWS. The platform is
region-parametric by construction (a workspace is one ``(account, region)`` pair and
every client is built from it), so this module exists only for the three places where
a *name* depends on the region: the inference-profile geography prefix, the IAM
partition, and the suggestions the registration form offers.
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
