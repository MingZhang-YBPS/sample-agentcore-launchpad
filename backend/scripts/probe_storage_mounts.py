#!/usr/bin/env python3
"""Live probes for BYO mounts per method (design storage-mounts §3.1) — REAL AWS.

Each probe deploys a small agent through the console API with an S3 Files (or EFS)
access point mounted and the agent in a VPC network profile, asks it to write a
marker file under the mount and read it back, and records the outcome. The
results go into `docs/architecture.md`'s mount matrix; a method whose probe fails
leaves `schemas/agent.BYO_MOUNT_METHODS` (with its reason in
`BYO_MOUNT_UNSUPPORTED`).

    P1  zip runtime (Strands): create with VPC + s3FilesAccessPoint, write + read
    P2  zip runtime: re-publish with the mount moved to another path
    P3  harness: create with the mount in agentCoreRuntimeEnvironment; the shell /
        file_operations tools reach it
    P4  harness: create PUBLIC, re-publish into the VPC profile (in place, or not)
    P5  BYOC container_image (only with --byoc-image; the image must serve the
        HTTP contract and answer the probe prompt by listing the mount)
    P8  read-only check: run with an access point whose role lacks ClientWrite
        (--expect-read-only) — the write must fail with EACCES / EROFS

P6 (POSIX identity) and P7 (a subnet AZ without a mount target) need a purpose-built
file system: run P1 against one with `--access-point` and read the error.

Run:  cd backend && uv run python scripts/probe_storage_mounts.py \\
          --access-point arn:aws:s3files:...:file-system/fs-x/access-point/ap-y \\
          --network-profile np-0123456789ab [--probe P1 --probe P3] [--keep]

Not run by the test suite; `tests/test_storage_mounts.py` only checks that the
specs it would send are valid.
"""

import argparse
import sys
import time
import uuid
from typing import Any

PREFIX = "probe-mount"
MARKER_PROMPT = (
    "Write the text {token} to the file {path}/launchpad-probe.txt, then read the "
    "file back and reply with its exact content only. If writing fails, reply with "
    "WRITE_FAILED and the error."
)
ALL_PROBES = ("P1", "P2", "P3", "P4", "P5")


def _kind(access_point: str) -> str:
    return "efs" if ":elasticfilesystem:" in access_point else "s3_files"


def mounted(access_point: str, path: str) -> dict[str, Any]:
    """`spec.filesystem` with one BYO mount (and the default session storage)."""
    return {_kind(access_point): [{"access_point_arn": access_point, "mount_path": path}]}


def probe_spec(probe: str, *, access_point: str, profile_id: str, suffix: str,
               byoc_image: str | None = None, path: str = "/mnt/probe") -> dict[str, Any]:
    """The spec a probe creates (P4 starts PUBLIC: no mount, no network)."""
    name = f"{PREFIX}-{probe.lower()}-{suffix}"
    network = {"profile_id": profile_id}
    if probe in ("P1", "P2"):
        return {"name": name, "method": "zip_runtime",
                "system_prompt": "You are a file-system probe. Follow instructions exactly.",
                "toolkits": [], "filesystem": mounted(access_point, path), "network": network}
    if probe in ("P3", "P4"):
        spec: dict[str, Any] = {
            "name": name, "method": "harness",
            "system_prompt": ("You are a file-system probe. Use your tools; follow "
                              "instructions exactly."),
            "native_tools": ["shell", "file_operations"],
        }
        if probe == "P3":
            spec.update(filesystem=mounted(access_point, path), network=network)
        return spec
    if probe == "P5":
        if not byoc_image:
            raise ValueError("P5 needs --byoc-image")
        return {"name": name, "method": "byoc",
                "byoc": {"artifact_kind": "container_image", "image_uri": byoc_image},
                "filesystem": mounted(access_point, path), "network": network}
    raise ValueError(f"unknown probe {probe}")


class Probe:
    def __init__(self, client: Any, timeout: int, keep: bool) -> None:
        self.client, self.timeout, self.keep = client, timeout, keep
        self.created: list[str] = []

    def _wait(self, agent_id: str) -> str:
        deadline = time.time() + self.timeout
        status = "deploying"
        while time.time() < deadline:
            status = self.client.get(f"/api/agents/{agent_id}").json()["status"]
            if status in ("active", "failed"):
                return status
            # Real-AWS deployment polling is intentionally paced and attempt-bounded.
            time.sleep(5)  # nosemgrep: arbitrary-sleep
        return status

    def _error(self, agent_id: str) -> str:
        agent = self.client.get(f"/api/agents/{agent_id}").json()
        deployments = agent.get("deployments") or [{}]
        return str(deployments[0].get("error") or "")

    def deploy(self, spec: dict[str, Any], agent_id: str | None = None) -> tuple[str, str]:
        if agent_id is None:
            res = self.client.post("/api/agents", json=spec)
        else:
            res = self.client.post(f"/api/agents/{agent_id}/redeploy", json=spec)
        if res.status_code >= 400:
            return agent_id or "", f"refused HTTP {res.status_code}: {res.text[:300]}"
        body = res.json()
        agent_id = agent_id or body["agent"]["id"]
        if agent_id not in self.created:
            self.created.append(agent_id)
        status = self._wait(agent_id)
        return agent_id, "active" if status == "active" else f"{status}: {self._error(agent_id)}"

    def write_read(self, agent_id: str, path: str) -> str:
        token = uuid.uuid4().hex[:12]
        import httpx
        from _e2e_client import chat_sse

        try:
            answer = chat_sse(self.client, agent_id,
                              MARKER_PROMPT.format(token=token, path=path))["text"]
        except httpx.HTTPStatusError as exc:
            return f"chat HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        except RuntimeError as exc:
            return f"chat failed: {exc}"[:300]
        if token in answer:
            return "write+read ok"
        return f"unexpected answer: {answer[:300]}"

    def cleanup(self) -> None:
        if self.keep:
            print(f"keeping {', '.join(self.created) or 'nothing'}")
            return
        for agent_id in self.created:
            self.client.delete(f"/api/agents/{agent_id}")


def run(args: argparse.Namespace) -> int:
    from _e2e_client import e2e_client

    client = e2e_client(args.base, timeout=180)
    probe = Probe(client, args.timeout, args.keep)
    suffix = uuid.uuid4().hex[:6]
    results: dict[str, str] = {}
    try:
        for name in args.probe or ALL_PROBES:
            if name == "P5" and not args.byoc_image:
                results[name] = "skipped (no --byoc-image)"
                continue
            spec = probe_spec(name, access_point=args.access_point,
                              profile_id=args.network_profile, suffix=suffix,
                              byoc_image=args.byoc_image)
            print(f"── {name}: deploying {spec['name']} ({spec['method']})")
            agent_id, outcome = probe.deploy(spec)
            if outcome != "active":
                results[name] = f"create {outcome}"
                continue
            if name == "P4":
                moved = {**spec, "filesystem": mounted(args.access_point, "/mnt/probe"),
                         "network": {"profile_id": args.network_profile}}
                _, outcome = probe.deploy(moved, agent_id)
                if outcome != "active":
                    results[name] = f"PUBLIC→VPC re-publish {outcome}"
                    continue
            if name == "P2":
                moved = {**spec, "filesystem": mounted(args.access_point, "/mnt/probe2")}
                _, outcome = probe.deploy(moved, agent_id)
                if outcome != "active":
                    results[name] = f"path change re-publish {outcome}"
                    continue
                results[name] = probe.write_read(agent_id, "/mnt/probe2")
                continue
            check = probe.write_read(agent_id, "/mnt/probe")
            if args.expect_read_only:
                check = ("read-only holds" if "WRITE_FAILED" in check
                         else f"read-only NOT enforced: {check}")
            results[name] = check
    finally:
        probe.cleanup()
    print("\n── results")
    for name, outcome in results.items():
        print(f"  {name}: {outcome}")
    return 0 if all("ok" in o or "holds" in o or o.startswith("skipped")
                    for o in results.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--access-point", required=True,
                        help="S3 Files or EFS access point ARN reachable from the profile")
    parser.add_argument("--network-profile", required=True,
                        help="a network profile id whose subnets have mount targets")
    parser.add_argument("--probe", action="append", choices=ALL_PROBES)
    parser.add_argument("--byoc-image", help="ECR image URI for P5")
    parser.add_argument("--expect-read-only", action="store_true", help="P8")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--keep", action="store_true", help="leave the probe agents deployed")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
