"""Blackout windows from the point-in-time calendar.

* scheduled event: ``[time - before, time + after]`` (per category, per instrument override)
* event with an unknown time (only its date known, or postponed without a new time):
  the whole New York calendar day (``unknown_time_policy = "block_day"``)
* unexpected announcement: ``[known_at, known_at + after]`` (it can only block from the
  moment it became known)
* cancelled events open no window; low-impact events open none unless configured
* overlapping windows are merged, keeping every contributing event
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.risk.news.calendar import MergedEvent
from src.risk.news.config import NewsConfig
from src.risk.news.event import NY, EventRecord, EventStatus, Impact


@dataclass
class Window:
    start: pd.Timestamp
    end: pd.Timestamp
    kind: str                     # "scheduled" | "unknown_time" | "unknown_impact" | "conflict" | "unexpected" | "emergency"
    events: list[EventRecord] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def impact(self) -> Impact:
        order = [Impact.HIGH, Impact.MEDIUM, Impact.UNKNOWN, Impact.LOW]
        imps = {e.impact for e in self.events}
        return next((i for i in order if i in imps), Impact.HIGH if self.kind == "emergency" else Impact.UNKNOWN)

    def contains(self, t: pd.Timestamp) -> bool:
        return self.start <= t < self.end

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "kind": self.kind,
                "impact": self.impact.value, "event_ids": [e.event_id for e in self.events],
                "event_names": [e.name for e in self.events], "sources": sorted({e.source for e in self.events}),
                "reasons": self.reasons}


def _day(d) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(d).tz_localize(NY)
    end = (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(NY)
    return start.tz_convert("UTC"), end.tz_convert("UTC")


def event_windows(merged: list[MergedEvent], cfg: NewsConfig, root: str) -> list[Window]:
    regions = set(cfg.regions_for(root))
    out: list[Window] = []
    for m in merged:
        conflict = bool(m.conflicts) and len(m.chosen) > 1
        for rec in m.chosen:
            if rec.region not in regions or rec.status is EventStatus.CANCELLED:
                continue
            # every relevant event blocks (an impact label can never remove a blackout); the window
            # is the most conservative over all categories that apply to the event
            before, after = pd.Timedelta(0), pd.Timedelta(0)
            for cat in (rec.category, *rec.alt_categories):
                b, a = cfg.window(root, cat)
                before, after = max(before, b), max(after, a)
            kind = "conflict" if conflict else ("unknown_impact" if rec.impact is Impact.UNKNOWN else "scheduled")
            if rec.category == "unexpected":
                out.append(Window(rec.known_at, rec.known_at + after, "unexpected", [rec], [f"unexpected: {rec.name}"]))
            elif rec.scheduled_at is not None and rec.status is not EventStatus.POSTPONED:
                out.append(Window(rec.scheduled_at - before, rec.scheduled_at + after, kind, [rec], m.conflicts[:]))
            elif rec.event_date is not None:  # time unknown (or postponed without a new time): never safe
                s, e = _day(rec.event_date)
                out.append(Window(s, e, "unknown_time", [rec], [f"{rec.name}: time unknown, whole day blocked"]))
    for w in cfg.emergency:
        out.append(Window(w.start, w.end, "emergency", [], [w.reason]))
    return merge_windows(out)


PRIORITY = ["emergency", "unexpected", "conflict", "unknown_time", "unknown_impact", "scheduled"]


def merge_windows(windows: list[Window]) -> list[Window]:
    merged: list[Window] = []
    for w in sorted(windows, key=lambda w: (w.start, w.end)):
        if merged and w.start <= merged[-1].end:
            cur = merged[-1]
            cur.end = max(cur.end, w.end)
            cur.events += w.events
            cur.reasons += [r for r in w.reasons if r not in cur.reasons]
            cur.kind = min(cur.kind, w.kind, key=PRIORITY.index)
        else:
            merged.append(Window(w.start, w.end, w.kind, list(w.events), list(w.reasons)))
    return merged
