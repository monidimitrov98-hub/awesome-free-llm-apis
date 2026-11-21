"""Fill rules for OHLC bars (pure functions, independently tested).

Conventions
-----------
* All prices are rounded to the contract tick. Order trigger prices are rounded in
  the direction that makes the order HARDER to trigger/fill (conservative):
  buy stop up, sell stop down, buy limit down, sell limit up.
* Slippage is applied adversely: buys pay more, sells receive less.
* If the bar opens beyond a stop, the stop fills at the open (gap), not at the stop.
* A limit fills at the open if the bar opens through it, otherwise at the limit if
  price traded through it (or touched it, when trade-through is disabled).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.backtest.orders import OrderType

_EPS = 1e-9


def round_to_tick(price: float, tick: float, mode: str = "nearest") -> float:
    """Round ``price`` to a multiple of ``tick`` ("nearest", "up" or "down")."""
    if tick <= 0:
        raise ValueError("tick must be positive")
    ratio = price / tick
    if mode == "nearest":
        steps = math.floor(ratio + 0.5)
    elif mode == "up":
        steps = math.ceil(ratio - _EPS)
    elif mode == "down":
        steps = math.floor(ratio + _EPS)
    else:
        raise ValueError(f"unknown rounding mode {mode!r}")
    return round(steps * tick, 10)


def conservative_order_price(price: float, tick: float, order_type: OrderType, is_buy: bool) -> float:
    """Round an order's trigger price so the order is harder to trigger/fill."""
    if order_type is OrderType.STOP:
        return round_to_tick(price, tick, "up" if is_buy else "down")
    if order_type is OrderType.LIMIT:
        return round_to_tick(price, tick, "down" if is_buy else "up")
    raise ValueError("only stop and limit orders have a trigger price")


def apply_slippage(price: float, is_buy: bool, ticks: int, tick: float) -> float:
    """Move ``price`` adversely by ``ticks`` ticks."""
    return round(price + (ticks * tick if is_buy else -ticks * tick), 10)


@dataclass(frozen=True)
class BarPrices:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class SimFill:
    """Raw fill result before costs: ``price`` already includes slippage."""

    price: float
    base_price: float  # price before slippage
    at_open: bool


def stop_triggered(bar: BarPrices, is_buy: bool, stop: float) -> tuple[bool, bool]:
    """Return ``(triggered, at_open)`` for a stop order on ``bar``."""
    if is_buy:
        if bar.open >= stop:
            return True, True
        return bar.high >= stop, False
    if bar.open <= stop:
        return True, True
    return bar.low <= stop, False


def limit_triggered(bar: BarPrices, is_buy: bool, limit: float, trade_through: bool) -> tuple[bool, bool]:
    """Return ``(triggered, at_open)`` for a limit order on ``bar``."""
    if is_buy:
        if bar.open < limit or (not trade_through and bar.open == limit):
            return True, True
        return (bar.low < limit) if trade_through else (bar.low <= limit), False
    if bar.open > limit or (not trade_through and bar.open == limit):
        return True, True
    return (bar.high > limit) if trade_through else (bar.high >= limit), False


def simulate_fill(
    bar: BarPrices,
    order_type: OrderType,
    is_buy: bool,
    price: float | None,
    *,
    tick: float,
    slippage_ticks: int,
    trade_through: bool = True,
) -> SimFill | None:
    """Fill price of one order on one bar, or None if it does not fill.

    Market orders fill at the open. Stop orders fill at the stop (or the open on a
    gap). Limit orders fill at the limit (or the open when it opens through).
    """
    if order_type is OrderType.MARKET:
        base, at_open = bar.open, True
    elif order_type is OrderType.STOP:
        assert price is not None
        hit, at_open = stop_triggered(bar, is_buy, price)
        if not hit:
            return None
        base = bar.open if at_open else price
    elif order_type is OrderType.LIMIT:
        assert price is not None
        hit, at_open = limit_triggered(bar, is_buy, price, trade_through)
        if not hit:
            return None
        base = bar.open if at_open else price
    else:  # pragma: no cover
        raise ValueError(order_type)
    return SimFill(price=apply_slippage(base, is_buy, slippage_ticks, tick), base_price=base, at_open=at_open)
