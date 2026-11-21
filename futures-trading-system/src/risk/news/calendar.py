"""Point-in-time calendar store: every version of every event, plus the refresh log.

``as_of(now)`` returns the calendar exactly as it was known at ``now``: only versions with
``known_at <= now``; actual values masked until their release time; sources merged with a
trust policy (a lower-reliability source never overrides a more reliable one; disagreements
are recorded as conflicts). The refresh log (successful / failed provider updates with the
period they cover) drives the freshness and integrity checks of NewsGuard.

Persistence is plain JSON (no pickle). The same format is used for live stores and for
historical calendars in backtests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src.config import ConfigError
from src.risk.news.event import (
    RELIABILITY_RANK,
    EventDataError,
    EventRecord,
    EventStatus,
    Reliability,
    parse_record,
    parse_time,
)

SCHEMA = "news-calendar/1"


@dataclass(frozen=True)
class RefreshRecord:
    time: pd.Timestamp
    provider: str
    ok: bool
    coverage_start: pd.Timestamp | None = None
    coverage_end: pd.Timestamp | None = None
    error: str = ""
    n_events: int = 0
    # per-category coverage (manual calendar): category -> {"through": iso, "verified_at": iso}.
    # None = the provider's coverage window applies to every category it covers.
    category_coverage: dict[str, dict[str, str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"time": self.time.isoformat(), "provider": self.provider, "ok": self.ok,
                "category_coverage": self.category_coverage,
                "coverage_start": self.coverage_start.isoformat() if self.coverage_start is not None else None,
                "coverage_end": self.coverage_end.isoformat() if self.coverage_end is not None else None,
                "error": self.error, "n_events": self.n_events}


@dataclass
class MergedEvent:
    """One event as known at a point in time, after merging every source."""

    event_id: str
    chosen: list[EventRecord]               # versions whose times all produce windows (several = conflict, union)
    sources: list[EventRecord]              # latest version of every source
    conflicts: list[str] = field(default_factory=list)

    @property
    def primary(self) -> EventRecord:
        return self.chosen[0]


class CalendarStore:
    def __init__(self, *, expected_providers: Iterable[str] = (), synthetic: bool = False, note: str = "") -> None:
        self.records: list[EventRecord] = []
        self.refreshes: list[RefreshRecord] = []
        self.expected_providers = set(expected_providers)
        self.priorities: dict[str, int] = {}
        self.provider_coverage: dict[str, list[str]] = {}   # provider -> event categories it is trusted to cover
        self.provider_class: dict[str, str] = {}            # provider -> verified_live | manual | synthetic | ...
        self.synthetic = synthetic
        self.note = note
        self.rejected: list[dict[str, Any]] = []
        self._latest: dict[tuple[str, str], tuple] = {}

    # ------------------------------------------------------------------ writing
    def add(self, rec: EventRecord) -> bool:
        """Add a version; an unchanged re-publication of the latest version is not stored again."""
        k = (rec.event_id, rec.source)
        content = (rec.name, rec.category, rec.scheduled_at, rec.event_date, rec.status, rec.impact, rec.region,
                   rec.previous, rec.forecast, rec.actual)
        cur = self._latest.get(k)
        if cur is not None and cur[1] == content and cur[0] <= rec.known_at:
            return False
        if cur is None or cur[0] <= rec.known_at:
            self._latest[k] = (rec.known_at, content)
        self.records.append(rec)
        return True

    def log_refresh(self, r: RefreshRecord) -> None:
        self.refreshes.append(r)

    # ------------------------------------------------------------------ reading
    def refreshes_until(self, now: pd.Timestamp) -> list[RefreshRecord]:
        return [r for r in self.refreshes if r.time <= now]

    def as_of(self, now: pd.Timestamp, conflict_policy: str = "union") -> list[MergedEvent]:
        latest: dict[tuple[str, str], EventRecord] = {}
        for r in self.records:
            if r.known_at > now:
                continue
            k = (r.event_id, r.source)
            cur = latest.get(k)
            if cur is None or (r.known_at, r.updated_at or r.known_at) >= (cur.known_at, cur.updated_at or cur.known_at):
                latest[k] = r
        by_event: dict[str, list[EventRecord]] = {}
        for (eid, _), r in latest.items():
            by_event.setdefault(eid, []).append(r.masked(now))
        out = []
        for eid, recs in by_event.items():
            top = max(RELIABILITY_RANK[r.reliability] for r in recs)
            # timed versions first: a timed official record REFINES a date-only one of the same day
            best = sorted((r for r in recs if RELIABILITY_RANK[r.reliability] == top),
                          key=lambda r: (r.scheduled_at is None, self.priorities.get(r.source, 99), r.source))
            conflicts = []
            for r in recs:
                if RELIABILITY_RANK[r.reliability] < top and _differs(r, best[0]):
                    conflicts.append(f"lower-reliability source {r.source} disagrees ({_desc(r)} vs {_desc(best[0])}); "
                                     f"kept {best[0].source}")
            disagreeing = [r for r in best[1:] if _differs(r, best[0])]
            chosen = [best[0]]
            if disagreeing:
                conflicts.append("equally reliable sources disagree: " + "; ".join(f"{r.source}: {_desc(r)}"
                                                                                  for r in best))
                if conflict_policy == "union":
                    # conservative: every scheduled time blocks; a cancellation does not override a schedule
                    live = [r for r in best if r.status is not EventStatus.CANCELLED]
                    chosen = live or [best[0]]
            out.append(MergedEvent(eid, chosen, recs, conflicts))
        return out

    def data_classes(self) -> dict[str, int]:
        """Number of event versions per provenance (verified_live / manual / synthetic / unknown)."""
        return _data_class(self)

    @property
    def is_synthetic(self) -> bool:
        return self.synthetic or any(r.provenance.value == "synthetic" for r in self.records)

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> str:
        return json.dumps({"schema": SCHEMA, "synthetic": self.synthetic, "note": self.note,
                           "expected_providers": sorted(self.expected_providers),
                           "provider_priority": self.priorities, "provider_coverage": self.provider_coverage,
                           "provider_class": self.provider_class,
                           "refreshes": [r.to_dict() for r in self.refreshes],
                           "events": [dict(r.to_dict(), source=r.source) for r in self.records]}, indent=1)

    def save(self, path: str | Path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(p)
        return hashlib.sha256(p.read_bytes()).hexdigest()

    @classmethod
    def load(cls, path: str | Path, classify) -> "CalendarStore":
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read news calendar {p}: {exc}") from exc
        return cls.from_json(text, classify, str(p))

    @classmethod
    def from_json(cls, text: str, classify, where: str = "calendar") -> "CalendarStore":
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"{where}: not valid JSON ({exc})") from exc
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            raise ConfigError(f"{where}: expected schema {SCHEMA!r}")
        store = cls(expected_providers=doc.get("expected_providers", []), synthetic=bool(doc.get("synthetic")),
                    note=str(doc.get("note", ""))[:500])
        store.priorities = {str(k): int(v) for k, v in (doc.get("provider_priority") or {}).items()}
        store.provider_coverage = {str(k): [str(c) for c in v] for k, v in (doc.get("provider_coverage") or {}).items()}
        store.provider_class = {str(k): str(v) for k, v in (doc.get("provider_class") or {}).items()}
        try:
            for r in doc.get("refreshes", []):
                store.log_refresh(RefreshRecord(
                    parse_time(r["time"], "refresh time"), str(r["provider"]), bool(r["ok"]),
                    parse_time(r["coverage_start"], "coverage_start") if r.get("coverage_start") else None,
                    parse_time(r["coverage_end"], "coverage_end") if r.get("coverage_end") else None,
                    str(r.get("error", ""))[:300], int(r.get("n_events", 0)),
                    _category_coverage(r.get("category_coverage"))))
            for i, e in enumerate(doc.get("events", [])):
                e = dict(e)
                source = str(e.pop("source", ""))
                rel = Reliability(e.pop("reliability", "unofficial"))
                if store.synthetic and rel is not Reliability.SYNTHETIC:
                    raise ValueError("a calendar marked synthetic may only contain synthetic events")
                known = parse_time(e.get("known_at"), f"event #{i + 1} known_at")
                if "event_date" in e:
                    e["date"] = e.pop("event_date")
                if e.get("category") == "unclassified":
                    e.pop("category")
                rec = parse_record(e, source=source, reliability=rel, known_at=known, classify=classify)
                store.add(rec)
        except (EventDataError, KeyError, ValueError, TypeError) as exc:
            raise ConfigError(f"{where}: invalid calendar data: {exc}") from exc
        return store

    def coverage_report(self, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, Any]:
        """Which part of [start, end] no successful refresh covered (missing historical calendar data)."""
        spans = sorted((r.coverage_start, r.coverage_end) for r in self.refreshes
                       if r.ok and r.coverage_start is not None and r.coverage_end is not None)
        gaps, cursor = [], start
        for s, e in spans:
            if e <= cursor:
                continue
            if s > cursor:
                gaps.append((cursor, min(s, end)))
            cursor = max(cursor, e)
            if cursor >= end:
                break
        if cursor < end:
            gaps.append((cursor, end))
        return {"start": start.isoformat(), "end": end.isoformat(), "synthetic_calendar": self.synthetic,
                "data_classes": self.data_classes(),
                "events": len(self.records), "gaps": [[a.isoformat(), b.isoformat()] for a, b in gaps if b > a]}


def _category_coverage(raw: Any) -> dict[str, dict[str, str]] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("category_coverage must be an object")
    out = {}
    for c, v in raw.items():
        out[str(c)] = {"through": parse_time(v["through"], "through").isoformat(),
                       "verified_at": parse_time(v["verified_at"], "verified_at").isoformat()}
    return out


def _data_class(store: "CalendarStore") -> dict[str, int]:
    out: dict[str, int] = {}
    for r in store.records:
        out[r.provenance.value] = out.get(r.provenance.value, 0) + 1
    return out


def _differs(a: EventRecord, b: EventRecord) -> bool:
    """Disagreement about when (or whether) an event happens. A date-only record and a timed
    record of the SAME day agree (the timed one is more precise)."""
    if (a.status is EventStatus.CANCELLED) != (b.status is EventStatus.CANCELLED):
        return True
    if a.scheduled_at is None or b.scheduled_at is None:
        return a.event_date != b.event_date
    return a.scheduled_at != b.scheduled_at


def _desc(r: EventRecord) -> str:
    return f"{r.status.value} {r.scheduled_at.isoformat() if r.scheduled_at is not None else r.event_date}"
