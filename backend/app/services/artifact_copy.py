"""Cross-account artifact copy — "build once, deploy many" (roadmap T24).

A promotion must deploy the artifact that was TESTED, not a rebuild of the same source.
Given a release bundle and the source and target `WorkspaceContext`, this module puts
that exact artifact into the target account and proves it arrived intact.

**Container images — ECR API, by digest.** Chosen mechanism: read the manifest from the
source with `BatchGetImage`, move each layer the target lacks with
`GetDownloadUrlForLayer` -> `InitiateLayerUpload` / `UploadLayerPart` /
`CompleteLayerUpload`, then `PutImage` the manifest with the *expected* `imageDigest`.
Why not the alternatives:

* Cross-account **blob mounting** is a Docker-registry-v2 `POST ...?mount=` feature; it
  works inside one registry, and cross-account only when the source repository policy
  grants the target — which would mean editing the DEV account's repository policy for
  every prod account. There is no ECR-API form of it. So layers are streamed through the
  hub instead: slower than a mount, but it needs no policy on the source and no trust
  from the source to the target.
* **ECR replication** is registry-wide and push-triggered, asynchronous, and not
  addressable per image: it would copy every dev image to prod, and a promotion could not
  wait on "this digest arrived".
* `docker pull/push` needs a Docker daemon and `GetAuthorizationToken` on both sides.

Verification is not optional: the manifest is hashed and compared with the bundle's
digest BEFORE anything is written; every layer is hashed while streaming and compared
BEFORE `CompleteLayerUpload`; and after `PutImage` the target is asked for the digest
again. Any disagreement raises `promotion.artifact_digest_mismatch` and nothing is
tagged. Multi-architecture indexes copy each child manifest first, then the index.

**Zip / BYOC source archives — S3.** The staged object (and its manifest) is streamed
from the source artifacts bucket into the target's and its SHA-256 compared with the
digest recorded when it was uploaded. The key keeps the `upload_id` and swaps the
workspace segment (`byoc/<workspace>/<upload_id>/...`), so the deployer's existing
`download_upload(target, target.id, upload_id)` finds it and the spec needs no rewrite.

**Idempotent.** Both paths first ask the target whether the artifact is already there
(digest present in ECR; object present with the same recorded SHA-256) and return
`already_present` without writing. A re-run after a partial failure resumes: layers the
target already has are skipped.

Every client comes from `WorkspaceContext.client`, i.e. the `aws_clients` funnel; the
only other network use is the presigned layer URL ECR itself returns.
"""

import hashlib
import io
import json
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

from botocore.exceptions import ClientError

from app.core.errors import AppError
from app.services import byoc_uploads
from app.services.workspace import WorkspaceContext

DEFAULT_REPO = "launchpad-agents"
DOCKER_INDEX = "application/vnd.docker.distribution.manifest.list.v2+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
ACCEPTED_MANIFESTS = [OCI_INDEX, DOCKER_INDEX, OCI_MANIFEST, DOCKER_MANIFEST]
_INDEX_TYPES = (OCI_INDEX, DOCKER_INDEX)
_MAX_PART = 20 * 1024 * 1024  # ECR's upload-part ceiling
_READ_CHUNK = 1024 * 1024

LayerReader = Callable[[str], Iterator[bytes]]


@dataclass
class CopyResult:
    """What a copy did, and the coordinates the target deployment must use."""

    kind: str  # container_image | source_archive | none
    status: str  # copied | already_present | not_applicable
    digest: str = ""
    # artifact fields to overlay on the bundle's when deploying into the target
    target_artifact: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def mismatch(what: str, expected: str, actual: str) -> AppError:
    return AppError(
        "promotion.artifact_digest_mismatch",
        f"{what} does not match the tested artifact (expected {expected}, got {actual}) — "
        "refusing to copy",
        {"expected": expected, "actual": actual},
        status_code=409,
    )


def _code(exc: ClientError) -> str:
    return str((exc.response or {}).get("Error", {}).get("Code", ""))


def _sha256_ref(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def default_layer_reader(url: str) -> Iterator[bytes]:
    """Stream a presigned ECR layer URL (the one HTTP call that is not the AWS SDK)."""
    if not url.startswith("https://"):
        raise AppError(
            "promotion.artifact_copy_failed",
            "ECR returned a non-HTTPS layer URL",
            status_code=502,
        )
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 — https checked
        while chunk := response.read(_READ_CHUNK):
            yield chunk


# ── ECR ──────────────────────────────────────────────────────────────────────────


def image_present(ecr: Any, repo: str, digest: str) -> bool:
    try:
        found = ecr.describe_images(repositoryName=repo, imageIds=[{"imageDigest": digest}])
    except ClientError as exc:
        if _code(exc) in ("ImageNotFoundException", "RepositoryNotFoundException"):
            return False
        raise
    return any(d.get("imageDigest") == digest for d in found.get("imageDetails") or [])


def _ensure_repo(ecr: Any, repo: str) -> None:
    try:
        ecr.describe_repositories(repositoryNames=[repo])
    except ClientError as exc:
        if _code(exc) != "RepositoryNotFoundException":
            raise
        ecr.create_repository(
            repositoryName=repo,
            imageScanningConfiguration={"scanOnPush": True},
            tags=[{"Key": "launchpad:managed", "Value": "true"}],
        )


def _rechunk(chunks: Iterator[bytes], size: int) -> Iterator[bytes]:
    buf = io.BytesIO()
    for chunk in chunks:
        buf.write(chunk)
        while buf.tell() >= size:
            data = buf.getvalue()
            yield data[:size]
            buf = io.BytesIO()
            buf.write(data[size:])
    if buf.tell():
        yield buf.getvalue()


def _copy_layer(
    src: Any, dst: Any, src_repo: str, dst_repo: str, layer: str, read: LayerReader
) -> None:
    url = src.get_download_url_for_layer(repositoryName=src_repo, layerDigest=layer)[
        "downloadUrl"
    ]
    upload = dst.initiate_layer_upload(repositoryName=dst_repo)
    part_size = min(int(upload.get("partSize") or _MAX_PART), _MAX_PART)
    hasher = hashlib.sha256()
    offset = 0
    for part in _rechunk(read(url), part_size):
        hasher.update(part)
        dst.upload_layer_part(
            repositoryName=dst_repo,
            uploadId=upload["uploadId"],
            partFirstByte=offset,
            partLastByte=offset + len(part) - 1,
            layerPartBlob=part,
        )
        offset += len(part)
    actual = f"sha256:{hasher.hexdigest()}"
    if actual != layer:
        # abandoned before CompleteLayerUpload: ECR expires the dangling upload
        raise mismatch(f"layer of {src_repo}", layer, actual)
    dst.complete_layer_upload(
        repositoryName=dst_repo, uploadId=upload["uploadId"], layerDigests=[layer]
    )


def copy_image(
    src: Any,
    dst: Any,
    *,
    src_repo: str,
    dst_repo: str,
    digest: str,
    tag: str | None = None,
    read_layer: LayerReader = default_layer_reader,
    steps: list[str] | None = None,
) -> str:
    """Copy one image (or index) by digest; returns `copied` or `already_present`."""
    steps = steps if steps is not None else []
    if image_present(dst, dst_repo, digest):
        steps.append(f"{digest[:19]} already in target")
        return "already_present"
    got = src.batch_get_image(
        repositoryName=src_repo,
        imageIds=[{"imageDigest": digest}],
        acceptedMediaTypes=ACCEPTED_MANIFESTS,
    )
    images = got.get("images") or []
    if not images:
        raise AppError(
            "promotion.artifact_missing",
            f"image {digest} is not in the source repository {src_repo}",
            {"failures": got.get("failures") or []},
            status_code=409,
        )
    manifest = images[0]["imageManifest"]
    media_type = images[0].get("imageManifestMediaType") or json.loads(manifest).get(
        "mediaType", DOCKER_MANIFEST
    )
    actual = _sha256_ref(manifest.encode())
    if actual != digest:
        raise mismatch("source manifest", digest, actual)
    body = json.loads(manifest)
    _ensure_repo(dst, dst_repo)
    if media_type in _INDEX_TYPES:
        for child in body.get("manifests") or []:
            copy_image(
                src, dst, src_repo=src_repo, dst_repo=dst_repo, digest=child["digest"],
                read_layer=read_layer, steps=steps,
            )
    else:
        blobs = [body["config"]["digest"]] + [layer["digest"] for layer in body["layers"]]
        have = dst.batch_check_layer_availability(repositoryName=dst_repo, layerDigests=blobs)
        available = {
            item["layerDigest"] for item in have.get("layers") or []
            if item.get("layerAvailability") == "AVAILABLE"
        }
        for blob in blobs:
            if blob in available:
                continue
            _copy_layer(src, dst, src_repo, dst_repo, blob, read_layer)
            steps.append(f"layer {blob[:19]} copied")
    put: dict[str, Any] = {
        "repositoryName": dst_repo,
        "imageManifest": manifest,
        "imageManifestMediaType": media_type,
        "imageDigest": digest,
    }
    if tag:
        put["imageTag"] = tag
    dst.put_image(**put)
    if not image_present(dst, dst_repo, digest):
        raise mismatch("target image after PutImage", digest, "absent")
    steps.append(f"manifest {digest[:19]} put")
    return "copied"


def _ecr_uri(ctx: WorkspaceContext, repo: str) -> str:
    override = (ctx.resources or {}).get("ecr_repo_uri")
    if override and str(override).rsplit("/", 1)[-1] == repo:
        return str(override)
    return f"{ctx.account_id}.dkr.ecr.{ctx.region}.amazonaws.com/{repo}"


def _source_repo_and_digest(artifact: dict[str, Any], source: WorkspaceContext) -> tuple[str, str]:
    repo = str((source.resources or {}).get("ecr_repo") or DEFAULT_REPO)
    digest = str(artifact.get("image_digest") or "")
    uri = str(artifact.get("image_uri") or "")
    if uri:
        registry_path = uri.split("/", 1)[-1]
        repo = registry_path.split("@", 1)[0].rsplit(":", 1)[0] if "/" in uri else repo
        if not digest and "@sha256:" in uri:
            digest = uri.split("@", 1)[1]
    return repo, digest


def copy_container_image(
    artifact: dict[str, Any],
    source: WorkspaceContext,
    target: WorkspaceContext,
    *,
    read_layer: LayerReader = default_layer_reader,
) -> CopyResult:
    repo, digest = _source_repo_and_digest(artifact, source)
    if not digest.startswith("sha256:"):
        raise AppError(
            "promotion.artifact_missing",
            "the bundle records no image digest — the target would have to rebuild",
            status_code=409,
        )
    dst_repo = str((target.resources or {}).get("ecr_repo") or DEFAULT_REPO)
    steps: list[str] = []
    status = copy_image(
        source.client("ecr"), target.client("ecr"),
        src_repo=repo, dst_repo=dst_repo, digest=digest,
        tag=f"promoted-{digest.split(':', 1)[1][:12]}",
        read_layer=read_layer, steps=steps,
    )
    uri = f"{_ecr_uri(target, dst_repo)}@{digest}"
    return CopyResult(
        kind="container_image",
        status=status,
        digest=digest,
        target_artifact={"image_digest": digest, "image_uri": uri},
        detail=f"{repo}@{digest[:19]} -> {target.account_id}/{dst_repo}",
        steps=steps,
    )


# ── S3 ───────────────────────────────────────────────────────────────────────────


def _bucket(ctx: WorkspaceContext) -> str:
    bucket = (ctx.resources or {}).get("artifacts_bucket")
    if not bucket:
        raise AppError(
            "promotion.artifact_copy_failed",
            f"workspace '{ctx.id}' has no artifacts_bucket — bootstrap it first",
            status_code=409,
        )
    return str(bucket)


def copy_source_archive(
    artifact: dict[str, Any], source: WorkspaceContext, target: WorkspaceContext
) -> CopyResult:
    upload_id = str(artifact.get("upload_id") or "")
    if not byoc_uploads.UPLOAD_ID_RE.fullmatch(upload_id):
        raise AppError(
            "promotion.artifact_missing", "the bundle records no staged upload", status_code=409
        )
    s_s3, t_s3 = source.client("s3"), target.client("s3")
    s_bucket, t_bucket = _bucket(source), _bucket(target)
    manifest = byoc_uploads.get_manifest(source, upload_id, s3_client=s_s3)
    expected = str(manifest.get("sha256") or "")
    if not expected:
        raise AppError(
            "promotion.artifact_missing",
            "the upload manifest records no sha256 — cannot verify a copy",
            status_code=409,
        )
    src_key = byoc_uploads.source_key(source.id, upload_id)
    dst_key = byoc_uploads.source_key(target.id, upload_id)
    try:
        head = t_s3.head_object(Bucket=t_bucket, Key=dst_key)
        if (head.get("Metadata") or {}).get("sha256") == expected:
            return CopyResult(
                kind="source_archive", status="already_present", digest=f"sha256:{expected}",
                target_artifact={"upload_id": upload_id},
                detail=f"s3://{t_bucket}/{dst_key}",
            )
    except ClientError as exc:
        if _code(exc) not in ("404", "NoSuchKey", "NotFound"):
            raise
    hasher = hashlib.sha256()
    with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as spool:
        body = s_s3.get_object(Bucket=s_bucket, Key=src_key)["Body"]
        while chunk := body.read(_READ_CHUNK):
            hasher.update(chunk)
            spool.write(chunk)
        if hasher.hexdigest() != expected:
            raise mismatch("source archive", f"sha256:{expected}", f"sha256:{hasher.hexdigest()}")
        spool.seek(0)
        t_s3.upload_fileobj(
            spool, t_bucket, dst_key,
            ExtraArgs={"Metadata": {"sha256": expected}, "ChecksumAlgorithm": "SHA256"},
        )
    # the manifest travels with it, re-homed to the target workspace
    moved = {**manifest, "workspace_id": target.id}
    t_s3.put_object(
        Bucket=t_bucket,
        Key=byoc_uploads.manifest_key(target.id, upload_id),
        Body=json.dumps(moved, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    head = t_s3.head_object(Bucket=t_bucket, Key=dst_key)
    if (head.get("Metadata") or {}).get("sha256") != expected:
        raise mismatch("target object", expected, str((head.get("Metadata") or {}).get("sha256")))
    return CopyResult(
        kind="source_archive", status="copied", digest=f"sha256:{expected}",
        target_artifact={"upload_id": upload_id},
        detail=f"s3://{s_bucket}/{src_key} -> s3://{t_bucket}/{dst_key}",
    )


# ── entry point ──────────────────────────────────────────────────────────────────


def copy_artifact(
    bundle: Any,
    source: WorkspaceContext,
    target: WorkspaceContext,
    *,
    read_layer: LayerReader = default_layer_reader,
) -> CopyResult:
    """Copy the bundle's tested artifact into `target`.

    A built image wins over a staged archive (a `container_source` upload is the build
    INPUT; the image is the tested artifact and rebuilding it is what this exists to
    avoid). A harness has no artifact.
    """
    artifact = bundle.artifact or {}
    if artifact.get("image_digest") or artifact.get("image_uri"):
        return copy_container_image(artifact, source, target, read_layer=read_layer)
    if artifact.get("upload_id"):
        return copy_source_archive(artifact, source, target)
    if bundle.method == "harness":
        return CopyResult(
            kind="none", status="not_applicable", detail="a harness has no build artifact"
        )
    raise AppError(
        "promotion.artifact_missing",
        "the bundle records no image digest or staged upload — the target would have to "
        "rebuild, which a promotion refuses to do",
        status_code=409,
    )
