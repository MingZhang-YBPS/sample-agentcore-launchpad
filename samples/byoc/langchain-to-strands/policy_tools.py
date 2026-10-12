"""Unicorn Air fee and baggage rules as two tools.

This module is the point of the sample: **it does not change** when the agent
moves from LangChain to Strands. Only the decorator import at the bottom of the
file differs, and the two framework entrypoints import from here, so the
business logic is written once.

Every number comes from the workshop's policy sheet (`samples/kb_docs`,
rules `A-02`…`A-15`); all of it is fictional. Keep the worked example in
`refund_quote` matching rule `A-07`: Standard, fare 1200, 72 h before
departure -> fee 300, taxes 80, refund 980.
"""

from typing import Any

# A-01 fare families, keyed by the value the model is told to pass.
FAMILIES = ("lite", "standard", "flex", "business", "business_flex")

# A-02 / A-03 fee as a percentage of the fare (taxes excluded), per time window.
# W1 = 168 h or more before departure, W2 = under 168 h and 4 h or more,
# W3 = under 4 h, after departure or a no-show. None = not allowed.
CHANGE_FEE_PCT: dict[str, tuple[float | None, float | None, float | None]] = {
    "lite": (20, 40, None),
    "standard": (5, 15, 30),
    "flex": (0, 0, 10),
    "business": (0, 10, 20),
    "business_flex": (0, 0, 0),
}
REFUND_FEE_PCT: dict[str, tuple[float | None, float | None, float | None]] = {
    "lite": (None, None, None),  # fare not refundable, taxes still are (A-06)
    "standard": (10, 25, 50),
    "flex": (0, 5, 10),
    "business": (5, 15, 30),
    "business_flex": (0, 0, 5),
}

MIN_FEE_CNY = 50  # A-04: a non-zero fee is at least this much
TAXES_CNY = 50 + 30  # A-06: airport construction + fuel surcharge, per segment

# A-12 free checked baggage, A-13 carry-on, in kilograms.
FREE_CHECKED_KG = {"lite": 0, "standard": 20, "flex": 25, "business": 30, "business_flex": 30}
CARRY_ON = {
    "lite": "1 件 ≤ 7 kg（20×40×55 cm）+ 1 个小件个人物品",
    "standard": "1 件 ≤ 7 kg（20×40×55 cm）+ 1 个小件个人物品",
    "flex": "1 件 ≤ 7 kg（20×40×55 cm）+ 1 个小件个人物品",
    "business": "2 件，每件 ≤ 8 kg",
    "business_flex": "2 件，每件 ≤ 8 kg",
}
EXCESS_PER_KG_CNY = 20  # A-15
MAX_PIECE_KG = 32  # A-15
LITE_PAID_BAG_CNY = {"online": 120, "airport": 200}  # A-14, per 20 kg


def _window(hours_to_departure: float) -> int:
    """W1 / W2 / W3 as 1 / 2 / 3 (A-02 note)."""
    if hours_to_departure >= 168:
        return 1
    if hours_to_departure >= 4:
        return 2
    return 3


def _normalize(fare_family: str) -> str:
    key = fare_family.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "轻享": "lite", "标准": "standard", "灵活": "flex",
        "公务标准": "business", "公务灵活": "business_flex",
        "economy_lite": "lite", "economy_standard": "standard", "economy_flex": "flex",
    }
    key = aliases.get(key, key)
    if key not in FAMILIES:
        raise ValueError(
            f"unknown fare family {fare_family!r}; expected one of {', '.join(FAMILIES)}"
        )
    return key


def _fee(pct: float | None, fare_cny: float) -> float | None:
    if pct is None:
        return None
    if pct == 0:
        return 0.0
    return round(max(fare_cny * pct / 100, MIN_FEE_CNY), 2)


def refund_quote(
    fare_family: str,
    fare_cny: float,
    hours_to_departure: float,
    action: str = "refund",
    involuntary: bool = False,
) -> dict[str, Any]:
    """Quote a Unicorn Air change or refund for one domestic segment.

    Args:
        fare_family: lite, standard, flex, business or business_flex.
        fare_cny: the fare in CNY, taxes excluded.
        hours_to_departure: hours until scheduled departure; 0 or less after it.
        action: "refund" or "change".
        involuntary: True when the airline cancelled, rescheduled, or delayed
            the flight by 3 hours or more.

    Returns the time window, the fee percentage, the fee, the taxes refunded and
    the amount the passenger gets back (refund) or still owes (change).
    """
    family = _normalize(fare_family)
    if action not in ("refund", "change"):
        raise ValueError('action must be "refund" or "change"')
    window = _window(hours_to_departure)

    if involuntary:  # A-08 overrides every family and window
        return {
            "fare_family": family, "window": "involuntary", "fee_pct": 0, "fee_cny": 0,
            "taxes_refunded_cny": TAXES_CNY if action == "refund" else 0,
            "total_refund_cny": round(fare_cny + TAXES_CNY, 2) if action == "refund" else 0,
            "rule": "A-08",
            "note": "航班取消、延误 3 小时以上或航司变更航班：免费改期或全额退票（含税费）。",
        }

    table = REFUND_FEE_PCT if action == "refund" else CHANGE_FEE_PCT
    pct = table[family][window - 1]
    fee = _fee(pct, fare_cny)

    if action == "refund":
        if fee is None:  # Lite: fare not refundable, taxes are (A-03 + A-06)
            return {
                "fare_family": family, "window": f"W{window}", "fee_pct": None, "fee_cny": None,
                "taxes_refunded_cny": TAXES_CNY, "total_refund_cny": TAXES_CNY,
                "rule": "A-03 + A-06", "note": "轻享票面不可退，税费仍全额退还。",
            }
        return {
            "fare_family": family, "window": f"W{window}", "fee_pct": pct, "fee_cny": fee,
            "taxes_refunded_cny": TAXES_CNY,
            "total_refund_cny": round(fare_cny - fee + TAXES_CNY, 2),
            "rule": "A-03 + A-04 + A-06",
            "note": "退款 = 票面价 − 手续费 + 税费；手续费不为零时最低 ¥50。",
        }

    if fee is None:
        return {
            "fare_family": family, "window": f"W{window}", "fee_pct": None, "fee_cny": None,
            "total_refund_cny": 0, "rule": "A-02",
            "note": "轻享在起飞前 4 小时内或起飞后不允许改期。",
        }
    return {
        "fare_family": family, "window": f"W{window}", "fee_pct": pct, "fee_cny": fee,
        "taxes_refunded_cny": 0, "fare_difference_cny": "另计，新票价更高时需补差价",
        "rule": "A-02 + A-04 + A-05",
        "note": "改期费用 = 手续费 + 票价差额；手续费不为零时最低 ¥50。",
    }


def baggage_allowance(fare_family: str) -> dict[str, Any]:
    """Return the Unicorn Air baggage allowance for a fare family.

    Args:
        fare_family: lite, standard, flex, business or business_flex.

    Returns the free checked allowance, the carry-on rule, the excess rate and,
    for Lite, the price of a paid checked bag.
    """
    family = _normalize(fare_family)
    out: dict[str, Any] = {
        "fare_family": family,
        "free_checked_kg": FREE_CHECKED_KG[family],
        "carry_on": CARRY_ON[family],
        "excess_per_kg_cny": EXCESS_PER_KG_CNY,
        "max_piece_kg": MAX_PIECE_KG,
        "rule": "A-12 + A-13 + A-15",
    }
    if family == "lite":
        out["paid_checked_bag_cny"] = LITE_PAID_BAG_CNY
        out["rule"] += " + A-14"
        out["note"] = (
            "轻享无免费托运额；起飞前 24 小时以上线上购买 ¥120/20 kg，机场柜台 ¥200/20 kg。"
        )
    return out


SYSTEM_PROMPT = (
    "你是独角兽航空的退改与行李规则助手。涉及金额、费率、时限或重量时，"
    "必须调用工具计算，不要自己估算。回答时说明用到的规则编号（如 A-03），"
    "并列出计算过程。无法用工具得到的信息，直接说不知道。"
)

# The worked example from rule A-07, used by the smoke test in README.md.
if __name__ == "__main__":
    import json

    print(json.dumps(refund_quote("standard", 1200, 72), ensure_ascii=False, indent=2))
    print(json.dumps(baggage_allowance("lite"), ensure_ascii=False, indent=2))
