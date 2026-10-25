"""Seed a workshop stack's network profile and storage entries into its console.

Run by the workshop CloudFormation template (launchpad-multiuser.yaml) at the end
of bootstrap, as root on the console instance, after the accounts are seeded:

    LAUNCHPAD_SEED_PASSWORD=... uv run --no-sync python scripts/seed_workshop_env.py \
        --stack <stack name> --region <region>

What it registers, so participants pick a template instead of typing VPC ids:

* network profile ``workshop-private`` -- the stack's two private subnets and agent
  security group, ``internet = nat`` (AWS services through the VPC endpoints, the
  NAT gateway only for the public internet), then its preflight;
* storage ``workshop-files`` -- the S3 Files access point (always), and
  ``workshop-efs`` when the stack was deployed with CreateEfs=Enabled;
* both bound to the default project, with ``workshop-private`` as its default
  network profile.

The ids come from the stack's own resources (``aws cloudformation
describe-stack-resource``, the instance role may read its stack), never from
arguments, so the script cannot register a network it was not deployed with. A
stack deployed into an existing VPC has none of these resources: the script then
prints why and exits 0 -- the organiser registers that network by hand.

Re-running is safe: an entry that already exists is left as it is. Only the AWS
CLI and the console's HTTP API are used (no boto3 here: every boto3 client lives
in app/services/aws_clients.py).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

BASE = "http://127.0.0.1:8000"
PROFILE_ID_NAME = "workshop-private"
FILES_ID = "workshop-files"
EFS_ID = "workshop-efs"

# logical id -> what it is; the network ones are required, EFS is optional
NETWORK_RESOURCES = ("WorkshopVpc", "PrivateSubnetA", "PrivateSubnetB", "AgentSecurityGroup")
FILES_RESOURCE = "FilesAccessPoint"
EFS_RESOURCE = "EfsAccessPoint"

Aws = Callable[[list[str]], Any]
Http = Callable[[str, str, Any], Any]


def aws_cli(region: str) -> Aws:
    """`aws <args> --region R --output json`, parsed; None when the call fails."""

    def run(args: list[str]) -> Any:
        proc = subprocess.run(
            ["aws", *args, "--region", region, "--output", "json"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout or "null")

    return run


def physical_id(aws: Aws, stack: str, logical_id: str) -> str | None:
    out = aws(["cloudformation", "describe-stack-resource", "--stack-name", stack,
               "--logical-resource-id", logical_id])
    detail = (out or {}).get("StackResourceDetail") or {}
    return detail.get("PhysicalResourceId") or None


def access_point_arn(aws: Aws, kind: str, access_point_id: str, region: str,
                     account: str) -> str | None:
    """The ARN the storage catalog wants, read back from the service."""
    if access_point_id.startswith("arn:"):
        return access_point_id
    if kind == "efs":
        out = aws(["efs", "describe-access-points", "--access-point-id", access_point_id])
        points = (out or {}).get("AccessPoints") or []
        return points[0].get("AccessPointArn") if points else None
    out = aws(["s3files", "get-access-point", "--access-point-id", access_point_id])
    return (out or {}).get("accessPointArn") or (out or {}).get("AccessPointArn")


def discover(aws: Aws, stack: str, region: str) -> dict[str, Any] | None:
    """The stack's network and storage ids; None when it built no network."""
    ids = {name: physical_id(aws, stack, name) for name in NETWORK_RESOURCES}
    if not all(ids.values()):
        return None
    ident = aws(["sts", "get-caller-identity"]) or {}
    account = str(ident.get("Account") or "")
    storage: list[dict[str, str]] = []
    for storage_id, logical, kind in ((FILES_ID, FILES_RESOURCE, "s3files"),
                                      (EFS_ID, EFS_RESOURCE, "efs")):
        point = physical_id(aws, stack, logical)
        arn = access_point_arn(aws, kind, point, region, account) if point else None
        if arn:
            storage.append({"id": storage_id, "arn": arn, "kind": kind})
    return {
        "vpc_id": ids["WorkshopVpc"],
        "subnet_ids": [ids["PrivateSubnetA"], ids["PrivateSubnetB"]],
        "security_group_ids": [ids["AgentSecurityGroup"]],
        "storage": storage,
    }


def _items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    for key in keys:
        if isinstance((payload or {}).get(key), list):
            return payload[key]
    return []


def seed(http: Http, found: dict[str, Any], log: Callable[[str], None] = print) -> dict[str, Any]:
    """Register the profile and storage, bind both to the default project.
    Idempotent: existing entries are reused, never modified."""
    profiles = _items(http("GET", "/api/network-profiles", None), "profiles", "items")
    profile = next((p for p in profiles if p.get("name") == PROFILE_ID_NAME), None)
    if profile is None:
        profile = http("POST", "/api/network-profiles", {
            "name": PROFILE_ID_NAME, "vpc_id": found["vpc_id"],
            "subnet_ids": found["subnet_ids"],
            "security_group_ids": found["security_group_ids"],
            "purpose": "agents", "internet": "nat",
        })
        log(f"network profile created: {profile['id']}")
    else:
        log(f"network profile exists: {profile['id']}")
    check = http("POST", f"/api/network-profiles/{profile['id']}/check", None) or {}
    log(f"network profile preflight: {check.get('status', 'unknown')}")

    existing = {e.get("id") for e in _items(http("GET", "/api/storage-mounts", None),
                                            "entries", "items", "storage")}
    storage_ids: list[str] = []
    for item in found["storage"]:
        if item["id"] not in existing:
            http("POST", "/api/storage-mounts", {
                "id": item["id"], "name": item["id"], "access_point_arn": item["arn"],
                "max_mode": "rw", "data_class": "scratch",
                "network_profile_id": profile["id"], "check": True,
            })
            log(f"storage registered: {item['id']} ({item['kind']})")
        else:
            log(f"storage exists: {item['id']}")
        storage_ids.append(item["id"])

    projects = _items(http("GET", "/api/projects", None), "projects")
    default = next((p for p in projects if p.get("is_default")), None)
    if default is None:
        log("no default project; bindings skipped")
        return {"profile_id": profile["id"], "storage_ids": storage_ids, "project_id": None}
    pid = default["id"]
    bound = http("GET", f"/api/network-profiles/projects/{pid}", None) or {}
    profile_ids = sorted({*(p["id"] for p in _items(bound, "profiles")), profile["id"]})
    http("PUT", f"/api/network-profiles/projects/{pid}",
         {"profile_ids": profile_ids,
          "default_profile_id": bound.get("default_profile_id") or profile["id"]})
    have = http("GET", f"/api/storage-mounts/projects/{pid}", None) or {}
    current = [e.get("id") for e in _items(have, "entries", "items", "storage")]
    http("PUT", f"/api/storage-mounts/projects/{pid}",
         {"storage_ids": sorted({*filter(None, current), *storage_ids})})
    log(f"bound to project {pid}: profile {profile['id']}, storage {storage_ids}")
    return {"profile_id": profile["id"], "storage_ids": storage_ids, "project_id": pid}


def console_http(password: str, base: str = BASE, workspace: str = "default") -> Http:
    """Admin session against the local console (same calls the console makes)."""

    def call(method: str, path: str, body: Any, cookie: str | None = None) -> Any:
        headers = {"Content-Type": "application/json", "X-Workspace": workspace}
        if cookie:
            headers["Cookie"] = cookie
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - loopback only
            raw = resp.read()
            return resp.headers, (json.loads(raw) if raw else None)

    head, _ = call("POST", "/api/auth/login", {"username": "admin", "password": password})
    cookie = next(c.split(";")[0] for c in head.get_all("Set-Cookie")
                  if c.startswith("launchpad_session="))
    return lambda method, path, body: call(method, path, body, cookie)[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)
    password = os.environ.get("LAUNCHPAD_SEED_PASSWORD", "")
    if not password:
        print("LAUNCHPAD_SEED_PASSWORD is not set", file=sys.stderr)
        return 2
    found = discover(aws_cli(args.region), args.stack, args.region)
    if found is None:
        print("stack built no private network (existing VPC): nothing to seed; register "
              "the network profile and storage by hand (organiser appendix)")
        return 0
    try:
        seed(console_http(password), found)
    except urllib.error.HTTPError as exc:
        print(f"seed failed: {exc.code} {exc.read()[:300]!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
