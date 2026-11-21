"""Market data validation: every event is checked before it can update bars, prices or fills.

Rejected events are never used (no price is ever "repaired" or fabricated). Checks:
timestamp present/UTC, known instrument, positive finite prices on the tick grid, bid <= ask,
trade size >= 0, duplicate event ids, out-of-order timestamps (dropped), implausible jumps
(dropped), events outside the trading session (kept for health, flagged). Staleness is judged
against the processing clock by :class:`DataHealth`.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.config import ContractSpec, SessionConfig
from src.data.sessions import assign_session_dates
from src.paper.events import MarketEvent


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    session_date: pd.Timestamp | None = None
    warnings: list[str] = field(default_factory=list)


class SessionCalendar:
    """Session date of a timestamp (New York session rules, DST-aware), cached per minute."""

    def __init__(self, session: SessionConfig, cache_size: int = 20_000) -> None:
        self.session = session
        self._cache: OrderedDict[pd.Timestamp, pd.Timestamp | None] = OrderedDict()
        self.cache_size = cache_size
        # session bounds and DST changes fall on bucket edges: 15-minute buckets when the session
        # starts/ends on a quarter hour (always true for the CME Globex session), else 1 minute
        quarter = session.start.minute % 15 == 0 and session.end.minute % 15 == 0 \
            and session.start.second == 0 and session.end.second == 0
        self._bucket = "15min" if quarter else "1min"

    def __call__(self, ts: pd.Timestamp) -> pd.Timestamp | None:
        key = ts.floor(self._bucket)
        if key not in self._cache:
            sd = assign_session_dates(pd.DatetimeIndex([key]), self.session).iloc[0]
            self._cache[key] = None if pd.isna(sd) else pd.Timestamp(sd)
            if len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return self._cache[key]


def _on_grid(price: float, tick: float) -> bool:
    return abs(price / tick - round(price / tick)) < 1e-6


class EventValidator:
    def __init__(self, specs: dict[str, ContractSpec], session: SessionConfig, *, dedupe_window: int = 200_000,
                 max_jump_ticks: int = 400, level_confirm_events: int = 3) -> None:
        self.level_confirm_events = level_confirm_events
        self._candidate: dict[str, tuple[float, int]] = {}
        self._ids_at_last: dict[str, set[str]] = {}
        self.specs = specs
        self.session = session
        self.dedupe_window = dedupe_window
        self.max_jump_ticks = max_jump_ticks
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._last_ts: dict[str, pd.Timestamp] = {}
        self._last_price: dict[str, float] = {}
        self.rejected: dict[str, int] = {}
        self.calendar = SessionCalendar(session)

    def _reject(self, reason: str) -> ValidationResult:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        return ValidationResult(False, reason)

    def check(self, ev: MarketEvent) -> ValidationResult:
        spec = self.specs.get(ev.root)
        if spec is None:
            return self._reject("unknown_instrument")
        if ev.ts is None or pd.isna(ev.ts) or ev.ts.tzinfo is None:
            return self._reject("missing_timestamp")
        key = f"{ev.root}:{ev.event_id}"
        if key in self._seen or ev.event_id in self._ids_at_last.get(ev.root, set()):
            return self._reject("duplicate_event")
        last = self._last_ts.get(ev.root)
        if last is not None and ev.ts < last:
            return self._reject("out_of_order")
        prices = [p for p in (ev.last, ev.bid, ev.ask) if p is not None]
        if ev.kind == "trade" and ev.last is None:
            return self._reject("trade_without_price")
        if ev.kind == "quote" and (ev.bid is None or ev.ask is None):
            return self._reject("quote_without_bid_ask")
        if any(p <= 0 for p in prices):
            return self._reject("non_positive_price")
        if any(not _on_grid(p, spec.tick_size) for p in prices):
            return self._reject("price_off_tick_grid")
        if ev.bid is not None and ev.ask is not None and ev.bid > ev.ask:
            return self._reject("crossed_quote")
        if ev.size < 0 or any(s is not None and s < 0 for s in (ev.bid_size, ev.ask_size)):
            return self._reject("negative_size")
        ref = self._last_price.get(ev.root)
        px = ev.last if ev.last is not None else (ev.bid + ev.ask) / 2
        level_reset = False
        if ref is not None and abs(px - ref) > self.max_jump_ticks * spec.tick_size:
            # A single outlier is rejected. A genuine move to a new level is accepted once
            # ``level_confirm_events`` consecutive events agree on it -- otherwise one real gap would
            # blind the system forever (no marks, protective stops never triggered).
            cand = self._candidate.get(ev.root)
            n = cand[1] + 1 if cand and abs(px - cand[0]) <= self.max_jump_ticks * spec.tick_size else 1
            self._candidate[ev.root] = (px, n)
            if n < self.level_confirm_events:
                return self._reject("implausible_price_jump")
            level_reset = True
        self._candidate.pop(ev.root, None)
        # accepted
        self._seen[key] = None
        if len(self._seen) > self.dedupe_window:
            self._seen.popitem(last=False)
        if last is None or ev.ts > last:
            self._ids_at_last[ev.root] = set()
        self._ids_at_last.setdefault(ev.root, set()).add(ev.event_id)
        self._last_ts[ev.root] = ev.ts
        self._last_price[ev.root] = px
        res = ValidationResult(True, session_date=self.calendar(ev.ts))
        if level_reset:
            res.warnings.append("price_level_reset")
        if res.session_date is None:
            res.warnings.append("outside_session")
        return res

    def state(self) -> dict[str, Any]:
        return {"last_ts": {k: v.isoformat() for k, v in self._last_ts.items()},
                "last_price": dict(self._last_price), "rejected": dict(self.rejected),
                "ids_at_last_ts": {k: sorted(v) for k, v in self._ids_at_last.items()}}

    def restore(self, st: dict[str, Any]) -> None:
        self._last_ts = {k: pd.Timestamp(v) for k, v in st.get("last_ts", {}).items()}
        self._last_price = {k: float(v) for k, v in st.get("last_price", {}).items()}
        self.rejected = dict(st.get("rejected", {}))
        # duplicates of the last processed timestamp must stay duplicates after a restart
        self._ids_at_last = {k: set(v) for k, v in st.get("ids_at_last_ts", {}).items()}


class DataHealth:
    """Connection and freshness per instrument. Entries are allowed only while HEALTHY:
    connected, fresh, and -- after any disconnect or staleness -- at least
    ``resume_after_events`` new valid events received ("recovery pending" otherwise)."""

    def __init__(self, roots: list[str], stale_after: pd.Timedelta, resume_after_events: int) -> None:
        self.roots = roots
        self.stale_after = stale_after
        self.resume_after = resume_after_events
        self.connected = False
        self.last_event: dict[str, pd.Timestamp] = {}
        self.fresh_since_problem: dict[str, int] = {r: 0 for r in roots}
        self.problem: dict[str, str] = {r: "no_data_yet" for r in roots}
        self.history: list[dict[str, Any]] = []

    def _set(self, root: str, problem: str, now: pd.Timestamp) -> None:
        if self.problem.get(root) != problem:
            self.history.append({"time": now.isoformat(), "root": root, "problem": problem})
        self.problem[root] = problem

    def on_connect(self, now: pd.Timestamp) -> None:
        self.connected = True
        self.history.append({"time": now.isoformat(), "event": "connected"})

    def on_disconnect(self, now: pd.Timestamp, reason: str = "") -> None:
        self.connected = False
        for r in self.roots:
            self.fresh_since_problem[r] = 0
            self._set(r, "disconnected", now)
        self.history.append({"time": now.isoformat(), "event": "disconnected", "reason": reason})

    def on_event(self, root: str, ts: pd.Timestamp) -> None:
        prev = self.last_event.get(root)
        self.last_event[root] = ts
        if prev is not None and ts - prev > self.stale_after:
            # a gap inside the stream: the period without data was stale
            self.fresh_since_problem[root] = 0
            self._set(root, "data_gap", ts)
        if self.problem.get(root):
            self.fresh_since_problem[root] = self.fresh_since_problem.get(root, 0) + 1
            if self.connected and self.fresh_since_problem[root] >= self.resume_after:
                self._set(root, "", ts)

    def check_stale(self, now: pd.Timestamp) -> None:
        for r in self.roots:
            last = self.last_event.get(r)
            if last is not None and now - last > self.stale_after and not self.problem.get(r):
                self.fresh_since_problem[r] = 0
                self._set(r, "stale", now)

    def flag(self, root: str, problem: str, now: pd.Timestamp) -> None:
        """An external data problem (e.g. a price level reset): recovery pending again."""
        self.fresh_since_problem[root] = 0
        self._set(root, problem, now)

    def healthy(self, root: str, now: pd.Timestamp) -> tuple[bool, str]:
        if not self.connected:
            return False, "feed_disconnected"
        last = self.last_event.get(root)
        if last is None:
            return False, "no_market_data"
        if now - last > self.stale_after:
            return False, "market_data_stale"
        p = self.problem.get(root)
        if p:
            return False, f"data_recovery_pending:{p}"
        return True, ""
