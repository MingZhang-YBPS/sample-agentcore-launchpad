"""A/B-test, recommendation and batch-evaluation calls use a short read timeout.

The runtime's data client waits up to 1000 s for an agent turn; on a stalled
CreateABTest that held a canary setup for exactly 1000 s (live 2026-10-18).
"""

from app.optimization import canary_service
from app.optimization import service as exp_service
from app.services.agentcore import client as ac_client


def test_optimization_modules_use_the_short_api_client():
    assert canary_service.data_client is ac_client.api_data_client
    assert exp_service.data_client is ac_client.api_data_client


def test_api_data_client_has_a_short_read_timeout_and_retries():
    captured = {}

    class Ctx:
        def client(self, name, cache_token=None, config=None):
            captured.update(name=name, read=config.read_timeout,
                            connect=config.connect_timeout,
                            attempts=config.retries["max_attempts"], token=cache_token)
            return object()

    ac_client.api_data_client(Ctx())
    assert captured["name"] == "bedrock-agentcore"
    assert captured["read"] <= 60 and captured["connect"] <= 5
    assert captured["attempts"] == 3
    # its own cache slot: never the runtime client's 1000 s config
    assert "1000" not in captured["token"]
