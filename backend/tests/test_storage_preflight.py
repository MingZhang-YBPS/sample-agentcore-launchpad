"""Storage × network × agent (design storage-mounts §5), the stored spec form of a
catalog mount (§4.2), the per-mode IAM statements (§4.3) and promotion mapping (§6).
No AWS call is made: the storage and EC2 readers are fakes."""

import copy

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Workspace
from app.models.network import NetworkProfile
from app.models.storage import StorageMount
from app.schemas.agent import AgentSpec
from app.services import agent_iam
from app.services import resource_mapping as rm
from app.services import storage_mounts as service
from tests.conftest import TEST_PROJECT, client_error, ws_ctx
from tests.test_project_scope import _project
from tests.test_storage_catalog import EFS_AP, MT_SG, S3_AP, VPC, _allow, _facts, _register

SUBNETS = ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"]
SG = "sg-0ccccccccccccccc3"
S3_AP2 = S3_AP.replace("fsap-0123456789abcdef0", "fsap-0123456789abcdef1")


def _ec2(*, egress=True, ingress=True, azs=("usw2-az1", "usw2-az2"), vpc=VPC):
    agent_sg = {"GroupId": SG, "VpcId": vpc, "IpPermissions": [],
                "IpPermissionsEgress": [{"IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049,
                                         "UserIdGroupPairs": [{"GroupId": MT_SG}]}]
                if egress else []}
    mt_sg = {"GroupId": MT_SG, "VpcId": vpc, "IpPermissionsEgress": [],
             "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049,
                                "UserIdGroupPairs": [{"GroupId": SG}]}] if ingress else []}
    return {
        "subnets": [{"SubnetId": s, "VpcId": vpc, "AvailabilityZoneId": az,
                     "CidrBlock": f"10.0.{i}.0/24"}
                    for i, (s, az) in enumerate(zip(SUBNETS, azs, strict=True))],
        "groups": [agent_sg, mt_sg],
    }


@pytest.fixture
def aws():
    state = {"storage": _facts(), "ec2": _ec2(), "ec2_calls": []}

    def read_storage(ctx, kind, arn):
        return state["storage"]

    def read_ec2(ctx, subnets, groups):
        state["ec2_calls"].append((list(subnets), list(groups)))
        value = state["ec2"]
        if isinstance(value, Exception):
            raise value
        return value

    prev_s, prev_e = service.set_reader(read_storage), service.set_ec2_reader(read_ec2)
    yield state
    service.set_reader(prev_s)
    service.set_ec2_reader(prev_e)


def _profile(pid="np-0123456789ab", **fields):
    db = SessionLocal()
    try:
        db.add(NetworkProfile(id=pid, workspace_id=fields.pop("workspace_id", DEFAULT_WORKSPACE_ID),
                              name=pid, vpc_id=VPC, subnet_ids=SUBNETS, security_group_ids=[SG],
                              status="ok", **fields))
        db.commit()
    finally:
        db.close()
    return pid


def _check(storage_id):
    db = SessionLocal()
    try:
        return service.run_check(db, ws_ctx(), storage_id)
    finally:
        db.close()


def _preflight(spec, project_id=TEST_PROJECT):
    db = SessionLocal()
    try:
        return service.preflight_spec(db, ws_ctx(), spec, project_id)
    finally:
        db.close()


def _spec_dict(*mounts, network="np-0123456789ab", method="container"):
    return {"name": "fs-agent", "method": method, "system_prompt": "p",
            "filesystem": {"mounts": list(mounts)},
            "network": {"profile_id": network} if network else None}


def _by_check(report):
    return {r["check"]: r for r in report["results"]}


# ── preflight (§5) ────────────────────────────────────────────────────────────


def test_a_reachable_mount_passes(aws):
    _profile()
    _register("datasets")
    _allow(TEST_PROJECT, "datasets")
    _check("datasets")
    report = _preflight(_spec_dict({"storage_id": "datasets", "mode": "ro"}))
    checks = _by_check(report)
    assert report["status"] == "ok", report
    for key in ("storage.status", "storage.vpc", "storage.azs", "storage.egress_2049",
                "storage.ingress_2049"):
        assert checks[key]["severity"] == "ok", checks[key]
    assert checks["storage.posix"]["severity"] == "info"
    assert all(r["storage_id"] == "datasets" for r in report["results"])
    # the profile's subnets + SGs and the mount targets' SGs, in one read
    assert aws["ec2_calls"] == [(SUBNETS, sorted([SG, MT_SG]))]


@pytest.mark.parametrize("change, check", [
    ({"ec2": {"vpc": "vpc-0bbbbbbbbbbbbbbbb"}}, "storage.vpc"),
    ({"storage": {"azs": ("usw2-az1",)}}, "storage.azs"),
    ({"ec2": {"egress": False}}, "storage.egress_2049"),
    ({"ec2": {"ingress": False}}, "storage.ingress_2049"),
])
def test_each_blocking_check(aws, change, check):
    aws["storage"] = _facts(**change.get("storage", {}))
    aws["ec2"] = _ec2(**change.get("ec2", {}))
    _profile()
    if "vpc" in change.get("ec2", {}):
        db = SessionLocal()
        try:
            db.get(NetworkProfile, "np-0123456789ab").vpc_id = None
            db.commit()
        finally:
            db.close()
    _register("datasets")
    _allow(TEST_PROJECT, "datasets")
    _check("datasets")
    report = _preflight(_spec_dict({"storage_id": "datasets"}))
    assert report["status"] == "blocked"
    assert _by_check(report)[check]["severity"] == "block", report


def test_an_ec2_read_failure_degrades_to_warnings_and_the_profile_azs(aws):
    _profile(check_results=[{"check": "subnets.azs", "severity": "ok",
                             "details": {"azs": ["usw2-az1", "usw2-az3"]}}])
    _register("datasets")
    _allow(TEST_PROJECT, "datasets")
    _check("datasets")
    aws["ec2"] = client_error("UnauthorizedOperation")
    checks = _by_check(_preflight(_spec_dict({"storage_id": "datasets"})))
    assert checks["storage.nfs"]["severity"] == "warn"
    assert "UnauthorizedOperation" in checks["storage.nfs"]["message"]
    # the profile's stored AZs still say usw2-az3 has no mount target
    assert checks["storage.azs"]["severity"] == "block"
    assert checks["storage.azs"]["details"]["azs"] == ["usw2-az3"]


def test_mode_status_and_shared_state(aws):
    _profile()
    _register("state", max_mode="rw", data_class="shared_state")
    _register("ref", EFS_AP)
    _allow(TEST_PROJECT, "state", "ref")
    _check("state")
    report = _preflight(_spec_dict({"storage_id": "ref", "mode": "rw"}))
    checks = _by_check(report)
    assert checks["storage.mode"]["severity"] == "block"
    # never checked: the status and the unknown mount targets warn, not block
    assert checks["storage.status"]["severity"] == "warn"
    assert checks["storage.mount_targets"]["severity"] == "warn"

    db = SessionLocal()
    try:
        db.get(Workspace, DEFAULT_WORKSPACE_ID).release_policy = {"release_mode": "gated"}
        db.commit()
    finally:
        db.close()
    shared = _by_check(_preflight(_spec_dict({"storage_id": "state", "mode": "rw"})))
    assert shared["storage.shared_state"]["severity"] == "warn"


def test_no_network_or_a_hidden_entry(aws):
    _register("datasets")
    _allow(TEST_PROJECT, "datasets")
    report = _preflight(_spec_dict({"storage_id": "datasets"}, network=None))
    assert report["status"] == "blocked"
    assert report["results"][0]["check"] == "storage.network"
    _project("alpha")
    with pytest.raises(NotFoundError) as caught:
        _preflight(_spec_dict({"storage_id": "datasets"}), project_id="alpha")
    assert caught.value.code == "storage.not_found"
    assert _preflight({"filesystem": {}}) == {"status": "ok", "results": []}


def test_assert_preflight_refuses_with_409(aws):
    aws["ec2"] = _ec2(egress=False)
    _profile()
    _register("datasets")
    _allow(TEST_PROJECT, "datasets")
    _check("datasets")
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as caught:
            service.assert_preflight(db, ws_ctx(), _spec_dict({"storage_id": "datasets"}),
                                     TEST_PROJECT)
    finally:
        db.close()
    assert caught.value.code == "storage.preflight_failed" and caught.value.status_code == 409
    assert any(r["check"] == "storage.egress_2049" for r in caught.value.detail["results"])


def test_the_check_with_a_default_profile_runs_the_cross_checks(aws):
    _profile()
    aws["storage"] = _facts(azs=("usw2-az1",))
    _register("datasets", network_profile_id="np-0123456789ab")
    entry = _check("datasets")
    assert entry["status"] == "blocked"
    azs = next(r for r in entry["check_results"] if r["check"] == "storage.azs")
    assert azs["profile_id"] == "np-0123456789ab" and azs["severity"] == "block"


def test_the_preflight_route(client, aws):
    _profile()
    _register("datasets")
    _allow(TEST_PROJECT, "datasets")
    _check("datasets")
    res = client.post("/api/storage-mounts/preflight", json={
        "mounts": [{"storage_id": "datasets"}], "network_profile_id": "np-0123456789ab",
        "method": "byoc", "byoc_artifact_kind": "container_image"})
    assert res.status_code == 200, res.text
    assert _by_check(res.json())["storage.arch"]["severity"] == "info"
    bad = client.post("/api/storage-mounts/preflight", json={"mounts": []})
    assert bad.status_code == 422


# ── the stored spec (§4.2) ────────────────────────────────────────────────────


def test_normalize_stamps_the_catalog_entry_and_replaces_client_values():
    _register("datasets", max_mode="rw")
    _allow(TEST_PROJECT, "datasets")
    db = SessionLocal()
    try:
        stored = service.normalize_spec_storage(db, DEFAULT_WORKSPACE_ID, TEST_PROJECT, _spec_dict(
            {"storage_id": "datasets", "mode": "rw", "kind": "efs", "access_point_arn": EFS_AP}))
    finally:
        db.close()
    mount = stored["filesystem"]["mounts"][0]
    assert mount == {"storage_id": "datasets", "mode": "rw", "mount_path": "/mnt/datasets",
                     "kind": "s3files", "access_point_arn": S3_AP}
    spec = AgentSpec.model_validate(stored)
    assert [(k, m.mode) for k, m in spec.filesystem.catalog_mounts()] == [("s3files", "rw")]
    assert service.filesystem_entries(spec) == [
        {"s3FilesAccessPoint": {"accessPointArn": S3_AP, "mountPath": "/mnt/datasets"}}]


def test_normalize_refuses_hidden_blocked_and_too_wide_mounts():
    _register("datasets")
    _register("broken", EFS_AP)
    _allow(TEST_PROJECT, "broken")
    db = SessionLocal()
    try:
        with pytest.raises(NotFoundError):
            service.normalize_spec_storage(db, DEFAULT_WORKSPACE_ID, TEST_PROJECT,
                                           _spec_dict({"storage_id": "datasets"}))
        _allow(TEST_PROJECT, "datasets", "broken")
        with pytest.raises(AppError) as wide:
            service.normalize_spec_storage(db, DEFAULT_WORKSPACE_ID, TEST_PROJECT,
                                           _spec_dict({"storage_id": "datasets", "mode": "rw"}))
        assert wide.value.code == "storage.mode_exceeds_max" and wide.value.status_code == 400
        db.query(StorageMount).filter_by(id="broken").one().status = "blocked"
        db.commit()
        with pytest.raises(AppError) as blocked:
            service.normalize_spec_storage(db, DEFAULT_WORKSPACE_ID, TEST_PROJECT,
                                           _spec_dict({"storage_id": "broken"}))
        assert blocked.value.code == "storage.blocked"
        plain = {"name": "x", "filesystem": {"s3_files": []}}
        assert service.normalize_spec_storage(
            db, DEFAULT_WORKSPACE_ID, TEST_PROJECT, plain) is plain
    finally:
        db.close()


# ── IAM (§4.3) ────────────────────────────────────────────────────────────────


def _agent_spec(**filesystem):
    return AgentSpec(name="fs-agent", method="container", system_prompt="p",
                     filesystem=filesystem, network={"profile_id": "np-0123456789ab"})


def test_inline_mounts_give_the_existing_policy_shape():
    spec = _agent_spec(s3_files=[{"access_point_arn": S3_AP, "mount_path": "/mnt/a"}],
                       efs=[{"access_point_arn": EFS_AP, "mount_path": "/mnt/b"}])
    assert service.storage_iam_statements(spec) == agent_iam.fs_policy_document(spec)["Statement"]
    assert service.storage_iam_statements(_agent_spec()) == []


def test_catalog_mounts_are_grouped_by_mode():
    spec = _agent_spec(mounts=[
        {"storage_id": "ref", "mode": "ro", "kind": "s3files", "access_point_arn": S3_AP},
        {"storage_id": "state", "mode": "rw", "kind": "s3files", "access_point_arn": S3_AP2},
        {"storage_id": "skills", "mode": "ro"},
    ])
    statements = service.storage_iam_statements(
        spec, {"skills": {"kind": "efs", "access_point_arn": EFS_AP}})
    by_actions = {tuple(s["Action"]): s for s in statements}
    rw = by_actions[("s3files:ClientMount", "s3files:ClientWrite")]
    assert rw["Condition"]["ArnEquals"]["s3files:AccessPointArn"] == [S3_AP2]
    ro = by_actions[("s3files:ClientMount",)]
    assert ro["Condition"]["ArnEquals"]["s3files:AccessPointArn"] == [S3_AP]
    assert sorted(by_actions[("s3files:GetAccessPoint",)]["Resource"]) == sorted([S3_AP, S3_AP2])
    fs_arn = S3_AP.split("/access-point/")[0]
    assert by_actions[("s3files:ListMountTargets",)]["Resource"] == [fs_arn]
    efs = by_actions[("elasticfilesystem:ClientMount",)]
    assert efs["Condition"]["ArnEquals"]["elasticfilesystem:AccessPointArn"] == [EFS_AP]
    assert not any("ClientWrite" in a for s in statements for a in s["Action"]
                   if s is not rw)


# ── promotion (§6) ────────────────────────────────────────────────────────────

PROD_AP = S3_AP.replace("111122223333", "444455556677")


@pytest.fixture
def prod():
    db = SessionLocal()
    try:
        db.add(Workspace(id="prod-ws", name="Prod", account_id="444455556677",
                         region="us-west-2", tier="prod", bootstrap_status="ready"))
        db.commit()
        yield db
    finally:
        db.close()


def _prod_entry(db, storage_id, arn=PROD_AP):
    service.register(db, "prod-ws", {"id": storage_id, "access_point_arn": arn}, "operator")


SOURCE = {"name": "fs-agent", "method": "container", "system_prompt": "p",
          "filesystem": {"mounts": [{"storage_id": "datasets", "mode": "ro",
                                     "mount_path": "/mnt/datasets", "kind": "s3files",
                                     "access_point_arn": S3_AP}]}}


def test_same_id_resolves_and_restamps_the_access_point(prod):
    _prod_entry(prod, "datasets")
    refs = rm.extract_references(SOURCE)
    assert [(r.kind, r.path_text) for r in refs] == [
        ("storage", "filesystem.mounts[0].storage_id")]
    res = rm.resolve_spec(prod, copy.deepcopy(SOURCE), source_workspace_id=DEFAULT_WORKSPACE_ID,
                          target_workspace_id="prod-ws")
    assert res.complete and res.resolved[0]["via"] == "same-id"
    assert res.spec["filesystem"]["mounts"][0]["access_point_arn"] == PROD_AP
    assert SOURCE["filesystem"]["mounts"][0]["access_point_arn"] == S3_AP  # not mutated
    assert rm.assert_storage_mapped(prod, SOURCE, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                    target_workspace_id="prod-ws", project_id=None) == ["datasets"]


def test_a_mapping_row_points_elsewhere(prod):
    _prod_entry(prod, "prod-data")
    rm.upsert_mapping(prod, workspace_id="prod-ws", kind="storage", name="datasets",
                      resource_id="prod-data", note=None, updated_by="t",
                      scope=rm.SCOPE_WORKSPACE)
    prod.commit()
    res = rm.resolve_spec(prod, SOURCE, source_workspace_id=DEFAULT_WORKSPACE_ID,
                          target_workspace_id="prod-ws")
    assert res.complete and res.spec["filesystem"]["mounts"][0]["storage_id"] == "prod-data"
    with pytest.raises(NotFoundError):
        rm.upsert_mapping(prod, workspace_id="prod-ws", kind="storage", name="x",
                          resource_id="nope", note=None, updated_by="t",
                          scope=rm.SCOPE_WORKSPACE)


def test_unmapped_storage_refuses_the_release(prod):
    res = rm.resolve_spec(prod, SOURCE, source_workspace_id=DEFAULT_WORKSPACE_ID,
                          target_workspace_id="prod-ws")
    assert not res.complete and res.unmapped[0]["code"] == "storage.unmapped"
    with pytest.raises(AppError) as caught:
        rm.assert_network_mapped(prod, SOURCE, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                 target_workspace_id="prod-ws", project_id=None)
    assert caught.value.code == "storage.unmapped" and caught.value.status_code == 409
    # an entry the project may not use counts as missing
    _prod_entry(prod, "datasets")
    _project("alpha", workspaces=(DEFAULT_WORKSPACE_ID, "prod-ws"))
    with pytest.raises(AppError):
        rm.assert_storage_mapped(prod, SOURCE, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                 target_workspace_id="prod-ws", project_id="alpha")


def test_inline_access_points_are_never_promoted(prod):
    spec = {"name": "fs-agent", "filesystem": {
        "s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/raw"}]}}
    res = rm.resolve_spec(prod, spec, source_workspace_id=DEFAULT_WORKSPACE_ID,
                          target_workspace_id="prod-ws")
    assert res.unmapped[0]["code"] == "storage.inline_not_promotable"
    with pytest.raises(AppError) as caught:
        rm.assert_network_mapped(prod, spec, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                 target_workspace_id="prod-ws", project_id=None)
    assert caught.value.code == "storage.inline_not_promotable"
    # a spec without mounts is untouched by the storage half
    assert rm.assert_network_mapped(prod, {"name": "x"}, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                    target_workspace_id="prod-ws", project_id=None) is None
