"""Risk engine: unit tests and engine integration."""

from datetime import time

import pandas as pd
import pytest

from src.backtest.orders import OrderRequest
from src.config import ConfigError
from src.risk.manager import RiskConfig, RiskManager, TradingWindow
from tests.backtest_helpers import GLOBEX, MNQ, ScriptedStrategy, flat_bars, frame, make_config, run, ts

D1 = pd.Timestamp("2025-03-10")
D2 = pd.Timestamp("2025-03-11")
NOON = ts("12:00")


def rm(**kw) -> RiskManager:
    return RiskManager(RiskConfig(**kw), GLOBEX)


# ----------------------------------------------------------------- unit

def test_trading_windows():
    w = TradingWindow.parse("09:35-15:45")
    assert w.contains(time(9, 35)) and w.contains(time(15, 44)) and not w.contains(time(15, 45))
    night = TradingWindow.parse("18:00-02:00")
    assert night.contains(time(23, 0)) and night.contains(time(1, 59)) and not night.contains(time(2, 0))
    with pytest.raises(ConfigError):
        TradingWindow.parse("9h-10h")


def test_session_restrictions():
    r = rm(trading_windows=(TradingWindow.parse("09:35-15:45"),))
    assert r.check_entry(when=ts("09:35"), session_date=D1, quantity=1).approved
    assert r.check_entry(when=ts("09:34"), session_date=D1, quantity=1).reason == "outside_trading_window"
    assert r.check_entry(when=ts("15:45"), session_date=D1, quantity=1).reason == "outside_trading_window"
    assert r.check_entry(when=NOON, session_date=None, quantity=1).reason == "outside_session"


def test_max_position_size():
    r = rm(max_position_size=2)
    assert r.check_entry(when=NOON, session_date=D1, quantity=2).approved
    assert r.check_entry(when=NOON, session_date=D1, quantity=3).reason == "max_position_size"
    assert r.check_entry(when=NOON, session_date=D1, quantity=0).reason == "invalid_quantity"


def test_daily_loss_limit_resets_next_day():
    r = rm(max_daily_loss=100)
    r.on_trade_closed(-60, D1)
    assert r.check_entry(when=NOON, session_date=D1, quantity=1).approved
    r.on_trade_closed(-40, D1)  # exactly -100 -> limit reached
    assert r.check_entry(when=NOON, session_date=D1, quantity=1).reason == "max_daily_loss"
    assert r.check_entry(when=NOON, session_date=D2, quantity=1).approved


def test_daily_profit_target():
    r = rm(daily_profit_target=150)
    r.on_trade_closed(100, D1)
    assert r.check_entry(when=NOON, session_date=D1, quantity=1).approved
    r.on_trade_closed(50, D1)
    assert r.check_entry(when=NOON, session_date=D1, quantity=1).reason == "daily_profit_target"
    assert r.check_entry(when=NOON, session_date=D2, quantity=1).approved


def test_max_daily_trades():
    r = rm(max_daily_trades=2)
    r.on_entry_filled(D1)
    r.on_entry_filled(D1)
    assert r.check_entry(when=NOON, session_date=D1, quantity=1).reason == "max_daily_trades"
    assert r.check_entry(when=NOON, session_date=D2, quantity=1).approved


def test_consecutive_losses_win_resets_and_daily_reset():
    r = rm(max_consecutive_losses=2, consecutive_losses_reset_daily=True)
    r.on_trade_closed(-10, D1)
    r.on_trade_closed(5, D1)  # a win resets the streak
    r.on_trade_closed(-10, D1)
    assert r.check_entry(when=NOON, session_date=D1, quantity=1).approved
    r.on_trade_closed(-10, D1)
    assert r.check_entry(when=NOON, session_date=D1, quantity=1).reason == "max_consecutive_losses"
    assert r.check_entry(when=NOON, session_date=D2, quantity=1).approved  # new day
    carry = rm(max_consecutive_losses=2, consecutive_losses_reset_daily=False)
    carry.on_trade_closed(-10, D1)
    carry.on_trade_closed(-10, D1)
    assert carry.check_entry(when=NOON, session_date=D2, quantity=1).reason == "max_consecutive_losses"


def test_emergency_stop():
    assert rm(emergency_stop=True).check_entry(when=NOON, session_date=D1, quantity=1).reason == "emergency_stop"
    r = rm()
    r.activate_emergency_stop("manual")
    assert r.emergency_active and r.check_entry(when=NOON, session_date=D1, quantity=1).reason == "emergency_stop"


def test_disabled_limits_allow_everything():
    r = rm()
    for _ in range(50):
        r.on_trade_closed(-1000, D1)
        r.on_entry_filled(D1)
    assert r.check_entry(when=ts("03:00"), session_date=D1, quantity=99).approved


@pytest.mark.parametrize("raw", [{"max_daily_loss": -1}, {"max_daily_trades": 1.5}, {"max_position_size": "2"}])
def test_risk_config_validation(raw):
    with pytest.raises(ConfigError):
        RiskConfig.from_dict(raw)


# ----------------------------------------------------------------- engine integration

def _losing_mnq_day(cfg, day="2025-03-10", extra=None):
    """Two short signals on MNQ; the first loses (stop), the second is the probe."""
    base = 20000
    rows = [("10:00", base, base + 0.5, base - 0.5, base), ("10:01", base, base + 5, base - 5, base + 2),
            ("10:02", base + 3, base + 12, base + 1, base + 11), ("10:03", base + 11, base + 12, base + 10, base + 11),
            ("10:04", base + 11, base + 12, base + 10, base + 11)]
    script = {"10:00": [OrderRequest.enter("short", stop_loss=base + 10)], "10:03": [OrderRequest.enter("short")]}
    return run(rows, script, contract=MNQ, config=cfg)[0]


def test_engine_daily_loss_blocks_next_entry():
    res = _losing_mnq_day(make_config(risk=RiskConfig(max_daily_loss=20)))
    assert len(res.trades) == 1 and res.trades.iloc[0].net_pnl == pytest.approx(-23.0)
    rej = res.rejections.iloc[0]
    assert (rej.reason, rej.stage, rej.time) == ("max_daily_loss", "signal", ts("10:04"))


def test_engine_daily_loss_not_reached():
    res = _losing_mnq_day(make_config(risk=RiskConfig(max_daily_loss=30)))
    assert len(res.trades) == 2


def test_engine_consecutive_losses_and_daily_trades():
    res = _losing_mnq_day(make_config(risk=RiskConfig(max_consecutive_losses=1)))
    assert list(res.rejections.reason) == ["max_consecutive_losses"]
    res = _losing_mnq_day(make_config(risk=RiskConfig(max_daily_trades=1)))
    assert list(res.rejections.reason) == ["max_daily_trades"]


def test_engine_daily_profit_target():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5003, 4998, 5002),
            ("10:02", 5002, 5011, 4999, 5010), ("10:03", 5010, 5011, 5009, 5010), ("10:04", 5010, 5011, 5009, 5010)]
    script = {"10:00": [OrderRequest.enter("long", stop_loss=4990, take_profit=5010)],
              "10:03": [OrderRequest.enter("long")]}
    res, _ = run(rows, script, config=make_config(risk=RiskConfig(daily_profit_target=40)))
    assert res.trades.iloc[0].net_pnl == pytest.approx(46.75)
    assert list(res.rejections.reason) == ["daily_profit_target"] and len(res.trades) == 1


def test_engine_max_position_size_rejects_before_execution():
    res, _ = run(frame(flat_bars("10:00", 3)), {"10:00": [OrderRequest.enter("long", quantity=3)]},
                 config=make_config(risk=RiskConfig(max_position_size=2)))
    assert res.orders.empty and res.fills.empty and list(res.rejections.reason) == ["max_position_size"]


def test_engine_window_checked_at_signal_and_fill_time():
    window = RiskConfig(trading_windows=(TradingWindow.parse("10:00-10:02"),))
    # Signal known at 10:01 (inside), but 10:01 bar incomplete -> fill attempt at 10:02 (outside).
    bars = frame(flat_bars("10:00", 4), complete=[True, False, True, True])
    res, _ = run(bars, {"10:00": [OrderRequest.enter("long")]}, config=make_config(risk=window))
    assert res.trades.empty
    rej = res.rejections[res.rejections.stage == "fill"].iloc[0]
    assert rej.reason == "outside_trading_window" and rej.time == ts("10:02")
    assert res.orders.iloc[0]["status"] == "rejected"
    # Signal known at 10:02 is outside the window at signal time already.
    res, _ = run(frame(flat_bars("10:00", 4)), {"10:01": [OrderRequest.enter("long")]}, config=make_config(risk=window))
    assert list(res.rejections.stage) == ["signal"]


def test_engine_emergency_stop_flattens_and_blocks():
    risk = RiskManager(RiskConfig(), GLOBEX)

    def decide(ctx):
        if ctx.bar.name == ts("10:00"):
            return [OrderRequest.enter("long")]
        if ctx.bar.name == ts("10:02"):
            risk.activate_emergency_stop("test")
            return None
        if ctx.bar.name == ts("10:04"):
            return [OrderRequest.enter("long")]
        return None

    res, _ = run(frame(flat_bars("10:00", 6)), strategy=ScriptedStrategy(decide=decide), risk_manager=risk)
    t = res.trades.iloc[0]
    # Activated during the 10:02 decision (at 10:03): flattened at the 10:03 open.
    assert t.exit_reason == "emergency_stop" and t.exit_time == ts("10:03") and t.exit_price == 4999.75
    assert len(res.trades) == 1 and list(res.rejections.reason) == ["emergency_stop"]
