"""Builders for Sprint 4.1 news-filter tests (deterministic, no network)."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from src.risk.news.calendar import CalendarStore, RefreshRecord
from src.risk.news.classifier import make_classifier
from src.risk.news.config import load_news_config
from src.risk.news.event import Reliability, parse_record
from src.risk.news.guard import NewsGuard

CFG = load_news_config()
CLASSIFY = make_classifier(CFG)


def T(s: str) -> pd.Timestamp:
    """'2025-03-10 08:30' New York wall time -> UTC (or pass an ISO string with offset/Z)."""
    ts = pd.Timestamp(s)
    return (ts.tz_localize("America/New_York") if ts.tzinfo is None else ts).tz_convert("UTC")


def rec(name, when=None, *, known="2025-03-01 00:00", source="fixture", rel="official", **kw):
    raw = {"name": name, **kw}
    if when is not None:
        raw["scheduled_at"] = when
    return parse_record(raw, source=source, reliability=Reliability(rel), known_at=T(known), classify=CLASSIFY)


def store(*records, refreshes=None, expected=("fixture",), cfg=CFG, coverage=None) -> CalendarStore:
    st = CalendarStore(expected_providers=expected)
    # by default every expected provider is trusted to cover every category (tests of coverage override this)
    st.provider_coverage = coverage if coverage is not None else {p: list(cfg.events) for p in expected}
    for r in records:
        st.add(r)
    for r in (refreshes if refreshes is not None else daily(expected)):
        st.log_refresh(r)
    return st


def ok(t, provider="fixture", start=None, end="2025-12-31 00:00") -> RefreshRecord:
    return RefreshRecord(T(t), provider, True, T(start or t), T(end))


def daily(providers=("fixture",), start="2025-02-25", end="2025-04-05"):
    """A successful refresh every day at 00:00 New York time covering the next 30 days (fresh calendar)."""
    out = []
    for d in pd.date_range(start, end, freq="D"):
        for p in providers:
            out.append(RefreshRecord(T(f"{d.date()} 00:00"), p, True, T(f"{d.date()} 00:00"),
                                     T(f"{d.date()} 00:00") + pd.Timedelta(days=30)))
    return out


def fail(t, provider="fixture", error="timeout") -> RefreshRecord:
    return RefreshRecord(T(t), provider, False, error=error)


def guard(*records, refreshes=None, cfg=CFG, instrument="MES", **kw) -> NewsGuard:
    return NewsGuard(cfg, store(*records, refreshes=refreshes, **kw), instrument)


def cfg(**kw):
    return replace(CFG, **kw)
