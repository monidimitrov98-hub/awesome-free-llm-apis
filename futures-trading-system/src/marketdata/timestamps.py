"""Timestamp provenance and conversion for provider data.

Conventions (all internal timestamps are tz-aware UTC ``pd.Timestamp``):

* ``exchange_ts`` -- stamped by the exchange. NinjaTrader 8 does not document one for
  ``MarketDataEventArgs``; it stays ``None`` and is NEVER substituted.
* ``provider_ts`` -- the provider's event time. NinjaTrader delivers ``MarketDataEventArgs.Time``
  as a local wall-clock time (``DateTimeKind.Unspecified``) in the time zone configured in
  NinjaTrader (Tools > Options > General); the bridge sends that local time, the Windows time-zone
  id and its own UTC conversion. Python re-converts with the IANA database and refuses events
  whose conversion disagrees, is ambiguous (DST fall-back hour) or does not exist (spring-forward).
* ``bridge_ts`` -- when the bridge handled the event (UTC, from the bridge machine's clock).
* ``received_at`` -- when this process read the message (UTC).
* ``processed_at`` -- when the runtime processed the event (UTC).

The event's canonical time ``MarketEvent.ts`` is the best available source, recorded in
``MarketEvent.ts_source`` ("exchange" or "provider").
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

# Windows time-zone ids (what .NET TimeZoneInfo.Id returns on Windows) -> IANA. Only zones with an
# unambiguous IANA equivalent are listed; an unknown id is refused at the bridge handshake.
WINDOWS_TO_IANA: dict[str, str] = {
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Arabian Standard Time": "Asia/Dubai",
    "India Standard Time": "Asia/Kolkata",
    "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
}

MAX_CONVERSION_DISAGREEMENT = timedelta(milliseconds=1)


class TimestampConversionError(ValueError):
    """A provider timestamp cannot be converted to UTC unambiguously."""


def iana_zone(windows_id: str) -> ZoneInfo:
    try:
        return ZoneInfo(WINDOWS_TO_IANA[windows_id])
    except KeyError:
        raise TimestampConversionError(f"unsupported time zone id {windows_id!r}") from None


_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?(Z?)$")


def parse_iso(text: str, *, utc_required: bool) -> datetime:
    """Strict ISO-8601 parsing ('YYYY-MM-DDTHH:MM:SS[.fffffff]' + 'Z' when ``utc_required``)."""
    m = _ISO.match(text) if isinstance(text, str) else None
    if m is None or bool(m.group(3)) != utc_required:
        raise TimestampConversionError("malformed timestamp" if m is None else
                                       ("UTC timestamp must end with 'Z'" if utc_required
                                        else "local timestamp must not carry an offset"))
    frac = (m.group(2) or "")[:6].ljust(6, "0")  # .NET writes up to 7 digits; Python keeps microseconds
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}.{frac}")
    except ValueError as exc:
        raise TimestampConversionError(f"malformed timestamp: {exc}") from None
    return dt.replace(tzinfo=timezone.utc) if utc_required else dt


def local_to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    """Convert a naive local wall time; ambiguous or nonexistent local times are refused."""
    a = local.replace(tzinfo=zone, fold=0)
    utc = a.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != local:
        raise TimestampConversionError("nonexistent local time (DST spring-forward gap)")
    if a.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
        raise TimestampConversionError("ambiguous local time (DST fall-back hour)")
    return utc


def provider_time_to_utc(t_local: str, t_utc: str, zone: ZoneInfo) -> pd.Timestamp:
    """The provider's time in UTC, verified: the bridge's own conversion must agree with ours."""
    ours = local_to_utc(parse_iso(t_local, utc_required=False), zone)
    theirs = parse_iso(t_utc, utc_required=True)
    if abs(ours - theirs) > MAX_CONVERSION_DISAGREEMENT:
        raise TimestampConversionError("bridge and backend disagree on the UTC time of the event")
    return pd.Timestamp(ours)


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")
