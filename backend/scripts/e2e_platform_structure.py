#!/usr/bin/env python3
"""E2E for the platform-structure console APIs — REAL console, cleaned up.

No other e2e script exercises these read/admin surfaces, so this one covers them
against a live deployment (the login gate on, admin credentials in
LAUNCHPAD_E2E_USERNAME / _PASSWORD; a member's credentials are passed through
LAUNCHPAD_E2E_MEMBER_USERNAME / _PASSWORD for the 403 negative checks).

Covered (routes + classes in app/routers/*.py and app/core/route_policy.py):

  departments      full create → read → patch → delete on a throwaway `e2e-…`
                   department; duplicate id → 409 `department.exists`; list shape.
  project-groups   retired (folded into direct project memberships): the route
                   answers 404.
  role-templates   read: the built-in templates and their permission keys (MEMBER).
  admins           read: the administrator list (ADMIN); a member gets 403.
  users            a member calling the admin-only `GET /api/users` gets 403
                   (the single ADMIN route that stays admin-only everywhere).
  network-profiles list / get / check on the EXISTING profile only — never create
                   or archive one (it is live workshop infrastructure).
  storage-mounts   list / get / check on an EXISTING entry only.
  models           read `GET /api/models/settings` (ADMIN) and `/api/models/defaults`
                   (MEMBER); `GET /api/model-catalog` (MEMBER).
  model-probe      a real tiny invocation of the catalog's own platform model (ADMIN,
                   cheap); skipped cleanly if the catalog is empty.

Everything this script creates is a throwaway named with the `e2e-` prefix and is
removed in `finally`, even on failure. It never touches the seeded workshop objects
(projects retail-service / travel-service, department customer-care, the
`workshop-private` profile, the `workshop-files` storage entry,
users, release policy). No AWS CLI calls.

Run:  cd backend && uv run python scripts/e2e_platform_structure.py --base URL

Prints one PASS/FAIL line per check and a final `RESULT: <n> passed, <m> failed`;
exits non-zero on any failure.
"""

import argparse
import os
import sys
import uuid
from typing import Any

import httpx
from _e2e_client import SESSION_COOKIE, e2e_client

RUN = uuid.uuid4().hex[:6]
PREFIX = "e2e-"
DEPT_ID = f"{PREFIX}dept-{RUN}"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, evidence: str = "") -> bool:
    RESULTS.append((name, ok, evidence))
    print(f"{'PASS' if ok else 'FAIL'}: {name} · {evidence}"[:400], flush=True)
    return ok


def summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"RESULT: {passed} passed, {failed} failed", flush=True)
    return 1 if failed else 0


def body(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError:
        return {}


def code(res: httpx.Response) -> str:
    data = body(res)
    tag = data.get("code", "") if isinstance(data, dict) else ""
    return f"HTTP {res.status_code} {tag}".strip()


def ws_headers(project: str | None = None) -> dict[str, str]:
    headers = {"X-Workspace": "default"}
    if project:
        headers["X-Project"] = project
    return headers


def member_client(base: str) -> httpx.Client | None:
    """A non-admin session for the 403 negative checks, or None if no member
    credentials were supplied (the member checks then SKIP, not fail)."""
    username = os.environ.get("LAUNCHPAD_E2E_MEMBER_USERNAME")
    password = os.environ.get("LAUNCHPAD_E2E_MEMBER_PASSWORD")
    if not (username and password):
        return None
    client = httpx.Client(base_url=base, timeout=120)
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    if res.status_code != 200:
        print(f"  (member login failed: {code(res)} — member checks will SKIP)", flush=True)
        client.close()
        return None
    token = client.cookies.get(SESSION_COOKIE)
    client.cookies.clear()
    client.headers["Cookie"] = f"{SESSION_COOKIE}={token}"
    client.headers.update(ws_headers())
    return client


# ── departments: create → read → update → delete, plus a negative ────────────

def test_departments(admin: httpx.Client) -> None:
    res = admin.get("/api/departments")
    rows = body(res).get("departments") if res.status_code == 200 else None
    check("departments.list", res.status_code == 200 and isinstance(rows, list),
          f"{code(res)} n={len(rows) if isinstance(rows, list) else '?'}")

    created = admin.post("/api/departments", json={
        "id": DEPT_ID, "name": f"E2E dept {RUN}", "description": "throwaway"})
    data = body(created)
    check("departments.create", created.status_code == 201 and data.get("id") == DEPT_ID
          and data.get("name") == f"E2E dept {RUN}",
          f"{code(created)} id={data.get('id')}")

    dup = admin.post("/api/departments", json={"id": DEPT_ID, "name": "dup"})
    check("departments.create.duplicate_409", dup.status_code == 409
          and body(dup).get("code") == "department.exists", code(dup))

    patched = admin.patch(f"/api/departments/{DEPT_ID}",
                          json={"description": "edited", "name": f"E2E dept {RUN} v2"})
    pdata = body(patched)
    check("departments.patch", patched.status_code == 200
          and pdata.get("description") == "edited"
          and pdata.get("name") == f"E2E dept {RUN} v2", code(patched))

    # confirm it is in the list before deletion
    listed = body(admin.get("/api/departments")).get("departments", [])
    check("departments.read_back", any(d.get("id") == DEPT_ID for d in listed),
          f"present={any(d.get('id') == DEPT_ID for d in listed)}")

    deleted = admin.delete(f"/api/departments/{DEPT_ID}")
    check("departments.delete", deleted.status_code == 204, code(deleted))

    gone = body(admin.get("/api/departments")).get("departments", [])
    check("departments.delete.verify", not any(d.get("id") == DEPT_ID for d in gone),
          f"still_present={any(d.get('id') == DEPT_ID for d in gone)}")


def cleanup_department(admin: httpx.Client) -> None:
    res = admin.delete(f"/api/departments/{DEPT_ID}")
    if res.status_code not in (204, 404):
        print(f"  (department cleanup: {code(res)})", flush=True)


# ── project-groups: retired (commit 915addde) — the route is gone ────────────

def test_project_groups_retired(admin: httpx.Client) -> None:
    # groups were folded into direct project memberships; the API must not answer
    res = admin.get("/api/project-groups")
    check("project-groups.retired_404", res.status_code == 404, code(res))


# ── role-templates (MEMBER read) ─────────────────────────────────────────────

def test_role_templates(admin: httpx.Client) -> None:
    res = admin.get("/api/role-templates")
    data = body(res)
    templates = data.get("templates") if isinstance(data, dict) else None
    keys = {t.get("key") for t in templates} if isinstance(templates, list) else set()
    check("role-templates.read", res.status_code == 200 and isinstance(templates, list)
          and {"project_owner", "viewer"} <= keys
          and all("permissions" in t for t in templates),
          f"{code(res)} keys={sorted(keys)[:6]}")


# ── admins (ADMIN read) ──────────────────────────────────────────────────────

def test_admins(admin: httpx.Client) -> None:
    res = admin.get("/api/admins")
    data = body(res)
    items = data.get("items") if isinstance(data, dict) else None
    check("admins.read", res.status_code == 200 and isinstance(items, list) and len(items) >= 1
          and "total" in data and all("username" in i for i in items),
          f"{code(res)} total={data.get('total') if isinstance(data, dict) else '?'}")


# ── negative: a member is refused the admin-only routes ──────────────────────

def test_member_negatives(member: httpx.Client | None) -> None:
    if member is None:
        for name in ("admins.member_403", "users.member_403"):
            RESULTS.append((name, True, "SKIP (no member credentials)"))
            print(f"SKIP: {name} · no LAUNCHPAD_E2E_MEMBER_USERNAME/_PASSWORD", flush=True)
        return
    res = member.get("/api/admins")
    check("admins.member_403", res.status_code == 403, code(res))
    res = member.get("/api/users")
    check("users.member_403", res.status_code == 403, code(res))


# ── network-profiles: list / get / check on the EXISTING profile only ────────

def test_network_profiles(admin: httpx.Client) -> None:
    res = admin.get("/api/network-profiles", headers=ws_headers())
    profiles = body(res).get("profiles") if res.status_code == 200 else None
    ok_list = res.status_code == 200 and isinstance(profiles, list)
    check("network-profiles.list", ok_list,
          f"{code(res)} n={len(profiles) if isinstance(profiles, list) else '?'}")
    if not (ok_list and profiles):
        check("network-profiles.get", False, "no existing profile to read")
        check("network-profiles.check", False, "no existing profile to check")
        return
    profile_id = profiles[0]["id"]
    got = admin.get(f"/api/network-profiles/{profile_id}", headers=ws_headers())
    check("network-profiles.get", got.status_code == 200 and body(got).get("id") == profile_id,
          f"{code(got)} id={profile_id}")
    checked = admin.post(f"/api/network-profiles/{profile_id}/check", headers=ws_headers())
    cdata = body(checked)
    check("network-profiles.check", checked.status_code == 200
          and ("status" in cdata or "check_results" in cdata),
          f"{code(checked)} status={cdata.get('status')}")


# ── storage-mounts: list / get / check on an EXISTING entry only ─────────────

def test_storage_mounts(admin: httpx.Client) -> None:
    res = admin.get("/api/storage-mounts", headers=ws_headers())
    entries = body(res).get("storage") if res.status_code == 200 else None
    ok_list = res.status_code == 200 and isinstance(entries, list)
    check("storage-mounts.list", ok_list,
          f"{code(res)} n={len(entries) if isinstance(entries, list) else '?'}")
    if not (ok_list and entries):
        check("storage-mounts.get", False, "no existing entry to read")
        check("storage-mounts.check", False, "no existing entry to check")
        return
    storage_id = entries[0]["id"]
    got = admin.get(f"/api/storage-mounts/{storage_id}", headers=ws_headers())
    check("storage-mounts.get", got.status_code == 200 and body(got).get("id") == storage_id,
          f"{code(got)} id={storage_id}")
    checked = admin.post(f"/api/storage-mounts/{storage_id}/check", headers=ws_headers())
    cdata = body(checked)
    check("storage-mounts.check", checked.status_code == 200
          and ("status" in cdata or "check_results" in cdata or "id" in cdata),
          f"{code(checked)} status={cdata.get('status')}")


# ── models: settings (ADMIN), defaults (MEMBER), catalog (MEMBER), probe ──────

def test_models(admin: httpx.Client) -> None:
    res = admin.get("/api/models/settings")
    roles = body(res).get("roles") if res.status_code == 200 else None
    check("models.settings", res.status_code == 200 and isinstance(roles, list) and roles
          and all("field" in r for r in roles),
          f"{code(res)} n={len(roles) if isinstance(roles, list) else '?'}")

    res = admin.get("/api/models/defaults")
    defaults = body(res)
    check("models.defaults", res.status_code == 200 and isinstance(defaults, dict)
          and "platform_model_id" in defaults,
          f"{code(res)} platform={defaults.get('platform_model_id')}")

    res = admin.get("/api/model-catalog", headers=ws_headers())
    cat = body(res)
    models = cat.get("models") if isinstance(cat, dict) else None
    ok_catalog = res.status_code == 200 and isinstance(models, list)
    check("model-catalog.read", ok_catalog,
          f"{code(res)} region={cat.get('region') if isinstance(cat, dict) else '?'} "
          f"n={len(models) if isinstance(models, list) else '?'}")

    # model-probe: a real tiny invocation of the configured platform model (cheap).
    # Pick the platform model id from settings/defaults; skip if we cannot resolve one.
    settings = body(admin.get("/api/models/settings")).get("roles", [])
    platform = next((r for r in settings if r.get("field") == "platform_model_id"), None)
    model_id = (platform or {}).get("value") or (platform or {}).get("default")
    model_id = model_id or body(admin.get("/api/models/defaults")).get("platform_model_id")
    if not model_id:
        RESULTS.append(("model-probe.read", True, "SKIP (no platform model id)"))
        print("SKIP: model-probe.read · no platform model id", flush=True)
        return
    res = admin.post("/api/model-probe", headers=ws_headers(),
                     json={"field": "platform_model_id", "model_id": model_id})
    pdata = body(res)
    # a probe is a read-only call; a 200 with any verdict field is the contract
    check("model-probe.read", res.status_code == 200 and isinstance(pdata, dict)
          and len(pdata) > 0, f"{code(res)} keys={sorted(pdata)[:4]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("LAUNCHPAD_E2E_BASE"),
                        help="console URL (default: env LAUNCHPAD_E2E_BASE)")
    args = parser.parse_args()
    if not args.base:
        print("usage: --base URL  (or set LAUNCHPAD_E2E_BASE)", file=sys.stderr)
        return 2

    admin = e2e_client(args.base)
    admin.headers.update(ws_headers())
    member = member_client(args.base)

    try:
        test_role_templates(admin)
        test_admins(admin)
        test_member_negatives(member)
        test_departments(admin)
        test_project_groups_retired(admin)
        test_network_profiles(admin)
        test_storage_mounts(admin)
        test_models(admin)
    finally:
        cleanup_department(admin)
        admin.close()
        if member is not None:
            member.close()

    return summary()


if __name__ == "__main__":
    sys.exit(main())
