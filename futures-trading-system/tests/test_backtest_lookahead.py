"""Anti-look-ahead, available_at enforcement, incomplete bars and duplicate signals."""

import numpy as np
import pandas as pd
import pytest

from src.backtest.orders import OrderRequest
from src.data.errors import DataError
from tests.backtest_helpers import ScriptedStrategy, flat_bars, frame, make_config, run, ts


def _random_rows(n: int, seed: int = 3, start: str = "09:00") -> list[tuple]:
    rng = np.random.default_rng(seed)
    price, rows = 5000.0, []
    t0 = pd.Timestamp(f"2025-03-10 {start}")
    for i in range(n):
        o = price
        c = o + rng.integers(-8, 9) * 0.25
        h = max(o, c) + rng.integers(0, 6) * 0.25
        l = min(o, c) - rng.integers(0, 6) * 0.25
        rows.append(((t0 + pd.Timedelta(minutes=i)).strftime("%H:%M"), o, h, l, c))
        price = c
    return rows


def momentum(ctx):
    """Toy decision rule used ONLY to exercise the engine (not a trading strategy)."""
    h = ctx.history
    if len(h) < 3:
        return None
    if ctx.position.is_flat and not ctx.has_pending_entry and h.close.iloc[-1] > h.close.iloc[-3]:
        c = ctx.bar.close
        return [OrderRequest.enter("long", stop_loss=c - 2, take_profit=c + 3)]
    if ctx.position.side is not None and h.close.iloc[-1] < h.close.iloc[-2]:
        return [OrderRequest.exit()]
    return None


# ----------------------------------------------------------------- available_at

def test_5m_signal_executes_on_1m_bar_at_available_at():
    sig = frame([("10:00", 5000, 5004, 4999, 5003), ("10:05", 5003, 5005, 5002, 5004)], minutes=5)
    ex = frame(flat_bars("10:00", 10, price=5000))
    res, strat = run(sig, {"10:00": [OrderRequest.enter("long")]}, execution=ex)
    t = res.trades.iloc[0]
    # The 10:00 5m bar is only known at 10:05: no 1m bar before 10:05 may fill the order.
    assert t.entry_time == ts("10:05") and t.entry_price == 5000.25
    first = strat.contexts[0]
    assert first.now == ts("10:05") and first.history.index[-1] == ts("10:00")
    assert len(first.history) == 1  # the 10:05 5m bar is not visible yet


def test_every_context_respects_available_at():
    sig = frame(_random_rows(120), minutes=1)
    res, strat = run(sig, strategy=ScriptedStrategy(decide=momentum))
    assert len(strat.contexts) == 120  # each bar decided exactly once
    for ctx in strat.contexts:
        assert pd.DatetimeIndex(ctx.history["available_at"]).max() <= ctx.now
        assert ctx.history.index[-1] == ctx.bar.name and ctx.now == ctx.bar["available_at"]


def test_signals_never_fill_before_next_bar():
    sig = frame(_random_rows(200), minutes=1)
    res, _ = run(sig, strategy=ScriptedStrategy(decide=momentum))
    assert len(res.trades) > 5
    assert (res.trades["entry_time"] >= res.trades["signal_time"] + pd.Timedelta(minutes=1)).all()
    filled = res.orders[res.orders.status == "filled"]
    assert (filled["filled_at"] >= filled["active_from"]).all()


def test_future_bars_do_not_change_past_decisions_or_trades():
    rows = _random_rows(200)
    cut = 120
    base_res, base = run(frame(rows), strategy=ScriptedStrategy(decide=momentum))
    shocked = [(r[0], *(np.array(r[1:]) + 50.0)) if i >= cut else r for i, r in enumerate(rows)]
    shock_res, shock = run(frame(shocked), strategy=ScriptedStrategy(decide=momentum))
    trunc_res, trunc = run(frame(rows[:cut]), strategy=ScriptedStrategy(decide=momentum))
    cut_time = ts(rows[cut][0])
    for other in (shock, trunc):
        for a, b in zip(base.contexts[:cut - 1], other.contexts[:cut - 1]):
            assert a.now == b.now
            pd.testing.assert_frame_equal(a.history, b.history)
    done = base_res.trades[base_res.trades.exit_time < cut_time].reset_index(drop=True)
    for other in (shock_res, trunc_res):
        o = other.trades[other.trades.exit_time < cut_time].reset_index(drop=True)
        pd.testing.assert_frame_equal(done, o)


def test_inconsistent_available_at_rejected():
    bars = frame(flat_bars("10:00", 3))
    bars["available_at"] = bars.index  # claims to be known at its own open
    with pytest.raises(DataError, match="available_at"):
        run(bars)
    with pytest.raises(DataError):
        run(frame(flat_bars("10:00", 3), minutes=1), execution=frame(flat_bars("10:00", 1, minutes=5), minutes=5))


def test_nan_prices_rejected_not_filled():
    bars = frame(flat_bars("10:00", 3))
    bars.iloc[1, 1] = np.nan
    with pytest.raises(DataError, match="NaN"):
        run(bars)


def test_data_gap_cancels_market_entry():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:10", 5020, 5021, 5019, 5020)]
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long")]})
    assert res.trades.empty
    assert res.orders.iloc[0]["reason"] == "fill_delay_exceeded"
    assert any("data gap" in w for w in res.warnings)


# ----------------------------------------------------------------- incomplete bars

def test_incomplete_signal_bar_skipped_by_default():
    bars = frame(flat_bars("10:00", 4), complete=[True, False, True, True])
    res, strat = run(bars, {"10:01": [OrderRequest.enter("long")]})
    assert [c.bar.name for c in strat.contexts] == [ts("10:00"), ts("10:02"), ts("10:03")]
    assert res.trades.empty and "incomplete_signal_bar_skipped" in set(res.rejections.reason)
    assert ts("10:01") in strat.contexts[1].history.index  # still part of later history


def test_incomplete_signal_bar_allowed_by_policy():
    bars = frame(flat_bars("10:00", 4), complete=[True, False, True, True])
    res, strat = run(bars, {"10:01": [OrderRequest.enter("long")]},
                     config=make_config(incomplete_signal_bar_policy="allow"))
    assert [c.bar_is_complete for c in strat.contexts] == [True, False, True, True]
    assert res.trades.iloc[0].entry_time == ts("10:02")


def test_entry_waits_for_complete_execution_bar():
    bars = frame(flat_bars("10:00", 4), complete=[True, False, True, True])
    res, _ = run(bars, {"10:00": [OrderRequest.enter("long")]})
    assert res.trades.iloc[0].entry_time == ts("10:02")  # 10:01 incomplete -> skipped


def test_entry_cancelled_when_incomplete_bars_exceed_delay():
    bars = frame(flat_bars("10:00", 5), complete=[True, False, False, False, True])
    res, _ = run(bars, {"10:00": [OrderRequest.enter("long")]})
    assert res.trades.empty and res.orders.iloc[0]["reason"] == "fill_delay_exceeded"


def test_entries_on_incomplete_bars_when_enabled_are_flagged():
    bars = frame(flat_bars("10:00", 3), complete=[True, False, True])
    res, _ = run(bars, {"10:00": [OrderRequest.enter("long")]}, config=make_config(entries_on_incomplete_bars=True))
    t = res.trades.iloc[0]
    assert t.entry_time == ts("10:01") and t.data_incomplete


def test_exits_still_processed_on_incomplete_bars():
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000), ("10:02", 5000, 5001, 4980, 4985)]
    bars = frame(rows, complete=[True, True, False])
    res, _ = run(bars, {"10:00": [OrderRequest.enter("long", stop_loss=4990)]})
    t = res.trades.iloc[0]
    assert t.exit_reason == "stop_loss" and t.exit_time == ts("10:02") and t.data_incomplete


# ----------------------------------------------------------------- duplicate prevention

def test_duplicate_signals_and_pending_entries_rejected():
    bars = frame(flat_bars("10:00", 5))
    script = {
        "10:00": [OrderRequest.enter("long", "stop", price=5100), OrderRequest.enter("long", "stop", price=5100)],
        "10:01": [OrderRequest.enter("long")],
    }
    res, _ = run(bars, script)
    assert len(res.orders) == 1
    assert list(res.rejections.reason) == ["duplicate_signal", "entry_pending"]


def test_no_entry_while_position_open():
    bars = frame(flat_bars("10:00", 5))
    res, _ = run(bars, {"10:00": [OrderRequest.enter("long")], "10:02": [OrderRequest.enter("short")]})
    assert len(res.trades) == 1 and "position_open" in set(res.rejections.reason)


def test_each_fill_executed_once():
    bars = frame(flat_bars("10:00", 6))
    res, strat = run(bars, {"10:00": [OrderRequest.enter("long")], "10:02": [OrderRequest.exit()],
                            "10:03": [OrderRequest.exit()]})
    assert len(strat.fills) == 2 and len(res.fills) == 2
    assert "no_position_to_exit" in set(res.rejections.reason)
