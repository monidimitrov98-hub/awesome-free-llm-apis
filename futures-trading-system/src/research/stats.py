"""Statistical safeguards for strategy research (Sprint 11).

Every function states what it assumes. Nothing here decides that a strategy "works": the evaluation
report turns these numbers into an evidence label, and a HUMAN decides on promotion.

* Inference unit = the TRADING DAY (daily net P&L), not the trade: trades of one day share regime, news and
  account state, so treating them as independent makes intervals falsely narrow.
* One-sided test of H0 "mean daily net P&L <= 0" (Student t on daily P&L) -> raw p-value.
* Multiple testing: every configuration ever attempted in an experiment family counts (failures included).
  Holm-Bonferroni (family-wise error) and Benjamini-Hochberg (false discovery rate) adjust the p-values; the
  report uses Holm (the stricter) for its evidence label and shows both.
* Power: the number of trading days needed to detect the OBSERVED daily effect size with 80 % power at the
  adjusted significance level. It is information for the human, not a pass mark.
* Sharpe / Sortino: only with >= 20 trading days (same rule as the shadow session report), annualised with
  sqrt(252), zero risk-free rate, on a stated capital base.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import numpy as np
from scipy import stats as st

MIN_DAYS_FOR_RATIOS = 20
POWER = 0.80
ALPHA = 0.05


def finite(values: Iterable[Any]) -> np.ndarray:
    arr = np.asarray([float(v) for v in values if v is not None], dtype=float)
    return arr[np.isfinite(arr)]


def one_sided_p(daily: Sequence[float]) -> float | None:
    """p-value of H0: mean <= 0 against H1: mean > 0 (Student t, daily observations). None if n < 2 or
    the sample has no variance (a constant sample proves nothing about the mean)."""
    x = finite(daily)
    n = len(x)
    if n < 2:
        return None
    sd = x.std(ddof=1)
    if not math.isfinite(sd) or sd <= 0:
        return None
    t = x.mean() / (sd / math.sqrt(n))
    return float(st.t.sf(t, n - 1))


def holm(pvalues: Sequence[float | None]) -> list[float | None]:
    """Holm-Bonferroni adjusted p-values (strong family-wise error control). None entries (untestable
    configurations) still COUNT in the family size m (they were attempted) and stay None."""
    m = len(pvalues)
    idx = sorted((i for i, p in enumerate(pvalues) if p is not None), key=lambda i: pvalues[i])
    out: list[float | None] = [None] * m
    running = 0.0
    for rank, i in enumerate(idx):
        adj = min(1.0, (m - rank) * float(pvalues[i]))
        running = max(running, adj)
        out[i] = running
    return out


def benjamini_hochberg(pvalues: Sequence[float | None]) -> list[float | None]:
    """Benjamini-Hochberg adjusted p-values (false discovery rate). m counts every attempt."""
    m = len(pvalues)
    idx = sorted((i for i, p in enumerate(pvalues) if p is not None), key=lambda i: pvalues[i])
    out: list[float | None] = [None] * m
    prev = 1.0
    for rank in range(len(idx) - 1, -1, -1):
        i = idx[rank]
        adj = min(prev, float(pvalues[i]) * m / (rank + 1), 1.0)
        prev = adj
        out[i] = adj
    return out


def required_days(daily: Sequence[float], alpha: float = ALPHA, power: float = POWER) -> int | None:
    """Trading days needed to detect the observed mean daily P&L (effect size d = mean / sd) with ``power``
    at one-sided level ``alpha`` (normal approximation). None if the observed effect is <= 0 or undefined."""
    x = finite(daily)
    if len(x) < 2:
        return None
    sd = x.std(ddof=1)
    if not math.isfinite(sd) or sd <= 0 or x.mean() <= 0:
        return None
    d = x.mean() / sd
    z = st.norm.ppf(1 - alpha) + st.norm.ppf(power)
    return int(math.ceil((z / d) ** 2))


def ratios(daily: Sequence[float], capital: float | None) -> dict[str, Any]:
    x = finite(daily)
    n = len(x)
    if n < MIN_DAYS_FOR_RATIOS or not capital:
        return {"sharpe": None, "sortino": None,
                "note": f"not meaningful: {n} trading day(s) (needs >= {MIN_DAYS_FOR_RATIOS}) "
                        + ("" if capital else "and a known account size")}
    r = x / capital
    sd = r.std(ddof=1)
    dsd = math.sqrt(float(np.sum(np.minimum(r, 0.0) ** 2)) / (n - 1))
    return {"sharpe": float(r.mean() / sd * math.sqrt(252)) if sd > 0 else None,
            "sortino": float(r.mean() / dsd * math.sqrt(252)) if dsd > 0 else None,
            "note": f"annualised from {n} daily returns on {capital:,.0f} (sqrt(252)); zero risk-free rate; "
                    "days without trades are not included"}


def day_bootstrap_mean(daily: Sequence[float], n_boot: int = 5000, seed: int = 11, conf: float = 0.95
                       ) -> tuple[float, float] | None:
    """Percentile interval of the mean daily P&L, resampling whole days (fixed seed)."""
    x = finite(daily)
    if len(x) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, len(x), size=(n_boot, len(x)))].mean(axis=1)
    lo, hi = np.percentile(means, [(1 - conf) / 2 * 100, (1 + conf) / 2 * 100])
    return float(lo), float(hi)


def t_interval(values: Sequence[float], conf: float = 0.95) -> tuple[float, float] | None:
    x = finite(values)
    n = len(x)
    if n < 2:
        return None
    sd = x.std(ddof=1)
    if not math.isfinite(sd):
        return None
    half = st.t.ppf(0.5 + conf / 2, n - 1) * sd / math.sqrt(n)
    return float(x.mean() - half), float(x.mean() + half)


__all__ = ["one_sided_p", "holm", "benjamini_hochberg", "required_days", "ratios", "day_bootstrap_mean",
           "t_interval", "MIN_DAYS_FOR_RATIOS"]
