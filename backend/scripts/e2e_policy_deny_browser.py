#!/usr/bin/env python3
"""Browser check of the Cedar policy-deny card in V3 and V2 chat — no AWS writes.

A real deny needs a Gateway with a Policy Engine in ENFORCE mode and a policy that
refuses the call; this check instead intercepts the chat stream and the history
read in the browser, so it proves the front end: the live ``policy_denied`` event
renders the card after its tool row, and a restored ``policy`` history row renders
it again (the backend side is covered by tests/test_chat_api.py).

Run:  cd backend && uv run python scripts/e2e_policy_deny_browser.py --ui http://localhost:5173 \\
        --agent <an active agent id> [--lang zh-CN]
Needs LAUNCHPAD_E2E_USERNAME / _PASSWORD when the login gate is on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from playwright.sync_api import Route, sync_playwright

REASON = "Policy evaluation denied due to cap-payouts"
TOOL = "hr-database___create_payout"


def sse(*events: tuple[str, dict]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui", default="http://localhost:5173")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--lang", default="zh-CN")
    args = parser.parse_args()
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<28} {detail}")
        if not ok:
            failures.append(name)

    stream = sse(
        ("meta", {"session_id": "e2e-policy-session"}),
        ("tool", {"name": TOOL}),
        ("policy_denied", {"tool": TOOL, "reason": REASON, "policy_id": "cap-payouts",
                           "gateway_id": "gw-e2e"}),
        ("delta", {"text": "I could not create that payout."}),
        ("done", {}),
    )
    history = {"messages": [
        {"id": 1, "role": "user", "text": "Pay Bob 500"},
        {"id": 2, "role": "tool", "text": "", "name": TOOL},
        {"id": 3, "role": "policy", "text": REASON, "name": TOOL},
        {"id": 4, "role": "agent", "text": "I could not create that payout."},
    ]}

    def on_chat(route: Route) -> None:
        req = route.request
        bare = req.url.split("?")[0].rstrip("/")
        if req.method == "POST" and bare.endswith(f"/api/chat/{args.agent}"):
            route.fulfill(status=200, headers={"content-type": "text/event-stream"}, body=stream)
        elif "/history" in req.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(history))
        else:
            route.continue_()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1480, "height": 940}, locale=args.lang)
        user = os.environ.get("LAUNCHPAD_E2E_USERNAME")
        password = os.environ.get("LAUNCHPAD_E2E_PASSWORD")
        if user and password:
            context.request.post(f"{args.ui}/api/auth/login",
                                 data={"username": user, "password": password})
        page = context.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.route("**/api/chat/**", on_chat)
        page.goto(f"{args.ui}/v2")
        page.evaluate(f"() => {{ localStorage.setItem('i18nextLng','{args.lang}');"
                      " localStorage.setItem('launchpad_v3_tour_done','1'); }")

        for console in ("v3", "v2"):
            page.evaluate(f"() => localStorage.setItem('launchpad_ui_version','{console}')")
            path = f"/{console}/chat?agent={args.agent}"
            page.goto(f"{args.ui}{path}", wait_until="networkidle")
            composer = ".v3-chat-composer textarea" if console == "v3" else "textarea"
            box = page.locator(composer).first
            box.fill("Pay Bob 500")
            page.keyboard.press("Enter")
            card = page.locator('[data-testid="policy-deny-card"]')
            try:
                card.first.wait_for(timeout=15000)
            except Exception:  # noqa: BLE001 - reported below
                pass
            reason = page.locator('[data-testid="policy-deny-reason"]').first
            live_ok = card.count() == 1 and reason.inner_text().strip() == REASON
            shown = reason.inner_text().strip() if card.count() else "no card"
            check(f"{console}.live card", live_ok, shown)
            link = page.locator('[data-testid="policy-deny-link"]').first
            href = link.get_attribute("href") if card.count() else ""
            check(f"{console}.links policies", "gateway=gw-e2e" in (href or ""), href or "")
            page.screenshot(path=f"/tmp/{args.lang}-{console}-policy-live.png")

            # restore: reopen the same session from history
            page.goto(f"{args.ui}{path}&session=e2e-policy-session", wait_until="networkidle")
            page.wait_for_timeout(1500)
            restored = page.locator('[data-testid="policy-deny-card"]')
            check(f"{console}.restored card", restored.count() == 1,
                  page.locator('[data-testid="policy-deny-reason"]').first.inner_text().strip()
                  if restored.count() else "no card")
            page.screenshot(path=f"/tmp/{args.lang}-{console}-policy-restored.png")
        real = [e for e in errors if "404" not in e and "favicon" not in e]
        check("no console errors", not real, "; ".join(e[:80] for e in real[:3]))
        browser.close()
    print(f"\n{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
