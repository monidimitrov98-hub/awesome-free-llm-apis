"""Regime-aware performance reporting.

Every trade is joined (by ``signal_id``) with the regime recorded when its signal was
evaluated, then metrics are computed per regime group with sample sizes and uncertainty:
a Wilson 95% interval for the win rate and a seeded bootstrap 95% interval for the
expectancy. Groups with fewer than ``min_trades`` trades are flagged -- no conclusion
should be drawn from them. MAE/MFE use the execution bars between entry and exit (the
whole entry and exit bars are included, so they are conservative bar-based estimates).
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

DEFAULT_GROUPS = ("trend", "range", "volatility", "suitability", "confidence_level", "context_trend")
REGIME_COLUMNS = ["signal_id", "trend", "range", "volatility", "volatility_change", "structure", "direction",
                  "trend_strength_atr", "confidence", "confidence_level", "suitability", "environment",
                  "suitability_reasons", "allowed", "block_reason", "context_trend", "regime_at_entry",
                  "trend_at_entry"]


def enrich_trades(trades: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    if decisions.empty:
        out = trades.copy()
        for c in REGIME_COLUMNS[1:]:
            out[c] = np.nan
        return out
    dec = decisions[[c for c in REGIME_COLUMNS if c in decisions.columns]].drop_duplicates("signal_id", keep="last")
    return trades.merge(dec, on="signal_id", how="left")


def add_excursions(trades: pd.DataFrame, execution: pd.DataFrame, point_value: float) -> pd.DataFrame:
    out = trades.copy()
    mae, mfe = [], []
    for t in out.itertuples():
        bars = execution.loc[(execution.index >= t.entry_time) & (execution.index <= t.exit_time)]
        if bars.empty:
            mae.append(np.nan)
            mfe.append(np.nan)
            continue
        sgn = 1 if t.side == "long" else -1
        best = bars["high"].max() if sgn > 0 else bars["low"].min()
        worst = bars["low"].min() if sgn > 0 else bars["high"].max()
        mfe.append(max((best - t.entry_price) * sgn, 0.0) * point_value * t.quantity)
        mae.append(max((t.entry_price - worst) * sgn, 0.0) * point_value * t.quantity)
    out["mae_dollars"], out["mfe_dollars"] = mae, mfe
    return out


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (centre - half, centre + half)


def bootstrap_mean(values: np.ndarray, n_boot: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    if values.size < 2:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(values, size=(n_boot, values.size), replace=True).mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def group_stats(t: pd.DataFrame, min_trades: int = 30) -> dict[str, Any]:
    pnl = t["net_pnl"].astype(float).to_numpy()
    n = pnl.size
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    cum = np.cumsum(pnl)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum])) - np.concatenate([[0.0], cum]))) if n else 0.0
    r = t["r_multiple"].astype(float).dropna() if "r_multiple" in t else pd.Series(dtype=float)
    pf = (wins.sum() / abs(losses.sum())) if losses.size else (math.inf if wins.size else math.nan)
    return {
        "trades": n, "sample_warning": n < min_trades,
        "win_rate": wins.size / n if n else None, "win_rate_ci95": wilson(int(wins.size), n),
        "net_pnl": float(pnl.sum()), "profit_factor": pf,
        "expectancy": float(pnl.mean()) if n else None, "expectancy_ci95": bootstrap_mean(pnl),
        "average_r": float(r.mean()) if len(r) else None,
        "max_drawdown_trade_sequence": dd,
        "avg_mae_dollars": float(t["mae_dollars"].mean()) if "mae_dollars" in t and n else None,
        "avg_mfe_dollars": float(t["mfe_dollars"].mean()) if "mfe_dollars" in t and n else None,
    }


def regime_performance(enriched: pd.DataFrame, decisions: pd.DataFrame,
                       groups: Iterable[str] = DEFAULT_GROUPS, min_trades: int = 30) -> dict[str, Any]:
    report: dict[str, Any] = {
        "note": ("Per-regime statistics are descriptive. Groups flagged sample_warning have fewer than "
                 f"{min_trades} trades and support no conclusion; a higher win rate alone is not a better strategy."),
        "overall": group_stats(enriched, min_trades) if len(enriched) else {"trades": 0},
        "by": {},
    }
    for g in groups:
        if g not in enriched.columns:
            continue
        report["by"][g] = {str(k): group_stats(v, min_trades)
                           for k, v in enriched.groupby(enriched[g].fillna("unknown"), sort=True)}
    if not decisions.empty:
        blocked = decisions[~decisions["allowed"].astype(bool)]
        report["gate"] = {"entry_signals_evaluated": int(len(decisions)), "blocked": int(len(blocked)),
                          "blocked_by_suitability": blocked["suitability"].value_counts().to_dict(),
                          "note": "outcomes of blocked signals are unknown (never traded); compare an "
                                  "enforcement='observe' run to study them"}
    return report
