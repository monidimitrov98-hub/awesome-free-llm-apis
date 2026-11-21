"""Engine execution tests with hand-calculated PnL.

Defaults (see make_config): $1.00 commission per contract per side, 1 tick slippage
on market and stop fills, 0 on limit fills, no risk limits, signal frame = execution frame.
MES: tick 0.25 = $1.25, $5/point. MNQ: tick 0.25 = $0.50, $2/point.
"""

import math

import pandas as pd
import pytest

from src.backtest.orders import OrderRequest
from tests.backtest_helpers import MES, MNQ, frame, make_config, run, ts

LONG_BRACKET = OrderRequest.enter("long", stop_loss=4990, take_profit=5010)


def only_trade(result):
    assert len(result.trades) == 1, result.trades
    return result.trades.iloc[0]


# ----------------------------------------------------------------- long / short

def test_long_market_take_profit_hand_calculated():
    rows = [("10:00", 5000, 5000.5, 4999.5, 5000), ("10:01", 5000, 5003, 4998, 5002),
            ("10:02", 5002, 5011, 4999, 5010), ("10:03", 5010, 5010.5, 5009.5, 5010)]
    res, _ = run(rows, {"10:00": [LONG_BRACKET]})
    t = only_trade(res)
    # Signal on the 10:00 bar is known at 10:01 -> market fill at the 10:01 open + 1 tick.
    assert t.entry_time == ts("10:01") and t.entry_price == 5000.25 and t.entry_at_open
    assert t.exit_time == ts("10:02") and t.exit_price == 5010.0 and t.exit_reason == "take_profit"
    assert t.gross_pnl == pytest.approx((5010 - 5000.25) * 5)  # 48.75
    assert t.commission == 2.0
    assert t.net_pnl == pytest.approx(46.75)
    assert t.slippage_cost == pytest.approx(1.25)  # one tick on the entry
    assert t.initial_risk == pytest.approx((5000.25 - 4990) * 5)  # 51.25
    assert t.r_multiple == pytest.approx(46.75 / 51.25)
    assert not t.ambiguous
    # Equity: after 10:01 close = 10000 - 1 commission + (5002 - 5000.25) * 5
    assert res.equity.loc[ts("10:02"), "equity"] == pytest.approx(10_007.75)
    assert res.metrics["final_equity"] == pytest.approx(10_046.75)


@pytest.mark.parametrize("contract,base,expected_gross", [(MNQ, 20000, -21.0), (MES, 5000, -52.5)])
def test_short_stop_loss_contract_specific(contract, base, expected_gross):
    rows = [("10:00", base, base + 0.5, base - 0.5, base), ("10:01", base, base + 5, base - 5, base + 2),
            ("10:02", base + 3, base + 12, base + 1, base + 11)]
    req = OrderRequest.enter("short", stop_loss=base + 10, take_profit=base - 50)
    res, _ = run(rows, {"10:00": [req]}, contract=contract)
    t = only_trade(res)
    assert t.side == "short" and t.entry_price == base - 0.25  # sell at open - 1 tick
    assert t.exit_price == base + 10.25 and t.exit_reason == "stop_loss"  # buy stop + 1 tick
    # 10.5 points against the short: MNQ 10.5 * $2 = -21, MES 10.5 * $5 = -52.5
    assert t.gross_pnl == pytest.approx(expected_gross)
    assert t.net_pnl == pytest.approx(expected_gross - 2.0)
    assert t.r_multiple == pytest.approx((expected_gross - 2) / (10.25 * contract.point_value))


def test_zero_costs_gives_pure_price_pnl():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5003, 4998, 5002), ("10:02", 5002, 5011, 4999, 5010)]
    res, _ = run(rows, {"10:00": [LONG_BRACKET]}, config=make_config(commission=0, market_ticks=0, stop_ticks=0))
    assert only_trade(res).net_pnl == pytest.approx((5010 - 5000) * 5)


def test_slippage_and_commission_scale_with_quantity():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5003, 4998, 5002), ("10:02", 5002, 5011, 4999, 5010)]
    cfg = make_config(commission=2.5, market_ticks=2)
    req = OrderRequest.enter("long", stop_loss=4990, take_profit=5010, quantity=3)
    t = only_trade(run(rows, {"10:00": [req]}, config=cfg)[0])
    assert t.entry_price == 5000.5  # 2 ticks
    assert t.commission == pytest.approx(2.5 * 3 * 2)  # 15
    assert t.slippage_cost == pytest.approx(0.5 * 3 * 5)  # 7.5
    assert t.net_pnl == pytest.approx((5010 - 5000.5) * 3 * 5 - 15)  # 127.5


# ----------------------------------------------------------------- stops, targets, gaps, ambiguity

def _long_then(bar_1002):
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5003, 4998, 5002), bar_1002,
            ("10:03", 5000, 5000.5, 4999.5, 5000)]
    return run(rows, {"10:00": [LONG_BRACKET]})[0]


def test_stop_and_target_in_same_bar_is_ambiguous_worst_case():
    t = only_trade(_long_then(("10:02", 5002, 5012, 4988, 5005)))
    assert t.exit_reason == "stop_loss" and t.ambiguous
    assert t.exit_price == 4989.75  # stop 4990 - 1 tick, never the favorable target
    assert t.gross_pnl == pytest.approx((4989.75 - 5000.25) * 5)


def test_gap_through_stop_fills_at_open():
    t = only_trade(_long_then(("10:02", 4985, 5015, 4980, 5000)))
    assert t.exit_reason == "stop_loss" and t.exit_price == 4984.75 and t.exit_at_open and not t.ambiguous


def test_gap_through_target_fills_at_open():
    t = only_trade(_long_then(("10:02", 5015, 5016, 4980, 5000)))
    assert t.exit_reason == "take_profit" and t.exit_price == 5015.0 and t.exit_at_open and not t.ambiguous


def test_target_needs_trade_through():
    res = _long_then(("10:02", 5002, 5010, 4999, 5008))  # touches 5010 only
    assert only_trade(res).exit_reason == "end_of_data"


def test_market_entry_bar_can_exit_same_bar_with_ambiguity():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5012, 4988, 5001), ("10:02", 5001, 5002, 5000, 5001)]
    t = only_trade(run(rows, {"10:00": [LONG_BRACKET]})[0])
    assert t.entry_time == t.exit_time == ts("10:01")
    assert t.exit_reason == "stop_loss" and t.ambiguous


def test_stop_entry_intrabar_conservative_on_entry_bar():
    req = OrderRequest.enter("long", "stop", price=5005, stop_loss=4998, take_profit=5010)
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5012, 4995, 5006), ("10:02", 5006, 5007, 5005, 5006)]
    t = only_trade(run(rows, {"10:00": [req]})[0])
    assert t.entry_price == 5005.25 and not t.entry_at_open
    # Low 4995 may have come before or after the entry: assume the stop was hit, never credit the target.
    assert t.exit_reason == "stop_loss" and t.ambiguous and t.exit_price == 4997.75
    assert t.gross_pnl == pytest.approx((4997.75 - 5005.25) * 5)  # -37.5


def test_stop_entry_then_target_next_bar():
    req = OrderRequest.enter("long", "stop", price=5005, stop_loss=4998, take_profit=5010)
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5006, 4999, 5005), ("10:02", 5005, 5011, 5004, 5010)]
    t = only_trade(run(rows, {"10:00": [req]})[0])
    assert (t.entry_price, t.exit_price, t.exit_reason) == (5005.25, 5010.0, "take_profit")
    assert t.gross_pnl == pytest.approx(23.75)


def test_stop_entry_gap_fills_at_open():
    req = OrderRequest.enter("long", "stop", price=5005, stop_loss=4998)
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5007, 5008, 5006, 5007)]
    t = only_trade(run(rows, {"10:00": [req]})[0])
    assert t.entry_price == 5007.25 and t.entry_at_open


def test_limit_entry_requires_trade_through():
    req = OrderRequest.enter("long", "limit", price=4995, stop_loss=4990, take_profit=5005)
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 4999, 5000, 4995, 4996),  # touch only
            ("10:02", 4996, 4997, 4994.75, 4996), ("10:03", 4996, 5006, 4996, 5005)]
    t = only_trade(run(rows, {"10:00": [req]})[0])
    assert t.entry_time == ts("10:02") and t.entry_price == 4995.0 and t.slippage_cost == 0
    assert t.exit_reason == "take_profit" and t.gross_pnl == pytest.approx(50.0)


def test_unfilled_stop_entry_is_cancelled_not_filled():
    req = OrderRequest.enter("long", "stop", price=5100)
    res, _ = run([("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000)], {"10:00": [req]})
    assert res.trades.empty
    assert res.orders.iloc[0]["status"] == "cancelled" and res.orders.iloc[0]["reason"] == "end_of_data"


def test_bracket_prices_rounded_conservatively():
    req = OrderRequest.enter("long", stop_loss=4990.1, take_profit=5010.1)
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000)]
    res, _ = run(rows, {"10:00": [req]})
    t = only_trade(res)
    assert t.stop_loss == 4990.0 and t.take_profit == 5010.25


@pytest.mark.parametrize("req", [OrderRequest.enter("long", stop_loss=5001), OrderRequest.enter("short", take_profit=5001),
                                 OrderRequest.enter("long", "limit")])
def test_invalid_requests_rejected(req):
    res, _ = run([("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000)], {"10:00": [req]})
    assert res.trades.empty and res.orders.empty
    assert res.rejections.iloc[0]["reason"] in {"invalid_bracket", "missing_order_price"}


# ----------------------------------------------------------------- exits, session handling, end of data

def test_strategy_exit_fills_next_bar_open():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5003, 4998, 5002),
            ("10:02", 5002, 5004, 5001, 5003), ("10:03", 5004, 5005, 5003, 5004)]
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long")], "10:02": [OrderRequest.exit()]})
    t = only_trade(res)
    assert t.exit_time == ts("10:03") and t.exit_price == 5003.75 and t.exit_reason == "strategy_exit"
    assert math.isnan(t.r_multiple)  # no stop -> no R


def test_end_of_data_closes_at_last_close():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5003, 4998, 5002)]
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long")]})
    t = only_trade(res)
    assert t.exit_reason == "end_of_data" and t.exit_price == 5001.75
    assert any("data ended" in w for w in res.warnings)
    assert res.equity["equity"].iloc[-1] == pytest.approx(10_000 + res.trades.net_pnl.sum())


def test_session_end_flatten_and_cutoff():
    rows = [(f"16:{m}", 5000, 5001, 4999, 5000) for m in range(50, 59)]
    res, _ = run(rows, {"16:50": [OrderRequest.enter("long")]})
    t = only_trade(res)
    assert t.entry_time == ts("16:51") and t.exit_time == ts("16:55") and t.exit_reason == "session_end"
    assert t.exit_price == 4999.75  # market exit at the 16:55 open - 1 tick
    # A signal on the 16:54 bar is known at 16:55 = flatten time: no new entry.
    res, _ = run(rows, {"16:54": [OrderRequest.enter("short")]})
    assert res.trades.empty and list(res.rejections.reason) == ["session_end_cutoff"]
    # A signal known at 16:54 may still fill on the 16:54 bar, then is flattened at 16:55.
    res, _ = run(rows, {"16:53": [OrderRequest.enter("short")]})
    t = only_trade(res)
    assert (t.entry_time, t.exit_time) == (ts("16:54"), ts("16:55"))


def test_pending_entry_cancelled_at_session_end():
    rows = [(f"16:{m}", 5000, 5001, 4999, 5000) for m in range(50, 58)]
    res, _ = run(rows, {"16:51": [OrderRequest.enter("long", "stop", price=5100)]})
    assert res.orders.iloc[0]["reason"] == "session_end"


def test_session_gap_exit_when_data_missing():
    a = frame([("16:30", 5000, 5001, 4999, 5000), ("16:31", 5000, 5001, 4999, 5000)])
    b = frame([("18:00", 5010, 5011, 5009, 5010)])  # next Globex session (trade date 03-11)
    res, _ = run(pd.concat([a, b]), {"16:30": [OrderRequest.enter("long")]})
    t = only_trade(res)
    assert t.exit_reason == "session_gap" and t.exit_time == ts("18:00")
    assert any("still open in a new session" in w for w in res.warnings)
