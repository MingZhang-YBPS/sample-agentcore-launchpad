#!/usr/bin/env python3
"""E2E for the console platform-ops surfaces no other e2e script exercises — SSO, SCIM,
system update, system agents, demos, announcements — against a LIVE console.

Covers, in order (routers {sso,scim,system_update,system_agents,announcements,tools},
core/route_policy):

  1. sso      list providers (the admin list's `acs_url_template` starts with the
              console base); create a DISABLED SAML and a DISABLED OIDC provider named
              `e2e-…`; get each; patch (rename); put / get the SAML one's group
              mappings; read its logins; POST /test (a clean failure / result — no real
              IdP); rotate the SAML SP key; mint a SCIM token on the SAML provider; call
              the SCIM API (/ServiceProviderConfig and GET /Users) with that bearer
              token; delete the SCIM token and confirm it is then refused 401; delete
              both providers. Never enables a provider, never touches
              /api/admin/sso/settings (it can hide local login).
  2. system   GET /api/system/update and POST /api/system/update/check only. Never
              prepare / schedule / rollback / cancel.
  3. presets  GET /api/system-agents (list only; never install / repair / uninstall).
  4. notices  GET /api/demos/browser/options and GET /api/announcements; then an
              announcement create → it shows in /manage → delete → it is gone.
  5. negative a member account (developer) is 403 on the admin SSO list and on the
              system-update routes.

Run:  cd backend && uv run python scripts/e2e_platform_ops.py --base URL

Reads admin credentials from LAUNCHPAD_E2E_USERNAME / _PASSWORD (via _e2e_client) and
a member's from LAUNCHPAD_E2E_MEMBER_USERNAME / _PASSWORD (the 403 negative checks SKIP
without them). Everything it creates is named `e2e-…` and removed in `finally`. It
never enables an identity provider, never installs a preset and never runs a real
system update. Prints one PASS/FAIL line per
check and a final `RESULT: <n> passed, <m> failed`; exits non-zero on any failure.
"""

import argparse
import os
import sys
import uuid
from typing import Any

import httpx
from _e2e_client import e2e_client

RUN = uuid.uuid4().hex[:6]
PREFIX = "e2e-"
SAML_ID = f"{PREFIX}saml-{RUN}"
OIDC_ID = f"{PREFIX}oidc-{RUN}"

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, evidence: str = "") -> bool:
    RESULTS.append((name, "PASS" if ok else "FAIL", evidence))
    print(f"── {name}: {'PASS' if ok else 'FAIL'} · {evidence}"[:400], flush=True)
    return ok


def summary() -> int:
    passed = sum(v == "PASS" for _, v, _ in RESULTS)
    failed = sum(v == "FAIL" for _, v, _ in RESULTS)
    print("\n═══ summary ═══")
    for name, verdict, evidence in RESULTS:
        if verdict == "FAIL":
            print(f"FAIL  {name:<40} {evidence[:120]}")
    print(f"\nRESULT: {passed} passed, {failed} failed")
    return 1 if failed else 0


def skip(name: str, why: str) -> None:
    RESULTS.append((name, "SKIP", why))
    print(f"── {name}: SKIP · {why}", flush=True)


def body(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError:
        return {}


def code(res: httpx.Response) -> str:
    data = body(res)
    detail = data.get("code", "") if isinstance(data, dict) else ""
    return f"HTTP {res.status_code} {detail}".strip()


def dev_client(base: str) -> httpx.Client | None:
    """A logged-in member client for the 403 checks, or None when no member creds are
    configured (the negative checks then SKIP, not fail)."""
    username = os.environ.get("LAUNCHPAD_E2E_MEMBER_USERNAME")
    password = os.environ.get("LAUNCHPAD_E2E_MEMBER_PASSWORD")
    if not (username and password):
        return None
    client = httpx.Client(base_url=base, timeout=120)
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    if res.status_code != 200:
        print(f"     member login failed: {code(res)}", flush=True)
        return None
    token = res.cookies.get("launchpad_session") or client.cookies.get("launchpad_session")
    if not token:
        return None
    client.cookies.clear()
    client.headers["Cookie"] = f"launchpad_session={token}"
    return client


# ── 1. SSO ──────────────────────────────────────────────────────────────────────


def sso(admin: httpx.Client, base: str) -> None:
    listing = body(admin.get("/api/admin/sso/providers"))
    acs_template = listing.get("acs_url_template") or ""
    base_root = base.rstrip("/")
    check("sso.list_providers",
          "items" in listing and isinstance(listing["items"], list)
          and acs_template.startswith(base_root),
          f"items={len(listing.get('items', []))} acs_url_template={acs_template!r}")

    # --- create a DISABLED SAML provider ---
    res = admin.post("/api/admin/sso/providers", json={
        "id": SAML_ID, "name": f"E2E SAML {RUN}", "preset": "saml"})
    saml = body(res)
    check("sso.create_saml", res.status_code == 201 and saml.get("kind") == "saml"
          and saml.get("enabled") is False, f"{code(res)} enabled={saml.get('enabled')}")

    # --- create a DISABLED OIDC provider ---
    res = admin.post("/api/admin/sso/providers", json={
        "id": OIDC_ID, "name": f"E2E OIDC {RUN}", "preset": "oidc"})
    oidc = body(res)
    check("sso.create_oidc", res.status_code == 201 and oidc.get("kind") == "oidc"
          and oidc.get("enabled") is False, f"{code(res)} enabled={oidc.get('enabled')}")

    # --- get both ---
    for name, pid, kind in (("saml", SAML_ID, "saml"), ("oidc", OIDC_ID, "oidc")):
        res = admin.get(f"/api/admin/sso/providers/{pid}")
        check(f"sso.get_{name}", res.status_code == 200 and body(res).get("kind") == kind,
              code(res))

    # --- patch (rename) the SAML provider ---
    res = admin.patch(f"/api/admin/sso/providers/{SAML_ID}",
                      json={"name": f"E2E SAML {RUN} renamed"})
    check("sso.patch_saml", res.status_code == 200
          and body(res).get("name") == f"E2E SAML {RUN} renamed", code(res))

    # --- group mappings: put then get back (kind=role → the account's global role) ---
    mapping = [{"group": "engineering", "kind": "role", "role_key": "member",
                "target_id": ""}]
    res = admin.put(f"/api/admin/sso/providers/{SAML_ID}/mappings",
                    json={"mappings": mapping})
    put_ok = res.status_code == 200
    check("sso.put_mappings", put_ok, code(res))
    res = admin.get(f"/api/admin/sso/providers/{SAML_ID}/mappings")
    got = body(res).get("mappings") or []
    check("sso.get_mappings", res.status_code == 200 and len(got) == 1
          and got[0].get("group") == "engineering",
          f"{code(res)} mappings={got}")

    # --- recent logins (empty is fine: it was never signed into) ---
    res = admin.get(f"/api/admin/sso/providers/{SAML_ID}/logins")
    check("sso.logins", res.status_code == 200 and "items" in body(res),
          f"{code(res)} items={len(body(res).get('items', []))}")

    # --- test: no real IdP, so a clean failure or result, never a 5xx ---
    res = admin.post(f"/api/admin/sso/providers/{SAML_ID}/test")
    data = body(res)
    check("sso.test_saml", res.status_code == 200 and "ok" in data,
          f"{code(res)} ok={data.get('ok')} reason={(data.get('reason') or data.get('error'))}")

    # --- rotate the SAML SP key ---
    before = body(admin.get(f"/api/admin/sso/providers/{SAML_ID}"))
    before_cert = (before.get("saml") or {}).get("sp_cert")
    res = admin.post(f"/api/admin/sso/providers/{SAML_ID}/saml/sp-key")
    after_cert = (body(res).get("saml") or {}).get("sp_cert")
    check("sso.rotate_sp_key", res.status_code == 200 and bool(after_cert)
          and after_cert != before_cert, f"{code(res)} cert_changed={after_cert != before_cert}")

    # --- SCIM token lifecycle on the SAML provider ---
    _scim(admin, base)

    # --- delete both providers (neither has linked accounts) ---
    for name, pid in (("saml", SAML_ID), ("oidc", OIDC_ID)):
        res = admin.delete(f"/api/admin/sso/providers/{pid}")
        check(f"sso.delete_{name}", res.status_code == 200 and body(res).get("ok") is True,
              code(res))


def _scim(admin: httpx.Client, base: str) -> None:
    res = admin.post(f"/api/admin/sso/providers/{SAML_ID}/scim/token")
    data = body(res)
    token = data.get("token")
    scim_base = (data.get("scim") or {}).get("base_url") or ""
    check("sso.scim_token_create",
          res.status_code == 200 and bool(token) and token.startswith("lpscim_")
          and scim_base.startswith(base.rstrip("/")),
          f"{code(res)} base_url={scim_base!r} enabled={(data.get('scim') or {}).get('enabled')}")
    if not token:
        return

    # The SCIM API authenticates by the provider's bearer token alone (no console
    # session, no workspace). Use a separate client so no session cookie rides along.
    scim = httpx.Client(base_url=base, timeout=120,
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/scim+json"})
    try:
        res = scim.get("/api/scim/v2/ServiceProviderConfig")
        spc = body(res)
        check("scim.service_provider_config",
              res.status_code == 200
              and "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
              in (spc.get("schemas") or []),
              f"{code(res)} schemas={spc.get('schemas')}")

        res = scim.get("/api/scim/v2/Users")
        users = body(res)
        check("scim.list_users",
              res.status_code == 200
              and "urn:ietf:params:scim:api:messages:2.0:ListResponse"
              in (users.get("schemas") or [])
              and "totalResults" in users,
              f"{code(res)} totalResults={users.get('totalResults')}")

        # delete the token, then the same bearer must be refused
        res = admin.delete(f"/api/admin/sso/providers/{SAML_ID}/scim/token")
        check("sso.scim_token_delete",
              res.status_code == 200
              and (body(res).get("scim") or {}).get("enabled") is False, code(res))

        res = scim.get("/api/scim/v2/Users")
        check("scim.refused_after_delete", res.status_code == 401,
              f"{code(res)} (expected 401)")
    finally:
        scim.close()


# ── 2. system update ──────────────────────────────────────────────────────────────


def system_update(admin: httpx.Client) -> None:
    res = admin.get("/api/system/update")
    data = body(res)
    check("system.update_status", res.status_code == 200 and isinstance(data, dict)
          and bool(data), f"{code(res)} keys={list(data)[:8]}")

    res = admin.post("/api/system/update/check")
    data = body(res)
    check("system.update_check", res.status_code == 200 and isinstance(data, dict),
          f"{code(res)} keys={list(data)[:8]}")


# ── 3. system agents ────────────────────────────────────────────────────────────


def system_agents(admin: httpx.Client) -> None:
    res = admin.get("/api/system-agents")
    data = body(res)
    check("system_agents.list",
          res.status_code == 200 and isinstance(data.get("presets"), list),
          f"{code(res)} presets={len(data.get('presets', []))}")


# ── 4. demos + announcements ────────────────────────────────────────────────────


def notices(admin: httpx.Client) -> None:
    res = admin.get("/api/demos/browser/options")
    check("demos.browser_options", res.status_code == 200 and isinstance(body(res), dict),
          code(res))

    res = admin.get("/api/announcements")
    data = body(res)
    check("announcements.list",
          res.status_code == 200 and isinstance(data.get("announcements"), list),
          f"{code(res)} total={data.get('total')}")

    # create → appears in /manage → delete → gone
    title = f"{PREFIX}announce-{RUN}"
    created = None
    res = admin.post("/api/announcements",
                     json={"title": title, "body": f"E2E platform-ops {RUN}"})
    data = body(res)
    created_ok = res.status_code == 201 and bool(data.get("id"))
    check("announcements.create", created_ok,
          f"{code(res)} id={data.get('id')}")
    if not created_ok:
        return
    created = data["id"]
    revision = data.get("revision", 1)
    try:
        manage = body(admin.get("/api/announcements/manage"))
        ids = {row.get("id") for row in manage.get("announcements", [])}
        check("announcements.shows_in_manage", created in ids,
              f"manage lists {len(ids)} items")
    finally:
        res = admin.delete(f"/api/announcements/{created}",
                           params={"expected_revision": revision})
        deleted_ok = res.status_code == 200 and body(res).get("deleted") is True
        check("announcements.delete", deleted_ok, code(res))
        if deleted_ok:
            manage = body(admin.get("/api/announcements/manage"))
            ids = {row.get("id") for row in manage.get("announcements", [])}
            check("announcements.gone_after_delete", created not in ids,
                  f"manage lists {len(ids)} items")


# ── 5. negative: a member is refused the admin routes ────────────────────────────


def negative(dev: httpx.Client | None) -> None:
    if dev is None:
        skip("negative.member_sso_403",
             "LAUNCHPAD_E2E_MEMBER_USERNAME/_PASSWORD not set — member 403 checks skipped")
        skip("negative.member_system_update_403", "no member credentials")
        skip("negative.member_system_check_403", "no member credentials")
        return
    res = dev.get("/api/admin/sso/providers")
    check("negative.member_sso_403", res.status_code == 403, f"{code(res)} (expected 403)")
    res = dev.get("/api/system/update")
    check("negative.member_system_update_403", res.status_code == 403,
          f"{code(res)} (expected 403)")
    res = dev.post("/api/system/update/check")
    check("negative.member_system_check_403", res.status_code == 403,
          f"{code(res)} (expected 403)")


def _cleanup(admin: httpx.Client) -> None:
    # defensive: remove anything an interrupted run left behind
    for pid in (SAML_ID, OIDC_ID):
        try:
            if admin.get(f"/api/admin/sso/providers/{pid}").status_code == 200:
                admin.delete(f"/api/admin/sso/providers/{pid}")
        except httpx.HTTPError:
            pass
    try:
        manage = body(admin.get("/api/announcements/manage"))
        for row in manage.get("announcements", []):
            content = row.get("content") or {}
            if str(content.get("title", "")).startswith(f"{PREFIX}announce-"):
                admin.delete(f"/api/announcements/{row['id']}",
                             params={"expected_revision": row.get("revision", 1)})
    except httpx.HTTPError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()

    admin = e2e_client(args.base, timeout=120)
    dev = dev_client(args.base)
    print(f"── base {args.base} · run {RUN}", flush=True)
    try:
        sso(admin, args.base)
        system_update(admin)
        system_agents(admin)
        notices(admin)
        negative(dev)
    except Exception as exc:  # noqa: BLE001 — report, then clean up
        check("unexpected", False, f"{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        try:
            _cleanup(admin)
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup error: {type(exc).__name__}: {exc}", flush=True)
        if dev is not None:
            dev.close()
        admin.close()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
