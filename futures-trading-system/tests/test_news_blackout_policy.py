"""Sprint 4.1 final blackout-policy hardening: category-specific MINIMUM windows that no
configuration can weaken (at load time AND at runtime), release-group maximums, and no alias
can capture a mandatory release. Every weakening attempt is rejected or safely overridden."""

import re
from dataclasses import replace

import pandas as pd
import pytest

from src.config import ConfigError
from src.risk.news.config import (CANONICAL_NAMES, DEFAULT_NEWS_CONFIG_PATH, MANDATORY_CATEGORIES,
                                  MANDATORY_WINDOWS, UNKNOWN_FLOOR, load_news_config)
from src.risk.news.guard import NewsGuard, NewsState as S
from tests.news_helpers import CFG, T, rec, store

TEXT = DEFAULT_NEWS_CONFIG_PATH.read_text()
M = lambda m: pd.Timedelta(minutes=m)  # noqa: E731
REQUIRED_TABLE = {  # the policy table of the task, verbatim
    "cpi": (30, 30), "core_cpi": (30, 30), "pce": (30, 30), "core_pce": (30, 30), "nfp": (30, 30),
    "unemployment_rate": (30, 30), "fomc_decision": (60, 60), "fomc_statement": (60, 60),
    "fed_press_conference": (30, 30), "ppi": (15, 15), "gdp": (15, 15), "retail_sales": (15, 15),
    "treasury": (15, 15), "ism_manufacturing": (10, 15), "ism_services": (10, 15), "jobless_claims": (5, 10),
    "jolts": (15, 15), "fed_chair_speech": (15, 45),
}


def section_window(cat, before, after):
    """Rewrite [events.<cat>] before/after in the shipped config."""
    pattern = rf"(\[events\.{cat}\]\n(?:.*\n)*?)before_minutes = [0-9.]+\nafter_minutes = [0-9.]+"
    text, n = re.subn(pattern, rf"\g<1>before_minutes = {before}\nafter_minutes = {after}", TEXT, count=1)
    assert n == 1, cat
    return text


def load(tmp_path, text):
    p = tmp_path / "n.toml"
    p.write_text(text, encoding="utf-8")
    return load_news_config(p)


# ----------------------------------------------------------------- the policy table itself

def test_floors_match_the_policy_table_and_cover_every_mandatory_category():
    assert {k: (v[0].total_seconds() / 60, v[1].total_seconds() / 60) for k, v in MANDATORY_WINDOWS.items()} == \
        {k: (float(a), float(b)) for k, (a, b) in REQUIRED_TABLE.items()}
    assert set(MANDATORY_WINDOWS) == set(MANDATORY_CATEGORIES) <= set(CFG.required_categories)
    assert set(CANONICAL_NAMES) == set(MANDATORY_CATEGORIES)
    for cat, (b, a) in REQUIRED_TABLE.items():  # shipped config is at least the floor
        assert CFG.window("MES", cat) >= (M(b), M(a)) and CFG.window("MNQ", cat) >= (M(b), M(a))


# ----------------------------------------------------------------- weakening attempts (load time)

@pytest.mark.parametrize("cat,before,after", [
    ("cpi", 0, 0), ("cpi", 1, 1), ("cpi", 5, 5), ("cpi", 15, 15), ("cpi", 30, 0), ("cpi", 0, 30),
    ("core_cpi", 15, 30), ("fomc_decision", 5, 5), ("fomc_decision", 30, 30), ("fomc_decision", 60, 0),
    ("fomc_statement", 30, 60), ("fed_press_conference", 15, 30), ("pce", 29, 30), ("nfp", 30, 29.5),
    ("ppi", 10, 15), ("gdp", 15, 5), ("retail_sales", 0, 15), ("treasury", 15, 14),
    ("ism_manufacturing", 9, 15), ("ism_services", 10, 10), ("jobless_claims", 4, 10), ("jobless_claims", 5, 9),
    ("jolts", 10, 10), ("fed_chair_speech", 15, 30), ("fed_chair_speech", 0, 45),
])
def test_weaker_windows_are_rejected(tmp_path, cat, before, after):
    with pytest.raises(ConfigError, match="mandatory minimum"):
        load(tmp_path, section_window(cat, before, after))


@pytest.mark.parametrize("cat,before,after", [("cpi", 45, 60), ("fomc_decision", 90, 120), ("jobless_claims", 5, 10)])
def test_wider_windows_are_allowed(tmp_path, cat, before, after):
    c = load(tmp_path, section_window(cat, before, after))
    assert c.window("MES", cat) == (M(before), M(after))


@pytest.mark.parametrize("override", ["cpi = [0, 0]", "cpi = [30, 5]", "fomc_decision = [30, 30]",
                                      "fed_chair_speech = [15, 15]", "jolts = [15, 10]"])
def test_instrument_overrides_cannot_weaken(tmp_path, override):
    text = TEXT.replace('[instruments.MNQ]\nregions = ["US"]', f'[instruments.MNQ]\nregions = ["US"]\noverrides = {{ {override} }}')
    with pytest.raises(ConfigError, match="mandatory minimum"):
        load(tmp_path, text)


def test_removing_a_category_definition_is_rejected(tmp_path):
    text, n = re.subn(r"\[events\.jolts\]\n(?:.+\n)*\n", "", TEXT, count=1)  # the whole section
    assert n == 1
    with pytest.raises(ConfigError, match="jolts"):
        load(tmp_path, text)
    text2, n = re.subn(r'(\[events\.cpi\]\n)patterns = \[[^\]]*\]', r'\1patterns = []', TEXT, count=1)
    assert n == 1
    with pytest.raises(ConfigError, match="must classify as mandatory category 'cpi'"):
        load(tmp_path, text2)  # patterns removed: CPI releases would fall back to weaker handling


def test_dropping_a_category_from_required_is_rejected(tmp_path):
    text = TEXT.replace('"retail_sales", "jolts"]', '"retail_sales"]')
    with pytest.raises(ConfigError, match="mandatory"):
        load(tmp_path, text)


@pytest.mark.parametrize("alias", [
    '[events.inflation_alias]\npatterns = ["consumer price index"]\nimpact = "low"\nbefore_minutes = 0\nafter_minutes = 0\n',
    '[events.a_fed]\npatterns = ["fomc", "federal funds"]\nimpact = "medium"\nbefore_minutes = 5\nafter_minutes = 5\n',
    '[events.jobs]\npatterns = ["employment situation", "payroll"]\nimpact = "low"\nbefore_minutes = 0\nafter_minutes = 0\n',
])
def test_new_alias_cannot_capture_a_mandatory_release(tmp_path, alias):
    text = TEXT.replace("# Event categories:", alias + "\n# Event categories:", 1)  # defined FIRST in the file
    with pytest.raises(ConfigError, match="30 min before / 30 min after"):
        load(tmp_path, text)  # weaker than 30/30: refused outright
    text = re.sub(r"(before|after)_minutes = [0-9]+", r"\1_minutes = 30", alias)
    text = TEXT.replace("# Event categories:", text + "\n# Event categories:", 1)
    c = load(tmp_path, text)  # a 30/30 alias may exist ...
    from src.risk.news.classifier import make_classifier

    classify = make_classifier(c)  # ... but mandatory categories always win classification
    for cat, names in CANONICAL_NAMES.items():
        for name in names:
            assert classify(name, "US")[0] == cat


def test_alias_capturing_what_mandatory_patterns_miss_falls_to_unknown_floor_or_its_own_window(tmp_path):
    # a release no mandatory pattern recognises keeps the conservative unknown fallback unless the
    # user defines a category for it; undefined categories never get a window below 30/30
    assert CFG.window("MES", "undefined_category") == UNKNOWN_FLOOR


@pytest.mark.parametrize("text_change", [
    lambda t: t.replace("[events.cpi]\n", "[events.cpi]\nbefore_minutes = 1\n", 1),          # duplicate key
    lambda t: t + "\n[events.cpi]\npatterns = []\nimpact = \"low\"\nbefore_minutes = 0\nafter_minutes = 0\n",  # duplicate table
])
def test_contradictory_values_for_the_same_event_are_rejected(tmp_path, text_change):
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load(tmp_path, text_change(TEXT))


@pytest.mark.parametrize("change,match", [
    (lambda t: section_window("cpi", 30, 30).replace("max_age_minutes = 1440", "max_age_minutes = 1440"), None),
    (lambda t: section_window("fomc_decision", 59, 60), "mandatory minimum"),
    (lambda t: t.replace("before_minutes = 30\nafter_minutes = 30\n\n# Event", "before_minutes = 20\nafter_minutes = 20\n\n# Event"), "unknown_impact"),
])
def test_valid_looking_configurations(tmp_path, change, match):
    if match is None:
        load(tmp_path, change(TEXT))  # an equal-to-floor config is fine
    else:
        with pytest.raises(ConfigError, match=match):
            load(tmp_path, change(TEXT))


# ----------------------------------------------------------------- runtime enforcement (defence in depth)

def weakened(**windows):
    ev = dict(CFG.events)
    for cat, (b, a) in windows.items():
        ev[cat] = replace(ev[cat], before=M(b), after=M(a))
    return replace(CFG, events=ev)  # bypasses the loader on purpose


@pytest.mark.parametrize("cat,name,when", [
    ("cpi", "Consumer Price Index", "2025-03-12T08:30:00"),
    ("fomc_decision", "FOMC rate decision", "2025-03-19T14:00:00"),
    ("jobless_claims", "Initial Jobless Claims", "2025-03-13T08:30:00"),
])
def test_runtime_clamps_a_programmatically_weakened_config(cat, name, when):
    c = weakened(**{cat: (0, 0)})
    b, a = MANDATORY_WINDOWS[cat]
    t = pd.Timestamp(when).tz_localize("America/New_York").tz_convert("UTC")
    g = NewsGuard(c, store(rec(name, when)), "MES")
    assert g.status(t - b).state is S.BLACKOUT_ACTIVE and g.status(t + a - M(1)).state is S.BLACKOUT_ACTIVE
    assert not g.check_entry(t - b, purpose="entry_signal").allowed
    insts = dict(c.instruments)
    insts["MES"] = {"regions": ["US"], "overrides": {cat: (M(0), M(0))}}
    g2 = NewsGuard(replace(c, instruments=insts), store(rec(name, when)), "MES")
    assert g2.status(t - b).state is S.BLACKOUT_ACTIVE


def test_release_group_gets_the_most_conservative_window():
    c = weakened(core_cpi=(90, 120))  # widen one member
    assert c.window("MES", "cpi") == (M(90), M(120)) == c.window("MES", "core_cpi")
    c2 = weakened(fomc_statement=(75, 90))
    assert c2.window("MES", "fomc_decision") == (M(75), M(90))
    t = pd.Timestamp("2025-03-12 08:30", tz="America/New_York").tz_convert("UTC")
    g = NewsGuard(c, store(rec("Consumer Price Index", "2025-03-12T08:30:00")), "MES")
    assert g.status(t - M(89)).state is S.BLACKOUT_ACTIVE and g.status(t + M(119)).state is S.BLACKOUT_ACTIVE
    # a CPI listed under both categories at the same time produces one merged, widest window
    both = NewsGuard(c, store(rec("Consumer Price Index", "2025-03-12T08:30:00"),
                              rec("Core CPI", "2025-03-12T08:30:00")), "MES")
    w = both.windows_at(t - M(200))
    assert len(w) == 1 and (w[0].start, w[0].end) == (t - M(90), t + M(120))


def test_weaker_configuration_cannot_authorize_a_trade_in_the_engine():
    from src.backtest.engine import BacktestEngine
    from src.backtest.orders import OrderRequest
    from tests.backtest_helpers import GLOBEX, MES, flat_bars, frame, make_config
    from tests.test_news_engine import Recorder

    c = weakened(cpi=(0, 0), core_cpi=(0, 0))
    g = NewsGuard(c, store(rec("Consumer Price Index", "2025-03-10T10:05:00")), "MES")
    res = BacktestEngine(make_config(), MES, GLOBEX).run(
        Recorder({"09:40": [OrderRequest.enter("long", stop_loss=4990, client_id="x")]}),
        frame(flat_bars("09:00", 120)), news_guard=g)
    assert res.trades.empty and list(res.rejections.reason) == ["news_blackout_active"]
