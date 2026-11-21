"""Risk engine foundation.

The :class:`RiskManager` can veto any entry before it is executed. The backtest
engine asks it twice: when the strategy's signal is processed and again at the
moment the order would fill. Exits are never blocked by risk limits.

All limits are configurable; no prop-firm rules are assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any

import pandas as pd

from src.config import ConfigError, SessionConfig


@dataclass(frozen=True)
class TradingWindow:
    """Local time window ``[start, end)``; crosses midnight when start >= end."""

    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    @classmethod
    def parse(cls, text: str) -> "TradingWindow":
        try:
            a, b = (part.strip() for part in text.split("-"))
            return cls(time.fromisoformat(a), time.fromisoformat(b))
        except ValueError as exc:
            raise ConfigError(f"Invalid trading window '{text}', expected 'HH:MM-HH:MM'") from exc


@dataclass(frozen=True)
class RiskConfig:
    """Risk limits. Numeric limits set to 0 are disabled."""

    max_daily_loss: float = 0.0
    max_daily_trades: int = 0
    max_position_size: int = 0
    max_consecutive_losses: int = 0
    consecutive_losses_reset_daily: bool = True
    daily_profit_target: float = 0.0
    trading_windows: tuple[TradingWindow, ...] = ()
    emergency_stop: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RiskConfig":
        """Build from the ``[risk]`` TOML table, validating values."""
        def non_negative(key: str, kind: type) -> Any:
            value = raw.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(f"risk.{key} must be a non-negative number, got {value!r}")
            if kind is int and int(value) != value:
                raise ConfigError(f"risk.{key} must be an integer")
            return kind(value)

        return cls(
            max_daily_loss=non_negative("max_daily_loss", float),
            max_daily_trades=non_negative("max_daily_trades", int),
            max_position_size=non_negative("max_position_size", int),
            max_consecutive_losses=non_negative("max_consecutive_losses", int),
            consecutive_losses_reset_daily=bool(raw.get("consecutive_losses_reset_daily", True)),
            daily_profit_target=non_negative("daily_profit_target", float),
            trading_windows=tuple(TradingWindow.parse(w) for w in raw.get("trading_windows", [])),
            emergency_stop=bool(raw.get("emergency_stop", False)),
        )


@dataclass(frozen=True)
class RiskDecision:
    """Outcome of a risk check. ``reason`` is a stable code when rejected."""

    approved: bool
    reason: str = ""


APPROVED = RiskDecision(True)


@dataclass
class _DayState:
    realized_pnl: float = 0.0
    trades: int = 0


@dataclass
class RiskManager:
    """Tracks daily state and approves or rejects entries."""

    config: RiskConfig
    session: SessionConfig
    _days: dict[pd.Timestamp, _DayState] = field(default_factory=dict)
    _consecutive_losses: int = 0
    _last_loss_day: pd.Timestamp | None = None
    _emergency_reason: str | None = None

    def __post_init__(self) -> None:
        if self.config.emergency_stop:
            self._emergency_reason = "configured emergency_stop = true"

    # --- state -----------------------------------------------------------
    def _day(self, session_date: pd.Timestamp) -> _DayState:
        return self._days.setdefault(pd.Timestamp(session_date), _DayState())

    def daily_realized_pnl(self, session_date: pd.Timestamp) -> float:
        return self._day(session_date).realized_pnl

    def daily_trades(self, session_date: pd.Timestamp) -> int:
        return self._day(session_date).trades

    def consecutive_losses(self, session_date: pd.Timestamp | None = None) -> int:
        if (
            self.config.consecutive_losses_reset_daily
            and session_date is not None
            and self._last_loss_day is not None
            and pd.Timestamp(session_date) != self._last_loss_day
        ):
            return 0
        return self._consecutive_losses

    @property
    def emergency_active(self) -> bool:
        return self._emergency_reason is not None

    @property
    def emergency_reason(self) -> str | None:
        return self._emergency_reason

    def activate_emergency_stop(self, reason: str) -> None:
        """Reject all further entries; the engine also flattens open positions."""
        self._emergency_reason = reason or "emergency stop"

    def on_entry_filled(self, session_date: pd.Timestamp) -> None:
        self._day(session_date).trades += 1

    def on_trade_closed(self, net_pnl: float, session_date: pd.Timestamp) -> None:
        """Book a closed trade (net of costs) on the session date of its exit."""
        day = pd.Timestamp(session_date)
        self._day(day).realized_pnl += net_pnl
        if net_pnl < 0:
            self._consecutive_losses = self.consecutive_losses(day) + 1
            self._last_loss_day = day
        else:
            self._consecutive_losses = 0
            self._last_loss_day = None

    # --- checks ----------------------------------------------------------
    def check_entry(self, *, when: pd.Timestamp, session_date: pd.Timestamp | None, quantity: int) -> RiskDecision:
        """Return whether an entry of ``quantity`` contracts is allowed at ``when``."""
        c = self.config
        if self.emergency_active:
            return RiskDecision(False, "emergency_stop")
        if session_date is None or pd.isna(session_date):
            return RiskDecision(False, "outside_session")
        if quantity < 1:
            return RiskDecision(False, "invalid_quantity")
        if c.max_position_size and quantity > c.max_position_size:
            return RiskDecision(False, "max_position_size")
        if c.trading_windows:
            local = when.tz_convert(self.session.timezone).time()
            if not any(w.contains(local) for w in c.trading_windows):
                return RiskDecision(False, "outside_trading_window")
        day = self._day(session_date)
        if c.max_daily_loss and day.realized_pnl <= -c.max_daily_loss:
            return RiskDecision(False, "max_daily_loss")
        if c.daily_profit_target and day.realized_pnl >= c.daily_profit_target:
            return RiskDecision(False, "daily_profit_target")
        if c.max_daily_trades and day.trades >= c.max_daily_trades:
            return RiskDecision(False, "max_daily_trades")
        if c.max_consecutive_losses and self.consecutive_losses(session_date) >= c.max_consecutive_losses:
            return RiskDecision(False, "max_consecutive_losses")
        return APPROVED
