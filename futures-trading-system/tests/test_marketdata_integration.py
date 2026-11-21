"""Sprint 6 integration tests: real loopback server + mock NinjaTrader bridge + the paper runtime.
No NinjaTrader installation, no external network, no credentials."""

import json
import socket
import threading
import time

import pandas as pd
import pytest

from src.marketdata.bridge import wait_until
from src.marketdata.mock_bridge import MockBridge
from src.paper.feeds import FeedError
from tests.marketdata_helpers import MES, MNQ, bridge_for, collect, live_session, make_feed, pump


@pytest.fixture
def feed():
    f, secret, clock = make_feed()
    yield f, secret, clock
    f.disconnect()


def connected(f, secret, **kw):
    b = bridge_for(f, secret, **kw)
    f.connect()
    return b


def subscribe_all(b):
    b.status("Connected")
    b.subscribe(MES)
    b.subscribe(MNQ)


def stream(b, clock, n, instruments=(MES, MNQ), px=None, step_ms=0):
    for i in range(n):
        for name in instruments:
            p = (px or {MES: 6000.0, MNQ: 21000.0})[name]
            t = clock.dt()
            b.quote(name, t, p - 0.25, p)
            b.trade(name, t, p, 1.0)
        if step_ms:
            time.sleep(step_ms / 1000)


# ----------------------------------------------------------------- connection and authentication
def test_authenticated_bridge_delivers_normalized_events_with_provenance(feed):
    f, secret, clock = feed
    b = connected(f, secret)
    subscribe_all(b)
    t = clock.dt()
    b.quote(MES, t, 5999.75, 6000.0, 7, 9)
    b.trade(MNQ, t, 21000.25, 3)
    (q, tr), updates = collect(f, 2)
    assert [u.reason for u in updates][:2] == ["not_subscribed", "not_subscribed"]  # ordered before the data
    assert (q.root, q.kind, q.bid, q.ask, q.bid_size, q.ask_size) == ("MES", "quote", 5999.75, 6000.0, 7, 9)
    assert (tr.root, tr.kind, tr.last, tr.size) == ("MNQ", "trade", 21000.25, 3.0)
    assert q.ts_source == "provider" and q.exchange_ts is None and q.provider_ts == q.ts  # never substituted
    assert q.received_at is not None and q.bridge_ts is not None and q.instrument == MES and q.provider_seq == 1
    assert abs((q.ts - pd.Timestamp(t)).total_seconds()) < 1e-3


def test_wrong_secret_unknown_time_zone_and_garbage_are_refused(feed):
    f, secret, _ = feed
    with pytest.raises(ConnectionRefusedError):
        MockBridge("127.0.0.1", f.server.port, secret).connect(wrong_secret=True)
    with pytest.raises(ConnectionRefusedError):
        MockBridge("127.0.0.1", f.server.port, secret, tz="Mars Standard Time").connect()
    s = socket.create_connection(("127.0.0.1", f.server.port))
    s.recv(200)
    s.sendall(b"GET / HTTP/1.1\r\n\r\n")
    assert b"refused" in s.recv(200)
    s.close()
    assert wait_until(lambda: f.server.stats.refused_handshakes == 3, 2)
    assert not f.server.authenticated.is_set()


def test_connect_without_a_bridge_fails_safely():
    f, _, _ = make_feed(connect_timeout=0.3)
    try:
        with pytest.raises(FeedError, match="no authenticated"):
            f.connect()
        assert not f.connected
    finally:
        f.disconnect()


def test_unauthenticated_connections_are_limited_and_time_out(feed):
    f, secret, _ = feed
    idle = [socket.create_connection(("127.0.0.1", f.server.port)) for _ in range(8)]
    time.sleep(0.2)
    b = None
    try:
        # the pending-handshake slots are full; once the idle sockets time out, the bridge gets in
        assert wait_until(lambda: f.server.stats.refused_handshakes >= 4, 4)
        b = connected(f, secret)
        assert f.server.authenticated.is_set()
    finally:
        for s in idle:
            s.close()
        if b:
            b.close()


def test_backend_never_sends_anything_after_the_handshake(feed):
    f, secret, clock = feed
    b = connected(f, secret)
    subscribe_all(b)
    stream(b, clock, 20)
    b.sock.settimeout(0.3)
    with pytest.raises((socket.timeout, TimeoutError)):
        b.sock.recv(1)  # no command channel: nothing ever arrives


# ----------------------------------------------------------------- protocol failures drop the connection
@pytest.mark.parametrize("attack", ["bad_mac", "replay", "oversized", "unknown_type", "nan"])
def test_protocol_violation_drops_the_connection_but_keeps_earlier_events(feed, attack):
    f, secret, clock = feed
    b = connected(f, secret)
    subscribe_all(b)
    b.trade(MES, clock.dt(), 6000.0)
    if attack == "bad_mac":
        b.send_raw(b"0" * 64 + b' {"type":"heartbeat","seq":4,"t_bridge":"2026-01-01T00:00:00Z"}\n')
    elif attack == "replay":
        b.seq -= 1
        b.heartbeat()
    elif attack == "oversized":
        b.send_raw(b"x" * 10_000 + b"\n")
    elif attack == "unknown_type":
        b.send({"type": "order", "t_bridge": "2026-01-01T00:00:00Z"})
    else:
        b.send_raw(b.message({"type": "heartbeat", "t_bridge": "2026-01-01T00:00:00Z"}).replace(b"}", b',"x":NaN}'))
    assert wait_until(lambda: f.server.lost.get(1) is not None, 2)
    events, _ = collect(f)
    assert [e.last for e in events] == [6000.0]           # validated earlier data is kept
    assert f.last_loss.startswith("bridge disconnected: protocol_violation")
    with pytest.raises(FeedError):
        f.poll()
    assert f.status()["authenticated"] is False


def test_heartbeat_timeout_detects_a_dead_bridge():
    f, secret, _ = make_feed(heartbeat_timeout=0.3)
    try:
        b = connected(f, secret)
        assert wait_until(lambda: f.server.lost.get(1) == "heartbeat_timeout", 2)
        collect(f)
        with pytest.raises(FeedError):
            f.poll()
        b.close()
    finally:
        f.disconnect()


# ----------------------------------------------------------------- runtime integration
def test_runtime_validates_live_events_and_reports_status(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    s.health.on_connect(clock())
    subscribe_all(b)
    stream(b, clock, 5)
    t = clock.dt()
    b.quote(MES, t, 6000.25, 6000.0)          # crossed
    b.quote(MES, t, 5980.0, 6020.0)           # spread too wide
    b.trade(MES, t, 6000.1, 1.0)              # off the tick grid
    b.trade(MES, t, -5.0, 1.0)                # impossible price
    b.trade(MES, t, 6000.0, -1.0)             # negative size
    assert pump(s, lambda s: sum(s.validator.rejected.values()) >= 5)
    assert s.validator.rejected == {"crossed_quote": 1, "spread_too_wide": 1, "price_off_tick_grid": 1,
                                    "non_positive_price": 1, "negative_size": 1}
    st = s.status()
    json.dumps(st, allow_nan=False)
    mes = st["instruments"]["MES"]
    assert st["feed"]["provider"] == "NinjaTrader 8" and st["feed"]["authenticated"]
    assert mes["subscription"] == "subscribed" and mes["health"] == "ok" and mes["entries_allowed_by_data"]
    assert mes["bid"] == 5999.75 and mes["ask"] == 6000.0 and mes["spread"] == 0.25 and mes["last"] == 6000.0
    assert st["latency"]["samples"] >= 20 and st["latency"]["processing_ms_p50"] is not None
    assert st["market_session"]["in_session"]
    s._commit()
    kinds = [e["kind"] for e in s.journal.entries()]
    assert "feed_health" in kinds and "market_event" in kinds


def test_partial_subscription_blocks_only_the_missing_instrument(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    s.health.on_connect(clock())
    b.status("Connected")
    b.subscribe(MES)
    b.subscribe(MNQ, "failed")
    stream(b, clock, 5)
    assert pump(s, lambda s: s.seq >= 20)
    now = clock()
    assert s.system_state("MES", now) == (True, "")
    assert s.system_state("MNQ", now) == (False, "data_blocked:not_subscribed")
    assert s.status()["instruments"]["MNQ"]["subscription"] == "failed"


def test_provider_connection_loss_blocks_entries_until_fresh_data_after_recovery(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    s.health.on_connect(clock())
    subscribe_all(b)
    stream(b, clock, 4)
    assert pump(s, lambda s: s.seq >= 16)
    assert s.system_state("MES", clock())[0]
    b.status("ConnectionLost")
    stream(b, clock, 3)            # data that still arrives does not lift the block
    assert pump(s, lambda s: s.seq >= 28)
    assert s.system_state("MES", clock()) == (False, "data_blocked:provider_not_connected:MOCK")
    b.status("Connected")
    b.heartbeat()
    assert pump(s, lambda s: "recovering" in s.health.problem["MES"])
    assert not s.system_state("MES", clock())[0]       # fresh data still required
    stream(b, clock, 2)
    assert pump(s, lambda s: s.system_state("MES", clock())[0])


def test_dropped_bridge_events_and_book_reset_require_fresh_data(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    s.health.on_connect(clock())
    subscribe_all(b)
    stream(b, clock, 4)
    assert pump(s, lambda s: s.system_state("MES", clock())[0])
    b.eseq += 5                    # the bridge's queue overflowed: 5 events never arrived
    b.trade(MES, clock.dt(), 6000.0)
    assert pump(s, lambda s: not s.system_state("MES", clock())[0])
    assert s.health.problem["MES"] == "bridge_dropped_events" and f.server.stats.dropped_by_bridge == 5
    stream(b, clock, 3)
    assert pump(s, lambda s: s.system_state("MES", clock())[0])
    b.send({"type": "book_reset", "instrument": MES, "t_bridge": b._now_bridge()})
    assert pump(s, lambda s: s.health.problem["MES"] == "book_reset")


def test_instrument_mismatch_is_rejected_never_mapped(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    subscribe_all(b)
    for name in ("MES 03-31", "ES 12-30", "MES"):
        b.trade(name, clock.dt(), 6000.0)
    b.trade(MES, clock.dt(), 6000.0)
    assert pump(s, lambda s: s.seq >= 1)
    assert f.server.stats.rejected_messages == {"instrument_mismatch": 3}
    assert s.seq == 1


def test_timestamp_problems_are_rejected_and_flagged(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    subscribe_all(b)
    md = b.md(MES, "trade", clock.dt(), price=6000.0, size=1.0)
    md["t_utc"] = md["t_utc"].replace("T1", "T0")             # bridge conversion disagrees
    b.send(md)
    md2 = b.md(MES, "trade", clock.dt(), price=6000.0, size=1.0)
    md2["t_local"] = "2026-11-01T01:30:00.0000000"            # ambiguous local time
    md2["t_utc"] = "2026-11-01T05:30:00.0000000Z"
    b.send(md2)
    assert wait_until(lambda: sum(f.server.stats.rejected_messages.values()) == 2, 2)
    assert set(f.server.stats.rejected_messages) == {"timestamp_bridge_and_backend_disagree_on_the_UTC_t",
                                                     "timestamp_ambiguous_local_time"}
    pump(s, lambda s: s.health.problem["MES"].startswith("timestamp"), 1)


def test_future_stale_delayed_duplicate_and_out_of_order_events(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    s.health.on_connect(clock())
    subscribe_all(b)
    stream(b, clock, 4)
    assert pump(s, lambda s: s.system_state("MES", clock())[0])
    now = clock.dt()
    b.trade(MES, now + pd.Timedelta(minutes=10), 6000.0)      # future
    b.trade(MES, now - pd.Timedelta(seconds=30), 6000.0)      # out of order (older than the last event)
    assert pump(s, lambda s: s.validator.rejected.get("out_of_order") == 1)
    assert s.validator.rejected.get("timestamp_in_future") == 1
    clock.offset = pd.Timedelta(seconds=10)                    # backend clock runs ahead: data now 10 s late
    b.trade(MES, clock.dt() - pd.Timedelta(seconds=10), 6000.0)
    assert pump(s, lambda s: s.health.problem["MES"] == "data_delayed")
    assert not s.system_state("MES", clock())[0]
    s._commit()
    assert [e["payload"]["root"] for e in s.journal.entries("data_delayed")] == ["MES"]
    clock.offset = pd.Timedelta(minutes=5)                     # nothing for 5 minutes: stale
    assert s.system_state("MES", clock()) == (False, "market_data_stale")


def test_bridge_restart_reconnects_and_requires_fresh_data(tmp_path, feed):
    f, secret, clock = feed
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    s.health.on_connect(clock())
    subscribe_all(b)
    stream(b, clock, 4)
    assert pump(s, lambda s: s.system_state("MES", clock())[0])
    b.close()                                                  # NinjaTrader restarted
    with pytest.raises(FeedError):
        pump(s, lambda s: False, 1)
    s.health.on_disconnect(clock(), "test")
    b2 = bridge_for(f, secret)
    assert s.connect()
    assert f.server.stats.reconnections == 1
    subscribe_all(b2)
    b2.eseq = 1000
    stream(b2, clock, 1)
    assert pump(s, lambda s: s.seq >= 20)
    assert not s.system_state("MES", clock())[0]               # recovery pending
    stream(b2, clock, 3)
    assert pump(s, lambda s: s.system_state("MES", clock())[0])


def test_new_connection_replaces_a_half_open_one(feed):
    f, secret, clock = feed
    b1 = connected(f, secret)
    b2 = bridge_for(f, secret)
    assert wait_until(lambda: f.server.lost.get(1) == "replaced_by_new_connection", 2)
    with pytest.raises(FeedError):
        collect(f)
        f.poll()
    f.connect()
    b2.status("Connected")
    assert wait_until(lambda: f.server.stats.provider_connection == "Connected", 2)
    b1.close()
    b2.close()


def test_backend_restart_recovers_the_journal_and_reconnects(tmp_path):
    f, secret, clock = make_feed()
    s = live_session(tmp_path, f, clock)
    b = connected(f, secret)
    s.health.on_connect(clock())
    subscribe_all(b)
    stream(b, clock, 3)
    assert pump(s, lambda s: s.seq >= 12)
    s._commit()
    s.journal.close()
    f.disconnect()
    b.close()
    f2, _, _ = make_feed(secret, clock)
    try:
        s2 = live_session(tmp_path, f2, clock, create=False)
        assert s2.seq == 12 and s2.reconciliation_ok
        b2 = bridge_for(f2, secret)
        assert s2.connect()
        subscribe_all(b2)
        stream(b2, clock, 1)
        assert pump(s2, lambda s: s.seq >= 16)
        b2.close()
    finally:
        f2.disconnect()


def test_full_run_loop_with_the_mock_bridge_ends_safely(tmp_path):
    f, secret, clock = make_feed()
    s = live_session(tmp_path, f, clock, mode="infrastructure_validation")
    stop = threading.Event()

    def bridge():
        b = MockBridge("127.0.0.1", f.server.port, secret)
        b.connect()
        subscribe_all(b)
        try:
            while not stop.is_set():
                stream(b, clock, 1)
                b.heartbeat()
                time.sleep(0.01)
        except OSError:
            pass  # the backend shut down and closed the connection (expected at the end of the run)
        b.close()
    t = threading.Thread(target=bridge, daemon=True)
    t.start()
    end = time.monotonic() + 1.5
    statuses = []
    outcome = s.run(stop_when_exhausted=False, idle_sleep=0.01, should_stop=lambda: time.monotonic() > end,
                    on_cycle=lambda ss: statuses.append(ss.status()["validation"]["events_processed"]))
    stop.set()
    t.join(2)
    f.disconnect()
    assert outcome == "stopped" and s.seq > 20 and statuses[-1] > 0
    assert not s.broker.orders                                  # infrastructure validation: no entries
    assert s.run_kind == "infrastructure_validation"
