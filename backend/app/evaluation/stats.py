"""Statistics the Agent-DLC gate and workbenches rely on — pure functions only.

Everything here is deterministic and dependency-free so the gate report it feeds is
reproducible: the same results always give the same verdict. The numbers follow the
Agent-DLC handbook's conventions:

* **Pass rates carry a Wilson interval** — 92% on 200 items is 87.4–95.0%, and a
  threshold inside that interval is "not distinguishable at this n".
* **Judges are calibrated with Cohen's κ, never accuracy** — a judge that always says
  "pass" on an 80%-pass set scores 80% accuracy and κ = 0.
* **Reliability is pass^k, not pass@k** — the share of scenarios that pass on *every*
  attempt is what a user who asks twice experiences.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Iterable, Sequence

Z_95 = 1.959963984540054


def wilson_interval(passes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion; (0, 0) when n == 0."""
    if n <= 0:
        return 0.0, 0.0
    if not 0 <= passes <= n:
        raise ValueError("passes must be between 0 and n")
    p = passes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    low = 0.0 if passes == 0 else max(0.0, centre - half)
    high = 1.0 if passes == n else min(1.0, centre + half)
    return low, high


def threshold_inside_interval(passes: int, n: int, threshold: float) -> bool:
    """True when the threshold cannot be told apart from the observed rate at this n."""
    low, high = wilson_interval(passes, n)
    return n > 0 and low <= threshold <= high


def compound_pass_rate(rates: Iterable[float]) -> float:
    """Share of sessions expected to clear every criterion (independence assumed)."""
    out = 1.0
    for rate in rates:
        out *= rate
    return out


def per_criterion_rate_for(target_overall: float, criteria: int) -> float:
    """The per-criterion rate needed for `criteria` gates to clear `target_overall`."""
    if criteria <= 0:
        return 1.0
    return target_overall ** (1.0 / criteria)


def two_proportion_sample_size(
    baseline: float, delta: float, *, alpha: float = 0.05, power: float = 0.8
) -> int:
    """Sessions per arm to detect an absolute change `delta` from `baseline`.

    Normal approximation for a two-sided two-proportion test (the figure the handbook
    quotes: baseline 90%, Δ 5pp ⇒ ≈ 432 per arm at α 0.05, power 0.8 — this formula
    gives 435 with the continuity-free pooled variance, within rounding).
    """
    if delta == 0:
        raise ValueError("delta must be non-zero")
    p1 = baseline
    p2 = min(max(baseline + delta, 0.0), 1.0)
    z_a = _norm_ppf(1 - alpha / 2)
    z_b = _norm_ppf(power)
    p_bar = (p1 + p2) / 2
    num = (
        z_a * math.sqrt(2 * p_bar * (1 - p_bar))
        + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
    ) ** 2
    return math.ceil(num / (p2 - p1) ** 2)


def two_proportion_p_value(pass_a: int, n_a: int, pass_b: int, n_b: int) -> float | None:
    """Two-sided p-value of a pooled z-test for p_a == p_b; None when undefined."""
    if n_a <= 0 or n_b <= 0:
        return None
    p_pool = (pass_a + pass_b) / (n_a + n_b)
    var = p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b)
    if var <= 0:
        return 1.0 if pass_a / n_a == pass_b / n_b else 0.0
    z = (pass_a / n_a - pass_b / n_b) / math.sqrt(var)
    return 2 * (1 - _norm_cdf(abs(z)))


# ── pass^k ──────────────────────────────────────────────────────────────────────


def pass_k(attempts_by_scenario: dict[str, Sequence[bool]], mode: str = "all") -> dict:
    """pass^k, mean@k and pass@k over repeated attempts of each scenario.

    `mode="all"` counts a scenario as passing only when every attempt passed (the default
    for unattended or irreversible actions); `"majority"` when more than half did.
    """
    scenarios = {k: list(v) for k, v in attempts_by_scenario.items() if len(v) > 0}
    if not scenarios:
        return {"scenarios": 0, "pass_k": None, "mean_k": None, "pass_at_k": None, "gap": None}
    k_values = {len(v) for v in scenarios.values()}
    strict = 0
    any_pass = 0
    total_attempts = 0
    total_passes = 0
    for attempts in scenarios.values():
        passes = sum(1 for a in attempts if a)
        total_attempts += len(attempts)
        total_passes += passes
        any_pass += 1 if passes else 0
        if mode == "majority":
            strict += 1 if passes * 2 > len(attempts) else 0
        else:
            strict += 1 if passes == len(attempts) else 0
    n = len(scenarios)
    rate_k = strict / n
    mean_k = total_passes / total_attempts
    return {
        "scenarios": n,
        "k": max(k_values) if len(k_values) == 1 else None,
        "mode": mode,
        "pass_k": rate_k,
        "mean_k": mean_k,
        "pass_at_k": any_pass / n,
        # how much of the average is luck: large ⇒ fix consistency before capability
        "gap": mean_k - rate_k,
    }


# ── agreement (judge calibration) ──────────────────────────────────────────────


def cohen_kappa(labels_a: Sequence[str], labels_b: Sequence[str]) -> float | None:
    """Cohen's κ for two raters over the same items; None when undefined."""
    if len(labels_a) != len(labels_b):
        raise ValueError("both raters must label the same items")
    n = len(labels_a)
    if n == 0:
        return None
    observed = sum(1 for a, b in zip(labels_a, labels_b, strict=True) if a == b) / n
    freq_a = Counter(labels_a)
    freq_b = Counter(labels_b)
    expected = sum(freq_a[c] * freq_b[c] for c in set(freq_a) | set(freq_b)) / (n * n)
    if expected >= 1.0:
        # both raters used a single identical label throughout: agreement is total but
        # carries no information about discrimination
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def kappa_bootstrap_ci(
    labels_a: Sequence[str],
    labels_b: Sequence[str],
    *,
    iterations: int = 1000,
    seed: int = 7,
) -> tuple[float, float] | None:
    """Percentile bootstrap 95% CI for Cohen's κ (seeded, so reports are reproducible)."""
    n = len(labels_a)
    if n < 2:
        return None
    rng = random.Random(seed)
    pairs = list(zip(labels_a, labels_b, strict=True))
    samples: list[float] = []
    for _ in range(iterations):
        draw = [pairs[rng.randrange(n)] for _ in range(n)]
        value = cohen_kappa([a for a, _ in draw], [b for _, b in draw])
        if value is not None:
            samples.append(value)
    if not samples:
        return None
    samples.sort()
    lo = samples[int(0.025 * (len(samples) - 1))]
    hi = samples[int(0.975 * (len(samples) - 1))]
    return lo, hi


def fleiss_kappa(ratings: Sequence[Sequence[str]]) -> float | None:
    """Fleiss' κ: each row is one item's labels from every rater (equal rater counts)."""
    rows = [list(r) for r in ratings if r]
    if not rows:
        return None
    m = len(rows[0])
    if m < 2 or any(len(r) != m for r in rows):
        raise ValueError("every item needs the same number (≥2) of ratings")
    categories = sorted({label for row in rows for label in row})
    n = len(rows)
    p_items = []
    totals: Counter[str] = Counter()
    for row in rows:
        counts = Counter(row)
        totals.update(counts)
        p_items.append((sum(c * c for c in counts.values()) - m) / (m * (m - 1)))
    p_bar = sum(p_items) / n
    p_e = sum((totals[c] / (n * m)) ** 2 for c in categories)
    if p_e >= 1.0:
        return 1.0 if p_bar == 1.0 else 0.0
    return (p_bar - p_e) / (1 - p_e)


def confusion(labels_judge: Sequence[str], labels_human: Sequence[str]) -> dict[str, int]:
    """`"<judge>/<human>" → count`, the cells of the calibration confusion matrix."""
    out: Counter[str] = Counter()
    for j, h in zip(labels_judge, labels_human, strict=True):
        out[f"{j}/{h}"] += 1
    return dict(out)


def kappa_band(kappa: float | None) -> str:
    """Landis & Koch band with the handbook's gating consequence."""
    if kappa is None:
        return "insufficient"
    if kappa < 0:
        return "inverted"  # the rubric is probably written backwards
    if kappa <= 0.20:
        return "none"
    if kappa <= 0.40:
        return "fair"
    if kappa <= 0.60:
        return "moderate"
    if kappa < 0.80:
        return "substantial"
    return "almost_perfect"


# ── drift ──────────────────────────────────────────────────────────────────────


def total_variation_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """TVD between two categorical distributions given as counts or shares."""
    sa = sum(a.values()) or 1.0
    sb = sum(b.values()) or 1.0
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0.0) / sa - b.get(k, 0.0) / sb) for k in keys)


def largest_shift(a: dict[str, float], b: dict[str, float]) -> tuple[str, float, float] | None:
    """The category whose share moved the most: (name, share_before, share_after)."""
    sa = sum(a.values()) or 1.0
    sb = sum(b.values()) or 1.0
    best = None
    for key in sorted(set(a) | set(b)):
        before, after = a.get(key, 0.0) / sa, b.get(key, 0.0) / sb
        if best is None:
            best = (key, before, after)
            continue
        moved, best_moved = abs(after - before), abs(best[2] - best[1])
        # ties go to the component that grew: "returns 8% → 21%" is the actionable
        # half of the pair, its complement falling is the same fact restated
        if moved > best_moved or (
            moved == best_moved and (after - before) > (best[2] - best[1])
        ):
            best = (key, before, after)
    return best


def rolling_median(values: Sequence[float]) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else (clean[mid - 1] + clean[mid]) / 2


# ── normal distribution helpers (no scipy dependency) ─────────────────────────


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |ε| < 1.2e-9)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    low, high = 0.02425, 1 - 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )
