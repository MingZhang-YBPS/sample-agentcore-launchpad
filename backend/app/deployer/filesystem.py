"""spec.filesystem → the AgentCore ``filesystemConfigurations`` union list.

One mapping for every method that mounts storage: the container runtime, the
Strands zip runtime, BYOC and the managed Harness (inside its
``environment.agentCoreRuntimeEnvironment``). BYO mounts (S3 Files / EFS) are
accepted on every method of ``schemas/agent.BYO_MOUNT_METHODS``, inside the agent's
network profile (storage-mounts §3) — ``AgentSpec`` refuses them on any other method
and without a VPC network — next to the managed session storage entry.

Update semantics differ per API (probed live 2026-10-04, us-west-2):
UpdateAgentRuntime *clears* an omitted ``filesystemConfigurations``, while
UpdateHarness *keeps* an omitted ``environment`` and replaces the list wholesale
when one is sent (``[]`` detaches everything).
"""

from app.schemas.agent import AgentSpec


def filesystem_configurations(spec: AgentSpec) -> list[dict]:
    """The AWS shapes for ``spec.filesystem``; ``[]`` when nothing is mounted."""
    fs = spec.filesystem
    out: list[dict] = []
    if fs.session_storage:
        out.append({"sessionStorage": {"mountPath": fs.session_storage.mount_path}})
    for mount in fs.s3_files:
        out.append({"s3FilesAccessPoint": {
            "accessPointArn": mount.access_point_arn, "mountPath": mount.mount_path,
        }})
    for mount in fs.efs:
        out.append({"efsAccessPoint": {
            "accessPointArn": mount.access_point_arn, "mountPath": mount.mount_path,
        }})
    from app.services.storage_mounts import filesystem_entries

    out.extend(filesystem_entries(spec))  # catalog mounts (storage-mounts §4.2)
    return out
