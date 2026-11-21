"""Regression tests for the Sprint 6 adversarial audit of the live market-data path."""

import re
import time
from pathlib import Path

import pytest

from src.marketdata.bridge import wait_until
from src.paper.feeds import FeedError
from tests.marketdata_helpers import MES, MNQ, bridge_for, collect, live_session, make_feed, pump

ADDON = Path(__file__).resolve().parents[1] / "ninjatrader" / "FtsMarketDataBridge.cs"


@pytest.fixture
def feed():
    f, secret, clock = make_feed()
    yield f, secret, clock
    f.disconnect()


# A1: a health update is applied exactly between the events received before and after it
def test_health_updates_are_applied_in_stream_order(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = bridge_for(f, secret)
    f.connect()
    s.health.on_connect(clock())
    b.status("Connected")
    b.subscribe(MES)
    b.subscribe(MNQ)
    for _ in range(3):
        b.trade(MES, clock.dt(), 6000.0)
    b.status("ConnectionLost")
    for _ in range(3):
        b.trade(MES, clock.dt(), 6000.0)
    assert pump(s, lambda s: s.seq >= 6)
    s._commit()
    seq = [(e["kind"], e["payload"].get("action")) for e in s.journal.entries() if e["kind"] in
           ("market_event", "feed_health")]
    block = len(seq) - 1 - seq[::-1].index(("feed_health", "block"))  # the ConnectionLost block
    assert [k for k, _ in seq[:block]].count("market_event") == 3
    assert [k for k, _ in seq[block:]].count("market_event") == 3


# A3: one NinjaTrader connection's recovery cannot clear another connection's outage
def test_provider_status_is_tracked_per_connection(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = bridge_for(f, secret)
    f.connect()
    s.health.on_connect(clock())
    b.subscribe(MES)
    b.subscribe(MNQ)
    b.status("ConnectionLost", "DataFeed")
    b.status("Connected", "Simulation")
    for _ in range(5):
        b.trade(MES, clock.dt(), 6000.0)
    assert pump(s, lambda s: s.seq >= 5)
    assert s.system_state("MES", clock()) == (False, "data_blocked:provider_not_connected:DataFeed")
    b.status("Disconnected", "DataFeed")  # switched off by the user: no longer blocks (no data -> stale check)
    for _ in range(4):
        b.trade(MES, clock.dt(), 6000.0)
    assert pump(s, lambda s: s.system_state("MES", clock())[0])


# A4: an unexpected backend error in the reader thread drops the connection (never a silent dead feed)
def test_internal_reader_error_drops_the_connection(feed, monkeypatch):
    f, secret, clock = feed
    b = bridge_for(f, secret)
    f.connect()

    def boom(*a, **k):
        raise KeyError("bug")
    monkeypatch.setattr(f.server, "_handle", boom)
    b.heartbeat()
    assert wait_until(lambda: (f.server.lost.get(1) or "").startswith("internal_error:KeyError"), 2)
    collect(f)
    assert f.last_loss.startswith("bridge disconnected: internal_error")
    with pytest.raises(FeedError):
        f.poll()


# A5: the record of lost connections is bounded
def test_lost_connection_history_is_bounded(feed):
    f, secret, _ = feed
    for _ in range(105):
        b = bridge_for(f, secret)
        b.close()
    assert wait_until(lambda: f.server.stats.connections == 105 and len(f.server.lost) <= 100, 5)


# A6: event ids never repeat across backend restarts (connection ids restart at 1)
def test_event_ids_are_unique_across_backend_restarts():
    ids = []
    for _ in range(2):
        f, secret, clock = make_feed()
        try:
            b = bridge_for(f, secret)
            f.connect()
            b.trade(MES, clock.dt(), 6000.0)
            events, _ = collect(f, 1)
            ids.append(events[0].event_id)
            b.close()
        finally:
            f.disconnect()
    assert ids[0] != ids[1] and all(i.endswith("-1-1") for i in ids)


# A2: the add-on reports subscriptions and the last provider status after EVERY handshake
def test_addon_reports_state_after_each_handshake():
    code = ADDON.read_text()
    loop = code[code.index("private void SendLoop"):code.index("private static void Send(")]
    after = loop[loop.index("Handshake(stream)"):]
    assert re.search(r"foreach \(Instrument instrument in subscribed\)\s*initial\.Add", after)
    assert "foreach (string name in failed)" in after and "lastStatus" in after
    assert after.index("initial.Add") < after.index("outbox.TryTake(out body, 250)")


# A7: a requested stop is honoured while the runtime waits for (re)connection -- no minute-long hang
def test_stop_is_honoured_while_waiting_for_the_bridge(tmp_path):
    f, _, clock = make_feed(connect_timeout=0.2)
    try:
        s = live_session(tmp_path, f, clock, mode="infrastructure_validation")
        t0 = time.monotonic()
        outcome = s.run(stop_when_exhausted=False, idle_sleep=0.005, should_stop=lambda: time.monotonic() - t0 > 0.5)
        assert outcome == "stopped" and time.monotonic() - t0 < 5 and s.clean_shutdown
    finally:
        f.disconnect()
