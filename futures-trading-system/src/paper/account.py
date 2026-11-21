"""Persistent paper account. Uses the EXISTING AccountGuard and RiskManager (no second,
weaker risk engine). Every balance or position change comes from an :class:`Execution`."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.backtest.config import BacktestConfig
from src.backtest.position import Position
from src.config import ContractSpec, SessionConfig
from src.paper.orders import Execution
from src.risk.account import AccountGuard
from src.risk.manager import RiskManager, _DayState


class AccountStateError(RuntimeError):
    """The account state is invalid or inconsistent."""


@dataclass
class OpenTrade:
    root: str
    side: str
    entry_time: pd.Timestamp
    entry_order_id: str
    quantity: int = 0
    entry_value: float = 0.0          # sum(qty * price) of entry fills
    commission: float = 0.0
    slippage: float = 0.0
    stop_loss: float | None = None
    signal_id: str | None = None
    session_date: pd.Timestamp | None = None
    context: dict[str, Any] = field(default_factory=dict)  # regime at signal etc. (reporting only)
    max_qty: int = 0

    @property
    def avg_entry(self) -> float:
        return self.entry_value / self.max_qty if self.max_qty else math.nan


class PaperAccount:
    def __init__(self, bt: BacktestConfig, specs: dict[str, ContractSpec], session: SessionConfig) -> None:
        self.bt = bt
        self.specs = specs
        self.session = session
        self.initial_balance = float(bt.initial_capital)
        self.positions = {r: Position(point_value=s.point_value) for r, s in specs.items()}
        self.realized_gross = 0.0
        self.commissions = 0.0
        self.slippage = 0.0
        self.marks: dict[str, float] = {}
        self.high_water = self.initial_balance
        self.max_drawdown = 0.0
        self.daily_net: dict[str, float] = {}
        self.open_trades: dict[str, OpenTrade] = {}
        self.closed_trades: list[dict[str, Any]] = []
        self.guard = AccountGuard(bt.account_rules, self.initial_balance)
        self.risk = RiskManager(bt.risk, session)
        self.applied_exec_ids: set[str] = set()

    # ------------------------------------------------------------------ valuation
    def mark(self, root: str, price: float) -> None:
        if not (math.isfinite(price) and price > 0):
            raise AccountStateError(f"invalid mark price {price!r}")
        self.marks[root] = price

    def unrealized(self) -> float:
        total = 0.0
        for r, pos in self.positions.items():
            if not pos.is_flat:
                m = self.marks.get(r)
                if m is None:
                    return math.nan  # unknown: never assume a value
                total += pos.unrealized_pnl(m)
        return total

    def known_equity(self) -> float:
        """Balance plus the unrealized PnL of the positions that HAVE a mark (unmarked ones excluded).
        Used only for protective checks while full equity is unknown; never for entries or peaks."""
        total = 0.0
        for r, pos in self.positions.items():
            m = self.marks.get(r)
            if not pos.is_flat and m is not None:
                total += pos.unrealized_pnl(m)
        return self.initial_balance + self.net_realized + total

    @property
    def net_realized(self) -> float:
        return self.realized_gross - self.commissions

    def equity(self) -> float:
        return self.initial_balance + self.net_realized + self.unrealized()

    def update_drawdown(self) -> None:
        eq = self.equity()
        if math.isfinite(eq):
            self.high_water = max(self.high_water, eq)
            self.max_drawdown = max(self.max_drawdown, self.high_water - eq)

    def position_qty(self, root: str) -> int:
        return self.positions[root].quantity

    # ------------------------------------------------------------------ bookings
    def apply(self, ex: Execution, session_date: pd.Timestamp | None, entry_meta: dict[str, Any] | None = None
              ) -> dict[str, Any] | None:
        """Book an execution; returns the closed-trade record when it closes a trade."""
        if ex.exec_id in self.applied_exec_ids:
            raise AccountStateError(f"execution {ex.exec_id} applied twice")
        self.applied_exec_ids.add(ex.exec_id)
        pos = self.positions[ex.root]
        before = pos.quantity
        realized = pos.apply_fill(ex.is_buy, ex.quantity, ex.fill_price)
        self.realized_gross += realized
        self.commissions += ex.commission
        self.slippage += ex.slippage_cost
        if ex.root not in self.marks:
            self.mark(ex.root, ex.fill_price)
        key = str(session_date.date()) if session_date is not None else "outside_session"
        self.daily_net[key] = self.daily_net.get(key, 0.0) + realized - ex.commission
        t = self.open_trades.get(ex.root)
        if ex.role == "entry":
            if t is None:
                meta = entry_meta or {}
                t = OpenTrade(ex.root, "long" if ex.is_buy else "short", ex.ts, ex.order_id,
                              stop_loss=meta.get("stop_loss"), signal_id=meta.get("signal_id"),
                              session_date=session_date, context=meta.get("context", {}))
                self.open_trades[ex.root] = t
                self.risk.on_entry_filled(session_date) if session_date is not None else None
            t.quantity += ex.quantity
            t.max_qty += ex.quantity
            t.entry_value += ex.quantity * ex.fill_price
            t.commission += ex.commission
            t.slippage += ex.slippage_cost
            self.update_drawdown()
            return None
        if t is None:
            raise AccountStateError(f"exit execution {ex.exec_id} without an open trade on {ex.root}")
        t.commission += ex.commission
        t.slippage += ex.slippage_cost
        t.context.setdefault("exit_gross", 0.0)
        t.context["exit_gross"] += realized
        t.quantity -= ex.quantity
        if before != 0 and pos.quantity == 0:
            del self.open_trades[ex.root]
            gross = t.context.pop("exit_gross")
            net = gross - t.commission
            spec = self.specs[ex.root]
            risk = (abs(t.avg_entry - t.stop_loss) * spec.point_value * t.max_qty) if t.stop_loss else math.nan
            rec = {"root": ex.root, "side": t.side, "quantity": t.max_qty, "entry_time": t.entry_time.isoformat(),
                   "exit_time": ex.ts.isoformat(), "entry_price": t.avg_entry, "exit_price": ex.fill_price,
                   "exit_reason": ex.role if ex.role in ("stop_loss", "take_profit") else ex.reason,
                   "exit_role": ex.role, "gross_pnl": gross, "commission": t.commission, "slippage_cost": t.slippage,
                   "net_pnl": net, "r_multiple": net / risk if risk and math.isfinite(risk) and risk > 0 else math.nan,
                   "signal_id": t.signal_id, "session_date": str(t.session_date.date()) if t.session_date is not None else None,
                   **{f"ctx_{k}": v for k, v in t.context.items()}}
            self.closed_trades.append(rec)
            if session_date is not None:
                self.risk.on_trade_closed(net, session_date)
            self.update_drawdown()
            return rec
        self.update_drawdown()
        return None

    # ------------------------------------------------------------------ persistence
    def state(self) -> dict[str, Any]:
        g = self.guard
        return {
            "initial_balance": self.initial_balance, "realized_gross": self.realized_gross,
            "commissions": self.commissions, "slippage": self.slippage, "marks": self.marks,
            "high_water": self.high_water, "max_drawdown": self.max_drawdown, "daily_net": self.daily_net,
            "positions": {r: {"quantity": p.quantity, "avg_price": p.avg_price} for r, p in self.positions.items()},
            "open_trades": {r: {**{k: (v.isoformat() if isinstance(v, pd.Timestamp) else v)
                                   for k, v in t.__dict__.items()}} for r, t in self.open_trades.items()},
            "closed_trades": self.closed_trades, "applied_exec_ids": sorted(self.applied_exec_ids),
            "guard": {"peak": g.peak, "breached": g.breached, "locked_permanently": g.locked_permanently,
                      "locked_session": str(g.locked_session) if g.locked_session is not None else None,
                      "min_buffer": g.min_buffer if math.isfinite(g.min_buffer) else None, "events": [
                          {k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in e.items()} for e in g.events]},
            "risk": {"days": {str(k.date()): vars(v) for k, v in self.risk._days.items()},
                     "consecutive_losses": self.risk._consecutive_losses,
                     "last_loss_day": str(self.risk._last_loss_day) if self.risk._last_loss_day is not None else None,
                     "emergency_reason": self.risk._emergency_reason},
        }

    def restore(self, st: dict[str, Any]) -> None:
        if float(st["initial_balance"]) != self.initial_balance:
            raise AccountStateError("stored initial balance differs from the configuration")
        self.realized_gross, self.commissions, self.slippage = (float(st[k]) for k in ("realized_gross", "commissions",
                                                                                       "slippage"))
        self.high_water, self.max_drawdown = float(st["high_water"]), float(st["max_drawdown"])
        self.daily_net = dict(st["daily_net"])
        for r, p in st["positions"].items():
            self.positions[r].quantity, self.positions[r].avg_price = int(p["quantity"]), float(p["avg_price"])
        self.open_trades = {}
        for r, t in st["open_trades"].items():
            t = dict(t)
            for k in ("entry_time", "session_date"):
                if t.get(k) is not None:
                    t[k] = pd.Timestamp(t[k])
            self.open_trades[r] = OpenTrade(**t)
        self.closed_trades = list(st["closed_trades"])
        self.applied_exec_ids = set(st["applied_exec_ids"])
        g = st["guard"]
        self.guard.peak, self.guard.breached = float(g["peak"]), bool(g["breached"])
        self.guard.locked_permanently = bool(g["locked_permanently"])
        self.guard.locked_session = pd.Timestamp(g["locked_session"]) if g["locked_session"] else None
        self.guard.min_buffer = float(g["min_buffer"]) if g["min_buffer"] is not None else math.inf
        self.guard.events = list(g["events"])
        rk = st["risk"]
        self.risk._days = {pd.Timestamp(k): _DayState(**v) for k, v in rk["days"].items()}
        self.risk._consecutive_losses = int(rk["consecutive_losses"])
        self.risk._last_loss_day = pd.Timestamp(rk["last_loss_day"]) if rk["last_loss_day"] else None
        if rk["emergency_reason"]:
            self.risk._emergency_reason = rk["emergency_reason"]
        self.marks = {}  # never trust stored prices: wait for fresh market data

    def summary(self) -> dict[str, Any]:
        eq = self.equity()
        return {"initial_balance": self.initial_balance, "equity": eq, "realized_gross": self.realized_gross,
                "commissions": self.commissions, "net_realized": self.net_realized, "unrealized": self.unrealized(),
                "slippage": self.slippage, "high_water": self.high_water, "max_drawdown": self.max_drawdown,
                "positions": {r: p.quantity for r, p in self.positions.items()},
                "drawdown_floor": self.guard.floor if self.guard.enabled else None,
                "remaining_buffer": self.guard.remaining_buffer(eq) if self.guard.enabled and math.isfinite(eq) else None,
                "guard": {"breached": self.guard.breached, "locked_permanently": self.guard.locked_permanently,
                          "locked_session": str(self.guard.locked_session) if self.guard.locked_session is not None
                          else None},
                "daily_net": self.daily_net}
