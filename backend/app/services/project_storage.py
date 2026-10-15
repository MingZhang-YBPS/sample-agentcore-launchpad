"""S3 and ECR prefixes per isolated project (projects design §10.10, R10-misc).

An agent of an **isolated** project keeps its deploy artifacts under the project's
own key prefix and its images in the project's own ECR repository, so the project's
permissions boundary (`project_iam`, §10.3) can hand its agents exactly those:

* **S3** — `projects/<pid>/agents/<name>/…` (code and BYOC packages),
  `projects/<pid>/builds/<name>/…` (CodeBuild sources). Existing keys
  (`agents/<name>/…`) are not moved: the next deploy writes to the new prefix, and the
  boundary keeps the legacy prefix read-only.
* **ECR** — `<shared repo>/<pid>` (`launchpad-agents/<pid>`, `resource_names`), made
  by the `ecr` stage of `project_provision` with the shared repository's scan setting
  (`workspace_bootstrap._ensure_repository`) and tagged with the project. A build uses
  it only once that row is **ready** in the workspace, so a deploy before (or without)
  a successful run keeps today's shared repository; images already there stay there.

A shared project — the `default` one, every single-project workspace — gets exactly
today's keys and repository, with no extra AWS call.

The `StorageProvisioner` here replaces the R10-model placeholder for the `ecr` and
`s3-prefix` stages; it registers itself on import (`set_provisioners`).
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

from app.models.project_isolation import KIND_ECR_REPO, KIND_S3_PREFIX, STATUS_READY
from app.services import project_isolation as isolation

logger = logging.getLogger(__name__)

SHARED_ECR_REPO = "launchpad-agents"
PROJECTS_PREFIX = "projects/"


def isolated(project_id: str | None) -> bool:
    """Whether an agent of `project_id` takes the project's prefixes (§10.10): the
    project exists and is isolated — the same test as its span attributes (§10.8)."""
    from app.services.project_telemetry import attributed

    return attributed(project_id)


def s3_prefix(project_id: str) -> str:
    return f"{PROJECTS_PREFIX}{project_id}/"


def artifact_key(project_id: str | None, key: str) -> str:
    """`key` (`agents/<name>/…`, `builds/<name>/…`) under the project's prefix for an
    isolated project; unchanged for every other one."""
    if project_id and isolated(project_id):
        return f"{s3_prefix(project_id)}{key}"
    return key


def shared_repo(workspace: Any) -> str:
    return str((getattr(workspace, "resources", None) or {}).get("ecr_repo")
               or SHARED_ECR_REPO)


def project_repo_name(workspace: Any, project_id: str) -> str:
    """`<shared repo>/<pid>` — the name the boundary's ECR statement allows."""
    return f"{shared_repo(workspace)}/{project_id}"


def _ready_repo(project_id: str, workspace_id: str) -> str | None:
    from app.core.db import SessionLocal
    from app.core.project_scope import project_context
    from app.models.project_isolation import ProjectResource

    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(ProjectResource, (project_id, workspace_id, KIND_ECR_REPO))
            if row is None or row.status != STATUS_READY or not row.resource_id:
                return None
            return str(row.resource_id)
    finally:
        db.close()


def ecr_repo(workspace: Any, project_id: str | None) -> str:
    """The repository an agent's images are built into: the project's once it is
    ready in this workspace, else the shared one."""
    if project_id and getattr(workspace, "id", None) and isolated(project_id):
        found = _ready_repo(project_id, workspace.id)
        if found:
            return found
    return shared_repo(workspace)


def ecr_repos(workspace: Any, project_id: str | None) -> list[str]:
    """Every repository an agent's images may live in: the one new builds use, then
    the shared one when that differs (images built before the project repository
    existed — read-only under the boundary, §10.3)."""
    current = ecr_repo(workspace, project_id)
    shared = shared_repo(workspace)
    return [current] if current == shared else [current, shared]


# ── the provisioner (stages `ecr`, `s3-prefix`) ──────────────────────────────


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "")) or type(exc).__name__
    return type(exc).__name__


class StorageProvisioner:
    """§10.10: the project's ECR repository and S3 prefix in one workspace."""

    def ensure_ecr_repo(self, ctx: isolation.ProvisionContext) -> str:
        from app.services import workspace_bootstrap
        from app.services.project_telemetry import project_tags

        workspace = ctx.workspace
        name = project_repo_name(workspace, ctx.project_id)
        ecr = workspace.client("ecr")
        try:
            repo = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
            ctx.log(f"reusing ECR repository {name}")
        except ClientError as exc:
            if _error_code(exc) != "RepositoryNotFoundException":
                raise
            repo = ecr.create_repository(
                repositoryName=name,
                # the shared repository's setting (workspace_bootstrap._ensure_repository)
                imageScanningConfiguration={"scanOnPush": True},
                tags=[{"Key": k, "Value": v} for k, v in project_tags(
                    ctx.project_id, workspace_id=ctx.workspace_id).items()],
            )["repository"]
            ctx.log(f"created ECR repository {name}")
        ctx.record(isolation.ProvisionedResource(
            kind=KIND_ECR_REPO, resource_id=name, arn=repo.get("repositoryArn"),
            detail={"uri": repo.get("repositoryUri")},
        ))
        # CodeBuild pushes the project's images: its role must reach the repository
        # (a workspace bootstrapped before this grant was widened)
        widened = workspace_bootstrap.ensure_codebuild_project_repos(workspace, ctx.log)
        return name if widened else f"{name} · CodeBuild role not widened (see log)"

    def ensure_s3_prefix(self, ctx: isolation.ProvisionContext) -> str:
        """Nothing to create — an S3 prefix exists once an object is written under it.
        Recorded so the console lists it with the project's other resources."""
        bucket = str((ctx.workspace.resources or {}).get("artifacts_bucket") or "")
        if not bucket:
            raise isolation.ProvisionError(
                "artifacts_bucket missing from this workspace's resource map — run its "
                "bootstrap"
            )
        prefix = s3_prefix(ctx.project_id)
        partition = getattr(ctx.workspace, "partition", "aws")
        ctx.record(isolation.ProvisionedResource(
            kind=KIND_S3_PREFIX, resource_id=prefix,
            arn=f"arn:{partition}:s3:::{bucket}/{prefix}", detail={"bucket": bucket},
        ))
        return f"s3://{bucket}/{prefix}"


isolation.set_provisioners(storage=StorageProvisioner())
