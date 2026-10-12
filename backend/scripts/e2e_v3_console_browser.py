#!/usr/bin/env python3
"""Browser pass over console V3 (`/v3`) — headless Chromium, read-mostly.

Signs in, opens every V3 page (command center, agents list and one agent, chat,
release gate) and every module page V3 hosts from V2 (they must render inside the
V3 shell, on its theme), exercises the ⌘K palette and the V2 ⇄ V3 switch, and
fails on a page that does not render, a console error, or an API call that
answered 5xx.
Screenshots land in `--out`. It sends one chat message when an agent is
available (`--chat`), which invokes that agent.

Run:  cd backend && uv run python scripts/e2e_v3_console_browser.py \\
        --ui http://localhost:5199 [--out /tmp/v3-shots] [--lang zh-CN] [--chat]
Needs LAUNCHPAD_E2E_USERNAME / _PASSWORD when the login gate is on.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui", default="http://localhost:5173")
    parser.add_argument("--out", default="/tmp/v3-shots")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--chat", action="store_true", help="send one message to the first agent")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<26} {detail}")
        if not ok:
            failures.append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1480, "height": 940}, locale=args.lang,
                                      color_scheme="dark")
        page = context.new_page()
        errors: list[str] = []
        bad: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("response",
                lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 500 else None)

        user = os.environ.get("LAUNCHPAD_E2E_USERNAME")
        password = os.environ.get("LAUNCHPAD_E2E_PASSWORD")
        if user and password:
            res = context.request.post(f"{args.ui}/api/auth/login",
                                       data={"username": user, "password": password})
            if res.status != 200:
                print(f"login failed: HTTP {res.status}")
                return 1
        page.goto(f"{args.ui}/v2")
        page.evaluate("lang => localStorage.setItem('i18nextLng', lang)", args.lang)

        def visit(name: str, path: str, ready: str) -> None:
            errors.clear()
            bad.clear()
            page.goto(f"{args.ui}{path}", wait_until="networkidle")
            page.wait_for_timeout(1200)
            shown = page.locator(ready).count() > 0
            page.screenshot(path=str(out / f"{args.lang}-{name}.png"), full_page=True)
            real = [e for e in errors if "404" not in e and "favicon" not in e]
            check(name, shown and not real and not bad,
                  f"ready={shown} console_errors={len(real)} 5xx={len(bad)}")
            for line in (real + bad)[:4]:
                print(f"        {line[:200]}")

        # V2 → V3 through the visible switch, not by typing a URL
        page.goto(f"{args.ui}/v2", wait_until="networkidle")
        page.locator('[data-testid="v2-switch-v3"]').click()
        page.wait_for_url("**/v3", timeout=10000)
        # the V3 shell is a lazy chunk: wait for it rather than racing it
        page.wait_for_selector('[data-testid="v3-shell"]', timeout=15000)
        check("switch.v2→v3", page.locator('[data-testid="v3-shell"]').count() == 1)
        stored = page.evaluate("localStorage.getItem('launchpad_ui_version')")
        check("switch.remembered", stored == "v3", f"stored={stored}")

        # first visit: the tour opens on its own, steps with the keyboard, and once
        # left it does not come back on the next visit
        page.goto(f"{args.ui}/v3", wait_until="networkidle")
        try:
            page.wait_for_selector(".v3-tour-card", timeout=8000)
            opened = True
        except Exception:  # noqa: BLE001 - reported as a failed check below
            opened = False
        stops = 0
        if opened:
            page.screenshot(path=str(out / f"{args.lang}-tour.png"))
            seen = set()
            for _ in range(8):
                seen.add(page.locator(".v3-tour-card h3").inner_text())
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(450)
            stops = len(seen)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        check("tour.first-visit", opened and stops >= 4, f"stops={stops}")
        page.goto(f"{args.ui}/v3", wait_until="networkidle")
        page.wait_for_timeout(1800)
        check("tour.once", page.locator(".v3-tour-card").count() == 0)
        check("launch.sequence", page.locator('[data-tour="launch"]').count() == 1)

        visit("home", "/v3", ".v3-title")
        visit("agents", "/v3/agents", "table.v3-table, .v3-empty")
        # the agent's name is the link into its detail (the row itself is not clickable)
        first = page.locator("table.v3-table tbody a.v3-inline-link").first
        if first.count():
            first.click()
            page.wait_for_timeout(1500)
            page.screenshot(path=str(out / f"{args.lang}-agent-detail.png"), full_page=True)
            check("agent.detail", page.locator(".v3-pipe-step, .v3-title").count() > 0,
                  page.url.split("?")[-1])
        visit("chat", "/v3/chat", ".v3-chat-composer")
        if args.chat and page.locator(".v3-chat-composer textarea:not([disabled])").count():
            page.locator(".v3-chat-composer textarea").fill(
                "Hello — what can you help with? One sentence.")
            page.keyboard.press("Enter")
            page.wait_for_selector(".v3-msg.agent .bubble", timeout=90000)
            page.wait_for_function(
                "() => !document.querySelector('.v3-msg.agent.streaming')", timeout=120000)
            page.screenshot(path=str(out / f"{args.lang}-chat-answered.png"), full_page=True)
            text = page.locator(".v3-msg.agent .bubble").last.inner_text()
            check("chat.answered", len(text.strip()) > 0, repr(text.strip()[:60]))
        visit("gate", "/v3/gate", ".v3-title")
        # rebuilt modules: each opens natively, and its V2 URL hands over to it
        visit("create", "/v3/create", ".v3-scenarios")
        visit("registry", "/v3/registry", ".v3-title")
        visit("knowledge", "/v3/knowledge", ".v3-title")
        visit("assistant", "/v3/assistant", ".v3-title")
        for name in ("releases", "issues", "environments", "observability", "memory",
                     "governance", "connections", "costs", "insights", "intents", "data",
                     "evaluators", "tasks", "online", "experiments", "standards", "skill-lab",
                     "users", "workspaces", "identity", "fleet", "announcements", "videos",
                     "video-management"):
            visit(name, f"/v3/{name}", ".v3-title")
        for v2, v3 in (("/v2/registry", "/v3/registry"), ("/v2/knowledge-bases", "/v3/knowledge"),
                       ("/v2/assistant", "/v3/assistant"), ("/v2/agents?view=new", "/v3/create"),
                       ("/v2/promotions", "/v3/releases"), ("/v2/costs?view=alerts", "/v3/costs"),
                       ("/v2/my-connections", "/v3/connections"),
                       ("/v2/observability", "/v3/observability"),
                       ("/v2/eval/tasks?view=new", "/v3/tasks"),
                       ("/v2/eval/standards", "/v3/standards"),
                       ("/v2/connections", "/v3/identity"), ("/v2/skill-lab", "/v3/skill-lab")):
            page.goto(f"{args.ui}{v2}", wait_until="networkidle")
            check(f"twin {v2}", page.url.replace(args.ui, "").startswith(v3),
                  page.url.replace(args.ui, ""))

        # Modules: every group unfolds, and every page it lists renders hosted in V3
        page.goto(f"{args.ui}/v3", wait_until="networkidle")
        page.wait_for_selector('[data-testid="v3-shell"]', timeout=15000)
        heads = page.locator('[data-testid^="v3-rail-group-"]')
        for i in range(heads.count()):
            if heads.nth(i).get_attribute("aria-expanded") != "true":
                heads.nth(i).click()
        page.screenshot(path=str(out / f"{args.lang}-rail-open.png"))
        links = page.locator(".v3-rail a.v3-rail-sub").evaluate_all(
            "els => els.map(e => e.getAttribute('href'))")
        check("rail.modules", len(links) >= 20, f"{len(links)} pages")
        for href in links:
            errors.clear()
            bad.clear()
            page.goto(f"{args.ui}{href}", wait_until="networkidle")
            page.wait_for_timeout(600)
            # a rebuilt module renders natively; every other one hosted in V3
            native = href.startswith("/v3")
            body = ".v3-title" if native else ".v3-host"
            shown = page.locator(f"[data-testid='v3-shell'] {body}").count() > 0
            shown = shown and page.locator("[data-testid='v2-shell']").count() == 0
            real = [e for e in errors if "404" not in e and "favicon" not in e]
            if not (shown and not real and not bad):
                check(f"v2 {href}", False,
                      f"shell={shown} console_errors={len(real)} 5xx={len(bad)}")
                for line in (real + bad)[:3]:
                    print(f"        {line[:200]}")
        check("rail.modules.hosted", not any(f.startswith("v2 ") for f in failures))
        # a hosted page must not undo the V3 choice
        stored = page.evaluate("localStorage.getItem('launchpad_ui_version')")
        check("rail.keeps.v3", stored == "v3", f"stored={stored}")

        # ⌘K: opens, filters, navigates
        page.goto(f"{args.ui}/v3", wait_until="networkidle")
        page.keyboard.press("Meta+k")
        opened = page.locator(".v3-cmdk").count() == 1
        page.keyboard.type("chat")
        page.wait_for_timeout(200)
        page.screenshot(path=str(out / f"{args.lang}-cmdk.png"))
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        check("cmdk", opened and "/v3/chat" in page.url, page.url.replace(args.ui, ""))

        # and back to V2: the choice is honoured on the next visit to "/"
        page.locator('[data-testid="v3-switch-v2"]').click()
        page.wait_for_selector('[data-testid="v2-shell"]', timeout=15000)
        check("switch.v3→v2.samepage", "/v2/chat" in page.url, page.url.replace(args.ui, ""))
        page.goto(f"{args.ui}/", wait_until="networkidle")
        check("switch.v3→v2", "/v2" in page.url, page.url.replace(args.ui, ""))
        browser.close()

    print(f"\n{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
