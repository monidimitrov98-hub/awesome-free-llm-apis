"""Normalised calendar event model and strict parsing of untrusted event payloads.

Every record is ONE VERSION of one event as published by one source, with ``known_at``:
the time this version became available. Point-in-time queries (``store.as_of``) only see
versions with ``known_at <= now``, so later schedule changes, cancellations and actual
values never leak into earlier decisions.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd


class EventDataError(ValueError):
    """A calendar payload is malformed or unsafe."""


class Impact(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class EventStatus(StrEnum):
    SCHEDULED = "scheduled"
    CHANGED = "changed"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    RELEASED = "released"
    UNKNOWN = "unknown"


class Reliability(StrEnum):
    OFFICIAL = "official"
    LICENSED = "licensed"
    UNOFFICIAL = "unofficial"
    SYNTHETIC = "synthetic"


class Provenance(StrEnum):
    """Where a record came from. Never present manual or synthetic data as verified live data."""

    VERIFIED_LIVE = "verified_live"      # fetched from a live provider verified against its real endpoint
    UNVERIFIED_LIVE = "unverified_live"  # fetched from a live provider that was not verified (should not happen)
    MANUAL = "manual"                    # curated by hand (json_file provider)
    SYNTHETIC = "synthetic"              # invented test data
    UNKNOWN = "unknown"                  # historical record without provenance (treated as untrusted for operations)


RELIABILITY_RANK = {Reliability.OFFICIAL: 3, Reliability.LICENSED: 2, Reliability.UNOFFICIAL: 1,
                    Reliability.SYNTHETIC: 0}
NY = "America/New_York"
_TEXT = re.compile(r"[^\x20-\x7E -￿]")
_ID = re.compile(r"^[A-Za-z0-9_.:@/\-]{1,200}$")
MAX_TEXT = 300


def clean_text(value: Any, field: str, *, required: bool = False, limit: int = MAX_TEXT) -> str | None:
    """Untrusted text: strings only, control characters removed, bounded length. Never interpreted."""
    if value is None or value == "":
        if required:
            raise EventDataError(f"{field} is required")
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise EventDataError(f"{field} must be text, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise EventDataError(f"{field} must be finite")
    text = _TEXT.sub("", str(value)).strip()
    if required and not text:
        raise EventDataError(f"{field} is empty")
    return text[:limit] or None


def check_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise EventDataError(f"invalid timezone {name!r}") from exc
    return name


def parse_time(value: Any, field: str, tz: str = "UTC") -> pd.Timestamp:
    """ISO timestamp -> UTC. A naive time is interpreted in ``tz`` (DST-aware, ambiguous/nonexistent rejected)."""
    if isinstance(value, pd.Timestamp):
        ts = value
    elif isinstance(value, str) and 8 <= len(value) <= 40:
        try:
            ts = pd.Timestamp(value)
        except (ValueError, TypeError) as exc:
            raise EventDataError(f"{field}: malformed timestamp {value!r}") from exc
    else:
        raise EventDataError(f"{field}: malformed timestamp {value!r}")
    if ts is pd.NaT or pd.isna(ts):
        raise EventDataError(f"{field}: missing timestamp")
    if ts.tzinfo is None:
        try:
            ts = ts.tz_localize(check_timezone(tz), ambiguous="raise", nonexistent="raise")
        except Exception as exc:  # noqa: BLE001 - pytz/zoneinfo errors vary by version
            raise EventDataError(f"{field}: {value!r} does not exist or is ambiguous in {tz} (DST)") from exc
    ts = ts.tz_convert("UTC")
    if not (pd.Timestamp("1990-01-01", tz="UTC") <= ts <= pd.Timestamp("2100-01-01", tz="UTC")):
        raise EventDataError(f"{field}: {value!r} out of range")
    return ts


@dataclass(frozen=True)
class EventRecord:
    event_id: str                      # canonical id shared by every source of the same event
    name: str
    category: str                      # classified category or "unclassified"
    region: str
    scheduled_at: pd.Timestamp | None  # UTC; None when only the date (or nothing) is known
    event_date: date | None            # New York calendar date of the event
    timezone: str                      # timezone the source used
    status: EventStatus
    impact: Impact
    source: str
    source_event_id: str
    reliability: Reliability
    known_at: pd.Timestamp             # when THIS version became available (point-in-time key)
    updated_at: pd.Timestamp | None = None
    previous: str | None = None
    forecast: str | None = None
    actual: str | None = None
    provenance: Provenance = Provenance.UNKNOWN

    @property
    def time_known(self) -> bool:
        return self.scheduled_at is not None

    def masked(self, now: pd.Timestamp) -> "EventRecord":
        """The record as seen at ``now``: an actual value is never visible before the release time."""
        if self.actual is not None and (self.scheduled_at is None or now < self.scheduled_at):
            return replace(self, actual=None)
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("scheduled_at", "known_at", "updated_at"):
            d[k] = None if d[k] is None else d[k].isoformat()
        d["event_date"] = None if self.event_date is None else self.event_date.isoformat()
        for k in ("status", "impact", "reliability", "provenance"):
            d[k] = str(d[k])
        return d


def canonical_id(category: str, event_date: date | None, name: str) -> str:
    """Same event from different sources -> same id. Unclassified names drop the reference period
    ("..., November 2024", "(Advance Estimate)") so BEA and Census listings of a joint release match."""
    if category == "unclassified":
        core = re.sub(r"\(.*?\)", " ", name.lower())
        core = re.split(r",| for | -- ", core)[0]
        base = re.sub(r"[^a-z0-9]+", "_", core).strip("_")[:60]
    else:
        base = category
    return f"{base}:{event_date.isoformat() if event_date else 'undated'}"


def parse_record(raw: Mapping[str, Any], *, source: str, reliability: Reliability, known_at: pd.Timestamp,
                 classify, default_region: str = "US", provenance: Provenance | None = None) -> EventRecord:
    """Validate one untrusted event dict and normalise it. ``classify(name, region, category)``
    returns ``(category, impact)``. Raises :class:`EventDataError`."""
    if not isinstance(raw, Mapping):
        raise EventDataError("event must be an object")
    allowed = {"event_id", "name", "category", "region", "currency", "scheduled_at", "date", "timezone", "status",
               "impact", "source_event_id", "known_at", "updated_at", "previous", "forecast", "actual", "provenance"}
    extra = set(raw) - allowed
    if extra:
        raise EventDataError(f"unknown event fields {sorted(extra)}")
    name = clean_text(raw.get("name"), "name", required=True)
    region = (clean_text(raw.get("region") or raw.get("currency"), "region") or default_region).upper()
    if region == "USD":
        region = "US"
    tz = check_timezone(clean_text(raw.get("timezone"), "timezone") or NY)
    status_raw = clean_text(raw.get("status"), "status") or "scheduled"
    try:
        status = EventStatus(status_raw.lower())
    except ValueError as exc:
        raise EventDataError(f"unknown status {status_raw!r}") from exc
    scheduled = parse_time(raw["scheduled_at"], "scheduled_at", tz) if raw.get("scheduled_at") else None
    if scheduled is not None:
        ev_date = scheduled.tz_convert(NY).date()
    elif raw.get("date"):
        try:
            ev_date = date.fromisoformat(str(raw["date"]))
        except ValueError as exc:
            raise EventDataError(f"malformed date {raw['date']!r}") from exc
    else:
        ev_date = None
    if scheduled is None and ev_date is None and status not in (EventStatus.CANCELLED, EventStatus.POSTPONED):
        raise EventDataError(f"{name}: neither a time nor a date")
    k_at = parse_time(raw["known_at"], "known_at") if raw.get("known_at") else known_at
    if k_at > known_at:
        raise EventDataError(f"{name}: known_at {k_at} is after the time it was received ({known_at})")
    category, impact = classify(name, region, clean_text(raw.get("category"), "category"),
                                clean_text(raw.get("impact"), "impact"))
    sid = clean_text(raw.get("source_event_id"), "source_event_id") or ""
    if provenance is None:
        try:
            provenance = Provenance(raw.get("provenance") or "unknown")
        except ValueError as exc:
            raise EventDataError(f"unknown provenance {raw.get('provenance')!r}") from exc
    if reliability is Reliability.SYNTHETIC:
        provenance = Provenance.SYNTHETIC  # synthetic data can never be labelled anything else
    eid = clean_text(raw.get("event_id"), "event_id")
    if eid is not None and not _ID.match(eid):
        raise EventDataError(f"invalid event_id {eid!r}")
    rec = EventRecord(
        event_id=eid or canonical_id(category, ev_date, name), name=name, category=category, region=region,
        scheduled_at=scheduled, event_date=ev_date, timezone=tz, status=status, impact=impact, source=source,
        source_event_id=sid, reliability=reliability, known_at=k_at,
        updated_at=parse_time(raw["updated_at"], "updated_at") if raw.get("updated_at") else None,
        previous=clean_text(raw.get("previous"), "previous", limit=40),
        forecast=clean_text(raw.get("forecast"), "forecast", limit=40),
        actual=clean_text(raw.get("actual"), "actual", limit=40), provenance=provenance)
    if rec.actual is not None and (rec.scheduled_at is None or rec.known_at < rec.scheduled_at):
        raise EventDataError(f"{name}: an actual value cannot be known before the release time")
    return rec
