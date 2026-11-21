"""End-to-end: Sprint 1 pipeline output -> backtest (5m signals, 1m execution) on SYNTHETIC data.

The decision rule below exists only to exercise the engine. It is not a trading
strategy and its results say nothing about profitability.
"""

import pandas as pd
import pytest

from src.backtest.config import load_backtest_config
from src.backtest.engine import BacktestEngine
from src.backtest.orders import OrderRequest
from src.pipeline import build_market_data
from tests.backtest_helpers import ScriptedStrategy
from tests.conftest import SAMPLES


def _toy(ctx):
    h = ctx.history
    if len(h) < 22 or not ctx.position.is_flat or ctx.has_pending_entry:
        return None
    prev, cur = h.iloc[-2], h.iloc[-1]
    atr = cur["atr_14"]
    if prev["ema_9"] <= prev["ema_21"] and cur["ema_9"] > cur["ema_21"]:
        return [OrderRequest.enter("long", stop_loss=cur.close - 2 * atr, take_profit=cur.close + 3 * atr)]
    if prev["ema_9"] >= prev["ema_21"] and cur["ema_9"] < cur["ema_21"]:
        return [OrderRequest.enter("short", stop_loss=cur.close + 2 * atr, take_profit=cur.close - 3 * atr)]
    return None


@pytest.fixture(scope="module")
def result(config):
    md = build_market_data(SAMPLES / "SYNTHETIC_MNQ_1m.csv", config)
    engine = BacktestEngine(load_backtest_config(), md.contract.spec, config.session)
    res = engine.run(ScriptedStrategy(decide=_toy), md.frames["5min"], md.frames["1min"])
    return res, md


def test_runs_and_is_internally_consistent(result):
    res, md = result
    t = res.trades
    assert len(t) > 0
    # Accounting: equity once flat == capital + sum of net trade PnL; net = gross - commission.
    assert res.equity["equity"].iloc[-1] == pytest.approx(10_000 + t.net_pnl.sum())
    assert (t.net_pnl - (t.gross_pnl - t.commission)).abs().max() < 1e-9
    assert res.metrics["net_profit"] == pytest.approx(t.net_pnl.sum())
    # Timing: 5m signals are known at bar open + 5 min and fill no earlier.
    assert (t.entry_time >= t.signal_time + pd.Timedelta(minutes=5)).all()
    assert (t.exit_time >= t.entry_time).all()
    # Prices on the MNQ tick grid.
    for col in ("entry_price", "exit_price"):
        assert ((t[col] / 0.25).round(9) % 1 == 0).all()


def test_risk_limits_respected(result):
    res, md = result
    t = res.trades
    local = pd.DatetimeIndex(t.entry_time).tz_convert("America/New_York")
    minutes = local.hour * 60 + local.minute
    assert ((minutes >= 9 * 60 + 35) & (minutes < 15 * 60 + 45)).all()  # configured window
    assert t.groupby("session_date").size().max() <= 5  # max_daily_trades
    assert (t.quantity <= 2).all()
    assert set(t.exit_reason) <= {"stop_loss", "take_profit", "session_end", "strategy_exit", "end_of_data"}
