"""spec.network → the ``{subnets, security_groups}`` every deploy method hands to
``runtime._network_configuration`` (design projects §12.6).

One resolver for every method — zip / studio / A2A, container, BYOC (code zip and
container artifacts alike) and the managed Harness — so no deployer carries its own
idea of PUBLIC. ``None`` means PUBLIC; a ``{profile_id}`` (or a legacy inline
network from an older snapshot) resolves through the workspace's network profile
(`services/network_profiles.resolve_vpc`), whatever the profile's preflight status:
the gate on new choices sits before the job (`pipeline.network_gate`), and a
resumed job must land where it started.
"""

from typing import Any

from app.schemas.agent import AgentSpec

# the job payload key a re-publish that changes the network carries (the old
# network, possibly None) — see `pipeline.network_gate` / the harness deployer
NETWORK_FROM_KEY = "network_from"


def spec_vpc(spec: AgentSpec, workspace_id: str | None) -> dict[str, list[str]] | None:
    """The resolved VPC placement of ``spec`` (None = PUBLIC)."""
    if spec.network is None:
        return None
    from app.core.db import SessionLocal
    from app.services import network_profiles

    db = SessionLocal()
    try:
        return network_profiles.resolve_vpc(db, workspace_id or "", spec.network)
    finally:
        db.close()


def describe(vpc: dict[str, list[str]] | None) -> str:
    """One log-line summary of a placement."""
    if not vpc:
        return "PUBLIC"
    return (f"VPC · subnets {','.join(vpc['subnets'])} · "
            f"security groups {','.join(vpc['security_groups'])}")


def network_of(spec: dict[str, Any] | None) -> Any:
    """The stored spec's ``network`` (None = PUBLIC), compared across a re-publish."""
    return (spec or {}).get("network") or None
