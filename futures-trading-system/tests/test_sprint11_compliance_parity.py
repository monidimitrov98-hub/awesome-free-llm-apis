"""Sprint 11 B: prop firm compliance inside the BACKTEST engine, and parity with the PAPER runtime.

The same ComplianceEngine class, the same profile file and the same requests are run through the backtest
engine and through the paper runtime (PaperBroker only). Decisions, block reasons, reduce-only quantities,
protective flattening and the official floor must agree. Known, documented differences: the paper chain
records the FIRST failing stage in its own order (session -> AccountGuard -> prop -> RiskManager) while the
backtest checks RiskManager before AccountGuard and prop -- every stage is an independent veto, so only the
first recorded reason can differ; these tests isolate the prop stage (no trading windows) to compare it.

Passing these tests says nothing about compliance with any real firm's current rules.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from src.backtest.engine import BacktestEngine
from src.risk.prop_firms.runtime import build_engine
from src.risk.prop_firms.versions import Activations, Registry
from tests.backtest_helpers import ScriptedStrategy, flat_bars, frame
from tests.paper_helpers import APP, BT, T, bt, long, path, session
from tests.test_prop_firms import write_profile

MES = APP.contracts["MES"]


def _btc():
    return bt(risk=replace(BT.risk, trading_windows=()))


def _profiles(tmp_path, *, activate=True, **kw):
    write_profile(tmp_path / "profiles", **kw)
    reg = Registry(tmp_path / "profiles")
    acts = Activations(tmp_path / "acts.jsonl")
    if activate:
        p = reg.get("test_firm", "1")
        acts.activate(p, approved_by="Test Reviewer", confirm_sha=p.sha256[:16], statement="test")
    return reg, acts


def _engine(reg, acts, btc):
    return build_engine(bt=btc, specs={r: APP.contracts[r] for r in ("MES", "MNQ")}, profile_id="test_firm",
                        version="1", registry=reg, activations=acts)


def _backtest(reg, acts, btc, rows, script):
    strat = ScriptedStrategy(script)
    bars = frame(rows)
    res = BacktestEngine(btc, MES, APP.session).run(strat, bars, prop_engine=_engine(reg, acts, btc))
    return res


def _paper(tmp_path, reg, acts, btc, events, script):
    s = session(tmp_path / "paper", events, script, btc=btc, prop_engine=_engine(reg, acts, btc))
    s.run()
    dec = [e["payload"] for e in s.journal.entries("entry_decision")]
    return s, dec


def _prop_reason_backtest(res):
    rj = res.rejections
    r = rj[rj["stage"] == "prop_firm"]
    return r["reason"].tolist()


def test_unverified_profile_blocks_entries_identically(tmp_path):
    reg, acts = _profiles(tmp_path, verification="UNVERIFIED", activate=False)
    btc = _btc()
    res = _backtest(reg, acts, btc, flat_bars("10:00", 30), {"10:04": [long(stop=4990.0, target=5010.0)]})
    s, dec = _paper(tmp_path, reg, acts, btc, path(T("10:00"), 30, lambda i: 5000.0),
                    {T("10:05"): [long(stop=4990.0, target=5010.0)]})
    assert _prop_reason_backtest(res) == ["prop_profile_not_operational"]
    assert [d["reason"] for d in dec] == ["prop_profile_not_operational"]
    assert res.trades.empty and not list(s.journal.entries("execution"))
    cov = res.compliance["coverage"]
    assert cov["verdict"] == "INCOMPLETE -- RESEARCH ONLY"
    assert any("not operational" in x for x in cov["issues"])


def test_contract_limit_blocks_identically(tmp_path):
    reg, acts = _profiles(tmp_path, contracts=1)
    btc = _btc()
    res = _backtest(reg, acts, btc, flat_bars("10:00", 30), {"10:04": [long(stop=4990.0, target=5010.0, quantity=2)]})
    s, dec = _paper(tmp_path, reg, acts, btc, path(T("10:00"), 30, lambda i: 5000.0),
                    {T("10:05"): [long(stop=4990.0, target=5010.0, quantity=2)]})
    assert _prop_reason_backtest(res) == ["prop_contract_limit"] == [d["reason"] for d in dec]
    assert res.trades.empty


def test_an_allowed_entry_passes_the_same_checks_and_is_recorded(tmp_path):
    reg, acts = _profiles(tmp_path)
    btc = _btc()
    res = _backtest(reg, acts, btc, flat_bars("10:00", 30), {"10:04": [long(stop=4990.0, target=5010.0)]})
    s, dec = _paper(tmp_path, reg, acts, btc, path(T("10:00"), 30, lambda i: 5000.0),
                    {T("10:05"): [long(stop=4990.0, target=5010.0)]})
    assert dec and dec[0]["allowed"]
    assert "prop_firm" in [x["stage"] for x in dec[0]["stages"]]
    pdq = res.compliance["decisions"]
    assert list(pdq["scope"]) == ["pre_trade", "fill"] and pdq["allowed"].all()        # same scopes as paper
    assert len(res.trades) == 1 and list(s.journal.entries("execution"))
    cov = res.compliance["coverage"]
    assert "T-MLL" in cov["applied"] and "T-CONTRACTS" in cov["applied"]


def test_flat_by_cutoff_blocks_late_entries_identically(tmp_path):
    reg, acts = _profiles(tmp_path)   # flat by 15:10 Chicago = 16:10 New York; entry cutoff 10 minutes earlier
    btc = _btc()
    res = _backtest(reg, acts, btc, flat_bars("16:00", 20), {"16:04": [long(stop=4990.0, target=5010.0)]})
    s, dec = _paper(tmp_path, reg, acts, btc, path(T("16:00"), 20, lambda i: 5000.0),
                    {T("16:05"): [long(stop=4990.0, target=5010.0)]})
    assert dec and not dec[0]["allowed"]
    assert _prop_reason_backtest(res) == [dec[0]["reason"]]
    assert res.trades.empty


def test_protective_flatten_at_the_prop_threshold_in_both_environments(tmp_path):
    internal = ("safety_reserve = 300.0\nflatten_reserve = 150.0\nuncertainty_reserve = 0.0\n"
                "require_trade_risk_within_buffer = false\n")
    reg, acts = _profiles(tmp_path, amount=600.0, lock=None, internal=internal)
    btc = bt(risk=replace(BT.risk, trading_windows=(), max_daily_loss=0.0))     # isolate the prop protection
    # same price path in both: flat at 5000, then a gap to 4910 (-450 per contract: within the flatten reserve)
    rows = flat_bars("10:00", 10) + flat_bars("10:10", 20, price=4910.0)
    res = _backtest(reg, acts, btc, rows, {"10:04": [long(stop=4800.0, target=5100.0)]})
    s, _ = _paper(tmp_path, reg, acts, btc, path(T("10:00"), 30, lambda i: 5000.0 if i < 40 else 4910.0),
                  {T("10:05"): [long(stop=4800.0, target=5100.0)]})
    assert res.trades["exit_reason"].tolist() == ["prop_firm_max_loss_protection"]
    assert "prop_firm_max_loss_protection" in [e["payload"]["reason"] for e in s.journal.entries("flatten_requested")]
    floor = 10000.0 - 600.0
    eq = res.equity["equity"]
    assert eq.min() > floor                                       # the official floor is never reached
    assert s.account.equity() > floor


def test_fail_closed_news_restriction_without_newsguard_in_backtests(tmp_path):
    extra = ('[[rules]]\nid = "T-NEWS"\ntype = "news_restriction"\ndescription = "no trading around news"\n'
             'verification = "VERIFIED"\nsources = ["S1"]\nparams = { minutes_before = 2, minutes_after = 2 }\n')
    reg, acts = _profiles(tmp_path, rules_extra=extra)
    btc = _btc()
    res = _backtest(reg, acts, btc, flat_bars("10:00", 30), {"10:04": [long(stop=4990.0, target=5010.0)]})
    assert res.trades.empty and len(_prop_reason_backtest(res)) == 1        # no NewsGuard -> cannot evaluate
    cov = res.compliance["coverage"]
    assert "T-NEWS" in cov["unavailable"] and cov["verdict"] == "INCOMPLETE -- RESEARCH ONLY"


def test_backtest_without_a_profile_is_unchanged_and_reports_no_compliance(tmp_path):
    btc = _btc()
    strat = ScriptedStrategy({"10:04": [long(stop=4990.0, target=5010.0)]})
    res = BacktestEngine(btc, MES, APP.session).run(strat, frame(flat_bars("10:00", 30)))
    assert res.compliance is None and len(res.trades) == 1
