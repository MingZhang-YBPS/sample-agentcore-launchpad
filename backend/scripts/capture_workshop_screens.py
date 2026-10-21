#!/usr/bin/env python3
"""Recapture the Agent-DLC workshop screenshots from a live console.

Reads the shot list in `docs/workshop-screens.yaml` and, for every shot and every
language, signs in as the shot's workshop account in a fresh headless Chromium
context, puts the browser into a known state (theme, language, tour seen, rail
mode ...), runs the shot's actions, crops to the shot's element(s), draws the
numbered highlight boxes the workshop captions refer to, and writes
`<name>.<zh|en>.webp` into the workshop's `static/images/`.

A shot's *data* preconditions (a published criteria table, a sealed golden set, a
gate report ...) are not faked: each shot lists them under `requires`, a probe of
the console's own API checks them, and a shot whose preconditions do not hold is
reported as SKIP instead of being captured against the wrong state. Shots that are
not console pages (AWS console, terminal, diagrams) are `manual:` and never
overwritten.

Run (from backend/):
    uv run python scripts/capture_workshop_screens.py \\
        --workshop ~/Agent-DLC-Workshop/agent-dlc-agentcore-launchpad \\
        [--only '10-setup-*' ...] [--lang zh] [--report] [--probe NAME]

Console URL and the five workshop passwords come from the CloudFormation stack
outputs (`--stack`, default agent-dlc-launchpad) and are never printed or written.
`LAUNCHPAD_WS_URL` and `LAUNCHPAD_WS_PASSWORD_<ACCOUNT>` (account upper-cased,
`-` → `_`) override them.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from PIL import Image
from playwright.sync_api import Page, sync_playwright

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "docs" / "workshop-screens.yaml"
LANGS = {"zh": "zh-CN", "en": "en"}
# stack output → workshop account
PASSWORD_OUTPUTS = {
    "admin": "ConsolePassword",
    "developer": "DeveloperPassword",
    "biz-owner": "BizOwnerPassword",
    "release-ops": "ReleaseOpsPassword",
    "sme": "SmePassword",
}


def _page_js() -> dict[str, str]:
    """The page-side helpers, kept as plain JS beside this file."""
    text = (Path(__file__).with_suffix(".js")).read_text()
    parts = re.split(r"^// @(\w+)\n", text, flags=re.M)
    # leading // lines document a helper; Playwright must see the bare function
    def body(chunk: str) -> str:
        return "\n".join(ln for ln in chunk.strip().splitlines() if not ln.startswith("//"))

    return {parts[i]: body(parts[i + 1]) for i in range(1, len(parts), 2)}


_JS = _page_js()
OVERLAY_JS, UNCLAMP_JS, PROBE_JS = _JS["OVERLAY_JS"], _JS["UNCLAMP_JS"], _JS["PROBE_JS"]
RELABEL_JS = _JS["RELABEL_JS"]


class SkipShot(Exception):
    """A precondition does not hold; the shot is reported, not captured."""


@dataclass
class Ctx:
    url: str
    passwords: dict[str, str]
    agents: dict[str, str] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    kbs: dict[str, str] = field(default_factory=dict)
    records: dict[str, str] = field(default_factory=dict)
    # the workshop repo: `upload` paths are relative to it
    workshop: Path | None = None
    # set by --assume-state: the walker vouches for every `state:` requirement
    assume_state: bool = False
    # --display-region: (actual, shown) — the walk ran in another region than the
    # workshop's; the page text says the workshop's region
    relabel: tuple[str, str] | None = None


# ── configuration ────────────────────────────────────────────────────────────


def stack_outputs(stack: str, region: str) -> dict[str, str]:
    raw = subprocess.check_output([
        "aws", "cloudformation", "describe-stacks", "--region", region, "--stack-name", stack,
        "--query", "Stacks[0].Outputs", "--output", "json"])
    return {o["OutputKey"]: o["OutputValue"] for o in json.loads(raw)}


def load_ctx(stack: str, region: str) -> Ctx:
    env_url = os.environ.get("LAUNCHPAD_WS_URL")
    outs: dict[str, str] = {}
    if not env_url or not all(_env_pw(a) for a in PASSWORD_OUTPUTS):
        outs = stack_outputs(stack, region)
    url = (env_url or outs["ConsoleUrl"]).rstrip("/")
    passwords = {a: _env_pw(a) or outs.get(k, "") for a, k in PASSWORD_OUTPUTS.items()}
    return Ctx(url=url, passwords=passwords)


def _env_pw(account: str) -> str | None:
    return os.environ.get("LAUNCHPAD_WS_PASSWORD_" + account.upper().replace("-", "_"))


def password_for(ctx: Ctx, account: str) -> str:
    """The five stack accounts come from the stack; any other account a shot signs in
    as (one the walk created, e.g. `developer-2`) from `LAUNCHPAD_WS_PASSWORD_<ACCOUNT>`."""
    pw = ctx.passwords.get(account) or _env_pw(account)
    if not pw:
        raise SkipShot(f"no password for {account} (set LAUNCHPAD_WS_PASSWORD_"
                       f"{account.upper().replace('-', '_')})")
    return pw


def api(ctx: Ctx, account: str = "admin") -> httpx.Client:
    client = httpx.Client(base_url=ctx.url, timeout=150)
    r = client.post("/api/auth/login",
                    json={"username": account, "password": password_for(ctx, account)})
    if r.status_code != 200:
        raise SystemExit(f"login as {account} failed: HTTP {r.status_code}")
    return client


def discover(ctx: Ctx) -> None:
    """Names the manifest refers to → ids, plus each agent's story facts."""
    c = api(ctx)
    try:
        # agents are project-scoped: without X-Project the list is the default
        # project's only, and the workshop's flywheel runs in retail-service
        projects = c.get("/api/projects").json()
        pids = [p["id"] for p in (projects if isinstance(projects, list)
                                  else projects.get("projects", projects.get("items", [])))]
        project_of: dict[str, str] = {}
        for pid in pids or ["default"]:
            res = c.get("/api/agents", headers={"X-Project": pid})
            if res.status_code != 200:
                continue
            for a in res.json().get("agents", []):
                ctx.agents.setdefault(a["name"], a["id"])
                project_of.setdefault(a["id"], pid)
            kres = c.get("/api/knowledge-bases", headers={"X-Project": pid})
            if kres.status_code == 200:
                for kb in kres.json().get("items", []):
                    ctx.kbs.setdefault(kb["name"], kb["kb_id"])
            rres = c.get("/api/registry/records", headers={"X-Project": pid})
            if rres.status_code == 200:
                body = rres.json()
                for rec in body if isinstance(body, list) else body.get("records", []):
                    ctx.records.setdefault(rec["name"], rec["record_id"])
        for u in c.get("/api/users").json().get("items", []):
            ctx.users[u["username"]] = u["id"]
        for name, aid in ctx.agents.items():
            r = c.get(f"/api/agents/{aid}/story",
                      headers={"X-Project": project_of.get(aid, "default")})
            if r.status_code == 200:
                ctx.facts[name] = r.json().get("facts", {})
    finally:
        c.close()


# ── manifest helpers ─────────────────────────────────────────────────────────


def per_lang(value: Any, lang: str) -> Any:
    """A manifest value may be a {zh: ..., en: ...} pair."""
    if isinstance(value, dict) and set(value) <= {"zh", "en"} and value:
        return value.get(lang, value.get("en"))
    return value


def expand(text: Any, ctx: Ctx, lang: str) -> Any:
    text = per_lang(text, lang)
    if not isinstance(text, str):
        return text

    def sub(m: re.Match[str]) -> str:
        kind, name = m.group(1), m.group(2)
        table = {"agent": ctx.agents, "user": ctx.users, "kb": ctx.kbs,
                 "record": ctx.records}[kind]
        if name not in table:
            raise SkipShot(f"no {kind} named {name!r} on the console")
        return table[name]

    def env(m: re.Match[str]) -> str:
        # a secret the manifest types but never stores (e.g. a trainee's password)
        value = os.environ.get(m.group(1))
        if not value:
            raise SkipShot(f"needs environment variable {m.group(1)}")
        return value

    text = re.sub(r"\{env:([A-Z][A-Z0-9_]*)\}", env, text)
    return re.sub(r"\{(agent|user|kb|record):([\w.-]+)\}", sub, text)


def check_requires(shot: dict[str, Any], ctx: Ctx) -> None:
    """`requires` entries: `agent:<name>`, `kb:<name>`, `record:<name>`, `user:<name>`,
    `fact:<agent>.<key>[=<value>]`."""
    for req in shot.get("requires", []):
        kind, _, rest = req.partition(":")
        if kind == "agent" and rest not in ctx.agents:
            raise SkipShot(f"needs agent {rest}")
        if kind == "kb" and rest not in ctx.kbs:
            raise SkipShot(f"needs knowledge base {rest}")
        if kind == "record" and rest not in ctx.records:
            raise SkipShot(f"needs registry record {rest}")
        if kind == "user" and rest not in ctx.users:
            raise SkipShot(f"needs user {rest}")
        if kind == "fact":
            target, _, want = rest.partition("=")
            agent, _, key = target.partition(".")
            have = ctx.facts.get(agent, {}).get(key)
            ok = (str(have).lower() == want.lower()) if want else bool(have)
            if not ok:
                raise SkipShot(f"needs {target}{'=' + want if want else ''} (is {have!r})")
        if kind == "state":
            # a precondition only a walk-through produces: the walker who reached
            # it says so with --assume-state; otherwise the shot waits for it
            if not ctx.assume_state:
                raise SkipShot(f"needs workshop state: {rest}")


def base_storage(lang: str, shot: dict[str, Any]) -> dict[str, str | None]:
    store: dict[str, str | None] = {
        "launchpad_v3_theme": shot.get("theme", "dark"),
        "i18nextLng": LANGS[lang],
        "launchpad_ui_version": "v3",
        "launchpad_v3_tour_done": "1",
        "lp.v3.create.chooser": "skip",
    }
    store.update(shot.get("storage", {}))
    return store


# ── capture ──────────────────────────────────────────────────────────────────


def run_actions(page: Page, actions: list[dict[str, Any]], ctx: Ctx, lang: str) -> None:
    for act in actions:
        (verb, arg), = act.items()
        arg = per_lang(arg, lang)
        if verb == "goto":
            page.goto(ctx.url + expand(arg, ctx, lang), wait_until="networkidle")
        elif verb == "click":
            page.locator(expand(arg, ctx, lang)).first.click()
        elif verb == "hover":
            page.locator(expand(arg, ctx, lang)).first.hover()
        elif verb == "fill":
            sel, value = arg
            page.locator(expand(sel, ctx, lang)).first.fill(expand(value, ctx, lang))
        elif verb == "upload":
            # [selector of an <input type=file>, [paths relative to the workshop repo]]
            sel, paths = arg
            base = ctx.workshop or Path(".")
            page.locator(expand(sel, ctx, lang)).first.set_input_files(
                [str(base / p) for p in per_lang(paths, lang)])
        elif verb == "press":
            page.keyboard.press(arg)
        elif verb == "select":
            # [selector of a <select>, visible option label]; ids differ per console
            sel, label = arg
            page.locator(expand(sel, ctx, lang)).first.select_option(label=expand(label, ctx, lang))
        elif verb == "wait":
            page.wait_for_selector(expand(arg, ctx, lang), timeout=30000)
        elif verb == "sleep":
            page.wait_for_timeout(int(arg))
        elif verb == "eval":
            page.evaluate(expand(arg, ctx, lang))  # placeholders ({env:…}) expand here too
        elif verb == "save_value":
            # [selector, file path]: keep a value the page shows only once (a reviewer link,
            # a one-time password; an <input>'s value, otherwise the element's text) so a
            # later step can use it; the file is written 0600
            sel, dest = arg
            out = Path(expand(dest, ctx, lang)).expanduser()
            el = page.locator(expand(sel, ctx, lang)).first
            is_field = el.evaluate(
                "e => e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement")
            out.write_text((el.input_value() if is_field else el.inner_text()).strip())
            out.chmod(0o600)
        elif verb == "expand_rail":
            # unfold every module group (what the reader sees after clicking each head)
            heads = page.locator('[data-testid^="v3-rail-group-"]')
            for i in range(heads.count()):
                if heads.nth(i).get_attribute("aria-expanded") != "true":
                    heads.nth(i).click()
        else:
            raise ValueError(f"unknown action {verb!r}")
        page.wait_for_timeout(250)


def union_rect(page: Page, sels: list[str]) -> dict[str, float] | None:
    """Document-coordinate rect of the union of every element the selectors match.

    Selectors are Playwright selectors, so `:has-text()` / `text=` work as well as CSS."""
    sx, sy = page.evaluate("() => [scrollX, scrollY]")
    rect: dict[str, float] | None = None
    for sel in sels:
        for loc in page.locator(sel).all():
            box = loc.bounding_box()
            if not box or not box["width"] or not box["height"]:
                continue
            r = {"x": box["x"] + sx, "y": box["y"] + sy, "w": box["width"], "h": box["height"]}
            rect = r if rect is None else _union(rect, r)
    return rect


def capture(pw: Any, shot: dict[str, Any], lang: str, ctx: Ctx, out: Path,
            probe: Path | None = None) -> Path:
    vw, vh = shot.get("viewport", [1440, 900])
    browser = pw.chromium.launch()
    try:
        context = browser.new_context(
            viewport={"width": vw, "height": vh}, device_scale_factor=shot.get("scale", 1.5),
            locale=LANGS[lang], color_scheme="dark", timezone_id="Asia/Shanghai")
        store = base_storage(lang, shot)
        # set once per tab, so what the app itself writes later survives navigation
        context.add_init_script(
            "(s => { if (sessionStorage.getItem('__ws')) return;"
            " sessionStorage.setItem('__ws', '1');"
            " for (const [k, v] of Object.entries(s)) v === null ? localStorage.removeItem(k)"
            " : localStorage.setItem(k, v); })(" + json.dumps(store) + ")")
        account = shot.get("account", "admin")
        if account != "anon":
            r = context.request.post(f"{ctx.url}/api/auth/login", data={
                "username": account, "password": password_for(ctx, account)})
            if r.status != 200:
                raise RuntimeError(f"login as {account}: HTTP {r.status}")
        page = context.new_page()
        page.goto(ctx.url + expand(shot["path"], ctx, lang), wait_until="networkidle")
        if shot.get("wait"):
            page.wait_for_selector(expand(shot["wait"], ctx, lang), timeout=30000)
        run_actions(page, shot.get("actions", []), ctx, lang)
        page.wait_for_timeout(int(shot.get("settle", 900)))
        page.evaluate("() => document.fonts && document.fonts.ready")
        if shot.get("hide"):
            page.add_style_tag(content=",".join(expand(s, ctx, lang) for s in shot["hide"])
                               + "{visibility:hidden!important}")
        if shot.get("mask"):
            page.add_style_tag(content=",".join(expand(s, ctx, lang) for s in shot["mask"])
                               + "{filter:blur(7px)!important}")
        if shot.get("unclamp", True):
            height = page.evaluate(UNCLAMP_JS)
            # a crop target that scrolls inside itself (the fixed rail) needs the
            # window to grow by whatever it hides
            if shot.get("clip"):
                hidden = page.evaluate(
                    "sels => Math.max(0, ...sels.flatMap(s => [...document.querySelectorAll(s)])"
                    ".map(e => e.scrollHeight - e.clientHeight))",
                    [s for s in _as_list(expand(shot["clip"], ctx, lang)) if ":" not in s])
                height = max(height, vh + hidden + 8 if hidden else 0)
            if height > vh:
                page.set_viewport_size({"width": vw, "height": min(height, 9000)})
                page.wait_for_timeout(400)
        page.mouse.move(vw - 2, vh - 2)
        if ctx.relabel:
            for src, dst in relabel_pairs(*ctx.relabel):
                page.evaluate(RELABEL_JS, {"from": src, "to": dst})
        if probe is not None:
            probe.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(probe / f"{shot['name']}.{lang}.png"), full_page=True)
            (probe / f"{shot['name']}.{lang}.txt").write_text(
                page.evaluate(PROBE_JS, shot.get("probe_root", "body")))
            return probe / f"{shot['name']}.{lang}.png"
        boxes = []
        for i, h in enumerate(shot.get("highlights", [])):
            sels = [expand(s, ctx, lang) for s in _as_list(h["box"])]
            rect = union_rect(page, sels)
            if rect is None:
                raise RuntimeError(f"highlight {i + 1} matched nothing: {sels}")
            boxes.append({"n": h.get("n"), "rect": rect, "pad": h.get("pad"),
                          "badge": h.get("badge")})
        drawn = page.evaluate(OVERLAY_JS, {"boxes": boxes, "pad": shot.get("box_pad", 4)})
        clip = None
        if shot.get("clip"):
            sels = [expand(s, ctx, lang) for s in _as_list(shot["clip"])]
            rect = union_rect(page, sels)
            if rect is None:
                raise RuntimeError(f"clip matched nothing: {sels}")
            m = shot.get("margin", 16)
            # frames and badges as drawn (a 'left' / 'right' / 'top' badge sits outside
            # its frame): the crop holds every one of them whole
            rect = crop_rect(rect, drawn)
            x, y = max(rect["x"] - m, 0), max(rect["y"] - m, 0)
            clip = {"x": x, "y": y,
                    "w": rect["x"] + rect["w"] + m - x, "h": rect["y"] + rect["h"] + m - y}
            width = page.evaluate("() => document.documentElement.scrollWidth")
            clip["w"] = min(clip["w"], width - x)
        area = ({"x": clip["x"], "y": clip["y"], "width": clip["w"], "height": clip["h"]}
                if clip else None)
        png = page.screenshot(full_page=True, clip=area, animations="disabled")
        target = out / f"{shot['name']}.{lang}.webp"
        Image.open(io.BytesIO(png)).convert("RGB").save(target, "WEBP", quality=88, method=6)
        return target
    finally:
        browser.close()


def _as_list(v: Any) -> list[str]:
    return v if isinstance(v, list) else [v]


def relabel_pairs(actual: str, shown: str) -> list[tuple[str, str]]:
    """What --display-region rewrites: the region itself (which also covers AZ names
    such as us-west-2a and ARNs) and the AZ-id prefix (usw2-az1 -> use1-az1)."""
    pairs = [(actual, shown)]
    a, s = _az_prefix(actual), _az_prefix(shown)
    if a and s and a != s:
        pairs.append((a + "-az", s + "-az"))
    return pairs


def _az_prefix(region: str) -> str | None:
    """us-east-1 -> use1, ap-southeast-2 -> apse2, eu-central-1 -> euc1."""
    m = re.fullmatch(r"([a-z]{2})-([a-z]+)-(\d+)", region)
    if not m:
        return None
    area, direction, n = m.groups()
    short = "".join(w[0] for w in re.findall(r"north|south|east|west|central", direction))
    return f"{area}{short}{n}" if short else None


def crop_rect(rect: dict[str, float], drawn: list[dict[str, float]]) -> dict[str, float]:
    """The crop target grown to every drawn highlight frame and badge."""
    for d in drawn:
        rect = _union(rect, d)
    return rect


def _union(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    x1, y1 = min(a["x"], b["x"]), min(a["y"], b["y"])
    x2, y2 = max(a["x"] + a["w"], b["x"] + b["w"]), max(a["y"] + a["h"], b["y"] + b["h"])
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


# ── report ───────────────────────────────────────────────────────────────────


def referenced(workshop: Path) -> set[str]:
    pat = re.compile(r"/static/images/([\w.-]+?)\.(?:zh|en)\.webp")
    names: set[str] = set()
    for md in (workshop / "content").rglob("*.md"):
        names.update(pat.findall(md.read_text()))
    return names


def report(shots: list[dict[str, Any]], workshop: Path) -> None:
    images = workshop / "static" / "images"
    on_disk = {p.name.rsplit(".", 2)[0] for p in images.glob("*.webp")}
    refs = referenced(workshop)
    listed = {s["name"] for s in shots}
    rows = {
        "auto (recaptured by this script)": sorted(s["name"] for s in shots if not s.get("manual")),
        "manual (kept, not a console page)": sorted(s["name"] for s in shots if s.get("manual")),
        "new (not on disk yet)": sorted(listed - on_disk),
        "on disk, not in the manifest": sorted(on_disk - listed),
        "on disk, no longer referenced": sorted(on_disk - refs),
        "referenced, missing on disk": sorted(refs - on_disk),
    }
    for title, names in rows.items():
        print(f"{title}: {len(names)}")
        for n in names if "auto" not in title else []:
            print(f"    {n}")


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--workshop", type=Path, required=True,
                   help="workshop content repo (images go to <it>/static/images)")
    p.add_argument("--manifest", type=Path, default=MANIFEST)
    p.add_argument("--only", nargs="*", default=["*"], help="shot name globs")
    p.add_argument("--lang", choices=sorted(LANGS), action="append")
    p.add_argument("--stack", default="agent-dlc-launchpad")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--report", action="store_true", help="only compare manifest, disk and content")
    p.add_argument("--assume-state", action="store_true",
                   help="the console is in the walk-through state the chosen shots' "
                        "`state:` requirements name (only the walker can say so)")
    p.add_argument("--probe", type=Path,
                   help="write a full-page PNG + element outline here instead of the crop")
    p.add_argument("--out", type=Path,
                   help="write the images here instead of <workshop>/static/images (a preview)")
    p.add_argument("--display-region",
                   help="the region the workshop runs in (e.g. us-east-1): when the walk "
                        "ran elsewhere, every occurrence of the console's region in the page "
                        "text (region chips, ARNs) is shown as this one")
    p.add_argument("--actual-region",
                   help="the console's own region, when it is not --region")
    args = p.parse_args()

    shots = yaml.safe_load(args.manifest.read_text())["shots"]
    names = [s["name"] for s in shots]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"duplicate shot names: {sorted(dupes)}")
    if args.report:
        report(shots, args.workshop)
        return 0
    chosen = [s for s in shots if any(fnmatch.fnmatch(s["name"], g) for g in args.only)]
    ctx = load_ctx(args.stack, args.region)
    ctx.assume_state = args.assume_state
    if args.display_region:
        actual = args.actual_region or args.region
        if actual != args.display_region:
            ctx.relabel = (actual, args.display_region)
    ctx.workshop = args.workshop
    discover(ctx)
    out = args.out or args.workshop / "static" / "images"
    out.mkdir(parents=True, exist_ok=True)
    tally = {"OK": 0, "SKIP": 0, "MANUAL": 0, "FAIL": 0}
    with sync_playwright() as pw:
        for shot in chosen:
            if shot.get("manual"):
                tally["MANUAL"] += 1
                print(f"MANUAL {shot['name']:<40} {shot['manual']}")
                continue
            try:
                check_requires(shot, ctx)
            except SkipShot as why:
                tally["SKIP"] += 1
                print(f"SKIP   {shot['name']:<40} {why}")
                continue
            for lang in args.lang or shot.get("langs", list(LANGS)):
                started = time.monotonic()
                try:
                    try:
                        path = capture(pw, shot, lang, ctx, out, args.probe)
                    except SkipShot:
                        raise
                    except Exception as first:  # noqa: BLE001 - one retry for a slow page
                        why = str(first).splitlines()[0][:120]
                        print(f"RETRY  {shot['name']:<40} {lang} {why}")
                        path = capture(pw, shot, lang, ctx, out, args.probe)
                    tally["OK"] += 1
                    print(f"OK     {shot['name']:<40} {lang} {path.name} "
                          f"{time.monotonic() - started:.0f}s")
                except SkipShot as why:
                    tally["SKIP"] += 1
                    print(f"SKIP   {shot['name']:<40} {lang} {why}")
                except Exception as exc:  # noqa: BLE001 - one bad shot must not stop the run
                    tally["FAIL"] += 1
                    print(f"FAIL   {shot['name']:<40} {lang} {str(exc).splitlines()[0][:200]}")
    print(" · ".join(f"{k} {v}" for k, v in tally.items()))
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
