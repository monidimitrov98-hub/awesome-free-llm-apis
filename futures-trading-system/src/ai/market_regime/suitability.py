"""Is VWAP Trend Pullback suitable in the current regime? (per direction)

Rules (all thresholds configurable, documented hypotheses -- not optimised):

NO_TRADE     invalid / stale / errored snapshot; extreme volatility (if enabled); a
             1-minute volatility shock; too many recent stop-outs.
UNFAVORABLE  no trend, weak trend, or a trend AGAINST the entry direction; a range
             regime (tight, wide, choppy, failed breakout); price too far from VWAP;
             sudden volatility expansion; higher-timeframe context against the entry.
FAVORABLE    strong or moderate trend in the entry direction, structure not against it,
             context aligned (if required) and calibrated confidence >= the configured level.
NEUTRAL      everything else (e.g. aligned trend with LOW or uncalibrated confidence).
"""

from __future__ import annotations

import math

from src.ai.market_regime.config import SuitabilityConfig
from src.ai.market_regime.models import (
    LEVEL_RANK,
    ConfidenceLevel,
    Environment,
    RangeRegime,
    RegimeSnapshot,
    StructureState,
    Suitability,
    SuitabilityDecision,
    TrendRegime,
    VolatilityChange,
    VolatilityRegime,
)

ENV = {Suitability.FAVORABLE: Environment.FAVORABLE, Suitability.NEUTRAL: Environment.NEUTRAL,
       Suitability.UNFAVORABLE: Environment.UNFAVORABLE, Suitability.NO_TRADE: Environment.NO_TRADE}
_AGAINST = {1: (StructureState.LH_LL, StructureState.CHANGE_OF_CHARACTER_DOWN, StructureState.BREAK_OF_STRUCTURE_DOWN),
            -1: (StructureState.HH_HL, StructureState.CHANGE_OF_CHARACTER_UP, StructureState.BREAK_OF_STRUCTURE_UP)}


def evaluate_suitability(snapshot: RegimeSnapshot, direction: str, cfg: SuitabilityConfig,
                         recent_stopouts: int = 0) -> SuitabilityDecision:
    sgn = 1 if direction == "long" else -1

    def out(s: Suitability, reasons: list[str]) -> SuitabilityDecision:
        return SuitabilityDecision(s, ENV[s], direction, tuple(reasons))

    if not snapshot.valid or snapshot.classification is None:
        return out(Suitability.NO_TRADE, list(snapshot.reasons) or ["regime_unavailable"])
    c = snapshot.classification
    no_trade = []
    if cfg.block_extreme_volatility and c.volatility is VolatilityRegime.EXTREME:
        no_trade.append("extreme_volatility")
    shock = snapshot.features.get("rf_shock_ratio")
    if shock is not None and math.isfinite(shock) and shock >= cfg.shock_ratio_no_trade:
        no_trade.append(f"volatility_shock_{shock:.1f}x")
    if cfg.max_recent_stopouts and recent_stopouts >= cfg.max_recent_stopouts:
        no_trade.append(f"recent_stopouts_{recent_stopouts}")
    if no_trade:
        return out(Suitability.NO_TRADE, no_trade)

    bad = []
    aligned_trend = {1: (TrendRegime.STRONG_BULLISH, TrendRegime.MODERATE_BULLISH),
                     -1: (TrendRegime.STRONG_BEARISH, TrendRegime.MODERATE_BEARISH)}[sgn]
    if c.trend not in aligned_trend:
        bad.append(f"trend_{c.trend.value}" if c.direction in (0, sgn) else "trend_against_direction")
    if c.range is not RangeRegime.NONE and c.range is not RangeRegime.RANGE_TO_TREND:
        bad.append(f"range_{c.range.value}")
    vd = snapshot.features.get("rf_vwap_dist_atr")
    if vd is not None and math.isfinite(vd) and abs(vd) > cfg.max_vwap_distance_atr:
        bad.append(f"extended_from_vwap_{vd:+.1f}atr")
    ratio = snapshot.features.get("rf_atr_ratio")
    if c.volatility_change is VolatilityChange.EXPANSION and ratio is not None and math.isfinite(ratio) \
            and ratio >= cfg.expansion_unfavorable_ratio:
        bad.append(f"sudden_volatility_expansion_{ratio:.1f}x")
    if cfg.require_context_alignment:
        ctx = snapshot.context
        if ctx is None:
            bad.append("context_unavailable")
        elif ctx.direction == -sgn:
            bad.append("context_against_direction")
    if bad:
        return out(Suitability.UNFAVORABLE, bad)

    good = [f"trend_{c.trend.value}"]
    if c.structure in _AGAINST[sgn]:
        return out(Suitability.NEUTRAL, good + [f"structure_{c.structure.value}"])
    needed = ConfidenceLevel(cfg.min_confidence_for_favorable)
    if LEVEL_RANK[snapshot.confidence_level] < LEVEL_RANK[needed]:
        return out(Suitability.NEUTRAL, good + [f"confidence_{snapshot.confidence_level.value}_below_{needed.value}"])
    return out(Suitability.FAVORABLE, good + [f"confidence_{snapshot.confidence_level.value}"])
