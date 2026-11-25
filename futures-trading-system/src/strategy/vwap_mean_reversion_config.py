"""Parameters of the VWAP Mean Reversion research strategy (Sprint 11).

Versions live in ``config/strategies/vwap_mean_reversion/<version>.toml`` and are immutable: their SHA-256
is registered in that folder's ``VERSIONS.toml`` (see :mod:`src.research.params`). Every key is validated;
unknown keys, contradictory values and unsupported options are refused.

IMPORTANT: the confirmation methods C1-C4 and the stop methods are EXPERIMENTAL candidates. The user's
original historical confirmation and stop rules are not known; none of these variants is presented as the
original implementation.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any

from src.config import ConfigError

CONFIRMATION_METHODS = {
    "C1": "engulfing: the confirmation bar is a bullish (bearish) engulfing of the previous bar",
    "C2": "close back inside the entry band (above the lower / below the upper band)",
    "C3": "close beyond the previous bar's high (long) / low (short) in the reversal direction",
    "C4": "RSI crosses back above the oversold (below the overbought) threshold",
}
STOP_METHODS = {
    "swing": "beyond the lowest low (highest high) of the last N signal bars",
    "atr": "N x ATR beyond the signal close",
    "band": "beyond the VWAP band with multiplier stop.band_k",
}


@dataclass(frozen=True)
class Scope:
    instruments: tuple[str, ...] = ("MES", "MNQ")
    session: str = "rth"
    signal_timeframe: str = "5min"
    execution_timeframe: str = "1min"
    enable_long: bool = True
    enable_short: bool = True


@dataclass(frozen=True)
class Indicators:
    vwap_price: str = "typical"
    band_k: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    atr_period: int = 14
    indicator_scope: str = "session_bars"   # RSI / ATR on the selected session's bars only ("all_bars" = every bar)
    min_sigma_ticks: float = 2.0            # no setup while the band width (sigma) is below this many ticks
    min_session_bars: int = 6               # no setup before this many signal bars of the session


@dataclass(frozen=True)
class Confirmation:
    method: str = "C2"
    max_bars_after_setup: int = 3           # the confirmation bar comes 1..N bars AFTER the setup bar


@dataclass(frozen=True)
class Stop:
    method: str = "swing"
    swing_lookback_bars: int = 5
    atr_multiple: float = 1.5
    band_k: float = 3.0
    buffer_ticks: int = 2
    max_risk_points: float = 0.0            # 0 = no maximum stop distance


@dataclass(frozen=True)
class Target:
    method: str = "vwap"                    # reversion toward the signal-bar VWAP
    fraction: float = 1.0                   # 1.0 = full reversion to VWAP
    min_reward_risk: float = 0.0            # 0 = off; else re-checked by the engine at the actual fill


@dataclass(frozen=True)
class Execution:
    order_type: str = "market"
    quantity: int = 0                       # 0 = engine sizing (config/backtest.toml [sizing])
    entry_cutoff_minutes: float = 30.0      # no new entry this close to the selected session's end
    max_entries_per_session: int = 3
    cooldown_bars_after_exit: int = 2
    max_holding_bars: int = 0               # 0 = off; else exit at market after N signal bars
    allow_pyramiding: bool = False          # must stay false in V1 (refused otherwise)


@dataclass(frozen=True)
class Regime:
    mode: str = "observe"                   # "off" | "observe" (labels trades, never blocks in V1)


@dataclass(frozen=True)
class VwapMeanReversionConfig:
    strategy: str
    version: str
    label: str
    scope: Scope
    indicators: Indicators
    confirmation: Confirmation
    stop: Stop
    target: Target
    execution: Execution
    regime: Regime
    notes: str = ""

    name = "vwap_mean_reversion"

    @property
    def band_col_lower(self) -> str:
        return f"mr_lower_{self.indicators.band_k:g}"

    @property
    def band_col_upper(self) -> str:
        return f"mr_upper_{self.indicators.band_k:g}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scope"]["instruments"] = list(self.scope.instruments)
        return d


_SECTIONS = {"version": None, "scope": Scope, "indicators": Indicators, "confirmation": Confirmation,
             "stop": Stop, "target": Target, "execution": Execution, "regime": Regime}
_VERSION_KEYS = {"strategy", "version", "label", "notes", "created"}


def _num(v: Any, name: str, *, lo: float | None = None, hi: float | None = None, integer: bool = False,
         strict_lo: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ConfigError(f"{name} must be a finite number, got {v!r}")
    if integer and not isinstance(v, int):
        raise ConfigError(f"{name} must be an integer, got {v!r}")
    if lo is not None and (v <= lo if strict_lo else v < lo):
        raise ConfigError(f"{name} must be {'>' if strict_lo else '>='} {lo}, got {v!r}")
    if hi is not None and v > hi:
        raise ConfigError(f"{name} must be <= {hi}, got {v!r}")
    return v


def parse_config(raw: dict[str, Any], *, sessions: tuple[str, ...], timeframes: tuple[str, ...]) -> VwapMeanReversionConfig:
    """Strictly validated parameters from a parsed TOML document."""
    unknown = set(raw) - set(_SECTIONS)
    if unknown:
        raise ConfigError(f"unknown section(s) {sorted(unknown)}")
    head = raw.get("version") or {}
    if set(head) - _VERSION_KEYS:
        raise ConfigError(f"unknown [version] key(s) {sorted(set(head) - _VERSION_KEYS)}")
    if head.get("strategy") != "vwap_mean_reversion":
        raise ConfigError("[version] strategy must be 'vwap_mean_reversion'")
    parts: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        if cls is None:
            continue
        data = raw.get(name, {})
        if not isinstance(data, dict):
            raise ConfigError(f"[{name}] must be a table")
        known = {f.name for f in fields(cls)}
        if set(data) - known:
            raise ConfigError(f"unknown key(s) in [{name}]: {sorted(set(data) - known)}")
        if name == "scope" and "instruments" in data:
            data = {**data, "instruments": tuple(data["instruments"])}
        parts[name] = cls(**data)
    c = VwapMeanReversionConfig(strategy=head["strategy"], version=str(head.get("version", "")),
                                label=str(head.get("label", "")), notes=str(head.get("notes", "")), **parts)
    _validate(c, sessions, timeframes)
    return c


def _validate(c: VwapMeanReversionConfig, sessions: tuple[str, ...], timeframes: tuple[str, ...]) -> None:
    s, i, cf, st, t, e = c.scope, c.indicators, c.confirmation, c.stop, c.target, c.execution
    if not c.version:
        raise ConfigError("[version] version is required")
    if not s.instruments or set(s.instruments) - {"MES", "MNQ"}:
        raise ConfigError("scope.instruments must be a non-empty subset of ['MES', 'MNQ']")
    if s.session not in sessions:
        raise ConfigError(f"scope.session {s.session!r} is not a session defined in config/settings.toml {list(sessions)}")
    for tf in (s.signal_timeframe, s.execution_timeframe):
        if tf not in timeframes:
            raise ConfigError(f"timeframe {tf!r} is not produced by the pipeline {list(timeframes)}")
    import pandas as pd

    if pd.Timedelta(s.execution_timeframe) > pd.Timedelta(s.signal_timeframe):
        raise ConfigError("scope.execution_timeframe must not be longer than scope.signal_timeframe")
    if not (s.enable_long or s.enable_short):
        raise ConfigError("at least one of scope.enable_long / enable_short must be true")
    if i.vwap_price not in ("typical", "close"):
        raise ConfigError("indicators.vwap_price must be 'typical' or 'close'")
    _num(i.band_k, "indicators.band_k", lo=0, strict_lo=True, hi=10)
    _num(i.rsi_period, "indicators.rsi_period", lo=2, integer=True, hi=500)
    _num(i.rsi_oversold, "indicators.rsi_oversold", lo=0, hi=100)
    _num(i.rsi_overbought, "indicators.rsi_overbought", lo=0, hi=100)
    if not i.rsi_oversold < i.rsi_overbought:
        raise ConfigError("indicators.rsi_oversold must be below rsi_overbought")
    _num(i.atr_period, "indicators.atr_period", lo=1, integer=True, hi=500)
    if i.indicator_scope not in ("session_bars", "all_bars"):
        raise ConfigError("indicators.indicator_scope must be 'session_bars' or 'all_bars'")
    _num(i.min_sigma_ticks, "indicators.min_sigma_ticks", lo=0)
    _num(i.min_session_bars, "indicators.min_session_bars", lo=1, integer=True)
    if cf.method not in CONFIRMATION_METHODS:
        raise ConfigError(f"confirmation.method must be ONE of {sorted(CONFIRMATION_METHODS)} (modes are never combined)")
    _num(cf.max_bars_after_setup, "confirmation.max_bars_after_setup", lo=1, integer=True, hi=50)
    if st.method not in STOP_METHODS:
        raise ConfigError(f"stop.method must be one of {sorted(STOP_METHODS)}")
    _num(st.swing_lookback_bars, "stop.swing_lookback_bars", lo=1, integer=True, hi=200)
    _num(st.atr_multiple, "stop.atr_multiple", lo=0, strict_lo=True, hi=20)
    _num(st.band_k, "stop.band_k", lo=0, strict_lo=True, hi=10)
    if st.method == "band" and not st.band_k > i.band_k:
        raise ConfigError("stop.band_k must be beyond (greater than) indicators.band_k for a band stop")
    _num(st.buffer_ticks, "stop.buffer_ticks", lo=0, integer=True, hi=100)
    _num(st.max_risk_points, "stop.max_risk_points", lo=0)
    if t.method != "vwap":
        raise ConfigError("target.method must be 'vwap' (reversion toward VWAP)")
    _num(t.fraction, "target.fraction", lo=0, strict_lo=True, hi=1)
    _num(t.min_reward_risk, "target.min_reward_risk", lo=0, hi=20)
    if e.order_type != "market":
        raise ConfigError("execution.order_type must be 'market' in V1")
    _num(e.quantity, "execution.quantity", lo=0, integer=True, hi=100)
    _num(e.entry_cutoff_minutes, "execution.entry_cutoff_minutes", lo=0, hi=1440)
    _num(e.max_entries_per_session, "execution.max_entries_per_session", lo=1, integer=True, hi=100)
    _num(e.cooldown_bars_after_exit, "execution.cooldown_bars_after_exit", lo=0, integer=True, hi=500)
    _num(e.max_holding_bars, "execution.max_holding_bars", lo=0, integer=True, hi=10_000)
    if e.allow_pyramiding is not False:
        raise ConfigError("execution.allow_pyramiding must be false (pyramiding is not researched in V1)")
    if c.regime.mode not in ("off", "observe"):
        raise ConfigError("regime.mode must be 'off' or 'observe' (the regime gate never blocks V1)")
