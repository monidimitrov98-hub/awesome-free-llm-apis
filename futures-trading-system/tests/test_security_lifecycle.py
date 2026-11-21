"""L3: explicit, idempotent order-lifecycle transitions in the strategy."""

import pandas as pd

from src.backtest.orders import Fill, OrderEvent, OrderRole
from src.strategy.vwap_pullback import VwapTrendPullbackStrategy
from src.strategy.vwap_pullback_config import load_strategy_config
from tests.strategy_fixtures import MES, NY, context_bars, ctx_for, long_bars

T = pd.Timestamp("2025-03-10 14:55", tz="UTC")


def submitted():
    s = VwapTrendPullbackStrategy(load_strategy_config(), NY)
    s.on_start(MES)
    req = s.on_bar(ctx_for(long_bars(), context_bars()))[0]
    return s, req.client_id


def ev(cid, status):
    return OrderEvent(cid, 1, status, T, "fill" if status == "filled" else "signal")


def test_duplicate_filled_events_consume_once():
    s, cid = submitted()
    for status in ("accepted", "filled", "filled", "filled"):
        s.on_order_event(ev(cid, status))
    assert s.signals_frame().status.iloc[0] == "filled" and len(s._consumed_pullbacks) == 1
    assert [v["issue"] for v in s.lifecycle_violations] == ["invalid transition"] * 2


def test_rejected_then_filled_is_impossible():
    s, cid = submitted()
    s.on_order_event(ev(cid, "rejected"))
    s.on_order_event(ev(cid, "filled"))
    assert s.signals_frame().status.iloc[0] == "rejected"
    assert s._consumed_pullbacks == set() and s.lifecycle_violations[0]["from"] == "rejected"


def test_out_of_order_and_delayed_events_ignored():
    s, cid = submitted()
    s.on_order_event(ev(cid, "filled"))  # before 'accepted': impossible
    assert s.signals_frame().status.iloc[0] == "submitted" and s._consumed_pullbacks == set()
    s.on_order_event(ev(cid, "accepted"))
    s.on_order_event(ev(cid, "filled"))
    s.on_order_event(ev(cid, "cancelled"))  # delayed cancel after the fill
    assert s.signals_frame().status.iloc[0] == "filled" and len(s.lifecycle_violations) == 2
    s.on_order_event(ev("unknown:id", "filled"))
    assert s.lifecycle_violations[-1]["issue"] == "unknown signal"


def test_duplicate_fill_notification_counted_once():
    s, cid = submitted()
    fill = Fill(7, OrderRole.ENTRY, True, 1, 5031.25, T, True, 1.0, 1.25, True, 5031.0)
    s.on_fill(fill)
    s.on_fill(fill)
    assert sum(s._entries_by_session.values()) == 1
