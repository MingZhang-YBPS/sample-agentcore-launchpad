"""A gated agent (live / candidate endpoints) is never a canary subject.

A canary routes the agent's production traffic through its own gateway and
compares against DEFAULT, so on a gated agent it would bypass the release gate
(AGENTS.md: every production path goes through ``production_endpoint``).
"""

from types import SimpleNamespace

from app.optimization.service import canary_capability

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/lab-AbCdEf1234"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/lab-AbCdEf1234"


def _row(**overrides):
    base = dict(method="zip_runtime", status="active", arn=RUNTIME_ARN, spec={},
                inbound_auth_mode=None, endpoint_mode=None, system_key=None)
    return SimpleNamespace(**{**base, **overrides})


def test_a_gated_runtime_agent_is_not_canary_eligible():
    cap = canary_capability(_row(endpoint_mode="live"))
    assert (cap["eligible"], cap["reason_code"]) == (False, "gated")
    assert "release gate" in cap["reason"]


def test_a_gated_harness_agent_is_not_canary_eligible():
    cap = canary_capability(_row(method="harness", arn=HARNESS_ARN, endpoint_mode="live"))
    assert (cap["eligible"], cap["reason_code"]) == (False, "gated")


def test_a_single_endpoint_agent_stays_eligible():
    for mode in (None, "default"):
        assert canary_capability(_row(endpoint_mode=mode))["eligible"] is True
