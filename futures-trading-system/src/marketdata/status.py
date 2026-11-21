"""Read-only status snapshot for the GUI (Sprint 7). Plain JSON-compatible data; no network server.

Schema (version 1), produced by :func:`build_status` from a running PaperSession:

    schema_version, generated_at (UTC ISO), run_kind, mode, accepting_entries
    feed:        provider-specific connection state (provider, connected, authenticated, reconnections,
                 provider_connection, subscriptions, queue depth/capacity, rejected/violations, ...)
    market_session: {session_date | null, in_session: bool}
    instruments: per root -> subscription, health ("ok" or the blocking reason), entries_allowed,
                 last_event_time, freshness_seconds, bid, ask, spread, last, quote_time, last_time
    latency:     processing latency (received -> processed) p50/p99/max in ms, data delay
                 (event time -> received) p50/p99/max in ms, sample count
    validation:  rejected events by reason
    timestamps:  conventions in force (exchange time unavailable from NinjaTrader; provider time used)

Freshness is measured against the runtime clock; quotes are reported as received and are NOT
claimed to be tradeable prices.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

SCHEMA_VERSION = 1


def _pct(values, q: float) -> float | None:
    vals = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not vals:
        return None
    k = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return round(vals[k] * 1000.0, 3)


def _iso(ts) -> str | None:
    return ts.isoformat() if isinstance(ts, pd.Timestamp) else None


def build_status(session) -> dict[str, Any]:
    now = session.now()
    feed = session.feed.status()
    subs = feed.get("subscriptions", {})
    instruments = {}
    for r in session.roots:
        ok, why = session.health.healthy(r, now)
        tick = session.last_tick.get(r, {})
        last_ev = session.health.last_event.get(r)
        bid, ask = tick.get("bid"), tick.get("ask")
        instruments[r] = {
            "subscription": subs.get(r, "n/a"),
            "health": "ok" if ok else why,
            "entries_allowed_by_data": ok,
            "last_event_time": _iso(last_ev),
            "freshness_seconds": round((now - last_ev).total_seconds(), 3) if last_ev is not None else None,
            "bid": bid, "ask": ask,
            "spread": round(ask - bid, 10) if bid is not None and ask is not None else None,
            "last": tick.get("last"), "quote_time": _iso(tick.get("quote_time")), "last_time": _iso(tick.get("last_time")),
        }
    sd = session.session_of(now) if now.value > 0 else None
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "run_kind": session.run_kind, "mode": session.pcfg.mode,
        "accepting_entries": session.accepting_entries,
        "feed": feed,
        "market_session": {"session_date": str(sd.date()) if sd is not None else None, "in_session": sd is not None},
        "instruments": instruments,
        "latency": {"processing_ms_p50": _pct(session.latency, 0.5), "processing_ms_p99": _pct(session.latency, 0.99),
                    "processing_ms_max": _pct(session.latency, 1.0),
                    "data_delay_ms_p50": _pct(session.feed_delay, 0.5), "data_delay_ms_p99": _pct(session.feed_delay, 0.99),
                    "data_delay_ms_max": _pct(session.feed_delay, 1.0), "samples": len(session.latency)},
        "validation": {"rejected": dict(session.validator.rejected), "events_processed": session.seq},
        "timestamps": {"canonical": "provider time converted to UTC (exchange time is not available from "
                                    "NinjaTrader MarketDataEventArgs)", "all_times": "UTC ISO-8601"},
    }


def write_status_file(status: dict[str, Any], path: Path) -> None:
    """Atomic, owner-only JSON file (what a local GUI can poll; no listener is opened)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2, default=str, allow_nan=False)
    os.replace(tmp, path)
