"""Sprint 4.1 final: custom, additional and unknown categories can never weaken blackout
protection. Non-mandatory categories have a non-negotiable 30/30 minimum (load time AND
runtime); mandatory categories keep their category-specific minimums; ambiguous or conflicting
classifications get the most conservative window; invalid classifications fail closed."""

import re
from dataclasses import replace

import pandas as pd
import pytest

from src.config import ConfigError
from src.risk.news.classifier import make_classifier
from src.risk.news.config import (CUSTOM_FLOOR, DEFAULT_NEWS_CONFIG_PATH, MANDATORY_WINDOWS, EventRule,
                                  load_news_config)
from src.risk.news.event import EventDataError, Impact, Reliability, parse_record
from src.risk.news.guard import NewsGuard, NewsState as S
from tests.news_helpers import CFG, T, daily, store

TEXT = DEFAULT_NEWS_CONFIG_PATH.read_text()
M = lambda m: pd.Timedelta(minutes=m)  # noqa: E731
CUSTOM = '[events.my_custom]\npatterns = ["widget index"]\nimpact = "{impact}"\nbefore_minutes = {b}\nafter_minutes = {a}\n'


def load(tmp_path, text):
    p = tmp_path / "n.toml"
    p.write_text(text, encoding="utf-8")
    return load_news_config(p)


def with_custom(b, a, impact="medium", extra=""):
    return TEXT.replace("# Event categories:", CUSTOM.format(b=b, a=a, impact=impact) + extra + "\n# Event categories:", 1)


def custom_cfg(b, a, impact=Impact.MEDIUM, patterns=("widget index",), name="my_custom"):
    """A config that BYPASSES the loader (built in code) with a weak custom category."""
    ev = dict(CFG.events)
    ev[name] = EventRule(name, patterns, impact, M(b), M(a), tuple(re.compile(x, re.I) for x in patterns))
    return replace(CFG, events=ev)


def rec(name, when, cfg=CFG, category=None, known="2025-03-01 00:00"):
    raw = {"name": name, "scheduled_at": when}
    if category:
        raw["category"] = category
    return parse_record(raw, source="fixture", reliability=Reliability.OFFICIAL, known_at=T(known),
                        classify=make_classifier(cfg))


def guard(cfg, *records):
    st = store(*records, cfg=cfg)
    return NewsGuard(cfg, st, "MES")


AT = "2025-03-12T10:00:00"
T0 = pd.Timestamp("2025-03-12 10:00", tz="America/New_York").tz_convert("UTC")


# 1. custom category configured with 0/0 ------------------------------------------------
def test_01_custom_zero_window_refused_and_clamped(tmp_path):
    with pytest.raises(ConfigError, match="30 min before / 30 min after"):
        load(tmp_path, with_custom(0, 0))
    c = custom_cfg(0, 0)
    assert c.window("MES", "my_custom") == CUSTOM_FLOOR
    g = guard(c, rec("Widget Index", AT, c))
    assert g.status(T0 - M(30)).state is S.BLACKOUT_ACTIVE and g.status(T0 + M(29)).state is S.BLACKOUT_ACTIVE


# 2. custom category configured with 5/5 ------------------------------------------------
@pytest.mark.parametrize("b,a", [(5, 5), (29, 30), (30, 29), (30, 0), (0, 30)])
def test_02_custom_weak_windows_refused(tmp_path, b, a):
    with pytest.raises(ConfigError, match="non-mandatory"):
        load(tmp_path, with_custom(b, a))
    assert custom_cfg(b, a).window("MES", "my_custom") == (max(M(b), M(30)), max(M(a), M(30)))


def test_02b_wider_custom_windows_allowed(tmp_path):
    c = load(tmp_path, with_custom(45, 60))
    assert c.window("MNQ", "my_custom") == (M(45), M(60))


# 3. weaker instrument override --------------------------------------------------------------
@pytest.mark.parametrize("cat", ["my_custom", "trade_balance", "minor_statistics", "unexpected"])
def test_03_weaker_override_for_custom_category(tmp_path, cat):
    text = with_custom(30, 30).replace('[instruments.MNQ]\nregions = ["US"]',
                                       f'[instruments.MNQ]\nregions = ["US"]\noverrides = {{ {cat} = [5, 5] }}')
    with pytest.raises(ConfigError, match="30/30 for non-mandatory"):
        load(tmp_path, text)
    c = custom_cfg(30, 30)
    insts = dict(c.instruments)
    insts["MNQ"] = {"regions": ["US"], "overrides": {"my_custom": (M(0), M(0))}}
    assert replace(c, instruments=insts).window("MNQ", "my_custom") == CUSTOM_FLOOR  # runtime clamp


# 4. a newly introduced category capturing a CPI event ----------------------------------------
def test_04_new_category_cannot_capture_cpi(tmp_path):
    text = TEXT.replace("# Event categories:", CUSTOM.format(b=30, a=30, impact="low").replace(
        "widget index", "consumer price index") + "\n# Event categories:", 1)
    c = load(tmp_path, text)  # allowed at 30/30, defined BEFORE cpi in the file
    r = rec("Consumer Price Index", AT, c)
    assert r.category == "cpi" and "my_custom" in r.alt_categories and r.impact is Impact.HIGH
    # bypassing the loader: a 0/0 "cpi" alias still cannot replace the CPI floor
    weak = custom_cfg(0, 0, Impact.LOW, ("consumer price index",), name="aaa_cpi_alias")
    r2 = rec("Consumer Price Index", AT, weak)
    assert r2.category == "cpi"
    g = guard(weak, r2)
    assert g.status(T0 - M(30)).state is S.BLACKOUT_ACTIVE and g.status(T0 + M(29)).state is S.BLACKOUT_ACTIVE


# 5. an unknown event assigned to a custom category -------------------------------------------
def test_05_unknown_event_in_custom_category_keeps_30_30():
    weak = custom_cfg(1, 1, Impact.LOW)
    unknown = rec("Quarterly Gadget Survey", AT)                      # unclassified -> 30/30
    assigned = rec("Quarterly Gadget Survey", AT, weak, category="my_custom")  # explicit weak custom category
    for c, r in ((CFG, unknown), (weak, assigned)):
        g = guard(c, r)
        assert g.status(T0 - M(30)).state is not S.NORMAL and g.status(T0 + M(29)).state is not S.NORMAL
        assert g.status(T0 - M(31)).state is S.NORMAL


# 6. conflicting mandatory and custom classifications ----------------------------------------
def test_06_conflicting_categories_use_the_most_conservative_window():
    wide = custom_cfg(90, 120, Impact.MEDIUM, ("producer price index",))
    r = rec("Producer Price Index", AT, wide)
    assert r.category == "ppi" and r.alt_categories == ("my_custom",)
    w = guard(wide, r).windows_at(T0 - M(200))[0]
    assert (w.start, w.end) == (T0 - M(90), T0 + M(120))  # custom is wider than PPI 15/15 -> custom wins
    # explicit (wrong) category hint on a CPI release: name still implies CPI -> CPI floor applies
    mis = rec("Consumer Price Index", AT, CFG, category="jobless_claims")
    assert mis.category == "jobless_claims" and "cpi" in mis.alt_categories and mis.impact is Impact.HIGH
    w2 = guard(CFG, mis).windows_at(T0 - M(200))[0]
    assert (w2.start, w2.end) == (T0 - M(30), T0 + M(30))  # not jobless claims' 5/10
    # a name matching two mandatory categories gets the wider of the two
    both = rec("CPI and PPI combined release", AT)
    assert {both.category, *both.alt_categories} >= {"cpi", "ppi"}
    w3 = guard(CFG, both).windows_at(T0 - M(200))[0]
    assert (w3.start, w3.end) == (T0 - M(30), T0 + M(30))


# 7. configuration that bypasses the normal loader --------------------------------------------
def test_07_programmatic_config_is_clamped_everywhere():
    ev = {k: replace(v, before=M(0), after=M(0), impact=Impact.LOW) for k, v in CFG.events.items()}
    insts = {k: {"regions": ["US"], "overrides": {c: (M(0), M(0)) for c in ev}} for k in CFG.instruments}
    c = replace(CFG, events=ev, instruments=insts, unknown_before=M(0), unknown_after=M(0), block_impacts=())
    for cat in ev:
        assert c.window("MES", cat) >= MANDATORY_WINDOWS.get(cat, CUSTOM_FLOOR)
    assert c.window("MES", "never_defined") == CUSTOM_FLOOR
    g = guard(c, rec("Wholesale Inventories", AT, c), rec("Consumer Price Index", "2025-03-12T08:30:00", c))
    assert g.status(T0 - M(30)).state is S.BLACKOUT_ACTIVE          # low impact, 0/0 configured -> still 30/30
    cpi = pd.Timestamp("2025-03-12 08:30", tz="America/New_York").tz_convert("UTC")
    assert g.status(cpi - M(30)).state is S.BLACKOUT_ACTIVE


# 8. missing custom category policy -------------------------------------------------------------
def test_08_missing_category_policy(tmp_path):
    assert CFG.window("MES", "category_without_definition") == CUSTOM_FLOOR
    with pytest.raises(ConfigError):
        load(tmp_path, TEXT.replace("# Event categories:", '[events.no_policy]\npatterns = ["gizmo"]\nimpact = "medium"\n'
                                    "\n# Event categories:", 1))  # no before/after: refused, never defaulted weak
    text = TEXT.replace('[instruments.MNQ]\nregions = ["US"]', '[instruments.MNQ]\nregions = ["US"]\noverrides = { ghost = [60, 60] }')
    with pytest.raises(ConfigError, match="known category"):
        load(tmp_path, text)


# 9. malformed or ambiguous classification ---------------------------------------------------------
@pytest.mark.parametrize("raw,match", [
    ({"name": "Consumer Price Index", "scheduled_at": AT, "category": "nonexistent"}, "unknown category"),
    ({"name": "Consumer Price Index", "scheduled_at": AT, "impact": "catastrophic"}, "unknown impact"),
    ({"name": "Consumer Price Index", "scheduled_at": AT, "category": ""}, None),  # empty hint -> classify by name
])
def test_09_invalid_classification_fails_closed(raw, match):
    from src.risk.news.service import refresh
    from src.risk.news.providers import JsonFileProvider

    if match is None:
        r = parse_record(raw, source="x", reliability=Reliability.OFFICIAL, known_at=T("2025-03-01 00:00"),
                         classify=make_classifier(CFG))
        assert r.category == "cpi"
        return
    with pytest.raises(EventDataError, match=match):
        parse_record(raw, source="x", reliability=Reliability.OFFICIAL, known_at=T("2025-03-01 00:00"),
                     classify=make_classifier(CFG))


def test_09b_invalid_classification_in_a_feed_blocks_entries(tmp_path):
    import json

    from src.risk.news.providers import JsonFileProvider
    from src.risk.news.service import refresh

    f = tmp_path / "feed.json"
    f.write_text(json.dumps({"coverage_start": "2025-01-01T00:00:00Z", "coverage_end": "2026-01-01T00:00:00Z",
                             "events": [{"name": "Consumer Price Index", "scheduled_at": AT, "category": "bogus"}]}))
    pc = replace(CFG.providers["manual"], path=str(f))
    st = store(refreshes=[], expected=("manual",))
    refresh(st, [JsonFileProvider(pc, CFG, make_classifier(CFG))], CFG, T("2025-03-12 09:00"), force=True)
    assert not st.refreshes[-1].ok and st.records == []
    g = NewsGuard(CFG, st, "MES")
    d = g.check_entry(T("2025-03-12 09:01"), purpose="entry_signal")
    assert not d.allowed and d.status.state in (S.PROVIDER_UNAVAILABLE, S.CALENDAR_INCOMPLETE)


def test_09c_ambiguous_custom_categories_take_the_widest():
    c = custom_cfg(45, 30, Impact.MEDIUM, ("gadget",), name="cat_a")
    ev = dict(c.events)
    ev["cat_b"] = EventRule("cat_b", ("survey",), Impact.LOW, M(30), M(75), (re.compile("survey", re.I),))
    c = replace(c, events=ev)
    r = rec("Gadget Survey", AT, c)
    assert {r.category, *r.alt_categories} == {"cat_a", "cat_b"}
    w = guard(c, r).windows_at(T0 - M(200))[0]
    assert (w.start, w.end) == (T0 - M(45), T0 + M(75))


# 10. end-to-end backtest ---------------------------------------------------------------------------
@pytest.mark.parametrize("weak", ["programmatic", "explicit_category"])
def test_10_weaker_custom_category_cannot_authorize_an_entry(weak):
    from src.backtest.engine import BacktestEngine
    from src.backtest.orders import OrderRequest
    from tests.backtest_helpers import GLOBEX, MES, flat_bars, frame, make_config
    from tests.test_news_engine import Recorder

    c = custom_cfg(0, 0, Impact.LOW, ("widget index",))
    when = "2025-03-10T10:05:00"  # protected 09:35 .. 10:35 (30/30)
    r = rec("Widget Index", when, c) if weak == "programmatic" else rec("Consumer Price Index", when, c,
                                                                         category="my_custom")
    g = NewsGuard(c, store(r, cfg=c), "MES")
    script = {"09:40": [OrderRequest.enter("long", stop_loss=4990, client_id="in_window")],
              "09:41": [OrderRequest.exit()],
              "10:40": [OrderRequest.enter("long", stop_loss=4990, client_id="after_window")]}
    res = BacktestEngine(make_config(), MES, GLOBEX).run(Recorder(script), frame(flat_bars("09:00", 120)), news_guard=g)
    assert list(res.trades.signal_id) == ["after_window"]
    assert "news_blackout_active" in set(res.rejections.reason)
