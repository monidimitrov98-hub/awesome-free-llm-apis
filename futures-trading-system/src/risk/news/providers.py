"""Calendar providers (provider-independent; normalised to :class:`EventRecord`).

* ``json_file``  a local curated calendar (same JSON schema as the store) -- e.g. built
                 from the Federal Reserve FOMC calendar, BEA, Census and DOL schedules.
* ``ics``        an iCalendar feed over HTTPS (e.g. the official BLS release calendar).
* ``rss``        an RSS 2.0 feed over HTTPS for unexpected official announcements
                 (see ``unexpected.py``); it produces no scheduled events.

Network access: HTTPS only, connection/read timeout, bounded response size, per-provider
rate limit, credentials only from environment variables and never logged. Feed content is
untrusted data: it is parsed, validated and never executed or interpreted as instructions.
XML documents with a DOCTYPE/ENTITY declaration are rejected (no entity expansion).
"""

from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd

from src.risk.news.config import NewsConfig, ProviderConfig
from src.risk.news.event import NY, EventDataError, EventRecord, EventStatus, Reliability, clean_text, parse_record
from src.config import PROJECT_ROOT


class ProviderError(RuntimeError):
    """A provider could not deliver valid data (message never contains credentials)."""


@dataclass
class FetchResult:
    records: list[EventRecord]
    coverage_start: pd.Timestamp | None
    coverage_end: pd.Timestamp | None
    invalid: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)  # rss items (unexpected news)


Transport = Callable[[str, dict[str, str], float, int], bytes]


def https_get(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> bytes:
    if not url.startswith("https://"):
        raise ProviderError("only https:// URLs are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "futures-research-calendar/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https enforced above
            data = resp.read(max_bytes + 1)
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"request failed: {type(exc).__name__}") from None
    if len(data) > max_bytes:
        raise ProviderError(f"response larger than {max_bytes} bytes")
    return data


def _redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


class Provider:
    def __init__(self, pc: ProviderConfig, cfg: NewsConfig, classify, transport: Transport | None = None) -> None:
        self.pc = pc
        self.cfg = cfg
        self.classify = classify
        self.transport = transport or https_get
        self.last_request: pd.Timestamp | None = None

    @property
    def name(self) -> str:
        return self.pc.name

    def due(self, now: pd.Timestamp) -> bool:
        return self.last_request is None or now - self.last_request >= self.cfg.min_request_interval

    def _get(self, now: pd.Timestamp) -> bytes:
        secret = os.environ.get(self.pc.api_key_env) if self.pc.api_key_env else None
        if self.pc.api_key_env and not secret:
            raise ProviderError(f"credential environment variable {self.pc.api_key_env} is not set")
        url = self.pc.url
        if secret and self.pc.api_key_param:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode({self.pc.api_key_param: secret})}"
        headers = {"Authorization": f"Bearer {secret}"} if secret and not self.pc.api_key_param else {}
        self.last_request = now
        try:
            return self.transport(url, headers, self.cfg.request_timeout, self.cfg.max_response_bytes)
        except ProviderError as exc:
            raise ProviderError(_redact(str(exc), secret)) from None

    def fetch(self, now: pd.Timestamp) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError

    def _record(self, raw: dict[str, Any], now: pd.Timestamp) -> EventRecord:
        return parse_record(raw, source=self.name, reliability=self.pc.reliability, known_at=now,
                            classify=self.classify)


class JsonFileProvider(Provider):
    def fetch(self, now: pd.Timestamp) -> FetchResult:
        import json

        from src.risk.news.event import parse_time

        p = PROJECT_ROOT / self.pc.path if not os.path.isabs(self.pc.path) else self.pc.path
        try:
            doc = json.loads(open(p, encoding="utf-8").read(self.cfg.max_response_bytes + 1))
        except (OSError, ValueError) as exc:
            raise ProviderError(f"cannot read {self.pc.path}: {type(exc).__name__}") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
            raise ProviderError("calendar file needs an 'events' list")
        recs, invalid = [], []
        for i, e in enumerate(doc["events"]):
            try:
                recs.append(self._record(e, now))
            except EventDataError as exc:
                invalid.append(f"event #{i + 1}: {exc}")
        try:
            cs = parse_time(doc["coverage_start"], "coverage_start") if doc.get("coverage_start") else None
            ce = parse_time(doc["coverage_end"], "coverage_end") if doc.get("coverage_end") else None
        except EventDataError as exc:
            raise ProviderError(str(exc)) from None
        return FetchResult(recs, cs, ce, invalid)


_ICS_DT = re.compile(r"^(\d{8})(T(\d{6})(Z)?)?$")


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_ics(text: str) -> list[dict[str, Any]]:
    """Minimal strict iCalendar (RFC 5545) VEVENT parser -> raw event dicts."""
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *params = head.split(";")
        cur[name.upper()] = (value.strip(), dict(p.split("=", 1) for p in params if "=" in p))
    out = []
    for ev in events:
        if "SUMMARY" not in ev or "DTSTART" not in ev:
            raise EventDataError("VEVENT without SUMMARY or DTSTART")
        value, params = ev["DTSTART"]
        m = _ICS_DT.match(value)
        if not m:
            raise EventDataError(f"malformed DTSTART {value!r}")
        d = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        raw: dict[str, Any] = {"name": ev["SUMMARY"][0].replace("\\,", ",").replace("\\;", ";"),
                               "source_event_id": ev.get("UID", ("", {}))[0][:200]}
        if m.group(2) is None or params.get("VALUE") == "DATE":
            raw["date"] = d.isoformat()  # all-day entry: time unknown
        else:
            t = m.group(3)
            iso = f"{d.isoformat()}T{t[:2]}:{t[2:4]}:{t[4:6]}"
            if m.group(4):
                raw["scheduled_at"] = iso + "Z"
            else:
                raw["scheduled_at"], raw["timezone"] = iso, params.get("TZID", NY).strip('"')
        status = ev.get("STATUS", ("", {}))[0].upper()
        if status == "CANCELLED":
            raw["status"] = "cancelled"
        if "LAST-MODIFIED" in ev:
            lm = ev["LAST-MODIFIED"][0]
            if re.match(r"^\d{8}T\d{6}Z$", lm):
                raw["updated_at"] = f"{lm[:4]}-{lm[4:6]}-{lm[6:8]}T{lm[9:11]}:{lm[11:13]}:{lm[13:15]}Z"
        out.append(raw)
    return out


class IcsProvider(Provider):
    def fetch(self, now: pd.Timestamp) -> FetchResult:
        body = self._get(now)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError("feed is not UTF-8") from None
        if "BEGIN:VCALENDAR" not in text[:2000]:
            raise ProviderError("not an iCalendar document")
        try:
            raws = parse_ics(text)
        except EventDataError as exc:
            raise ProviderError(f"malformed feed: {exc}") from None
        recs, invalid = [], []
        for i, r in enumerate(raws):
            try:
                recs.append(self._record(r, now))
            except EventDataError as exc:
                invalid.append(f"event #{i + 1}: {exc}")
        times = [r.scheduled_at for r in recs if r.scheduled_at is not None and r.scheduled_at >= now]
        # the feed covers from now up to its last listed future event
        return FetchResult(recs, now, max(times) if times else None, invalid)


def parse_rss(body: bytes, max_items: int = 200) -> list[dict[str, Any]]:
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        raise ProviderError("XML with DOCTYPE/ENTITY declarations is refused")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ProviderError(f"malformed XML: {exc}") from None
    items = []
    for it in root.iter("item"):
        items.append({"title": clean_text(it.findtext("title"), "title") or "",
                      "link": clean_text(it.findtext("link"), "link", limit=500) or "",
                      "published": clean_text(it.findtext("pubDate"), "pubDate") or "",
                      "description": clean_text(it.findtext("description"), "description", limit=1000) or ""})
        if len(items) >= max_items:
            break
    return items


class RssProvider(Provider):
    def fetch(self, now: pd.Timestamp) -> FetchResult:
        items = parse_rss(self._get(now))
        return FetchResult([], None, None, [], items)


TYPES = {"json_file": JsonFileProvider, "ics": IcsProvider, "rss": RssProvider}


def build_providers(cfg: NewsConfig, classify, transport: Transport | None = None) -> list[Provider]:
    return [TYPES[pc.type](pc, cfg, classify, transport) for pc in cfg.providers.values() if pc.enabled]


__all__ = ["Provider", "ProviderError", "FetchResult", "build_providers", "parse_ics", "parse_rss", "https_get",
           "EventStatus", "Reliability"]
