#!/usr/bin/env python3
"""Portable browser E2E for taskgen input attachments.

Covers the part a browser is the right tool for: that the panel stages files
through the shared task-asset endpoint, renders them at the path the prompt and
the import both use, removes one, and puts the surviving staged tokens into the
job request. It then CANCELS the submitted job immediately — generation itself
runs a real model on the AgentCore worker, so waiting here would burn minutes and
tokens to re-prove what the hermetic tests already pin.

The review table's documents column and the unused-uploads note need a finished
generation, so they are covered by the real-AWS script
(`e2e_skill_lab_taskgen_assets.py`), which has one anyway.

Runs against an already-started frontend/backend (signs in with
LAUNCHPAD_E2E_USERNAME / _PASSWORD when set), writes only the requested
evidence directory, and leaves no job running.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import Request, Response, sync_playwright

CSV = b"region,revenue\nAPAC,1240\nEMEA,980\n"
MD = b"# Q2 brief\n\nRevenue up 14%.\n"


def tid(scope, test_id: str):
    """The classic page's test id, or the same id as the V2 page hosted in console V3
    renders it (`v2-` prefixed) — classic `/skill-lab` URLs now redirect there."""
    return scope.locator(f'[data-testid="{test_id}"], [data-testid="v2-{test_id}"]')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--evidence-dir", default="/tmp/skill-lab-taskgen-e2e")
    parser.add_argument("--browser", default=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    fixtures = evidence / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "rows.csv").write_bytes(CSV)
    (fixtures / "brief.md").write_bytes(MD)
    (fixtures / "extra.txt").write_bytes(b"removed before submit\n")

    network: list[dict[str, object]] = []
    job_id: str | None = None
    with sync_playwright() as playwright:
        browser = (
            playwright.chromium.launch(headless=True, executable_path=args.browser)
            if args.browser
            else playwright.chromium.launch(headless=True)
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        # a console with the login gate on (prod) needs a session first
        user = os.environ.get("LAUNCHPAD_E2E_USERNAME")
        password = os.environ.get("LAUNCHPAD_E2E_PASSWORD")
        if user and password:
            res = page.request.post(f"{args.base_url}/api/auth/login",
                                    data={"username": user, "password": password})
            if not res.ok:
                raise SystemExit(f"login failed: HTTP {res.status}")

        def request_finished(request: Request) -> None:
            if "/api/skill-lab/" not in request.url:
                return
            response: Response | None = request.response()
            entry: dict[str, object] = {
                "method": request.method,
                "url": request.url,
                "status": response.status if response else None,
                "content_type": request.headers.get("content-type", ""),
            }
            if request.method == "POST" and request.url.endswith("/jobs"):
                entry["json"] = request.post_data_json
            network.append(entry)

        page.on("requestfinished", request_finished)
        try:
            page.goto(
                f"{args.base_url}/skill-lab?view=tasksets&gen=new", wait_until="networkidle"
            )
            tid(page, "taskgen-attach-input").set_input_files(
                [fixtures / "rows.csv", fixtures / "brief.md", fixtures / "extra.txt"]
            )
            # Three rows, each labelled with the runtime path — not the bare
            # filename — so the panel agrees with the prompt and the import.
            for name in ("rows.csv", "brief.md", "extra.txt"):
                row = tid(page, f"taskgen-attachment-{name}")
                row.wait_for(timeout=30_000)
                assert f"data/{name}" in row.inner_text(), row.inner_text()

            page.locator('[data-testid="taskgen-attachment-remove-extra.txt"], '
                         '[data-testid="v2-taskgen-attachment-extra.txt"] button').first.click()
            page.wait_for_timeout(300)
            assert tid(page, "taskgen-attachment-extra.txt").count() == 0

            upload = next(
                item for item in network if str(item["url"]).endswith("/task-assets")
            )
            assert str(upload["content_type"]).startswith("multipart/form-data; boundary=")
            assert upload["status"] == 201, upload

            # Submit with the smallest possible run, then cancel at once.
            # Skill rows toggle on click (the ☐/☑ is a glyph, not a checkbox input).
            page.locator('[data-testid="taskgen-skill-list"], [data-testid="v2-taskgen-skills"]'
                         ).locator("tr:has(td)").first.click()
            # Wait for the RESPONSE, not the request: `expect_request` resolves the
            # moment the request is issued, so reading "the newest job" afterwards
            # raced the server creating the row — it once cancelled the previous
            # run's job and left this run's generation running on AWS.
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/api/skill-lab/jobs"),
                timeout=60_000,
            ) as submitted:
                page.locator('[data-testid="taskgen-wizard-submit"], '
                             '[data-testid="v2-taskgen-submit"]').first.click()
            created = submitted.value
            body = created.request.post_data_json
            assert isinstance(body, dict), body
            attachments = body.get("attachments")
            assert isinstance(attachments, list) and len(attachments) == 2, body
            assert all(
                str(row["staged_asset"]).startswith("ta_") for row in attachments
            ), attachments
            # The id comes from the creating response, so cancel can never target
            # somebody else's job.
            job_id = str(created.json()["id"])
            page.screenshot(path=evidence / "taskgen-attachments.png", full_page=True)
        finally:
            # Generation runs a real model on the worker: stop it regardless of how
            # the assertions above went, so this script never leaves work running.
            if job_id:
                page.request.post(
                    f"{args.base_url}/api/skill-lab/jobs/{job_id}/cancel"
                )
            (evidence / "taskgen-network.json").write_text(
                json.dumps(network, indent=2), encoding="utf-8"
            )
            browser.close()
    print(json.dumps({"ok": True, "evidence_dir": str(evidence), "job": job_id}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
