#!/usr/bin/env python3
"""Browser pass over the Agent-DLC console (`/v2/eval/standards`) — headless Chromium.

Signs in, opens every `?view=` sub-page (and the account-free annotate page with a
dead token), and fails on: a page that does not render its view, a console error, or
an API call that answered 5xx. Screenshots land in `--out` for a human look.

Read-only against the backend: it opens pages and reads; it creates nothing.

Run:  cd backend && uv run python scripts/e2e_agent_dlc_browser.py \\
        --ui http://localhost:5199 [--out /tmp/dlc-shots] [--lang zh-CN]
Needs LAUNCHPAD_E2E_USERNAME / _PASSWORD when the login gate is on.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWS = ["scorecard", "criteria", "golden", "admission", "calibration", "release",
         "watch", "compare", "audit"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui", default="http://localhost:5173")
    parser.add_argument("--out", default="/tmp/dlc-shots")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--agent", default=None, help="agent id to scope the views to")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900},
                                      locale=args.lang)
        page = context.new_page()
        errors: list[str] = []
        bad: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: bad.append(f"{r.status} {r.url}")
                if r.status >= 500 else None)

        user = os.environ.get("LAUNCHPAD_E2E_USERNAME")
        password = os.environ.get("LAUNCHPAD_E2E_PASSWORD")
        if user and password:
            res = context.request.post(f"{args.ui}/api/auth/login",
                                       data={"username": user, "password": password})
            if res.status != 200:
                print(f"login failed: HTTP {res.status}")
                return 1
        # the language is persisted by the console; set it before the first render
        page.goto(f"{args.ui}/v2")
        page.evaluate("lang => localStorage.setItem('i18nextLng', lang)", args.lang)

        for view in VIEWS:
            errors.clear()
            bad.clear()
            query = f"view={view}" + (f"&agent={args.agent}" if args.agent else "")
            page.goto(f"{args.ui}/v2/eval/standards?{query}", wait_until="networkidle")
            page.wait_for_timeout(1200)
            tab = page.locator(f'[data-testid="v2-tab-{view}"]')
            selected = tab.count() > 0 and tab.first.get_attribute("aria-selected") == "true"
            shot = out / f"{args.lang}-{view}.png"
            page.screenshot(path=str(shot), full_page=True)
            # a stale/expected 404 for an optional read is not a failure; errors are
            real_errors = [e for e in errors if "404" not in e and "favicon" not in e]
            ok = selected and not real_errors and not bad
            print(f"{'PASS' if ok else 'FAIL'}  {view:<12} tab={selected} "
                  f"console_errors={len(real_errors)} 5xx={len(bad)} → {shot}")
            for line in (real_errors + bad)[:5]:
                print(f"        {line[:200]}")
            if not ok:
                failures.append(view)

        # the account-free annotate page with a dead token: the opaque "gone" state
        anon = browser.new_context(locale=args.lang).new_page()
        anon.goto(f"{args.ui}/r/annotate/shr_not-a-real-token", wait_until="networkidle")
        gone = anon.locator('[data-testid="annotate-gone"]').count() == 1
        anon.screenshot(path=str(out / f"{args.lang}-annotate-gone.png"))
        print(f"{'PASS' if gone else 'FAIL'}  annotate-gone")
        if not gone:
            failures.append("annotate-gone")
        browser.close()

    print(f"\n{len(VIEWS) + 1 - len(failures)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
