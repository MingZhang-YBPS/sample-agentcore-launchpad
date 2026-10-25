"""AgentCore client factories — the only place these clients are named.

Preview API drift is contained here and in the sibling wrapper modules;
everything else passes clients explicitly so tests can inject stubs. Construction
goes through ``services.aws_clients`` via the workspace context, so a client
belongs to a workspace's account/region rather than to a process-wide region.

Every factory takes the workspace it targets, and takes it as a required
argument: a request's ``ws.context``, or ``context_for_workspace(row.
workspace_id)`` in a background worker. There is deliberately no default — a
factory that could fall back to the hub would silently send a second
workspace's work to the wrong account.
"""

from typing import Any

from botocore.config import Config

from app.core.config import get_settings
from app.services.workspace import WorkspaceContext


def control_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("bedrock-agentcore-control")


def data_client(ctx: WorkspaceContext, read_timeout: float | None = None) -> Any:
    # Called on every invoke/chat turn — cache_token keeps it on the factory's
    # cached path despite the per-settings Config. `read_timeout` overrides the
    # setting for callers with a known-shorter or -longer call (one cached client
    # per distinct value).
    timeout = read_timeout or get_settings().agentcore_read_timeout_s
    return ctx.client(
        "bedrock-agentcore",
        cache_token=f"read_timeout={timeout}",
        config=Config(read_timeout=timeout),
    )


def memory_data_client(ctx: WorkspaceContext) -> Any:
    """Data-plane client for AgentCore Memory (events, records): a short read timeout,
    not the runtime's 1000 s — a memory call never legitimately takes that long, and a
    stalled one must not hold the request that made it."""
    timeout = get_settings().agentcore_memory_read_timeout_s
    return ctx.client(
        "bedrock-agentcore",
        cache_token=f"read_timeout={timeout};connect=5;retries=2",
        config=Config(read_timeout=timeout, connect_timeout=5,
                      retries={"max_attempts": 2, "mode": "standard"}),
    )


def api_data_client(ctx: WorkspaceContext) -> Any:
    """Data-plane client for request/response AgentCore APIs that never stream an
    agent turn: A/B tests, configuration-bundle recommendations, batch-evaluation
    reads. A 60 s read timeout with standard retries, not the runtime's 1000 s:
    a stalled CreateABTest otherwise held a canary setup for exactly 1000 s before
    botocore's retry went through (live 2026-10-18: 17 min instead of ~1)."""
    timeout = get_settings().agentcore_api_read_timeout_s
    return ctx.client(
        "bedrock-agentcore",
        cache_token=f"read_timeout={timeout};connect=5;retries=3",
        config=Config(read_timeout=timeout, connect_timeout=5,
                      retries={"max_attempts": 3, "mode": "standard"}),
    )


# Methods whose runtime runs a platform template that emits a ``heartbeat`` event at
# least every 15 s (strands_agent / claude_sdk_agent main.py templates). BYOC and
# imported runtimes run the customer's own code, which owes no heartbeat.
HEARTBEAT_METHODS = frozenset({"zip_runtime", "container"})


def heartbeat_data_client(ctx: WorkspaceContext) -> Any:
    """Data-plane client for a heartbeat runtime's stream: a shorter read timeout,
    because a silence longer than a few heartbeats is a dead stream."""
    timeout = get_settings().agentcore_heartbeat_read_timeout_s
    # No automatic retry: botocore re-sends a timed-out InvokeAgentRuntime under the
    # same runtime session, so the agent answered the question twice in one session
    # (live 2026-10-13: an extra trace with no expected answer, two evaluator errors
    # and a drift alert). The callers retry in a fresh session instead.
    return ctx.client(
        "bedrock-agentcore",
        cache_token=f"read_timeout={timeout};retries=0",
        config=Config(read_timeout=timeout, retries={"max_attempts": 1, "mode": "standard"}),
    )


def stream_data_client(
    ctx: WorkspaceContext, method: str | None, protocol: str | None = "http",
) -> Any:
    """The data-plane client to stream one runtime invocation with."""
    if method in HEARTBEAT_METHODS and (protocol or "http") == "http":
        return heartbeat_data_client(ctx)
    return data_client(ctx)


def registry_control_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("agent-registry-control")


def registry_data_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("agent-registry")


def agent_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("bedrock-agent")


def agent_runtime_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("bedrock-agent-runtime")


def iam_client(ctx: WorkspaceContext) -> Any:
    return ctx.client("iam")
