"""The workshop screenshot recapture script: its pure parts and its manifest.

The capture itself drives a real console and is run by hand; these tests pin what
decides *which* shots run and how a manifest entry is read.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "scripts" / "capture_workshop_screens.py"
MANIFEST = ROOT / "docs" / "workshop-screens.yaml"


def _load():
    spec = importlib.util.spec_from_file_location("capture_workshop_screens", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


cws = _load()


def _ctx():
    return cws.Ctx(url="https://console.example", passwords={},
                   agents={"lab-service-assistant": "a1"}, users={"biz-owner": "u1"},
                   facts={"lab-service-assistant": {"criteria_signed": True, "api_keys": 0}})


def test_page_helpers_load_as_bare_functions():
    for name in ("OVERLAY_JS", "UNCLAMP_JS", "PROBE_JS"):
        js = getattr(cws, name)
        assert js.lstrip().startswith("("), name
        assert "// @" not in js


def test_per_lang_picks_the_language_and_falls_back_to_english():
    assert cws.per_lang({"zh": "登录", "en": "Sign in"}, "zh") == "登录"
    assert cws.per_lang({"en": "Sign in"}, "zh") == "Sign in"
    assert cws.per_lang("plain", "zh") == "plain"
    # a dict that is not a language pair is passed through
    assert cws.per_lang({"n": 1}, "zh") == {"n": 1}


def test_expand_names_to_ids_and_skips_a_missing_name():
    ctx = _ctx()
    path = cws.expand("/v3/agents?id={agent:lab-service-assistant}", ctx, "zh")
    assert path == "/v3/agents?id=a1"
    with pytest.raises(cws.SkipShot):
        cws.expand("/v3/agents?id={agent:nope}", ctx, "zh")


@pytest.mark.parametrize("requires, skips", [
    (["agent:lab-service-assistant"], False),
    (["agent:lab-policy-advisor"], True),
    (["user:biz-owner"], False),
    (["fact:lab-service-assistant.criteria_signed"], False),
    (["fact:lab-service-assistant.criteria_signed=true"], False),
    (["fact:lab-service-assistant.api_keys"], True),
    (["state:a sealed golden set"], True),
])
def test_requires(requires, skips):
    shot = {"name": "x", "requires": requires}
    if skips:
        with pytest.raises(cws.SkipShot):
            cws.check_requires(shot, _ctx())
    else:
        cws.check_requires(shot, _ctx())


def test_base_storage_defaults_and_overrides():
    store = cws.base_storage("en", {"storage": {"lp.v3.create.chooser": None}})
    assert store["launchpad_v3_theme"] == "dark"
    assert store["i18nextLng"] == "en"
    assert store["launchpad_v3_tour_done"] == "1"
    assert store["lp.v3.create.chooser"] is None


def test_manifest_is_well_formed():
    shots = yaml.safe_load(MANIFEST.read_text())["shots"]
    names = [s["name"] for s in shots]
    assert len(names) == len(set(names))
    accounts = set(cws.PASSWORD_OUTPUTS) | {"anon"}
    for shot in shots:
        assert shot["status"] in {"keep", "replace", "new"}, shot["name"]
        if shot.get("manual"):
            continue
        # an account the walk itself created signs in from LAUNCHPAD_WS_PASSWORD_<ACCOUNT>
        assert shot["account"] in accounts or shot.get("walk_account"), shot["name"]
        # a path is one string, or one per language ({zh: ..., en: ...})
        paths = shot["path"].values() if isinstance(shot["path"], dict) else [shot["path"]]
        assert all(p.startswith("/") for p in paths), shot["name"]
        numbers = [h["n"] for h in shot.get("highlights", []) if h.get("n") is not None]
        # badges follow the caption: 1, 2, 3 ... with no gap
        assert numbers == list(range(1, len(numbers) + 1)), shot["name"]


def test_assume_state_satisfies_state_but_not_missing_agents():
    ctx = _ctx()
    ctx.assume_state = True
    cws.check_requires({"name": "x", "requires": ["state:a sealed golden set"]}, ctx)
    with pytest.raises(cws.SkipShot):
        cws.check_requires({"name": "x", "requires": ["state:fresh", "agent:nope"]}, ctx)
