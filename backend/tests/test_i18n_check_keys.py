"""scripts/i18n_check.py: a static t("…") key with no locale entry fails the gate.

Regression: the V3 project overview rendered the raw strings
`v3.projects.overview.usage` / `v3.projects.overview.noQuota` because the keys were
never added, and the parity check (en ↔ zh-CN) cannot see a key missing from both.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "i18n_check.py"


@pytest.fixture()
def i18n(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("i18n_check_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    src = tmp_path / "src"
    (src / "locales").mkdir(parents=True)
    monkeypatch.setattr(mod, "SRC", src)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod, src


def _write(src: Path, body: str) -> None:
    (src / "Page.tsx").write_text(body, encoding="utf-8")


def test_missing_key_fails(i18n, capsys):
    mod, src = i18n
    _write(src, 'const a = t("v3.projects.overview.usage");\n')
    assert mod.missing_key_check({"v3.projects.overview.members"}) is True
    assert "Page.tsx:1: v3.projects.overview.usage" in capsys.readouterr().out


def test_existing_key_passes(i18n):
    mod, src = i18n
    _write(src, 'const a = t("v3.projects.overview.usage");\n')
    assert mod.missing_key_check({"v3.projects.overview.usage"}) is False


def test_plural_and_context_suffixes_resolve(i18n):
    mod, src = i18n
    _write(
        src,
        't("v3.registry.batch.confirmTitle", { context: a, count: n });\n'
        't("v3.users.days", { count: 2 });\n',
    )
    keys = {"v3.registry.batch.confirmTitle_approve_one", "v3.users.days_other"}
    assert mod.missing_key_check(keys) is False


def test_dynamic_and_undotted_keys_are_skipped(i18n):
    mod, src = i18n
    _write(src, 't(`v3.x.${kind}`); t("plain"); t("a.b");\n')
    assert mod.missing_key_check({"a.b"}) is False


def test_locale_sources_are_not_scanned(i18n):
    mod, src = i18n
    (src / "locales" / "keys.ts").write_text('t("nowhere.at.all")\n', encoding="utf-8")
    assert mod.missing_key_check(set()) is False
