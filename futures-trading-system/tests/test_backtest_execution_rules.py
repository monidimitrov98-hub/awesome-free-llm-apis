"""Pure fill-rule tests: tick rounding, slippage, stop/limit triggering."""

import pytest

from src.backtest.execution import (
    BarPrices,
    apply_slippage,
    conservative_order_price,
    limit_triggered,
    round_to_tick,
    simulate_fill,
)
from src.backtest.orders import OrderType

BAR = BarPrices(open=100.0, high=102.0, low=98.0, close=101.0)


@pytest.mark.parametrize(
    "price,mode,expected",
    [(100.1, "nearest", 100.0), (100.13, "nearest", 100.25), (100.1, "up", 100.25), (100.24, "down", 100.0),
     (100.25, "up", 100.25), (100.25, "down", 100.25), (5000.375, "nearest", 5000.5)],
)
def test_round_to_tick(price, mode, expected):
    assert round_to_tick(price, 0.25, mode) == expected


def test_conservative_rounding_makes_orders_harder_to_fill():
    assert conservative_order_price(100.1, 0.25, OrderType.STOP, is_buy=True) == 100.25  # buy stop up
    assert conservative_order_price(100.1, 0.25, OrderType.STOP, is_buy=False) == 100.0  # sell stop down
    assert conservative_order_price(100.1, 0.25, OrderType.LIMIT, is_buy=True) == 100.0  # buy limit down
    assert conservative_order_price(100.1, 0.25, OrderType.LIMIT, is_buy=False) == 100.25  # sell limit up
    with pytest.raises(ValueError):
        conservative_order_price(100.0, 0.25, OrderType.MARKET, True)
    with pytest.raises(ValueError):
        round_to_tick(1.0, 0.0)


def test_slippage_is_adverse_and_on_tick():
    assert apply_slippage(100.0, True, 2, 0.25) == 100.5
    assert apply_slippage(100.0, False, 2, 0.25) == 99.5
    assert apply_slippage(100.0, True, 0, 0.25) == 100.0


def test_market_fills_at_open_plus_slippage():
    f = simulate_fill(BAR, OrderType.MARKET, True, None, tick=0.25, slippage_ticks=1)
    assert (f.price, f.base_price, f.at_open) == (100.25, 100.0, True)
    f = simulate_fill(BAR, OrderType.MARKET, False, None, tick=0.25, slippage_ticks=1)
    assert f.price == 99.75


def test_stop_orders():
    # intrabar trigger: fill at stop + slippage
    f = simulate_fill(BAR, OrderType.STOP, True, 101.0, tick=0.25, slippage_ticks=1)
    assert (f.price, f.at_open) == (101.25, False)
    # gap through the stop: fill at the open (worse than the stop)
    gap = BarPrices(103.0, 104.0, 102.5, 103.5)
    f = simulate_fill(gap, OrderType.STOP, True, 101.0, tick=0.25, slippage_ticks=1)
    assert (f.price, f.at_open) == (103.25, True)
    f = simulate_fill(BAR, OrderType.STOP, False, 98.5, tick=0.25, slippage_ticks=1)
    assert f.price == 98.25
    assert simulate_fill(BAR, OrderType.STOP, True, 102.25, tick=0.25, slippage_ticks=1) is None


def test_limit_orders_trade_through():
    # buy limit at the exact low: not filled when trade-through is required
    assert simulate_fill(BAR, OrderType.LIMIT, True, 98.0, tick=0.25, slippage_ticks=0) is None
    f = simulate_fill(BAR, OrderType.LIMIT, True, 98.0, tick=0.25, slippage_ticks=0, trade_through=False)
    assert f.price == 98.0
    f = simulate_fill(BAR, OrderType.LIMIT, True, 98.25, tick=0.25, slippage_ticks=0)
    assert (f.price, f.at_open) == (98.25, False)
    # opening through the limit: filled at the (better) open
    f = simulate_fill(BAR, OrderType.LIMIT, True, 100.5, tick=0.25, slippage_ticks=0)
    assert (f.price, f.at_open) == (100.0, True)
    f = simulate_fill(BAR, OrderType.LIMIT, False, 101.75, tick=0.25, slippage_ticks=0)
    assert f.price == 101.75
    assert limit_triggered(BAR, False, 102.0, True) == (False, False)
