"""Market data adapters. The strategy and execution engine only see validated
:class:`MarketEvent` objects; adapters never decide anything.

* :class:`BarReplayFeed`   historical 1-minute OHLCV bars -> SYNTHETIC trade events (O, then the
                           adverse extreme first, then the other extreme, then C). An approximation
                           for replay testing only; results are labelled ``replay``.
* :class:`TickReplayFeed`  a recorded trade/quote CSV (UTC timestamps) -> events, unchanged.
* :class:`SimulatedRealtimeFeed`  wraps a replay feed, releases events according to a clock
                           (wall clock or injected), and can inject disconnects/stalls for tests.
* :class:`LiveFeed`        INTERFACE ONLY for a future genuine provider. No provider is
                           implemented or connected; :class:`UnconfiguredLiveFeed` always refuses.

No adapter here opens a network connection.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Iterable, Iterator

import pandas as pd

from src.paper.events import EventError, MarketEvent, event_from_mapping


class FeedError(RuntimeError):
    """The feed is disconnected or failed."""


class MarketDataFeed(ABC):
    kind: str = "replay"   # "replay" | "simulated_realtime" | "live"
    name: str = "feed"

    def __init__(self) -> None:
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    @abstractmethod
    def poll(self, max_events: int = 1000) -> list[MarketEvent]:
        """Next available events (empty when none yet). Raises FeedError when disconnected."""

    @property
    @abstractmethod
    def exhausted(self) -> bool:
        """True when a finite (replay) feed has delivered every event."""


def bars_to_events(bars: pd.DataFrame, root: str, source: str = "bar_replay") -> Iterator[MarketEvent]:
    """Synthetic trade path for each 1-minute bar (times within the minute)."""
    offsets = (pd.Timedelta(seconds=0), pd.Timedelta(seconds=20), pd.Timedelta(seconds=40), pd.Timedelta(seconds=59))
    for t, row in bars.iterrows():
        o, h, low, c, v = (float(row[k]) for k in ("open", "high", "low", "close", "volume"))
        path = (o, low, h, c) if c >= o else (o, h, low, c)
        size = max(v / 4.0, 0.0)
        for i, (px, off) in enumerate(zip(path, offsets)):
            yield MarketEvent(f"{root}-{t.strftime('%Y%m%dT%H%M')}-{i}", root, "trade", t + off, px, size,
                              source=source, synthetic_from_bars=True)


class _ListFeed(MarketDataFeed):
    def __init__(self, events: Iterable[MarketEvent]) -> None:
        super().__init__()
        self._events = list(events)
        self._pos = 0

    def poll(self, max_events: int = 1000) -> list[MarketEvent]:
        if not self.connected:
            raise FeedError("feed not connected")
        out = self._events[self._pos:self._pos + max_events]
        self._pos += len(out)
        return out

    @property
    def exhausted(self) -> bool:
        return self._pos >= len(self._events)

    def peek_time(self) -> pd.Timestamp | None:
        return self._events[self._pos].ts if self._pos < len(self._events) else None


class BarReplayFeed(_ListFeed):
    kind = "replay"
    name = "bar_replay"

    def __init__(self, bars_by_root: dict[str, pd.DataFrame]) -> None:
        events = [e for root, bars in bars_by_root.items() for e in bars_to_events(bars, root)]
        events.sort(key=lambda e: (e.ts, e.root, e.event_id))
        super().__init__(events)


class TickReplayFeed(_ListFeed):
    """CSV columns: event_id, root, kind, ts (ISO with offset), last, size, bid, ask, bid_size, ask_size.
    Rows that cannot be parsed are kept as ``bad_rows`` (never silently dropped from the report)."""

    kind = "replay"
    name = "tick_replay"

    def __init__(self, path: str | Path) -> None:
        self.bad_rows: list[tuple[int, str]] = []
        events = []
        with open(path, newline="", encoding="utf-8") as fh:
            for i, row in enumerate(csv.DictReader(fh), start=2):
                try:
                    events.append(event_from_mapping(row, source=f"tick_replay:{Path(path).name}"))
                except EventError as exc:
                    self.bad_rows.append((i, str(exc)))
        super().__init__(events)  # file order kept: ordering problems are detected by the validator


class SimulatedRealtimeFeed(MarketDataFeed):
    """Releases the inner feed's events when ``clock()`` reaches ``start + (event.ts - first.ts) / speed``.
    ``faults``: list of (event_index, "disconnect") -- the feed disconnects before that event."""

    kind = "simulated_realtime"
    name = "simulated_realtime"

    def __init__(self, inner: _ListFeed, clock: Callable[[], pd.Timestamp], speed: float = 1.0,
                 faults: list[tuple[int, str]] | None = None) -> None:
        super().__init__()
        self.inner = inner
        self.clock = clock
        self.speed = speed
        self.faults = dict(faults or [])
        self._start: pd.Timestamp | None = None
        self._first: pd.Timestamp | None = None
        self._delivered = 0

    def connect(self) -> None:
        super().connect()
        self.inner.connect()
        if self._start is None:
            self._start, self._first = self.clock(), self.inner.peek_time()

    def poll(self, max_events: int = 1000) -> list[MarketEvent]:
        if not self.connected:
            raise FeedError("feed disconnected")
        out = []
        while len(out) < max_events and not self.inner.exhausted:
            if self.faults.get(self._delivered) == "disconnect":
                self.faults.pop(self._delivered)
                self.connected = False
                if out:
                    return out
                raise FeedError("simulated disconnect")
            due = self._start + (self.inner.peek_time() - self._first) / self.speed
            if self.clock() < due:
                break
            out += self.inner.poll(1)
            self._delivered += 1
        return out

    @property
    def exhausted(self) -> bool:
        return self.inner.exhausted


class LiveFeed(MarketDataFeed):
    """Interface a genuine live market data provider must implement (Sprint 5: none exists).

    Requirements for an implementation: authenticated connection with credentials from the
    environment (never config files or Git), exchange timestamps in UTC, trades and top-of-book
    quotes for MES/MNQ, sequence numbers or ids for duplicate/out-of-order detection, heartbeat,
    and FeedError on any disconnect. Market data licensing (CME) and the provider's subscription
    are the user's responsibility. Read-only: a market data feed never sends orders."""

    kind = "live"


class UnconfiguredLiveFeed(LiveFeed):
    name = "unconfigured_live"

    def connect(self) -> None:
        raise FeedError("no live market data provider is configured or implemented (Sprint 5: replay only)")

    def poll(self, max_events: int = 1000) -> list[MarketEvent]:
        raise FeedError("no live market data provider")

    @property
    def exhausted(self) -> bool:
        return False
