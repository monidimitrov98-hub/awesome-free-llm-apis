"""Builders for Sprint 6 live-market-data tests: a real loopback bridge server and the mock bridge.
No NinjaTrader, no network beyond 127.0.0.1, no credentials."""

from __future__ import annotations

import secrets
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.marketdata.bridge import NinjaTraderBridgeFeed, wait_until
from src.marketdata.config import MarketDataConfig
from src.marketdata.instruments import InstrumentResolver
from src.marketdata.mock_bridge import MockBridge
from tests.paper_helpers import APP, PCFG, T, allow_news, session

MES, MNQ = "MES 12-30", "MNQ 12-30"     # far-future contracts: the tests never "expire"
CONTRACTS = {"MES": MES, "MNQ": MNQ}


def md_cfg(**kw) -> MarketDataConfig:
    base = dict(host="127.0.0.1", port=0, secret_file=Path("unused"), max_message_bytes=4096, queue_max_events=5000,
                connect_timeout=3.0, heartbeat_timeout=5.0, handshake_timeout=2.0, max_pending_handshakes=4,
                contracts=dict(CONTRACTS))
    return MarketDataConfig(**(base | kw))


class Clock:
    """A clock running at real speed from a fixed in-session start (Monday 2025-03-10 10:00 New York)."""

    def __init__(self, start: pd.Timestamp | None = None) -> None:
        self.start = start if start is not None else T("10:00")
        self.t0 = time.monotonic()
        self.offset = pd.Timedelta(0)

    def __call__(self) -> pd.Timestamp:
        return self.start + pd.Timedelta(seconds=time.monotonic() - self.t0) + self.offset

    def dt(self) -> datetime:
        return self().floor("us").to_pydatetime().astimezone(timezone.utc)


def make_feed(secret: bytes | None = None, clock: Clock | None = None, roots=("MES", "MNQ"), **kw):
    secret = secret or secrets.token_bytes(32)
    clock = clock or Clock()
    cfg = md_cfg(contracts={r: CONTRACTS[r] for r in roots}, **kw)
    feed = NinjaTraderBridgeFeed(cfg, secret, InstrumentResolver(cfg.contracts, APP.contracts), clock=clock)
    feed.server.start()
    return feed, secret, clock


def bridge_for(feed, secret, **kw) -> MockBridge:
    b = MockBridge("127.0.0.1", feed.server.port, secret, **kw)
    b.connect()
    return b


def live_session(tmp_path, feed, clock, *, roots=("MES", "MNQ"), mode="paper", script=None, **kw):
    pcfg = replace(PCFG, mode=mode, instruments=tuple(roots), resume_after_events=3)
    return session(tmp_path, [], script or {}, pcfg=pcfg, news=allow_news(tuple(roots)),
                   feed_cls=lambda _events: feed, clock=clock, **kw)


def collect(feed, n=None, timeout=3.0):
    """Drain the feed (events AND ordered health updates) until ``n`` events, the queue is empty
    (``n`` None), the connection loss (raised by poll only after everything before it was drained;
    recorded in ``feed.last_loss``) or the timeout."""
    from src.paper.feeds import FeedError

    events, updates = [], []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            got = feed.poll(10_000)
        except FeedError as exc:
            feed.last_loss = str(exc)
            updates += feed.health_updates()
            break
        ups = feed.health_updates()
        updates += ups
        events += got
        if n is not None and len(events) >= n:
            break
        if n is None and not got and not ups and feed.server.events.qsize() == 0 and feed._held is None:
            try:
                feed.poll(1)  # surfaces a pending connection loss
            except FeedError as exc:
                feed.last_loss = str(exc)
            break
        if not got:
            time.sleep(0.002)
    return events, updates


def pump(s, until, timeout=3.0):
    """Process events and feed updates until ``until(s)`` (or the timeout)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s._apply_feed_updates()
        for ev in s.feed.poll(1000):
            s.process_event(ev)
        if until(s):
            s._apply_feed_updates()
            return True
        time.sleep(0.005)
    return until(s)


__all__ = ["MES", "MNQ", "CONTRACTS", "md_cfg", "Clock", "make_feed", "bridge_for", "live_session", "pump", "collect",
           "wait_until"]
