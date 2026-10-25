#!/usr/bin/env python3
"""Browser pass over the V3 cost pages as a department administrator, then the
organization-level department editor as an administrator — headless Chromium.

What it checks (design: docs/architecture.md, "Budgets: projects, departments and the
three cost views"):

  department admin  tabs are Project budget + Department only (no Spend / Alerts /
                    Platform), the rail has the department entry and no platform one,
                    `?view=platform` does not open; the Project budget tab points to
                    the Department tab; the department page shows the ceiling, has no
                    department editor and folds archived projects read-only;
                    allocating through the page saves (read back over the API); an
                    allocation that does not fit is held in the page and the exact
                    remainder saves; lowering in a full department saves; the overrun
                    switch is locked until the department allows it, then gives the
                    80 · 105 · 110 % lines; clearing a budget and its switch saves.
  administrator     the department editor refuses a ceiling below what is allocated
                    and saves a higher one; the platform page shows it.

Fixtures (kept between runs, like the suite's `e2e-lab`): department
`e2e-costs-dept` with projects `e2e-costs-a` / `e2e-costs-b`, created when missing,
unarchived at the start and archived again at the end. The department administrator is
a throwaway account `e2e-dadmin-<run>` with a generated password, deleted at the end.
Every budget and switch it sets is cleared at the end, also on failure. No AWS call.

Run:  cd backend && uv run python scripts/e2e_costs_dept_admin_browser.py \\
        --ui https://<console> [--out /tmp/costs-shots]
Needs LAUNCHPAD_E2E_USERNAME / _PASSWORD (an administrator) when the login gate is on.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from _e2e_client import E2E_WORKSPACE, PASSWORD_VARS, USERNAME_VARS, _first_env, e2e_client
from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright

DEPT, DEPT_NAME = "e2e-costs-dept", "E2E costs dept"
P1, P1_NAME = "e2e-costs-a", "E2E costs A"
P2, P2_NAME = "e2e-costs-b", "E2E costs B"
STORE = {"launchpad_v3_theme": "dark", "i18nextLng": "en", "launchpad_ui_version": "v3",
         "launchpad_v3_tour_done": "1"}


def must(res: httpx.Response, what: str, *ok: int) -> dict:
    if res.status_code not in (ok or (200,)):
        raise SystemExit(f"setup: {what} returned HTTP {res.status_code} {res.text[:200]}")
    return res.json() if res.content else {}


def ensure_fixtures(admin: httpx.Client) -> None:
    depts = must(admin.get("/api/departments"), "GET /api/departments").get("departments", [])
    dept = next((d for d in depts if d.get("id") == DEPT), None)
    if dept is None:
        must(admin.post("/api/departments", json={
            "id": DEPT, "name": DEPT_NAME,
            "description": "Department of the cost-page browser e2e (kept between runs)"}),
            "create the department", 201)
    elif dept.get("archived"):
        must(admin.post(f"/api/departments/{DEPT}/unarchive"), "unarchive the department")
    for pid, name in ((P1, P1_NAME), (P2, P2_NAME)):
        res = admin.get(f"/api/projects/{pid}")
        if res.status_code == 404:
            must(admin.post("/api/projects", json={
                "id": pid, "name": name, "department_id": DEPT, "workspaces": [E2E_WORKSPACE],
                "description": "Cost-page browser e2e fixture (kept between runs)"}),
                f"create {pid}", 201)
            continue
        project = must(res, f"GET {pid}")
        if project.get("status") == "archived":
            must(admin.post(f"/api/projects/{pid}/unarchive"), f"unarchive {pid}")
        if project.get("department_id") != DEPT:
            must(admin.put(f"/api/projects/{pid}/budget", json={"monthly_budget_usd": None}),
                 f"clear {pid}'s budget before the move")
            must(admin.patch(f"/api/projects/{pid}", json={"department_id": DEPT}),
                 f"move {pid} into {DEPT}")


def reset_budgets(admin: httpx.Client) -> None:
    for pid in (P1, P2):
        admin.put(f"/api/projects/{pid}/budget",
                  json={"monthly_budget_usd": None, "allow_overrun": False})
    admin.put(f"/api/departments/{DEPT}/budget",
              json={"monthly_budget_usd": None, "allow_overrun": False})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui", default="http://localhost:5173")
    parser.add_argument("--out", default="/tmp/costs-shots")
    args = parser.parse_args()
    base = args.ui.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results: list[tuple[bool, str]] = []

    admin = e2e_client(base, timeout=60, project=False)
    print("version", (admin.get("/api/system/version").json() or {}).get("short"))
    ensure_fixtures(admin)
    reset_budgets(admin)
    must(admin.put(f"/api/departments/{DEPT}/budget", json={"monthly_budget_usd": 100}),
         "set the department budget to $100")

    da_name = f"e2e-dadmin-{secrets.token_hex(3)}"
    da_password = "Lp-" + secrets.token_urlsafe(18)
    da = must(admin.post("/api/users", json={
        "username": da_name, "email": f"{da_name}@example.com", "password": da_password,
        "role": "member", "never_expires": True}), "create the department admin", 201)
    da_id = da.get("id") or (da.get("user") or {}).get("id")
    must(admin.put(f"/api/departments/{DEPT}/admins", json={"add": [da_id]}),
         "make the account a department administrator")

    def budget_of(pid: str) -> dict:
        return (admin.get(f"/api/projects/{pid}/quotas").json() or {}).get("budget") or {}

    def step(name: str, fn: Callable[[], None], page: Page) -> None:
        try:
            fn()
            results.append((True, name))
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - reported as a failed step
            results.append((False, name))
            print(f"FAIL  {name} -- {str(exc).splitlines()[0][:300]}")
            safe = re.sub(r"[^a-z0-9]+", "-", name.lower())[:60]
            page.screenshot(path=str(out / f"fail-{safe}.png"), full_page=True)

    def login(browser: Browser, user: str, password: str | None) -> tuple[BrowserContext, Page]:
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
        ctx.add_init_script("(s => { for (const [k, v] of Object.entries(s)) "
                            "localStorage.setItem(k, v); })(" + json.dumps(STORE) + ")")
        page = ctx.new_page()
        if password:
            res = page.request.post(f"{base}/api/auth/login",
                                    data={"username": user, "password": password})
            if not res.ok:
                raise SystemExit(f"browser login {user}: HTTP {res.status}")
        return ctx, page

    def row(page: Page, pid: str):
        return page.locator(f"[data-testid=v3-costs-department-row-{pid}]")

    def save(page: Page, scope, text: str) -> None:
        """Click `scope`'s Save and wait for ITS toast: an earlier step's toast with the
        same text must be gone first, and the pointer is parked so a hover tooltip of a
        neighbouring row's button cannot sit over this one."""
        toast = page.get_by_text(text)
        expect(toast).to_have_count(0, timeout=15000)
        page.mouse.move(0, 0)
        scope.get_by_role("button", name="Save").click()
        expect(toast).to_be_visible(timeout=15000)

    def until(read: Callable[[], dict], ok: Callable[[dict], bool], what: str) -> dict:
        """The API read-back, polled briefly: the page may still be reloading."""
        for _ in range(20):
            value = read()
            if ok(value):
                return value
            time.sleep(0.5)
        raise AssertionError(f"{what}: {json.dumps(value)[:300]}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx, page = login(browser, da_name, da_password)

            # ── what a department administrator can open ─────────────────────
            def tabs() -> None:
                page.goto(f"{base}/v3/costs")
                head = page.locator("main .v3-filters")
                expect(head.get_by_role("button", name="Project budget")).to_be_visible(
                    timeout=20000)
                expect(head.get_by_role("button", name="Department")).to_be_visible()
                for hidden in ("Spend", "Alerts", "Platform"):
                    assert head.get_by_role("button", name=hidden, exact=True).count() == 0, \
                        hidden
            step("tabs: Project budget + Department only", tabs, page)

            def rail() -> None:
                assert page.locator('a[href="/v3/costs?view=department"]').count() >= 1
                assert page.locator('a[href="/v3/costs?view=platform"]').count() == 0
            step("rail: department entry, no platform entry", rail, page)

            def project_hint() -> None:
                page.goto(f"{base}/v3/costs?view=project")
                expect(page.get_by_test_id("v3-costs-project-manage-hint")).to_be_visible(
                    timeout=20000)
                page.get_by_role("link", name="Open the Department tab").click()
                expect(page.get_by_test_id("v3-costs-department")).to_be_visible(timeout=20000)
                assert "view=department" in page.url
            step("project tab: hint links to the Department tab", project_hint, page)

            def no_platform() -> None:
                page.goto(f"{base}/v3/costs?view=platform")
                page.wait_for_timeout(3000)
                assert page.get_by_test_id("v3-costs-platform").count() == 0
            step("?view=platform does not open for a department admin", no_platform, page)

            # ── the department page ──────────────────────────────────────────
            def dept_page() -> None:
                page.goto(f"{base}/v3/costs?view=department&id={DEPT}")
                expect(page.get_by_test_id("v3-costs-department")).to_be_visible(timeout=20000)
                expect(page.locator("main").get_by_text("$100.00").first).to_be_visible()
                assert page.get_by_test_id("v3-costs-department-editor").count() == 0
                expect(row(page, P1)).to_be_visible()
                expect(row(page, P2)).to_be_visible()
                archived = page.get_by_test_id("v3-costs-archived")
                if archived.count():
                    archived.locator("summary").click()
                    assert archived.locator("input").count() == 0
                    assert archived.locator("button").count() == 0
            step("department page: ceiling, no dept editor, archived read-only", dept_page,
                 page)

            def allocate() -> None:
                r = row(page, P1)
                r.locator("input.v3-input").fill("60")
                save(page, r, f"Budget of {P1_NAME} saved")
                until(lambda: budget_of(P1), lambda b: b.get("budget_usd") == 60, "A at $60")
                expect(page.locator(".v3-stat").filter(has_text="Allocated")
                       .get_by_text("$60.00")).to_be_visible(timeout=10000)
            step("allocate $60 through the page", allocate, page)

            def over() -> None:
                must(admin.put(f"/api/projects/{P1}/budget", json={"monthly_budget_usd": 60}),
                     "pin the first project at $60")  # independent of the step before
                page.reload()
                r = row(page, P2)
                r.locator("input.v3-input").fill("50")
                expect(r.locator("small", has_text="Does not fit the department")).to_be_visible(
                    timeout=20000)
                expect(r.get_by_role("button", name="Save")).to_be_disabled()
                r.locator("input.v3-input").fill("40")
                expect(r.get_by_role("button", name="Save")).to_be_enabled()
                save(page, r, f"Budget of {P2_NAME} saved")
                until(lambda: budget_of(P2), lambda b: b.get("budget_usd") == 40, "B at $40")
                expect(page.locator(".v3-stat").filter(has_text="Unallocated")
                       .get_by_text("$0.00")).to_be_visible(timeout=10000)
            step("over-allocation held in the page; the exact remainder saves", over, page)

            def lower_full() -> None:
                r = row(page, P1)
                r.locator("input.v3-input").fill("55")
                save(page, r, f"Budget of {P1_NAME} saved")
                until(lambda: budget_of(P1), lambda b: b.get("budget_usd") == 55, "A at $55")
            step("lowering a budget in a full department saves", lower_full, page)

            def overrun_locked() -> None:
                page.reload()
                expect(row(page, P1)).to_be_visible(timeout=20000)
                assert row(page, P1).locator("input[type=checkbox]").is_disabled()
            step("overrun switch locked while the department forbids it", overrun_locked, page)

            def overrun_on() -> None:
                must(admin.put(f"/api/projects/{P1}/budget", json={"monthly_budget_usd": 55}),
                     "pin the first project at $55")
                must(admin.put(f"/api/departments/{DEPT}/budget", json={"allow_overrun": True}),
                     "allow overrun on the department")
                page.reload()
                r = row(page, P1)
                box = r.locator("input[type=checkbox]")
                expect(box).to_be_enabled(timeout=20000)
                box.check()
                save(page, r, f"Budget of {P1_NAME} saved")
                until(lambda: budget_of(P1),
                      lambda b: b.get("allow_overrun") is True
                      and b.get("block_at_usd") == round(55 * 1.1, 4), "A overrun on")
                expect(row(page, P1).get_by_text("80% · 105% · 110%")).to_be_visible(
                    timeout=10000)
            step("overrun switch on once the department allows it (80/105/110)", overrun_on,
                 page)

            def clear() -> None:
                r = row(page, P1)
                expect(r.locator("input[type=checkbox]")).to_be_checked(timeout=20000)
                r.locator("input[type=checkbox]").uncheck()
                r.locator("input.v3-input").fill("")
                save(page, r, f"Budget of {P1_NAME} saved")
                until(lambda: admin.get(f"/api/projects/{P1}/quotas").json(),
                      lambda q: q.get("budget") is None and q.get("allow_overrun") is False,
                      "A cleared")
            step("clearing a budget and its switch through the page", clear, page)
            ctx.close()

            # ── the organization level: the department editor ────────────────
            ctx, page = login(browser, admin_user(), admin_password())

            def dept_editor() -> None:
                admin.put(f"/api/departments/{DEPT}/budget", json={"allow_overrun": False})
                must(admin.put(f"/api/projects/{P2}/budget", json={"monthly_budget_usd": 40}),
                     "pin the second project at $40")
                page.goto(f"{base}/v3/costs?view=department&id={DEPT}")
                editor = page.get_by_test_id("v3-costs-department-editor")
                expect(editor).to_be_visible(timeout=20000)
                field = editor.locator("input.v3-input")
                field.fill("30")  # below the $40 still allocated
                expect(editor.get_by_role("button", name="Save")).to_be_disabled()
                field.fill("120")
                save(page, editor, "Department budget saved")
                until(lambda: admin.get(f"/api/departments/{DEPT}/budget").json(),
                      lambda v: v.get("monthly_budget_usd") == 120
                      and v.get("allocated_usd") == 40, "department at $120 / $40")
            step("admin: department editor refuses below-allocated, saves 120", dept_editor,
                 page)

            def platform() -> None:
                page.goto(f"{base}/v3/costs?view=platform")
                table = page.get_by_test_id("v3-costs-platform-departments")
                expect(table).to_be_visible(timeout=20000)
                line = table.locator("tr").filter(has_text=DEPT_NAME)
                expect(line.get_by_text("$120.00")).to_be_visible()
                expect(line.get_by_text("$40.00")).to_be_visible()
            step("admin: platform page shows the department's new ceiling", platform, page)
            ctx.close()
            browser.close()
    finally:
        reset_budgets(admin)
        for pid in (P1, P2):
            admin.post(f"/api/projects/{pid}/archive")
        admin.put(f"/api/departments/{DEPT}/admins", json={"remove": [da_id]})
        res = admin.delete(f"/api/users/{da_id}")
        print(f"cleanup: budgets cleared, projects archived, {da_name} deleted "
              f"(HTTP {res.status_code})")

    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


def admin_user() -> str:
    return _first_env(USERNAME_VARS) or "admin"


def admin_password() -> str | None:
    return _first_env(PASSWORD_VARS)


if __name__ == "__main__":
    sys.exit(main())
