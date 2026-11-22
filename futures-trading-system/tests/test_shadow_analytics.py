"""Sprint 8 shadow analytics: statistics, verified journal reading, session isolation, MES/MNQ separation,
DST session dates, data gaps, regimes, blocked signals (hypothetical), cost scenarios, account survival,
live-vs-replay compatibility, validation status, exports, CLI and terminal API.

Synthetic journals are written with the real ``Journal`` class (same hash chain as trading); one real
replay run (deterministic TEST strategy, synthetic sample data) checks the end-to-end pipeline."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import time
from pathlib import Path

import pandas as pd
import pytest

from src.analytics.shadow import stats
from src.analytics.shadow.events import JournalSource
from src.analytics.shadow.library import (MAX_ROWS, AnalyticsLibrary, Filters, export_meta, export_rows,
                                          load_analytics_config, resolve_journals, safe_cell, to_csv, to_json)
from src.analytics.shadow.model import session_date
from src.analytics.shadow.reports import BLOCK_CATEGORIES, HYPOTHETICAL, SIMULATED, categorize
from src.config import ConfigError, PROJECT_ROOT
from src.paper.journal import KEY_ENV, Journal, JournalError

CFG = load_analytics_config()
CFG_FAST = {**CFG, "statistics": {**CFG["statistics"], "bootstrap_iterations": 200}}
WALL = pd.Timestamp("2026-11-20T12:00:00Z")


# ============================================================================ synthetic journal builder
def ident(session_type="SHADOW", sha="a" * 64, regime=None, test=False, provider="NinjaTrader 8 bridge", trusted=True):
    return {"schema": 1, "session_type": session_type, "data_label": "LIVE" if session_type == "SHADOW" else "HISTORICAL",
            "provider": provider, "test_strategy": test,
            "strategy": {"names": {"MES": "vwap_trend_pullback", "MNQ": "vwap_trend_pullback"}, "config_sha256": sha,
                         "version": f"vwap_trend_pullback@{sha[:12]}", "signal_timeframe": "5min", "context_timeframe": "15min"},
            "code": {"trusted": trusted, "commit": "abc", "source_sha256": "c" * 64},
            "regime": regime or {"enabled": True, "config_sha256": "r" * 64, "model_version": "rules-v1"},
            "contracts": {"MES": {"tick_size": 0.25, "point_value": 5.0}, "MNQ": {"tick_size": 0.25, "point_value": 2.0}},
            "session_rules_sha256": "s" * 64, "costs": {"sha256": "k" * 64}, "account_profile": {"profile": "generic_example"}}


class JB:
    """Writes a realistic journal (payload shapes as journaled by PaperSession)."""

    def __init__(self, path: Path, identity: dict | None = None, *, with_identity=True):
        self.j = Journal(path, create=True)
        self.path = path
        self.n = 0
        # like PaperSession: created at wall-clock time (microseconds make every run's first row -- its id -- unique)
        self.j.append("session_created", pd.Timestamp.now(tz="UTC"), {"run_kind": "shadow", "instruments": ["MES", "MNQ"], "initial_balance": 10000.0,
                                                "news_readiness": {"verdict": "OK"}})
        if with_identity:
            self.j.append("run_identity", WALL, identity or ident())

    def quote(self, root, ts, bid, ask):
        ts = pd.Timestamp(ts)
        self.j.append("market_event", ts, {"root": root, "ts": ts.isoformat(), "kind": "quote", "bid": bid, "ask": ask, "instrument": root})

    def trade_ev(self, root, ts, last):
        ts = pd.Timestamp(ts)
        self.j.append("market_event", ts, {"root": root, "ts": ts.isoformat(), "kind": "trade", "last": last, "size": 1})

    def bars(self, root, start, closes, spread=1.0):
        t = pd.Timestamp(start)
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c
            self.j.add_bar(root, t + pd.Timedelta(minutes=i), (o, max(o, c) + spread, min(o, c) - spread, c, 10.0))

    def bar(self, root, ts, o, h, l, c):
        self.j.add_bar(root, pd.Timestamp(ts), (o, h, l, c, 10.0))

    def signal(self, root, ts, direction="long", close=5000.0, stop=4996.0, target=5006.0, regime=None, allowed=True,
               reason="", sid=None):
        ts = pd.Timestamp(ts)
        sid = sid or f"vwap:{direction}:{root}:{ts.isoformat()}"
        self.j.append("strategy_decision", ts, {"root": root, "decision": "signal", "signal_id": sid, "time": ts.isoformat(),
                                                "direction": direction, "close": close, "stop_price": stop, "target_price": target,
                                                "stop_distance": abs(close - stop), "target_distance": abs(target - close),
                                                "rsi": 55.0, "rvol": 1.2, "trend_strength_atr": 0.8})
        if regime is not None:
            self.j.append("regime_decision", ts, {"root": root, "signal_id": sid, **regime})
        self.j.append("entry_decision", ts, {"root": root, "signal_id": sid, "side": direction, "allowed": allowed,
                                             "reason": reason, "stages": []})
        return sid

    def reject(self, root, ts, reason):
        ts = pd.Timestamp(ts)
        self.j.append("strategy_decision", ts, {"root": root, "decision": "reject", "reasons": reason, "time": ts.isoformat()})

    def trade(self, root, sid, entry_ts, exit_ts, entry_px, exit_px, net, side="long", stop=None, target=None,
              exit_role="take_profit", qty=1):
        self.n += 1
        e_ts, x_ts = pd.Timestamp(entry_ts), pd.Timestamp(exit_ts)
        oid = f"N{self.n:07d}"
        buy = side == "long"
        self.j.append("order_submitted", e_ts, {"kind": "submitted", "order_id": oid, "root": root, "details": {
            "order_id": oid, "client_id": sid, "role": "entry", "root": root, "order_type": "market", "is_buy": buy,
            "quantity": qty, "reference_price": entry_px, "stop_loss": stop, "take_profit": target}})
        self.j.append("execution", e_ts, {"order_id": oid, "root": root, "quantity": qty, "ts": e_ts.isoformat(),
                                          "fill_price": entry_px, "reference_price": entry_px, "reason": "market_at_quote",
                                          "quote_based": True, "synthetic_data": False, "slippage_ticks": 0, "slippage_cost": 0.0})
        self.j.append("order_filled", e_ts, {"kind": "filled", "order_id": oid})
        gross = net + 1.0
        risk = abs(entry_px - stop) * (5.0 if root == "MES" else 2.0) if stop is not None else None
        self.j.append("trade_closed", x_ts, {"root": root, "signal_id": sid, "side": side, "quantity": qty,
                                             "entry_time": e_ts.isoformat(), "exit_time": x_ts.isoformat(),
                                             "entry_price": entry_px, "exit_price": exit_px, "gross_pnl": gross,
                                             "commission": 1.0, "slippage_cost": 0.0, "net_pnl": net,
                                             "r_multiple": (net / risk) if risk else None, "exit_reason": exit_role,
                                             "exit_role": exit_role})

    def account(self, ts, equity, buffer, hw=10000.0):
        self.j.append("account_update", pd.Timestamp(ts), {"equity": equity, "remaining_buffer": buffer, "high_water": hw,
                                                           "drawdown_floor": hw - 2000, "max_drawdown": hw - equity,
                                                           "guard": {"breached": False}})

    def raw(self, kind, ts, payload):
        self.j.append(kind, pd.Timestamp(ts), payload)

    def done(self) -> Path:
        self.j.commit()
        self.j.close()
        return self.path


def lib_of(*paths) -> AnalyticsLibrary:
    return AnalyticsLibrary([Path(p) for p in paths], CFG_FAST)


def simple_run(tmp_path, name="shadow", session_type="SHADOW", day="2026-11-18", root="MES", nets=(25.0, -20.0, 15.0), sha="a" * 64,
               regime_rec=None, test=False):
    b = JB(tmp_path / name / "paper_state.db", ident(session_type, sha=sha, test=test))
    base = pd.Timestamp(f"{day}T15:00:00Z")
    for i, net in enumerate(nets):
        t = base + pd.Timedelta(minutes=10 * i)
        b.quote(root, t, 5000.0, 5000.25)
        sid = b.signal(root, t, regime=regime_rec)
        b.trade(root, sid, t, t + pd.Timedelta(minutes=5), 5000.25, 5005.0 if net > 0 else 4996.0, net, stop=4996.0, target=5006.0)
    return b.done()


# ============================================================================ statistics
def test_wilson_t_interval_and_profit_factor():
    lo, hi = stats.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert isinstance(lo, float) and stats.wilson(0, 0) is None
    assert stats.wilson(0, 5)[0] == 0.0 and stats.wilson(5, 5)[1] == 1.0
    lo, hi = stats.t_interval([1.0, 2.0, 3.0, 4.0])
    assert lo < 2.5 < hi
    assert stats.t_interval([1.0]) is None
    assert stats.profit_factor([10, 20]) is None  # undefined without losses, never "infinite"
    assert stats.profit_factor([10, -5]) == 2.0


def test_cluster_bootstrap_is_seeded_and_guards_degenerate_inputs():
    vals, days = [1.0, -2.0, 3.0, 0.5, -1.0, 2.0], ["d1", "d1", "d2", "d2", "d3", "d3"]
    a = stats.cluster_bootstrap(vals, days, lambda s: sum(s) / len(s), 300, seed=1)
    assert a == stats.cluster_bootstrap(vals, days, lambda s: sum(s) / len(s), 300, seed=1)
    assert stats.cluster_bootstrap(vals, days, lambda s: 1.0, 0) is None       # n_boot 0
    assert stats.cluster_bootstrap(vals, ["d"] * 6, lambda s: 1.0, 100) is None  # one cluster: no interval
    assert stats.cluster_bootstrap([1.0], ["d"], lambda s: 1.0, 100) is None


def test_sample_levels_drawdown_and_streaks():
    assert stats.sample_level(0) == "NO DATA"
    assert stats.sample_level(29) == "INSUFFICIENT SAMPLE SIZE"
    assert stats.sample_level(99) == "LIMITED SAMPLE"
    assert stats.sample_level(100).startswith("ADEQUATE") and "not proof" in stats.sample_level(100)
    assert stats.max_drawdown([10, -5, -10, 20, -3]) == 15
    assert stats.max_consecutive_losses([1, -1, -1, 2, -1, -1, -1]) == 3
    s = stats.trade_stats([], n_boot=10)
    assert s["trades"] == 0 and s["win_rate"] is None and s["expectancy"] is None and s["profit_factor"] is None


# ============================================================================ journal reading
def test_reader_verifies_the_chain_and_reports_tampering(tmp_path):
    p = simple_run(tmp_path)
    assert JournalSource(p).refresh().rows > 0
    db = sqlite3.connect(p)
    db.execute("UPDATE journal SET payload = replace(payload, '25.0', '2500.0') WHERE kind='trade_closed'")
    db.commit()
    db.close()
    with pytest.raises(JournalError, match="chain broken"):
        JournalSource(p).refresh()
    lib = lib_of(p)
    assert lib.sessions(Filters())["count"] == 0
    v = lib.validation()
    assert v["overall"] == "FAIL" and v["checks"][0]["status"] == "FAIL"


def _content(p: Path) -> str:
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()
    finally:
        db.close()


def test_reader_is_incremental_and_never_writes(tmp_path):
    p = simple_run(tmp_path)
    before = _content(p)
    src = JournalSource(p)
    rows = src.refresh().rows
    j = Journal(p)
    j.append("trade_closed", pd.Timestamp("2026-11-18T18:00:00Z"), {"root": "MES", "side": "long", "quantity": 1,
             "entry_time": "2026-11-18T17:00:00+00:00", "exit_time": "2026-11-18T18:00:00+00:00", "entry_price": 1.0,
             "exit_price": 2.0, "gross_pnl": 5.0, "commission": 0.0, "slippage_cost": 0.0, "net_pnl": 5.0})
    j.commit()
    j.close()
    after_write = _content(p)
    files = sorted(x.name for x in p.parent.iterdir())
    assert src.refresh().rows == rows + 1 and len(src.data.trades) == 4
    lib = lib_of(p)
    for rep in ("sessions", "performance", "costs", "execution", "regimes", "blocked", "reliability", "account"):
        getattr(lib, rep)(Filters())
    lib.validation()
    lib.close()
    src.close()
    assert _content(p) == after_write != before
    assert sorted(x.name for x in p.parent.iterdir()) == files  # no file created by the analysis


def test_keyed_journal_needs_its_key(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "k" * 32)
    p = simple_run(tmp_path)
    assert JournalSource(p).refresh().rows > 0
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(JournalError, match="HMAC-protected"):
        JournalSource(p).refresh()
    monkeypatch.setenv(KEY_ENV, "x" * 32)
    with pytest.raises(JournalError, match="does not match"):
        JournalSource(p).refresh()


def test_rejected_duplicate_events_and_gaps_are_counted(tmp_path):
    b = JB(tmp_path / "r" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    for i in range(5):
        b.trade_ev("MES", t + pd.Timedelta(seconds=i), 5000.0)
    b.trade_ev("MES", t + pd.Timedelta(minutes=10), 5001.0)  # 10-minute in-session silence
    b.raw("data_rejected", t + pd.Timedelta(seconds=3), {"root": "MES", "reason": "duplicate_event"})
    b.raw("data_rejected", t + pd.Timedelta(seconds=4), {"root": "MES", "reason": "duplicate_event"})
    b.raw("feed_connected", t, {"feed": "bridge", "kind": "live"})
    p = b.done()
    lib = lib_of(p)
    s = lib.sessions(Filters())["sessions"][0]
    assert s["market_events"] == 6 and s["data_gaps"] == 1 and s["data_rejected"] == 2
    assert s["market_data_quality"] == "gaps detected"
    r = lib.reliability(Filters())["runs"][0]
    assert r["invalid_events"] == {"duplicate_event": 2} and r["stale_or_gap_incidents"] == 1
    assert "NOT detectable" in r["sequence_gap_detection"]
    assert set(r["states"]) == {"provider_connected", "bridge_connected", "valid_events", "data_fresh", "system_operational"}


def test_wall_clock_and_pre_clock_rows_do_not_distort_the_timeline(tmp_path):
    b = JB(tmp_path / "r" / "paper_state.db", ident("REPLAY"))  # session_created / identity at WALL (2026)
    b.raw("feed_connected", pd.Timestamp("1970-01-01T00:00:00Z"), {"feed": "replay"})  # replay clock not started yet
    b.trade_ev("MES", "2025-03-12T15:00:00Z", 5000.0)
    b.trade_ev("MES", "2025-03-12T16:00:00Z", 5001.0)
    p = b.done()
    src = JournalSource(p)
    d = src.refresh()
    assert d.first_ts == pd.Timestamp("2025-03-12T15:00:00Z") and d.last_ts == pd.Timestamp("2025-03-12T16:00:00Z")
    r = lib_of(p).reliability(Filters())["runs"][0]
    assert r["observation_seconds"] == 3600 and r["data_availability_pct"] is not None


# ============================================================================ sessions, isolation, separation
def test_session_types_are_never_combined(tmp_path):
    rep = simple_run(tmp_path, "replay", "REPLAY", day="2026-11-10", nets=(100.0, 100.0))
    sh = simple_run(tmp_path, "shadow", "SHADOW", day="2026-11-18", nets=(-10.0,))
    lib = lib_of(rep, sh)
    assert lib.sessions(Filters())["session_types"] == ["REPLAY", "SHADOW"]
    for name in ("performance", "costs", "execution", "regimes", "blocked"):
        assert "never combined" in getattr(lib, name)(Filters())["refused"], name
    p = lib.performance(Filters(session_type="SHADOW"))
    assert p["overall"]["trades"] == 1 and p["overall"]["net_pnl"] == -10.0 and p["session_type"] == "SHADOW"
    assert lib.performance(Filters(session_type="REPLAY"))["overall"]["net_pnl"] == 200.0


def test_mes_and_mnq_are_separate(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    sid = b.signal("MES", t)
    b.trade("MES", sid, t, t + pd.Timedelta(minutes=3), 5000.0, 5005.0, 24.0, stop=4996.0, target=5006.0)
    sid = b.signal("MNQ", t, close=20000.0, stop=19990.0, target=20015.0)
    b.trade("MNQ", sid, t, t + pd.Timedelta(minutes=3), 20000.0, 19990.0, -21.0, stop=19990.0, target=20015.0, exit_role="stop_loss")
    lib = lib_of(b.done())
    perf = lib.performance(Filters(session_type="SHADOW"))
    assert set(perf["by_instrument"]) == {"MES", "MNQ"}
    assert perf["by_instrument"]["MES"]["net_pnl"] == 24.0 and perf["by_instrument"]["MNQ"]["net_pnl"] == -21.0
    assert lib.performance(Filters(root="MNQ"))["overall"]["trades"] == 1
    ids = [s["session_id"] for s in lib.sessions(Filters())["sessions"]]
    assert len(ids) == 2 and ids[0].endswith(":MES") and ids[1].endswith(":MNQ")
    with pytest.raises(ValueError):
        Filters(root="ES")


def test_cme_trading_dates_across_dst():
    # 18:30 New York time belongs to the NEXT trading date, before and after the March DST switch
    assert session_date(pd.Timestamp("2025-03-06T23:30:00Z")) == "2025-03-07"   # EST
    assert session_date(pd.Timestamp("2025-03-10T22:30:00Z")) == "2025-03-11"   # EDT
    assert session_date(pd.Timestamp("2025-03-06T22:30:00Z")) is None           # 17:30 EST: daily maintenance break
    assert session_date(pd.Timestamp("2025-11-03T23:30:00Z")) == "2025-11-04"   # after the November switch


def test_sessions_are_dated_by_cme_trading_date(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db")
    b.trade_ev("MES", "2025-03-10T22:30:00Z", 5000.0)  # Monday 18:30 EDT -> Tuesday session
    b.trade_ev("MES", "2025-03-11T14:00:00Z", 5001.0)
    s = lib_of(b.done()).sessions(Filters())["sessions"]
    assert [x["trading_date"] for x in s] == ["2025-03-11"] and s[0]["market_events"] == 2
    assert s[0]["start"] == pd.Timestamp("2025-03-10T22:30:00Z")


# ============================================================================ regimes
def test_regime_buckets_use_only_regime_engine_records(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    strong = {"trend": "strong_bullish", "volatility": "high", "regime_valid": True, "suitability": "FAVORABLE",
              "range": "not_ranging", "volatility_change": "expansion"}
    sid = b.signal("MES", t, regime=strong)
    b.trade("MES", sid, t, t + pd.Timedelta(minutes=2), 5000.0, 5005.0, 24.0, stop=4996.0, target=5006.0)
    sid = b.signal("MES", t + pd.Timedelta(minutes=30), regime={"trend": "unavailable", "regime_valid": False})
    b.trade("MES", sid, t + pd.Timedelta(minutes=30), t + pd.Timedelta(minutes=32), 5000.0, 4996.0, -21.0, stop=4996.0)
    b.raw("strategy_decision", t + pd.Timedelta(minutes=60), {"root": "MES", "decision": "signal", "signal_id": "x3",
                                                               "time": (t + pd.Timedelta(minutes=60)).isoformat(), "ctx_trend": "strong_bullish"})
    b.trade("MES", "x3", t + pd.Timedelta(minutes=60), t + pd.Timedelta(minutes=62), 5000.0, 5001.0, 4.0, stop=4996.0)
    rep = lib_of(b.done()).regimes(Filters())
    by = {x["bucket"]: x for x in rep["buckets"]}
    assert by["strong_trend"]["trades"] == 1 and by["strong_trend"]["net_pnl"] == 24.0
    assert by["high_volatility"]["trades"] == 1 and by["vol_expansion"]["trades"] == 1
    # an invalid regime and a trade with no regime record are "unavailable" -- never inferred from strategy context
    assert by["unavailable"]["trades"] == 2 and rep["trades_without_regime_record"] == 1
    assert by["moderate_trend"]["trades"] == 0 and by["range"]["trades"] == 0


# ============================================================================ blocked signals
def test_block_reasons_are_categorised_exactly():
    assert categorize("news_calendar_stale", BLOCK_CATEGORIES) == "NewsGuard"
    assert categorize("account_drawdown_buffer", BLOCK_CATEGORIES) == "AccountGuard"
    assert categorize("max_daily_loss", BLOCK_CATEGORIES) == "RiskManager"
    assert categorize("regime_unfavorable", BLOCK_CATEGORIES) == "Market regime"
    assert categorize("market_data_stale", BLOCK_CATEGORIES) == "Data health"
    assert categorize("something_new", BLOCK_CATEGORIES) == "Other"


def test_blocked_signals_counterfactual_is_hypothetical_and_never_in_pnl(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    b.signal("MES", t, allowed=False, reason="news_event_window", close=5000.0, stop=4996.0, target=5006.0)
    b.bar("MES", t + pd.Timedelta(minutes=1), 5000.0, 5003.0, 4999.0, 5002.0)
    b.bar("MES", t + pd.Timedelta(minutes=2), 5002.0, 5007.0, 5001.0, 5006.5)   # target first
    t2 = t + pd.Timedelta(minutes=30)
    b.signal("MES", t2, allowed=False, reason="account_drawdown_buffer", close=5000.0, stop=4996.0, target=5006.0)
    b.bar("MES", t2 + pd.Timedelta(minutes=1), 5000.0, 5008.0, 4995.0, 5000.0)  # both levels in one bar
    t3 = t + pd.Timedelta(minutes=90)
    b.signal("MES", t3, allowed=False, reason="max_daily_loss")                  # no later bars
    b.reject("MES", t, "cooldown_active")
    b.reject("MES", t, "rsi_not_turning")
    lib = lib_of(b.done())
    rep = lib.blocked(Filters())
    assert rep["by_category"] == {"NewsGuard": 1, "AccountGuard": 1, "RiskManager": 1}
    assert {r["reason"] for r in rep["by_reason"]} == {"news_event_window", "account_drawdown_buffer", "max_daily_loss"}
    cats = {r["reason"]: r["category"] for r in rep["strategy_condition_failures"]}
    assert cats == {"cooldown_active": "Cooldown", "rsi_not_turning": "Strategy conditions"}
    cf = rep["counterfactual"]
    assert cf["label"] == HYPOTHETICAL and "never added" in cf["method"]
    assert (cf["target_first"], cf["ambiguous_bar"], cf["unavailable"]) == (1, 1, 1)
    assert all(r["label"] == HYPOTHETICAL for r in cf["rows"])
    perf = lib.performance(Filters())
    assert perf["overall"]["trades"] == 0 and perf["overall"]["net_pnl"] == 0.0
    assert sum(s["net_pnl"] for s in lib.sessions(Filters())["sessions"]) == 0.0


# ============================================================================ costs, execution, account
def test_cost_scenarios_are_separate_from_recorded_results(tmp_path):
    p = simple_run(tmp_path, nets=(25.0, -20.0, 15.0, 30.0))
    lib = lib_of(p)
    cfg_before = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in (PROJECT_ROOT / "config").rglob("*") if f.is_file()}
    rec = lib.performance(Filters())["overall"]["net_pnl"]
    c = lib.costs(Filters())
    assert c["label"].startswith("RESEARCH SCENARIOS") and "unchanged" in c["label"]
    nets = [s["net_pnl"] for s in c["scenarios"]]
    assert nets[0] == pytest.approx(rec) == c["baseline_net_pnl"]
    assert all(n <= nets[0] for n in nets[1:]) and nets[-1] < nets[0]
    # 1 extra tick per side on MES = 2 x 0.25 x 5 = 2.50 per trade (4 trades)
    higher = next(s for s in c["scenarios"] if s["scenario"].startswith("higher slippage"))
    assert higher["net_pnl"] == pytest.approx(rec - 4 * 2.5)
    assert lib.performance(Filters())["overall"]["net_pnl"] == rec  # recorded results untouched
    assert cfg_before == {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in (PROJECT_ROOT / "config").rglob("*") if f.is_file()}


def test_execution_quality_is_labelled_simulated_and_spreads_come_from_journaled_quotes(tmp_path):
    lib = lib_of(simple_run(tmp_path))
    e = lib.execution(Filters())
    assert e["label"] == SIMULATED and e["orders"] == 3
    assert e["entry"]["spread_at_fill"]["n"] == 3 and e["entry"]["spread_at_fill"]["median"] == 0.25
    assert e["entry"]["fill_vs_expected_ticks"]["median"] == 0.0
    assert any("no queue position" in n for n in e["notes"])


def test_account_survival_uses_the_configured_profile_and_marks_unavailable_stress(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    b.signal("MES", t, allowed=False, reason="account_drawdown_buffer")
    p = b.done()
    lib = lib_of(p)
    r = lib.account(Filters())["runs"][0]
    assert r["profile"] == "generic_example" and r["account_guard_blocks"] == 1 and "SIMULATED" in r["label"]
    assert all(x["result"] == "UNAVAILABLE" for x in r["stress"]["rows"])  # no buffer recorded yet: nothing invented
    b2 = JB(tmp_path / "s2" / "paper_state.db")
    sid = b2.signal("MES", t)
    b2.trade("MES", sid, t, t + pd.Timedelta(minutes=2), 5000.0, 4996.0, -21.0, stop=4996.0, exit_role="stop_loss")
    b2.account(t + pd.Timedelta(minutes=2), 9979.0, 1979.0)
    r = lib_of(b2.done()).account(Filters())["runs"][0]
    first = r["stress"]["rows"][0]
    assert first["loss_per_trade_used"] == 21.0 and first["buffer_after"] == pytest.approx(1979.0 - 3 * 21.0)
    assert "never guarantees" in r["stress"]["label"]


# ============================================================================ live vs replay
def test_compare_refuses_incompatible_identities(tmp_path):
    rep = simple_run(tmp_path, "replay", "REPLAY", day="2026-11-10", sha="a" * 64)
    sh = simple_run(tmp_path, "shadow", "SHADOW", day="2026-11-18", sha="b" * 64)
    out = lib_of(rep, sh).compare("MES")
    assert out["comparable"] is False and any("strategy_config_sha256 differs" in r for r in out["reasons"])
    assert lib_of(rep).compare("MES")["comparable"] is False  # no shadow side


def test_compare_compatible_sets_per_instrument(tmp_path):
    rep = simple_run(tmp_path, "replay", "REPLAY", day="2026-11-10")
    sh = simple_run(tmp_path, "shadow", "SHADOW", day="2026-11-18", nets=(5.0,))
    out = lib_of(rep, sh).compare("MES")
    assert out["comparable"] is True and out["REPLAY"]["trades"] == 3 and out["SHADOW"]["trades"] == 1
    assert any("should have happened" in n for n in out["notes"])
    assert lib_of(rep, sh).compare("MNQ")["comparable"] is False  # MNQ has no observations: never borrowed from MES


# ============================================================================ validation
def test_validation_flags_chronology_test_strategies_and_missing_identity(tmp_path):
    rep = simple_run(tmp_path, "replay", "REPLAY", day="2026-11-18")
    sh = simple_run(tmp_path, "shadow", "SHADOW", day="2026-11-10", test=True)
    b = JB(tmp_path / "old" / "paper_state.db", with_identity=False)
    b.trade_ev("MES", "2026-11-18T15:00:00Z", 5000.0)
    old = b.done()
    v = {c["check"]: c for c in lib_of(rep, sh, old).validation()["checks"]}
    assert v["chronology replay vs shadow"]["status"] == "WARN"
    assert v["production strategy only"]["status"] == "WARN"
    assert v["run identity recorded"]["status"] == "WARN"
    assert v["look-ahead"]["status"].startswith("PASS")
    ok = {c["check"]: c for c in lib_of(simple_run(tmp_path, "r2", "REPLAY", day="2026-11-01"),
                                        simple_run(tmp_path, "s2", "SHADOW", day="2026-11-19")).validation()["checks"]}
    assert ok["chronology replay vs shadow"]["status"] == "PASS"


def test_untrusted_code_is_flagged(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db", ident(trusted=False))
    b.trade_ev("MES", "2026-11-18T15:00:00Z", 5000.0)
    v = {c["check"]: c for c in lib_of(b.done()).validation()["checks"]}
    assert v["code reproducibility"]["status"] == "WARN"


# ============================================================================ filters, sources, exports
@pytest.mark.parametrize("kw", [{"root": "ES"}, {"start": "2025-1-1"}, {"end": "x"}, {"session_type": "LIVE"},
                                {"strategy_version": "a;drop"}, {"provider": "<script>"}, {"session_id": "../../x"}])
def test_filters_are_validated(kw):
    with pytest.raises(ValueError):
        Filters(**kw)


def test_journal_patterns_stay_inside_the_project():
    for bad in ("/etc/passwd", "../x/*.db", "data/../../x.db"):
        with pytest.raises(ConfigError):
            resolve_journals([bad], 10)


def test_exports_neutralise_formulas_and_carry_provenance(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db", ident(provider="=HYPERLINK(\"http://x\")"))
    b.signal("MES", pd.Timestamp("2026-11-18T15:00:00Z"), allowed=False, reason="=cmd|' /C calc'!A0")
    lib = lib_of(b.done())
    rows, rep = export_rows(lib, "sessions", Filters())
    text = to_csv(rows, export_meta("sessions", Filters(), rep))
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0][0].startswith("# report") and any("limitations" in r[0] for r in parsed[:8] if r)
    assert not any(cell[:1] in ("=", "+", "-", "@") for row in parsed for cell in row)
    assert "'=HYPERLINK" in text
    rows, rep = export_rows(lib, "blocked", Filters())
    assert safe_cell(rows[0]["reason"]).startswith("'=")
    assert safe_cell(-5.0) == -5.0 and safe_cell("-x") == "'-x"
    j = json.loads(to_json({"v": float("nan"), "t": pd.Timestamp("2026-01-01", tz="UTC")}, {"report": "x"}))
    assert j["report"]["v"] is None and j["report"]["t"].startswith("2026-01-01")


def test_exports_are_bounded():
    rows = [{"a": i} for i in range(MAX_ROWS + 50)]
    assert len(to_csv(rows, {}).strip().splitlines()) == MAX_ROWS + 1


def test_analytics_config_rejects_unknown_sections_and_negative_scenarios(tmp_path):
    p = tmp_path / "a.toml"
    p.write_text("[trading]\nx = 1\n")
    with pytest.raises(ConfigError):
        load_analytics_config(p)
    p.write_text('[[scenarios]]\nname = "x"\nextra_slippage_ticks_per_side = -1\n')
    with pytest.raises(ConfigError):
        load_analytics_config(p)


# ============================================================================ CLI
def test_cli_sessions_export_and_errors(tmp_path, monkeypatch, capsys):
    import run_shadow_analytics as cli
    from src.analytics.shadow import library

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(library, "PROJECT_ROOT", tmp_path)
    cfg = tmp_path / "an.toml"
    cfg.write_text((PROJECT_ROOT / "config" / "analytics.toml").read_text().replace(
        'journals = ["data/terminal/shadow/paper_state.db", "data/paper/paper_state.db", "data/analytics/journals/*/paper_state.db"]',
        'journals = ["runs/*/paper_state.db"]'))
    simple_run(tmp_path / "runs", "a", "SHADOW")
    assert cli.main(["--config", str(cfg), "sessions"]) == 0
    assert "1 session(s); types ['SHADOW']" in capsys.readouterr().out
    out = tmp_path / "exp"
    assert cli.main(["--config", str(cfg), "export", "trades", "--session-type", "SHADOW", "--out", str(out)]) == 0
    assert len(list(out.glob("trades_SHADOW_*.csv"))) == 1
    assert cli.main(["--config", str(cfg), "compare"]) == 1  # MES and MNQ are compared separately
    assert cli.main(["--config", str(cfg), "--journal", "/etc/hostname", "sessions"]) == 1
    assert cli.main(["--config", str(cfg), "sessions", "--start", "2026-13-99x"]) == 1
    simple_run(tmp_path / "runs", "b", "REPLAY", day="2026-11-01")
    capsys.readouterr()
    assert cli.main(["--config", str(cfg), "export", "trades", "--out", str(out)]) == 2  # mixed types refused
    assert "never combined" in capsys.readouterr().err


# ============================================================================ end to end: real replay run + terminal API
TOKEN = "t" * 43
BASE = "http://127.0.0.1:8765"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def replay_run(tmp_path_factory):
    from src.terminal.modes import ModeOptions
    from src.terminal.service import TerminalService
    from tests.e2e.e2e_server import EveryNthBarTestStrategy

    d = tmp_path_factory.mktemp("replay")
    cfg = d / "an.toml"
    cfg.write_text((PROJECT_ROOT / "config" / "analytics.toml").read_text().replace(
        'journals = ["data/terminal/shadow/paper_state.db", "data/paper/paper_state.db", "data/analytics/journals/*/paper_state.db"]',
        "journals = []"))
    svc = TerminalService(ModeOptions(mode="replay", speed=1e7, replay_start="2025-03-12", state_dir=d / "state",
                                      strategy_factory=lambda r: EveryNthBarTestStrategy(), tests_disable_regime_gate=True))
    svc.analytics_config = cfg
    svc.start()
    end = time.monotonic() + 300
    while svc.state not in ("stopped", "failed") and time.monotonic() < end:
        time.sleep(0.2)
    assert svc.state == "stopped", svc.error
    yield svc, d / "state" / "paper_state.db"
    svc.stop()


def test_runtime_journals_identity_and_prices(replay_run):
    _, db = replay_run
    j = Journal(db)
    ids = list(j.entries("run_identity"))
    assert len(ids) == 1
    p = ids[0]["payload"]
    assert p["session_type"] == "REPLAY" and p["data_label"] == "SYNTHETIC" and p["test_strategy"] is True
    assert len(p["strategy"]["config_sha256"]) == 64 and p["regime"]["enabled"] is False
    ev = next(j.entries("market_event"))["payload"]
    assert ev["root"] in ("MES", "MNQ") and "last" in ev and ev["synthetic"] is True
    j.close()


def test_end_to_end_replay_analytics(replay_run):
    _, db = replay_run
    lib = AnalyticsLibrary([db], CFG_FAST)
    raw = [e["payload"] for e in Journal(db).entries("trade_closed")]
    ses = lib.sessions(Filters())
    assert ses["session_types"] == ["REPLAY"] and {s["instrument"] for s in ses["sessions"]} == {"MES", "MNQ"}
    assert sum(s["completed_trades"] for s in ses["sessions"]) == len(raw) > 0
    perf = lib.performance(Filters(session_type="REPLAY"))
    assert perf["overall"]["trades"] == len(raw)
    assert perf["overall"]["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in raw))
    assert perf["overall"]["sample"] == "INSUFFICIENT SAMPLE SIZE" or perf["overall"]["trades"] >= 30
    reg = {b["bucket"]: b for b in lib.regimes(Filters())["buckets"]}
    assert reg["unavailable"]["trades"] == len(raw)  # the regime gate was disabled: nothing is inferred
    e = lib.execution(Filters())
    assert e["label"] == SIMULATED and any("synthetic OHLC" in n for n in e["notes"])
    v = {c["check"]: c["status"] for c in lib.validation()["checks"]}
    assert v["production strategy only"] == "WARN" and v["journal integrity"] == "PASS"
    assert lib.blocked(Filters())["counterfactual"]["label"] == HYPOTHETICAL
    lib.close()


def test_terminal_shadow_api(replay_run):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app

    svc, _ = replay_run
    with TestClient(create_app(svc, token=TOKEN, port=8765), base_url=BASE) as c:
        for rep in ("sessions", "performance", "costs", "execution", "regimes", "blocked", "reliability", "account",
                    "validation", "journals"):
            assert c.get(f"/api/shadow/{rep}").status_code == 401
            r = c.get(f"/api/shadow/{rep}", headers=AUTH)
            assert r.status_code == 200, (rep, r.text[:200])
            assert r.json()["meta"]["report"] == rep and "report" in r.json()
        s = c.get("/api/shadow/sessions", headers=AUTH).json()["report"]
        assert s["journals"][0]["run"] == "this terminal (REPLAY)"
        assert c.get("/api/shadow/compare?root=MES", headers=AUTH).json()["report"]["comparable"] is False
        for bad in ("/api/shadow/nope", "/api/shadow/sessions?root=ES", "/api/shadow/sessions?start=2025-1-1",
                    "/api/shadow/sessions?regime=a;b", "/api/shadow/sessions?session_type=LIVE",
                    "/api/shadow/compare", "/api/shadow/compare?root=MES&a=REPLAY&b=REPLAY",
                    "/api/shadow/export/trades.xlsx", "/api/shadow/export/config.csv"):
            assert c.get(bad, headers=AUTH).status_code == 422, bad
        r = c.get("/api/shadow/export/trades.csv?session_type=REPLAY", headers=AUTH)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"] == 'attachment; filename="shadow_trades_replay.csv"'
        assert r.text.startswith('"# report: ""trades"""')
        r = c.get("/api/shadow/export/sessions.json", headers=AUTH)
        assert r.status_code == 200 and json.loads(r.text)["meta"]["report"] == "sessions"
        for method in ("post", "put", "delete", "patch"):
            assert getattr(c, method)("/api/shadow/sessions", headers=AUTH).status_code == 405
        for route in c.app.routes:
            assert (getattr(route, "methods", None) or set()) <= {"GET", "HEAD"}, route.path


def test_terminal_shadow_api_refuses_mixed_export(replay_run, tmp_path):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app

    svc, _ = replay_run
    other = simple_run(tmp_path, "shadow", "SHADOW")
    with TestClient(create_app(svc, token=TOKEN, port=8765), base_url=BASE) as c:
        with svc._analytics_lock:
            svc._library().add(other)
        r = c.get("/api/shadow/export/trades.csv", headers=AUTH)
        assert r.status_code == 409 and "never combined" in r.json()["detail"]
        assert c.get("/api/shadow/performance", headers=AUTH).json()["report"]["refused"]
        with svc._analytics_lock:
            svc._library().sources.pop(str(other)).close()


def test_terminal_shadow_api_reports_a_broken_config(replay_run, tmp_path):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app

    svc, _ = replay_run
    bad = tmp_path / "bad.toml"
    bad.write_text("[trading]\n")
    saved = (svc._analytics, svc.analytics_config)
    svc._analytics, svc.analytics_config = None, bad
    try:
        with TestClient(create_app(svc, token=TOKEN, port=8765), base_url=BASE) as c:
            r = c.get("/api/shadow/sessions", headers=AUTH)
            assert r.status_code == 503 and "configuration" in r.json()["detail"]
    finally:
        svc._analytics, svc.analytics_config = saved


def test_service_has_no_trading_or_config_writing_methods(replay_run):
    svc, _ = replay_run
    lib = svc._library()
    for obj in (svc, lib):
        assert not any(hasattr(obj, m) for m in ("submit", "place_order", "cancel_order", "flatten", "set_config",
                                                  "apply_scenario", "promote", "write_config"))


def test_keep_state_option_stores_mock_and_replay_journals_for_analytics():
    import run_terminal

    a = run_terminal.parse_args(["--mode", "replay", "--keep-state"])
    assert a.keep_state is True
    assert os.path.relpath(PROJECT_ROOT / "data" / "analytics" / "journals", PROJECT_ROOT) == os.path.join("data", "analytics", "journals")
    assert "data/analytics/journals/*/paper_state.db" in CFG["journals"]


# ============================================================================ audit regressions
def test_filters_apply_before_blocked_and_regime_aggregation(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    b.signal("MES", t, allowed=False, reason="news_event_window", regime={"trend": "strong_bullish", "regime_valid": True})
    b.signal("MNQ", t, allowed=False, reason="max_daily_loss", regime={"trend": "no_trend", "regime_valid": True})
    b.reject("MNQ", t, "cooldown_active")
    b.raw("fill_authorization", t, {"root": "MNQ", "allowed": False, "reason": "account_drawdown_buffer"})
    lib = lib_of(b.done())
    mes = lib.blocked(Filters(root="MES"))
    assert mes["by_category"] == {"NewsGuard": 1} and mes["strategy_condition_failures"] == []
    mnq = lib.blocked(Filters(root="MNQ"))
    assert mnq["by_category"] == {"RiskManager": 1, "AccountGuard (at fill)": 1}
    assert lib.blocked(Filters(end="2026-11-01"))["authorization_blocks"] == 0
    reg = {x["bucket"]: x["observations"] for x in lib.regimes(Filters(root="MES"))["buckets"]}
    assert reg["strong_trend"] == 1 and sum(reg.values()) == 1
    # the regime filter selects signals / trades, never whole sessions
    assert lib.sessions(Filters(regime="strong_bullish"))["count"] == 2


def test_a_copied_journal_is_analysed_once(tmp_path):
    import shutil

    p = simple_run(tmp_path, nets=(10.0, 20.0))
    copy = tmp_path / "copy" / "paper_state.db"
    copy.parent.mkdir()
    shutil.copy(p, copy)
    lib = lib_of(p, copy)
    assert lib.performance(Filters())["overall"]["trades"] == 2  # not 4
    v = {c["check"]: c for c in lib.validation()["checks"]}
    assert v["duplicate journals"]["status"] == "WARN" and v["journal integrity"]["status"] == "PASS"
    assert any("duplicate of" in (j["error"] or "") for j in lib.journal_status())


def test_compare_ignores_runs_without_observations_in_scope(tmp_path):
    rep = simple_run(tmp_path, "replay", "REPLAY", day="2026-11-10")
    sh = simple_run(tmp_path, "shadow", "SHADOW", day="2026-11-18")
    other = simple_run(tmp_path, "mnq", "SHADOW", day="2026-11-18", root="MNQ", sha="b" * 64)  # different config, MNQ only
    assert lib_of(rep, sh, other).compare("MES")["comparable"] is True
    assert lib_of(rep, sh, other).compare("MNQ")["comparable"] is False


def test_decided_counterfactuals_are_cached(tmp_path, monkeypatch):
    from src.analytics.shadow import reports

    b = JB(tmp_path / "s" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    b.signal("MES", t, allowed=False, reason="news_event_window")
    b.bar("MES", t + pd.Timedelta(minutes=1), 5000.0, 5007.0, 4999.0, 5006.5)
    lib = lib_of(b.done())
    assert lib.blocked(Filters())["counterfactual"]["target_first"] == 1
    calls = []
    real = reports._hypothetical
    monkeypatch.setattr(reports, "_hypothetical", lambda *a: calls.append(1) or real(*a))
    assert lib.blocked(Filters())["counterfactual"]["target_first"] == 1 and calls == []


def test_journal_errors_do_not_expose_absolute_paths(tmp_path):
    lib = lib_of(tmp_path / "missing" / "paper_state.db")
    lib.sessions(Filters())
    assert all(str(tmp_path) not in e for e in lib.errors.values())


def test_contract_comes_from_the_feed_and_bars_cache_is_bounded(tmp_path):
    from src.analytics.shadow.model import bars_window

    b = JB(tmp_path / "s" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    b.raw("market_event", t, {"root": "MES", "ts": t.isoformat(), "kind": "trade", "last": 5000.0, "instrument": "MES 12-26"})
    b.bars("MES", t, [5000.0, 5001.0])
    p = b.done()
    lib = lib_of(p)
    s = lib.sessions(Filters())["sessions"][0]
    assert s["contract"] == "MES 12-26"
    src = next(iter(lib.sources.values()))
    cache: dict = {}
    for _ in range(3):
        bars_window(src, "MES", t, t + pd.Timedelta(minutes=5), cache)
        src.data.rows += 1  # the journal grew: the cached copy is replaced, not accumulated
    assert len(cache) == 1


def test_execution_reports_ambiguous_ohlc_exit_bars(tmp_path):
    b = JB(tmp_path / "s" / "paper_state.db")
    t = pd.Timestamp("2026-11-18T15:00:00Z")
    sid = b.signal("MES", t)
    b.bar("MES", t, 5000.0, 5001.0, 4999.0, 5000.5)
    b.bar("MES", t + pd.Timedelta(minutes=1), 5000.5, 5007.0, 4995.0, 5001.0)  # stop 4996 and target 5006 both touched
    b.trade("MES", sid, t, t + pd.Timedelta(minutes=1, seconds=30), 5000.0, 5006.0, 29.0, stop=4996.0, target=5006.0)
    e = lib_of(b.done()).execution(Filters())
    assert e["ambiguous_ohlc_exit_bars"] == 1 and "intrabar order is unknown" in e["ambiguous_ohlc_basis"]


def test_identity_failure_never_stops_a_session(monkeypatch):
    from src.analytics.shadow import identity
    from src.paper.runtime import PaperSession

    def boom(session):
        raise KeyError("x")
    monkeypatch.setattr(identity, "run_identity", boom)
    rows = []

    class Fake:
        session_meta = {"session_type": "SHADOW", "provider": "NinjaTrader 8"}

        def _journal(self, kind, payload, ts):
            rows.append((kind, payload))
    PaperSession._journal_identity(Fake(), pd.Timestamp("2026-11-18T15:00:00Z"))
    assert rows == [("run_identity", {"schema": 1, "error": "KeyError", "session_type": "SHADOW", "data_label": None,
                                      "provider": "NinjaTrader 8", "test_strategy": False})]
