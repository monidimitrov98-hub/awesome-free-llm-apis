"""Read a paper-session journal (the authoritative, hash-chained event log) into analysable run data.

* READ-ONLY: the journal is opened with a read-only SQLite connection (``mode=ro``); keyed journals
  need the key and are verified with the same chain hash as :class:`~src.paper.journal.Journal`.
  Nothing is ever written (no pragma changes, no table creation).
* Every row is chain-verified as it is read (incrementally: only rows after the last verified seq),
  so analytics never use an edited journal silently.
* Records keep their originating journal ``seq``: every metric is traceable to its events.
* Memory is bounded: market events are aggregated (counts, gaps, last quote) instead of stored;
  strategy evaluations are kept only for signals (rejections are counted by reason).
* Quotes are snapshotted at decision / order / execution time from the event stream itself
  (the journal is read in sequence order), so spreads are the ones the system actually saw.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.paper.journal import _KEY_CHECK_MSG, KEY_ENV, Journal, JournalError

MAX_GAPS = 5000
GAP_SECONDS = 180.0  # a pause between valid events longer than this (inside a session) is a data gap


@dataclass
class Rec:
    seq: int
    ts: pd.Timestamp
    kind: str
    payload: dict[str, Any]
    quote: dict[str, Any] | None = None   # (bid, ask, quote_ts, last, last_ts) of the record's root at that moment


@dataclass
class MarketStats:
    events: int = 0
    trades: int = 0
    quotes: int = 0
    synthetic: int = 0
    first: pd.Timestamp | None = None
    last: pd.Timestamp | None = None
    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = field(default_factory=list)
    gaps_dropped: int = 0
    per_day: Counter = field(default_factory=Counter)      # trading (session) date -> events
    day_span: dict = field(default_factory=dict)            # trading date -> [first event ts, last event ts]
    day_contracts: dict = field(default_factory=dict)       # trading date -> contracts seen (e.g. "MES 12-26")
    bid: float | None = None
    ask: float | None = None
    quote_ts: pd.Timestamp | None = None
    last_price: float | None = None
    last_ts: pd.Timestamp | None = None
    spreads_seen: int = 0
    spread_sum: float = 0.0


class RunData:
    """Everything analytics needs from ONE journal (one run of one account)."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.run_id: str | None = None
        self.created: dict[str, Any] = {}
        self.identities: list[Rec] = []
        self.decisions: list[Rec] = []          # entry_decision (signal-time authorization)
        self.fill_auth: list[Rec] = []          # fill_authorization (fill-time re-check)
        self.signals: list[Rec] = []            # strategy_decision rows with decision == "signal"
        self.evaluations: Counter = Counter()   # (date, root, decision) -> count
        self.strategy_rejects: Counter = Counter()  # (date, root, reason) -> count
        self.regime: list[Rec] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.executions: list[Rec] = []
        self.trades: list[Rec] = []
        self.account_updates: list[Rec] = []
        self.protection: list[Rec] = []
        self.feed: list[Rec] = []
        self.data_rejected: Counter = Counter()  # (date, reason) -> count
        self.market: dict[str, MarketStats] = defaultdict(MarketStats)
        # time span on the run's MARKET clock (replay time in replay runs). Rows stamped with wall-clock time
        # (session_created / run_identity / recovered) or before the clock started (epoch) are excluded.
        self.first_ts: pd.Timestamp | None = None
        self.last_ts: pd.Timestamp | None = None
        self.rows = 0

    def identity_at(self, seq: int) -> dict[str, Any] | None:
        cur = None
        for r in self.identities:
            if r.seq <= seq:
                cur = r.payload
            else:
                break
        return cur


FEED_KINDS = {"feed_connected", "feed_disconnected", "feed_connect_failed", "feed_health", "data_delayed",
              "price_level_reset", "system_error", "shutdown", "persistence_failure", "persistence_recovered",
              "recovered", "session_change", "session_created"}
WALL_CLOCK_KINDS = {"session_created", "run_identity", "recovered"}
MIN_VALID_TS = pd.Timestamp("2000-01-01", tz="UTC")
PROTECTION_KINDS = {"account_guard", "flatten_requested", "emergency_stop", "daily_loss_limit", "protective_stop_restored"}


class ReadOnlyJournal:
    """Read-only view of a paper journal: same verification rules as ``Journal``, no write access."""

    _hash = Journal._hash
    bars = Journal.bars
    close = Journal.close

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise JournalError(f"no journal file {self.path.name!r}")
        uri = "file:" + urllib.parse.quote(str(self.path.resolve())) + "?mode=ro"
        try:
            self.db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
            keyed = self.db.execute("SELECT v FROM meta WHERE k='keyed'").fetchone()
            key_check = self.db.execute("SELECT v FROM meta WHERE k='key_check'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise JournalError(f"journal {self.path.name} is unreadable: {exc}") from exc
        if keyed is None:
            self.db.close()
            raise JournalError("journal has no metadata")
        env = os.environ.get(KEY_ENV)
        self.keyed = keyed[0] == "1"
        if self.keyed and not env:
            self.db.close()
            raise JournalError(f"this journal is HMAC-protected: set {KEY_ENV} to verify it")
        if not self.keyed and env:
            self.db.close()
            raise JournalError(f"{KEY_ENV} is set but this journal is NOT HMAC-protected (refused, as for trading)")
        self._key = env.encode() if self.keyed else b""
        if self.keyed and key_check is not None and not hmac.compare_digest(
                key_check[0], hmac.new(self._key, _KEY_CHECK_MSG, hashlib.sha256).hexdigest()):
            self.db.close()
            raise JournalError(f"{KEY_ENV} does not match this journal's key")


class JournalSource:
    """Incremental, verified reader of one journal file."""

    def __init__(self, path: str | Path, label: str | None = None) -> None:
        self.path = Path(path)
        self.label = label or self.path.parent.name
        self._lock = threading.Lock()
        self._journal: ReadOnlyJournal | None = None
        self.data = RunData(self.label)
        self._seq = 0
        self._prev = "GENESIS"
        self.error: str | None = None
        self._sd_cache: dict[int, str | None] = {}

    def close(self) -> None:
        with self._lock:
            if self._journal is not None:
                self._journal.close()
                self._journal = None

    def refresh(self) -> RunData:
        """Read (and verify) rows appended since the last call. Raises JournalError on a broken chain."""
        with self._lock:
            if self._journal is None:
                self._journal = ReadOnlyJournal(self.path)
            j = self._journal
            cur = j.db.execute("SELECT seq, ts, kind, payload, prev_hash, hash FROM journal WHERE seq > ? ORDER BY seq",
                               (self._seq,))
            for seq, ts, kind, payload, ph, h in cur:
                if seq != self._seq + 1 or ph != self._prev or j._hash(ph, seq, ts, kind, payload) != h:
                    self.error = f"journal chain broken at seq {seq}"
                    raise JournalError(self.error)
                self._seq, self._prev = seq, h
                if self.data.run_id is None:
                    self.data.run_id = hashlib.sha256(h.encode()).hexdigest()[:16]
                self._ingest(seq, pd.Timestamp(ts), kind, json.loads(payload))
            return self.data

    def bars(self, root: str) -> pd.DataFrame:
        with self._lock:
            if self._journal is None:
                self._journal = ReadOnlyJournal(self.path)
            return self._journal.bars(root)

    # ------------------------------------------------------------------ ingestion
    def _session_day(self, ts: pd.Timestamp) -> str | None:
        """CME trading date of ``ts`` (cached per hour: session boundaries fall on whole hours)."""
        hour = ts.value // 3_600_000_000_000
        day = self._sd_cache.get(hour, False)
        if day is False:
            from src.analytics.shadow.model import session_date

            if len(self._sd_cache) > 50_000:
                self._sd_cache.clear()
            day = self._sd_cache[hour] = session_date(ts.floor("h"))
        return day

    def _quote(self, root: str | None) -> dict[str, Any] | None:
        if not root or root not in self.data.market:
            return None
        m = self.data.market[root]
        return {"bid": m.bid, "ask": m.ask, "quote_ts": m.quote_ts, "last": m.last_price, "last_ts": m.last_ts}

    def _ingest(self, seq: int, ts: pd.Timestamp, kind: str, p: dict[str, Any]) -> None:
        d = self.data
        d.rows += 1
        if kind not in WALL_CLOCK_KINDS and ts >= MIN_VALID_TS:
            d.first_ts = ts if d.first_ts is None else min(d.first_ts, ts)
            d.last_ts = ts if d.last_ts is None else max(d.last_ts, ts)
        if kind == "market_event":
            root = p.get("root")
            m = d.market[root]
            ets = pd.Timestamp(p["ts"]) if p.get("ts") else ts
            if m.last_ts is not None or m.quote_ts is not None:
                prev = max(x for x in (m.last_ts, m.quote_ts) if x is not None)
                gap = (ets - prev).total_seconds()
                if gap > GAP_SECONDS:
                    if len(m.gaps) < MAX_GAPS:
                        m.gaps.append((prev, ets))
                    else:
                        m.gaps_dropped += 1
            m.events += 1
            m.first = ets if m.first is None else min(m.first, ets)
            m.last = ets if m.last is None else max(m.last, ets)
            day = self._session_day(ets)
            if day is not None:
                m.per_day[day] += 1
                inst = p.get("instrument")
                if inst and inst != root:
                    cs = m.day_contracts.setdefault(day, set())
                    if len(cs) < 4:
                        cs.add(str(inst)[:40])
                span = m.day_span.get(day)
                if span is None:
                    m.day_span[day] = [ets, ets]
                else:
                    span[0], span[1] = min(span[0], ets), max(span[1], ets)
            if p.get("synthetic"):
                m.synthetic += 1
            if p.get("kind") == "quote" and p.get("bid") is not None and p.get("ask") is not None:
                m.quotes += 1
                m.bid, m.ask, m.quote_ts = p["bid"], p["ask"], ets
                m.spreads_seen += 1
                m.spread_sum += p["ask"] - p["bid"]
            elif p.get("kind") == "trade" and p.get("last") is not None:
                m.trades += 1
                m.last_price, m.last_ts = p["last"], ets
            elif p.get("kind") == "trade":
                m.trades += 1
                m.last_ts = ets
            return
        if kind == "run_identity":
            d.identities.append(Rec(seq, ts, kind, p))
        elif kind == "session_created":
            d.created = p
        elif kind == "entry_decision":
            d.decisions.append(Rec(seq, ts, kind, p, self._quote(p.get("root"))))
        elif kind == "fill_authorization":
            d.fill_auth.append(Rec(seq, ts, kind, p))
        elif kind == "strategy_decision":
            dec = p.get("decision")
            date = str(p.get("session_date"))[:10]
            d.evaluations[(date, p.get("root"), dec)] += 1
            if dec == "signal":
                d.signals.append(Rec(seq, ts, kind, p, self._quote(p.get("root"))))
            elif dec in ("reject", "skip"):
                for reason in str(p.get("reasons") or p.get("rejection_reason") or "").split(";"):
                    if reason:
                        d.strategy_rejects[(date, p.get("root"), reason)] += 1
        elif kind == "regime_decision":
            d.regime.append(Rec(seq, ts, kind, p))
        elif kind.startswith("order_"):
            oid = p.get("order_id")
            o = d.orders.setdefault(oid, {"order_id": oid, "events": [], "details": None})
            if kind == "order_submitted":
                o["details"] = p.get("details")
                o["submitted_ts"] = ts
                o["submitted_seq"] = seq
                o["quote_at_submit"] = self._quote(p.get("root"))
            o["events"].append({"kind": kind[6:], "ts": ts, "seq": seq, "reason": p.get("reason")})
        elif kind == "execution":
            d.executions.append(Rec(seq, ts, kind, p, self._quote(p.get("root"))))
        elif kind == "trade_closed":
            d.trades.append(Rec(seq, ts, kind, p))
        elif kind == "account_update":
            d.account_updates.append(Rec(seq, ts, kind, p))
        elif kind in PROTECTION_KINDS:
            d.protection.append(Rec(seq, ts, kind, p))
        elif kind == "data_rejected":
            d.data_rejected[(str(ts.date()), p.get("reason"))] += 1
        if kind in FEED_KINDS:
            d.feed.append(Rec(seq, ts, kind, p))
