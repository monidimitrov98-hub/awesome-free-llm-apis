"""Risk engine foundation.

The :class:`RiskManager` can veto any entry before it is executed. The backtest
engine asks it twice: when the strategy's signal is processed and again at the
moment the order would fill. Exits are never blocked by risk limits.

Daily loss
----------
``daily_loss_basis = "realized"`` counts closed trades only (net of all costs).
``"realized_and_unrealized"`` also counts the open position:

    daily PnL = realized net PnL of the day
              + (mark - average entry fill) * signed qty * point value   (entry slippage
                is already inside the average entry fill)
              - entry commission already paid for the open position

Exit commission and exit slippage are not counted until they happen (they then
appear in the realized net PnL), so nothing is counted twice. Once the limit is
reached for a session date the day is marked as breached and stays breached, even
if prices recover. ``daily_loss_action`` decides what happens to an open position:
``"flatten"`` closes it (the engine treats the limit like a protective stop) and
``"block_entries"`` only blocks new entries.

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
    daily_loss_basis: str = "realized"  # "realized" | "realized_and_unrealized"
    daily_loss_action: str = "flatten"  # "flatten" | "block_entries" (unrealized basis only)

    def __post_init__(self) -> None:
        if self.daily_loss_basis not in {"realized", "realized_and_unrealized"}:
            raise ConfigError("risk.daily_loss_basis must be 'realized' or 'realized_and_unrealized'")
        if self.daily_loss_action not in {"flatten", "block_entries"}:
            raise ConfigError("risk.daily_loss_action must be 'flatten' or 'block_entries'")

    @property
    def tracks_unrealized_loss(self) -> bool:
        return bool(self.max_daily_loss) and self.daily_loss_basis == "realized_and_unrealized"

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
            daily_loss_basis=str(raw.get("daily_loss_basis", "realized")),
            daily_loss_action=str(raw.get("daily_loss_action", "flatten")),
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
    loss_limit_breached: bool = False


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

    def daily_pnl(self, session_date: pd.Timestamp, open_pnl: float = 0.0) -> float:
        """Daily PnL on the configured basis. ``open_pnl`` = unrealized gross PnL at the
        mark minus the entry commission already paid (ignored for the realized basis)."""
        realized = self._day(session_date).realized_pnl
        return realized + open_pnl if self.config.daily_loss_basis == "realized_and_unrealized" else realized

    def daily_loss_breached(self, session_date: pd.Timestamp) -> bool:
        return self._day(session_date).loss_limit_breached

    def mark_daily_loss_breached(self, session_date: pd.Timestamp) -> None:
        self._day(session_date).loss_limit_breached = True

    def check_daily_loss(self, session_date: pd.Timestamp, open_pnl: float = 0.0) -> bool:
        """Evaluate the daily loss limit; marks and returns True when it is reached."""
        limit = self.config.max_daily_loss
        if limit and self.daily_pnl(session_date, open_pnl) <= -limit + 1e-9:
            self.mark_daily_loss_breached(session_date)
        return self.daily_loss_breached(session_date)

    def daily_loss_trigger_price(
        self,
        *,
        session_date: pd.Timestamp,
        is_long: bool,
        quantity: int,
        avg_price: float,
        paid_costs: float,
        point_value: float,
        tick: float,
    ) -> float | None:
        """Price at which the open position brings the daily PnL to ``-max_daily_loss``.

        Rounded to the tick so that it triggers no later than the limit (up for a
        long, down for a short). None when the unrealized basis is not configured.
        """
        if not self.config.tracks_unrealized_loss or quantity <= 0:
            return None
        from src.backtest.execution import round_to_tick  # local import: risk must not depend on backtest at import

        room = -self.config.max_daily_loss - self._day(session_date).realized_pnl + paid_costs
        move = room / (quantity * point_value)  # <= 0 normally
        if is_long:
            return round_to_tick(avg_price + move, tick, "up")
        return round_to_tick(avg_price - move, tick, "down")

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
        if c.max_daily_loss and (day.loss_limit_breached or day.realized_pnl <= -c.max_daily_loss):
            return RiskDecision(False, "max_daily_loss")
        if c.daily_profit_target and day.realized_pnl >= c.daily_profit_target:
            return RiskDecision(False, "daily_profit_target")
        if c.max_daily_trades and day.trades >= c.max_daily_trades:
            return RiskDecision(False, "max_daily_trades")
        if c.max_consecutive_losses and self.consecutive_losses(session_date) >= c.max_consecutive_losses:
            return RiskDecision(False, "max_consecutive_losses")
        return APPROVED
