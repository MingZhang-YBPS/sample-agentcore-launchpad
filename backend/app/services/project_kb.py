"""Phase 2 isolation, knowledge bases (projects design §10.5, R10-kb): the project KB
service role, the project S3 prefix, and the provisioner of `project_provision`'s
`kb-role` stage.

Today every KB of a workspace is created with the one shared service role
`launchpad-kb-role[-<region>]` (it reads `<artifacts>/kb/*`, and a per-KB inline policy
`launchpad-kb-<kb_id>` extends it to an external bucket) and uploads land under
`kb/<kb_id>/`. An isolated project gets instead:

* its own role `launchpad-kb-role-<pid>[-<region>]`, tagged `launchpad:project=<pid>`,
  whose base grant is `<artifacts>/projects/<pid>/kb/*` only — the per-KB inline
  policies of the project's KBs on external buckets go on this role, so the role
  reads exactly the project's prefix plus the buckets its KBs registered;
* new KBs created with that role, tagged `launchpad:project` (the boundary's
  `ProjectTagged` statement authorizes on the tag), and uploading under
  `projects/<pid>/kb/<kb_id>/`.

Which layout a KB has is read from the KB itself: a KB whose `roleArn` is a recorded
project KB role (`project_resources` kind `kb_role`) belongs to that project's layout;
any other KB — every KB of a shared project, and the KBs an isolated project had
before it was isolated — keeps the shared role and the `kb/<kb_id>/` path. So
single-project workspaces behave exactly as before, and existing KBs keep reading
until they are moved with the explicit `migrate_kb` (§10.5 "存量 KB"):

    copy objects → grant external sources on the project role → UpdateKnowledgeBase
    roleArn → tag → new data source on the new prefix → sync
    … then `finish_kb_migration` (once that sync completed): drop the old data
    source and the shared role's inline policy  (先加后减, §10.16)

TO VERIFY (design §10.5): whether `UpdateKnowledgeBase` accepts a changed `roleArn` on
a MANAGED knowledge base. If the service refuses it, `migrate_kb` stops before any
change to the KB (the copied objects are harmless) with
`kb.role_update_unsupported`; the design's fallback — rebuild the KB on the project
role and keep the old one until its agents are re-pinned — is not automated here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.core.regions import partition_for_region
from app.models.project_isolation import (
    KIND_KB_ROLE,
    KIND_M2M_CLIENT,
    STATUS_FAILED,
    ProjectResource,
)
from app.services import project_isolation as isolation
from app.services.project_iam import PROJECT_TAG_KEY, isolated_project
from app.services.workspace_iam import (
    WORKSPACE_TAG_KEY,
    ForeignResourceError,
    regional_role_name,
    service_trust_policy,
)

logger = logging.getLogger(__name__)

# the shared layout: what every KB had before phase 2
SHARED_ROOT = "kb/"
BASE_POLICY_NAME = "launchpad-kb"
MAX_ROLE_NAME = 64


def project_root(project_id: str) -> str:
    """`projects/<pid>/kb/` — inside R10-misc's project prefix `projects/<pid>/`."""
    return f"{isolation.resource_names(project_id).s3_prefix}kb/"


def kb_role_name(project_id: str, region: str) -> str:
    """`launchpad-kb-role-<pid>[-<region>]`: the regional rule of the shared roles
    (role names are account-global, one workspace per account and region). A 32-char
    project id plus a long region can pass IAM's 64 characters; the id is then cut
    and a digest keeps the name unique and deterministic."""
    name = regional_role_name(isolation.resource_names(project_id).kb_role, region)
    if len(name) <= MAX_ROLE_NAME:
        return name
    digest = hashlib.sha256(project_id.encode()).hexdigest()[:6]
    suffix = name.removeprefix(isolation.resource_names(project_id).kb_role)
    keep = MAX_ROLE_NAME - len("launchpad-kb-role-") - len(suffix) - len(digest) - 1
    return f"launchpad-kb-role-{project_id[:keep].rstrip('-')}-{digest}{suffix}"


def kb_role_policy(project_id: str, artifacts_bucket: str, region: str) -> dict[str, Any]:
    """The project role's base grant: the project's KB prefix of the artifacts bucket
    (`workspace_iam.kb_role_policy` with `kb/` narrowed to `projects/<pid>/kb/`)."""
    bucket_arn = f"arn:{partition_for_region(region)}:s3:::{artifacts_bucket}"
    root = project_root(project_id)
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "KbDataObjects",
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": f"{bucket_arn}/{root}*",
            },
            {
                "Sid": "KbDataList",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": bucket_arn,
                "Condition": {"StringLike": {"s3:prefix": f"{root}*"}},
            },
        ],
    }


def _role_tags(project_id: str, workspace_id: str) -> list[dict[str, str]]:
    # deliberately no `launchpad:isolation` tag: that one is the agent-role CreateRole
    # condition of the spoke template, and a service role carries no boundary
    return [
        {"Key": PROJECT_TAG_KEY, "Value": project_id},
        {"Key": WORKSPACE_TAG_KEY, "Value": workspace_id},
        {"Key": "launchpad:managed", "Value": "true"},
    ]


def _is_already_exists(exc: Exception) -> bool:
    return "EntityAlreadyExists" in f"{type(exc).__name__}{exc}"


def _role_tag_map(iam: Any, role_name: str) -> dict[str, str]:
    return {str(t.get("Key")): str(t.get("Value"))
            for t in iam.list_role_tags(RoleName=role_name).get("Tags", [])}


@dataclass(frozen=True)
class KbRole:
    name: str
    arn: str
    created: bool


def ensure_kb_role(iam: Any, *, project_id: str, workspace_id: str, account_id: str,
                   region: str, artifacts_bucket: str,
                   on_exists: Callable[[KbRole], None] = lambda _r: None,
                   log: Callable[[str], None] = lambda _m: None) -> KbRole:
    """Create or reconcile the project's KB service role; idempotent by name.

    `on_exists` is called the moment the role exists (before its policy is written),
    so a job killed in between leaves it recorded. A role of the name that is not
    tagged for this project is refused, as `workspace_iam.ensure_role` refuses an
    untagged shared role: adopting it would hand this project another one's reads."""
    name = kb_role_name(project_id, region)
    trust = service_trust_policy("bedrock.amazonaws.com", account_id)
    try:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=(f"Assumed by Bedrock Managed Knowledge Bases of project "
                         f"{project_id} to ingest its S3 data sources"),
            Tags=_role_tags(project_id, workspace_id),
        )["Role"]["Arn"]
        role = KbRole(name, arn, True)
        log(f"created role {name}")
    except Exception as exc:  # noqa: BLE001 — the SDK's typed error is client-specific
        if not _is_already_exists(exc):
            raise
        tags = _role_tag_map(iam, name)
        if tags.get(PROJECT_TAG_KEY) != project_id:
            raise ForeignResourceError(
                f"IAM role {name} already exists without the {PROJECT_TAG_KEY}="
                f"{project_id} tag — it was not made by Launchpad for this project; "
                "remove it, then retry"
            ) from exc
        role = KbRole(name, iam.get_role(RoleName=name)["Role"]["Arn"], False)
        if tags.get(WORKSPACE_TAG_KEY) != workspace_id:
            # one workspace per (account, region): the previous owner is gone
            iam.tag_role(RoleName=name,
                         Tags=[{"Key": WORKSPACE_TAG_KEY, "Value": workspace_id}])
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        log(f"reusing role {name}")
    on_exists(role)
    iam.put_role_policy(
        RoleName=name,
        PolicyName=BASE_POLICY_NAME,
        PolicyDocument=json.dumps(kb_role_policy(project_id, artifacts_bucket, region)),
    )
    return role


# ── the provisioner ───────────────────────────────────────────────────────────


class KbRoleProvisioner:
    """The `kb-role` stage of `project_provision` (registered at import below)."""

    def ensure_kb_role(self, ctx: isolation.ProvisionContext) -> str:
        workspace = ctx.workspace
        bucket = (workspace.resources or {}).get("artifacts_bucket")
        if not bucket:
            raise isolation.ProvisionError(
                "artifacts_bucket missing from this workspace's resource map — run its "
                "bootstrap, then retry"
            )
        detail = {"bucket": bucket, "s3_prefix": project_root(ctx.project_id)}

        def record(role: KbRole) -> None:
            ctx.record(isolation.ProvisionedResource(
                KIND_KB_ROLE, resource_id=role.name, arn=role.arn, detail=detail,
            ))

        role = ensure_kb_role(
            workspace.client("iam"), project_id=ctx.project_id,
            workspace_id=ctx.workspace_id, account_id=workspace.account_id,
            region=workspace.region, artifacts_bucket=bucket, on_exists=record,
            log=ctx.log,
        )
        out = f"{role.name} → s3://{bucket}/{detail['s3_prefix']}"
        # The project's M2M client must be an allowed client of the shared KB gateway
        # (§10.5: its targets stay on `launchpad-kb-gw`, Cedar narrows them, R10-gw).
        # On a first run the client does not exist yet (`m2m` runs after this stage);
        # R10-gw's stage calls `kb_gateway.allow_project_client` itself, and a re-run
        # of this one catches up.
        client_id = (ctx.resources.get(KIND_M2M_CLIENT) or {}).get("resource_id")
        if client_id and (workspace.resources or {}).get("kb_gateway_id"):
            from app.services import kb_gateway
            from app.services.agentcore.client import control_client

            kb_gateway.allow_project_client(control_client(workspace), workspace, client_id)
            out += "; project M2M client allowed on the KB gateway"
        return out


# ── which role and prefix a KB uses ───────────────────────────────────────────


@dataclass(frozen=True)
class KbPlacement:
    """The service role a KB uses and where its uploads live. `project_id` is None for
    the shared layout (`kb/<kb_id>/` under the workspace role)."""

    role_arn: str
    root: str = SHARED_ROOT
    project_id: str | None = None

    @property
    def role_name(self) -> str:
        return self.role_arn.rsplit("/", 1)[-1]

    @property
    def tags(self) -> dict[str, str]:
        return {PROJECT_TAG_KEY: self.project_id} if self.project_id else {}

    def prefix(self, kb_id: str) -> str:
        return f"{self.root}{kb_id}/"


def _kb_role_row(project_id: str, workspace_id: str) -> ProjectResource | None:
    db = SessionLocal()
    try:
        return db.get(ProjectResource, (project_id, workspace_id, KIND_KB_ROLE))
    finally:
        db.close()


def shared_placement(workspace: Any) -> KbPlacement:
    return KbPlacement(role_arn=str((workspace.resources or {}).get("kb_role_arn") or ""))


def placement_for_new_kb(workspace: Any, project_id: str | None) -> KbPlacement:
    """Where a KB created now in `project_id` goes: the project role and prefix in an
    isolated project, the shared role and `kb/` otherwise (every shared project, so
    every single-project workspace). An isolated project whose role is not provisioned
    in this workspace yet is refused (409) rather than silently given the shared role."""
    if not isolated_project(project_id):
        return shared_placement(workspace)
    row = _kb_role_row(project_id, workspace.id)
    if row is None or not row.arn or row.status == STATUS_FAILED:
        state = "failed" if row is not None and row.status == STATUS_FAILED else "missing"
        raise AppError(
            "project.kb_role_unavailable",
            f"project '{project_id}' is isolated but its knowledge-base role in this "
            f"workspace is {state} — retry the project's provisioning, then create the "
            "knowledge base",
            {"project_id": project_id, "workspace_id": workspace.id},
            status_code=409,
        )
    return KbPlacement(role_arn=row.arn, root=project_root(project_id),
                       project_id=project_id)


def placement_of(workspace: Any, detail: dict[str, Any] | None) -> KbPlacement:
    """The layout of an existing KB, from its `roleArn`: a recorded project KB role
    means that project's layout; anything else (or no detail) the shared one."""
    role_arn = str((detail or {}).get("roleArn") or "")
    if role_arn:
        db = SessionLocal()
        try:
            with project_context(None):
                project_id = db.scalar(select(ProjectResource.project_id).where(
                    ProjectResource.workspace_id == workspace.id,
                    ProjectResource.kind == KIND_KB_ROLE,
                    ProjectResource.arn == role_arn,
                ).limit(1))
        finally:
            db.close()
        if project_id:
            return KbPlacement(role_arn=role_arn, root=project_root(project_id),
                               project_id=project_id)
    shared = shared_placement(workspace)
    return KbPlacement(role_arn=role_arn or shared.role_arn)


def kb_owner(workspace_id: str, kb_id: str) -> str | None:
    """The project a KB belongs to: its `resource_creators` row, else the workspace's
    default project (`creators.outside_current_project`'s rule)."""
    from app.models.ledger import ResourceCreator
    from app.services import access, creators

    db = SessionLocal()
    try:
        with project_context(None):
            owner = db.scalar(select(ResourceCreator.project_id).where(
                ResourceCreator.workspace_id == workspace_id,
                ResourceCreator.kind == creators.KB,
                ResourceCreator.resource_id == kb_id,
            ).limit(1))
            if owner:
                return owner
            bindings = access.workspace_bindings(db, workspace_id)
    finally:
        db.close()
    default = next((b.project_id for b in bindings if b.is_workspace_default),
                   bindings[0].project_id if bindings else None)
    return default


# ── moving an existing KB to the project role (§10.5 "存量 KB") ────────────────


def _copy_prefix(s3: Any, bucket: str, source: str, target: str) -> int:
    """Copy every object under `source` to the same relative key under `target`
    (server-side; existing targets are overwritten, so a re-run is harmless)."""
    copied, token = 0, None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": source}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            key = obj["Key"]
            s3.copy_object(Bucket=bucket, Key=target + key.removeprefix(source),
                           CopySource={"Bucket": bucket, "Key": key})
            copied += 1
        if not page.get("IsTruncated"):
            return copied
        token = page.get("NextContinuationToken")


def _is_validation(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return "ValidationException" in f"{code}{type(exc).__name__}"


def migrate_kb(workspace: Any, kb_id: str, project_id: str,
               log: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Move an existing KB of an isolated project onto the project role and prefix.
    Explicit (an operator action), idempotent, and additive until the role switch:
    the old objects and the old data source stay until `finish_kb_migration`.

    A KB of another project answers 404 like a missing one."""
    from app.services import knowledge

    if kb_owner(workspace.id, kb_id) != project_id:
        raise NotFoundError("kb.not_found", "knowledge base not found")
    target = placement_for_new_kb(workspace, project_id)
    if target.project_id is None:
        raise AppError("project.isolation_disabled",
                       f"project '{project_id}' is not isolated — enable isolation first",
                       {"project_id": project_id}, status_code=409)
    client = knowledge.agent_client(workspace)
    detail = knowledge._require_managed(knowledge._get_kb(client, kb_id))
    current = placement_of(workspace, detail)
    artifacts = (workspace.resources or {}).get("artifacts_bucket") or ""
    sources = knowledge.data_source_locations(client, kb_id)
    old_uploads = [ds for ds, (bucket, prefix) in sources.items()
                   if bucket == artifacts and (prefix or "").startswith(SHARED_ROOT)]
    external = {ds: loc for ds, loc in sources.items()
                if loc[0] and loc[0] != artifacts}
    out: dict[str, Any] = {"kb_id": kb_id, "project_id": project_id,
                           "role_arn": target.role_arn, "prefix": target.prefix(kb_id),
                           "copied": 0, "old_data_sources": old_uploads}

    # 1. copy the uploads (the old ones keep serving the old data source meanwhile)
    if artifacts and old_uploads:
        out["copied"] = _copy_prefix(workspace.client("s3"), artifacts,
                                     f"{SHARED_ROOT}{kb_id}/", target.prefix(kb_id))
        log(f"copied {out['copied']} object(s) to {target.prefix(kb_id)}")
    # 2. external sources: the project role reads them before it is the KB's role
    for bucket, prefix in external.values():
        knowledge._sync_kb_policy(workspace, kb_id, bucket, prefix or "",
                                  role_name=target.role_name)
    # 3. the role. TO VERIFY: UpdateKnowledgeBase accepts a new roleArn (module doc).
    if current.role_arn != target.role_arn:
        try:
            client.update_knowledge_base(
                knowledgeBaseId=kb_id,
                name=detail["name"],
                description=detail.get("description") or detail["name"],
                roleArn=target.role_arn,
                knowledgeBaseConfiguration=detail["knowledgeBaseConfiguration"],
            )
        except Exception as exc:
            if not _is_validation(exc):
                raise
            raise AppError(
                "kb.role_update_unsupported",
                "the knowledge-base service refused to change this KB's role; rebuild "
                "the KB in the project (it then uses the project role) and re-pin its "
                f"agents, then delete the old one ({exc})",
                {"kb_id": kb_id, "project_id": project_id}, status_code=409,
            ) from exc
        log(f"knowledge base {kb_id} now uses {target.role_name}")
    # 4. the tag the project boundary authorizes on
    arn = detail.get("knowledgeBaseArn")
    if arn:
        client.tag_resource(resourceArn=arn, tags=target.tags)
    # 5. a data source on the new prefix, synced; the old one stays until finish
    new_ds = None
    if artifacts and old_uploads:
        new_ds = knowledge._create_data_source(client, workspace, kb_id, {"mode": "upload"},
                                               placement=target)
        try:
            out["sync"] = knowledge.start_sync(workspace, kb_id, new_ds)
        except AppError as exc:  # not ready yet — the detail view offers the sync
            out["sync_error"] = exc.message
    out["data_source"] = new_ds
    out["state"] = "syncing" if new_ds else "done"
    return out


def finish_kb_migration(workspace: Any, kb_id: str, project_id: str) -> dict[str, Any]:
    """The "减" half: once the new data source's latest sync COMPLETE, delete the old
    `kb/<kb_id>/` data source and the shared role's inline policy for the KB. Leaves
    the old objects (R10-migrate's retire removes legacy keys). 409 while syncing."""
    from app.services import knowledge

    if kb_owner(workspace.id, kb_id) != project_id:
        raise NotFoundError("kb.not_found", "knowledge base not found")
    client = knowledge.agent_client(workspace)
    detail = knowledge._require_managed(knowledge._get_kb(client, kb_id))
    placement = placement_of(workspace, detail)
    if placement.project_id != project_id:
        raise AppError("kb.not_migrated",
                       "this knowledge base does not use the project role yet — migrate "
                       "it first", {"kb_id": kb_id}, status_code=409)
    artifacts = (workspace.resources or {}).get("artifacts_bucket") or ""
    sources = knowledge.data_source_locations(client, kb_id)
    new = [ds for ds, (bucket, prefix) in sources.items()
           if bucket == artifacts and (prefix or "").startswith(placement.root)]
    old = [ds for ds, (bucket, prefix) in sources.items()
           if bucket == artifacts and (prefix or "").startswith(SHARED_ROOT)]
    for ds in new:
        jobs = knowledge.list_ingestion_jobs(workspace, kb_id, ds)
        if not jobs or jobs[0].get("status") != "COMPLETE":
            raise AppError("kb.migration_syncing",
                           "the new data source has not completed a sync yet — wait for "
                           "it, then finish", {"kb_id": kb_id, "ds_id": ds},
                           status_code=409)
    for ds in old:
        client.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds)
    shared = shared_placement(workspace)
    if shared.role_arn and shared.role_name != placement.role_name:
        knowledge._delete_kb_policy(workspace, kb_id, role_name=shared.role_name)
    return {"kb_id": kb_id, "deleted_data_sources": old, "state": "done"}


isolation.set_provisioners(kb=KbRoleProvisioner())
