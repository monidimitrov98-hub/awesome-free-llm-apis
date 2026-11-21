"""Sprint 6 stress tests (sized for the normal suite; scripts/benchmark_marketdata.py runs larger loads):
high-frequency input, queue saturation with a slow consumer, repeated disconnects, bounded memory and
threads, runtime processing latency."""

import threading
import time
import tracemalloc

import pytest

from src.marketdata.bridge import wait_until
from src.paper.feeds import FeedError
from tests.marketdata_helpers import MES, MNQ, bridge_for, collect, live_session, make_feed, pump


def _flood(b, clock, n):
    t = clock.dt()
    for _ in range(n):
        b.quote(MES, t, 5999.75, 6000.0)


def test_high_frequency_input_is_delivered_completely_and_in_order():
    f, secret, clock = make_feed(queue_max_events=50_000)
    try:
        b = bridge_for(f, secret)
        f.connect()
        n = 10_000
        t0 = time.perf_counter()
        _flood(b, clock, n)
        got, _ = collect(f, n, 20)
        rate = n / (time.perf_counter() - t0)
        assert [e.provider_seq for e in got] == list(range(1, n + 1))       # nothing lost, nothing reordered
        assert f.server.stats.queue_overflows == 0 and rate > 2_000, rate
        b.close()
    finally:
        f.disconnect()


def test_slow_consumer_saturates_the_bounded_queue_and_fails_closed():
    f, secret, clock = make_feed(queue_max_events=200)
    try:
        b = bridge_for(f, secret)
        f.connect()
        try:
            _flood(b, clock, 2_000)                         # nobody polls
        except OSError:
            pass                                            # the backend dropped the connection mid-flood
        assert wait_until(lambda: f.server.lost.get(1) is not None, 5)
        assert f.server.stats.queue_overflows == 1 and "overflow" in f.server.lost[1]
        assert f.server.events.qsize() == 200               # bounded: never more than the capacity
        got, ups = collect(f)
        assert "backpressure_overflow" in [u.reason for u in ups]   # recorded, never silent
        assert [e.provider_seq for e in got] == list(range(1, len(got) + 1)) and len(got) >= 197
        with pytest.raises(FeedError):
            f.poll()
    finally:
        f.disconnect()


def test_repeated_disconnects_leak_no_threads_or_connections():
    f, secret, clock = make_feed()
    try:
        base = threading.active_count()
        for i in range(25):
            b = bridge_for(f, secret)
            f.connect()
            b.trade(MES, clock.dt(), 6000.0)
            b.close()
            assert wait_until(lambda: f.server.lost.get(f._conn_id) is not None, 2)
            with pytest.raises(FeedError):
                collect(f)
                f.poll()
        assert wait_until(lambda: threading.active_count() <= base + 1, 3), threading.active_count()
        assert f.server.stats.connections == 25 and f.server.stats.reconnections == 24
    finally:
        f.disconnect()


def test_long_session_memory_is_bounded(tmp_path):
    f, secret, clock = make_feed()
    s = live_session(tmp_path, f, clock, mode="infrastructure_validation")
    try:
        b = bridge_for(f, secret)
        f.connect()
        s.health.on_connect(clock())
        b.status("Connected")
        b.subscribe(MES)
        b.subscribe(MNQ)
        tracemalloc.start()
        done = 0
        peak_after_warmup = None
        for round_ in range(6):
            for _ in range(250):
                t = clock.dt()
                b.quote(MES, t, 5999.75, 6000.0)
                b.trade(MNQ, t, 21000.0, 1.0)
            done += 500
            assert pump(s, lambda s: s.seq >= done, 20)
            s._commit()
            if round_ == 1:
                peak_after_warmup = tracemalloc.get_traced_memory()[0]
        current = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        # rolling stats are bounded; growth over 2,000 more events stays small (journal is on disk)
        assert len(s.latency) <= 2000 and len(s.feed_delay) <= 2000 and len(s.health.history) <= 5000
        assert current - peak_after_warmup < 8_000_000, current - peak_after_warmup
        st = s.status()
        assert st["latency"]["processing_ms_p99"] is not None
        b.close()
    finally:
        f.disconnect()
