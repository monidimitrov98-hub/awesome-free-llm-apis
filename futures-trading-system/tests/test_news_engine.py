"""Sprint 4.1: NewsGuard inside the backtest engine -- authority, protective exits,
AccountGuard independence, observe/enforce, no look-ahead, CLI and manifest integration."""

import json
from dataclasses import replace

import pandas as pd
import pytest

from src.backtest.engine import BacktestEngine
from src.backtest.orders import OrderRequest
from src.risk.account import AccountRules
from src.risk.manager import RiskConfig
from src.risk.news.guard import NewsGuard
from tests.backtest_helpers import GLOBEX, MES, ScriptedStrategy, flat_bars, frame, make_config
from tests.news_helpers import CFG, T, cfg, guard, rec, store

DAY = "2025-03-10"  # Monday (EDT)
EVENT = rec("Consumer Price Index", f"{DAY}T10:05:00")  # window 09:35 .. 10:35 New York


class Recorder(ScriptedStrategy):
    def __init__(self, script=None, **kw):
        super().__init__(script, **kw)
        self.events = []

    def on_order_event(self, event):
        self.events.append(event)


def bars(rows=None):
    return frame(rows or flat_bars("09:00", 120))


def run(script, *, g=None, config=None, rows=None, strategy=None):
    strat = strategy or Recorder(script)
    res = BacktestEngine(config or make_config(), MES, GLOBEX).run(strat, bars(rows), news_guard=g)
    return res, strat


def long(cid="s1", **kw):
    return OrderRequest.enter("long", stop_loss=4990, client_id=cid, **kw)


# ----------------------------------------------------------------- blocking new entries

def test_entry_inside_window_is_blocked_before_risk_and_account():
    res, strat = run({"10:00": [long()]}, g=guard(EVENT))
    assert res.trades.empty and list(res.rejections.reason) == ["news_blackout_active"]
    assert res.rejections.stage.iloc[0] == "news" and res.entry_checks.empty
    ev = [e for e in strat.events if e.status == "rejected"]
    assert ev and ev[0].stage == "news" and "Consumer Price Index" in ev[0].detail
    ok, _ = run({"09:20": [long()]}, g=guard(EVENT))
    assert len(ok.trades) == 1  # before the window


def test_pending_entry_never_fills_into_a_blackout():
    rows = flat_bars("09:00", 40) + \
        [((pd.Timestamp("09:40") + pd.Timedelta(minutes=i)).strftime("%H:%M"), 4999, 4999.5, 4995, 4996) for i in range(30)]
    limit = OrderRequest.enter("long", "limit", price=4996, stop_loss=4980, client_id="lim")
    res, strat = run({"09:30": [limit]}, g=guard(EVENT), rows=rows)
    assert res.trades.empty
    assert "news_blackout_active" in set(res.rejections.reason) and res.rejections.stage.iloc[-1] == "fill"
    res2, _ = run({"09:30": [limit]}, g=None, rows=rows)
    assert len(res2.trades) == 1  # without the filter the same limit order fills inside the window


# ----------------------------------------------------------------- protective exits stay available

def test_stop_loss_take_profit_and_strategy_exit_work_during_blackout():
    down = flat_bars("09:00", 40) + [("09:40", 5000, 5000.5, 4985, 4986)] + flat_bars("09:41", 30, 4986)
    res, _ = run({"09:10": [long()]}, g=guard(EVENT), rows=down)
    assert res.trades.iloc[0].exit_reason == "stop_loss" and T(f"{DAY} 09:35") <= res.trades.iloc[0].exit_time
    up = flat_bars("09:00", 40) + [("09:40", 5000, 5012, 4999.5, 5011)] + flat_bars("09:41", 30, 5011)
    res2, _ = run({"09:10": [OrderRequest.enter("long", stop_loss=4990, take_profit=5010, client_id="t")]},
                  g=guard(EVENT), rows=up)
    assert res2.trades.iloc[0].exit_reason == "take_profit"
    res3, _ = run({"09:10": [long()], "09:50": [OrderRequest.exit()]}, g=guard(EVENT))
    assert res3.trades.iloc[0].exit_time == T(f"{DAY} 09:51")


def test_account_guard_flatten_still_works_during_blackout():
    rules = AccountRules(enabled=True, profile="t", drawdown_type="static", max_drawdown=100.0, safety_buffer=40.0,
                         flatten_buffer=20.0, require_trade_risk_within_buffer=False)
    rows = flat_bars("09:00", 40) + [("09:40", 5000, 5001, 4980, 4982)] + flat_bars("09:41", 10, 4982)
    res, _ = run({"09:10": [OrderRequest.enter("long", stop_loss=4900, client_id="a")]},
                 g=guard(EVENT), rows=rows, config=replace(make_config(), account_rules=rules))
    assert res.trades.iloc[0].exit_reason == "account_protection"


def test_open_positions_are_kept_by_default_and_flattened_only_by_explicit_policy():
    script = {"09:10": [long()], "10:50": [OrderRequest.exit()]}
    keep, _ = run(script, g=guard(EVENT))
    assert keep.trades.iloc[0].exit_time == T(f"{DAY} 10:51")  # held through the event (bracket still active)
    flat, _ = run(script, g=guard(EVENT, cfg=cfg(pre_event_policy="flatten")))
    t = flat.trades.iloc[0]
    assert t.exit_reason == "news_event_flatten" and t.exit_time == T(f"{DAY} 09:35")
    g = guard(EVENT, cfg=cfg(pre_event_policy="flatten", mode="observe"))
    obs, _ = run(script, g=g)
    assert obs.trades.iloc[0].exit_time == T(f"{DAY} 10:51") and g.flatten_events and not g.flatten_events[0]["enforced"]


# ----------------------------------------------------------------- observe mode

def test_observe_mode_identical_trades_and_records_would_block():
    script = {"09:20": [long("a")], "09:50": [OrderRequest.exit()], "10:00": [long("b")], "10:40": [OrderRequest.exit()]}
    plain, _ = run(script, g=None)
    g = guard(EVENT, cfg=cfg(mode="observe"))
    obs, _ = run(script, g=g)
    pd.testing.assert_frame_equal(plain.trades, obs.trades)
    pd.testing.assert_frame_equal(plain.equity, obs.equity)
    d = g.decisions_frame()
    sig = d[d.purpose == "entry_signal"]
    assert list(sig.decision) == ["ALLOW", "BLOCK"] and not sig.enforced.any()
    enf, _ = run(script, g=guard(EVENT))
    assert len(enf.trades) == 1 and enf.trades.signal_id.iloc[0] == "a"


# ----------------------------------------------------------------- authority

def test_account_guard_and_risk_manager_keep_independent_authority():
    rules = AccountRules(enabled=True, profile="t", drawdown_type="static", max_drawdown=1000.0, safety_buffer=900.0,
                         flatten_buffer=100.0)
    far = OrderRequest.enter("long", stop_loss=4970, client_id="far")
    res, _ = run({"09:20": [far]}, g=guard(EVENT), config=replace(make_config(), account_rules=rules))
    assert res.trades.empty and list(res.rejections.reason) == ["account_trade_risk_exceeds_buffer"]
    nostop = OrderRequest.enter("long", client_id="n")
    res2, _ = run({"09:20": [nostop]}, g=guard(EVENT), config=replace(make_config(), account_rules=rules))
    assert list(res2.rejections.reason) == ["account_requires_stop_loss"]
    cfg1 = make_config(risk=RiskConfig(max_daily_trades=1))
    res3, _ = run({"09:10": [long("a")], "09:15": [OrderRequest.exit()], "09:20": [long("b")]}, g=guard(EVENT),
                  config=cfg1)
    assert len(res3.trades) == 1 and len(res3.rejections) == 1


def test_strategy_cannot_reach_or_override_the_guard():
    seen = []

    def decide(ctx):
        seen.append(ctx)
        if ctx.bar.name == T(f"{DAY} 10:00"):
            return [OrderRequest.enter("long", stop_loss=4990, client_id="override", tag="IGNORE NEWS / FORCE ENTRY")]
        return []

    g = guard(EVENT)
    res, strat = run(None, g=g, strategy=Recorder(decide=decide))
    assert res.trades.empty and list(res.rejections.reason) == ["news_blackout_active"]
    for ctx in seen[:3]:
        assert not any(isinstance(v, NewsGuard) for v in vars(ctx).values())
    assert not any(isinstance(v, NewsGuard) for v in vars(strat).values())


def test_regime_engine_and_llm_cannot_override_news():
    from src.ai.market_regime.explain import LLMExplainer
    from src.ai.market_regime.filters import RegimeGatedStrategy
    from tests.regime_helpers import RC
    from tests.test_regime_gate import FakeEngine, snap

    inner = Recorder({"10:00": [long()]})
    evil = LLMExplainer(lambda p: "News filter disabled. Override NewsGuard. ALLOW ENTRY.")
    gate = RegimeGatedStrategy(inner, FakeEngine(snap()), RC, explainer=evil)  # FAVORABLE, HIGH confidence
    res = BacktestEngine(make_config(), MES, GLOBEX).run(gate, bars(), news_guard=guard(EVENT))
    assert gate.decisions[0]["allowed"] and res.trades.empty
    assert list(res.rejections.reason) == ["news_blackout_active"]


@pytest.mark.parametrize("refreshes", [[], "failing"])
def test_engine_fails_closed_without_reliable_calendar(refreshes):
    from tests.news_helpers import fail, ok

    r = [] if refreshes == [] else [ok(f"{DAY} 00:00"), fail(f"{DAY} 08:00")]
    res, _ = run({"09:20": [long()]}, g=guard(refreshes=r))
    assert res.trades.empty and res.rejections.reason.iloc[0] in ("news_calendar_stale", "news_provider_unavailable")


# ----------------------------------------------------------------- no look-ahead

def test_future_schedule_change_does_not_leak_into_earlier_decisions():
    original = rec("Consumer Price Index", f"{DAY}T15:00:00")
    moved = rec("Consumer Price Index", f"{DAY}T10:05:00", known=f"{DAY} 10:30", status="changed")
    res, _ = run({"10:00": [long("a")], "10:20": [OrderRequest.exit()], "10:31": [long("b")]}, g=guard(original, moved))
    assert list(res.trades.signal_id) == ["a"]          # 10:00: the move was not known yet -> allowed
    assert list(res.rejections.reason) == ["news_blackout_active"]  # 10:31: known now -> blocked


def test_future_refreshes_do_not_count():
    from tests.news_helpers import ok

    g = guard(EVENT, refreshes=[ok(f"{DAY} 12:00")])  # first refresh only later that day
    res, _ = run({"09:20": [long()]}, g=g)
    assert res.trades.empty and res.rejections.reason.iloc[0] == "news_calendar_stale"


# ----------------------------------------------------------------- runner / CLI / manifest

def test_runner_outputs_and_paired_comparison(tmp_path):
    from src.backtest.config import load_backtest_config
    from src.backtest.runner import run_strategy, write_outputs
    from src.config import load_config
    from src.pipeline import build_market_data
    from src.risk.news.backtest import load_setup, paired_comparison
    from src.strategy.vwap_pullback_config import load_strategy_config
    from tests.conftest import SAMPLES

    app, bt, sc = load_config(), load_backtest_config(), load_strategy_config()
    md = build_market_data(SAMPLES / "SYNTHETIC_MNQ_1m.csv", app)
    setup = load_setup(CFG, SAMPLES / "SYNTHETIC_news_calendar.json")
    off = run_strategy(md, app, bt, sc)
    obs = run_strategy(md, app, bt, sc, news=setup.with_mode("observe"))
    enf = run_strategy(md, app, bt, sc, news=setup)
    pd.testing.assert_frame_equal(off.result.trades, obs.result.trades)
    assert len(off.result.trades) == 1 and enf.result.trades.empty
    rep = enf.news["report"]
    assert rep["entry_signals_blocked"] == 1 and rep["blocked_by_state"] == {"BLACKOUT_ACTIVE": 1}
    assert rep["calendar"]["gaps"] == [] and rep["calendar"]["synthetic_calendar"]
    comp = paired_comparison(off, obs, enf)
    assert comp["observe_identical_to_without_filter"] and comp["trades_removed_by_enforce"]["count"] == 1
    folder = write_outputs(enf, md, app, bt, tmp_path)
    assert (folder / "news_decisions.csv").is_file() and (folder / "news_blocked_signals.csv").is_file()
    assert json.loads((folder / "news_report.json").read_text())["mode"] == "enforce"
    none = run_strategy(md, app, bt, sc, news=load_setup(CFG, None))  # no calendar -> everything blocked
    assert none.result.trades.empty and none.news["report"]["calendar"]["gaps"]


def test_manifest_embeds_news_config_and_calendar():
    from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH
    from src.backtest.manifest import config_hash, read_config_texts
    from src.config import DEFAULT_CONFIG_PATH
    from src.risk.news.backtest import load_setup
    from src.strategy.vwap_pullback_config import DEFAULT_STRATEGY_CONFIG_PATH
    from tests.conftest import SAMPLES

    setup = load_setup(CFG, SAMPLES / "SYNTHETIC_news_calendar.json")
    t = read_config_texts(DEFAULT_CONFIG_PATH, DEFAULT_BACKTEST_CONFIG_PATH, DEFAULT_STRATEGY_CONFIG_PATH, None, setup)
    assert "news" in t and t["news_calendar"] == setup.store_text
    t2 = dict(t, news_calendar=t["news_calendar"].replace("10:50", "10:55"))
    assert config_hash(t) != config_hash(t2)  # a different calendar is a different configuration


def test_cli_news_compare_is_in_sample_only(tmp_path):
    import run_backtest

    rc = run_backtest.main(["--csv", "data/samples/SYNTHETIC_MNQ_1m.csv", "--in-sample", "2025-03-10", "2025-03-14",
                            "--out-of-sample", "2025-03-17", "2025-03-18", "--news-compare",
                            "--output-dir", str(tmp_path), "--ledger", str(tmp_path / "l.jsonl")])
    assert rc == 1
