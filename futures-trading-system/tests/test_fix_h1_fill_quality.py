"""H1: stop width and reward/risk re-validated at the ACTUAL fill, for every sizing method.

Fixture setup (strategy_fixtures.py): planned from close 5030.5 with stop 5008.5 and target
5074.5 -> planned stop distance 22.00, planned R 2.00. Limits sent to the engine:
max stop distance 3 x ATR(8) = 24, min stop distance max(0.5 x 8, 4 ticks) = 4, min R 0.9 x 2 = 1.8.
Market fill = 1m open + 1 tick (long) / - 1 tick (short).
"""

import pytest

from src.backtest.config import SizingConfig
from src.backtest.engine import BacktestEngine
from src.strategy.vwap_pullback import VwapTrendPullbackStrategy
from src.strategy.vwap_pullback_config import load_strategy_config
from tests.backtest_helpers import GLOBEX, frame, make_config
from tests.strategy_fixtures import MES, NY, context_bars, ctx_for, long_bars, short_bars, short_context

CFG = load_strategy_config()


def run_long(open_, config=None, c=None):
    if c is None:
        signal, ctx = long_bars(), context_bars()
        rows = [("10:55", open_, open_ + 1, open_ - 1, open_), ("10:56", open_, open_ + 1, open_ - 1, open_)]
    else:  # mirrored short
        signal, ctx = short_bars(c), short_context(c)
        o = c - open_
        rows = [("10:55", o, o + 1, o - 1, o), ("10:56", o, o + 1, o - 1, o)]
    strat = VwapTrendPullbackStrategy(CFG, NY)
    res = BacktestEngine(config or make_config(), MES, GLOBEX).run(strat, signal, frame(rows),
                                                                   context_bars={"15min": ctx})
    return res, strat


def test_strategy_sends_fill_limits():
    s = VwapTrendPullbackStrategy(CFG, NY)
    s.on_start(MES)
    req = s.on_bar(ctx_for(long_bars(), context_bars()))[0]
    assert (req.max_stop_distance, req.min_stop_distance, req.min_reward_risk) == (24.0, 4.0, 1.8)
    assert req.client_id == "vwap_trend_pullback:long:2025-03-10T14:50:00+00:00"


@pytest.mark.parametrize("c", [None, 10060.0])  # long and mirrored short
@pytest.mark.parametrize("open_,reason,actual_dist,actual_rr", [
    (5040, "stop_too_wide_at_fill", 31.75, (5074.5 - 5040.25) / 31.75),        # adverse gap, 3.97 ATR
    (5032, "reward_risk_below_min_at_fill", 23.75, (5074.5 - 5032.25) / 23.75),  # 1.78R < 1.8R
    (5012, "stop_too_tight_at_fill", 3.75, (5074.5 - 5012.25) / 3.75),          # favourable gap, < 4 pts
])
def test_bad_fill_rejected_with_fixed_sizing(open_, reason, actual_dist, actual_rr, c):
    res, strat = run_long(open_, c=c)  # make_config(): FIXED sizing, 1 contract
    assert res.trades.empty and res.fills.empty  # nothing opened, stop/target never moved
    rej = res.rejections.iloc[0]
    assert (rej.stage, rej.reason) == ("fill", reason)
    chk = res.entry_checks.iloc[0]
    assert chk.outcome == "rejected" and chk.reason == reason
    assert chk.planned_stop_distance == pytest.approx(22.0) and chk.planned_reward_risk == pytest.approx(2.0)
    assert chk.actual_stop_distance == pytest.approx(actual_dist)
    assert chk.actual_reward_risk == pytest.approx(actual_rr)
    d = strat.diagnostics_frame()
    row = d[d.decision == "signal"].iloc[0]
    assert (row.order_status, row.order_reason) == ("rejected", reason)


@pytest.mark.parametrize("c", [None, 10060.0])
def test_acceptable_fill_records_planned_and_actual(c):
    res, _ = run_long(5031, c=c)
    t = res.trades.iloc[0]
    assert t.planned_stop_distance == pytest.approx(22.0) and t.actual_stop_distance == pytest.approx(22.75)
    assert t.planned_reward_risk == pytest.approx(2.0)
    assert t.actual_reward_risk == pytest.approx(43.25 / 22.75)  # 1.90 >= 1.8
    # Actual dollar risk: 22.75 pts x $5 + $2 round-trip commission + 1 stop tick x $1.25.
    assert t.actual_risk_dollars == pytest.approx(117.0)
    assert res.entry_checks.iloc[0].outcome == "accepted"


def test_favourable_gap_within_limits_is_accepted():
    res, _ = run_long(5020)  # fill 5020.25: stop distance 11.75 >= 4, R = 54.25 / 11.75 = 4.6
    assert len(res.trades) == 1 and res.trades.iloc[0].actual_reward_risk == pytest.approx(54.25 / 11.75)


def test_risk_sizing_does_not_replace_fill_quality_checks():
    cfg = make_config(sizing=SizingConfig("risk", risk_per_trade=10_000.0))  # huge budget
    res, _ = run_long(5040, config=cfg)
    assert res.trades.empty and list(res.rejections.reason) == ["stop_too_wide_at_fill"]


def test_disabling_fill_limits_is_explicit():
    from dataclasses import replace

    loose = replace(CFG, stop=replace(CFG.stop, max_distance_atr=0.0, min_distance_atr=0.0, min_distance_ticks=0),
                    target=replace(CFG.target, min_actual_r_fraction=0.0))
    strat = VwapTrendPullbackStrategy(loose, NY)
    rows = [("10:55", 5040, 5041, 5039, 5040), ("10:56", 5040, 5041, 5039, 5040)]
    res = BacktestEngine(make_config(), MES, GLOBEX).run(strat, long_bars(), frame(rows),
                                                         context_bars={"15min": context_bars()})
    assert len(res.trades) == 1  # only because every fill limit was switched off on purpose
