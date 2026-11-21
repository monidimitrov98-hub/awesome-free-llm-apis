"""Sprint 5: market data adapters, validation, session calendar and data health."""

import pandas as pd
import pytest

from src.data.sessions import assign_session_dates
from src.paper.events import EventError, MarketEvent, event_from_mapping
from src.paper.feeds import (BarReplayFeed, FeedError, SimulatedRealtimeFeed, TickReplayFeed, UnconfiguredLiveFeed,
                             bars_to_events)
from src.paper.validation import DataHealth, EventValidator, SessionCalendar
from tests.paper_helpers import APP, ListFeed, T, quote, trade

SPECS = {r: APP.contracts[r] for r in ("MES", "MNQ")}


def bars(n=3, start="10:00"):
    idx = pd.date_range(T(start), periods=n, freq="1min", name="timestamp")
    return pd.DataFrame({"open": 5000.0, "high": 5001.0, "low": 4999.0, "close": [5000.5, 4999.5, 5000.0][:n],
                         "volume": 40.0}, index=idx)


# ----------------------------------------------------------------- feeds
def test_feed_lifecycle_and_disconnect():
    f = ListFeed([trade(T("10:00"), 5000.0)])
    with pytest.raises(FeedError):
        f.poll()
    f.connect()
    assert len(f.poll()) == 1 and f.exhausted
    f.disconnect()
    with pytest.raises(FeedError):
        f.poll()


def test_bar_replay_is_labelled_synthetic_and_ordered():
    evs = list(bars_to_events(bars(), "MES"))
    assert len(evs) == 15 and all(e.synthetic_from_bars and e.kind == "trade" for e in evs)
    up = [e.last for e in evs[:5]]
    assert up == [5000.0, 5000.0, 4999.0, 5001.0, 5000.5]  # up bar: open, open (no volume), low, high, close
    assert [e.last for e in evs[5:10]] == [5000.0, 5000.0, 5001.0, 4999.0, 4999.5]  # down bar
    assert evs[1].size == 0 and all(e.bar_high == 5001.0 and e.bar_low == 4999.0 for e in evs[:5])
    assert all(a.ts < b.ts for a, b in zip(evs, evs[1:]))
    f = BarReplayFeed({"MES": bars(), "MNQ": bars()})
    f.connect()
    got = f.poll(10_000)
    assert len(got) == 30 and all(a.ts <= b.ts for a, b in zip(got, got[1:]))


def test_tick_replay_reports_bad_rows(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("event_id,root,kind,ts,last,size,bid,ask,bid_size,ask_size\n"
                 "1,MES,trade,2025-03-10T14:00:00+00:00,5000.0,2,,,,\n"
                 "2,MES,quote,2025-03-10T14:00:01+00:00,,,4999.75,5000.0,5,6\n"
                 "3,MES,trade,2025-03-10 14:00:02,5000.0,2,,,,\n"          # naive timestamp
                 "4,MES,trade,2025-03-10T14:00:03+00:00,abc,2,,,,\n"         # bad price
                 "5,MES,boom,2025-03-10T14:00:04+00:00,5000.0,1,,,,\n")      # bad kind
    f = TickReplayFeed(p)
    assert len(f._events) == 2 and [r for r, _ in f.bad_rows] == [4, 5, 6]


def test_simulated_realtime_paces_and_disconnects():
    now = {"t": pd.Timestamp("2026-01-01", tz="UTC")}
    inner = ListFeed([trade(T("10:00") + pd.Timedelta(seconds=10 * i), 5000.0) for i in range(5)])
    f = SimulatedRealtimeFeed(inner, lambda: now["t"], speed=10.0, faults=[(3, "disconnect")])
    f.connect()
    assert len(f.poll()) == 1                     # only the first is due
    now["t"] += pd.Timedelta(seconds=2)           # 20 s of data at 10x
    assert len(f.poll()) == 2
    now["t"] += pd.Timedelta(seconds=10)
    with pytest.raises(FeedError, match="disconnect"):
        f.poll()
    f.connect()
    assert len(f.poll()) == 2 and f.exhausted


def test_live_feed_is_not_implemented():
    f = UnconfiguredLiveFeed()
    with pytest.raises(FeedError, match="no live market data provider"):
        f.connect()
    assert f.kind == "live"


def test_event_parsing_is_strict():
    ok = event_from_mapping({"event_id": "1", "root": "mes", "kind": "trade", "ts": "2025-03-10T14:00:00Z",
                             "last": "5000.25", "size": "1"})
    assert ok.root == "MES" and ok.ts.tz is not None
    for bad in ({"event_id": "1", "root": "MES", "kind": "trade", "ts": "2025-03-10 14:00", "last": 1},
                {"event_id": "1", "root": "MES", "kind": "trade", "last": 1},
                {"event_id": "1", "root": "MES", "kind": "trade", "ts": "2025-03-10T14:00Z", "last": "nan"},
                {"event_id": "", "root": "MES", "kind": "trade", "ts": "2025-03-10T14:00Z", "last": 1}):
        with pytest.raises(EventError):
            event_from_mapping(bad)


# ----------------------------------------------------------------- validation
def test_validator_rejections():
    v = EventValidator(SPECS, APP.session, max_jump_ticks=40)
    t0 = T("10:00")
    assert v.check(trade(t0, 5000.0, eid="a")).ok
    cases = [
        (trade(t0, 5000.0, eid="a"), "duplicate_event"),
        (trade(t0 - pd.Timedelta(seconds=1), 5000.0), "out_of_order"),
        (trade(t0, 5000.1), "price_off_tick_grid"),
        (trade(t0, -1.0), "non_positive_price"),
        (quote(t0, 5000.25, 5000.0), "crossed_quote"),
        (trade(t0, 5100.0), "implausible_price_jump"),
        (MarketEvent("x", "ES", "trade", t0, 5000.0, 1), "unknown_instrument"),
        (MarketEvent("y", "MES", "quote", t0, None, 0, None, 5000.0), "quote_without_bid_ask"),
        (MarketEvent("z", "MES", "trade", t0, 5000.0, -2), "negative_size"),
    ]
    for ev, reason in cases:
        r = v.check(ev)
        assert not r.ok and r.reason == reason, (reason, r)
    assert v.check(trade(t0 + pd.Timedelta(seconds=1), 5000.25)).ok
    out = v.check(trade(T("17:30"), 5000.25))
    assert out.ok and out.session_date is None and "outside_session" in out.warnings


@pytest.mark.parametrize("start,end", [("2025-03-07 12:00", "2025-03-11 12:00"),   # DST spring forward
                                       ("2025-10-31 12:00", "2025-11-04 12:00")])  # DST fall back
def test_session_calendar_matches_pipeline_every_minute(start, end):
    idx = pd.date_range(pd.Timestamp(start, tz=NY_TZ), pd.Timestamp(end, tz=NY_TZ), freq="1min").tz_convert("UTC")
    expected = assign_session_dates(idx, APP.session)
    cal = SessionCalendar(APP.session)
    for t in list(idx[::7]) + [i + pd.Timedelta(seconds=59) for i in idx[::131]]:
        e = expected.loc[t.floor("min")]
        got = cal(t)
        assert (got is None and pd.isna(e)) or got == e, t


NY_TZ = "America/New_York"


def test_data_health_stale_disconnect_and_recovery():
    h = DataHealth(["MES"], pd.Timedelta(minutes=3), resume_after_events=3)
    t0 = T("10:00")
    assert h.healthy("MES", t0) == (False, "feed_disconnected")
    h.on_connect(t0)
    assert h.healthy("MES", t0)[1] == "no_market_data"
    for i in range(3):
        h.on_event("MES", t0 + pd.Timedelta(seconds=i))
    assert h.healthy("MES", t0 + pd.Timedelta(seconds=3)) == (True, "")
    assert h.healthy("MES", t0 + pd.Timedelta(minutes=4))[1] == "market_data_stale"
    h.on_event("MES", t0 + pd.Timedelta(minutes=10))  # after a gap: recovery pending
    assert "data_recovery_pending" in h.healthy("MES", t0 + pd.Timedelta(minutes=10))[1]
    h.on_event("MES", t0 + pd.Timedelta(minutes=10, seconds=1))
    h.on_event("MES", t0 + pd.Timedelta(minutes=10, seconds=2))
    assert h.healthy("MES", t0 + pd.Timedelta(minutes=10, seconds=2)) == (True, "")
    h.on_disconnect(t0 + pd.Timedelta(minutes=11), "test")
    assert h.healthy("MES", t0 + pd.Timedelta(minutes=11))[1] == "feed_disconnected"
    h.on_connect(t0 + pd.Timedelta(minutes=11))
    h.on_event("MES", t0 + pd.Timedelta(minutes=11, seconds=1))
    assert "data_recovery_pending" in h.healthy("MES", t0 + pd.Timedelta(minutes=11, seconds=1))[1]
