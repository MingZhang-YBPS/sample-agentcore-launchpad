"""T24 — cross-account artifact copy, against stubbed source/target clients.

No AWS: an in-memory fake registry / bucket stands in for each side. What matters is the
refusal behavior (digest mismatch writes nothing) and idempotence (a re-run copies nothing).
"""

import hashlib
import io
import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.core.errors import AppError
from app.services import artifact_copy as ac
from app.services import byoc_uploads
from tests.conftest import ws_ctx


def _err(code, op="Op"):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeEcr:
    """Just enough of the ECR API for copy_image."""

    def __init__(self, images=None, layers=None, blobs=None):
        self.images: dict[str, tuple[str, str]] = images or {}  # digest -> (manifest, type)
        self.layers: set[str] = set(layers or ())
        self.blobs: dict[str, bytes] = blobs or {}  # source side: digest -> bytes
        self.uploads: dict[str, bytearray] = {}
        self.puts: list[dict] = []
        self.created: list[str] = []
        self.repo_exists = True

    def describe_images(self, repositoryName, imageIds):
        digest = imageIds[0]["imageDigest"]
        if not self.repo_exists:
            raise _err("RepositoryNotFoundException")
        if digest not in self.images:
            raise _err("ImageNotFoundException")
        return {"imageDetails": [{"imageDigest": digest}]}

    def describe_repositories(self, repositoryNames):
        if not self.repo_exists:
            raise _err("RepositoryNotFoundException")

    def create_repository(self, **kw):
        self.repo_exists = True
        self.created.append(kw["repositoryName"])

    def batch_get_image(self, repositoryName, imageIds, acceptedMediaTypes):
        digest = imageIds[0]["imageDigest"]
        if digest not in self.images:
            return {"images": [], "failures": [{"failureCode": "ImageNotFound"}]}
        manifest, media = self.images[digest]
        return {"images": [{"imageManifest": manifest, "imageManifestMediaType": media}]}

    def batch_check_layer_availability(self, repositoryName, layerDigests):
        return {
            "layers": [
                {
                    "layerDigest": d,
                    "layerAvailability": "AVAILABLE" if d in self.layers else "UNAVAILABLE",
                }
                for d in layerDigests
            ]
        }

    def get_download_url_for_layer(self, repositoryName, layerDigest):
        return {"downloadUrl": f"https://ecr.example/{layerDigest}"}

    def initiate_layer_upload(self, repositoryName):
        self.uploads["u1"] = bytearray()
        return {"uploadId": "u1", "partSize": 8}

    def upload_layer_part(
        self, repositoryName, uploadId, partFirstByte, partLastByte, layerPartBlob
    ):
        assert partFirstByte == len(self.uploads[uploadId])
        assert partLastByte - partFirstByte + 1 == len(layerPartBlob) <= 8
        self.uploads[uploadId].extend(layerPartBlob)

    def complete_layer_upload(self, repositoryName, uploadId, layerDigests):
        assert _sha(bytes(self.uploads[uploadId])) == layerDigests[0]
        self.layers.add(layerDigests[0])

    def put_image(self, **kw):
        self.puts.append(kw)
        self.images[kw["imageDigest"]] = (kw["imageManifest"], kw["imageManifestMediaType"])


def _image():
    config, layer = b'{"config":1}', b"layer-bytes-longer-than-one-part"
    manifest = json.dumps(
        {
            "schemaVersion": 2, "mediaType": ac.DOCKER_MANIFEST,
            "config": {"digest": _sha(config)},
            "layers": [{"digest": _sha(layer)}],
        }
    )
    return manifest, _sha(manifest.encode()), {_sha(config): config, _sha(layer): layer}


def _wire(monkeypatch, source_ecr, target_ecr):
    def fake_client(self, service, cache_token=None, **cfg):
        return {"src": source_ecr, "dst": target_ecr}[self.id]

    monkeypatch.setattr("app.services.workspace.WorkspaceContext.client", fake_client)


def _ctxs():
    return (
        ws_ctx({"ecr_repo": "launchpad-agents"}, id="src"),
        ws_ctx({"ecr_repo": "launchpad-agents"}, id="dst", account_id="444455556677"),
    )


def _reader(blobs):
    def read(url):
        data = blobs[url.rsplit("/", 1)[1]]
        for i in range(0, len(data), 5):
            yield data[i : i + 5]

    return read


def test_container_copy_moves_the_image_by_digest(monkeypatch):
    manifest, digest, blobs = _image()
    src = FakeEcr(images={digest: (manifest, ac.DOCKER_MANIFEST)}, blobs=blobs)
    dst = FakeEcr()
    dst.repo_exists = False
    _wire(monkeypatch, src, dst)
    source, target = _ctxs()
    bundle = SimpleNamespace(method="container", artifact={"image_digest": digest})

    result = ac.copy_artifact(bundle, source, target, read_layer=_reader(blobs))

    assert result.status == "copied" and result.digest == digest
    assert dst.created == ["launchpad-agents"]
    assert digest in dst.images and set(blobs) <= dst.layers
    assert dst.puts[0]["imageDigest"] == digest
    assert dst.puts[0]["imageTag"] == f"promoted-{digest[7:19]}"
    assert result.target_artifact["image_uri"] == (
        f"444455556677.dkr.ecr.us-west-2.amazonaws.com/launchpad-agents@{digest}"
    )


def test_container_copy_is_idempotent(monkeypatch):
    manifest, digest, blobs = _image()
    src = FakeEcr(images={digest: (manifest, ac.DOCKER_MANIFEST)}, blobs=blobs)
    dst = FakeEcr()
    _wire(monkeypatch, src, dst)
    source, target = _ctxs()
    bundle = SimpleNamespace(method="container", artifact={"image_digest": digest})
    ac.copy_artifact(bundle, source, target, read_layer=_reader(blobs))
    dst.puts.clear()

    again = ac.copy_artifact(bundle, source, target, read_layer=_reader(blobs))

    assert again.status == "already_present"
    assert dst.puts == []  # nothing written on the re-run


def test_a_partial_earlier_run_only_copies_what_is_missing(monkeypatch):
    manifest, digest, blobs = _image()
    config_digest = next(d for d, b in blobs.items() if b.startswith(b'{"config'))
    src = FakeEcr(images={digest: (manifest, ac.DOCKER_MANIFEST)}, blobs=blobs)
    dst = FakeEcr(layers={config_digest})
    _wire(monkeypatch, src, dst)
    source, target = _ctxs()
    seen = []

    def spy(url):
        seen.append(url)
        yield from _reader(blobs)(url)

    ac.copy_artifact(
        SimpleNamespace(method="container", artifact={"image_digest": digest}),
        source, target, read_layer=spy,
    )
    assert len(seen) == 1 and config_digest not in seen[0]


def test_a_manifest_that_does_not_hash_to_the_digest_is_refused(monkeypatch):
    manifest, digest, blobs = _image()
    src = FakeEcr(images={digest: (manifest + " ", ac.DOCKER_MANIFEST)}, blobs=blobs)
    dst = FakeEcr()
    _wire(monkeypatch, src, dst)
    source, target = _ctxs()
    with pytest.raises(AppError) as err:
        ac.copy_artifact(
            SimpleNamespace(method="container", artifact={"image_digest": digest}),
            source, target, read_layer=_reader(blobs),
        )
    assert err.value.code == "promotion.artifact_digest_mismatch"
    assert dst.puts == [] and not dst.images


def test_a_corrupt_layer_is_refused_before_it_is_completed(monkeypatch):
    manifest, digest, blobs = _image()
    src = FakeEcr(images={digest: (manifest, ac.DOCKER_MANIFEST)}, blobs=blobs)
    dst = FakeEcr()
    _wire(monkeypatch, src, dst)
    source, target = _ctxs()
    tampered = {d: b[:-1] + b"X" for d, b in blobs.items()}
    with pytest.raises(AppError) as err:
        ac.copy_artifact(
            SimpleNamespace(method="container", artifact={"image_digest": digest}),
            source, target, read_layer=_reader(tampered),
        )
    assert err.value.code == "promotion.artifact_digest_mismatch"
    assert dst.puts == [] and not dst.layers


def test_a_bundle_without_coordinates_is_refused_not_rebuilt():
    source, target = _ctxs()
    with pytest.raises(AppError) as err:
        ac.copy_artifact(SimpleNamespace(method="zip_runtime", artifact={}), source, target)
    assert err.value.code == "promotion.artifact_missing"
    harness = ac.copy_artifact(SimpleNamespace(method="harness", artifact={}), source, target)
    assert harness.status == "not_applicable"


def test_an_image_index_copies_its_children_first(monkeypatch):
    m1, d1, b1 = _image()
    index = json.dumps({"mediaType": ac.OCI_INDEX, "manifests": [{"digest": d1}]})
    di = _sha(index.encode())
    src = FakeEcr(
        images={d1: (m1, ac.DOCKER_MANIFEST), di: (index, ac.OCI_INDEX)}, blobs=b1
    )
    dst = FakeEcr()
    _wire(monkeypatch, src, dst)
    source, target = _ctxs()
    ac.copy_artifact(
        SimpleNamespace(method="container", artifact={"image_digest": di}),
        source, target, read_layer=_reader(b1),
    )
    assert [p["imageDigest"] for p in dst.puts] == [d1, di]


# ── S3 ───────────────────────────────────────────────────────────────────────────


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, dict]] = {}
        self.writes = 0

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _err("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _err("404")
        return {"Metadata": self.objects[(Bucket, Key)][1]}

    def upload_fileobj(self, fileobj, Bucket, Key, ExtraArgs=None):
        self.writes += 1
        self.objects[(Bucket, Key)] = (fileobj.read(), (ExtraArgs or {}).get("Metadata", {}))

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.writes += 1
        self.objects[(Bucket, Key)] = (Body, {})


def _s3_setup(monkeypatch, zip_bytes=b"PK-zip-bytes", recorded=None):
    recorded = recorded or hashlib.sha256(zip_bytes).hexdigest()
    src, dst = FakeS3(), FakeS3()
    up = "abc123"
    src.objects[("src-bkt", byoc_uploads.source_key("src", up))] = (zip_bytes, {})
    src.objects[("src-bkt", byoc_uploads.manifest_key("src", up))] = (
        json.dumps({"upload_id": up, "workspace_id": "src", "sha256": recorded}).encode(), {},
    )
    monkeypatch.setattr(
        "app.services.workspace.WorkspaceContext.client",
        lambda self, service, cache_token=None, **cfg: {"src": src, "dst": dst}[self.id],
    )
    source = ws_ctx({"artifacts_bucket": "src-bkt"}, id="src")
    target = ws_ctx({"artifacts_bucket": "dst-bkt"}, id="dst", account_id="444455556677")
    bundle = SimpleNamespace(method="byoc", artifact={"upload_id": up})
    return src, dst, source, target, bundle, up


def test_zip_copy_lands_under_the_target_workspace_and_is_idempotent(monkeypatch):
    src, dst, source, target, bundle, up = _s3_setup(monkeypatch)

    first = ac.copy_artifact(bundle, source, target)
    assert first.status == "copied" and first.target_artifact == {"upload_id": up}
    assert ("dst-bkt", byoc_uploads.source_key("dst", up)) in dst.objects
    moved = json.loads(dst.objects[("dst-bkt", byoc_uploads.manifest_key("dst", up))][0])
    assert moved["workspace_id"] == "dst"

    writes = dst.writes
    second = ac.copy_artifact(bundle, source, target)
    assert second.status == "already_present" and dst.writes == writes


def test_zip_copy_refuses_when_the_bytes_do_not_match_the_recorded_sha(monkeypatch):
    src, dst, source, target, bundle, _ = _s3_setup(monkeypatch, recorded="0" * 64)
    with pytest.raises(AppError) as err:
        ac.copy_artifact(bundle, source, target)
    assert err.value.code == "promotion.artifact_digest_mismatch"
    assert dst.writes == 0
