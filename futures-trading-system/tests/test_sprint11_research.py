"""Sprint 11 E/F/G/H: research runs and statistical safeguards, Monte Carlo, evaluation reports, experiment
registry, one-time OOS reveal, human promotion gates, paper/shadow gating and read-only terminal views."""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
from src.backtest.validation import ValidationLedger
from src.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from src.research import governance as G
from src.research import runs as R
from src.research.chain import ChainError
from src.research.evaluation import evaluate, evidence_label
from src.research.experiments import ExperimentRegistry
from src.research.monte_carlo import CostScenario, prop_rules, repriced, simulate
from src.research.params import load_version
from src.research.stats import benjamini_hochberg, holm, one_sided_p, ratios, required_days

ROOT = Path(__file__).resolve().parents[1]
MNQ = ROOT / "data/samples/SYNTHETIC_MNQ_1m.csv"
MES = ROOT / "data/samples/SYNTHETIC_MES_1m.csv"
TRUSTED = {"commit": "a" * 40, "dirty": False, "source_sha256": "s" * 64, "source_files": 1, "trusted": True,
           "untrusted_reasons": []}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FTS_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("FTS_EXPERIMENTS_PATH", str(tmp_path / "experiments.jsonl"))
    monkeypatch.setattr(G, "GOVERNANCE_FILE", tmp_path / "promotions.jsonl")
    ValidationLedger(tmp_path / "ledger.jsonl").initialise("test")
    return R.ResearchEnv(load_config(), load_backtest_config(), tmp_path / "out", DEFAULT_CONFIG_PATH,
                         DEFAULT_BACKTEST_CONFIG_PATH, registry=ExperimentRegistry())


# ============================================================================ statistics
def test_multiple_testing_adjustments_match_known_values():
    p = [0.01, 0.04, 0.03, 0.005, None]                   # the None (failed attempt) still counts: m = 5
    h = holm(p)
    assert h[3] == pytest.approx(0.025) and h[0] == pytest.approx(0.04) and h[2] == pytest.approx(0.09)
    assert h[1] == pytest.approx(0.09) and h[4] is None
    b = benjamini_hochberg(p)
    assert b[3] == pytest.approx(0.025) and b[0] == pytest.approx(0.025)
    assert b[2] == pytest.approx(0.05) and b[1] == pytest.approx(0.05)
    assert all(x is None or 0 <= x <= 1 for x in h + b)


def test_one_sided_test_power_and_ratios():
    assert one_sided_p([1.0]) is None and one_sided_p([2.0, 2.0, 2.0]) is None      # no variance: no claim
    assert one_sided_p([10, 12, 9, 11, 10, 13] * 5) < 1e-6
    assert one_sided_p([-10, -12, -9, -11] * 5) > 0.999
    assert required_days([-1, -2, -3]) is None
    assert required_days([1, 3, 2, 4, 0, 5]) > 0
    r = ratios([10.0] * 5, 10000)
    assert r["sharpe"] is None and "needs >= 20" in r["note"]
    assert ratios([10, -5, 7, 3, -2] * 4, 10000)["sharpe"] is not None


def test_evidence_labels_never_claim_more_than_the_data_supports():
    assert evidence_label(29, 20, (1.0, 2.0), 0.001) == "INSUFFICIENT EVIDENCE"
    assert evidence_label(100, 1, (1.0, 2.0), 0.001) == "INSUFFICIENT EVIDENCE"
    assert evidence_label(100, 40, (1.0, 2.0), 0.001) == "POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE"
    assert evidence_label(100, 40, (1.0, 2.0), 0.2) == "INCONCLUSIVE"          # multiple-testing adjusted p too high
    assert evidence_label(100, 40, (-1.0, 2.0), 0.001) == "INCONCLUSIVE"
    assert evidence_label(100, 40, (-3.0, -1.0), 0.99) == "NEGATIVE EXPECTANCY IN THIS SAMPLE"


def _trades(n_days=40, per_day=2, mean=5.0, sd=20.0, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    k = 0
    for d in pd.bdate_range("2025-01-02", periods=n_days):
        for j in range(per_day):
            k += 1
            net = float(rng.normal(mean, sd))
            t0 = pd.Timestamp(d).tz_localize("America/New_York") + pd.Timedelta(hours=10, minutes=30 * j)
            rows.append({"trade_id": k, "side": "long", "quantity": 1, "entry_time": t0.tz_convert("UTC"),
                         "exit_time": (t0 + pd.Timedelta(minutes=20)).tz_convert("UTC"), "net_pnl": net,
                         "gross_pnl": net + 2.5, "commission": 2.0, "slippage_cost": 0.5, "total_costs": 2.5,
                         "exit_reason": "take_profit" if net > 0 else "stop_loss", "session_date": d,
                         "exit_session_date": d, "r_multiple": net / 20, "initial_risk": 20.0,
                         "actual_risk_dollars": 20.0, "mae_dollars": abs(min(net, 0.0)) + 1.0,
                         "mfe_dollars": max(net, 0.0) + 1.0})
    return pd.DataFrame(rows)


def test_family_size_weakens_every_result():
    t = _trades(mean=6.0, sd=20.0, seed=4)
    one = evaluate(t, capital=10000, scope="validation", tz="America/New_York")
    many = evaluate(t, capital=10000, scope="validation", tz="America/New_York", family_size=200)
    assert many["tests"]["p_holm"] >= one["tests"]["p_holm"]
    assert many["tests"]["family_size"] == 200
    assert one["sample"]["trades"] == 80 and one["sample"]["trading_days_with_trades"] == 40
    assert one["excursions"]["mae_dollars"]["n"] == 80
    assert one["breakdowns"]["note"].startswith("EXPLORATORY")
    assert "No claim of profitability is made by this report." in one["limitations"]


# ============================================================================ Monte Carlo
def test_monte_carlo_is_deterministic_and_documented():
    t = _trades()
    a = simulate(t, capital=10000, tick_value=1.25, n_paths=200, seed=7, ruin_loss=500, drawdown_levels=(100, 300))
    b = simulate(t, capital=10000, tick_value=1.25, n_paths=200, seed=7, ruin_loss=500, drawdown_levels=(100, 300))
    c = simulate(t, capital=10000, tick_value=1.25, n_paths=200, seed=8, ruin_loss=500, drawdown_levels=(100, 300))
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert a["methods"]["day_block"]["as_backtested"]["final_pnl"] != c["methods"]["day_block"]["as_backtested"]["final_pnl"]
    assert a["manifest"]["sha256"] == b["manifest"]["sha256"] != c["manifest"]["sha256"]
    assert a["historical"]["evidence_type"] == "HISTORICAL"
    assert a["methods"]["trade_iid"]["as_backtested"]["evidence_type"] == "MONTE_CARLO"
    sh = a["methods"]["shuffle"]["as_backtested"]["final_pnl"]
    assert sh["p5"] == pytest.approx(sh["p95"]) == pytest.approx(t["net_pnl"].sum())   # same trades, other order
    base, stress = (a["methods"]["day_block"][k]["prob_final_pnl_negative"]
                    for k in ("as_backtested", "stress_+2_ticks_commission_x2"))
    assert stress >= base
    assert set(a["assumptions"]) >= {"trade_iid", "day_block", "moving_block", "shuffle", "costs", "prop"}
    p = a["methods"]["day_block"]["as_backtested"]["prob_drawdown_at_least"]
    assert p["100"] >= p["300"]


def test_cost_scenarios_reprice_exactly():
    t = _trades(n_days=3)
    out = repriced(t, CostScenario("x", 2.0, 2.0), tick_value=1.25)
    assert np.allclose(out, t["net_pnl"] - (2 * 2 * 1.25) - 2.0)


def test_monte_carlo_only_simulates_verified_prop_rules(tmp_path):
    from src.risk.prop_firms.profile import load_profile
    from tests.test_prop_firms import write_profile

    prof = load_profile(write_profile(tmp_path, ml="trailing_eod", amount=500.0, lock=10100.0,
                                      ml_extra=', lock_verification = "CONFLICTING"'))
    ev, skipped = prop_rules(prof)
    assert [r["rule"] for r in ev] == ["T-MLL"] and "lock_at" not in ev[0] and "NOT applied" in ev[0]["lock_note"]
    unv = load_profile(write_profile(tmp_path / "u", verification="UNVERIFIED"))
    ev2, skipped2 = prop_rules(unv)
    assert ev2 == [] and skipped2[0]["rule"] == "T-MLL" and "never assumed" in skipped2[0]["why"]
    t = _trades(mean=-8.0, sd=30.0)
    rep = simulate(t, capital=10000, tick_value=1.25, n_paths=100, seed=3, profile=prof, profile_operational=False)
    assert rep["prop"]["label"].startswith("RESEARCH ESTIMATE")
    assert 0 <= rep["methods"]["day_block"]["as_backtested"]["prop_breach_probability"]["T-MLL"] <= 1
    assert rep["historical"]["prop_breaches"]["T-MLL"] in (True, False)


def test_monte_carlo_refuses_bad_parameters_and_handles_no_trades():
    t = _trades(n_days=3)
    with pytest.raises(ValueError):
        simulate(t, capital=10000, tick_value=1.25, n_paths=0)
    with pytest.raises(ValueError):
        simulate(t, capital=10000, tick_value=1.25, methods=("magic",))
    assert simulate(t.iloc[0:0], capital=10000, tick_value=1.25)["status"].startswith("NO TRADES")


# ============================================================================ research runs
def test_every_attempt_is_recorded_including_failures(env):
    with pytest.raises(ConfigError):
        R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2030-01-01",
                       end="2030-01-31")                                  # no data in the period: fails AFTER start
    r = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10",
                       end="2025-03-14")
    att = env.registry.attempts()
    assert [a["status"] for a in att] == ["failed", "ok"]
    assert att[1]["registered"] and att[1]["version_sha256"] == load_version("vwap_mean_reversion", "V1-C2-SWING",
                                                                               env.app).sha256
    m = json.loads((r["folder"] / "manifest.json").read_text())
    assert m["strategy"]["sha256"] == att[1]["version_sha256"] and m["strategy"]["params_sha256"]
    assert m["data"]["sha256"] and m["config"]["settings_sha256"] and m["results_fingerprint"]
    assert R.verify_manifest(r["folder"])["ok"]
    assert "Research simulation only" in (r["folder"] / "report.md").read_text()
    assert (r["folder"] / "decision_trace.csv").is_file()


def test_manifest_corruption_is_detected(env):
    r = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ)
    (r["folder"] / "trades.csv").write_text("tampered\n")
    v = R.verify_manifest(r["folder"])
    assert not v["ok"] and "trades.csv was modified" in v["problems"]
    m = json.loads((r["folder"] / "manifest.json").read_text())
    m["evaluation"]["evidence"] = "POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE"
    (r["folder"] / "manifest.json").write_text(json.dumps(m))
    assert "manifest.json was modified (its own hash does not match)" in R.verify_manifest(r["folder"])["problems"]


def test_experiment_registry_tampering_breaks_the_chain(env):
    R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10", end="2025-03-12")
    lines = env.registry.path.read_text().splitlines()
    env.registry.path.write_text("\n".join(lines[1:]) + "\n")            # delete the first record
    with pytest.raises(ChainError):
        env.registry.records()
    with pytest.raises(ChainError):                                       # never extended once broken
        env.registry.start(strategy="x", version="v", version_sha256="s", registered=True, params_sha256="p",
                           instrument="MES", data_sha256="d", period={}, purpose="train", family="f")


def test_research_needs_the_ledger(env, tmp_path, monkeypatch):
    monkeypatch.setenv("FTS_LEDGER_PATH", str(tmp_path / "missing.jsonl"))
    with pytest.raises(ConfigError, match="No validation ledger"):
        R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ)


def test_sweeps_stay_inside_declared_bounds_and_are_not_ranked(env):
    with pytest.raises(ConfigError, match="outside the declared research bounds"):
        R.sweep(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, {"indicators.band_k": [2.0, 9.0]})
    assert env.registry.attempts() == []                                  # refused BEFORE anything ran
    with pytest.raises(ConfigError, match="never out of sample"):
        R.sweep(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, {"indicators.band_k": [2.0]}, purpose="oos")
    out = R.sweep(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ,
                  {"indicators.band_k": [1.5, 2.0], "stop.swing_lookback_bars": [3, 5]}, purpose="train",
                  start="2025-03-10", end="2025-03-14")
    assert [r["params"] for r in out["rows"]] == sorted([r["params"] for r in out["rows"]],
                                                         key=lambda p: (p["indicators.band_k"], p["stop.swing_lookback_bars"]))
    assert out["family_size"] == 4 and all("stability" in r for r in out["rows"])
    assert "NOT ranked" in out["note"]
    for r in out["rows"]:
        m = json.loads((Path(r["folder"]) / "manifest.json").read_text())
        assert m["strategy"]["overrides"] == r["params"] and m["strategy"]["registered"] is True


def test_walk_forward_purge_and_purged_kfold_embargo():
    d = list(pd.bdate_range("2025-01-01", periods=12))
    wf = R.walk_forward_folds(d, train=4, test=2, purge=1)
    for train, (t0, t1) in wf:
        (a, b), = train
        assert b < t0 and d.index(t0) - d.index(b) == 2                 # exactly one purged session between
    tests_ = [x[1] for x in wf]
    assert all(tests_[i][1] < tests_[i + 1][0] for i in range(len(tests_) - 1))     # test windows never overlap
    with pytest.raises(ConfigError):
        R.walk_forward_folds(d, train=4, test=2, step=1)
    cv = R.purged_kfold(d, k=3, purge=1, embargo=2)
    for train, (t0, t1) in cv:
        i0, i1 = d.index(t0), d.index(t1)
        used = {x for a, b in train for x in d[d.index(a):d.index(b) + 1]}
        assert not used & set(d[max(0, i0 - 1):i1 + 3])                   # purge before + embargo after removed


def test_walk_forward_selects_by_the_declared_objective_and_records_every_attempt(env):
    rs = R.resolve(env, "vwap_mean_reversion", "V1-C2-SWING")
    md, _ = env.market_data(MNQ)
    dates = R.selected_sessions(env, rs, md)
    out = R.run_folds(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, R.walk_forward_folds(dates, 2, 1, purge=1),
                      mode="walk_forward", grid={"indicators.band_k": [1.5, 2.0]})
    assert out["objective"] == R.OBJECTIVE and "never net profit" in out["objective"]
    assert out["aggregate"]["scope"] == "wf_out_of_sample"
    purposes = [a["purpose"] for a in env.registry.attempts()]
    assert purposes.count("train") == 2 * len(out["folds"])            # every grid point on every training window
    assert purposes.count("walk_forward") == len(out["folds"])


def test_compare_only_like_for_like_and_never_ranks(env):
    a = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ)
    b = R.run_research(env, "vwap_mean_reversion", "V1-C4-SWING", MNQ)
    c = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MES)
    out = R.compare([a["folder"], b["folder"]])
    assert [r["run"] for r in out["runs"]] == sorted(r["run"] for r in out["runs"])
    assert "Not ranked" in out["note"] and out["pairs"][0]["reading"].endswith("(unadjusted; exploratory)")
    with pytest.raises(ConfigError, match="equivalent"):
        R.compare([a["folder"], c["folder"]])


# ============================================================================ one-time OOS reveal
def test_oos_reveal_safeguards(env, monkeypatch):
    with pytest.raises(ConfigError, match="untrusted code"):
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, "2025-03-17", "2025-03-18")
    monkeypatch.setattr(R, "code_identity", lambda: dict(TRUSTED))
    with pytest.raises(ConfigError, match="no parameter version"):     # never substituted by another version
        R.reveal_oos(env, "vwap_mean_reversion", "V1-NOPE", MNQ, "2025-03-17", "2025-03-18")
    R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10",
                   end="2025-03-14")
    with pytest.raises(ConfigError, match="not out of sample"):          # research touched these dates
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, "2025-03-13", "2025-03-18")
    res = R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, "2025-03-17", "2025-03-18")
    led = ValidationLedger(Path(env.ledger()[0].path)).records()
    rec = [r for r in led if r.get("type") == "oos_reveal"]
    assert len(rec) == 1 and rec[0]["strategy"] == "vwap_mean_reversion" and rec[0]["version"] == "V1-C2-SWING"
    m = json.loads((res["folder"] / "manifest.json").read_text())
    assert m["purpose"] == "oos" and m["oos"]["ledger_record_hash"] == rec[0]["record_hash"]
    assert m["evaluation"]["scope"] == "oos"
    # the identical configuration can only REPLAY (no new ledger record, identical results)
    again = R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, "2025-03-17", "2025-03-18")
    assert again["replay_identical"] is True
    assert len([r for r in ValidationLedger(Path(env.ledger()[0].path)).records() if r.get("type") == "oos_reveal"]) == 1
    # any other configuration on overlapping revealed dates is refused; research on them is refused forever
    with pytest.raises(ConfigError, match="already revealed"):
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C4-SWING", MNQ, "2025-03-18", "2025-03-18")
    with pytest.raises(ConfigError, match="already\\s+revealed"):
        R.run_research(env, "vwap_mean_reversion", "V1-C4-SWING", MNQ, purpose="validation", start="2025-03-17",
                       end="2025-03-18")
    with pytest.raises(ConfigError, match="revealed"):
        R.sweep(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, {"indicators.band_k": [1.5]}, purpose="full")
    with pytest.raises(ConfigError, match="reveal_oos"):
        R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="oos", start="2025-03-17",
                       end="2025-03-18")


def test_unregistered_and_derived_configurations_can_never_be_revealed(env, monkeypatch):
    monkeypatch.setattr(R, "code_identity", lambda: dict(TRUSTED))
    with pytest.raises(TypeError):
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, "2025-03-17", "2025-03-18",
                     overrides={"indicators.band_k": 1.5})


# ============================================================================ governance
def _promote(env, to, **ev):
    pv = load_version("vwap_mean_reversion", "V1-C2-SWING", env.app)
    return G.transition("vwap_mean_reversion", "V1-C2-SWING", env.app, to=to, approved_by="Test Human",
                        confirm_sha=pv.sha256[:16], statement="reviewed the evidence in detail", evidence=ev)


def test_promotion_is_human_ordered_and_evidence_based(env, monkeypatch):
    pv = load_version("vwap_mean_reversion", "V1-C2-SWING", env.app)
    for bad in ("LIVE", "LIVE_APPROVED", "live"):
        with pytest.raises(ConfigError, match="no live state"):
            _promote(env, bad)
    with pytest.raises(ConfigError, match="no step"):
        _promote(env, "OOS_VALIDATED")
    with pytest.raises(ConfigError, match="needs --run"):
        _promote(env, "BACKTESTED")
    with pytest.raises(ConfigError, match="--confirm"):
        G.transition("vwap_mean_reversion", "V1-C2-SWING", env.app, to="BACKTESTED", approved_by="Test Human",
                     confirm_sha="0" * 16, statement="reviewed the evidence in detail")
    with pytest.raises(ConfigError, match="approved-by"):
        G.transition("vwap_mean_reversion", "V1-C2-SWING", env.app, to="BACKTESTED", approved_by="",
                     confirm_sha=pv.sha256[:16], statement="reviewed the evidence in detail")
    other = R.run_research(env, "vwap_mean_reversion", "V1-C4-SWING", MNQ, purpose="train", start="2025-03-10",
                           end="2025-03-12")
    with pytest.raises(ConfigError, match="is not a run of"):
        _promote(env, "BACKTESTED", run=other["folder"])
    derived = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10",
                             end="2025-03-12", overrides={"indicators.band_k": 1.5})
    with pytest.raises(ConfigError, match="overrides"):
        _promote(env, "BACKTESTED", run=derived["folder"])
    good = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10",
                          end="2025-03-12")
    rec = _promote(env, "BACKTESTED", run=good["folder"])
    assert rec["from"] == "RESEARCH" and rec["evidence"][0]["manifest_sha256"]
    assert G.state("vwap_mean_reversion", "V1-C2-SWING", pv.sha256)["state"] == "BACKTESTED"
    # OOS_VALIDATED needs a ledger-recorded OOS run with POSITIVE evidence: synthetic data never gives that
    monkeypatch.setattr(R, "code_identity", lambda: dict(TRUSTED))
    oos = R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, "2025-03-17", "2025-03-18")
    with pytest.raises(ConfigError, match="only 'POSITIVE EXPECTANCY"):
        _promote(env, "OOS_VALIDATED", run=oos["folder"])
    with pytest.raises(ConfigError, match="not a one-time out-of-sample"):
        _promote(env, "OOS_VALIDATED", run=good["folder"])
    # suspension / reinstatement / rejection
    _promote(env, "SUSPENDED")
    with pytest.raises(ConfigError, match="reinstated to BACKTESTED"):
        _promote(env, "OOS_VALIDATED", run=oos["folder"])
    _promote(env, "BACKTESTED")
    _promote(env, "REJECTED")
    with pytest.raises(ConfigError, match="REJECTED"):
        _promote(env, "SUSPENDED")
    assert G.allows("vwap_mean_reversion", "V1-C2-SWING", pv.sha256, "shadow") == (
        False, "vwap_mean_reversion@V1-C2-SWING is REJECTED; shadow needs SHADOW_APPROVED or PAPER_APPROVED (human promotion)")
    assert G.allows("vwap_mean_reversion", "V1-C2-SWING", pv.sha256, "live")[0] is False


def test_a_tampered_promotion_store_approves_nothing(env):
    pv = load_version("vwap_mean_reversion", "V1-C2-SWING", env.app)
    st = G.store()
    for a, b in (("RESEARCH", "BACKTESTED"), ("BACKTESTED", "OOS_VALIDATED"), ("OOS_VALIDATED", "SHADOW_APPROVED")):
        st.append({"kind": "transition", "strategy": "vwap_mean_reversion", "version": "V1-C2-SWING",
                   "version_sha256": pv.sha256, "from": a, "to": b, "approved_by": "T", "statement": "fixture",
                   "evidence": []})
    assert G.allows("vwap_mean_reversion", "V1-C2-SWING", pv.sha256, "shadow")[0] is True
    assert G.allows("vwap_mean_reversion", "V1-C2-SWING", pv.sha256, "paper")[0] is False
    text = G.GOVERNANCE_FILE.read_text().replace('"to": "SHADOW_APPROVED"', '"to": "PAPER_APPROVED"')
    G.GOVERNANCE_FILE.write_text(text)
    ok, why = G.allows("vwap_mean_reversion", "V1-C2-SWING", pv.sha256, "paper")
    assert not ok and "integrity" in why
    assert G.allows("vwap_mean_reversion", "V1-C2-SWING", pv.sha256, "shadow")[0] is False


def test_nothing_but_the_cli_can_promote():
    """No module (research runs, engines, terminal, paper runtime) calls governance.transition or registers a
    version: both are reachable only from run_research.py, a human command."""
    callers = []
    targets = {"src.research.governance": "transition", "src.research.params": "register_version"}
    for p in list((ROOT / "src").rglob("*.py")) + [ROOT / f for f in ("run_paper.py", "run_backtest.py",
                                                                       "run_terminal.py", "run_research.py")]:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        names, modules = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                names |= {(a.asname or a.name) for a in node.names if a.name == targets[node.module]}
            if isinstance(node, ast.ImportFrom) and node.module == "src.research":
                modules |= {(a.asname or a.name) for a in node.names if a.name in ("governance", "params")}
            if isinstance(node, ast.Import):
                modules |= {(a.asname or a.name) for a in node.names if a.name in targets}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id in names:
                callers.append(f"{p.relative_to(ROOT)}:{f.id}")
            if isinstance(f, ast.Attribute) and f.attr in targets.values() and isinstance(f.value, ast.Name) \
                    and f.value.id in modules:
                callers.append(f"{p.relative_to(ROOT)}:{f.attr}")
    callers = [c for c in callers if not c.startswith(("src/research/governance.py", "src/research/params.py"))]
    assert sorted(callers) == ["run_research.py:register_version", "run_research.py:transition"]


def test_paper_runtime_refuses_unapproved_versions(env):
    import run_paper

    rc = run_paper.main(["--mode", "replay", "--state-dir", str(env.out_root / "paper"), "--new-account", "--bars",
                         f"MES={MES}", "--strategy", "vwap_mean_reversion@V1-C2-SWING"])
    assert rc == 1
    with pytest.raises(ConfigError, match="needs SHADOW_APPROVED"):
        from src.research.paper_binding import binding_for

        binding_for("vwap_mean_reversion", "V1-C2-SWING", env.app, None, use="shadow")
    with pytest.raises(ConfigError, match="needs PAPER_APPROVED"):
        binding_for("vwap_mean_reversion", "V1-C2-SWING", env.app, None, use="paper")


def test_an_approved_version_runs_in_the_paper_runtime_through_every_layer(env, tmp_path):
    """SHADOW_APPROVED version in a replay: PaperBroker only, every authorization stage, RTH flattening."""
    import run_paper

    pv = load_version("vwap_mean_reversion", "V1-C2-SWING", env.app)
    st = G.store()
    for a, b in (("RESEARCH", "BACKTESTED"), ("BACKTESTED", "OOS_VALIDATED"), ("OOS_VALIDATED", "SHADOW_APPROVED")):
        st.append({"kind": "transition", "strategy": "vwap_mean_reversion", "version": "V1-C2-SWING",
                   "version_sha256": pv.sha256, "from": a, "to": b, "approved_by": "T", "statement": "fixture",
                   "evidence": []})
    src = MNQ.read_text().splitlines()
    keep = [ln for ln in src if ln.startswith("#") or ln.startswith("timestamp") or "2025-03-11" <= ln[:10] <= "2025-03-12"]
    csv = tmp_path / "SYNTHETIC_MNQ_1m.csv"
    csv.write_text("\n".join(keep) + "\n")
    rep_path = tmp_path / "rep.json"
    rc = run_paper.main(["--mode", "replay", "--state-dir", str(tmp_path / "st"), "--new-account", "--bars",
                         f"MNQ={csv}", "--strategy", "vwap_mean_reversion@V1-C2-SWING",
                         "--news-calendar", str(ROOT / "data/samples/SYNTHETIC_news_calendar.json"),
                         "--report", str(rep_path)])
    assert rc == 0
    rep = json.loads(rep_path.read_text())
    assert rep["session"]["strategy"]["id"] == "vwap_mean_reversion" and rep["session"]["strategy"]["sha256"] == pv.sha256
    assert rep["session"]["strategy"]["approval"] == "SHADOW_APPROVED" and rep["session"]["session_type"] == "REPLAY"
    assert rep["signals"]["strategy_signals"] >= 1 and rep["signals"]["entry_decisions"] >= 1
    from src.paper.journal import Journal

    j = Journal(tmp_path / "st" / "paper_state.db")
    decs = [e["payload"] for e in j.entries("entry_decision")]
    stages = [x["stage"] for d in decs if d["allowed"] for x in d["stages"]]
    assert {"mode", "news", "account_guard", "risk_manager"} <= set(stages)     # every layer was applied
    for e in j.entries("execution"):
        t = pd.Timestamp(e["payload"]["ts"]).tz_convert("America/New_York")
        assert (9, 30) <= (t.hour, t.minute) <= (15, 55)                 # RTH strategy: flat before 16:00
    ident = [e["payload"] for e in j.entries("run_identity")][0]
    assert ident["strategy"]["registry"]["sha256"] == pv.sha256
    j.close()


# ============================================================================ read-only terminal views
def test_terminal_research_views_are_read_only(env, tmp_path):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app
    from src.terminal.modes import ModeOptions
    from src.terminal.service import TerminalService

    r = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10",
                       end="2025-03-12")
    svc = TerminalService(ModeOptions(mode="mock", research_root=env.out_root, governance_path=G.GOVERNANCE_FILE))
    app = create_app(svc, token="t" * 32, port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        h = {"Authorization": "Bearer " + "t" * 32}
        ov = c.get("/api/research/overview", headers=h)
        assert ov.status_code == 200
        body = ov.json()
        assert body["read_only"] is True and body["runs"][0]["attempt"] == r["attempt"]
        assert any("experimental" in x for x in body["limitations"])
        d = c.get(f"/api/research/run?attempt={r['attempt']}", headers=h).json()
        assert d["integrity"]["ok"] and d["evaluation"]["evidence"]
        assert c.get("/api/research/run?attempt=../../etc", headers=h).status_code == 422
        assert c.get("/api/research/run?attempt=" + "0" * 16, headers=h).status_code == 404
        assert c.get("/api/research/overview").status_code in (401, 403)
        for m in ("post", "put", "delete"):
            assert getattr(c, m)("/api/research/overview", headers=h).status_code == 405


# ============================================================================ CLI
def test_research_cli_end_to_end(env, tmp_path, capsys):
    import run_research

    out = str(tmp_path / "cli")
    assert run_research.main(["--json", "strategies"]) == 0
    assert "vwap_mean_reversion" in capsys.readouterr().out
    assert run_research.main(["versions", "--strategy", "vwap_mean_reversion"]) == 0
    assert "confirm_sha16" in capsys.readouterr().out
    assert run_research.main(["--output-dir", out, "--json", "run", "--strategy", "vwap_mean_reversion", "--version",
                              "V1-C3-SWING", "--csv", str(MES), "--purpose", "train", "--start", "2025-03-10",
                              "--end", "2025-03-14", "--cost-sensitivity"]) == 0
    res = json.loads(capsys.readouterr().out.split("***\n", 1)[-1])
    folder = Path(res["folder"])
    m = json.loads((folder / "manifest.json").read_text())
    assert [c["scenario"] for c in m["cost_sensitivity"]][0] == "as_configured" and len(m["cost_sensitivity"]) == 6
    assert run_research.main(["verify", "--run", str(folder)]) == 0
    capsys.readouterr()
    assert run_research.main(["monte-carlo", "--run", str(folder), "--paths", "50", "--seed", "3"]) == 0
    assert "resample the backtest's own trades" in capsys.readouterr().out
    mc = json.loads((folder / "monte_carlo_seed3_paths50.json").read_text())
    assert mc["strategy"]["sha256"] == m["strategy"]["sha256"]
    assert run_research.main(["--output-dir", out, "sweep", "--strategy", "vwap_mean_reversion", "--version",
                              "V1-C3-SWING", "--csv", str(MES), "--grid", "indicators.band_k=1,2"]) == 1
    assert "outside the declared research bounds" in capsys.readouterr().err
    pv = load_version("vwap_mean_reversion", "V1-C3-SWING", env.app)
    assert run_research.main(["promote", "--strategy", "vwap_mean_reversion", "--version", "V1-C3-SWING", "--to",
                              "LIVE", "--approved-by", "Test Human", "--confirm", pv.sha256[:16],
                              "--statement", "trying to approve live trading"]) == 1
    assert "no live state" in capsys.readouterr().err
    assert run_research.main(["promote", "--strategy", "vwap_mean_reversion", "--version", "V1-C3-SWING", "--to",
                              "BACKTESTED", "--approved-by", "Test Human", "--confirm", pv.sha256[:16],
                              "--statement", "reviewed the training run", "--run", str(folder)]) == 0
    assert "BACKTESTED" in capsys.readouterr().out
    assert run_research.main(["--json", "experiments", "--strategy", "vwap_mean_reversion"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out


# ============================================================================ audit regressions (Sprint 11)
def test_promotion_refuses_edited_or_forged_run_evidence(env, monkeypatch):
    """AUDIT: the OOS evidence label used to be read from the editable manifest. Promotion now verifies every
    run folder and takes the OOS label from the hash-chained ledger record."""
    from src.backtest.manifest import text_sha256

    r = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10",
                       end="2025-03-12")
    (r["folder"] / "trades.csv").write_text("edited\n")
    with pytest.raises(ConfigError, match="failed verification"):
        _promote(env, "BACKTESTED", run=r["folder"])
    good = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10",
                          end="2025-03-12")
    _promote(env, "BACKTESTED", run=good["folder"])
    monkeypatch.setattr(R, "code_identity", lambda: dict(TRUSTED))
    oos = R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, "2025-03-17", "2025-03-18")
    mp = oos["folder"] / "manifest.json"
    m = json.loads(mp.read_text())
    m.pop("manifest_sha256")
    m["evaluation"]["evidence"] = "POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE"      # a forger who also re-hashes
    m["manifest_sha256"] = text_sha256(json.dumps(m, sort_keys=True, default=str))
    mp.write_text(json.dumps(m))
    assert R.verify_manifest(oos["folder"])["ok"]
    with pytest.raises(ConfigError, match="only 'POSITIVE EXPECTANCY"):              # the LEDGER label decides
        _promote(env, "OOS_VALIDATED", run=oos["folder"])


def test_monte_carlo_evidence_must_come_from_the_verified_run(env, monkeypatch, tmp_path):
    pv = load_version("vwap_mean_reversion", "V1-C2-SWING", env.app)
    st = G.store()
    for a, b in (("RESEARCH", "BACKTESTED"), ("BACKTESTED", "OOS_VALIDATED")):
        st.append({"kind": "transition", "strategy": "vwap_mean_reversion", "version": "V1-C2-SWING",
                   "version_sha256": pv.sha256, "from": a, "to": b, "approved_by": "T", "statement": "fixture",
                   "evidence": []})
    r = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", MNQ, purpose="train", start="2025-03-10",
                       end="2025-03-12")
    fake = tmp_path / "mc.json"
    fake.write_text(json.dumps({"strategy": {"sha256": pv.sha256}, "run": {"manifest_sha256": "0" * 64}}))
    with pytest.raises(ConfigError, match="not produced from this"):
        _promote(env, "SHADOW_APPROVED", run=r["folder"], monte_carlo=fake)


def test_suspension_blocks_new_entries_of_a_running_paper_session(env):
    """AUDIT: approval used to be checked only at start. A running session now re-reads the promotion state."""
    from src.research.paper_binding import binding_for
    from tests.paper_helpers import session

    pv = load_version("vwap_mean_reversion", "V1-C2-SWING", env.app)
    st = G.store()
    for a, b in (("RESEARCH", "BACKTESTED"), ("BACKTESTED", "OOS_VALIDATED"), ("OOS_VALIDATED", "SHADOW_APPROVED")):
        st.append({"kind": "transition", "strategy": "vwap_mean_reversion", "version": "V1-C2-SWING",
                   "version_sha256": pv.sha256, "from": a, "to": b, "approved_by": "T", "statement": "fixture",
                   "evidence": []})
    b = binding_for("vwap_mean_reversion", "V1-C2-SWING", env.app, None, use="shadow")
    s = session(env.out_root / "paper", [], binding=b)
    from tests.paper_helpers import T

    t = T("10:00")
    s.last_event_ts = t
    s.health.healthy = lambda root, ts: (True, "")
    assert s.system_state("MES", t) == (True, "")
    st.append({"kind": "transition", "strategy": "vwap_mean_reversion", "version": "V1-C2-SWING",
               "version_sha256": pv.sha256, "from": "SHADOW_APPROVED", "to": "SUSPENDED", "approved_by": "T",
               "statement": "suspended", "evidence": []})
    s._gov_cache = None                                   # the 30-second re-read interval has elapsed
    assert s.system_state("MES", t) == (False, "strategy_not_approved")
    G.GOVERNANCE_FILE.write_text("corrupt\n")
    s._gov_cache = None
    assert s.system_state("MES", t) == (False, "strategy_not_approved")
    # the RTH strategy's flatten time is the earlier RTH end (16:00 New York minus 5 minutes), not Globex 17:00
    assert s.flatten_time(pd.Timestamp("2025-03-10")) == T("15:55")
    s.journal.close()
