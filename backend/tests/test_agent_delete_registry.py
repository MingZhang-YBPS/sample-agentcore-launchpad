"""Deleting an agent retires the Registry record it owns.

Regression: before this, `delete_agent_row` removed the runtime and the execution role but
left the A2A record the register stage had created, so the catalog kept advertising an
endpoint that no longer existed. The roadmap e2e found 15 such orphans in one account.

The record delete itself is `registry_console.delete_agent_record` (only the agent's own
record, read back first — see `test_assistant_purge_dependencies`); these tests pin the
ordinary delete's use of it: no call without a record or a Registry, and the record goes
after the runtime and the role.
"""

from types import SimpleNamespace

import pytest

from app.models.ledger import Agent
from app.routers import agents as agents_router
from app.services import registry_console
from app.services.agentcore import registry as reg
from app.services.workspace import WorkspaceContext


def _ctx(registry_id: str | None = "REG123") -> WorkspaceContext:
    resources = {"registry_id": registry_id} if registry_id else {}
    return WorkspaceContext(account_id="111122223333", region="eu-central-1", resources=resources)


def _agent(record_id: str | None = "rec-1") -> Agent:
    return Agent(id="a1", name="gone-agent", method="harness", status="active",
                 registry_record_id=record_id, spec={})


class FakeRegistry:
    def __init__(self):
        self.reads: list[str] = []
        self.deleted: list[tuple[str, str]] = []

    def get_registry_record(self, registryId, recordId):  # noqa: N803 - boto3 shape
        self.reads.append(recordId)
        return {"name": "gone-agent", "recordType": reg.ga_record_type("A2A"),
                "status": "APPROVED"}

    def delete_registry_record(self, registryId, recordId):  # noqa: N803 - boto3 shape
        self.deleted.append((registryId, recordId))


@pytest.fixture
def fake(monkeypatch):
    registry = FakeRegistry()
    monkeypatch.setattr(registry_console, "registry_control_client", lambda ctx: registry)
    return registry


def test_the_agents_own_record_is_deleted(fake):
    assert agents_router.delete_agent_registry_record(_agent(), _ctx()) is True
    assert fake.deleted == [("REG123", "rec-1")]


def test_an_agent_without_a_record_makes_no_call(fake):
    assert agents_router.delete_agent_registry_record(_agent(None), _ctx()) is True
    assert fake.reads == [] and fake.deleted == []


def test_a_workspace_without_registry_makes_no_call(fake):
    """Accounts whose SCP denies Registry setup never registered anything."""
    assert agents_router.delete_agent_registry_record(_agent(), _ctx(registry_id=None)) is True
    assert fake.reads == [] and fake.deleted == []


def test_the_teardown_retires_the_record_after_the_resources(monkeypatch, fake):
    """Order matters: the record goes after the runtime and the role, never before."""
    calls: list[str] = []
    monkeypatch.setattr(
        agents_router.harness_method, "delete_agent_resources",
        lambda agent, ws: calls.append("resource"),
    )
    monkeypatch.setattr(
        agents_router.agent_iam, "delete_execution_role",
        lambda agent, settings, ws, log: calls.append("role"),
    )
    real = agents_router.delete_agent_registry_record

    def spy(agent, ws):
        calls.append("record")
        return real(agent, ws)

    monkeypatch.setattr(agents_router, "delete_agent_registry_record", spy)
    monkeypatch.setattr(agents_router, "get_settings", lambda: SimpleNamespace())
    assert agents_router._delete_agent_resources(_agent(), _ctx()) is True
    assert calls == ["resource", "role", "record"]
    assert fake.deleted == [("REG123", "rec-1")]
