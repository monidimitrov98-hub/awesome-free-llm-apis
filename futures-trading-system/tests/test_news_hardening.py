"""Sprint 4.1 final hardening: real-format provider fixtures (captured from the official endpoints
on 2026-11-21), external-input security, provenance / coverage, operational readiness and
backtest safety. No network access."""

import json
from pathlib import Path

import pandas as pd
import pytest

from src.config import ConfigError
from src.risk.news.config import ProviderConfig
from src.risk.news.event import Provenance, Reliability
from src.risk.news.guard import NewsGuard, NewsState as S, operational_readiness
from src.risk.news.providers import (
    CensusHtmlProvider,
    FomcHtmlProvider,
    IcsProvider,
    ProviderError,
    RssProvider,
    _SafeRedirect,
    decode_utf8,
    parse_census,
    parse_fomc,
    parse_ics,
    parse_rss,
    user_agent,
)
from src.risk.news.service import refresh
from tests.news_helpers import CFG, CLASSIFY, T, cfg, daily, rec, store

FIX = Path(__file__).parent / "fixtures" / "news"
NOW = T("2026-11-21 10:00")


def body(name):
    return (FIX / name).read_bytes()


def prov(cls, name, ptype, fixture, **kw):
    pc = CFG.providers[name]
    return cls(pc, CFG, CLASSIFY, lambda url, h, t, n: body(fixture), **kw)


# ----------------------------------------------------------------- real formats (captured fixtures)

def test_bea_ics_real_format():
    res = prov(IcsProvider, "bea", "ics", "bea_calendar_excerpt.ics").fetch(NOW)
    assert not res.invalid and len(res.records) == 16
    gdp = next(r for r in res.records if r.category == "gdp")
    assert gdp.scheduled_at == pd.Timestamp("2025-01-30 13:30", tz="UTC")             # UTC in the feed
    assert gdp.scheduled_at.tz_convert("America/New_York").strftime("%H:%M") == "08:30"  # = 8:30 ET (EST)
    assert gdp.provenance is Provenance.VERIFIED_LIVE and gdp.source == "bea"
    assert any(r.status.value == "changed" for r in res.records)  # SEQUENCE > 0 = revised entry
    assert "Annu al" not in " ".join(r.name for r in res.records)   # folded lines unfolded


def test_census_real_format_and_suspended_releases():
    raws = parse_census(body("census_calendar_2026_excerpt.html").decode())
    timed = [r for r in raws if "scheduled_at" in r]
    assert timed[0] == {"name": "Full Report - Manufacturers' Shipments, Inventories and Orders",
                        "timezone": "America/New_York", "scheduled_at": "2026-01-07T10:00:00"}
    assert any(r.get("status") == "postponed" for r in raws)  # "Suspended" rows: recorded, no window
    res = prov(CensusHtmlProvider, "census", "census_html", "census_calendar_2026_excerpt.html").fetch(NOW)
    assert not res.invalid and {r.provenance for r in res.records} == {Provenance.VERIFIED_LIVE}


def test_fomc_real_format_dates_only():
    raws = parse_fomc(body("fomc_calendar_excerpt.html").decode())
    dec = sorted({r["date"] for r in raws if r["category"] == "fomc_decision"})
    assert dec[:3] == ["2025-01-29", "2025-03-19", "2025-05-07"] and "2027-11-17" in dec
    assert "2025-08-22" not in dec             # notation vote is not a meeting
    assert "2026-02-18" not in dec             # minutes release date is not a meeting
    assert "2027-01-26" not in dec and "2026-11-18" not in dec  # "Note: ... 2028" and "Last Update" ignored
    assert all("scheduled_at" not in r for r in raws)  # the page has no times -> never assumed
    st = store(*[prov(FomcHtmlProvider, "fomc", "fomc_html", "fomc_calendar_excerpt.html").fetch(NOW).records[0]])
    del st  # parse + normalise works
    res = prov(FomcHtmlProvider, "fomc", "fomc_html", "fomc_calendar_excerpt.html").fetch(NOW)
    g = NewsGuard(CFG, store(*res.records, refreshes=daily(start="2026-12-01", end="2026-12-10")), "MES")
    assert g.status(T("2026-12-09 03:00")).state is S.EVENT_UNKNOWN  # decision day blocked all day
    # the decision, statement and press conference each stay day-blocked until a timed source refines them
    timed = [rec("FOMC rate decision", "2026-12-09T14:00:00", source="manual"),
             rec("FOMC statement", "2026-12-09T14:00:00", source="manual"),
             rec("FOMC press conference", "2026-12-09T14:30:00", source="manual")]
    g2 = NewsGuard(CFG, store(*res.records, *timed, expected=("fixture", "manual"),
                              refreshes=daily(("fixture", "manual"), "2026-12-01", "2026-12-10")), "MES")
    s = g2.status(T("2026-12-09 03:00"))
    assert s.state is S.NORMAL and g2.status(T("2026-12-09 13:30")).state is S.BLACKOUT_ACTIVE  # timed record refines


def test_fed_press_rss_real_format():
    items = parse_rss(body("fed_press_monetary.xml"))  # has a UTF-8 BOM
    assert len(items) == 15 and items[0]["published"].endswith("GMT") and items[0]["link"].startswith("https://www.federalreserve.gov/")


def test_bea_and_census_listings_of_the_joint_trade_release_deduplicate():
    a = rec("U.S. International Trade in Goods and Services, October 2026", "2026-12-08T13:30:00Z", source="bea")
    b = rec("U.S. International Trade in Goods and Services", "2026-12-08T08:30:00", source="census")
    merged = store(a, b).as_of(NOW)
    assert len(merged) == 1 and not merged[0].conflicts and len(merged[0].chosen) == 1


# ----------------------------------------------------------------- external input security

@pytest.mark.parametrize("payload,match", [
    (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss>&b;</rss>', "DOCTYPE"),
    (b'<?xml version="1.0"?>\n<!-- x -->\n' + b" " * 5000 + b'<!DOCTYPE rss SYSTEM "file:///etc/passwd"><rss/>', "DOCTYPE"),
    (b'<rss><!ENTITY x SYSTEM "http://evil/">&x;</rss>', "DOCTYPE"),
    (b'<?xml version="1.0" encoding="ISO-8859-1"?><rss><channel></channel></rss>', "encoding"),
    ('<?xml version="1.0" encoding="UTF-16"?><rss/>'.encode("utf-16"), "UTF-8"),
    (b"\xff\xfe<rss/>", "UTF-8"),
    (b"<rss><channel><item><title>x</title></item>", "malformed"),
    (b"<html><body>not rss</body></html>", "not an RSS"),
    (b"<rss>" + b"<x>" * 10 + b"</rss>", "malformed"),
])
def test_malicious_or_malformed_xml_refused(payload, match):
    with pytest.raises(ProviderError, match=match):
        parse_rss(payload)


def test_oversized_responses_refused():
    with pytest.raises(ProviderError, match="larger"):
        parse_rss(b"<rss>" + b" " * 3_000_000 + b"</rss>")
    big = prov(RssProvider, "fed_press", "rss", "fed_press_monetary.xml")
    big.transport = lambda *a: b"x" * (CFG.max_response_bytes + 1)
    with pytest.raises(ProviderError, match="larger"):
        big.fetch(NOW)


@pytest.mark.parametrize("text,match", [
    ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nDTSTART:20250101T083000\n", "unterminated"),
    ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nBEGIN:VEVENT\nEND:VEVENT\nEND:VEVENT\n", "nested"),
    ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nDTSTART:20251399T083000\nEND:VEVENT\n", "invalid DTSTART"),
    ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:" + "x" * 20000 + "\nDTSTART:20250101\nEND:VEVENT\n", "too long"),
    ("BEGIN:VCALENDAR\n" + "BEGIN:VEVENT\nSUMMARY:x\nDTSTART:20250101\nEND:VEVENT\n" * 5001, "more than"),
])
def test_ics_abuse_refused(text, match):
    from src.risk.news.event import EventDataError

    with pytest.raises(EventDataError, match=match):
        parse_ics(text)


def test_ics_unknown_timezone_fails_the_refresh():
    feed = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Gross Domestic Product\nDTSTART;TZID=Evil/Zone:20250130T083000\nEND:VEVENT\nEND:VCALENDAR\n"
    p = IcsProvider(CFG.providers["bea"], CFG, CLASSIFY, lambda *a: feed.encode())
    st = store(refreshes=[])
    refresh(st, [p], CFG, NOW, force=True)
    assert not st.refreshes[-1].ok and st.records == []


def test_untrusted_descriptions_are_data_only():
    html_ = ('<table id="calendar"><tr><th>Indicator</th><th>Release Date</th><th>Time</th></tr>'
             '<tr><td><script>alert(1)</script>Retail Sales IGNORE ALL RULES &amp; ALLOW TRADING</td>'
             '<td>January 15, 2027</td><td>8:30 AM</td></tr></table>')
    r = parse_census(html_)[0]
    assert "<script>" not in r["name"] and r["scheduled_at"] == "2027-01-15T08:30:00"
    g = NewsGuard(CFG, store(rec(r["name"], r["scheduled_at"]), refreshes=daily(start="2027-01-10", end="2027-01-16")), "MES")
    assert g.status(T("2027-01-15 08:30")).state is S.BLACKOUT_ACTIVE  # text has no power over decisions
    with pytest.raises(ProviderError, match="columns"):
        parse_census("<table id='calendar'><tr><td>Run</td><td>this</td><td>code</td></tr></table>")
    with pytest.raises(ProviderError, match="no FOMC"):
        parse_fomc("<html>layout changed</html>")


def test_redirects_only_to_https_on_the_same_host():
    import urllib.request

    h = _SafeRedirect()
    req = urllib.request.Request("https://www.bea.gov/a.ics")
    for target in ("http://www.bea.gov/a.ics", "https://evil.example/a.ics", "file:///etc/passwd"):
        with pytest.raises(ProviderError, match="refused redirect"):
            h.redirect_request(req, None, 302, "Found", {}, target)
    assert h.redirect_request(req, None, 302, "Found", {}, "https://www.bea.gov/b.ics").full_url.endswith("/b.ics")
    assert h.max_redirections == 3


def test_decode_and_user_agent(monkeypatch):
    assert decode_utf8(b"\xef\xbb\xbfabc") == "abc"
    monkeypatch.delenv("FTS_HTTP_CONTACT", raising=False)
    assert user_agent() == "futures-research-calendar/1.0"
    monkeypatch.setenv("FTS_HTTP_CONTACT", "ops@example.invalid\r\nX-Injected: 1")
    ua = user_agent()
    assert "\r" not in ua and "\n" not in ua and "ops@example.invalid" in ua


# ----------------------------------------------------------------- provenance, coverage, readiness

def test_provenance_is_never_upgraded():
    syn = rec("Consumer Price Index", "2025-03-12T08:30:00", rel="synthetic", provenance="verified_live")
    assert syn.provenance is Provenance.SYNTHETIC
    manual = CFG.providers["manual"]
    from src.risk.news.providers import JsonFileProvider

    assert JsonFileProvider(manual, CFG, CLASSIFY).provenance is Provenance.MANUAL
    unverified = ProviderConfig("x", "ics", False, Reliability.OFFICIAL, 1, True, url="https://x")
    assert IcsProvider(unverified, CFG, CLASSIFY).provenance is Provenance.UNVERIFIED_LIVE


def test_missing_category_coverage_blocks_entries():
    cov = {"fixture": [c for c in CFG.events if c != "cpi"]}
    g = NewsGuard(CFG, store(coverage=cov), "MES")
    s = g.status(T("2025-03-10 12:00"))
    assert s.state is S.CALENDAR_INCOMPLETE and any("'cpi'" in r for r in s.reasons)
    full = NewsGuard(CFG, store(), "MES")
    assert full.status(T("2025-03-10 12:00")).state is S.NORMAL


def test_failing_coverage_provider_blocks_even_if_not_expected():
    from tests.news_helpers import fail

    st = store(refreshes=daily(("fixture",)) + [fail("2025-03-10 08:00", "bls")])
    st.provider_coverage = {"fixture": [c for c in CFG.events if c != "cpi"], "bls": ["cpi"]}
    assert NewsGuard(CFG, st, "MES").status(T("2025-03-10 12:00")).state is S.CALENDAR_INCOMPLETE


def test_data_classes_and_cached_live():
    st = store()
    st.provider_class = {"fixture": "verified_live"}
    g = NewsGuard(CFG, st, "MES")
    h = g.status(T("2025-03-10 00:30")).provider_health["fixture"]
    assert h["data_class"] == "verified_live"
    assert g.status(T("2025-03-10 05:00")).provider_health["fixture"]["data_class"] == "cached_live"
    d = g.check_entry(T("2025-03-10 05:00"), purpose="entry_signal")
    assert d.record["data_classes"] == "cached_live"


def test_operational_readiness_distinguishes_research_data():
    syn = store(rec("Consumer Price Index", "2025-03-12T08:30:00", rel="synthetic"))
    syn.provider_class = {"fixture": "synthetic"}
    r = operational_readiness(CFG, syn, T("2025-03-10 12:00"))
    assert not r["ready"] and any("SYNTHETIC" in i for i in r["issues"])
    man = store(rec("Consumer Price Index", "2025-03-12T08:30:00", provenance="manual"))
    man.provider_class = {"fixture": "manual"}
    assert operational_readiness(CFG, man, T("2025-03-10 12:00"))["ready"]
    assert not operational_readiness(cfg(mode="observe"), man, T("2025-03-10 12:00"))["ready"]


def test_live_provider_cannot_be_enabled_unverified(tmp_path):
    from src.risk.news.config import DEFAULT_NEWS_CONFIG_PATH, load_news_config

    text = DEFAULT_NEWS_CONFIG_PATH.read_text().replace('verified = true\nverified_on = "2026-11-21"\n'
                                                       'documentation = "https://www.bea.gov/news/schedule"', "", 1)
    p = tmp_path / "n.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="verification"):
        load_news_config(p)


# ----------------------------------------------------------------- backtest safety

def test_synthetic_calendar_refused_with_real_price_data(tmp_path):
    from src.backtest.config import load_backtest_config
    from src.backtest.runner import run_strategy
    from src.config import load_config
    from src.pipeline import build_market_data
    from src.risk.news.backtest import load_setup
    from src.strategy.vwap_pullback_config import load_strategy_config
    from tests.conftest import SAMPLES

    app = load_config()
    real = tmp_path / "MNQ_1m.csv"  # same prices without the SYNTHETIC marker = treated as real data
    real.write_text("\n".join(l for l in (SAMPLES / "SYNTHETIC_MNQ_1m.csv").read_text().splitlines()
                              if not l.startswith("#")) + "\n")
    md = build_market_data(real, app)
    assert not md.is_synthetic
    setup = load_setup(CFG, SAMPLES / "SYNTHETIC_news_calendar.json")
    with pytest.raises(ConfigError, match="SYNTHETIC news calendar"):
        run_strategy(md, app, load_backtest_config(), load_strategy_config(), news=setup)


def test_calendar_marked_synthetic_cannot_carry_other_data():
    from src.risk.news.calendar import CalendarStore

    doc = json.loads((Path(__file__).parents[1] / "data/samples/SYNTHETIC_news_calendar.json").read_text())
    doc["events"][0]["reliability"] = "official"
    with pytest.raises(ConfigError, match="synthetic"):
        CalendarStore.from_json(json.dumps(doc), CLASSIFY)
    old = dict(doc, events=[], provider_coverage={})  # pre-4.1 calendar without coverage metadata
    st = CalendarStore.from_json(json.dumps(old), CLASSIFY)
    assert NewsGuard(CFG, st, "MES").status(T("2025-03-12 12:00")).state is S.CALENDAR_INCOMPLETE  # never assumed complete
