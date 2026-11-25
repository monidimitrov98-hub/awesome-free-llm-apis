"""CME equity index futures calendar (Sprint 12): sessions, closures, expirations, gap classification.

All session logic works in America/Chicago with tz-aware conversions (never a fixed UTC offset), so DST
transitions are handled by the timezone database. Stored timestamps are always UTC.

* ETH / Globex trading day ``D``: ``D-1 17:00`` to ``D 16:00`` CT; closed Friday 16:00 -> Sunday 17:00 and
  during the daily halt 16:00-17:00 CT (Mon-Thu).
* RTH: 08:30-15:00 CT on trade dates Monday-Friday (a research convention, not an exchange session).
* Expiry: quarterly contracts terminate at 08:30 CT on the third Friday of the contract month.
* Holidays: only VERIFIED entries of the calendar file are expected closures. Rule-based US holiday
  candidates are reported as HOLIDAY_CANDIDATE_UNVERIFIED.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import MONTH_CODES, PROJECT_ROOT, ConfigError

CALENDAR_FILE = PROJECT_ROOT / "config" / "calendars" / "cme_equity_index.toml"
_DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def _t(s: str) -> time:
    try:
        h, m = (int(x) for x in str(s).split(":"))
        return time(h, m)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"invalid time {s!r} (HH:MM)") from exc


@dataclass(frozen=True)
class Holiday:
    date: date
    kind: str                 # closed | early_close
    close: time | None
    source: str
    verification: str


@dataclass(frozen=True)
class Calendar:
    id: str
    version: str
    timezone: str
    eth_open: time
    eth_close: time
    rth_open: time
    rth_close: time
    halts: tuple[tuple[time, time, tuple[int, ...]], ...]
    termination: time
    months: str
    verification: str
    holidays: tuple[Holiday, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def identity(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "timezone": self.timezone, "verification": self.verification,
                "eth": [str(self.eth_open), str(self.eth_close)], "rth": [str(self.rth_open), str(self.rth_close)],
                "verified_holidays": sum(h.verification == "VERIFIED" for h in self.holidays)}

    # ------------------------------------------------------------------ sessions (vectorised)
    def local(self, ts: pd.DatetimeIndex | pd.Series) -> pd.DatetimeIndex:
        idx = pd.DatetimeIndex(ts)
        if idx.tz is None:
            raise ConfigError("timestamps must be tz-aware (UTC)")
        return idx.tz_convert(self.timezone)

    def trade_dates(self, ts) -> pd.Series:
        """Globex trade date of each bar open (NaT when the market is closed by the weekly / daily schedule)."""
        loc = self.local(ts)
        tod = loc.hour * 60 + loc.minute
        open_m = self.eth_open.hour * 60 + self.eth_open.minute
        d = pd.Series(loc.normalize().tz_localize(None), index=range(len(loc)))
        d = d.where(tod < open_m, d + pd.Timedelta(days=1))
        wd = d.dt.weekday
        out = d.where(wd < 5)                       # trade dates are Monday..Friday
        return out.where(self.is_open(ts).to_numpy())

    def is_open(self, ts) -> pd.Series:
        """Scheduled open (weekly schedule, daily halts, VERIFIED holiday closures / early closes)."""
        loc = self.local(ts)
        tod = loc.hour * 60 + loc.minute
        wd = loc.weekday
        o = self.eth_open.hour * 60 + self.eth_open.minute
        c = self.eth_close.hour * 60 + self.eth_close.minute
        is_open = np.ones(len(loc), dtype=bool)
        is_open &= ~(wd == 5)                                   # Saturday
        is_open &= ~((wd == 6) & (tod < o))                     # Sunday before the open
        is_open &= ~((wd == 4) & (tod >= c))                    # Friday after the close
        for s, e, days in self.halts:
            sm, em = s.hour * 60 + s.minute, e.hour * 60 + e.minute
            is_open &= ~(np.isin(wd, days) & (tod >= sm) & (tod < em))
        verified = [h for h in self.holidays if h.verification == "VERIFIED"]
        if verified:
            td = pd.Series(loc.normalize().tz_localize(None)).where(tod < o, pd.Series(loc.normalize().tz_localize(None)) + pd.Timedelta(days=1))
            for h in verified:
                on = (td.dt.date == h.date).to_numpy()
                if h.kind == "closed":
                    is_open &= ~on
                elif h.close is not None:
                    cm = h.close.hour * 60 + h.close.minute
                    is_open &= ~(on & (tod >= cm) & (tod < o))
        return pd.Series(is_open)

    def is_rth(self, ts) -> pd.Series:
        loc = self.local(ts)
        tod = loc.hour * 60 + loc.minute
        a = self.rth_open.hour * 60 + self.rth_open.minute
        b = self.rth_close.hour * 60 + self.rth_close.minute
        return pd.Series((loc.weekday < 5) & (tod >= a) & (tod < b)) & self.is_open(ts)

    # ------------------------------------------------------------------ expiry
    def expiry(self, contract_month: int, year: int) -> pd.Timestamp:
        """Termination of trading (UTC) of a quarterly contract: third Friday, ``termination`` CT."""
        first = date(year, contract_month, 1)
        third_friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        return pd.Timestamp.combine(third_friday, self.termination).tz_localize(self.timezone).tz_convert("UTC")

    # ------------------------------------------------------------------ holidays
    def holiday_candidates(self, years) -> dict[date, str]:
        out: dict[date, str] = {}
        for y in years:
            out.update(us_holiday_candidates(int(y)))
        return out


def load_calendar(path: Path | None = None) -> Calendar:
    p = Path(path or CALENDAR_FILE)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read calendar {p}: {exc}") from exc
    allowed = {"calendar", "eth", "rth", "weekend", "daily_halts", "expiry", "holidays"}
    if set(raw) - allowed:
        raise ConfigError(f"{p.name}: unknown section(s) {sorted(set(raw) - allowed)}")
    c = raw.get("calendar") or {}
    if not c.get("id") or not c.get("version"):
        raise ConfigError(f"{p.name}: [calendar] id and version are required")
    halts = []
    for h in raw.get("daily_halts", []):
        days = tuple(_DAYS[d] for d in h.get("weekdays", []))
        halts.append((_t(h["start"]), _t(h["end"]), days))
    hol = []
    for h in raw.get("holidays", []):
        if h.get("kind") not in ("closed", "early_close") or h.get("verification") not in ("VERIFIED", "UNVERIFIED"):
            raise ConfigError(f"{p.name}: holiday {h} needs kind closed|early_close and verification")
        if h["verification"] == "VERIFIED" and not str(h.get("source", "")).startswith("http"):
            raise ConfigError(f"{p.name}: a VERIFIED holiday needs a source URL")
        hol.append(Holiday(date.fromisoformat(str(h["date"])), h["kind"],
                           _t(h["close"]) if h.get("close") else None, str(h.get("source", "")), h["verification"]))
    ex = raw.get("expiry") or {}
    months = str(ex.get("months", "HMUZ"))
    if any(m not in MONTH_CODES for m in months):
        raise ConfigError(f"{p.name}: invalid expiry months")
    return Calendar(c["id"], str(c["version"]), c.get("timezone", "America/Chicago"), _t(raw["eth"]["open"]),
                    _t(raw["eth"]["close"]), _t(raw["rth"]["open"]), _t(raw["rth"]["close"]), tuple(halts),
                    _t(ex.get("termination", "08:30")), months, str(c.get("verification", "UNVERIFIED")), tuple(hol))


def _nth_weekday(y: int, m: int, wd: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(wd - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, wd: int) -> date:
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def us_holiday_candidates(y: int) -> dict[date, str]:
    """Rule-based US market holidays (CANDIDATES only: CME equity futures often trade with an early halt on
    these days rather than closing; the actual schedule must come from CME)."""
    out = {_observed(date(y, 1, 1)): "New Year's Day", _nth_weekday(y, 1, 0, 3): "Martin Luther King Jr. Day",
           _nth_weekday(y, 2, 0, 3): "Presidents' Day", _easter(y) - timedelta(days=2): "Good Friday",
           _last_weekday(y, 5, 0): "Memorial Day", _observed(date(y, 7, 4)): "Independence Day",
           _nth_weekday(y, 9, 0, 1): "Labor Day", _nth_weekday(y, 11, 3, 4): "Thanksgiving Day",
           _observed(date(y, 12, 25)): "Christmas Day"}
    if y >= 2022:
        out[_observed(date(y, 6, 19))] = "Juneteenth"
    return out


__all__ = ["Calendar", "load_calendar", "us_holiday_candidates", "CALENDAR_FILE"]
