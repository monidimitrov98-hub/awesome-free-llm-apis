"""Sprint 4.1 operational calendar readiness: the manual calendar template, its validation,
and the readiness verdicts (never optimistic). Deterministic: no network."""

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

import run_news_calendar
from src.risk.news.calendar import CalendarStore
from src.risk.news.config import DEFAULT_NEWS_CONFIG_PATH
from src.risk.news.guard import NewsGuard, NewsState as S
from src.risk.news.manual import SCHEMA, manual_categories, validate_manual
from src.risk.news.providers import CensusHtmlProvider, FomcHtmlProvider, IcsProvider, JsonFileProvider, ProviderError
from src.risk.news.readiness import (CONFIG, INCOMPLETE, INVALID, PROVIDER, READY, STALE, readiness_report)
from src.risk.news.service import refresh
from tests.news_helpers import CFG, CLASSIFY, T, cfg

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "data" / "news" / "manual_calendar.json"
FIX = Path(__file__).parent / "fixtures" / "news"
NOW = T("2026-11-21 10:00")  # New York time
VERIFIED_AT = "2026-11-20T15:00:00Z"
BLS = "https://www.bls.gov/schedule/news_release/"


def template():
    return json.loads(TEMPLATE.read_text())


def complete():
    """A fully verified TEST calendar (test data only -- never committed as real dates)."""
    doc = template()
    for c in doc["categories"].values():
        c.update(status="verified", verified_through="2026-12-31", last_verified=VERIFIED_AT)
    ev = lambda name, cat, when, src=BLS + "cpi.htm": {  # noqa: E731
        "name": name, "category": cat, "scheduled_at": when, "timezone": "America/New_York", "source": src,
        "provenance": "manual", "verification_status": "verified", "last_verified": VERIFIED_AT}
    doc["events"] = [ev("TEST Consumer Price Index", "cpi", "2026-12-10T08:30:00"),
                     ev("TEST Employment Situation", "nfp", "2026-12-04T08:30:00", BLS + "empsit.htm"),
                     {"name": "TEST Fed Chair testimony", "category": "fed_chair_speech", "date": "2026-12-02",
                      "timezone": "America/New_York", "source": "https://www.federalreserve.gov/newsevents/calendar.htm",
                      "provenance": "manual", "verification_status": "date_only", "last_verified": VERIFIED_AT}]
    dol = "https://oui.doleta.gov/unemploy/claims_arch.asp"
    ism = "https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/"
    doc["events"] += [ev(f"TEST Jobless claims {d}", "jobless_claims", f"{d}T08:30:00", dol)
                      for d in ("2026-11-25", "2026-12-03", "2026-12-10", "2026-12-17", "2026-12-23", "2026-12-31")]
    doc["events"] += [ev("TEST Producer Price Index", "ppi", "2026-12-11T08:30:00", BLS + "ppi.htm"),
                      ev("TEST JOLTS", "jolts", "2026-12-08T10:00:00", BLS + "jolts.htm"),
                      ev("TEST ISM Manufacturing PMI", "ism_manufacturing", "2026-12-01T10:00:00", ism),
                      ev("TEST ISM Services PMI", "ism_services", "2026-12-03T10:00:00", ism)]
    return doc


# ----------------------------------------------------------------- the committed template

def test_template_has_every_category_and_no_guessed_dates():
    doc = template()
    assert doc["schema"] == SCHEMA and doc["events"] == []
    needed = {"cpi", "core_cpi", "nfp", "unemployment_rate", "ppi", "jobless_claims", "ism_manufacturing",
              "ism_services", "fomc_decision", "fomc_statement", "fed_press_conference", "fed_chair_speech", "treasury"}
    assert needed <= set(doc["categories"]) and set(manual_categories(CFG)) <= set(doc["categories"])
    for c in doc["categories"].values():
        assert c["status"] == "unresolved" and c["verified_through"] is None and c["last_verified"] is None
        assert c["official_source"].startswith("https://")
    text = TEMPLATE.read_text()
    assert "scheduled_at\"" not in text and "\"date\"" not in text  # nothing guessed


def test_template_is_valid_but_keeps_trading_blocked():
    rep = validate_manual(template(), CFG, NOW, CLASSIFY)
    assert rep.valid and rep.coverage == {} and not rep.to_dict()["covers_operational_horizon"]
    assert all(not v["covered"] for v in rep.categories.values())


def test_required_categories_cover_the_operational_list():
    for c in ("cpi", "core_cpi", "nfp", "unemployment_rate", "ppi", "jobless_claims", "ism_manufacturing",
              "ism_services", "fomc_decision", "fomc_statement", "fed_press_conference", "fed_chair_speech",
              "treasury", "pce", "gdp", "retail_sales"):
        assert c in CFG.required_categories


# ----------------------------------------------------------------- validation

def mutate(fn):
    d = complete()
    fn(d)
    return validate_manual(d, CFG, NOW, CLASSIFY)


@pytest.mark.parametrize("fn,match", [
    (lambda d: d["categories"].pop("ppi"), "missing event category 'ppi'"),
    (lambda d: d["events"][0].pop("scheduled_at"), "missing timestamp"),
    (lambda d: d["events"][2].pop("date"), "missing date"),
    (lambda d: d["events"][0].update(source="https://www.forexfactory.com/calendar"), "unofficial source"),
    (lambda d: d["events"][0].update(source="http://www.bls.gov/x"), "unofficial source"),
    (lambda d: d["events"][0].update(source=None), "unofficial source"),
    (lambda d: d["categories"]["cpi"].update(official_source="https://example.com/cpi"), "official_source"),
    (lambda d: d["events"].append(dict(d["events"][0], name="CPI again")), "duplicate event"),
    (lambda d: [e.update(event_id="x") for e in d["events"][:2]], "duplicate event_id"),
    (lambda d: d["events"][0].update(timezone="Eastern Time"), "invalid timezone"),
    (lambda d: d["events"][0].update(provenance="verified_live"), "provenance"),
    (lambda d: d["events"].append({"name": "x", "category": "ppi", "timezone": "America/New_York", "source": BLS + "ppi.htm",
                                   "provenance": "manual", "verification_status": "unresolved", "date": "2026-12-12"}),
     "must not carry a date"),
    (lambda d: d["events"][0].update(category="moon"), "not declared"),
    (lambda d: d["events"][0].update(verification_status="probably"), "verification_status"),
    (lambda d: d["events"][0].update(last_verified="2027-01-01T00:00:00Z"), "future"),
    (lambda d: d["events"][0].update(scheduled_at="2026-03-08T02:30:00"), "DST"),
    (lambda d: d["categories"]["cpi"].update(verified_through=None), "verified needs"),
    (lambda d: d["categories"]["cpi"].update(status="maybe"), "status"),
    (lambda d: d["categories"]["treasury"].update(status="unresolved"), "cannot have verified_through"),
    (lambda d: d.update(extra=1), "unknown top-level"),
    (lambda d: d["events"][0].update(evil="rm -rf /"), "unknown fields"),
    (lambda d: d.update(schema="news-calendar/1"), "schema"),
])
def test_validation_errors(fn, match):
    rep = mutate(fn)
    assert not rep.valid and any(match in e for e in rep.errors), rep.errors


def test_complete_calendar_coverage_and_upcoming_events():
    rep = validate_manual(complete(), CFG, NOW, CLASSIFY)
    d = rep.to_dict()
    assert rep.valid and d["covers_operational_horizon"] and not d["categories_not_covered"]
    names = [u["name"] for u in d["upcoming_high_impact"]]
    assert names == ["TEST Fed Chair testimony", "TEST Employment Situation"]  # CPI on 12-10 is beyond 14 days
    assert d["upcoming_high_impact"][0]["time_known"] is False


def test_unresolved_event_stale_and_short_horizon_uncover_a_category():
    def unresolved(d):
        d["events"].append({"name": "TEST PPI (date not announced)", "category": "ppi", "timezone": "America/New_York",
                            "source": BLS + "ppi.htm", "provenance": "manual", "verification_status": "unresolved"})
    r = mutate(unresolved)
    assert r.valid and not r.categories["ppi"]["covered"] and "ppi" not in r.coverage
    stale = mutate(lambda d: d["categories"]["cpi"].update(last_verified="2026-11-01T00:00:00Z"))
    assert not stale.categories["cpi"]["covered"] and "stale" in stale.categories["cpi"]["reasons"][0]
    short = mutate(lambda d: d["categories"]["nfp"].update(verified_through="2026-11-28"))
    assert not short.categories["nfp"]["covered"] and "short horizon" in short.categories["nfp"]["reasons"][0]
    old = mutate(lambda d: d["events"][0].update(last_verified="2026-10-01T00:00:00Z"))
    assert old.not_recently_verified == ["TEST Consumer Price Index"]


# ----------------------------------------------------------------- store + readiness

def body(name):
    return (FIX / name).read_bytes()


def build_store(doc, tmp_path, *, c=CFG, failing=(), now=NOW):
    path = tmp_path / "manual.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    pcs = dict(c.providers)
    from dataclasses import replace

    pcs["manual"] = replace(pcs["manual"], path=str(path))
    c = replace(c, providers=pcs)

    def transport(fixture, name):
        def get(url, *a):
            if name in failing:
                raise ProviderError("request failed: URLError")
            if name == "census" and "2026" not in url:  # the current-year page lists 2027
                return body("census_calendar_2027_excerpt.html")
            return body(fixture)
        return get
    providers = [JsonFileProvider(pcs["manual"], c, CLASSIFY),
                 IcsProvider(pcs["bea"], c, CLASSIFY, transport("bea_calendar_excerpt.ics", "bea")),
                 CensusHtmlProvider(pcs["census"], c, CLASSIFY, transport("census_calendar_2026_excerpt.html", "census")),
                 FomcHtmlProvider(pcs["fomc"], c, CLASSIFY, transport("fomc_calendar_excerpt.html", "fomc"))]
    st = CalendarStore()
    refresh(st, providers, c, now, force=True)
    return c, st, (str(path), doc)


def verdict(doc, tmp_path, **kw):
    c, st, manual = build_store(doc, tmp_path, **kw)
    now = kw.get("now", NOW)
    return readiness_report(c, st, now + pd.Timedelta(minutes=1), CLASSIFY, manual=manual), c, st


def test_ready_only_with_a_complete_verified_calendar(tmp_path):
    rep, c, st = verdict(complete(), tmp_path)
    assert rep["verdict"] == READY and rep["ready"], rep["findings"]
    assert all(r["covered"] for r in rep["categories"].values())
    assert rep["categories"]["cpi"]["by"] == "manual" and rep["categories"]["gdp"]["by"] == "bea"
    g = NewsGuard(c, st, "MES")  # the same calendar drives NewsGuard's windows
    wins = g.windows_at(NOW + pd.Timedelta(minutes=1))

    def kind(t):
        return next((w.kind for w in wins if w.contains(T(t))), None)
    assert kind("2026-12-04 08:15") == "scheduled"     # NFP window
    assert kind("2026-12-02 15:00") == "unknown_time"  # date_only Fed Chair: whole day
    assert g.status(NOW + pd.Timedelta(minutes=1)).state is S.NORMAL
    # ...and without a new refresh the calendar becomes stale: entries blocked again
    assert g.status(T("2026-12-04 08:15")).state is not S.NORMAL


def test_template_readiness_is_incomplete(tmp_path):
    rep, c, st = verdict(template(), tmp_path)
    assert rep["verdict"] == INCOMPLETE and not rep["ready"]
    assert NewsGuard(c, st, "MES").status(NOW + pd.Timedelta(minutes=1)).state is S.CALENDAR_INCOMPLETE


@pytest.mark.parametrize("cat", sorted(set(CFG.required_categories)))
def test_never_ready_when_any_mandatory_category_is_missing(tmp_path, cat):
    doc = complete()
    if cat in doc["categories"]:
        doc["categories"][cat].update(status="unresolved", verified_through=None, last_verified=None)
        if cat in ("fomc_decision", "fomc_statement", "fed_press_conference"):
            rep, _, _ = verdict(doc, tmp_path, failing=("fomc",))  # and its live source down
        else:
            doc["events"] = [e for e in doc["events"] if e["category"] != cat]
            rep, _, _ = verdict(doc, tmp_path)
    else:  # covered by a live provider only (gdp, pce -> bea; retail_sales -> census)
        rep, _, _ = verdict(doc, tmp_path, failing=("bea", "census"))
    assert rep["verdict"] != READY and not rep["ready"]
    assert not rep["categories"][cat]["covered"]


def test_each_verdict(tmp_path):
    bad = complete()
    bad["events"][0]["timezone"] = "Nowhere/Zone"
    assert verdict(bad, tmp_path)[0]["verdict"] == INVALID
    assert verdict(complete(), tmp_path, c=cfg(mode="observe"))[0]["verdict"] == CONFIG
    assert verdict(complete(), tmp_path, failing=("bea",))[0]["verdict"] == PROVIDER
    stale = complete()
    for x in stale["categories"].values():
        x["last_verified"] = "2026-11-10T00:00:00Z"
    assert verdict(stale, tmp_path)[0]["verdict"] == STALE
    short = complete()
    short["categories"]["treasury"]["verified_through"] = "2026-11-25"
    assert verdict(short, tmp_path)[0]["verdict"] == INCOMPLETE
    old_format = {"coverage_start": "2020-01-01T00:00:00Z", "coverage_end": "2099-01-01T00:00:00Z", "events": []}
    rep, c, st = verdict(old_format, tmp_path)
    assert rep["verdict"] == INVALID  # an unstructured file can never make the system operational


def test_guard_stale_vs_incomplete_states(tmp_path):
    stale = complete()
    stale["categories"]["cpi"]["last_verified"] = "2026-11-10T00:00:00Z"
    c, st, _ = build_store(stale, tmp_path)
    assert NewsGuard(c, st, "MES").status(NOW + pd.Timedelta(minutes=1)).state is S.CALENDAR_STALE
    c, st, _ = build_store(complete(), tmp_path)
    later = T("2026-11-29 10:00")  # 9 days after verification: stale without a new check
    s = NewsGuard(c, st, "MES").status(later)
    assert s.state in (S.CALENDAR_STALE, S.PROVIDER_UNAVAILABLE)  # also no refresh for > 24 h
    invalid = complete()
    invalid["events"][0]["source"] = "https://example.com"
    c, st, _ = build_store(invalid, tmp_path)
    assert st.refreshes[0].ok is False  # invalid manual data -> failed refresh -> blocked
    assert NewsGuard(c, st, "MES").status(NOW + pd.Timedelta(minutes=1)).state is S.PROVIDER_UNAVAILABLE


def test_synthetic_store_is_invalid_for_operations(tmp_path):
    c, st, manual = build_store(complete(), tmp_path)
    from tests.news_helpers import rec

    st.add(rec("Consumer Price Index", "2026-12-15T08:30:00", rel="synthetic", known="2026-11-21 09:00"))
    assert readiness_report(c, st, NOW + pd.Timedelta(minutes=1), CLASSIFY, manual=manual)["verdict"] == INVALID


# ----------------------------------------------------------------- CLI

def test_cli_validate_manual(tmp_path, capsys):
    assert run_news_calendar.main(["--validate-manual", str(TEMPLATE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["valid"] and not out["covers_operational_horizon"]
    bad = tmp_path / "bad.json"
    d = template()
    d["categories"].pop("cpi")
    bad.write_text(json.dumps(d))
    assert run_news_calendar.main(["--validate-manual", str(bad)]) == 9
    (tmp_path / "x.json").write_text("{broken")
    assert run_news_calendar.main(["--validate-manual", str(tmp_path / "x.json")]) == 9


def test_cli_readiness_without_data_is_not_ready(tmp_path, capsys):
    assert run_news_calendar.main(["--readiness", "--store", str(tmp_path / "none.json"),
                                   "--json", str(tmp_path / "r.json")]) == 8
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["verdict"] != READY and "READINESS:" in capsys.readouterr().out
