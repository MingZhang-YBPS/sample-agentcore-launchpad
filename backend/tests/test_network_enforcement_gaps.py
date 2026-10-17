"""The enforcement panel's "not enforceable" notes are localized by a stable code.

The server keeps the English text for API callers and sends index-aligned codes; every
code needs console copy in both locales, or the zh console shows raw English again.
"""

import json
from pathlib import Path

from app.services.network_enforcement import NOT_ENFORCEABLE

LOCALES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "locales"


def test_codes_are_unique_and_texts_kept() -> None:
    codes = [code for code, _ in NOT_ENFORCEABLE]
    assert len(codes) == len(set(codes))
    assert all(text for _, text in NOT_ENFORCEABLE)


def test_every_code_has_console_copy_in_both_locales() -> None:
    for lang in ("en", "zh-CN"):
        gap = json.loads((LOCALES / lang / "common.json").read_text(encoding="utf-8"))[
            "v3"]["network"]["enforce"]["gap"]
        for code, _ in NOT_ENFORCEABLE:
            assert gap.get(code), f"{lang}: v3.network.enforce.gap.{code} is missing"
