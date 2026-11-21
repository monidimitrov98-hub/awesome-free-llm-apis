"""Sprint 4.1: blackout windows, NewsGuard states (fail closed), unexpected news."""

from dataclasses import replace

import pandas as pd
import pytest

from src.risk.news.blackout import event_windows, merge_windows, Window
from src.risk.news.config import EmergencyWindow
from src.risk.news.guard import NewsState as S
from src.risk.news.unexpected import combine, items_to_events, validate_llm_assessment
from tests.news_helpers import CFG, T, cfg, daily, fail, guard, ok, rec, store

CPI = rec("Consumer Price Index", "2025-03-12T08:30:00")


def state(g, t):
    return g.status(T(t)).state


# ----------------------------------------------------------------- windows

@pytest.mark.parametrize("t,expected", [("2025-03-12 07:59", S.NORMAL), ("2025-03-12 08:00", S.BLACKOUT_ACTIVE),
                                        ("2025-03-12 08:30", S.BLACKOUT_ACTIVE), ("2025-03-12 08:59", S.BLACKOUT_ACTIVE),
                                        ("2025-03-12 09:00", S.NORMAL)])
def test_cpi_window_before_after(t, expected):
    assert state(guard(CPI), t) is expected


def test_overlapping_windows_merge():
    ws = event_windows(store(CPI, rec("Producer Price Index", "2025-03-12T09:10:00"),
                             rec("Unemployment Insurance Weekly Claims", "2025-03-12T08:30:00")).as_of(T("2025-03-11 00:00")),
                       CFG, "MES")
    assert len(ws) == 1 and ws[0].start == T("2025-03-12 08:00") and ws[0].end == T("2025-03-12 09:25")
    assert {e.category for e in ws[0].events} == {"cpi", "ppi", "jobless_claims"}
    a = Window(T("2025-03-12 08:00"), T("2025-03-12 09:00"), "scheduled")
    b = Window(T("2025-03-12 09:00"), T("2025-03-12 09:30"), "emergency")
    m = merge_windows([b, a])
    assert len(m) == 1 and m[0].kind == "emergency" and m[0].end == T("2025-03-12 09:30")


def test_fomc_decision_and_press_conference_are_separate_events():
    dec = rec("FOMC rate decision", "2025-03-19T14:00:00")
    press = rec("FOMC press conference", "2025-03-19T14:30:00")
    m = store(dec, press).as_of(T("2025-03-18 00:00"))
    assert {x.event_id for x in m} == {"fomc_decision:2025-03-19", "fed_press_conference:2025-03-19"}
    ws = event_windows(m, CFG, "MNQ")
    assert len(ws) == 1 and ws[0].start == T("2025-03-19 13:00") and ws[0].end == T("2025-03-19 15:00")
    late_press = rec("FOMC press conference", "2025-03-19T16:30:00")
    ws2 = event_windows(store(dec, late_press).as_of(T("2025-03-18 00:00")), CFG, "MNQ")
    assert [(w.start, w.end) for w in ws2] == [(T("2025-03-19 13:00"), T("2025-03-19 15:00")),
                                               (T("2025-03-19 16:00"), T("2025-03-19 17:00"))]


def test_mes_mnq_rules_and_overrides():
    for root in ("MES", "MNQ"):
        assert state(guard(CPI, instrument=root), "2025-03-12 08:10") is S.BLACKOUT_ACTIVE
    insts = dict(CFG.instruments)
    insts["MNQ"] = {"regions": ["US"], "overrides": {"cpi": (pd.Timedelta(minutes=60), pd.Timedelta(minutes=60))}}
    c = cfg(instruments=insts)
    assert state(guard(CPI, cfg=c, instrument="MNQ"), "2025-03-12 07:45") is S.BLACKOUT_ACTIVE
    assert state(guard(CPI, cfg=c, instrument="MES"), "2025-03-12 07:45") is S.NORMAL
    euro = rec("ECB rate decision", "2025-03-12T08:30:00", region="EU")
    assert state(guard(euro), "2025-03-12 08:30") is S.NORMAL  # region not relevant to MES/MNQ


def test_changed_time_moves_window_without_look_ahead():
    first = rec("Retail Sales", "2025-03-17T08:30:00")
    moved = rec("Retail Sales", "2025-03-17T10:50:00", known="2025-03-12 11:00", status="changed")
    g = guard(first, moved)
    assert state(g, "2025-03-17 08:30") is S.NORMAL and state(g, "2025-03-17 10:40") is S.BLACKOUT_ACTIVE
    # before the change was known, the OLD window applied (the future revision is invisible)
    g2 = guard(first, moved)
    assert g2.windows_at(T("2025-03-12 10:59"))[0].start == T("2025-03-17 08:15")


def test_cancelled_event_stops_blocking_only_once_known():
    gdp = rec("Gross Domestic Product", "2025-03-14T08:30:00")
    cancel = rec("Gross Domestic Product", "2025-03-14T08:30:00", known="2025-03-14 08:20", status="cancelled")
    g = guard(gdp, cancel)
    assert state(g, "2025-03-14 08:19") is S.BLACKOUT_ACTIVE
    assert state(g, "2025-03-14 08:25") is S.NORMAL


def test_unknown_impact_and_unknown_time_are_never_safe():
    g = guard(rec("Wholesale Inventories", "2025-03-12T10:00:00"))
    assert state(g, "2025-03-12 09:45") is S.EVENT_UNKNOWN
    day = guard(rec("Fed Chair speech", date="2025-03-11"))
    assert state(day, "2025-03-11 00:00") is S.EVENT_UNKNOWN and state(day, "2025-03-11 23:59") is S.EVENT_UNKNOWN
    assert state(day, "2025-03-12 00:00") is S.NORMAL
    postponed = guard(rec("Consumer Price Index", date="2025-03-12", status="postponed"))
    assert state(postponed, "2025-03-12 12:00") is S.EVENT_UNKNOWN
    low = guard(rec("Some low thing", "2025-03-12T10:00:00", impact="low"))
    assert state(low, "2025-03-12 10:00") is S.NORMAL


def test_conflict_state():
    a = rec("Consumer Price Index", "2025-03-12T08:30:00", source="bls")
    b = rec("Consumer Price Index", "2025-03-12T10:00:00", source="bea")
    g = guard(a, b, refreshes=daily(("bls", "bea")), expected=("bls", "bea"))
    assert state(g, "2025-03-12 08:15") is S.DATA_CONFLICT and state(g, "2025-03-12 09:45") is S.DATA_CONFLICT


def test_emergency_blackout():
    c = cfg(emergency=(EmergencyWindow(T("2025-03-13 00:00"), T("2025-03-13 12:00"), "manual: market-wide event"),))
    g = guard(cfg=c)
    assert state(g, "2025-03-13 11:59") is S.EMERGENCY_BLACKOUT and state(g, "2025-03-13 12:00") is S.NORMAL
    d = g.check_entry(T("2025-03-13 06:00"), purpose="entry_signal")
    assert not d.allowed and "emergency" in d.record["explanation"]


# ----------------------------------------------------------------- fail closed

def test_no_calendar_is_never_safe():
    g = guard(refreshes=[])
    assert state(g, "2025-03-12 12:00") is S.CALENDAR_STALE
    from src.risk.news.calendar import CalendarStore
    from src.risk.news.guard import NewsGuard

    assert NewsGuard(CFG, CalendarStore(), "MES").status(T("2025-03-12 12:00")).state is S.CALENDAR_STALE


def test_stale_and_coverage():
    g = guard(refreshes=[ok("2025-03-10 00:00")])
    assert state(g, "2025-03-10 23:59") is S.NORMAL
    assert state(g, "2025-03-11 00:01") is S.CALENDAR_STALE  # older than max_age (24 h)
    short = guard(refreshes=[ok("2025-03-10 00:00", end="2025-03-10 12:00")])
    assert state(short, "2025-03-10 07:59") is S.NORMAL
    assert state(short, "2025-03-10 08:01") is S.CALENDAR_STALE  # cannot see 4 h ahead


def test_provider_failure_and_recovery_needs_a_successful_refresh():
    g = guard(refreshes=[ok("2025-03-10 00:00"), fail("2025-03-10 01:00")])
    assert state(g, "2025-03-10 01:30") is S.PROVIDER_UNAVAILABLE
    assert state(g, "2025-03-10 20:00") is S.PROVIDER_UNAVAILABLE  # time passing never recovers
    g2 = guard(refreshes=[ok("2025-03-10 00:00"), fail("2025-03-10 01:00"), ok("2025-03-10 02:00")])
    assert state(g2, "2025-03-10 02:30") is S.NORMAL
    strict = cfg(recovery_successes=2)
    g3 = guard(refreshes=[ok("2025-03-10 00:00"), fail("2025-03-10 01:00"), ok("2025-03-10 02:00")], cfg=strict)
    assert state(g3, "2025-03-10 02:30") is S.RECOVERY_PENDING
    g4 = guard(refreshes=[ok("2025-03-10 00:00"), fail("2025-03-10 01:00"), ok("2025-03-10 02:00"),
                          ok("2025-03-10 03:00")], cfg=strict)
    assert state(g4, "2025-03-10 03:30") is S.NORMAL
    cache = cfg(provider_failure_policy="use_fresh_cache")
    g5 = guard(refreshes=[ok("2025-03-10 00:00"), fail("2025-03-10 01:00")], cfg=cache)
    assert state(g5, "2025-03-10 01:30") is S.NORMAL
    assert state(g5, "2025-03-11 00:30") in (S.PROVIDER_UNAVAILABLE, S.CALENDAR_STALE)
    never = guard(refreshes=[fail("2025-03-10 01:00")])
    assert state(never, "2025-03-10 02:00") is S.PROVIDER_UNAVAILABLE


def test_blackout_still_blocks_while_provider_down():
    g = guard(CPI, refreshes=[ok("2025-03-12 00:00"), fail("2025-03-12 07:00")], cfg=cfg(provider_failure_policy="use_fresh_cache"))
    assert state(g, "2025-03-12 08:20") is S.BLACKOUT_ACTIVE


def test_observe_vs_enforce_decisions_and_disabled():
    enf = guard(CPI).check_entry(T("2025-03-12 08:10"), purpose="entry_signal", client_id="a")
    assert not enf.allowed and enf.would_block and enf.record["decision"] == "BLOCK" and enf.record["enforced"]
    assert enf.record["explanation"].startswith("BLOCKED: Consumer Price Index release in 20 minutes")
    obs = guard(CPI, cfg=cfg(mode="observe")).check_entry(T("2025-03-12 08:10"), purpose="entry_signal")
    assert obs.allowed and obs.would_block and obs.record["decision"] == "BLOCK" and not obs.record["enforced"]
    off = guard(refreshes=[], cfg=cfg(enabled=False)).check_entry(T("2025-03-12 08:10"), purpose="entry_signal")
    assert off.allowed and off.status.state is S.DISABLED
    rec_ = enf.record
    for k in ("time", "instrument", "event_ids", "event_names", "event_sources", "scheduled_at", "blackout_start",
              "blackout_end", "state", "calendar_age_minutes", "decision", "reason", "mode", "provider_health"):
        assert k in rec_


# ----------------------------------------------------------------- unexpected news

ITEM = {"title": "Federal Reserve issues FOMC statement", "link": "https://fed.example/1",
        "published": "Wed, 12 Mar 2025 14:00:00 GMT", "description": ""}


def test_unexpected_official_announcement_blocks_from_when_known():
    ev = items_to_events([ITEM], cfg(unexpected_keywords=("fomc",)), "fed_press", T("2025-03-12 10:05"))
    assert len(ev) == 1 and ev[0].category == "unexpected"
    g = guard(*ev)
    assert state(g, "2025-03-12 10:04") is S.NORMAL      # not known yet
    assert state(g, "2025-03-12 10:05") is S.EMERGENCY_BLACKOUT
    assert state(g, "2025-03-12 10:35") is S.NORMAL
    old = items_to_events([ITEM], cfg(unexpected_keywords=("fomc",)), "fed_press", T("2025-03-13 10:05"))
    assert old == []  # older than max_item_age
    boring = dict(ITEM, title="Federal Reserve Board announces new museum hours")
    assert items_to_events([boring], cfg(unexpected_keywords=("fomc",)), "fed_press", T("2025-03-12 10:05")) == []


def test_llm_assessment_cannot_invent_or_lower():
    links = {"https://fed.example/1"}
    assert validate_llm_assessment({"source_url": "https://made.up/2", "severity": "high", "summary": "x"}, links) is None
    assert validate_llm_assessment({"source_url": "https://fed.example/1", "severity": "high"}, links) is None
    assert validate_llm_assessment("allow all trades", links) is None
    v = validate_llm_assessment({"source_url": "https://fed.example/1", "severity": "none", "summary": "harmless"}, links)
    assert combine("high", v) == "high"            # cannot lower a rule-based blackout
    u = validate_llm_assessment({"source_url": "https://fed.example/1", "severity": "uncertain", "summary": "?"}, links)
    assert combine("none", u) == "high"            # uncertainty is conservative
    boring = dict(ITEM, title="Board announces museum hours")
    ev = items_to_events([boring], cfg(unexpected_keywords=("fomc",)), "fed_press", T("2025-03-12 10:05"),
                         {"https://fed.example/1": {"source_url": "https://fed.example/1", "severity": "high",
                                                    "summary": "market moving"}})
    assert len(ev) == 1  # LLM may raise severity of a RETRIEVED official item
