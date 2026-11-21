"""M3: strategy diagnostics reconciled with engine outcomes; pullbacks consumed only on fills."""

import pandas as pd

from src.backtest.engine import BacktestEngine
from src.backtest.orders import OrderEvent
from src.risk.manager import RiskConfig, TradingWindow
from src.strategy.vwap_pullback import Reason, VwapTrendPullbackStrategy
from src.strategy.vwap_pullback_config import load_strategy_config
from tests.backtest_helpers import GLOBEX, frame, make_config
from tests.strategy_fixtures import MES, NY, context_bars, ctx_for, long_bars

CFG = load_strategy_config()
GOOD_EXEC = [("10:55", 5031, 5032, 5030, 5031), ("10:56", 5031, 5075, 5030, 5074)]


def engine_run(exec_rows, config=None):
    s = VwapTrendPullbackStrategy(CFG, NY)
    res = BacktestEngine(config or make_config(), MES, GLOBEX).run(
        s, long_bars(), frame(exec_rows), context_bars={"15min": context_bars()})
    return res, s


def test_filled_signal_linked_end_to_end():
    res, s = engine_run(GOOD_EXEC)
    sig = s.signals_frame().iloc[0]
    assert sig.status == "filled" and pd.notna(sig.accepted_at) and pd.notna(sig.filled_at)
    assert res.trades.iloc[0].signal_id == sig.signal_id
    assert res.entry_checks.iloc[0].client_id == sig.signal_id
    row = s.diagnostics_frame().query("decision == 'signal'").iloc[0]
    assert (row.signal_id, row.order_status, row.order_id) == (sig.signal_id, "filled", sig.order_id)


def test_engine_rejection_is_reported_not_accepted():
    cfg = make_config(risk=RiskConfig(trading_windows=(TradingWindow.parse("11:00-15:00"),)))
    res, s = engine_run(GOOD_EXEC, cfg)
    sig = s.signals_frame().iloc[0]
    assert (sig.status, sig.stage, sig.reason) == ("rejected", "signal", "outside_trading_window")
    assert pd.isna(sig.accepted_at) and pd.isna(sig.filled_at)
    row = s.diagnostics_frame().query("decision == 'signal'").iloc[0]
    assert row.order_status == "rejected" and row.order_reason == "outside_trading_window"
    assert res.rejections.iloc[0].client_id == sig.signal_id


def test_cancelled_order_reported_and_pullback_released():
    res, s = engine_run([("11:00", 5031, 5032, 5030, 5031)])  # no 1m bar within 2 minutes
    sig = s.signals_frame().iloc[0]
    assert (sig.status, sig.reason) == ("cancelled", "fill_delay_exceeded")
    assert s._reserved_pullbacks == {} and s._consumed_pullbacks == set()


def test_rejected_order_does_not_consume_pullback():
    s = VwapTrendPullbackStrategy(CFG, NY)
    s.on_start(MES)
    req = s.on_bar(ctx_for(long_bars(), context_bars(), MES))[0]
    # While reserved (submitted, not yet resolved) the same pullback is blocked.
    assert s.on_bar(ctx_for(long_bars(), context_bars(), MES)) == []
    assert s.diagnostics_frame().rejection_reason.iloc[-2] == Reason.SAME_PULLBACK
    s.on_order_event(OrderEvent(req.client_id, None, "rejected", pd.Timestamp("2025-03-10 14:55", tz="UTC"),
                                "signal", "outside_trading_window"))
    again = s.on_bar(ctx_for(long_bars(), context_bars(), MES))
    assert len(again) == 1  # released: a later valid confirmation may use it


def test_rejected_fill_does_not_count_towards_entries_per_session():
    res, s = engine_run([("10:55", 5040, 5041, 5039, 5040), ("10:56", 5040, 5041, 5039, 5040)])
    assert res.trades.empty and s.signals_frame().iloc[0].status == "rejected"
    assert sum(s._entries_by_session.values()) == 0


def test_every_submitted_signal_has_a_final_state():
    for rows in (GOOD_EXEC, [("11:00", 5031, 5032, 5030, 5031)], [("10:55", 5040, 5041, 5039, 5040)]):
        _, s = engine_run(rows)
        assert set(s.signals_frame().status) <= {"filled", "rejected", "cancelled"}
