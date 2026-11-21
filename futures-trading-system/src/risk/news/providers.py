"""Calendar providers (provider-independent; normalised to :class:`EventRecord`).

Verified against the real endpoints on 2026-11-21 (see docs/news_provider_verification.md):

* ``ics``          iCalendar over HTTPS -- BEA release calendar (``bea``). The BLS feed uses
                   the same format but refused automated access (bot policy) -> not verified.
* ``census_html``  Census economic-indicator list view (HTML table: indicator, date, time ET).
* ``fomc_html``    Federal Reserve FOMC meeting calendar (HTML; dates only, no times -> the
                   decision day is blocked as an event without a confirmed time).
* ``rss``          RSS 2.0 over HTTPS -- Federal Reserve monetary-policy press releases
                   (unexpected announcements, see ``unexpected.py``).
* ``json_file``    a local curated calendar (MANUAL data).

Every response is untrusted input: HTTPS only (redirects only to HTTPS on the same host, at
most 3), connection/read timeout, bounded size, UTF-8 only, per-provider rate limit,
credentials only from environment variables and never logged, XML DOCTYPE/ENTITY refused,
HTML parsed with the standard-library tokenizer (no scripts, no entity expansion beyond
HTML character references), bounded item counts. Nothing received is executed or treated
as an instruction.
"""

from __future__ import annotations

import html
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Callable

import pandas as pd

from src.config import PROJECT_ROOT
from src.risk.news.config import NewsConfig, ProviderConfig
from src.risk.news.event import NY, EventDataError, EventRecord, Provenance, clean_text, parse_record

MAX_EVENTS = 5000
MAX_LINE = 10_000
CONTACT_ENV = "FTS_HTTP_CONTACT"
BS = chr(92)  # backslash


class ProviderError(RuntimeError):
    """A provider could not deliver valid data (message never contains credentials)."""


@dataclass
class FetchResult:
    records: list[EventRecord]
    coverage_start: pd.Timestamp | None
    coverage_end: pd.Timestamp | None
    invalid: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)  # rss items (unexpected news)
    category_coverage: dict[str, dict[str, str]] | None = None  # manual calendar: per-category coverage


Transport = Callable[[str, dict[str, str], float, int], bytes]


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise ProviderError(f"refused redirect to {new.scheme}://{new.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def user_agent() -> str:
    contact = clean_text(os.environ.get(CONTACT_ENV), "contact", limit=120)
    return "futures-research-calendar/1.0" + (f" (contact: {contact})" if contact else "")


def https_get(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> bytes:
    if not url.startswith("https://"):
        raise ProviderError("only https:// URLs are allowed")
    opener = urllib.request.build_opener(_SafeRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), **headers})
    try:
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise ProviderError(f"HTTP {resp.status}")
            data = resp.read(max_bytes + 1)
    except ProviderError:
        raise
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"request failed: {type(exc).__name__}") from None
    if len(data) > max_bytes:
        raise ProviderError(f"response larger than {max_bytes} bytes")
    return data


def decode_utf8(body: bytes, max_bytes: int | None = None) -> str:
    if max_bytes is not None and len(body) > max_bytes:
        raise ProviderError(f"response larger than {max_bytes} bytes")
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise ProviderError("response is not valid UTF-8") from None


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

    @property
    def provenance(self) -> Provenance:
        if self.pc.type == "json_file":
            return Provenance.MANUAL
        return Provenance.VERIFIED_LIVE if self.pc.verified else Provenance.UNVERIFIED_LIVE

    def due(self, now: pd.Timestamp) -> bool:
        return self.last_request is None or now - self.last_request >= self.cfg.min_request_interval

    def _get(self, now: pd.Timestamp, url: str | None = None) -> bytes:
        secret = os.environ.get(self.pc.api_key_env) if self.pc.api_key_env else None
        if self.pc.api_key_env and not secret:
            raise ProviderError(f"credential environment variable {self.pc.api_key_env} is not set")
        url = url or self.pc.all_urls()[0]
        if secret and self.pc.api_key_param:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode({self.pc.api_key_param: secret})}"
        headers = {"Authorization": f"Bearer {secret}"} if secret and not self.pc.api_key_param else {}
        self.last_request = now
        try:
            body = self.transport(url, headers, self.cfg.request_timeout, self.cfg.max_response_bytes)
        except ProviderError as exc:
            raise ProviderError(_redact(str(exc), secret)) from None
        if len(body) > self.cfg.max_response_bytes:
            raise ProviderError(f"response larger than {self.cfg.max_response_bytes} bytes")
        return body

    def fetch(self, now: pd.Timestamp) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError

    def _record(self, raw: dict[str, Any], now: pd.Timestamp) -> EventRecord:
        return parse_record(raw, source=self.name, reliability=self.pc.reliability, known_at=now,
                            classify=self.classify, provenance=self.provenance)

    def _records(self, raws: list[dict[str, Any]], now: pd.Timestamp) -> tuple[list[EventRecord], list[str]]:
        if len(raws) > MAX_EVENTS:
            raise ProviderError(f"more than {MAX_EVENTS} events")
        recs, invalid = [], []
        for i, r in enumerate(raws):
            try:
                recs.append(self._record(r, now))
            except EventDataError as exc:
                invalid.append(f"event #{i + 1}: {exc}")
        return recs, invalid


class JsonFileProvider(Provider):
    def fetch(self, now: pd.Timestamp) -> FetchResult:
        import json

        from src.risk.news.event import parse_time

        p = PROJECT_ROOT / self.pc.path if not os.path.isabs(self.pc.path) else self.pc.path
        try:
            with open(p, "rb") as fh:
                doc = json.loads(decode_utf8(fh.read(self.cfg.max_response_bytes + 1), self.cfg.max_response_bytes))
        except (OSError, ValueError) as exc:
            raise ProviderError(f"cannot read {self.pc.path}: {type(exc).__name__}") from None
        if isinstance(doc, dict) and doc.get("schema") == "manual-calendar/1":
            from src.risk.news.manual import validate_manual

            rep = validate_manual(doc, self.cfg, now, self.classify)
            if not rep.valid:
                raise ProviderError(f"manual calendar invalid ({len(rep.errors)} errors): {rep.errors[0][:200]}")
            recs, invalid = self._records(rep.events, now)
            ends = [pd.Timestamp(v["through"]) for v in rep.coverage.values()]
            return FetchResult(recs, now, max(ends) if ends else None, invalid, category_coverage=rep.coverage)
        if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
            raise ProviderError("calendar file needs an 'events' list")
        recs, invalid = self._records(doc["events"], now)
        try:
            cs = parse_time(doc["coverage_start"], "coverage_start") if doc.get("coverage_start") else None
            ce = parse_time(doc["coverage_end"], "coverage_end") if doc.get("coverage_end") else None
        except EventDataError as exc:
            raise ProviderError(str(exc)) from None
        return FetchResult(recs, cs, ce, invalid)


# ---------------------------------------------------------------------- iCalendar
_ICS_DT = re.compile(r"^(\d{8})(T(\d{6})(Z)?)?$")


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if len(raw) > MAX_LINE:
            raise EventDataError("iCalendar line too long")
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
            if len(lines[-1]) > MAX_LINE:
                raise EventDataError("iCalendar line too long")
        else:
            lines.append(raw)
    return lines


def _ics_text(value: str) -> str:
    return (value.replace(BS + "n", " ").replace(BS + "N", " ").replace(BS + ",", ",").replace(BS + ";", ";")
            .replace(BS + BS, BS))


def parse_ics(text: str) -> list[dict[str, Any]]:
    """Minimal strict iCalendar (RFC 5545) VEVENT parser -> raw event dicts."""
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            if cur is not None:
                raise EventDataError("nested VEVENT")
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
                if len(events) > MAX_EVENTS:
                    raise EventDataError(f"more than {MAX_EVENTS} events")
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *params = head.split(";")
        cur[name.upper()] = (value.strip(), dict(p.split("=", 1) for p in params if "=" in p))
    if cur is not None:
        raise EventDataError("unterminated VEVENT")
    out = []
    for ev in events:
        if "SUMMARY" not in ev or "DTSTART" not in ev:
            raise EventDataError("VEVENT without SUMMARY or DTSTART")
        value, params = ev["DTSTART"]
        m = _ICS_DT.match(value)
        if not m:
            raise EventDataError(f"malformed DTSTART {value[:40]!r}")
        try:
            d = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        except ValueError as exc:
            raise EventDataError(f"invalid DTSTART date {value[:40]!r}") from exc
        raw: dict[str, Any] = {"name": _ics_text(ev["SUMMARY"][0]), "source_event_id": ev.get("UID", ("", {}))[0][:200]}
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
        seq = ev.get("SEQUENCE", ("0", {}))[0]
        if seq.isdigit() and int(seq) > 0 and "status" not in raw:
            raw["status"] = "changed"  # RFC 5545: SEQUENCE increments when the organizer revises the event
        if "LAST-MODIFIED" in ev:
            lm = ev["LAST-MODIFIED"][0]
            if re.match(r"^\d{8}T\d{6}Z$", lm):
                raw["updated_at"] = f"{lm[:4]}-{lm[4:6]}-{lm[6:8]}T{lm[9:11]}:{lm[11:13]}:{lm[13:15]}Z"
        out.append(raw)
    return out


class IcsProvider(Provider):
    def fetch(self, now: pd.Timestamp) -> FetchResult:
        text = decode_utf8(self._get(now))
        if "BEGIN:VCALENDAR" not in text[:2000]:
            raise ProviderError("not an iCalendar document")
        try:
            raws = parse_ics(text)
        except EventDataError as exc:
            raise ProviderError(f"malformed feed: {exc}") from None
        recs, invalid = self._records(raws, now)
        return FetchResult(recs, now, _horizon(recs, now), invalid)


def _horizon(recs: list[EventRecord], now: pd.Timestamp) -> pd.Timestamp | None:
    """A feed covers from now up to its last listed future event (nothing beyond is known)."""
    times = [r.scheduled_at if r.scheduled_at is not None else
             pd.Timestamp(r.event_date).tz_localize(NY).tz_convert("UTC")
             for r in recs if r.scheduled_at is not None or r.event_date is not None]
    future = [t for t in times if t >= now]
    return max(future) if future else None


# ---------------------------------------------------------------------- RSS
def parse_rss(body: bytes, max_items: int = 200, max_bytes: int = 2_000_000) -> list[dict[str, Any]]:
    text = decode_utf8(body, max_bytes)
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low or "<!element" in low or "<!attlist" in low:
        raise ProviderError("XML with DOCTYPE/ENTITY declarations is refused")
    decl = re.match(r"\s*<\?xml[^>]*encoding=[\"']([^\"']+)", text)
    if decl and decl.group(1).lower().replace("_", "-") not in ("utf-8", "utf8"):
        raise ProviderError(f"XML encoding {decl.group(1)!r} refused (UTF-8 only)")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise ProviderError(f"malformed XML: {exc}") from None
    if root.tag != "rss":
        raise ProviderError("not an RSS document")
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
        items = parse_rss(self._get(now), max_bytes=self.cfg.max_response_bytes)
        return FetchResult([], None, None, [], items)


# ---------------------------------------------------------------------- HTML (official pages)
class _TableText(HTMLParser):
    """Collects the text of table cells (row by row) of the table with a given id."""

    def __init__(self, table_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.table_id, self.depth, self.rows, self.row, self.cell = table_id, 0, [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            if self.depth or a.get("id") == self.table_id:
                self.depth += 1
        elif self.depth == 1 and tag == "tr":
            self.row = []
        elif self.depth == 1 and tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self.depth:
            self.depth -= 1
        elif self.depth == 1 and tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
            if len(self.rows) > MAX_EVENTS:
                raise ProviderError(f"more than {MAX_EVENTS} table rows")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def parse_census(text: str) -> list[dict[str, Any]]:
    """Census 'Economic Indicator Release Schedule: List View': Indicator | Release Date | Time | Period.
    Times are US Eastern (verified against the BEA feed's UTC times for the joint trade release)."""
    p = _TableText("calendar")
    p.feed(text)
    p.close()
    rows = [r for r in p.rows if r]
    if not rows or [c.lower() for c in rows[0][:3]] != ["indicator", "release date", "time"]:
        raise ProviderError("Census calendar table not found or its columns changed")
    out = []
    for r in rows[1:]:
        if len(r) < 3:
            continue
        name, d, t = r[0], r[1], r[2].upper().replace(".", "")
        low = d.lower()
        if any(w in low for w in ("suspend", "postpon", "tbd", "to be determined")):
            out.append({"name": name, "status": "postponed"})  # no date: recorded, opens no window
            continue
        if "cancel" in low:
            out.append({"name": name, "status": "cancelled"})
            continue
        try:
            day = datetime.strptime(d, "%B %d, %Y").date()
        except ValueError:
            raise EventDataError(f"Census: unreadable date {d[:40]!r}") from None
        raw: dict[str, Any] = {"name": name, "timezone": NY}
        try:
            tm = datetime.strptime(t, "%I:%M %p").time()
            raw["scheduled_at"] = f"{day.isoformat()}T{tm.strftime('%H:%M:%S')}"
        except ValueError:
            raw["date"] = day.isoformat()  # no usable time: the whole day is blocked
        out.append(raw)
    return out


class CensusHtmlProvider(Provider):
    def fetch(self, now: pd.Timestamp) -> FetchResult:
        raws: list[dict[str, Any]] = []
        for url in self.pc.all_urls():
            try:
                raws += parse_census(decode_utf8(self._get(now, url)))
            except EventDataError as exc:
                raise ProviderError(f"malformed Census calendar: {exc}") from None
        recs, invalid = self._records(raws, now)
        return FetchResult(recs, now, _horizon(recs, now), invalid)


_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_MEETING = re.compile(rf"\b({_MONTHS})(?:/({_MONTHS}))?\s+(\d{{1,2}})(?:\s*-\s*(?:({_MONTHS})\s+)?(\d{{1,2}}))?\*?(\s*\(unscheduled\)|\s*\(notation vote\))?(?!\d|,\s*\d{{4}})")


def parse_fomc(text: str) -> list[dict[str, Any]]:
    """FOMC meetings per year from the Fed's calendar page. The page gives DATES only: the
    decision day (last meeting day) becomes a decision and a press-conference event without a
    confirmed time (blocked for the whole day unless a timed source refines it)."""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", text)))
    plain = re.sub(r"\s+", " ", plain)
    parts = re.split(r"(\d{4}) FOMC Meetings", plain)
    out = []
    for i in range(1, len(parts) - 1, 2):
        year = int(parts[i])
        body = re.split(r"Note:|Back to Top|Last Update|\* Meeting associated", parts[i + 1][:20000])[0]
        body = re.sub(r"\(Released [^)]*\)", " ", body)  # minutes release dates are not meetings
        for m in _MEETING.finditer(body):
            if m.group(6) and "notation" in m.group(6):
                continue
            end_month = m.group(4) or m.group(2) or m.group(1)
            end_day = int(m.group(5) or m.group(3))
            try:
                d = datetime.strptime(f"{end_month} {end_day} {year}", "%B %d %Y").date()
            except ValueError:
                raise EventDataError(f"FOMC: invalid date {m.group(0)[:40]!r}") from None
            tag = " (unscheduled)" if m.group(6) else ""
            out.append({"name": f"FOMC rate decision{tag}", "date": d.isoformat(), "category": "fomc_decision"})
            out.append({"name": f"FOMC statement{tag}", "date": d.isoformat(), "category": "fomc_statement"})
            out.append({"name": f"FOMC press conference{tag}", "date": d.isoformat(),
                        "category": "fed_press_conference"})
    if not out:
        raise ProviderError("no FOMC meetings found (page layout changed?)")
    return out


class FomcHtmlProvider(Provider):
    def fetch(self, now: pd.Timestamp) -> FetchResult:
        try:
            raws = parse_fomc(decode_utf8(self._get(now)))
        except EventDataError as exc:
            raise ProviderError(f"malformed FOMC calendar: {exc}") from None
        recs, invalid = self._records(raws, now)
        return FetchResult(recs, now, _horizon(recs, now), invalid)


TYPES = {"json_file": JsonFileProvider, "ics": IcsProvider, "rss": RssProvider, "census_html": CensusHtmlProvider,
         "fomc_html": FomcHtmlProvider}


def build_providers(cfg: NewsConfig, classify, transport: Transport | None = None, *,
                    include_disabled: bool = False) -> list[Provider]:
    return [TYPES[pc.type](pc, cfg, classify, transport) for pc in cfg.providers.values()
            if pc.enabled or include_disabled]
