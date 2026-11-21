"""Market data events: trades and quotes (UTC timestamps).

``synthetic_from_bars`` marks events derived from OHLCV bars by the bar replay adapter: their
intrabar order and timing are an APPROXIMATION, never real ticks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd


class EventError(ValueError):
    """A market data event is malformed."""


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    root: str                      # MES / MNQ
    kind: str                      # "trade" | "quote"
    ts: pd.Timestamp               # exchange/feed timestamp, UTC
    last: float | None = None      # trade price
    size: float = 0.0              # trade size (contracts)
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    source: str = ""
    synthetic_from_bars: bool = False
    bar_high: float | None = None   # synthetic events only: the source bar's range (for worst-case rules)
    bar_low: float | None = None
    received_at: pd.Timestamp | None = field(default=None, compare=False)

    @property
    def is_trade(self) -> bool:
        return self.kind == "trade"

    def ref(self) -> dict[str, Any]:
        """Compact reference for the audit journal."""
        return {"event_id": self.event_id, "root": self.root, "kind": self.kind, "ts": self.ts.isoformat()}


def _num(v: Any, name: str, *, allow_none: bool = True) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v == "":
        if allow_none:
            return None
        raise EventError(f"{name} missing")
    if isinstance(v, bool):
        raise EventError(f"{name} must be a number")
    try:
        x = float(v)
    except (TypeError, ValueError) as exc:
        raise EventError(f"{name} must be a number, got {v!r}") from exc
    if not math.isfinite(x):
        raise EventError(f"{name} must be finite")
    return x


def event_from_mapping(raw: Mapping[str, Any], *, source: str = "") -> MarketEvent:
    """Parse an untrusted event record (strict; raises EventError)."""
    try:
        ts = pd.Timestamp(raw["ts"])
    except (KeyError, ValueError, TypeError) as exc:
        raise EventError(f"missing or malformed timestamp: {raw.get('ts')!r}") from exc
    if ts is pd.NaT or pd.isna(ts):
        raise EventError("missing timestamp")
    if ts.tzinfo is None:
        raise EventError("timestamps must carry a timezone (UTC expected)")
    kind = str(raw.get("kind", "")).lower()
    if kind not in ("trade", "quote"):
        raise EventError(f"unknown event kind {raw.get('kind')!r}")
    root = str(raw.get("root", "")).upper()
    if not root:
        raise EventError("missing root")
    eid = str(raw.get("event_id") or "")
    if not eid:
        raise EventError("missing event_id")
    return MarketEvent(eid, root, kind, ts.tz_convert("UTC"), _num(raw.get("last"), "last"),
                       _num(raw.get("size"), "size") or 0.0, _num(raw.get("bid"), "bid"), _num(raw.get("ask"), "ask"),
                       _num(raw.get("bid_size"), "bid_size"), _num(raw.get("ask_size"), "ask_size"), source)
