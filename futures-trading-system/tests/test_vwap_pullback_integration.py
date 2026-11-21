"""VWAP Trend Pullback through the Sprint 2 engine, the risk manager and the runner."""

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from src.backtest.config import SizingConfig, load_backtest_config
from src.backtest.engine import BacktestEngine
from src.backtest.runner import run_from_csv, summary_table, write_outputs
from src.risk.manager import RiskConfig, TradingWindow
from src.strategy.base import StrategyContext
from src.strategy.vwap_pullback import VwapTrendPullbackStrategy
from src.strategy.vwap_pullback_config import load_strategy_config
from tests.backtest_helpers import GLOBEX, frame, make_config
from tests.conftest import SAMPLES
from tests.strategy_fixtures import (
    MES,
    MNQ,
    NY,
    context_bars,
    long_bars,
    short_bars,
    short_context,
    set_at,
    ts,
)

CFG = load_strategy_config()


class Recorder(VwapTrendPullbackStrategy):
    """The real strategy, recording what the engine showed it."""

    def __init__(self, cfg=CFG):
        super().__init__(cfg, NY)
        self.seen: list[StrategyContext] = []

    def on_bar(self, ctx):
        self.seen.append(ctx)
        return super().on_bar(ctx)


def run(signal, context, execution, contract=MES, config=None, strategy=None):
    strat = strategy or Recorder()
    engine = BacktestEngine(config or make_config(), contract, GLOBEX)
    res = engine.run(strat, signal, execution, context_bars={"15min": context} if context is not None else None)
    return res, strat


def long_exec(second=("10:56", 5031, 5075, 5030, 5074)):
    return frame([("10:55", 5031, 5032, 5030, 5031), second, ("10:57", 5074, 5075, 5073, 5074)])


def mirror_rows(rows, c):
    return [(t, c - o, c - l, c - h, c - cl) for t, o, h, l, cl in rows]


# ----------------------------------------------------------------- 24 long and short through the engine

def test_long_take_profit_through_engine_hand_calculated():
    res, strat = run(long_bars(), context_bars(), long_exec())
    t = res.trades.iloc[0]
    # Signal on the 10:50 bar is known at 10:55 -> market fill at the 10:55 1m open + 1 tick.
    assert (t.side, t.entry_time, t.entry_price) == ("long", ts("10:55"), 5031.25)
    assert (t.stop_loss, t.take_profit) == (5008.5, 5074.5)
    assert (t.exit_reason, t.exit_time, t.exit_price) == ("take_profit", ts("10:56"), 5074.5)
    assert t.gross_pnl == pytest.approx((5074.5 - 5031) * 5)  # 217.50 before costs
    assert t.net_pnl == pytest.approx(217.5 - 1.25 - 2.0)  # 214.25
    assert t.r_multiple == pytest.approx(214.25 / ((5031.25 - 5008.5) * 5))
    assert res.metrics["net_profit"] == pytest.approx(214.25)
    d = strat.diagnostics_frame()
    assert d[d.decision == "signal"]["time"].tolist() == [ts("10:55")]


def test_long_stop_loss_through_engine():
    res, _ = run(long_bars(), context_bars(), long_exec(("10:56", 5031, 5031.5, 5008, 5009)))
    t = res.trades.iloc[0]
    assert (t.exit_reason, t.exit_price) == ("stop_loss", 5008.25)  # 5008.5 - 1 tick
    assert t.net_pnl == pytest.approx((5008.25 - 5031.25) * 5 - 2)


def test_short_mnq_take_profit_through_engine_hand_calculated():
    c = 40060.0
    rows = mirror_rows([("10:55", 5031, 5032, 5030, 5031), ("10:56", 5031, 5075, 5030, 5074),
                        ("10:57", 5074, 5075, 5073, 5074)], c)
    res, _ = run(short_bars(c), short_context(c), frame(rows), contract=MNQ)
    t = res.trades.iloc[0]
    assert (t.side, t.entry_price, t.take_profit, t.exit_price) == ("short", 35028.75, 34985.5, 34985.5)
    assert t.gross_pnl == pytest.approx((35029 - 34985.5) * 2)  # 87.00, MNQ $2 per point
    assert t.net_pnl == pytest.approx(87 - 0.5 - 2.0)  # 84.50


# ----------------------------------------------------------------- 25 risk manager integration

def test_risk_window_rejects_strategy_signal():
    cfg = make_config(risk=RiskConfig(trading_windows=(TradingWindow.parse("11:00-15:00"),)))
    res, strat = run(long_bars(), context_bars(), long_exec(), config=cfg)
    assert res.trades.empty
    rej = res.rejections.iloc[0]
    assert (rej.stage, rej.reason) == ("signal", "outside_trading_window")
    assert (strat.diagnostics_frame().decision == "signal").sum() == 1  # strategy said yes, risk said no


def test_risk_based_sizing_applied_by_engine():
    # Signal estimate: 22 pts x $5 + $2 + 2 ticks x $1.25 = 114.5 per contract -> floor(500 / 114.5) = 4.
    # At the fill (5031.25): 22.75 x 5 + 3.25 = 117 -> 4 x 117 = 468 <= 500: kept.
    cfg = make_config(sizing=SizingConfig("risk", risk_per_trade=500.0))
    res, _ = run(long_bars(), context_bars(), long_exec(), config=cfg)
    assert res.trades.iloc[0].quantity == 4 and res.adjustments.empty


def test_max_position_size_blocks_strategy_entry():
    cfg = make_config(sizing=SizingConfig("fixed", contracts=3), risk=RiskConfig(max_position_size=2))
    res, _ = run(long_bars(), context_bars(), long_exec(), config=cfg)
    assert res.trades.empty and list(res.rejections.reason) == ["max_position_size"]


def test_engine_rejects_take_profit_invalidated_by_gap():
    res, _ = run(long_bars(), context_bars(), frame([("10:55", 5080, 5081, 5079, 5080)]))
    assert res.trades.empty and list(res.rejections.reason) == ["invalid_bracket_at_fill"]


# ----------------------------------------------------------------- 26-27 session boundary, missing execution data

def test_session_end_cutoff_blocks_late_signal():
    cfg = replace(CFG, filters=replace(CFG.filters, session_windows=()))
    signal, ctx = long_bars("15:00"), context_bars("15:00")  # decision at 16:55 = flatten time
    res, strat = run(signal, ctx, frame([("16:55", 5031, 5032, 5030, 5031)]), strategy=Recorder(cfg))
    assert res.trades.empty and list(res.rejections.reason) == ["session_end_cutoff"]


def test_missing_execution_data_cancels_entry():
    res, _ = run(long_bars(), context_bars(), frame([("11:00", 5031, 5032, 5030, 5031)]))
    assert res.trades.empty
    assert res.orders.iloc[0]["reason"] == "fill_delay_exceeded"
    assert any("data gap" in w for w in res.warnings)


# ----------------------------------------------------------------- 13-15 multi-timeframe alignment, no look-ahead

def test_engine_never_shows_unfinished_context_candles():
    ctx = set_at(context_bars(), 7, "vwap", 9999.0)  # 10:45 15m candle, complete only at 11:00
    res, strat = run(long_bars(), ctx, long_exec())
    for seen in strat.seen:
        view = seen.context["15min"]
        assert view.empty or pd.DatetimeIndex(view["available_at"]).max() <= seen.now
    last = strat.seen[-1]
    assert last.now == ts("10:55") and last.context["15min"].index[-1] == ts("10:30")
    assert len(res.trades) == 1  # the unfinished bearish candle did not influence the decision


def test_context_candle_visible_exactly_at_completion():
    signal = long_bars("09:05")  # last 5m bar 10:55, decided at 11:00
    res, strat = run(signal, context_bars(), frame([("11:00", 5031, 5032, 5030, 5031)]))
    last = strat.seen[-1]
    assert last.now == ts("11:00") and last.context["15min"].index[-1] == ts("10:45")
    by_now = {c.now: c.context["15min"].index[-1] if len(c.context["15min"]) else None for c in strat.seen}
    # The 10:30 candle completes at 10:45 exactly: visible at now = 10:45, not at 10:40.
    assert by_now[ts("10:40")] == ts("10:15") and by_now[ts("10:45")] == ts("10:30")


def test_no_lookahead_future_bars_do_not_change_past_decisions():
    base = long_bars()
    future_idx = pd.date_range(base.index[-1] + pd.Timedelta(minutes=5), periods=12, freq="5min", name="timestamp")
    rng = np.random.default_rng(5)
    fut = pd.DataFrame({c: base[c].iloc[-1] for c in base.columns}, index=future_idx)
    fut["available_at"] = fut.index + pd.Timedelta(minutes=5)
    fut["session_date"] = base["session_date"].iloc[-1]
    calm = pd.concat([base, fut])
    wild = calm.copy()
    tail = wild.index[-12:]
    for col in ("open", "high", "low", "close", "ema_9", "ema_21", "vwap"):
        wild.loc[tail, col] = wild.loc[tail, col] + rng.normal(0, 30, 12)
    wild.loc[tail, "high"] = wild.loc[tail, ["open", "high", "low", "close"]].max(axis=1)
    wild.loc[tail, "low"] = wild.loc[tail, ["open", "high", "low", "close"]].min(axis=1)
    ex = long_exec()
    diags = []
    for sig in (base, calm, wild):
        res, strat = run(sig, context_bars(), ex)
        d = strat.diagnostics_frame()
        diags.append(d[d.time <= ts("10:55")].reset_index(drop=True))
        assert res.trades.iloc[0].entry_time == ts("10:55")
    pd.testing.assert_frame_equal(diags[0], diags[1])
    pd.testing.assert_frame_equal(diags[0], diags[2])


# ----------------------------------------------------------------- runner on historical CSV

@pytest.fixture(scope="module")
def sample_runs(config):
    return run_from_csv(SAMPLES / "SYNTHETIC_MNQ_1m.csv", config, load_backtest_config(), CFG, [1.5, 2.0])


def test_runner_outputs_and_determinism(sample_runs, config, tmp_path):
    md, runs = sample_runs
    assert [r.label for r in runs] == ["r_1.5", "r_2"]
    folder = write_outputs(runs[1], md, config, load_backtest_config(), tmp_path)
    for name in ("trades.csv", "equity.csv", "daily_pnl.csv", "metrics.json", "config_used.json",
                 "signal_diagnostics.csv", "engine_rejections.csv", "adjustments.csv", "warnings.txt"):
        assert (folder / name).is_file()
    used = json.loads((folder / "config_used.json").read_text())
    assert used["synthetic_data"] is True and used["strategy"]["target"]["r_multiple"] == 2.0
    assert used["contract"]["point_value"] == 2.0 and used["backtest"]["risk"]["max_daily_loss"] == 300.0
    # Determinism: an identical second run gives identical trades and diagnostics.
    _, again = run_from_csv(SAMPLES / "SYNTHETIC_MNQ_1m.csv", config, load_backtest_config(), CFG, [2.0])
    pd.testing.assert_frame_equal(runs[1].result.trades, again[0].result.trades)
    pd.testing.assert_frame_equal(runs[1].diagnostics, again[0].diagnostics)
    assert list(summary_table(runs).columns) == ["r_1.5", "r_2"]


def test_runner_trades_respect_all_rules(sample_runs):
    _, runs = sample_runs
    for run_ in runs:
        t, d = run_.result.trades, run_.diagnostics
        signals = d[d.decision == "signal"]
        assert len(t) <= len(signals)
        assert (t.entry_time >= t.signal_time + pd.Timedelta(minutes=5)).all()
        for row in signals.itertuples():  # every signal has a valid bracket around its reference close
            if row.direction == "long":
                assert row.stop_price < row.close < row.target_price
            else:
                assert row.target_price < row.close < row.stop_price
        # RVOL needs 5 earlier sessions: early bars are NaN and must never produce a signal.
        assert signals["rvol"].notna().all() and (signals["rvol"] >= CFG.rvol.min_rvol).all()
        assert (d.loc[d.rvol_status == "unavailable", "decision"] != "signal").all()
        assert (d.rvol_status == "unavailable").any()


def test_cli_runs(tmp_path):
    import run_backtest

    code = run_backtest.main(["--csv", str(SAMPLES / "SYNTHETIC_MES_1m.csv"), "--output-dir", str(tmp_path)])
    assert code == 0 and (tmp_path / "r_2" / "trades.csv").is_file()
    assert run_backtest.main(["--csv", str(tmp_path / "missing.csv")]) == 1
