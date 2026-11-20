"""Configuration loading.

All settings are read from a TOML file (``config/settings.toml`` by default) and
turned into frozen dataclasses. Values are validated here so that the rest of
the code can rely on them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "settings.toml"

CANONICAL_COLUMNS: tuple[str, ...] = ("timestamp", "open", "high", "low", "close", "volume")
MONTH_CODES: str = "FGHJKMNQUVXZ"


class ConfigError(ValueError):
    """Raised when the configuration file is missing values or contains invalid ones."""


@dataclass(frozen=True)
class SessionConfig:
    """A trading session defined by local start/end times (end exclusive)."""

    name: str
    start: time
    end: time
    timezone: str

    @property
    def is_overnight(self) -> bool:
        """True when the session crosses midnight (start >= end)."""
        return self.start >= self.end

    @property
    def start_seconds(self) -> int:
        """Session start as seconds after local midnight."""
        return self.start.hour * 3600 + self.start.minute * 60 + self.start.second

    @property
    def end_seconds(self) -> int:
        """Session end as seconds after local midnight."""
        return self.end.hour * 3600 + self.end.minute * 60 + self.end.second

    @property
    def duration(self) -> pd.Timedelta:
        """Nominal length of the session (ignores DST transitions and early closes)."""
        seconds = self.end_seconds - self.start_seconds
        if self.is_overnight:
            seconds += 24 * 3600
        return pd.Timedelta(seconds=seconds)


@dataclass(frozen=True)
class ContractSpec:
    """Static specification of a futures root symbol."""

    root: str
    description: str
    tick_size: float
    point_value: float
    listed_months: str


@dataclass(frozen=True)
class DataConfig:
    """Settings for reading and normalizing CSV files."""

    columns: dict[str, str]
    source_timezone: str
    timestamp_format: str | None
    timestamp_unit: str | None
    ambiguous_time: str
    timestamp_label: str
    nonexistent_time: str
    base_frequency: str
    delimiter: str
    comment_char: str | None
    symbol_column: str | None
    symbol_filter: str | None
    default_symbol: str | None
    on_error: str
    drop_outside_session: bool


@dataclass(frozen=True)
class IndicatorConfig:
    """Indicator parameters."""

    ema_fast: int
    ema_slow: int
    rsi_period: int
    atr_period: int
    vwap_price: str
    rvol_method: str
    rvol_lookback: int
    rvol_min_periods: int


@dataclass(frozen=True)
class AppConfig:
    """Complete application configuration."""

    data: DataConfig
    contracts: dict[str, ContractSpec]
    session: SessionConfig
    sessions: dict[str, SessionConfig]
    timeframes: list[str]
    indicators: IndicatorConfig
    source_path: Path | None = field(default=None, compare=False)


def _require(section: dict[str, Any], key: str, where: str) -> Any:
    if key not in section:
        raise ConfigError(f"Missing required setting '{key}' in [{where}]")
    return section[key]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_time(value: str, where: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"Invalid time '{value}' in {where}; expected HH:MM") from exc


def _check_timezone(name: str, where: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"Unknown timezone '{name}' in {where}. On Windows install the 'tzdata' package."
        ) from exc
    return name


def _positive_int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{where} must be a positive integer, got {value!r}")
    return value


def _check_frequency(value: str, where: str) -> str:
    try:
        delta = pd.Timedelta(pd.tseries.frequencies.to_offset(value))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"Invalid frequency '{value}' in {where}") from exc
    if delta <= pd.Timedelta(0):
        raise ConfigError(f"Frequency in {where} must be positive, got '{value}'")
    return value


def _parse_session(name: str, raw: dict[str, Any]) -> SessionConfig:
    where = f"sessions.{name}"
    start = _parse_time(_require(raw, "start", where), where)
    end = _parse_time(_require(raw, "end", where), where)
    tz = _check_timezone(str(_require(raw, "timezone", where)), where)
    return SessionConfig(name=name, start=start, end=end, timezone=tz)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate the TOML configuration.

    Args:
        path: Path to a TOML file. Defaults to ``config/settings.toml``.

    Returns:
        A validated :class:`AppConfig`.

    Raises:
        ConfigError: If the file is missing or a value is invalid.
    """
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        raise ConfigError(f"Configuration file not found: {cfg_path}")
    with cfg_path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Cannot parse {cfg_path}: {exc}") from exc

    # --- data ---
    d = _require(raw, "data", "root")
    columns_raw = _require(d, "columns", "data")
    columns: dict[str, str] = {}
    for canonical in CANONICAL_COLUMNS:
        columns[canonical] = str(_require(columns_raw, canonical, "data.columns")).strip()

    ambiguous = str(d.get("ambiguous_time", "raise"))
    if ambiguous not in {"raise", "infer", "NaT"}:
        raise ConfigError("data.ambiguous_time must be 'raise', 'infer' or 'NaT'")
    nonexistent = str(d.get("nonexistent_time", "raise"))
    if nonexistent not in {"raise", "NaT"}:
        raise ConfigError("data.nonexistent_time must be 'raise' or 'NaT'")
    on_error = str(d.get("on_error", "raise"))
    if on_error not in {"raise", "drop"}:
        raise ConfigError("data.on_error must be 'raise' or 'drop'")
    label = str(d.get("timestamp_label", "open"))
    if label not in {"open", "close"}:
        raise ConfigError("data.timestamp_label must be 'open' or 'close'")
    unit = _optional_str(d.get("timestamp_unit"))
    if unit is not None and unit not in {"s", "ms", "us", "ns"}:
        raise ConfigError("data.timestamp_unit must be empty or one of s, ms, us, ns")

    data_cfg = DataConfig(
        columns=columns,
        source_timezone=_check_timezone(str(_require(d, "source_timezone", "data")), "data"),
        timestamp_format=_optional_str(d.get("timestamp_format")),
        timestamp_unit=unit,
        ambiguous_time=ambiguous,
        timestamp_label=label,
        nonexistent_time=nonexistent,
        base_frequency=_check_frequency(str(d.get("base_frequency", "1min")), "data.base_frequency"),
        delimiter=str(d.get("delimiter", ",")),
        comment_char=_optional_str(d.get("comment_char")),
        symbol_column=_optional_str(d.get("symbol_column")),
        symbol_filter=_optional_str(d.get("symbol_filter")),
        default_symbol=_optional_str(d.get("default_symbol")),
        on_error=on_error,
        drop_outside_session=bool(d.get("drop_outside_session", True)),
    )

    # --- contracts ---
    contracts_raw = _require(raw, "contracts", "root")
    contracts: dict[str, ContractSpec] = {}
    for root, spec in contracts_raw.items():
        where = f"contracts.{root}"
        tick = float(_require(spec, "tick_size", where))
        pv = float(_require(spec, "point_value", where))
        if tick <= 0 or pv <= 0:
            raise ConfigError(f"{where}: tick_size and point_value must be positive")
        months = str(spec.get("listed_months", MONTH_CODES)).upper()
        if not months or any(m not in MONTH_CODES for m in months):
            raise ConfigError(f"{where}.listed_months contains invalid month codes")
        contracts[root.upper()] = ContractSpec(
            root=root.upper(),
            description=str(spec.get("description", root)),
            tick_size=tick,
            point_value=pv,
            listed_months=months,
        )
    if not contracts:
        raise ConfigError("At least one contract must be configured in [contracts]")

    # --- sessions ---
    sessions_raw = _require(raw, "sessions", "root")
    sessions = {name: _parse_session(name, s) for name, s in sessions_raw.items()}
    active = str(_require(_require(raw, "session", "root"), "active", "session"))
    if active not in sessions:
        raise ConfigError(f"Active session '{active}' is not defined under [sessions]")

    # --- resample ---
    timeframes = list(_require(_require(raw, "resample", "root"), "timeframes", "resample"))
    if not timeframes:
        raise ConfigError("resample.timeframes must not be empty")
    base = pd.Timedelta(pd.tseries.frequencies.to_offset(data_cfg.base_frequency))
    for tf in timeframes:
        _check_frequency(tf, "resample.timeframes")
        delta = pd.Timedelta(pd.tseries.frequencies.to_offset(tf))
        if delta < base or delta % base != pd.Timedelta(0):
            raise ConfigError(
                f"Timeframe '{tf}' must be a multiple of base_frequency '{data_cfg.base_frequency}'"
            )

    # --- indicators ---
    ind = _require(raw, "indicators", "root")
    rv = ind.get("relative_volume", {})
    vwap_price = str(ind.get("vwap_price", "typical"))
    if vwap_price not in {"typical", "close"}:
        raise ConfigError("indicators.vwap_price must be 'typical' or 'close'")
    rv_method = str(rv.get("method", "time_of_day"))
    if rv_method not in {"time_of_day", "rolling"}:
        raise ConfigError("indicators.relative_volume.method must be 'time_of_day' or 'rolling'")
    ind_cfg = IndicatorConfig(
        ema_fast=_positive_int(ind.get("ema_fast", 9), "indicators.ema_fast"),
        ema_slow=_positive_int(ind.get("ema_slow", 21), "indicators.ema_slow"),
        rsi_period=_positive_int(ind.get("rsi_period", 14), "indicators.rsi_period"),
        atr_period=_positive_int(ind.get("atr_period", 14), "indicators.atr_period"),
        vwap_price=vwap_price,
        rvol_method=rv_method,
        rvol_lookback=_positive_int(rv.get("lookback", 20), "relative_volume.lookback"),
        rvol_min_periods=_positive_int(rv.get("min_periods", 5), "relative_volume.min_periods"),
    )
    if ind_cfg.rvol_min_periods > ind_cfg.rvol_lookback:
        raise ConfigError("relative_volume.min_periods cannot exceed lookback")

    return AppConfig(
        data=data_cfg,
        contracts=contracts,
        session=sessions[active],
        sessions=sessions,
        timeframes=timeframes,
        indicators=ind_cfg,
        source_path=cfg_path,
    )
