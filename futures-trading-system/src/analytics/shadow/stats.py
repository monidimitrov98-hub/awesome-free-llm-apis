"""Statistics for trade samples, with their limitations stated.

* Sample-size levels: < 30 trades = INSUFFICIENT, < 100 = LIMITED, otherwise ADEQUATE. None of them
  proves a future edge.
* Win rate: Wilson score interval (valid for small n, never outside [0, 1]).
* Expectancy (mean net P&L per trade): Student-t interval AND a cluster bootstrap that resamples whole
  trading DAYS (trades of one day are correlated: same regime, same news, same account state), so
  the interval is not falsely narrow. The wider of the two is reported as the conservative interval.
* Profit factor: bootstrap percentile interval (day clusters); undefined (None) without losses.
* All bootstraps use a fixed seed: results are reproducible.
* Breakdowns (by regime, weekday, ...) are EXPLORATORY: many subgroups invite false discoveries
  (multiple comparisons); no correction is applied and no subgroup is ever called significant.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import numpy as np
from scipy import stats as st

INSUFFICIENT, LIMITED = 30, 100


def sample_level(n: int) -> str:
    if n == 0:
        return "NO DATA"
    if n < INSUFFICIENT:
        return "INSUFFICIENT SAMPLE SIZE"
    if n < LIMITED:
        return "LIMITED SAMPLE"
    return "ADEQUATE SAMPLE (not proof of a future edge)"


def wilson(successes: int, n: int, conf: float = 0.95) -> tuple[float, float] | None:
    if n <= 0:
        return None
    z = st.norm.ppf(0.5 + conf / 2)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return float(max(0.0, centre - half)), float(min(1.0, centre + half))


def t_interval(values: Sequence[float], conf: float = 0.95) -> tuple[float, float] | None:
    n = len(values)
    if n < 2:
        return None
    arr = np.asarray(values, dtype=float)
    sd = arr.std(ddof=1)
    if not math.isfinite(sd):
        return None
    half = st.t.ppf(0.5 + conf / 2, n - 1) * sd / math.sqrt(n)
    return float(arr.mean() - half), float(arr.mean() + half)


def profit_factor(values: Iterable[float]) -> float | None:
    arr = np.asarray(list(values), dtype=float)
    gains, losses = arr[arr > 0].sum(), -arr[arr < 0].sum()
    if losses <= 0:
        return None  # undefined without losses (never reported as infinite)
    return float(gains / losses)


def cluster_bootstrap(values: Sequence[float], clusters: Sequence[Any], stat, n_boot: int = 2000, seed: int = 8,
                      conf: float = 0.95) -> tuple[float, float] | None:
    """Percentile interval of ``stat`` when whole clusters (e.g. trading days) are resampled."""
    if len(values) < 2 or n_boot <= 0:
        return None
    groups: dict[Any, list[float]] = {}
    for v, c in zip(values, clusters):
        groups.setdefault(c, []).append(float(v))
    keys = list(groups)
    if len(keys) < 2:
        return None
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(keys), size=len(keys))
        sample = [x for i in pick for x in groups[keys[i]]]
        v = stat(sample)
        if v is not None and math.isfinite(v):
            out.append(v)
    if len(out) < n_boot * 0.5:
        return None
    lo, hi = np.percentile(out, [(1 - conf) / 2 * 100, (1 + conf) / 2 * 100])
    return float(lo), float(hi)


def max_drawdown(pnls: Sequence[float]) -> float:
    """Largest peak-to-trough decline of the cumulative sum (trade sequence; a positive number)."""
    peak = cum = dd = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def max_consecutive_losses(pnls: Sequence[float]) -> int:
    best = cur = 0
    for x in pnls:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def summary(values: Sequence[float]) -> dict[str, Any]:
    arr = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if not len(arr):
        return {"n": 0, "mean": None, "median": None, "p25": None, "p75": None, "min": None, "max": None, "std": None}
    return {"n": int(len(arr)), "mean": float(arr.mean()), "median": float(np.median(arr)),
            "p25": float(np.percentile(arr, 25)), "p75": float(np.percentile(arr, 75)), "min": float(arr.min()),
            "max": float(arr.max()), "std": float(arr.std(ddof=1)) if len(arr) > 1 else None}


def mean(values: Iterable[float | None]) -> float | None:
    arr = [v for v in values if v is not None and math.isfinite(v)]
    return float(sum(arr) / len(arr)) if arr else None


def trade_stats(trades: list[dict[str, Any]], *, n_boot: int = 2000, seed: int = 8) -> dict[str, Any]:
    """Core metrics for a list of trade records (keys: net_pnl, gross_pnl, commission, slippage_cost,
    r_multiple, duration_min, mfe_r, mae_r, day). Every figure is None when it cannot be computed."""
    n = len(trades)
    nets = [t["net_pnl"] for t in trades]
    days = [t.get("day") for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    exp_t = t_interval(nets)
    exp_b = cluster_bootstrap(nets, days, lambda s: float(np.mean(s)), n_boot, seed)
    conservative = None
    if exp_t and exp_b:
        conservative = (min(exp_t[0], exp_b[0]), max(exp_t[1], exp_b[1]))
    else:
        conservative = exp_t or exp_b
    return {
        "trades": n, "sample": sample_level(n), "trading_days": len(set(days)),
        "wins": len(wins), "losses": len(losses), "win_rate": (len(wins) / n) if n else None,
        "win_rate_ci95": wilson(len(wins), n),
        "average_winner": mean(wins), "average_loser": mean(losses),
        "average_r": mean(t.get("r_multiple") for t in trades),
        "expectancy": mean(nets), "expectancy_ci95": conservative, "expectancy_ci95_t": exp_t,
        "expectancy_ci95_day_bootstrap": exp_b,
        "gross_pnl": float(sum(t["gross_pnl"] for t in trades)) if n else 0.0,
        "net_pnl": float(sum(nets)) if n else 0.0,
        "commissions": float(sum(t["commission"] for t in trades)) if n else 0.0,
        "slippage_cost": float(sum(t["slippage_cost"] for t in trades)) if n else 0.0,
        "profit_factor": profit_factor(nets) if n else None,
        "profit_factor_ci95": cluster_bootstrap(nets, days, profit_factor, n_boot, seed) if n >= 2 else None,
        "max_drawdown": max_drawdown(nets), "max_consecutive_losses": max_consecutive_losses(nets),
        "average_duration_min": mean(t.get("duration_min") for t in trades),
        "average_mfe_r": mean(t.get("mfe_r") for t in trades), "average_mae_r": mean(t.get("mae_r") for t in trades),
    }
