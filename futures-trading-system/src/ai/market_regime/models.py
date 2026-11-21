"""Typed, JSON-serialisable outputs of the regime engine (dashboard-ready)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TrendRegime(StrEnum):
    STRONG_BULLISH = "strong_bullish"
    MODERATE_BULLISH = "moderate_bullish"
    STRONG_BEARISH = "strong_bearish"
    MODERATE_BEARISH = "moderate_bearish"
    WEAK = "weak_or_deteriorating"
    NONE = "no_trend"


class RangeRegime(StrEnum):
    TIGHT_CONSOLIDATION = "tight_consolidation"
    WIDE_RANGE = "wide_sideways_range"
    CHOPPY = "choppy"
    FAILED_BREAKOUT = "failed_breakout"
    RANGE_TO_TREND = "potential_range_to_trend"
    NONE = "not_ranging"


class VolatilityRegime(StrEnum):
    VERY_LOW = "very_low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"
    UNKNOWN = "unknown"


class VolatilityChange(StrEnum):
    EXPANSION = "expansion"
    CONTRACTION = "contraction"
    STABLE = "stable"
    UNKNOWN = "unknown"


class StructureState(StrEnum):
    HH_HL = "higher_highs_higher_lows"
    LH_LL = "lower_highs_lower_lows"
    BREAK_OF_STRUCTURE_UP = "break_of_structure_up"
    BREAK_OF_STRUCTURE_DOWN = "break_of_structure_down"
    CHANGE_OF_CHARACTER_UP = "change_of_character_up"
    CHANGE_OF_CHARACTER_DOWN = "change_of_character_down"
    FAILED_BREAK = "failed_structural_break"
    COMPRESSION = "compression"
    MIXED = "mixed"
    INSUFFICIENT = "insufficient_pivots"


class Environment(StrEnum):
    FAVORABLE = "favorable_for_vwap_pullback"
    UNFAVORABLE = "unfavorable_for_vwap_pullback"
    NEUTRAL = "neutral"
    NO_TRADE = "no_trade"


class Suitability(StrEnum):
    FAVORABLE = "FAVORABLE"
    NEUTRAL = "NEUTRAL"
    UNFAVORABLE = "UNFAVORABLE"
    NO_TRADE = "NO_TRADE"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT_DATA"


LEVEL_RANK = {ConfidenceLevel.INSUFFICIENT: 0, ConfidenceLevel.LOW: 1, ConfidenceLevel.MEDIUM: 2,
              ConfidenceLevel.HIGH: 3}


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class Classification:
    """Layer-1 (or Layer-2) classification of one completed bar."""

    trend: TrendRegime
    range: RangeRegime
    volatility: VolatilityRegime
    volatility_change: VolatilityChange
    structure: StructureState
    direction: int  # +1 bullish, -1 bearish, 0 neutral
    trend_score: float  # raw evidence score in [0, 1] (NOT a probability)
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegimeSnapshot:
    """Everything the regime engine knows at one decision time."""

    time: Any
    bar_time: Any
    instrument: str
    model_version: str
    valid: bool
    classification: Classification | None
    context: Classification | None
    confidence: float | None  # calibrated probability, None when uncalibrated / invalid
    confidence_level: ConfidenceLevel
    features: dict[str, float] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    error: str | None = None

    @property
    def direction_label(self) -> str:
        if self.classification is None:
            return "unknown"
        return {1: "bullish", -1: "bearish", 0: "neutral"}[self.classification.direction]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction_label
        return _clean(d)


@dataclass(frozen=True)
class SuitabilityDecision:
    """Suitability of VWAP Trend Pullback for one direction at one decision time."""

    suitability: Suitability
    environment: Environment
    direction: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return _clean(asdict(self))
