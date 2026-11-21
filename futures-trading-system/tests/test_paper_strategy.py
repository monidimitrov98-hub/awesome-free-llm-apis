"""Sprint 5: integration with the EXISTING strategy and regime engine -- decision parity with the
backtest on identical bars, a real authorized trade, no look-ahead, incomplete bars skipped."""

from dataclasses import replace
from functools import lru_cache

import pandas as pd
import pytest

from src.ai.market_regime.config import load_regime_config
from src.backtest.config import load_backtest_config
from src.backtest.runner import run_strategy
from src.paper.feeds import BarReplayFeed
from src.paper.journal import Journal
from src.paper.runtime import PaperSession
from src.pipeline import build_market_data
from src.risk.news.backtest import load_setup
from src.risk.news.config import load_news_config
from tests.conftest import SAMPLES
from tests.paper_helpers import APP, PCFG, RC_OFF, SC, T, allow_news, long, path, session

DAY = pd.Timestamp("2025-03-17")


@lru_cache(maxsize=1)
def sample():
    md = build_market_data(SAMPLES / "SYNTHETIC_MNQ_1m.csv", APP)
    sd = md.frames["1min"]["session_date"]
    live = (sd == DAY).to_numpy()
    before = (sd < DAY).to_numpy()
    return md, md.base[before], md.base[live]


def paper_run(tmp, regime, news):
    md, warm, live = sample()
    pcfg = replace(PCFG, instruments=("MNQ",))
    s = PaperSession(pcfg, APP, load_backtest_config(), SC, regime, news, BarReplayFeed({"MNQ": live}),
                     Journal(tmp / "state.db", create=True), warmup={"MNQ": warm})
    s.run()
    return s


@pytest.fixture(scope="module")
def regime_parity(tmp_path_factory):
    rc = load_regime_config()
    setup = load_setup(load_news_config(), SAMPLES / "SYNTHETIC_news_calendar.json")
    s = paper_run(tmp_path_factory.mktemp("parity"), rc, {"MNQ": setup.guard("MNQ")})
    md = sample()[0]
    bt = run_strategy(md, APP, load_backtest_config(), SC, regime=rc, news=setup)
    return s, bt


def test_strategy_decisions_identical_to_backtest(regime_parity):
    s, bt = regime_parity
    paper = pd.DataFrame([e["payload"] for e in s.journal.entries("strategy_decision")])
    d = bt.diagnostics
    d = d[pd.to_datetime(d["session_date"]) == DAY]
    key = lambda f: list(zip(pd.to_datetime(f["bar_time"], utc=True), f["direction"], f["decision"],  # noqa: E731
                             f["rejection_reason"].fillna("")))
    # the replay's last 5-minute bar never completes (no later event or clock tick): it is not decided
    last = pd.to_datetime(d["bar_time"], utc=True).max()
    d_done = d[pd.to_datetime(d["bar_time"], utc=True) < last]
    assert len(paper) == len(d_done) > 500 and len(d) - len(d_done) == 2
    assert key(paper) == key(d_done)
    assert (paper["decision"] == "signal").sum() == 1


def test_regime_gate_decision_identical_to_backtest(regime_parity):
    s, bt = regime_parity
    paper = [e["payload"] for e in s.journal.entries("regime_decision")]
    btd = bt.regime["decisions"]
    assert [p["suitability"] for p in paper] == list(btd["suitability"]) == ["UNFAVORABLE"]
    assert [p["signal_id"] for p in paper] == list(btd["signal_id"])
    assert s.decisions[0]["reason"] == "regime_unfavorable" and not s.broker.orders


def test_real_strategy_signal_is_authorized_then_rechecked_at_the_actual_fill(tmp_path):
    s = paper_run(tmp_path, RC_OFF, allow_news(("MNQ",)))
    md = sample()[0]
    bt = run_strategy(md, APP, load_backtest_config(), SC, regime=RC_OFF)
    bt_trade = bt.result.trades[pd.to_datetime(bt.result.trades["session_date"]) == DAY].iloc[0]
    assert [d["allowed"] for d in s.decisions] == [True]
    o = next(o for o in s.broker.orders.values() if o.role == "entry")
    assert o.client_id == bt_trade.signal_id                         # same signal as the backtest
    assert o.stop_loss == bt_trade.stop_loss and o.take_profit == bt_trade.take_profit
    fa = [e["payload"] for e in s.journal.entries("fill_authorization")][0]
    assert fa["event_id"].endswith("-1")                             # the event AFTER the decision event (-0)
    # The backtest fills at the next bar open; paper fills on the next market event, here the bar's
    # adverse extreme. At that ACTUAL price the strategy's minimum reward/risk is not met, so the entry
    # is rejected -- never converted into a fill.
    assert not fa["allowed"] and fa["reason"] == "reward_risk_below_min_at_fill"
    assert o.state.value == "rejected" and not list(s.journal.entries("execution"))
    assert not s.account.closed_trades


def test_no_look_ahead_in_contexts(tmp_path):
    seen = {}
    s = session(tmp_path, path(T("10:00"), 30, lambda i: 5000.0 + (i % 7) * 0.25), {}, strategies=seen)
    s.run()
    ctxs = seen["MES"].contexts
    assert len(ctxs) == 5  # 10:05..10:25; the 10:30 bar never completes (data ends at 10:29)
    for c in ctxs:
        assert c.history.index[-1] + pd.Timedelta(minutes=5) == c.now
        assert (pd.DatetimeIndex(c.history["available_at"]) <= c.now).all()
        for f in c.context.values():
            assert (pd.DatetimeIndex(f["available_at"]) <= c.now).all()


def test_incomplete_signal_bar_is_never_traded(tmp_path):
    evs = [e for e in path(T("10:00"), 15, lambda i: 5000.0) if not (T("10:07") <= e.ts < T("10:08"))]  # missing minute
    seen = {}
    s = session(tmp_path, evs, {T("10:10"): [long("x")]}, strategies=seen)
    s.run()
    skipped = [e["payload"] for e in s.journal.entries("signal_bar_skipped")]
    assert [p["bar_time"] for p in skipped] == [str(T("10:05"))]
    assert all(c.now != T("10:10") for c in seen["MES"].contexts) and not s.decisions
