"""Layer 1: interpretable rule-based regime classification of completed bars.

Input: the causal ``rf_*`` features of ONE bar (or a frame of bars). Output: trend, range,
volatility, volatility change and structure regimes, a direction and a raw evidence score
(NOT a probability -- probabilities come from calibration, see confidence.py). Thresholds
live in [classification] and are documented hypotheses, not optimised values.
"""

from __future__ import annotations

import math
from typing import Mapping

import numpy as np
import pandas as pd

from src.ai.market_regime.config import ClassificationConfig
from src.ai.market_regime.models import (
    Classification,
    RangeRegime,
    StructureState,
    TrendRegime,
    VolatilityChange,
    VolatilityRegime,
)


def _f(row: Mapping[str, float], key: str) -> float:
    v = row.get(key, math.nan)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return math.nan
    return v


def classify_row(row: Mapping[str, float], cfg: ClassificationConfig) -> Classification | None:
    """Classify one bar from its features; None when the features are not valid."""
    if not bool(row.get("rf_valid", False)):
        return None
    s, eff = _f(row, "rf_spread_atr"), _f(row, "rf_efficiency")
    vd, slope = _f(row, "rf_vwap_dist_atr"), _f(row, "rf_slope_atr")
    struct = _f(row, "rf_structure")
    evidence: list[str] = []

    # --- trend
    direction = int(np.sign(s)) if abs(s) >= cfg.moderate_trend_spread_atr else 0
    vwap_ok = direction != 0 and np.sign(vd) == direction
    struct_ok = direction != 0 and struct == direction
    slope_ok = direction != 0 and np.sign(slope) == direction
    if direction == 0:
        trend = TrendRegime.NONE
        evidence.append(f"EMA spread {s:+.2f} ATR below {cfg.moderate_trend_spread_atr}")
    elif abs(s) >= cfg.strong_trend_spread_atr and eff >= cfg.strong_trend_efficiency and vwap_ok and struct_ok:
        trend = TrendRegime.STRONG_BULLISH if direction > 0 else TrendRegime.STRONG_BEARISH
        evidence.append(f"strong: spread {s:+.2f} ATR, efficiency {eff:.2f}, VWAP and structure aligned")
    elif eff >= cfg.trend_efficiency and vwap_ok and slope_ok:
        trend = TrendRegime.MODERATE_BULLISH if direction > 0 else TrendRegime.MODERATE_BEARISH
        evidence.append(f"moderate: spread {s:+.2f} ATR, efficiency {eff:.2f}")
    else:
        trend = TrendRegime.WEAK
        evidence.append(f"weak/deteriorating: spread {s:+.2f} ATR but efficiency {eff:.2f}, "
                        f"VWAP {'ok' if vwap_ok else 'against'}, slope {'ok' if slope_ok else 'against'}")
        direction = direction if (vwap_ok or slope_ok) else 0
    parts = [min(abs(s) / cfg.strong_trend_spread_atr, 1.0), min(eff / cfg.strong_trend_efficiency, 1.0),
             float(vwap_ok), float(struct_ok), float(slope_ok)]
    score = float(np.nanmean(parts)) if direction != 0 else float(1.0 - min(abs(s) / cfg.moderate_trend_spread_atr, 1.0))

    # --- range
    rng, prev_rng = _f(row, "rf_range_atr"), _f(row, "rf_prev_range_atr")
    dirch, rvol = _f(row, "rf_dir_changes"), _f(row, "rf_rvol")
    breakout, failed = _f(row, "rf_breakout"), _f(row, "rf_failed_breakout")
    trending = trend not in (TrendRegime.NONE, TrendRegime.WEAK)
    if failed and not math.isnan(failed) and failed != 0:
        rg = RangeRegime.FAILED_BREAKOUT
        evidence.append("breakout of the prior range reversed back inside it")
    elif (breakout and not math.isnan(breakout) and breakout != 0 and prev_rng <= cfg.tight_range_atr
          and not math.isnan(rvol) and rvol >= cfg.transition_rvol):
        rg = RangeRegime.RANGE_TO_TREND
        evidence.append(f"breakout from compression ({prev_rng:.1f} ATR) on RVOL {rvol:.2f}")
    elif trending:
        rg = RangeRegime.NONE
    elif eff <= cfg.chop_efficiency and dirch >= cfg.chop_direction_changes:
        rg = RangeRegime.CHOPPY
        evidence.append(f"choppy: efficiency {eff:.2f}, direction changes {dirch:.2f}")
    elif rng <= cfg.tight_range_atr:
        rg = RangeRegime.TIGHT_CONSOLIDATION
        evidence.append(f"tight range {rng:.1f} ATR")
    elif rng >= cfg.wide_range_atr:
        rg = RangeRegime.WIDE_RANGE
        evidence.append(f"wide sideways range {rng:.1f} ATR")
    else:
        rg = RangeRegime.NONE

    # --- volatility
    pct, ratio = _f(row, "rf_atr_pct"), _f(row, "rf_atr_ratio")
    hist_n = _f(row, "rf_atr_hist_n")
    # A percentile threshold is only resolvable with enough history: >= 1/(1-p) values for
    # the upper tail (34 for 0.97) and >= 1/p for the lower tail (10 for 0.10).
    can_extreme = not math.isnan(hist_n) and hist_n >= math.ceil(1.0 / (1.0 - cfg.vol_extreme_pct))
    can_very_low = not math.isnan(hist_n) and hist_n >= math.ceil(1.0 / cfg.vol_very_low_pct)
    if math.isnan(pct):
        vol = VolatilityRegime.UNKNOWN
    elif pct >= cfg.vol_extreme_pct and can_extreme:
        vol = VolatilityRegime.EXTREME
    elif pct >= cfg.vol_high_pct:
        vol = VolatilityRegime.HIGH
        if pct >= cfg.vol_extreme_pct:
            evidence.append(f"above every past value but only {hist_n:.0f} observations: not classed extreme")
    elif pct < cfg.vol_very_low_pct and can_very_low:
        vol = VolatilityRegime.VERY_LOW
    else:
        vol = VolatilityRegime.NORMAL
    if math.isnan(ratio):
        vchg = VolatilityChange.UNKNOWN
    elif ratio >= cfg.expansion_ratio:
        vchg = VolatilityChange.EXPANSION
    elif ratio <= cfg.contraction_ratio:
        vchg = VolatilityChange.CONTRACTION
    else:
        vchg = VolatilityChange.STABLE
    evidence.append(f"volatility percentile {pct:.2f}, ATR ratio {ratio:.2f}")

    # --- structure (confirmed pivots only)
    choch, bos, sfail = _f(row, "rf_choch"), _f(row, "rf_bos"), _f(row, "rf_struct_failed")
    if choch == 1:
        st = StructureState.CHANGE_OF_CHARACTER_UP
    elif choch == -1:
        st = StructureState.CHANGE_OF_CHARACTER_DOWN
    elif bos == 1:
        st = StructureState.BREAK_OF_STRUCTURE_UP
    elif bos == -1:
        st = StructureState.BREAK_OF_STRUCTURE_DOWN
    elif sfail == 1:
        st = StructureState.FAILED_BREAK
    elif rng <= cfg.tight_range_atr and ratio <= cfg.contraction_ratio:
        st = StructureState.COMPRESSION
    elif struct == 1:
        st = StructureState.HH_HL
    elif struct == -1:
        st = StructureState.LH_LL
    elif struct == 0:
        st = StructureState.MIXED
    else:
        st = StructureState.INSUFFICIENT
    return Classification(trend, rg, vol, vchg, st, direction, score, tuple(evidence))


def classify_frame(features: pd.DataFrame, cfg: ClassificationConfig) -> pd.DataFrame:
    """Classify every bar (used for validation); invalid bars get NaN/None."""
    rows = []
    for _, r in features.iterrows():
        c = classify_row(r, cfg)
        rows.append({"trend": c.trend.value if c else None, "range": c.range.value if c else None,
                     "volatility": c.volatility.value if c else None,
                     "volatility_change": c.volatility_change.value if c else None,
                     "structure": c.structure.value if c else None,
                     "direction": c.direction if c else np.nan, "trend_score": c.trend_score if c else np.nan})
    return pd.DataFrame(rows, index=features.index)


def predicted_class(direction: float, trend: str | None) -> float:
    """Map a classification to the label space {+1, -1, 0} used by validation."""
    if trend is None:
        return math.nan
    if trend in (TrendRegime.STRONG_BULLISH, TrendRegime.MODERATE_BULLISH):
        return 1.0
    if trend in (TrendRegime.STRONG_BEARISH, TrendRegime.MODERATE_BEARISH):
        return -1.0
    return 0.0
