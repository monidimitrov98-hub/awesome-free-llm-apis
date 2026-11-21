"""Configuration of the VWAP Trend Pullback strategy (``config/strategy_vwap_pullback.toml``)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError
from src.risk.manager import TradingWindow

DEFAULT_STRATEGY_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "strategy_vwap_pullback.toml"
PULLBACK_LEVELS = ("ema_fast", "ema_slow", "vwap")


@dataclass(frozen=True)
class TrendConfig:
    require_price_vs_vwap: bool = True
    require_ema_alignment: bool = True
    require_structure: bool = True
    min_trend_strength_atr: float = 0.10


@dataclass(frozen=True)
class ContextConfig:
    require_price_vs_vwap: bool = True
    require_ema_alignment: bool = True


@dataclass(frozen=True)
class StructureConfig:
    swing_left_bars: int = 2
    swing_right_bars: int = 2
    lookback_bars: int = 60


@dataclass(frozen=True)
class PullbackConfig:
    levels: tuple[str, ...] = PULLBACK_LEVELS
    lookback_bars: int = 3
    zone_atr: float = 0.25
    max_penetration_atr: float = 0.50
    require_structure_intact: bool = True


@dataclass(frozen=True)
class ExtensionConfig:
    max_vwap_distance_atr: float = 1.5
    max_ema_distance_atr: float = 1.0


@dataclass(frozen=True)
class ConfirmationConfig:
    require_directional_close: bool = True
    require_close_beyond_prev_extreme: bool = False
    require_close_beyond_ema_fast: bool = True


@dataclass(frozen=True)
class RsiConfig:
    enabled: bool = True
    long_pullback_max: float = 50.0
    long_recovery_min: float = 50.0
    long_entry_max: float = 75.0
    short_pullback_min: float = 50.0
    short_recovery_max: float = 50.0
    short_entry_min: float = 25.0
    require_turn: bool = True


@dataclass(frozen=True)
class RvolConfig:
    enabled: bool = True
    min_rvol: float = 1.0


@dataclass(frozen=True)
class StopConfig:
    method: str = "swing"
    buffer_ticks: int = 2
    atr_multiplier: float = 1.5
    min_distance_atr: float = 0.5
    max_distance_atr: float = 3.0
    min_distance_ticks: int = 4
    max_distance_ticks: int = 0


@dataclass(frozen=True)
class TargetConfig:
    r_multiple: float = 2.0
    # At the ACTUAL fill, reward/risk must be >= this fraction of the planned R
    # (0.9 x 2.0 = 1.8). Enforced by the engine; 0 disables.
    min_actual_r_fraction: float = 0.9

    @property
    def min_actual_r_multiple(self) -> float:
        return self.r_multiple * self.min_actual_r_fraction


@dataclass(frozen=True)
class FilterConfig:
    session_windows: tuple[TradingWindow, ...] = ()
    max_entries_per_session: int = 3
    cooldown_minutes: float = 15.0
    one_entry_per_pullback: bool = True


@dataclass(frozen=True)
class VwapPullbackConfig:
    """Complete strategy configuration (all values are research hypotheses)."""

    name: str = "vwap_trend_pullback"
    signal_timeframe: str = "5min"
    context_timeframe: str | None = "15min"
    execution_timeframe: str = "1min"
    enable_long: bool = True
    enable_short: bool = True
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    trend: TrendConfig = field(default_factory=TrendConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    structure: StructureConfig = field(default_factory=StructureConfig)
    pullback: PullbackConfig = field(default_factory=PullbackConfig)
    extension: ExtensionConfig = field(default_factory=ExtensionConfig)
    confirmation: ConfirmationConfig = field(default_factory=ConfirmationConfig)
    rsi: RsiConfig = field(default_factory=RsiConfig)
    rvol: RvolConfig = field(default_factory=RvolConfig)
    stop: StopConfig = field(default_factory=StopConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)

    # Column names produced by the Sprint 1 pipeline.
    @property
    def ema_fast_col(self) -> str:
        return f"ema_{self.ema_fast}"

    @property
    def ema_slow_col(self) -> str:
        return f"ema_{self.ema_slow}"

    @property
    def rsi_col(self) -> str:
        return f"rsi_{self.rsi_period}"

    @property
    def atr_col(self) -> str:
        return f"atr_{self.atr_period}"

    def required_columns(self) -> list[str]:
        return ["open", "high", "low", "close", "is_complete", "session_date", "available_at",
                self.ema_fast_col, self.ema_slow_col, self.rsi_col, self.atr_col, "vwap", "rvol"]

    def with_r_multiple(self, r: float) -> "VwapPullbackConfig":
        """Same configuration with another target R (the fill-time fraction is kept)."""
        return replace(self, target=replace(self.target, r_multiple=float(r)))

    def __post_init__(self) -> None:
        _validate(self)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict (for writing the configuration used next to backtest results)."""
        def conv(obj: Any) -> Any:
            if isinstance(obj, TradingWindow):
                return f"{obj.start:%H:%M}-{obj.end:%H:%M}"
            if hasattr(obj, "__dataclass_fields__"):
                return {f.name: conv(getattr(obj, f.name)) for f in fields(obj)}
            if isinstance(obj, (tuple, list)):
                return [conv(v) for v in obj]
            return obj
        return conv(self)


def _validate(c: VwapPullbackConfig) -> None:
    def positive(value: float, name: str, allow_zero: bool = False) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or (value == 0 and not allow_zero):
            raise ConfigError(f"{name} must be {'>= 0' if allow_zero else '> 0'}, got {value!r}")

    for tf_name in ("signal_timeframe", "execution_timeframe"):
        try:
            pd.Timedelta(pd.tseries.frequencies.to_offset(getattr(c, tf_name)))
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"strategy.{tf_name} is not a valid frequency") from exc
    sig = pd.Timedelta(pd.tseries.frequencies.to_offset(c.signal_timeframe))
    exe = pd.Timedelta(pd.tseries.frequencies.to_offset(c.execution_timeframe))
    if exe > sig:
        raise ConfigError("execution_timeframe must not be longer than signal_timeframe")
    if c.context_timeframe:
        ctx = pd.Timedelta(pd.tseries.frequencies.to_offset(c.context_timeframe))
        if ctx < sig:
            raise ConfigError("context_timeframe must not be shorter than signal_timeframe")
    if not (c.enable_long or c.enable_short):
        raise ConfigError("At least one of enable_long / enable_short must be true")
    for name in ("ema_fast", "ema_slow", "rsi_period", "atr_period"):
        positive(getattr(c, name), f"indicators.{name}")
    if c.ema_fast >= c.ema_slow:
        raise ConfigError("indicators.ema_fast must be smaller than ema_slow")
    positive(c.trend.min_trend_strength_atr, "trend.min_trend_strength_atr", allow_zero=True)
    s = c.structure
    for name in ("swing_left_bars", "swing_right_bars", "lookback_bars"):
        positive(getattr(s, name), f"structure.{name}")
    if s.lookback_bars < s.swing_left_bars + s.swing_right_bars + 1:
        raise ConfigError("structure.lookback_bars is too small for the swing window")
    p = c.pullback
    if not p.levels or any(lv not in PULLBACK_LEVELS for lv in p.levels):
        raise ConfigError(f"pullback.levels must be a non-empty subset of {PULLBACK_LEVELS}")
    positive(p.lookback_bars, "pullback.lookback_bars")
    positive(p.zone_atr, "pullback.zone_atr", allow_zero=True)
    positive(p.max_penetration_atr, "pullback.max_penetration_atr", allow_zero=True)
    positive(c.extension.max_vwap_distance_atr, "extension.max_vwap_distance_atr", allow_zero=True)
    positive(c.extension.max_ema_distance_atr, "extension.max_ema_distance_atr", allow_zero=True)
    r = c.rsi
    for name in ("long_pullback_max", "long_recovery_min", "long_entry_max", "short_pullback_min",
                 "short_recovery_max", "short_entry_min"):
        v = getattr(r, name)
        if not 0 <= v <= 100:
            raise ConfigError(f"rsi.{name} must be within 0..100")
    if r.long_recovery_min > r.long_entry_max or r.short_recovery_max < r.short_entry_min:
        raise ConfigError("rsi recovery and entry bounds are contradictory")
    positive(c.rvol.min_rvol, "rvol.min_rvol", allow_zero=True)
    st = c.stop
    if st.method not in {"swing", "atr"}:
        raise ConfigError("stop.method must be 'swing' or 'atr'")
    positive(st.buffer_ticks, "stop.buffer_ticks", allow_zero=True)
    positive(st.atr_multiplier, "stop.atr_multiplier")
    positive(st.min_distance_atr, "stop.min_distance_atr", allow_zero=True)
    positive(st.max_distance_atr, "stop.max_distance_atr", allow_zero=True)
    positive(st.min_distance_ticks, "stop.min_distance_ticks", allow_zero=True)
    positive(st.max_distance_ticks, "stop.max_distance_ticks", allow_zero=True)
    if st.max_distance_atr and st.min_distance_atr >= st.max_distance_atr:
        raise ConfigError("stop.min_distance_atr must be < max_distance_atr")
    if st.max_distance_ticks and st.min_distance_ticks >= st.max_distance_ticks:
        raise ConfigError("stop.min_distance_ticks must be < max_distance_ticks")
    positive(c.target.r_multiple, "target.r_multiple")
    positive(c.target.min_actual_r_fraction, "target.min_actual_r_fraction", allow_zero=True)
    if c.target.min_actual_r_fraction > 1:
        raise ConfigError("target.min_actual_r_fraction must be within 0..1")
    f = c.filters
    positive(f.max_entries_per_session, "filters.max_entries_per_session", allow_zero=True)
    positive(f.cooldown_minutes, "filters.cooldown_minutes", allow_zero=True)


def _section(raw: dict[str, Any], name: str, cls: type, convert: dict[str, Any] | None = None) -> Any:
    data = dict(raw.get(name, {}))
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Unknown setting(s) in [{name}]: {sorted(unknown)}")
    for key, fn in (convert or {}).items():
        if key in data:
            data[key] = fn(data[key])
    return cls(**data)


def load_strategy_config(path: str | Path | None = None) -> VwapPullbackConfig:
    """Load and validate the strategy TOML file."""
    cfg_path = Path(path) if path is not None else DEFAULT_STRATEGY_CONFIG_PATH
    if not cfg_path.is_file():
        raise ConfigError(f"Strategy configuration not found: {cfg_path}")
    with cfg_path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Cannot parse {cfg_path}: {exc}") from exc
    sections = {"strategy", "indicators", "trend", "context", "structure", "pullback", "extension",
                "confirmation", "rsi", "rvol", "stop", "target", "filters"}
    unknown = set(raw) - sections
    if unknown:
        raise ConfigError(f"Unknown section(s) in {cfg_path}: {sorted(unknown)}")
    s = raw.get("strategy", {})
    ind = raw.get("indicators", {})
    allowed = {"strategy": {"name", "signal_timeframe", "context_timeframe", "execution_timeframe",
                            "enable_long", "enable_short"},
               "indicators": {"ema_fast", "ema_slow", "rsi_period", "atr_period"}}
    for name, keys in allowed.items():
        extra = set(raw.get(name, {})) - keys
        if extra:
            raise ConfigError(f"Unknown setting(s) in [{name}]: {sorted(extra)}")
    try:
        return VwapPullbackConfig(
            name=str(s.get("name", "vwap_trend_pullback")),
            signal_timeframe=str(s.get("signal_timeframe", "5min")),
            context_timeframe=(str(s.get("context_timeframe", "15min")).strip() or None),
            execution_timeframe=str(s.get("execution_timeframe", "1min")),
            enable_long=bool(s.get("enable_long", True)),
            enable_short=bool(s.get("enable_short", True)),
            ema_fast=ind.get("ema_fast", 9),
            ema_slow=ind.get("ema_slow", 21),
            rsi_period=ind.get("rsi_period", 14),
            atr_period=ind.get("atr_period", 14),
            trend=_section(raw, "trend", TrendConfig),
            context=_section(raw, "context", ContextConfig),
            structure=_section(raw, "structure", StructureConfig),
            pullback=_section(raw, "pullback", PullbackConfig, {"levels": tuple}),
            extension=_section(raw, "extension", ExtensionConfig),
            confirmation=_section(raw, "confirmation", ConfirmationConfig),
            rsi=_section(raw, "rsi", RsiConfig),
            rvol=_section(raw, "rvol", RvolConfig),
            stop=_section(raw, "stop", StopConfig),
            target=_section(raw, "target", TargetConfig),
            filters=_section(raw, "filters", FilterConfig,
                             {"session_windows": lambda ws: tuple(TradingWindow.parse(w) for w in ws)}),
        )
    except TypeError as exc:
        raise ConfigError(f"Invalid strategy configuration: {exc}") from exc
