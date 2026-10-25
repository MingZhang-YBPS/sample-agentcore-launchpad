"""Agent-DLC statistics, checked against the handbook's worked examples."""

import pytest

from app.evaluation import stats


def test_wilson_interval_matches_the_handbook_example():
    # 92% on 200 items is 87.4–95.0% (±3.8pp)
    low, high = stats.wilson_interval(184, 200)
    assert low == pytest.approx(0.874, abs=0.002)
    assert high == pytest.approx(0.950, abs=0.002)


def test_wilson_edges():
    assert stats.wilson_interval(0, 0) == (0.0, 0.0)
    low, high = stats.wilson_interval(0, 10)
    assert low == 0.0 and 0 < high < 0.35
    low, high = stats.wilson_interval(10, 10)
    assert high == 1.0 and low > 0.65
    with pytest.raises(ValueError):
        stats.wilson_interval(11, 10)


def test_a_threshold_inside_the_interval_is_flagged():
    assert stats.threshold_inside_interval(184, 200, 0.90) is True
    assert stats.threshold_inside_interval(184, 200, 0.97) is False


def test_compound_rate_of_eight_gates_at_95_percent():
    assert stats.compound_pass_rate([0.95] * 8) == pytest.approx(0.663, abs=0.001)
    assert stats.compound_pass_rate([0.95] * 16) == pytest.approx(0.440, abs=0.001)
    # eight gates need ~98.7% each to clear 90% overall
    assert stats.per_criterion_rate_for(0.90, 8) == pytest.approx(0.987, abs=0.001)


def test_sample_size_for_five_points_at_ninety_percent():
    n = stats.two_proportion_sample_size(0.90, 0.05)
    assert 420 <= n <= 445  # handbook quotes ≈432 per arm
    # smaller effects need far more traffic
    assert stats.two_proportion_sample_size(0.90, 0.02) > 3000


def test_two_proportion_p_value():
    # the handbook: on 200 items a 5pp lift is not significant (p ≈ 0.07)
    p = stats.two_proportion_p_value(184, 200, 174, 200)
    assert 0.05 < p < 0.12
    assert stats.two_proportion_p_value(1, 0, 1, 1) is None


def test_pass_k_versus_mean_k():
    attempts = {
        "a": [True, True, True],
        "b": [True, False, True],
        "c": [False, False, False],
        "d": [True, True, True],
    }
    out = stats.pass_k(attempts)
    assert out["k"] == 3
    assert out["pass_k"] == pytest.approx(0.5)
    assert out["mean_k"] == pytest.approx(8 / 12)
    assert out["pass_at_k"] == pytest.approx(0.75)
    assert out["gap"] == pytest.approx(8 / 12 - 0.5)
    majority = stats.pass_k(attempts, mode="majority")
    assert majority["pass_k"] == pytest.approx(0.75)


def test_pass_k_of_nothing():
    assert stats.pass_k({})["pass_k"] is None


def test_a_rubber_stamp_judge_has_kappa_zero_despite_80_percent_accuracy():
    human = ["pass"] * 80 + ["fail"] * 20
    judge = ["pass"] * 100
    accuracy = sum(1 for h, j in zip(human, judge, strict=True) if h == j) / 100
    assert accuracy == 0.8
    assert stats.cohen_kappa(judge, human) == pytest.approx(0.0)
    assert stats.kappa_band(0.0) == "none"


def test_kappa_perfect_and_inverted():
    a = ["pass", "fail", "pass", "fail"]
    assert stats.cohen_kappa(a, a) == pytest.approx(1.0)
    inverted = ["fail", "pass", "fail", "pass"]
    assert stats.cohen_kappa(a, inverted) == pytest.approx(-1.0)
    assert stats.kappa_band(-0.3) == "inverted"
    assert stats.kappa_band(0.85) == "almost_perfect"
    assert stats.kappa_band(0.7) == "substantial"
    assert stats.kappa_band(None) == "insufficient"


def test_kappa_bootstrap_is_reproducible_and_brackets_the_point():
    a = ["pass"] * 12 + ["fail"] * 8
    b = ["pass"] * 10 + ["fail"] * 2 + ["fail"] * 6 + ["pass"] * 2
    point = stats.cohen_kappa(a, b)
    ci = stats.kappa_bootstrap_ci(a, b)
    assert ci == stats.kappa_bootstrap_ci(a, b)
    assert ci[0] <= point <= ci[1]


def test_fleiss_kappa_three_raters():
    rows = [["p", "p", "p"], ["f", "f", "f"], ["p", "p", "f"], ["f", "f", "p"]]
    value = stats.fleiss_kappa(rows)
    assert 0 < value < 1
    with pytest.raises(ValueError):
        stats.fleiss_kappa([["p", "p"], ["p"]])


def test_confusion_cells():
    cells = stats.confusion(["pass", "pass", "fail"], ["pass", "fail", "fail"])
    assert cells == {"pass/pass": 1, "pass/fail": 1, "fail/fail": 1}


def test_tvd_names_the_component_that_moved():
    before = {"returns": 8, "orders": 60, "billing": 32}
    after = {"returns": 21, "orders": 50, "billing": 29}
    assert stats.total_variation_distance(before, after) == pytest.approx(0.13, abs=0.001)
    name, was, now = stats.largest_shift(before, after)
    assert name == "returns" and was == pytest.approx(0.08) and now == pytest.approx(0.21)


def test_rolling_median():
    assert stats.rolling_median([3, 1, 2]) == 2
    assert stats.rolling_median([4, 1, 2, 3]) == 2.5
    assert stats.rolling_median([]) is None
