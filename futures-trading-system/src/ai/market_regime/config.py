"""Configuration of the market regime engine (``config/ai/market_regime.toml``).

Strict: unknown sections/keys are rejected, so nothing in this file can reach the risk
engine or AccountGuard (they are configured elsewhere and are not readable here).
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError

DEFAULT_REGIME_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "ai" / "market_regime.toml"
ENFORCEMENT = ("observe", "block_no_trade", "block_unfavorable", "require_favorable")
LEVELS = ("HIGH", "MEDIUM", "LOW")


@dataclass(frozen=True)
class FeatureConfig:
    efficiency_window: int = 12
    range_window: int = 24
    slope_bars: int = 3
    volatility_reference: str = "time_of_day"  # "time_of_day" | "rolling"
    atr_history_sessions: int = 60
    atr_history_sessions_min: int = 5
    atr_history: int = 200
    atr_history_min: int = 60
    atr_ratio_window: int = 48
    failed_break_bars: int = 3
    body_window: int = 12
    swing_left: int = 2
    swing_right: int = 2
    shock_window: int = 5

    @property
    def min_history_bars(self) -> int:
        """Bars needed before every rolling feature can be valid (time-of-day volatility
        additionally needs ``atr_history_sessions_min`` earlier sessions)."""
        return max(self.atr_ratio_window, self.range_window, self.efficiency_window, self.body_window,
                   self.atr_history_min if self.volatility_reference == "rolling" else 0) + 1


@dataclass(frozen=True)
class ClassificationConfig:
    strong_trend_spread_atr: float = 0.75
    moderate_trend_spread_atr: float = 0.30
    strong_trend_efficiency: float = 0.50
    trend_efficiency: float = 0.30
    chop_efficiency: float = 0.20
    chop_direction_changes: float = 0.60
    tight_range_atr: float = 3.0
    wide_range_atr: float = 8.0
    vol_very_low_pct: float = 0.10
    vol_high_pct: float = 0.80
    vol_extreme_pct: float = 0.97
    expansion_ratio: float = 1.5
    contraction_ratio: float = 0.70
    transition_rvol: float = 1.5


@dataclass(frozen=True)
class ConfidenceConfig:
    high: float = 0.65
    medium: float = 0.50
    min_bin_samples: int = 30


@dataclass(frozen=True)
class SuitabilityConfig:
    max_vwap_distance_atr: float = 2.0
    block_extreme_volatility: bool = True
    shock_ratio_no_trade: float = 3.0
    expansion_unfavorable_ratio: float = 2.0
    max_recent_stopouts: int = 2
    stopout_window_minutes: float = 60.0
    require_context_alignment: bool = True
    min_confidence_for_favorable: str = "MEDIUM"


@dataclass(frozen=True)
class StalenessConfig:
    max_context_staleness_minutes: float = 15.0
    max_fast_staleness_minutes: float = 2.0


@dataclass(frozen=True)
class LabelConfig:
    horizon_bars: int = 12
    trend_efficiency: float = 0.40
    trend_move_atr: float = 1.0


@dataclass(frozen=True)
class MLConfig:
    model: str = "hist_gradient_boosting"
    random_state: int = 7
    max_depth: int = 4
    n_estimators: int = 200
    learning_rate: float = 0.05
    min_train_samples: int = 500


@dataclass(frozen=True)
class ValidationConfig:
    purge_bars: int = 12
    embargo_bars: int = 12
    train_sessions: int = 20
    test_sessions: int = 5


@dataclass(frozen=True)
class DiagnosticsConfig:
    max_transitions: int = 20


@dataclass(frozen=True)
class RegimeConfig:
    enabled: bool = True
    model_version: str = "regime-rules-0.1"
    classifier: str = "rules"
    enforcement: str = "block_unfavorable"
    signal_timeframe: str = "5min"
    context_timeframe: str | None = "15min"
    fast_timeframe: str | None = "1min"
    calibration_path: str | None = None
    ml_model_path: str | None = None
    features: FeatureConfig = field(default_factory=FeatureConfig)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    suitability: SuitabilityConfig = field(default_factory=SuitabilityConfig)
    staleness: StalenessConfig = field(default_factory=StalenessConfig)
    labels: LabelConfig = field(default_factory=LabelConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)
    source_path: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _validate(self)

    def to_dict(self) -> dict[str, Any]:
        def conv(o: Any) -> Any:
            if hasattr(o, "__dataclass_fields__"):
                return {f.name: conv(getattr(o, f.name)) for f in fields(o) if f.name != "source_path"}
            return str(o) if isinstance(o, Path) else o
        return conv(self)


def _validate(c: RegimeConfig) -> None:
    def num(v: Any, name: str, lo: float = 0.0, lo_open: bool = False, hi: float | None = None) -> None:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ConfigError(f"regime {name} must be a finite number, got {v!r}")
        if v < lo or (lo_open and v == lo) or (hi is not None and v > hi):
            raise ConfigError(f"regime {name}={v} out of range")

    def integer(v: Any, name: str, lo: int = 1) -> None:
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            raise ConfigError(f"regime {name} must be an integer >= {lo}, got {v!r}")

    if c.classifier not in ("rules", "ml"):
        raise ConfigError("regime.classifier must be 'rules' or 'ml'")
    if c.classifier == "ml" and not c.ml_model_path:
        raise ConfigError("regime.classifier = 'ml' requires regime.ml_model_path")
    if c.enforcement not in ENFORCEMENT:
        raise ConfigError(f"regime.enforcement must be one of {ENFORCEMENT}")
    for name in ("signal_timeframe", "context_timeframe", "fast_timeframe"):
        tf = getattr(c, name)
        if tf:
            try:
                pd.Timedelta(pd.tseries.frequencies.to_offset(tf))
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"regime.{name} {tf!r} is not a valid frequency") from exc
    f = c.features
    if f.volatility_reference not in ("time_of_day", "rolling"):
        raise ConfigError("features.volatility_reference must be 'time_of_day' or 'rolling'")
    if f.atr_history_sessions_min > f.atr_history_sessions:
        raise ConfigError("features.atr_history_sessions_min cannot exceed atr_history_sessions")
    for k in ("atr_history_sessions", "atr_history_sessions_min",
              "efficiency_window", "range_window", "slope_bars", "atr_history", "atr_history_min",
              "atr_ratio_window", "failed_break_bars", "body_window", "swing_left", "swing_right", "shock_window"):
        integer(getattr(f, k), f"features.{k}")
    if f.atr_history_min > f.atr_history:
        raise ConfigError("features.atr_history_min cannot exceed atr_history")
    k = c.classification
    for name in ("strong_trend_spread_atr", "moderate_trend_spread_atr", "tight_range_atr", "wide_range_atr",
                 "expansion_ratio", "transition_rvol"):
        num(getattr(k, name), f"classification.{name}", lo_open=True)
    for name in ("strong_trend_efficiency", "trend_efficiency", "chop_efficiency", "chop_direction_changes",
                 "vol_very_low_pct", "vol_high_pct", "vol_extreme_pct", "contraction_ratio"):
        num(getattr(k, name), f"classification.{name}", hi=1.0)
    if not k.moderate_trend_spread_atr < k.strong_trend_spread_atr:
        raise ConfigError("moderate_trend_spread_atr must be < strong_trend_spread_atr")
    if not k.chop_efficiency <= k.trend_efficiency <= k.strong_trend_efficiency:
        raise ConfigError("need chop_efficiency <= trend_efficiency <= strong_trend_efficiency")
    if not k.tight_range_atr < k.wide_range_atr:
        raise ConfigError("tight_range_atr must be < wide_range_atr")
    if not k.vol_very_low_pct < k.vol_high_pct < k.vol_extreme_pct:
        raise ConfigError("need vol_very_low_pct < vol_high_pct < vol_extreme_pct")
    if not k.contraction_ratio < 1.0 < k.expansion_ratio:
        raise ConfigError("need contraction_ratio < 1 < expansion_ratio")
    q = c.confidence
    num(q.medium, "confidence.medium", hi=1.0)
    num(q.high, "confidence.high", hi=1.0)
    if not q.medium < q.high:
        raise ConfigError("confidence.medium must be < confidence.high")
    integer(q.min_bin_samples, "confidence.min_bin_samples")
    s = c.suitability
    num(s.max_vwap_distance_atr, "suitability.max_vwap_distance_atr", lo_open=True)
    num(s.shock_ratio_no_trade, "suitability.shock_ratio_no_trade", lo_open=True)
    num(s.expansion_unfavorable_ratio, "suitability.expansion_unfavorable_ratio", lo_open=True)
    integer(s.max_recent_stopouts, "suitability.max_recent_stopouts", lo=0)
    num(s.stopout_window_minutes, "suitability.stopout_window_minutes")
    if s.min_confidence_for_favorable not in LEVELS:
        raise ConfigError(f"suitability.min_confidence_for_favorable must be one of {LEVELS}")
    num(c.staleness.max_context_staleness_minutes, "staleness.max_context_staleness_minutes", lo_open=True)
    num(c.staleness.max_fast_staleness_minutes, "staleness.max_fast_staleness_minutes", lo_open=True)
    lb = c.labels
    integer(lb.horizon_bars, "labels.horizon_bars")
    num(lb.trend_efficiency, "labels.trend_efficiency", lo_open=True, hi=1.0)
    num(lb.trend_move_atr, "labels.trend_move_atr", lo_open=True)
    m = c.ml
    if m.model not in ("hist_gradient_boosting", "random_forest"):
        raise ConfigError("ml.model must be 'hist_gradient_boosting' or 'random_forest'")
    integer(m.random_state, "ml.random_state", lo=0)
    integer(m.max_depth, "ml.max_depth")
    integer(m.n_estimators, "ml.n_estimators")
    num(m.learning_rate, "ml.learning_rate", lo_open=True, hi=1.0)
    integer(m.min_train_samples, "ml.min_train_samples")
    v = c.validation
    integer(v.purge_bars, "validation.purge_bars", lo=0)
    integer(v.embargo_bars, "validation.embargo_bars", lo=0)
    if v.purge_bars < lb.horizon_bars:
        raise ConfigError("validation.purge_bars must be >= labels.horizon_bars (label overlap would leak)")
    integer(v.train_sessions, "validation.train_sessions")
    integer(v.test_sessions, "validation.test_sessions")
    integer(c.diagnostics.max_transitions, "diagnostics.max_transitions")


_SECTIONS = {"features": FeatureConfig, "classification": ClassificationConfig, "confidence": ConfidenceConfig,
             "suitability": SuitabilityConfig, "staleness": StalenessConfig, "labels": LabelConfig, "ml": MLConfig,
             "validation": ValidationConfig, "diagnostics": DiagnosticsConfig}
_TOP = {"enabled", "model_version", "classifier", "enforcement", "signal_timeframe", "context_timeframe",
        "fast_timeframe", "calibration_path", "ml_model_path"}


def load_regime_config(path: str | Path | None = None) -> RegimeConfig:
    """Load and validate the regime configuration (unknown sections/keys are errors)."""
    p = Path(path) if path is not None else DEFAULT_REGIME_CONFIG_PATH
    if not p.is_file():
        raise ConfigError(f"Regime configuration not found: {p}")
    with p.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Cannot parse {p}: {exc}") from exc
    unknown = set(raw) - set(_SECTIONS) - {"regime"}
    if unknown:
        raise ConfigError(f"Unknown section(s) in {p}: {sorted(unknown)} (account/risk settings are not allowed here)")
    top = raw.get("regime", {})
    extra = set(top) - _TOP
    if extra:
        raise ConfigError(f"Unknown setting(s) in [regime]: {sorted(extra)}")
    sections = {}
    for name, cls in _SECTIONS.items():
        data = raw.get(name, {})
        bad = set(data) - {f.name for f in fields(cls)}
        if bad:
            raise ConfigError(f"Unknown setting(s) in [{name}]: {sorted(bad)}")
        sections[name] = cls(**data)
    opt = lambda v: (str(v).strip() or None) if v is not None else None  # noqa: E731
    try:
        return RegimeConfig(
            enabled=bool(top.get("enabled", True)), model_version=str(top.get("model_version", "regime-rules-0.1")),
            classifier=str(top.get("classifier", "rules")), enforcement=str(top.get("enforcement", "block_unfavorable")),
            signal_timeframe=str(top.get("signal_timeframe", "5min")),
            context_timeframe=opt(top.get("context_timeframe", "15min")),
            fast_timeframe=opt(top.get("fast_timeframe", "1min")),
            calibration_path=opt(top.get("calibration_path")), ml_model_path=opt(top.get("ml_model_path")),
            source_path=p, **sections)
    except TypeError as exc:
        raise ConfigError(f"Invalid regime configuration: {exc}") from exc
