"""Manual economic calendar (``data/news/manual_calendar.json``, schema ``manual-calendar/1``).

The manual file covers what no verified live provider covers (BLS releases, jobless claims,
ISM, Fed Chair speeches, Treasury refunding, exact FOMC times). It is curated by hand from
OFFICIAL sources and is validated strictly:

* ``categories``: one entry per category the manual provider is configured to cover, with
  ``official_source`` (https URL on an official domain), ``status`` ("verified" or
  "unresolved"), ``verified_through`` (the date up to which the maintainer CONFIRMED that every
  event of this category is listed; ``null`` while unresolved) and ``last_verified`` (UTC time of
  the last check against the official source).
* ``events``: one entry per release with ``name``, ``category``, ``source``, ``timezone``,
  ``provenance`` ("manual"), ``last_verified`` and ``verification_status``:
  "verified" (exact date AND time, ``scheduled_at``), "date_only" (``date`` only -> the whole
  New York day is blocked) or "unresolved" (no confirmed date -> the category is NOT covered).

A category counts as covered only when it is "verified", recently re-verified, reaches far
enough ahead, and has no unresolved event. Nothing here ever turns an unknown into "safe":
an unresolved, stale or missing category keeps NewsGuard in CALENDAR_INCOMPLETE /
CALENDAR_STALE and new entries blocked.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from src.risk.news.config import NewsConfig
from src.risk.news.event import NY, EventDataError, Impact, check_timezone, parse_time

SCHEMA = "manual-calendar/1"
TOP_KEYS = {"schema", "note", "instructions", "categories", "events"}
CATEGORY_KEYS = {"official_source", "status", "verified_through", "last_verified", "notes"}
EVENT_KEYS = {"event_id", "name", "category", "scheduled_at", "date", "timezone", "source", "last_verified",
              "provenance", "verification_status", "notes", "previous", "forecast"}
EVENT_STATUSES = ("verified", "date_only", "unresolved")


@dataclass
class ManualReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    categories: dict[str, dict[str, Any]] = field(default_factory=dict)  # per-category verdict
    events: list[dict[str, Any]] = field(default_factory=list)          # raw events usable by NewsGuard
    coverage: dict[str, dict[str, str]] = field(default_factory=dict)   # category -> {through, verified_at}
    upcoming_high_impact: list[dict[str, Any]] = field(default_factory=list)
    not_recently_verified: list[str] = field(default_factory=list)
    horizon_end: str = ""

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        covered = [c for c, v in self.categories.items() if v.get("covered")]
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings,
                "operational_horizon_end": self.horizon_end,
                "covers_operational_horizon": bool(self.categories) and len(covered) == len(self.categories),
                "categories_covered": covered,
                "categories_not_covered": [c for c in self.categories if c not in covered],
                "categories": self.categories, "coverage": self.coverage, "events_usable": len(self.events),
                "events_not_recently_verified": self.not_recently_verified,
                "upcoming_high_impact": self.upcoming_high_impact}


def official_url(url: Any, cfg: NewsConfig) -> bool:
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 500:
        return False
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in cfg.official_domains)


def manual_categories(cfg: NewsConfig) -> tuple[str, ...]:
    """Categories the manual file must describe: everything the manual provider covers."""
    pcs = [p for p in cfg.providers.values() if p.type == "json_file" and p.enabled]
    return tuple(dict.fromkeys(c for p in pcs for c in p.covers))


def _day_end(d: date) -> pd.Timestamp:
    return (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")


def validate_manual(doc: Any, cfg: NewsConfig, now: pd.Timestamp, classify) -> ManualReport:
    rep = ManualReport()
    err, warn = rep.errors.append, rep.warnings.append
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        err(f"not a manual calendar: expected schema {SCHEMA!r}")
        return rep
    if set(doc) - TOP_KEYS:
        err(f"unknown top-level keys {sorted(set(doc) - TOP_KEYS)}")
    cats = doc.get("categories")
    events = doc.get("events")
    if not isinstance(cats, dict) or not isinstance(events, list):
        err("'categories' must be an object and 'events' a list")
        return rep
    horizon_end = now + cfg.manual_horizon
    rep.horizon_end = horizon_end.isoformat()
    required = manual_categories(cfg)
    # ---------------------------------------------------------------- categories
    for c in required:
        if c not in cats:
            err(f"missing event category {c!r}")
    for c, entry in cats.items():
        verdict: dict[str, Any] = {"status": "unresolved", "covered": False, "reasons": []}
        rep.categories[c] = verdict
        if c not in cfg.events:
            err(f"category {c!r}: unknown category")
            continue
        if not isinstance(entry, dict) or set(entry) - CATEGORY_KEYS:
            err(f"category {c!r}: must be an object with keys {sorted(CATEGORY_KEYS)}")
            continue
        if not official_url(entry.get("official_source"), cfg):
            err(f"category {c!r}: official_source must be an https URL on an official domain "
                f"({', '.join(cfg.official_domains)})")
        status = entry.get("status")
        if status not in ("verified", "unresolved"):
            err(f"category {c!r}: status must be 'verified' or 'unresolved'")
            continue
        verdict["status"] = status
        if status == "unresolved":
            if entry.get("verified_through") is not None:
                err(f"category {c!r}: an unresolved category cannot have verified_through")
            verdict["reasons"].append("unresolved: no verified schedule")
            continue
        try:
            through = date.fromisoformat(str(entry.get("verified_through")))
            checked = parse_time(entry.get("last_verified"), f"{c} last_verified")
        except (ValueError, EventDataError) as exc:
            err(f"category {c!r}: verified needs verified_through (YYYY-MM-DD) and last_verified (UTC) ({exc})")
            continue
        if checked > now:
            err(f"category {c!r}: last_verified {checked} is in the future")
            continue
        verdict.update(verified_through=through.isoformat(), last_verified=checked.isoformat())
        if now - checked > cfg.manual_max_age:
            verdict["reasons"].append(f"stale: last verified {(now - checked).days} days ago "
                                      f"(max {cfg.manual_max_age.days})")
        if _day_end(through) < horizon_end:
            verdict["reasons"].append(f"short horizon: verified through {through}, required through "
                                      f"{horizon_end.tz_convert(NY).date()}")
        rep.coverage[c] = {"through": _day_end(through).isoformat(), "verified_at": checked.isoformat()}
    # ---------------------------------------------------------------- events
    seen_ids: set[str] = set()
    seen_keys: set[tuple] = set()
    for i, e in enumerate(events):
        where = f"event #{i + 1}"
        if not isinstance(e, dict):
            err(f"{where}: must be an object")
            continue
        where = f"event #{i + 1} ({str(e.get('name', '?'))[:60]})"
        if set(e) - EVENT_KEYS:
            err(f"{where}: unknown fields {sorted(set(e) - EVENT_KEYS)}")
            continue
        c = e.get("category")
        if c not in cats:
            err(f"{where}: category {c!r} is not declared in 'categories'")
            continue
        if not isinstance(e.get("name"), str) or not e["name"].strip():
            err(f"{where}: name is required")
            continue
        if e.get("provenance") != "manual":
            err(f"{where}: provenance must be 'manual'")
        if not official_url(e.get("source"), cfg):
            err(f"{where}: unknown or unofficial source {e.get('source')!r}")
        try:
            check_timezone(str(e.get("timezone") or ""))
        except EventDataError:
            err(f"{where}: invalid timezone {e.get('timezone')!r}")
            continue
        vs = e.get("verification_status")
        if vs not in EVENT_STATUSES:
            err(f"{where}: verification_status must be one of {list(EVENT_STATUSES)}")
            continue
        if vs != "unresolved":
            try:
                lv = parse_time(e.get("last_verified"), "last_verified")
                if lv > now:
                    err(f"{where}: last_verified is in the future")
                elif now - lv > cfg.manual_max_age:
                    rep.not_recently_verified.append(str(e.get("name"))[:80])
            except EventDataError:
                err(f"{where}: last_verified (UTC) is required for a {vs} event")
        if e.get("event_id") is not None:
            if e["event_id"] in seen_ids:
                err(f"{where}: duplicate event_id {e['event_id']!r}")
            seen_ids.add(e["event_id"])
        if vs == "unresolved":
            if e.get("scheduled_at") or e.get("date"):
                err(f"{where}: an unresolved event must not carry a date or time")
            rep.categories[c]["covered"] = False
            rep.categories[c]["reasons"].append(f"unresolved event: {e['name'][:60]}")
            rep.coverage.pop(c, None)
            continue
        if vs == "verified" and not e.get("scheduled_at"):
            err(f"{where}: missing timestamp (a verified event needs scheduled_at)")
            continue
        if vs == "date_only" and (not e.get("date") or e.get("scheduled_at")):
            err(f"{where}: missing date (a date_only event needs date and no scheduled_at)")
            continue
        try:
            when = parse_time(e["scheduled_at"], "scheduled_at", e["timezone"]) if vs == "verified" else None
            day = when.tz_convert(NY).date() if when is not None else date.fromisoformat(str(e["date"]))
        except (EventDataError, ValueError) as exc:
            err(f"{where}: {exc}")
            continue
        key = (c, when if when is not None else day)
        if key in seen_keys:
            err(f"{where}: duplicate event ({c} at {key[1]})")
            continue
        seen_keys.add(key)
        raw = {"name": e["name"], "category": c, "timezone": e["timezone"], "provenance": "manual"}
        if e.get("event_id"):
            raw["event_id"] = e["event_id"]
        raw.update({"scheduled_at": e["scheduled_at"]} if when is not None else {"date": day.isoformat()})
        rep.events.append(raw)
        t = when if when is not None else pd.Timestamp(day).tz_localize(NY).tz_convert("UTC")
        _, impact = classify(e["name"], "US", c, None)
        if now <= t <= horizon_end and impact is Impact.HIGH:
            rep.upcoming_high_impact.append({"name": e["name"], "category": c, "time": t.isoformat(),
                                             "time_ny": t.tz_convert(NY).strftime("%Y-%m-%d %H:%M %Z"),
                                             "time_known": when is not None})
    for c, v in rep.categories.items():
        if v["status"] == "verified" and c in rep.coverage and not v["reasons"]:
            v["covered"] = True
    if rep.not_recently_verified:
        warn(f"{len(rep.not_recently_verified)} events not verified within {cfg.manual_max_age.days} days")
    missing_events = [c for c, v in rep.categories.items() if v["status"] == "unresolved"]
    if missing_events:
        warn(f"unresolved categories (trading stays blocked if they are required): {missing_events}")
    rep.upcoming_high_impact.sort(key=lambda x: x["time"])
    return rep
