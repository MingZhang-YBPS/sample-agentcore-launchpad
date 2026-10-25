"""The workshop stack seeder: what it reads from the stack and what it registers.

AWS and the console are both stubbed: discovery gets canned CLI answers, seeding a
recording fake of the console API.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "scripts" / "seed_workshop_env.py"


def _load():
    spec = importlib.util.spec_from_file_location("seed_workshop_env", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sw = _load()

PHYSICAL = {
    "WorkshopVpc": "vpc-0aaa",
    "PrivateSubnetA": "subnet-0a",
    "PrivateSubnetB": "subnet-0b",
    "AgentSecurityGroup": "sg-0agent",
    "FilesAccessPoint": "fsap-0files",
}
FILES_ARN = "arn:aws:s3files:us-east-1:111122223333:file-system/fs-0f/access-point/fsap-0files"
EFS_ARN = "arn:aws:elasticfilesystem:us-east-1:111122223333:access-point/fsap-0efs"


def fake_aws(physical: dict[str, str]):
    calls: list[list[str]] = []

    def run(args: list[str]):
        calls.append(args)
        if args[:2] == ["cloudformation", "describe-stack-resource"]:
            pid = physical.get(args[-1])
            return {"StackResourceDetail": {"PhysicalResourceId": pid}} if pid else None
        if args[:2] == ["sts", "get-caller-identity"]:
            return {"Account": "111122223333"}
        if args[:2] == ["s3files", "get-access-point"]:
            return {"accessPointArn": FILES_ARN}
        if args[:2] == ["efs", "describe-access-points"]:
            return {"AccessPoints": [{"AccessPointArn": EFS_ARN}]}
        raise AssertionError(args)

    run.calls = calls
    return run


class FakeConsole:
    def __init__(self, profiles=None, storage=None, bound_profiles=None, default_profile=None):
        self.profiles = list(profiles or [])
        self.storage = list(storage or [])
        self.bound_profiles = list(bound_profiles or [])
        self.default_profile = default_profile
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if (method, path) == ("GET", "/api/network-profiles"):
            return {"profiles": self.profiles}
        if (method, path) == ("POST", "/api/network-profiles"):
            row = {"id": "np-1", **body}
            self.profiles.append(row)
            return row
        if method == "POST" and path.endswith("/check"):
            return {"status": "ok"}
        if (method, path) == ("GET", "/api/storage-mounts"):
            return {"entries": self.storage}
        if (method, path) == ("POST", "/api/storage-mounts"):
            self.storage.append({"id": body["id"]})
            return body
        if (method, path) == ("GET", "/api/projects"):
            return {"projects": [{"id": "proj-x", "is_default": False},
                                 {"id": "proj-default", "is_default": True}]}
        if (method, path) == ("GET", "/api/network-profiles/projects/proj-default"):
            return {"profiles": [{"id": p} for p in self.bound_profiles],
                    "default_profile_id": self.default_profile}
        if (method, path) == ("GET", "/api/storage-mounts/projects/proj-default"):
            return {"entries": []}
        if method == "PUT":
            return body
        raise AssertionError((method, path))

    def put(self, path):
        return next(body for m, p, body in self.calls if m == "PUT" and p == path)


def test_discover_reads_ids_from_the_stack_and_storage_arns_from_the_service():
    found = sw.discover(fake_aws(PHYSICAL), "ws-stack", "us-east-1")
    assert found == {
        "vpc_id": "vpc-0aaa",
        "subnet_ids": ["subnet-0a", "subnet-0b"],
        "security_group_ids": ["sg-0agent"],
        "storage": [{"id": "workshop-files", "arn": FILES_ARN, "kind": "s3files"}],
    }


def test_discover_adds_efs_when_the_stack_created_it():
    found = sw.discover(fake_aws({**PHYSICAL, "EfsAccessPoint": "fsap-0efs"}), "s", "us-east-1")
    assert [s["id"] for s in found["storage"]] == ["workshop-files", "workshop-efs"]
    assert found["storage"][1]["arn"] == EFS_ARN


def test_discover_returns_none_for_an_existing_vpc_stack():
    assert sw.discover(fake_aws({"FilesAccessPoint": "fsap-0files"}), "s", "us-east-1") is None


def test_seed_registers_profile_storage_and_binds_the_default_project():
    console = FakeConsole()
    found = sw.discover(fake_aws(PHYSICAL), "s", "us-east-1")
    out = sw.seed(console, found, log=lambda _m: None)

    created = next(b for m, p, b in console.calls
                   if (m, p) == ("POST", "/api/network-profiles"))
    assert created["internet"] == "nat" and created["subnet_ids"] == ["subnet-0a", "subnet-0b"]
    storage = next(b for m, p, b in console.calls if (m, p) == ("POST", "/api/storage-mounts"))
    assert storage["access_point_arn"] == FILES_ARN and storage["network_profile_id"] == "np-1"
    assert console.put("/api/network-profiles/projects/proj-default") == {
        "profile_ids": ["np-1"], "default_profile_id": "np-1"}
    assert console.put("/api/storage-mounts/projects/proj-default") == {
        "storage_ids": ["workshop-files"]}
    assert out == {"profile_id": "np-1", "storage_ids": ["workshop-files"],
                   "project_id": "proj-default"}


def test_seed_is_idempotent_and_keeps_an_admin_chosen_default():
    console = FakeConsole(profiles=[{"id": "np-1", "name": "workshop-private"}],
                          storage=[{"id": "workshop-files"}],
                          bound_profiles=["np-other"], default_profile="np-other")
    found = sw.discover(fake_aws(PHYSICAL), "s", "us-east-1")
    sw.seed(console, found, log=lambda _m: None)

    posts = [p for m, p, _b in console.calls if m == "POST" and not p.endswith("/check")]
    assert posts == []
    assert console.put("/api/network-profiles/projects/proj-default") == {
        "profile_ids": ["np-1", "np-other"], "default_profile_id": "np-other"}


def test_main_without_password_refuses(monkeypatch, capsys):
    monkeypatch.delenv("LAUNCHPAD_SEED_PASSWORD", raising=False)
    assert sw.main(["--stack", "s", "--region", "us-east-1"]) == 2
    assert "LAUNCHPAD_SEED_PASSWORD" in capsys.readouterr().err
