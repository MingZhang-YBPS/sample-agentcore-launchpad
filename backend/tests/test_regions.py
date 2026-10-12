"""Region-parametric behaviour: profile prefixes, partitions, generated-agent code."""

import re
from pathlib import Path

import pytest

from app.core.regions import (
    AGENTCORE_SUGGESTED_REGIONS,
    inference_profile_prefix,
    localize_model_id,
    partition_for_region,
)
from app.schemas.agent import INFERENCE_PROFILE_PREFIXES, AgentSpec
from app.services.agent_iam import RoleContext, model_resources
from app.services.workspace import WorkspaceContext
from app.templates.strands_agent import render_main_py

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


@pytest.mark.parametrize(
    ("region", "prefix"),
    [
        ("us-west-2", "us"),
        ("us-east-1", "us"),
        ("us-east-2", "us"),
        ("eu-central-1", "eu"),
        ("eu-west-1", "eu"),
        ("ap-southeast-2", "apac"),
        ("ap-northeast-1", "apac"),
        ("us-gov-west-1", "us-gov"),
        ("sa-east-1", None),
        ("", None),
    ],
)
def test_inference_profile_prefix(region, prefix):
    assert inference_profile_prefix(region) == prefix


@pytest.mark.parametrize(
    ("model_id", "region", "expected"),
    [
        ("us.anthropic.claude-sonnet-5", "eu-central-1", "eu.anthropic.claude-sonnet-5"),
        ("us.amazon.nova-pro-v1:0", "ap-southeast-2", "apac.amazon.nova-pro-v1:0"),
        ("eu.anthropic.claude-sonnet-5", "us-west-2", "us.anthropic.claude-sonnet-5"),
        ("us.anthropic.claude-sonnet-5", "us-east-1", "us.anthropic.claude-sonnet-5"),
        # global and bare ids are region-independent
        ("global.anthropic.claude-sonnet-5", "eu-west-1", "global.anthropic.claude-sonnet-5"),
        ("amazon.nova-pro-v1:0", "eu-west-1", "amazon.nova-pro-v1:0"),
        # families that are not known to be issued per geography are never re-prefixed
        ("us.openai.gpt-5.6-sol", "eu-west-1", "us.openai.gpt-5.6-sol"),
        # unknown geography: leave alone rather than guess
        ("us.anthropic.claude-sonnet-5", "sa-east-1", "us.anthropic.claude-sonnet-5"),
    ],
)
def test_localize_model_id(model_id, region, expected):
    assert localize_model_id(model_id, region) == expected


@pytest.mark.parametrize(
    ("region", "partition"),
    [
        ("us-west-2", "aws"),
        ("eu-central-1", "aws"),
        ("cn-north-1", "aws-cn"),
        ("us-gov-west-1", "aws-us-gov"),
    ],
)
def test_partition_for_region(region, partition):
    assert partition_for_region(region) == partition
    assert WorkspaceContext(account_id="123456789012", region=region).partition == partition


def test_role_policy_arns_follow_the_partition():
    ctx = RoleContext(
        account_id="123456789012",
        region="us-gov-west-1",
        artifacts_bucket="b",
        ecr_repo_arn="",
    )
    arns = model_resources("us-gov.anthropic.claude-sonnet-5", ctx)
    assert arns and all(a.startswith("arn:aws-us-gov:") for a in arns)


def test_us_gov_profile_prefix_is_recognised_by_the_byoc_validator():
    assert "us-gov." in INFERENCE_PROFILE_PREFIXES


def test_suggested_regions_are_well_formed_and_unique():
    assert len(set(AGENTCORE_SUGGESTED_REGIONS)) == len(AGENTCORE_SUGGESTED_REGIONS)
    assert all(re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", r) for r in AGENTCORE_SUGGESTED_REGIONS)


def _template_sources() -> list[Path]:
    return [p for p in TEMPLATES.rglob("*") if p.suffix == ".tmpl"]


def test_no_template_defaults_to_a_literal_region():
    """The runtime sets AWS_REGION; a template must read it, not fall back to a region."""
    offenders = []
    for path in _template_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(us|eu|ap|sa|ca|me|af|il)-[a-z]+-\d", code):
                # The one deliberate pin: Mantle models live in their own region and
                # the override is LAUNCHPAD_MANTLE_REGION.
                if "LAUNCHPAD_MANTLE_REGION" in code:
                    continue
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []


def test_rendered_strands_agent_has_no_region_leak():
    spec = AgentSpec(name="region-test", method="zip_runtime", system_prompt="hi")
    code = render_main_py(spec)
    assert "us-west-2" not in code
    assert "\"us.\\" not in code and "'us." not in code and '"us.' not in code
    assert 'os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")' in code
