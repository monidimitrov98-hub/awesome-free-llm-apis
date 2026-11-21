"""Independent adversarial audit of Sprint 4.1 (2026-11-21): one regression test (or more) per
finding. Each test reproduces a bypass that existed at 92f9b57 and asserts it is now closed."""

import json
import re
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from src.config import ConfigError
from src.risk.news.config import DEFAULT_NEWS_CONFIG_PATH, MANDATORY_CATEGORIES, load_news_config
from src.risk.news.event import strict_json_loads
from src.risk.news.guard import NewsGuard, NewsState as S
from src.risk.news.manual import official_url, validate_manual
from src.risk.news.readiness import INCOMPLETE, PROVIDER, READY, readiness_report
from tests.news_helpers import CFG, CLASSIFY, T, rec, store
from tests.test_news_manual import NOW, build_store, complete, template

TEXT = DEFAULT_NEWS_CONFIG_PATH.read_text()
AFTER = NOW + pd.Timedelta(minutes=1)


def cfg_with(tmp_path, pattern, repl):
    text, n = re.subn(pattern, repl, TEXT, count=1)
    assert n == 1, pattern
    p = tmp_path / "n.toml"
    p.write_text(text, encoding="utf-8")
    return load_news_config(p)


def ready(doc, tmp_path, **kw):
    c, st, man = build_store(doc, tmp_path, **kw)
    return readiness_report(c, st, AFTER, CLASSIFY, manual=man), c, st


# ---------------------------------------------------------------- HIGH-1 verified without events
def test_h1_verified_categories_without_events_are_not_coverage(tmp_path):
    doc = complete()
    doc["events"] = []  # every category "verified" through 2026-12-31, nothing listed
    rep = validate_manual(doc, CFG, NOW, CLASSIFY)
    assert not rep.categories["jobless_claims"]["covered"]
    assert "implausible" in " ".join(rep.categories["jobless_claims"]["reasons"])
    r, c, st = ready(doc, tmp_path)
    assert r["verdict"] == INCOMPLETE
    g = NewsGuard(c, st, "MES")
    assert g.status(AFTER).state is S.CALENDAR_INCOMPLETE and not g.check_entry(AFTER, purpose="entry_signal").allowed


def test_h1_release_group_counts_once_and_long_spans_fail(tmp_path):
    rep = validate_manual(complete(), CFG, NOW, CLASSIFY)
    assert rep.categories["core_cpi"]["covered"]  # covered by the CPI release listed under "cpi"
    far = complete()
    far["categories"]["cpi"]["verified_through"] = "2027-03-31"  # only one CPI listed in 4 months
    assert not validate_manual(far, CFG, NOW, CLASSIFY).categories["cpi"]["covered"]


# ---------------------------------------------------------------- HIGH-2 config can drop/weaken categories
@pytest.mark.parametrize("pattern,repl,match", [
    (r"required_categories = \[[^\]]*\]", 'required_categories = ["cpi"]', "mandatory"),
    (r'(\[events\.cpi\]\npatterns = [^\n]*\n)impact = "high"', r'\1impact = "low"', "impact"),
    (r'(\[events\.ppi\]\npatterns = [^\n]*\nimpact = "medium"\n)before_minutes = 15\nafter_minutes = 15',
     r"\1before_minutes = 0\nafter_minutes = 0", "before/after"),
    (r'block = \["high", "medium", "unknown"\]', 'block = ["high", "unknown"]', "block"),
    (r'(\[unknown_impact\][^\n]*\n)before_minutes = 30', r"\1before_minutes = 0", "unknown_impact"),
    (r'\[instruments\.MNQ\]\nregions = \["US"\]', '[instruments.MNQ]\nregions = ["EU"]', "US"),
    (r'\[instruments\.MES\]\nregions = \["US"\]', '[instruments.MES]\nregions = ["US"]\noverrides = { cpi = [0, 0] }',
     "overrides"),
    (r"max_age_minutes = 1440", "max_age_minutes = 525600", "max_age"),
    (r"lookahead_minutes = 240", "lookahead_minutes = 1", "lookahead"),
    (r"max_verification_age_days = 7", "max_verification_age_days = 365", "max_verification_age"),
    (r"operational_horizon_days = 14", "operational_horizon_days = 1", "horizon"),
    (r"official_domains = \[", 'official_domains = ["forexfactory.com", ', "official_domains"),
])
def test_h2_config_cannot_weaken_mandatory_protection(tmp_path, pattern, repl, match):
    with pytest.raises(ConfigError, match=match):
        cfg_with(tmp_path, pattern, repl)


def test_h2_config_may_still_tighten(tmp_path):
    c = cfg_with(tmp_path, r'(\[events\.ppi\]\npatterns = [^\n]*\n)impact = "medium"', r'\1impact = "high"')
    assert c.events["ppi"].impact.value == "high"


# ---------------------------------------------------------------- HIGH-3 config "covers" claims
def test_h3_live_provider_cannot_claim_categories_its_data_lacks(tmp_path):
    c = cfg_with(tmp_path, r'covers = \["gdp", "pce", "core_pce", "trade_balance"\]',
                 'covers = ["gdp", "pce", "core_pce", "trade_balance", ' + ", ".join(
                     f'"{k}"' for k in MANDATORY_CATEGORIES if k not in ("gdp", "pce")) + "]")
    r, c2, st = ready(template(), tmp_path, c=c)  # manual unresolved; BEA "covers" everything by config
    assert r["verdict"] == INCOMPLETE and not r["categories"]["cpi"]["covered"]
    assert any("lists no upcoming cpi" in x for x in r["categories"]["cpi"]["reasons"])
    g = NewsGuard(c2, st, "MES")
    assert g.status(AFTER).state is S.CALENDAR_INCOMPLETE


# ---------------------------------------------------------------- MEDIUM-1 fabricated verification
@pytest.mark.parametrize("pattern,repl", [
    (r'\[providers\.bls\]\ntype = "ics"\nenabled = false[^\n]*',
     '[providers.bls]\ntype = "ics"\nenabled = true\nverified = true\nverified_on = "2026-11-21"\n'
     'documentation = "https://www.bls.gov/help/hlpiCAL.htm"'),
    (r'url = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"',
     'url = "https://www.bea.gov/other.ics"'),
    (r'verified_on = "2026-11-21"\ndocumentation = "https://www.bea.gov/news/schedule"',
     'verified_on = "2026-11-22"\ndocumentation = "https://www.bea.gov/news/schedule"'),
])
def test_m1_verification_must_match_recorded_evidence(tmp_path, pattern, repl):
    with pytest.raises(ConfigError, match="verification record"):
        cfg_with(tmp_path, pattern, repl)


def test_m1_stale_provider_evidence_blocks_readiness(tmp_path):
    r, c, st = ready(complete(), tmp_path)
    assert r["verdict"] == READY
    old = replace(c, provider_evidence={k: "2026-01-01T00:00:00+00:00" for k in c.provider_evidence})
    assert readiness_report(old, st, AFTER, CLASSIFY, manual=build_store(complete(), tmp_path)[2])["verdict"] != READY


# ---------------------------------------------------------------- MEDIUM-2 strict JSON
@pytest.mark.parametrize("text,match", [
    ('{"a": 1, "a": 2}', "duplicate"), ('{"a": NaN}', "NaN"), ('{"a": Infinity}', "Infinity"),
    ("[" * 50 + "]" * 50, "deeper"), ("[" * 200000 + "]" * 200000, "deeper"),
])
def test_m2_strict_json(text, match):
    with pytest.raises(ValueError, match=match):
        strict_json_loads(text)


def test_m2_duplicate_key_cannot_flip_a_category_to_verified(tmp_path, capsys):
    import run_news_calendar

    raw = json.dumps(complete()).replace('"status": "verified"', '"status": "unresolved", "status": "verified"', 1)
    f = tmp_path / "dup.json"
    f.write_text(raw)
    assert run_news_calendar.main(["--validate-manual", str(f)]) == 9
    from src.risk.news.providers import JsonFileProvider, ProviderError

    pc = replace(CFG.providers["manual"], path=str(f))
    with pytest.raises(ProviderError):
        JsonFileProvider(pc, CFG, CLASSIFY).fetch(NOW)


# ---------------------------------------------------------------- MEDIUM-3 contradictory records
def test_m3_contradictory_times_for_one_release_are_errors():
    d = complete()
    d["events"].append(dict(d["events"][0], category="core_cpi", name="TEST core CPI",
                            scheduled_at="2026-12-10T10:00:00"))  # CPI at 08:30, core CPI at 10:00
    rep = validate_manual(d, CFG, NOW, CLASSIFY)
    assert not rep.valid and any("contradictory" in e for e in rep.errors)
    same_time = complete()
    same_time["events"].append(dict(same_time["events"][0], category="core_cpi", name="TEST core CPI"))
    assert validate_manual(same_time, CFG, NOW, CLASSIFY).valid  # same release, same time: fine
    two_speeches = complete()
    two_speeches["events"].append(dict(two_speeches["events"][2], name="TEST second appearance"))
    two_speeches["events"][-1].pop("date")
    two_speeches["events"][-1].update(verification_status="verified", scheduled_at="2026-12-02T15:00:00")
    assert validate_manual(two_speeches, CFG, NOW, CLASSIFY).valid  # two Chair appearances may share a day


# ---------------------------------------------------------------- MEDIUM-4 degraded provider looked ready
def test_m4_failing_provider_never_ready_even_with_cached_data(tmp_path):
    from src.risk.news.calendar import RefreshRecord

    c = replace(CFG, provider_failure_policy="use_fresh_cache")
    _, st, man = build_store(complete(), tmp_path, c=c)
    c2 = build_store(complete(), tmp_path, c=c)[0]
    st.log_refresh(RefreshRecord(NOW + pd.Timedelta(seconds=30), "bea", False, error="timeout"))
    assert readiness_report(c2, st, AFTER, CLASSIFY, manual=man)["verdict"] == PROVIDER
    h = NewsGuard(c2, st, "MES").status(AFTER).provider_health["bea"]
    assert h["status"] == "degraded_cached"  # entries may use the fresh cache (explicit policy), never "ok"


# ---------------------------------------------------------------- MEDIUM-5 fill inside a multi-minute bar
def test_m5_pending_entry_cannot_fill_into_a_window_inside_the_execution_bar():
    from src.backtest.engine import BacktestEngine
    from src.backtest.orders import OrderRequest
    from tests.backtest_helpers import GLOBEX, MES, flat_bars, frame, make_config
    from tests.test_news_engine import Recorder

    ev = rec("Consumer Price Index", "2025-03-10T10:07:00")  # window 09:37 .. 10:37 (NY)
    rows = flat_bars("09:00", 7, minutes=5) + [("09:35", 5000, 5000.5, 4994, 4995)] + flat_bars("09:40", 6, 4995, minutes=5)
    limit = OrderRequest.enter("long", "limit", price=4996, stop_loss=4980, client_id="lim")
    strat = Recorder({"09:30": [limit]})
    res = BacktestEngine(make_config(), MES, GLOBEX).run(strat, frame(rows, minutes=5), news_guard=store_guard(ev))
    # the 09:35 bar opens before the window (09:37) but the limit fills inside it
    assert res.trades.empty and "news_blackout_active" in set(res.rejections.reason)
    plain = BacktestEngine(make_config(), MES, GLOBEX).run(Recorder({"09:30": [limit]}), frame(rows, minutes=5))
    assert len(plain.trades) == 1


def store_guard(*records):
    return NewsGuard(CFG, store(*records), "MES")


# ---------------------------------------------------------------- MEDIUM-6 readiness vs authorization
def test_m6_operational_guard_uses_readiness_rules():
    syn = store(rec("Consumer Price Index", "2025-03-12T08:30:00", rel="synthetic"))
    assert NewsGuard(CFG, syn, "MES").status(T("2025-03-12 12:00")).state is S.NORMAL  # research mode
    op = NewsGuard(CFG, syn, "MES", operational=True)
    assert op.status(T("2025-03-12 12:00")).state is S.CALENDAR_INCOMPLETE
    assert not op.check_entry(T("2025-03-12 12:00"), purpose="entry_signal").allowed
    unknown = store(rec("Consumer Price Index", "2025-03-12T08:30:00"))  # provider class unknown
    assert NewsGuard(CFG, unknown, "MES", operational=True).status(T("2025-03-12 12:00")).state is S.CALENDAR_INCOMPLETE


def test_m6_ready_implies_operational_guard_allows_and_not_ready_states_block(tmp_path):
    r, c, st = ready(complete(), tmp_path)
    assert r["verdict"] == READY
    assert NewsGuard(c, st, "MES", operational=True).status(AFTER).state is S.NORMAL
    r2, c2, st2 = ready(template(), tmp_path)
    assert r2["verdict"] == INCOMPLETE
    assert not NewsGuard(c2, st2, "MES", operational=True).check_entry(AFTER, purpose="entry_signal").allowed


# ---------------------------------------------------------------- LOW findings
def test_l1_historical_store_cannot_claim_verification_after_its_refresh():
    from src.risk.news.calendar import CalendarStore

    doc = {"schema": "news-calendar/1", "refreshes": [{"time": "2026-11-21T10:00:00Z", "provider": "manual", "ok": True,
                                                       "category_coverage": {"cpi": {"through": "2026-12-31T05:00:00Z",
                                                                                     "verified_at": "2026-11-25T00:00:00Z"}}}],
           "events": []}
    with pytest.raises(ConfigError, match="after the refresh"):
        CalendarStore.from_json(json.dumps(doc), CLASSIFY)


def test_l2_https_download_has_a_total_deadline(monkeypatch):
    import src.risk.news.providers as prov

    class Slow:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"x" * 10

    class Opener:
        def open(self, req, timeout):
            return Slow()

    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(prov, "_opener", lambda: Opener())
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    with pytest.raises(prov.ProviderError, match="total time"):
        prov.https_get("https://www.bea.gov/x.ics", {}, 10, 10_000_000)


@pytest.mark.parametrize("url", ["https://bls.gov.evil.example/cpi", "https://www.bls.gov@evil.example/cpi",
                                 "https://evilbls.gov/cpi", "http://www.bls.gov/cpi", "https://www.bls.gov:evil/x",
                                 "javascript:alert(1)", "https://" + "a" * 600 + ".bls.gov/"])
def test_l3_official_source_spoofing_refused(url):
    assert not official_url(url, CFG)
    assert official_url("https://www.bls.gov/schedule/news_release/cpi.htm", CFG)


@pytest.mark.parametrize("raw,match", [
    ({"scheduled_at": "2026-11-01T01:30:00"}, "DST"),   # ambiguous (fall back)
    ({"scheduled_at": "2026-03-08T02:30:00"}, "DST"),   # nonexistent (spring forward)
    ({"scheduled_at": "2026-13-01T08:30:00"}, "malformed"),
    ({"scheduled_at": "2026-12-10T08:30:00+09:00"}, None),  # explicit offset is honoured, converted to UTC
])
def test_l4_manual_time_validation(raw, match):
    d = complete()
    d["events"][0].update(raw)
    rep = validate_manual(d, CFG, NOW, CLASSIFY)
    if match is None:
        assert rep.valid
        cpi = next(e for e in rep.events if e["name"] == "TEST Consumer Price Index")
        assert cpi["scheduled_at"] == "2026-12-10T08:30:00+09:00"
    else:
        assert not rep.valid and any(match in e for e in rep.errors)


@pytest.mark.parametrize("day,hours", [("2026-03-08", 23), ("2026-11-01", 25), ("2026-12-02", 24)])
def test_l5_date_only_blocks_the_whole_new_york_day_across_dst(day, hours):
    from tests.news_helpers import daily

    d0 = pd.Timestamp(day)
    g = NewsGuard(CFG, store(rec("Fed Chair testimony", date=day),
                             refreshes=daily(start=str((d0 - pd.Timedelta(days=2)).date()),
                                             end=str((d0 + pd.Timedelta(days=2)).date()))), "MES")
    w = g.windows_at(T(f"{day} 00:00"))[0]
    assert w.start == T(f"{day} 00:00") and (w.end - w.start) == pd.Timedelta(hours=hours)
    assert g.status(T(f"{day} 20:00")).state is S.EVENT_UNKNOWN  # (23:59 on a 25 h day is also stale)
    assert g.status(w.end).state is S.NORMAL


def test_l6_single_entry_creation_site_in_the_engine():
    src = (Path(__file__).resolve().parents[1] / "src/backtest/engine.py").read_text()
    assert src.count("role=OrderRole.ENTRY") == 1
    i = src.index("role=OrderRole.ENTRY")
    assert src.rfind("def _submit_entry", 0, i) > src.rfind("\n    def ", 0, src.rfind("def _submit_entry", 0, i))
    assert src.index("self.news.check_entry(now", src.index("def _submit_entry")) < i
