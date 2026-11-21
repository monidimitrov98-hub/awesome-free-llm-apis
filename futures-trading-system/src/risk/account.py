"""Account-level drawdown protection (generic, broker/prop-firm independent).

The :class:`AccountGuard` tracks account equity against a drawdown floor and
protects the account independently of any strategy:

* ``static``           floor = initial balance - max_drawdown (never moves)
* ``trailing_eod``     floor = highest END-OF-SESSION equity - max_drawdown
* ``trailing_intraday`` floor = highest INTRADAY equity - max_drawdown. With bars the
  intraday peak is unknown; ``intraday_peak_source = "favorable_extreme"``
  (default, conservative) raises the peak with each bar's most favourable price
  BEFORE the bar's adverse move is evaluated; ``"close"`` uses bar closes.

Optional ``floor_lock_offset``: the floor never rises above
``initial balance + floor_lock_offset`` (e.g. 0 = stops trailing at the starting balance).

Protections (all in dollars of equity above the floor = remaining buffer):

* new entries are blocked when the remaining buffer <= ``safety_buffer``;
* with ``require_trade_risk_within_buffer`` an entry is rejected when its actual risk
  (stop distance at the fill, commission and stop slippage) would leave less than
  ``safety_buffer`` if stopped out;
* when the buffer falls to ``flatten_buffer`` the open position is closed (the engine
  treats that equity level as a protective stop) and entries are locked for the rest of
  the session (``lock_after_flatten = "session"``) or for good (``"permanent"``);
* equity at or below the floor = breach: everything is locked permanently.

Equity = initial capital + realized net PnL (commissions included) + unrealized PnL
at the evaluated price.

Risk inputs are validated strictly: an entry whose risk is missing, NaN, infinite,
zero or negative is rejected (``account_risk_unknown``); with the guard enabled every
entry needs a valid protective stop-loss. ``gap_reserve_ticks`` (per contract) is added
to the trade risk as a conservative allowance for stops filling through gaps.

The engine places the emergency-flatten trigger so that, AFTER the estimated exit
commission and stop slippage, equity is still ``floor + flatten_buffer``. These are
ESTIMATES: a bar-based simulation cannot guarantee real-world fill prices, and no
finite reserve protects against every gap. An equity at or below the floor is always
recorded as a breach.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT, ConfigError

DRAWDOWN_TYPES = ("static", "trailing_eod", "trailing_intraday")


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class AccountRules:
    """Generic account rules. ``enabled = False`` disables the guard entirely."""

    enabled: bool = False
    profile: str = "disabled"
    drawdown_type: str = "static"
    max_drawdown: float = 0.0
    safety_buffer: float = 0.0
    flatten_buffer: float = 0.0
    floor_lock_offset: float | None = None
    intraday_peak_source: str = "favorable_extreme"
    lock_after_flatten: str = "session"
    require_trade_risk_within_buffer: bool = True
    gap_reserve_ticks: float = 0.0
    description: str = ""

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if self.drawdown_type not in DRAWDOWN_TYPES:
            raise ConfigError(f"account drawdown_type must be one of {DRAWDOWN_TYPES}")
        for name in ("max_drawdown", "safety_buffer", "flatten_buffer", "gap_reserve_ticks"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ConfigError(f"account {name} must be a finite number, got {value!r}")
        if self.max_drawdown <= 0:
            raise ConfigError("account max_drawdown must be positive")
        if not 0 < self.flatten_buffer <= self.safety_buffer < self.max_drawdown:
            raise ConfigError("account buffers must satisfy 0 < flatten_buffer <= safety_buffer < max_drawdown "
                              "(a zero flatten buffer would leave a flattened account exactly at the floor)")
        if self.gap_reserve_ticks < 0:
            raise ConfigError("account gap_reserve_ticks must be >= 0")
        if self.floor_lock_offset is not None:
            off = self.floor_lock_offset
            if isinstance(off, bool) or not isinstance(off, (int, float)) or not math.isfinite(off) or off < 0:
                # A negative offset would cap the floor BELOW the configured rule (weaker protection).
                raise ConfigError(f"account floor_lock_offset must be a finite number >= 0, got {off!r}")
        if self.intraday_peak_source not in {"favorable_extreme", "close"}:
            raise ConfigError("intraday_peak_source must be 'favorable_extreme' or 'close'")
        if self.lock_after_flatten not in {"session", "permanent"}:
            raise ConfigError("lock_after_flatten must be 'session' or 'permanent'")


def load_account_rules(path: str | Path) -> AccountRules:
    """Load a generic account-rule profile (TOML, section ``[account_rules]``)."""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.is_file():
        raise ConfigError(f"Account rule profile not found: {p}")
    with p.open("rb") as fh:
        raw = tomllib.load(fh).get("account_rules")
    if raw is None:
        raise ConfigError(f"{p} has no [account_rules] section")
    known = set(AccountRules.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"Unknown account rule(s) {sorted(unknown)} in {p}")
    try:
        return AccountRules(**raw)
    except TypeError as exc:
        raise ConfigError(f"Invalid account rules in {p}: {exc}") from exc


@dataclass
class AccountGuard:
    """Stateful drawdown protection for one simulated account."""

    rules: AccountRules
    initial_balance: float
    peak: float = field(init=False)
    breached: bool = field(default=False, init=False)
    locked_permanently: bool = field(default=False, init=False)
    locked_session: Any = field(default=None, init=False)
    min_buffer: float = field(init=False)
    events: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.peak = self.initial_balance
        self.min_buffer = self.rules.max_drawdown if self.rules.enabled else float("inf")

    @property
    def enabled(self) -> bool:
        return self.rules.enabled

    @property
    def floor(self) -> float:
        r = self.rules
        if not r.enabled:
            return float("-inf")
        base = self.initial_balance if r.drawdown_type == "static" else self.peak
        floor = base - r.max_drawdown
        if r.floor_lock_offset is not None:
            floor = min(floor, self.initial_balance + r.floor_lock_offset)
        return floor

    def remaining_buffer(self, equity: float) -> float:
        return equity - self.floor

    # --- updates -------------------------------------------------------
    def observe_intraday(self, equity: float, *, favorable: bool) -> None:
        """Raise the trailing peak (intraday model only)."""
        r = self.rules
        if not r.enabled or r.drawdown_type != "trailing_intraday":
            return
        if favorable and r.intraday_peak_source != "favorable_extreme":
            return  # peak tracked from closes only
        self.peak = max(self.peak, equity)

    def on_session_end(self, equity: float, session_date: Any) -> None:
        """End-of-session update (end-of-day trailing model)."""
        if self.rules.enabled and self.rules.drawdown_type == "trailing_eod":
            self.peak = max(self.peak, equity)
        if self.locked_session is not None and self.locked_session != session_date:
            self.locked_session = None

    def evaluate(self, equity: float, when: Any, session_date: Any, position_open: bool = True) -> str | None:
        """Evaluate equity (use the worst price of the bar). Returns 'breach', 'flatten' or None.

        A breach is detected in any state; the flatten lock only applies while a position
        is (or was during the bar) open. When flat, the safety buffer blocks new entries.
        """
        if not self.rules.enabled:
            return None
        buffer = self.remaining_buffer(equity)
        self.min_buffer = min(self.min_buffer, buffer)
        if buffer <= 1e-9 and not self.breached:
            self.breached = self.locked_permanently = True
            self.events.append({"time": when, "event": "drawdown_breach", "equity": equity, "floor": self.floor})
            return "breach"
        if position_open and buffer <= self.rules.flatten_buffer + 1e-9 and not self.is_locked(session_date):
            self._lock(when, session_date, equity)
            return "flatten"
        return None

    def register_flatten(self, when: Any, session_date: Any, equity: float) -> None:
        """An emergency flatten happened: lock entries (session or permanently) regardless of
        where the exit filled, then re-evaluate for a breach."""
        if not self.rules.enabled:
            return
        if not self.is_locked(session_date):
            self._lock(when, session_date, equity)
        self.evaluate(equity, when, session_date, position_open=False)

    def _lock(self, when: Any, session_date: Any, equity: float) -> None:
        if self.rules.lock_after_flatten == "permanent":
            self.locked_permanently = True
        else:
            self.locked_session = session_date
        self.events.append({"time": when, "event": "flatten_buffer_reached", "equity": equity, "floor": self.floor})

    def is_locked(self, session_date: Any) -> bool:
        return self.locked_permanently or (self.locked_session is not None and self.locked_session == session_date)

    # --- checks --------------------------------------------------------
    def entry_block_reason(self, equity: float, session_date: Any, trade_risk: float | None = None) -> str | None:
        """Why a new entry must not be opened (None when allowed).

        ``trade_risk=None`` is the pre-trade (signal-time) check without a known risk. At
        the fill use :meth:`fill_block_reason`, which REQUIRES a valid risk value.
        """
        r = self.rules
        if not r.enabled:
            return None
        if not _finite(equity):
            return "account_equity_unknown"
        if trade_risk is not None and not (_finite(trade_risk) and trade_risk > 0):
            return "account_risk_unknown"
        if self.breached:
            return "account_drawdown_breached"
        if self.is_locked(session_date):
            return "account_locked_after_flatten"
        buffer = self.remaining_buffer(equity)
        if buffer <= r.safety_buffer + 1e-9:
            return "account_safety_buffer"
        if trade_risk is not None and r.require_trade_risk_within_buffer and buffer - trade_risk < r.safety_buffer - 1e-9:
            return "account_trade_risk_exceeds_buffer"
        return None

    def fill_block_reason(self, equity: float, session_date: Any, trade_risk: float | None) -> str | None:
        """Fill-time check: the trade's risk must be a finite positive number."""
        if not self.rules.enabled:
            return None
        if trade_risk is None or not (_finite(trade_risk) and trade_risk > 0):
            return "account_risk_unknown"
        return self.entry_block_reason(equity, session_date, trade_risk)

    def flatten_equity_level(self) -> float | None:
        """Equity at which an open position must be closed (floor + flatten_buffer)."""
        return self.floor + self.rules.flatten_buffer if self.rules.enabled else None

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.rules.enabled,
            "profile": self.rules.profile,
            "drawdown_type": self.rules.drawdown_type,
            "max_drawdown": self.rules.max_drawdown,
            "final_floor": self.floor,
            "peak": self.peak,
            "min_remaining_buffer": self.min_buffer,
            "breached": self.breached,
            "events": list(self.events),
        }
