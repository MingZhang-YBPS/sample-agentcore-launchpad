"""Each AgentCore endpoint logs to its own group and service name (found on real AWS:
`…-candidate` holds `harness_<name>.candidate`). A gate run must read the candidate's,
and a gated agent's dashboards must read `live`, not an idle DEFAULT."""

from types import SimpleNamespace

import pytest

from app.evaluation import service


class _Logs:
    def __init__(self, names):
        self.names = names

    def describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [{"logGroupName": n, "creationTime": i}
                              for i, n in enumerate(self.names)
                              if n.startswith(logGroupNamePrefix)]}


GROUPS = [f"/aws/bedrock-agentcore/runtimes/harness_shop-XXXX-{e}"
          for e in ("DEFAULT", "live", "candidate")]


def _harness(mode="default"):
    return SimpleNamespace(method="harness", resource_id="shop-AbCd123", endpoint_mode=mode)


@pytest.mark.parametrize(("mode", "qualifier", "endpoint"), [
    ("default", None, "DEFAULT"),       # the historical behaviour is unchanged
    ("live", None, "live"),             # a gated agent's production traffic
    ("live", "candidate", "candidate"),  # a gate run reads what it invoked
])
def test_harness_telemetry_follows_the_endpoint(mode, qualifier, endpoint):
    name, group = service.resolve_telemetry(_harness(mode), SimpleNamespace(),
                                            _Logs(GROUPS), qualifier=qualifier)
    assert name == f"harness_shop.{endpoint}"
    assert group.endswith(f"-{endpoint}")


def test_runtime_telemetry_follows_the_endpoint(monkeypatch):
    monkeypatch.setattr(service, "control_client", lambda ws: object())
    monkeypatch.setattr(service.rt, "get_runtime", lambda c, rid: {"agentRuntimeName": "shop"})
    agent = SimpleNamespace(method="zip_runtime", resource_id="shop-rt1", endpoint_mode="live")
    assert service.resolve_telemetry(agent, SimpleNamespace(), qualifier="candidate") == (
        "shop.candidate", "/aws/bedrock-agentcore/runtimes/shop-rt1-candidate")
    assert service.resolve_telemetry(agent, SimpleNamespace())[0] == "shop.live"


def test_a_never_invoked_endpoint_says_so():
    from app.core.errors import AppError

    with pytest.raises(AppError) as exc:
        service.resolve_telemetry(_harness("live"), SimpleNamespace(),
                                  _Logs(GROUPS[:1]))  # only DEFAULT exists yet
    assert exc.value.code == "eval.harness_no_telemetry"
