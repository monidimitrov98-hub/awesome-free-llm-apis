"""Performance metrics with hand-calculated values."""

import math

import pandas as pd
import pytest

from src.backtest.metrics import compute_metrics, max_drawdown
from src.backtest.orders import OrderRequest
from tests.backtest_helpers import make_config, run


def _trades():
    d1, d2 = pd.Timestamp("2025-03-10"), pd.Timestamp("2025-03-11")
    return pd.DataFrame({
        "side": ["long", "short", "long", "short"],
        "net_pnl": [100.0, -50.0, 30.0, -20.0],
        "gross_pnl": [103.0, -47.0, 33.0, -17.0],
        "commission": [2.0, 2.0, 2.0, 2.0],
        "slippage_cost": [1.25, 1.25, 1.25, 1.25],
        "r_multiple": [2.0, -1.0, 0.6, float("nan")],
        "ambiguous": [False, True, False, False],
        "exit_session_date": [d1, d1, d2, d2],
    })


def test_hand_calculated_metrics():
    eq = pd.DataFrame({"equity": [10100.0, 10050.0, 10080.0, 10060.0]})
    m, daily = compute_metrics(_trades(), eq, 10_000.0)
    assert m["total_trades"] == 4
    assert m["net_profit"] == pytest.approx(60.0)
    assert m["gross_profit"] == pytest.approx(130.0) and m["gross_loss"] == pytest.approx(-70.0)
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["profit_factor"] == pytest.approx(130 / 70)
    assert m["average_win"] == pytest.approx(65.0) and m["average_loss"] == pytest.approx(-35.0)
    assert m["expectancy"] == pytest.approx(15.0)
    assert m["average_r_multiple"] == pytest.approx((2.0 - 1.0 + 0.6) / 3)  # NaN R excluded
    assert m["max_drawdown"] == pytest.approx(50.0)  # 10100 -> 10050
    assert m["max_drawdown_pct"] == pytest.approx(50 / 10100)
    assert m["total_commission"] == 8.0 and m["total_slippage_cost"] == 5.0
    assert m["ambiguous_trades"] == 1
    assert m["long"] == {"trades": 2, "net_profit": 130.0, "win_rate": 1.0}
    assert m["short"] == {"trades": 2, "net_profit": -70.0, "win_rate": 0.0}
    assert daily.tolist() == [50.0, 10.0]
    assert m["final_equity"] == 10060.0


def test_edge_cases():
    empty = pd.DataFrame(columns=_trades().columns)
    m, daily = compute_metrics(empty, pd.DataFrame(), 10_000.0)
    assert m["total_trades"] == 0 and math.isnan(m["win_rate"]) and math.isnan(m["profit_factor"])
    assert m["max_drawdown"] == 0 and daily.empty
    winners = _trades().iloc[[0, 2]]
    assert compute_metrics(winners, pd.DataFrame(), 10_000.0)[0]["profit_factor"] == math.inf


def test_max_drawdown_includes_starting_capital():
    assert max_drawdown(pd.Series([10000.0, 9900.0, 10200.0, 10000.0])) == (200.0, pytest.approx(200 / 10200))
    m, _ = compute_metrics(_trades(), pd.DataFrame({"equity": [9950.0]}), 10_000.0)
    assert m["max_drawdown"] == pytest.approx(50.0)


def test_engine_metrics_include_costs():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5003, 4998, 5002),
            ("10:02", 5002, 5011, 4999, 5010), ("10:03", 5010, 5011, 5009, 5010)]
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long", stop_loss=4990, take_profit=5010)]},
                 config=make_config())
    m = res.metrics
    assert m["net_profit"] == pytest.approx(46.75)  # (5010 - 5000.25) * 5 - 2
    assert m["gross_pnl_before_costs"] == pytest.approx(50.0)  # 10 points * $5 before slippage
    assert m["total_commission"] == 2.0 and m["total_slippage_cost"] == pytest.approx(1.25)
    assert m["final_equity"] == pytest.approx(10_046.75)
    assert res.daily_pnl.tolist() == [pytest.approx(46.75)]
    # The equity curve is consistent with the trade log once flat.
    assert res.equity["equity"].iloc[-1] == pytest.approx(10_000 + res.trades.net_pnl.sum())
