"""H2 (incomplete bars / missing timestamps in market structure) and M1 (stable pullback id)."""

from dataclasses import replace

import pandas as pd
import pytest

from src.backtest.orders import OrderEvent, Side
from src.indicators.swings import confirmed_swings, market_structure
from src.strategy.vwap_pullback import Reason, VwapTrendPullbackStrategy
from src.strategy.vwap_pullback_config import load_strategy_config
from tests.strategy_fixtures import MES, NY, context_bars, ctx_for, long_bars, set_at

CFG = load_strategy_config()
STEP = pd.Timedelta(minutes=5)


def decide(bars, cfg=CFG, s=None):
    if s is None:
        s = VwapTrendPullbackStrategy(cfg, NY)
        s.on_start(MES)
    reqs = s.on_bar(ctx_for(bars, context_bars(), MES))
    return reqs, s.diagnostics_frame(), s


# ----------------------------------------------------------------- H2: swing validity

def _swings(bars, **kw):
    return confirmed_swings(bars["high"], bars["low"], 2, 2, **kw)


def test_incomplete_bar_in_pivot_window_rejects_pivot():
    bars = set_at(long_bars(), 15, "is_complete", False)  # right neighbour of the L2 pivot (bar 14)
    sw = _swings(bars, valid=bars["is_complete"], step=STEP)
    assert pd.isna(sw["swing_low"].iloc[16]) and bool(sw["rejected_low"].iloc[16])
    full = _swings(long_bars(), valid=long_bars()["is_complete"], step=STEP)
    assert full["swing_low"].iloc[16] == 5009.0  # unchanged when all bars are complete


def test_missing_timestamp_is_not_adjacent():
    bars = long_bars().drop(long_bars().index[13])  # 10:05 missing next to the L2 pivot
    sw = _swings(bars, valid=bars["is_complete"], step=STEP)
    assert 5009.0 not in sw["swing_low"].dropna().tolist()
    assert sw["rejected_low"].any()
    legacy = _swings(bars)  # without step, positions would be treated as adjacent
    assert 5009.0 in legacy["swing_low"].dropna().tolist()


def test_gap_outside_pivot_windows_keeps_valid_pivots():
    bars = long_bars().drop(long_bars().index[0])
    ms = market_structure(bars["high"], bars["low"], 2, 2, valid=bars["is_complete"], step=STEP).iloc[-1]
    assert (ms.last_low, ms.prev_low, ms.structure, ms.rejected_pivots) == (5009, 5001, "bullish", 0)


@pytest.mark.parametrize("pos", [12, 13, 14, 15, 16])
def test_strategy_rejects_structure_built_on_incomplete_bars(pos):
    reqs, d, _ = decide(set_at(long_bars(), pos, "is_complete", False))
    row = d.iloc[0]
    assert reqs == [] and row.rejection_reason == Reason.STRUCTURE_DATA_INVALID
    assert row.structure_data.startswith("rejected_pivots=")


def test_strategy_rejects_structure_across_missing_bar():
    reqs, d, _ = decide(long_bars().drop(long_bars().index[13]))
    assert reqs == [] and d.iloc[0].rejection_reason == Reason.STRUCTURE_DATA_INVALID


def test_stop_never_derived_from_incomplete_candle():
    cfg = replace(CFG, trend=replace(CFG.trend, require_structure=False))
    _, d, _ = decide(set_at(long_bars(), 14, "is_complete", False), cfg)
    row = d.iloc[0]
    # L2 (bar 14) is invalid; the last VALID swing low is L1 5001 -> stop 5000.5, 30 pts > 24 -> too wide.
    assert row.stop_price == 5000.5 and Reason.STOP_TOO_WIDE in row.reasons.split(";")


def test_short_side_structure_integrity():
    from tests.strategy_fixtures import short_bars, short_context

    bars = set_at(short_bars(), 14, "is_complete", False)
    s = VwapTrendPullbackStrategy(CFG, NY)
    s.on_start(MES)
    assert s.on_bar(ctx_for(bars, short_context(), MES)) == []
    assert s.diagnostics_frame().set_index("direction").loc["short", "rejection_reason"] == \
        Reason.STRUCTURE_DATA_INVALID


# ----------------------------------------------------------------- M1: stable pullback id

NO_PIVOT = replace(CFG, enable_short=False, trend=replace(CFG.trend, require_structure=False),
                   stop=replace(CFG.stop, method="atr"), structure=replace(CFG.structure, lookback_bars=5),
                   filters=replace(CFG.filters, cooldown_minutes=0))


def _next_bar(bars, **values):
    nxt = bars.iloc[[-1]].copy()
    nxt.index = nxt.index + STEP
    nxt["available_at"] = nxt["available_at"] + STEP
    for k, v in values.items():
        nxt[k] = v
    return pd.concat([bars, nxt])


def test_fallback_id_stable_across_consecutive_bars_atr_mode():
    bars = long_bars()
    bars2 = _next_bar(bars, open=5030.6, close=5031.0, high=5031.5, low=5030.0, rsi_14=57.0)
    s = VwapTrendPullbackStrategy(NO_PIVOT, NY)
    s.on_start(MES)
    first = s.on_bar(ctx_for(bars, context_bars(), MES))
    second = s.on_bar(ctx_for(bars2, context_bars(), MES))
    d = s.diagnostics_frame()
    assert len(first) == 1 and second == []
    assert d.pullback_id.iloc[0] == d.pullback_id.iloc[1] == "2025-03-10 14:30:00+00:00"
    assert d.rejection_reason.iloc[1] == Reason.SAME_PULLBACK


def test_fallback_id_changes_only_with_a_new_extreme():
    bars = long_bars()
    s = VwapTrendPullbackStrategy(NO_PIVOT, NY)
    s.on_start(MES)
    s.on_bar(ctx_for(bars, context_bars(), MES))
    # New session high 5040, then a fresh pullback bar touching EMA9 and a bullish confirmation.
    b = _next_bar(bars, open=5031, high=5040, low=5030.5, close=5039, ema_9=5031, ema_21=5025, vwap=5027)
    b = _next_bar(b, open=5038, high=5038.5, low=5032.5, close=5033, ema_9=5033, ema_21=5026, vwap=5028,
                  rsi_14=48.0)
    b = _next_bar(b, open=5033, high=5036.5, low=5032.75, close=5036, ema_9=5033.5, ema_21=5026.5, vwap=5028.5,
                  rsi_14=56.0)
    s.on_bar(ctx_for(b, context_bars(), MES))
    d = s.diagnostics_frame()
    assert d.pullback_id.iloc[-1] == str(b.index[-3])  # the new high's bar
    assert d.pullback_id.iloc[-1] != d.pullback_id.iloc[0]
    assert Reason.SAME_PULLBACK not in d.reasons.iloc[-1].split(";")


def test_fallback_id_equals_later_confirmed_pivot_id():
    """Entered via the fallback id; once the extreme bar is confirmed as a pivot the id is the same."""
    s = VwapTrendPullbackStrategy(replace(NO_PIVOT, structure=replace(CFG.structure, lookback_bars=60)), NY)
    s.on_start(MES)
    bars = long_bars().iloc[:20]  # bar 18 (14:30 UTC) is the session high, not yet confirmed
    no_pivot = pd.Series({"last_high_time": pd.NaT, "last_low_time": pd.NaT})
    ms_id = s._pullback_id(ctx_for(bars, context_bars(), MES), Side.LONG, no_pivot, bars.index[-1])
    _, d, _ = decide(long_bars(), s=s)  # bar 18 now confirmed as the swing high
    assert ms_id == d.pullback_id.iloc[-1] == "2025-03-10 14:30:00+00:00"


def test_filled_pullback_stays_consumed_after_the_trade():
    s = VwapTrendPullbackStrategy(CFG, NY)
    s.on_start(MES)
    req = s.on_bar(ctx_for(long_bars(), context_bars(), MES))[0]
    t = pd.Timestamp("2025-03-10 14:55", tz="UTC")
    s.on_order_event(OrderEvent(req.client_id, 1, "accepted", t, "signal"))
    s.on_order_event(OrderEvent(req.client_id, 1, "filled", t, "fill"))
    assert s.on_bar(ctx_for(long_bars(), context_bars(), MES)) == []  # flat again, same pullback
    assert s.diagnostics_frame().rejection_reason.iloc[-2] == Reason.SAME_PULLBACK
