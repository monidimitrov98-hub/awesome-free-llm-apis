"""Event-driven backtesting engine (historical simulation only, no broker).

Timeline
--------
Two bar streams are merged into one chronological event queue:

* **Signal bars** (1m/5m/15m) produce a ``SignalEvent`` at their ``available_at``
  (bar open + timeframe). Only then is the strategy called, with history limited
  to bars whose ``available_at`` <= now.
* **Execution bars** (default: the same frame; typically 1m) produce an
  ``ExecutionEvent`` at their open time. Pending orders are simulated against that
  bar's OHLC, then the position is marked to market at its close (recorded at the
  bar's ``available_at``).

At equal timestamps signal events are processed first, so an order created at time
T can only fill on an execution bar that opens at or after T: a signal computed at
the close of bar k executes, at the earliest, at the open of the next bar.

Checks at the actual entry fill
-------------------------------
Signal-time checks use a reference price (the signal bar close for market orders,
the order price otherwise). When an entry would fill, the engine re-checks against
the simulated fill price (slippage included) BEFORE opening anything:

1. Bracket validity: a long needs ``stop_loss < fill < take_profit``, a short
   ``take_profit < fill < stop_loss``. A gap that breaks this rejects the entry
   (``invalid_bracket_at_fill``); an invalid protective bracket is never created.
2. Risk budget (entries sized by ``sizing.method = "risk"``): the actual risk
   ``qty * risk_per_contract(fill, stop)`` (round-trip commission and stop-exit
   slippage included; entry slippage is inside the fill price) must not exceed
   ``risk_per_trade``. ``sizing.fill_risk_policy = "reduce"`` lowers the quantity to
   ``floor(risk_per_trade / risk_per_contract)`` (logged in ``adjustments``; rejected
   if that is 0); ``"reject"`` rejects the entry. The quantity is never increased.
3. The risk engine (windows, daily limits, size) is asked again.

Daily loss with unrealized PnL
------------------------------
With ``risk.daily_loss_basis = "realized_and_unrealized"`` the limit is evaluated on
every execution event while a position is open, at the bar's most adverse price
(low for a long, high for a short), after this bar's fills. With
``daily_loss_action = "flatten"`` the price at which the daily PnL reaches the limit
is treated like a protective stop (gap rule: fill at the open if the bar opens
beyond it; exit reason ``daily_loss_limit``). The bracket stop and this level are
both stops on the same side, so whichever is closer to the market triggers first
without ambiguity. With ``"block_entries"`` the position is kept but no new entry
is allowed that day. Exits are never blocked.

Ambiguity
---------
OHLC bars do not tell the order of prices inside a bar. When a stop-loss and a
take-profit could both have been hit in the same bar (and neither at the open), the
stop-loss is assumed to fill first (worst case) and the trade is flagged
``ambiguous``. On the bar where a stop/limit entry filled intrabar, a take-profit is
not credited and a stop-loss within the bar range is assumed hit (also flagged).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.config import BacktestConfig
from src.backtest.execution import BarPrices, SimFill, conservative_order_price, simulate_fill, apply_slippage
from src.backtest.metrics import compute_metrics
from src.backtest.orders import Fill, Order, OrderRequest, OrderRole, OrderStatus, OrderType, Side
from src.backtest.position import Position
from src.backtest.sizing import position_size, risk_per_contract
from src.config import ContractSpec, SessionConfig
from src.data.errors import DataError
from src.data.sessions import session_bounds
from src.risk.manager import RiskManager
from src.strategy.base import PositionSnapshot, Strategy, StrategyContext

REQUIRED_COLUMNS = ("open", "high", "low", "close", "is_complete", "available_at", "session_date")
EXIT_ROLES = (OrderRole.EXIT, OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT)


@dataclass
class BacktestResult:
    """Everything produced by one backtest run."""

    trades: pd.DataFrame
    orders: pd.DataFrame
    fills: pd.DataFrame
    equity: pd.DataFrame
    rejections: pd.DataFrame
    daily_pnl: pd.Series
    metrics: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    adjustments: pd.DataFrame = field(default_factory=pd.DataFrame)


def _validate_bars(df: pd.DataFrame, name: str) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"{name} bars are missing columns {missing}; use the Sprint 1 pipeline output")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise DataError(f"{name} bars need a tz-aware DatetimeIndex")
    if df.empty:
        raise DataError(f"{name} bars are empty")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise DataError(f"{name} bars must be sorted with unique timestamps")
    if df[["open", "high", "low", "close"]].isna().any().any():
        raise DataError(f"{name} bars contain NaN prices; missing data must not be filled")
    if not (pd.DatetimeIndex(df["available_at"]) > df.index).all():
        raise DataError(f"{name} bars: available_at must be after the bar open")


def _bar_length(df: pd.DataFrame) -> pd.Timedelta:
    lengths = pd.DatetimeIndex(df["available_at"]) - df.index
    if lengths.nunique() != 1:
        raise DataError("All bars of one frame must have the same length")
    return pd.Timedelta(lengths[0])


@dataclass
class _OpenTrade:
    side: Side
    quantity: int
    entry_order: Order
    entry_fill: Fill
    signal_time: pd.Timestamp | None
    session_date: pd.Timestamp
    stop_loss: float | None
    take_profit: float | None


class BacktestEngine:
    """Runs one strategy over historical bars for one contract.

    Args:
        config: Backtest configuration.
        contract: Contract specification (tick size and point value).
        session: Trading session (defines session dates and the end-of-session flatten).
    """

    def __init__(self, config: BacktestConfig, contract: ContractSpec, session: SessionConfig) -> None:
        self.config = config
        self.contract = contract
        self.session = session
        self.cost = config.cost_for(contract.root)

    def run(
        self,
        strategy: Strategy,
        signal_bars: pd.DataFrame,
        execution_bars: pd.DataFrame | None = None,
        *,
        risk_manager: RiskManager | None = None,
    ) -> BacktestResult:
        """Run the backtest. Inputs are not modified."""
        sim = _Simulation(self, strategy, signal_bars, execution_bars, risk_manager)
        return sim.run()


class _Simulation:
    """State of one run. Kept separate so the engine object is reusable."""

    def __init__(
        self,
        engine: BacktestEngine,
        strategy: Strategy,
        signal_bars: pd.DataFrame,
        execution_bars: pd.DataFrame | None,
        risk_manager: RiskManager | None,
    ) -> None:
        self.cfg = engine.config
        self.spec = engine.contract
        self.session = engine.session
        self.cost = engine.cost
        self.strategy = strategy
        self.signal = signal_bars.copy()
        self.execution = (execution_bars if execution_bars is not None else signal_bars).copy()
        _validate_bars(self.signal, "signal")
        _validate_bars(self.execution, "execution")
        self.signal_len = _bar_length(self.signal)
        self.exec_len = _bar_length(self.execution)
        if self.exec_len > self.signal_len:
            raise DataError("Execution bars must not be longer than signal bars")
        self.risk = risk_manager or RiskManager(self.cfg.risk, self.session)

        self.position = Position(point_value=self.spec.point_value)
        self.open_trade: _OpenTrade | None = None
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.trades: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self.adjustments: list[dict[str, Any]] = []
        self.equity_rows: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.realized_net = 0.0
        self.processed_signals: set[pd.Timestamp] = set()
        self._next_id = 1
        self._bounds_cache: dict[pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]] = {}
        self._last_mark: float | None = None

    # ------------------------------------------------------------------ helpers
    def _session_end(self, session_date: pd.Timestamp) -> pd.Timestamp:
        if session_date not in self._bounds_cache:
            b = session_bounds([session_date], self.session).iloc[0]
            self._bounds_cache[session_date] = (b["start"], b["end"])
        return self._bounds_cache[session_date][1]

    def _flatten_time(self, session_date: pd.Timestamp) -> pd.Timestamp:
        return self._session_end(session_date) - self.cfg.execution.flatten_before_session_end

    def _new_order(self, *, register: bool = True, **kwargs: Any) -> Order:
        order = Order(id=self._next_id, **kwargs)
        self._next_id += 1
        if register:
            self.orders.append(order)
        return order

    def _pending(self, roles: tuple[OrderRole, ...]) -> list[Order]:
        return [o for o in self.orders if o.is_active and o.role in roles]

    def _cancel(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.CANCELLED
        order.reason = reason

    def _reject(self, when: pd.Timestamp, stage: str, reason: str, detail: str = "", order: Order | None = None) -> None:
        self.rejections.append({"time": when, "stage": stage, "reason": reason, "detail": detail,
                                "order_id": order.id if order else None})
        if order is not None:
            order.status = OrderStatus.REJECTED
            order.reason = reason

    def _snapshot(self) -> PositionSnapshot:
        if self.position.is_flat:
            return PositionSnapshot(None, 0, 0.0, 0.0)
        side = Side.LONG if self.position.quantity > 0 else Side.SHORT
        mark = self._last_mark if self._last_mark is not None else self.position.avg_price
        return PositionSnapshot(side, abs(self.position.quantity), self.position.avg_price,
                                self.position.unrealized_pnl(mark))

    # ------------------------------------------------------------------ main loop
    def run(self) -> BacktestResult:
        events: list[tuple[pd.Timestamp, int, int]] = []
        sig_avail = pd.DatetimeIndex(self.signal["available_at"])
        events += [(t, 0, k) for k, t in enumerate(sig_avail)]
        events += [(t, 1, j) for j, t in enumerate(self.execution.index)]
        events.sort(key=lambda e: (e[0], e[1], e[2]))

        self.strategy.on_start(self.spec)
        sig = self.signal
        self._sig_open = sig["open"].to_numpy(float)
        ex = self.execution
        self._ex = {
            c: ex[c].to_numpy() for c in ("open", "high", "low", "close", "is_complete", "session_date", "available_at")
        }
        for when, kind, pos in events:
            if kind == 0:
                self._on_signal(pos, when)
            else:
                self._on_execution(pos)
        self._finish()
        return self._result()

    # ------------------------------------------------------------------ signal events
    def _on_signal(self, k: int, now: pd.Timestamp) -> None:
        bar_time = self.signal.index[k]
        if bar_time in self.processed_signals:  # defensive: each bar is decided once
            return
        self.processed_signals.add(bar_time)
        bar = self.signal.iloc[k]
        complete = bool(bar["is_complete"])
        if not complete and self.cfg.execution.incomplete_signal_bar_policy == "skip":
            self._reject(now, "signal", "incomplete_signal_bar_skipped", str(bar_time))
            return
        history = self.signal.iloc[: k + 1]
        assert (pd.DatetimeIndex(history["available_at"]) <= now).all(), "look-ahead guard"
        session_date = bar["session_date"] if pd.notna(bar["session_date"]) else None
        ctx = StrategyContext(
            now=now,
            bar=bar,
            history=history,
            bar_is_complete=complete,
            session_date=session_date,
            position=self._snapshot(),
            has_pending_entry=bool(self._pending((OrderRole.ENTRY,))),
            contract=self.spec,
        )
        requests = self.strategy.on_bar(ctx) or []
        entry_seen = False
        for req in requests:
            if not isinstance(req, OrderRequest):
                raise TypeError("Strategy.on_bar must return OrderRequest objects")
            if req.action == "entry":
                if entry_seen:
                    self._reject(now, "signal", "duplicate_signal", "more than one entry from one bar")
                    continue
                entry_seen = True
                self._submit_entry(req, now, bar_time, float(bar["close"]), session_date)
            elif req.action == "exit":
                self._submit_exit(now, req.tag)
            elif req.action == "cancel_entry":
                for o in self._pending((OrderRole.ENTRY,)):
                    self._cancel(o, "cancelled_by_strategy")
            else:
                raise ValueError(f"Unknown request action {req.action!r}")

    def _submit_entry(self, req: OrderRequest, now: pd.Timestamp, bar_time: pd.Timestamp,
                      ref_close: float, session_date: pd.Timestamp | None) -> None:
        if not self.position.is_flat:
            self._reject(now, "signal", "position_open", req.tag)
            return
        if self._pending((OrderRole.ENTRY,)):
            self._reject(now, "signal", "entry_pending", req.tag)
            return
        assert req.side is not None
        is_buy = req.side is Side.LONG
        tick = self.spec.tick_size
        price = None
        if req.order_type is not OrderType.MARKET:
            if req.price is None or req.price <= 0:
                self._reject(now, "signal", "missing_order_price", req.tag)
                return
            price = conservative_order_price(req.price, tick, req.order_type, is_buy)
        ref = price if price is not None else ref_close
        # Brackets: protective stop and target, rounded so they are harder to trigger.
        stop = (conservative_order_price(req.stop_loss, tick, OrderType.STOP, not is_buy)
                if req.stop_loss is not None else None)
        target = (conservative_order_price(req.take_profit, tick, OrderType.LIMIT, not is_buy)
                  if req.take_profit is not None else None)
        sgn = req.side.sign
        if stop is not None and (stop - ref) * sgn >= 0:
            self._reject(now, "signal", "invalid_bracket", f"stop_loss {stop} not beyond entry {ref}")
            return
        if target is not None and (target - ref) * sgn <= 0:
            self._reject(now, "signal", "invalid_bracket", f"take_profit {target} not beyond entry {ref}")
            return
        size = position_size(self.cfg.sizing, self.spec, entry_price=ref, stop_loss=stop,
                             requested=req.quantity, cost=self.cost, slippage=self.cfg.slippage,
                             order_type=req.order_type)
        if size.quantity < 1:
            self._reject(now, "signal", size.reason, req.tag)
            return
        if session_date is not None and now >= self._flatten_time(session_date):
            self._reject(now, "signal", "session_end_cutoff", req.tag)
            return
        decision = self.risk.check_entry(when=now, session_date=session_date, quantity=size.quantity)
        if not decision.approved:
            self._reject(now, "signal", decision.reason, req.tag)
            return
        expire = now + self.cfg.execution.max_fill_delay if req.order_type is OrderType.MARKET else None
        self._new_order(role=OrderRole.ENTRY, order_type=req.order_type, is_buy=is_buy,
                        quantity=size.quantity, price=price, created_at=now, active_from=now,
                        signal_time=bar_time, session_date=session_date, expire_at=expire,
                        tag=req.tag, stop_loss=stop, take_profit=target,
                        risk_budget=(self.cfg.sizing.risk_per_trade
                                     if self.cfg.sizing.method == "risk" and req.quantity is None else None),
                        requested_quantity=size.quantity)

    def _submit_exit(self, now: pd.Timestamp, tag: str) -> None:
        if self.position.is_flat:
            self._reject(now, "signal", "no_position_to_exit", tag)
            return
        if self._pending((OrderRole.EXIT,)):
            self._reject(now, "signal", "exit_pending", tag)
            return
        self._new_order(role=OrderRole.EXIT, order_type=OrderType.MARKET, is_buy=self.position.quantity < 0,
                        quantity=abs(self.position.quantity), price=None, created_at=now, active_from=now,
                        signal_time=None, session_date=self.open_trade.session_date if self.open_trade else None,
                        tag=tag or "strategy_exit")

    # ------------------------------------------------------------------ execution events
    def _on_execution(self, j: int) -> None:
        ex = self._ex
        t = self.execution.index[j]
        bar = BarPrices(float(ex["open"][j]), float(ex["high"][j]), float(ex["low"][j]), float(ex["close"][j]))
        complete = bool(ex["is_complete"][j])
        sd = ex["session_date"][j]
        session_date = pd.Timestamp(sd) if not pd.isna(sd) else None

        # 1. Session / emergency housekeeping (uses only configuration and this bar's open).
        if self.open_trade is not None:
            reason = None
            if self.risk.emergency_active:
                reason = "emergency_stop"
            elif session_date is None:
                reason = "outside_session"
            elif session_date != self.open_trade.session_date:
                reason = "session_gap"
                self.warnings.append(f"{t}: position from session {self.open_trade.session_date.date()} "
                                     "still open in a new session (missing data); closed at this bar's open")
            elif t >= self._flatten_time(session_date):
                reason = "session_end"
            if reason:
                self._force_exit(t, bar, complete, reason)
        for o in self._pending((OrderRole.ENTRY,)):
            if self.risk.emergency_active:
                self._cancel(o, "emergency_stop")
            elif session_date is None or o.session_date is None or session_date != o.session_date:
                self._cancel(o, "session_changed")
            elif t >= self._flatten_time(session_date):
                self._cancel(o, "session_end")
            elif o.expire_at is not None and t > o.expire_at:
                self._cancel(o, "fill_delay_exceeded")
                self.warnings.append(f"{t}: market entry from {o.created_at} cancelled, no bar within "
                                     f"{self.cfg.execution.max_fill_delay} (data gap)")

        # 2. Entry fills (only when flat).
        if self.open_trade is None:
            for o in self._pending((OrderRole.ENTRY,)):
                if o.active_from > t:
                    continue
                if not complete and not self.cfg.execution.entries_on_incomplete_bars:
                    continue  # wait for a complete bar (market orders may then expire)
                sim = self._simulate(o, bar)
                if sim is None:
                    continue
                problem = self._bracket_problem(o, sim.price)
                if problem:
                    self._reject(t, "fill", "invalid_bracket_at_fill", problem, order=o)
                    continue
                if not self._fit_risk_budget(o, sim.price, t):
                    continue
                decision = self.risk.check_entry(when=t, session_date=session_date, quantity=o.quantity)
                if not decision.approved:
                    self._reject(t, "fill", decision.reason, o.tag, order=o)
                    continue
                self._open(o, sim, t, complete, session_date)
                break

        # 3. Exits for the open position.
        if self.open_trade is not None:
            self._process_exits(t, bar, complete, session_date)

        # 3b. Daily loss incl. unrealized PnL, evaluated at this bar's most adverse price.
        if self.open_trade is not None and session_date is not None and self.cfg.risk.tracks_unrealized_loss:
            worst = bar.low if self.position.quantity > 0 else bar.high
            if self.risk.check_daily_loss(session_date, self._open_pnl(worst)):
                self.warnings.append(f"{t}: daily loss limit reached with an open position "
                                     f"(action: {self.cfg.risk.daily_loss_action})")

        # 4. Mark to market at this bar's close, recorded when the close is known.
        self._last_mark = bar.close
        unrealized = self.position.unrealized_pnl(bar.close)
        self.equity_rows.append({
            "time": pd.Timestamp(ex["available_at"][j]),
            "equity": self.cfg.initial_capital + self.realized_net + unrealized,
            "realized": self.realized_net,
            "unrealized": unrealized,
            "position": self.position.quantity,
            "session_date": session_date,
        })

    def _open_pnl(self, mark: float) -> float:
        """Unrealized gross PnL at ``mark`` minus the entry commission already paid."""
        assert self.open_trade is not None
        return self.position.unrealized_pnl(mark) - self.open_trade.entry_fill.commission

    def _bracket_problem(self, order: Order, fill_price: float) -> str:
        """Why the order's bracket is invalid at ``fill_price`` ('' when valid)."""
        sgn = 1 if order.is_buy else -1
        if order.stop_loss is not None and (order.stop_loss - fill_price) * sgn >= 0:
            return f"stop_loss {order.stop_loss} not beyond fill {fill_price}"
        if order.take_profit is not None and (order.take_profit - fill_price) * sgn <= 0:
            return f"take_profit {order.take_profit} not beyond fill {fill_price}"
        return ""

    def _fit_risk_budget(self, order: Order, fill_price: float, t: pd.Timestamp) -> bool:
        """Apply sizing.fill_risk_policy at the actual fill. False when the entry is rejected."""
        if order.risk_budget is None or order.stop_loss is None:
            return True
        z = self.cfg.sizing
        per = risk_per_contract(self.spec, entry_price=fill_price, stop_loss=order.stop_loss, cost=self.cost,
                                slippage=self.cfg.slippage, include_costs=z.include_costs_in_risk,
                                entry_slippage_ticks=0)
        if order.quantity * per <= order.risk_budget + 1e-9:
            return True
        detail = (f"actual risk {order.quantity} x {per:.2f} = {order.quantity * per:.2f} "
                  f"> budget {order.risk_budget:.2f} at fill {fill_price}")
        fitted = int(order.risk_budget // per + 1e-9) if per > 0 else 0
        if z.fill_risk_policy == "reject" or fitted < 1:
            self._reject(t, "fill", "risk_budget_exceeded_at_fill", detail, order=order)
            return False
        self.adjustments.append({"time": t, "order_id": order.id, "reason": "quantity_reduced_to_risk_budget",
                                 "from_quantity": order.quantity, "to_quantity": fitted,
                                 "risk_per_contract": per, "detail": detail})
        order.quantity = fitted
        return True

    def _simulate(self, order: Order, bar: BarPrices) -> SimFill | None:
        s = self.cfg.slippage
        ticks = {OrderType.MARKET: s.market_ticks, OrderType.STOP: s.stop_ticks, OrderType.LIMIT: s.limit_ticks}
        return simulate_fill(bar, order.order_type, order.is_buy, order.price, tick=self.spec.tick_size,
                             slippage_ticks=ticks[order.order_type],
                             trade_through=self.cfg.execution.limit_fill_requires_trade_through)

    def _record_fill(self, order: Order, sim: SimFill, t: pd.Timestamp, complete: bool) -> Fill:
        commission = self.cost.commission_per_contract_per_side * order.quantity
        slip_cost = abs(sim.price - sim.base_price) * order.quantity * self.spec.point_value
        fill = Fill(order.id, order.role, order.is_buy, order.quantity, sim.price, t, sim.at_open,
                    commission, slip_cost, complete, base_price=sim.base_price)
        order.status, order.filled_at, order.fill_price = OrderStatus.FILLED, t, sim.price
        self.fills.append(fill)
        self.realized_net -= commission
        self.strategy.on_fill(fill)
        return fill

    def _open(self, order: Order, sim: SimFill, t: pd.Timestamp, complete: bool, session_date: pd.Timestamp) -> None:
        fill = self._record_fill(order, sim, t, complete)
        self.position.apply_fill(order.is_buy, order.quantity, sim.price)
        self.risk.on_entry_filled(session_date)
        side = Side.LONG if order.is_buy else Side.SHORT
        self.open_trade = _OpenTrade(side, order.quantity, order, fill, order.signal_time, session_date,
                                     order.stop_loss, order.take_profit)
        exit_buy = not order.is_buy
        common = dict(is_buy=exit_buy, quantity=order.quantity, created_at=t, active_from=t,
                      signal_time=order.signal_time, session_date=session_date, tag=order.tag)
        if order.stop_loss is not None:
            self._new_order(role=OrderRole.STOP_LOSS, order_type=OrderType.STOP, price=order.stop_loss, **common)
        if order.take_profit is not None:
            self._new_order(role=OrderRole.TAKE_PROFIT, order_type=OrderType.LIMIT, price=order.take_profit, **common)

    def _process_exits(self, t: pd.Timestamp, bar: BarPrices, complete: bool,
                       session_date: pd.Timestamp | None) -> None:
        trade = self.open_trade
        assert trade is not None
        active = [o for o in self._pending(EXIT_ROLES) if o.active_from <= t]
        market = [o for o in active if o.role is OrderRole.EXIT]
        if market:
            self._close(market[0], self._simulate(market[0], bar), t, complete, "strategy_exit", False)
            return
        sl = next((o for o in active if o.role is OrderRole.STOP_LOSS), None)
        tp = next((o for o in active if o.role is OrderRole.TAKE_PROFIT), None)
        intrabar_entry_bar = trade.entry_fill.bar_time == t and not trade.entry_fill.at_open
        stop_reason = "stop_loss"

        # Daily-loss level as a protective stop; the closer of the two stops governs.
        if (session_date is not None and self.cfg.risk.tracks_unrealized_loss
                and self.cfg.risk.daily_loss_action == "flatten"):
            level = self.risk.daily_loss_trigger_price(
                session_date=session_date, is_long=self.position.quantity > 0,
                quantity=abs(self.position.quantity), avg_price=self.position.avg_price,
                paid_costs=trade.entry_fill.commission, point_value=self.spec.point_value,
                tick=self.spec.tick_size)
            long = self.position.quantity > 0
            if level is not None and (sl is None or (level > sl.price if long else level < sl.price)):
                sl = self._new_order(register=False, role=OrderRole.EXIT, order_type=OrderType.STOP,
                                     is_buy=not long, quantity=abs(self.position.quantity), price=level,
                                     created_at=t, active_from=t, signal_time=None,
                                     session_date=trade.session_date, tag="daily_loss_limit")
                stop_reason = "daily_loss_limit"

        sl_sim = self._simulate(sl, bar) if sl else None
        if sl_sim is not None and intrabar_entry_bar and sl_sim.at_open:
            # The open happened BEFORE the entry; the stop can only have been hit after it.
            assert sl is not None and sl.price is not None
            sl_sim = SimFill(apply_slippage(sl.price, sl.is_buy, self.cfg.slippage.stop_ticks, self.spec.tick_size),
                             sl.price, False)
        tp_sim = None if (tp is None or intrabar_entry_bar) else self._simulate(tp, bar)

        if sl_sim and stop_reason == "daily_loss_limit":
            assert sl is not None
            self.orders.append(sl)  # register the synthetic exit only when it fills
            if session_date is not None:
                self.risk.mark_daily_loss_breached(session_date)
        if sl_sim and tp_sim:
            if sl_sim.at_open:
                self._close(sl, sl_sim, t, complete, stop_reason, False)
            elif tp_sim.at_open:
                self._close(tp, tp_sim, t, complete, "take_profit", False)
            else:  # both inside the bar, order unknown -> worst case
                self._close(sl, sl_sim, t, complete, stop_reason, True)
        elif sl_sim:
            self._close(sl, sl_sim, t, complete, stop_reason, intrabar_entry_bar)
        elif tp_sim:
            self._close(tp, tp_sim, t, complete, "take_profit", False)

    def _force_exit(self, t: pd.Timestamp, bar: BarPrices, complete: bool, reason: str) -> None:
        assert self.open_trade is not None
        for o in self._pending(EXIT_ROLES):
            self._cancel(o, reason)
        order = self._new_order(role=OrderRole.EXIT, order_type=OrderType.MARKET,
                                is_buy=self.position.quantity < 0, quantity=abs(self.position.quantity),
                                price=None, created_at=t, active_from=t, signal_time=None,
                                session_date=self.open_trade.session_date, tag=reason)
        self._close(order, self._simulate(order, bar), t, complete, reason, False)

    def _close(self, order: Order, sim: SimFill | None, t: pd.Timestamp, complete: bool,
               reason: str, ambiguous: bool) -> None:
        trade = self.open_trade
        assert trade is not None and sim is not None
        fill = self._record_fill(order, sim, t, complete)
        gross = self.position.apply_fill(order.is_buy, order.quantity, sim.price)
        self.realized_net += gross
        for o in self._pending(EXIT_ROLES):
            self._cancel(o, "oco_cancelled")
        entry = trade.entry_fill
        commission = entry.commission + fill.commission
        slippage_cost = entry.slippage_cost + fill.slippage_cost
        net = gross - commission  # `gross` from fill prices already includes slippage
        gross_before_costs = ((fill.base_price - entry.base_price) * trade.side.sign
                              * trade.quantity * self.spec.point_value)
        assert abs(gross_before_costs - slippage_cost - commission - net) < 1e-6
        initial_risk = (abs(entry.price - trade.stop_loss) * trade.quantity * self.spec.point_value
                        if trade.stop_loss is not None else np.nan)
        exit_session = self._ex_session(t)
        self.trades.append({
            "trade_id": len(self.trades) + 1,
            "side": trade.side.value,
            "quantity": trade.quantity,
            "signal_time": trade.signal_time,
            "entry_order_type": trade.entry_order.order_type.value,
            "entry_time": entry.bar_time,
            "entry_at_open": entry.at_open,
            "entry_price": entry.price,
            "exit_time": t,
            "exit_at_open": sim.at_open,
            "exit_price": sim.price,
            "exit_reason": reason,
            "stop_loss": trade.stop_loss,
            "take_profit": trade.take_profit,
            "gross_pnl": gross_before_costs,
            "commission": commission,
            "slippage_cost": slippage_cost,
            "total_costs": commission + slippage_cost,
            "net_pnl": net,
            "initial_risk": initial_risk,
            "r_multiple": net / initial_risk if initial_risk and not np.isnan(initial_risk) else np.nan,
            "ambiguous": ambiguous,
            "data_incomplete": (not entry.bar_complete) or (not complete),
            "session_date": trade.session_date,
            "exit_session_date": exit_session if exit_session is not None else trade.session_date,
        })
        self.risk.on_trade_closed(net, self.trades[-1]["exit_session_date"])
        self.open_trade = None

    def _ex_session(self, t: pd.Timestamp) -> pd.Timestamp | None:
        sd = self.execution.at[t, "session_date"] if t in self.execution.index else None
        return pd.Timestamp(sd) if sd is not None and not pd.isna(sd) else None

    # ------------------------------------------------------------------ end of data
    def _finish(self) -> None:
        last_t = self.execution.index[-1]
        if self.open_trade is not None:
            close = float(self.execution["close"].iloc[-1])
            bar = BarPrices(close, close, close, close)
            complete = bool(self.execution["is_complete"].iloc[-1])
            self.warnings.append(f"{last_t}: data ended with an open position; closed at the last close")
            self._force_exit(last_t, bar, complete, "end_of_data")
            unrealized = 0.0
            self.equity_rows[-1].update(equity=self.cfg.initial_capital + self.realized_net + unrealized,
                                        realized=self.realized_net, unrealized=unrealized, position=0)
        for o in self.orders:
            if o.is_active:
                self._cancel(o, "end_of_data")

    def _result(self) -> BacktestResult:
        trade_cols = ["trade_id", "side", "quantity", "signal_time", "entry_order_type", "entry_time",
                      "entry_at_open", "entry_price", "exit_time", "exit_at_open", "exit_price", "exit_reason",
                      "stop_loss", "take_profit", "gross_pnl", "commission", "slippage_cost", "total_costs", "net_pnl",
                      "initial_risk", "r_multiple", "ambiguous", "data_incomplete", "session_date",
                      "exit_session_date"]
        trades = pd.DataFrame(self.trades, columns=trade_cols)
        orders = pd.DataFrame([vars(o) | {"role": o.role.value, "order_type": o.order_type.value,
                                          "status": o.status.value} for o in self.orders])
        fills = pd.DataFrame([vars(f) | {"role": f.role.value} for f in self.fills])
        equity = pd.DataFrame(self.equity_rows).set_index("time") if self.equity_rows else pd.DataFrame()
        rejections = pd.DataFrame(self.rejections, columns=["time", "stage", "reason", "detail", "order_id"])
        adjustments = pd.DataFrame(self.adjustments, columns=["time", "order_id", "reason", "from_quantity",
                                                              "to_quantity", "risk_per_contract", "detail"])
        metrics, daily = compute_metrics(trades, equity, self.cfg.initial_capital)
        return BacktestResult(trades, orders, fills, equity, rejections, daily, metrics, self.warnings, adjustments)
