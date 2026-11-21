"""Sprint 4: historical validation integrity, model identity/reproducibility, and the
end-to-end integration of the regime gate with the real strategy and backtest engine."""

import json
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import run_regime_validation as rrv
from src.ai.market_regime import ml
from src.ai.market_regime.features import ML_FEATURES
from src.ai.market_regime.identity import instrument_key, model_identity
from src.ai.market_regime.labels import forward_labels
from src.ai.market_regime.validation import build_dataset, evaluate_instrument, folds, purged_train_mask
from src.backtest.config import load_backtest_config
from src.backtest.manifest import read_config_texts, regime_artifact_hashes
from src.backtest.runner import run_strategy
from src.config import ConfigError
from src.strategy.vwap_pullback_config import load_strategy_config
from tests.regime_helpers import APP, RC, regime_market

SC = load_strategy_config()
KW = dict(ema_fast=SC.ema_fast_col, ema_slow=SC.ema_slow_col, atr_col=SC.atr_col, rsi_col=SC.rsi_col)
SMALL = replace(RC, validation=replace(RC.validation, train_sessions=6, test_sessions=2),
                ml=replace(RC.ml, min_train_samples=100, n_estimators=30))


@lru_cache(maxsize=2)
def dataset(root="MES"):
    md, truth, path = regime_market(root=root, sessions=14, seed=11)
    return build_dataset(md.frames["5min"], SMALL, APP.session, **KW), md, truth, path


# ----------------------------------------------------------------- labels

def test_labels_hand_calculated_and_never_features():
    idx = pd.date_range("2025-03-10 14:00", periods=6, freq="5min", tz="UTC")
    bars = pd.DataFrame({"close": [100, 101, 102, 103, 102, 101.0], "atr_14": 1.0, "session_date": pd.Timestamp("2025-03-10"),
                         "available_at": idx + pd.Timedelta(minutes=5)}, index=idx)
    lab = forward_labels(bars, replace(RC.labels, horizon_bars=3, trend_efficiency=0.5, trend_move_atr=1.0))
    assert lab["label"].iloc[0] == 1.0          # +3 over 3 bars, efficiency 1
    assert lab["label"].iloc[2] == 0.0          # +1 -1 -1 -> net -1, efficiency 1/3
    assert lab["label"].iloc[3:].isna().all()   # horizon beyond the data
    assert lab["label_end"].iloc[0] == idx[3]
    assert not any(c.startswith(("label", "fwd_")) for c in ML_FEATURES)


def test_labels_do_not_cross_sessions_or_gaps():
    ds, *_ = dataset()
    ok = ds["label"].notna()
    assert (pd.to_datetime(ds.loc[ok, "label_end"]) > ds.index[ok]).all()
    end_session = ds["session"].reindex(pd.to_datetime(ds.loc[ok, "label_end"])).to_numpy()
    assert (end_session == ds.loc[ok, "session"].to_numpy()).all()


# ----------------------------------------------------------------- folds / purge / embargo

def test_folds_are_chronological_and_disjoint():
    s = list(pd.date_range("2025-01-01", periods=20, freq="D"))
    fs = folds(s, 6, 3)
    assert len(fs) == 4
    for tr, te in fs:
        assert max(tr) < min(te) and not set(tr) & set(te)
    assert [te[0] for _, te in fs] == sorted(te[0] for _, te in fs)


def test_purge_and_embargo():
    ds, *_ = dataset()
    step = pd.Timedelta(minutes=5)
    sessions = sorted(ds["session"].dropna().unique())
    test_start = ds.index[ds["session"] == sessions[8]][0]
    mask = purged_train_mask(ds, sessions[:8], test_start, step, SMALL.validation.embargo_bars)
    assert mask.sum() > 0
    assert pd.to_datetime(ds.loc[mask, "label_end"]).max() < test_start - SMALL.validation.embargo_bars * step
    assert ds.loc[mask, "label"].notna().all() and ds.loc[mask, "rf_valid"].all()


def test_walk_forward_report_sealed_oos_and_separate_instruments():
    for root in ("MES", "MNQ"):
        ds, md, *_ = dataset(root)
        rep = evaluate_instrument(ds, SMALL, oos_sessions=3, reveal_oos=False)
        assert isinstance(rep["final_oos"], str) and rep["final_oos"].startswith("SEALED")
        final_start = pd.Timestamp(rep["final_oos_sessions"][0])
        for f in rep["walk_forward"]:
            assert pd.Timestamp(f["test"][1]) < final_start            # folds never touch the final OOS
            assert pd.Timestamp(f["max_train_label_end"]) < pd.Timestamp(f["test_start"])
            r = f["rules"]
            assert set(r["per_class"]) == {"trend_down", "no_trend", "trend_up"}
            assert np.array(r["confusion_matrix"]["rows_true_cols_pred"]).sum() == r["n"]
            assert "calibration" in r and "stability_by_session" in r and "transitions" in r
        assert "pooled_walk_forward_rules" in rep and md.contract.root == root
        revealed = evaluate_instrument(ds, SMALL, oos_sessions=3, reveal_oos=True)
        assert isinstance(revealed["final_oos"], dict) and "rules" in revealed["final_oos"]


def test_ml_training_never_sees_test_labels(monkeypatch):
    """Poison every label from the test period onwards: fold results must not change."""
    ds, *_ = dataset()
    rep = evaluate_instrument(ds, SMALL, oos_sessions=3, reveal_oos=False)
    f1 = rep["walk_forward"][0]
    poisoned = ds.copy()
    later = poisoned.index >= pd.Timestamp(f1["test_start"])
    shuffled = poisoned.loc[later, "label"].sample(frac=1.0, random_state=1).to_numpy()
    poisoned.loc[later, "label"] = shuffled
    rep2 = evaluate_instrument(poisoned, SMALL, oos_sessions=3, reveal_oos=False)
    assert rep2["walk_forward"][0]["train_rows"] == f1["train_rows"]
    # the fitted calibration / model depend only on training rows -> identical predictions
    a, b = f1.get("ml", {}), rep2["walk_forward"][0].get("ml", {})
    if "error" not in a:
        assert a["fit_rows"] == b["fit_rows"] and a["calibration_rows"] == b["calibration_rows"]


# ----------------------------------------------------------------- model identity & artifacts

def test_model_identity_covers_everything():
    base = model_identity(RC, {"MES": "a"}, "src")["fingerprint"]
    variants = [model_identity(RC, {"MES": "b"}, "src"), model_identity(RC, {"MES": "a"}, "src2"),
                model_identity(replace(RC, labels=replace(RC.labels, horizon_bars=10),
                                       validation=replace(RC.validation, purge_bars=12)), {"MES": "a"}, "src"),
                model_identity(replace(RC, ml=replace(RC.ml, max_depth=5)), {"MES": "a"}, "src"),
                model_identity(replace(RC, features=replace(RC.features, efficiency_window=10)), {"MES": "a"}, "src"),
                model_identity(replace(RC, classification=replace(RC.classification, tight_range_atr=2.5)), {"MES": "a"}, "src")]
    assert len({v["fingerprint"] for v in variants} | {base}) == len(variants) + 1
    assert model_identity(RC, {"MES": "a"}, "src")["fingerprint"] == base  # deterministic
    assert instrument_key("MES", True) == "MES:synthetic" and instrument_key("MES", False) == "MES"


def test_ml_fit_is_reproducible_and_tamper_evident(tmp_path):
    ds, *_ = dataset()
    tr = ds[ds["label"].notna() & ds["rf_valid"]]
    x, y = ml.feature_matrix(tr), tr["label"].to_numpy(float)
    m1, m2 = ml.fit(SMALL.ml, x, y), ml.fit(SMALL.ml, x, y)
    assert np.array_equal(ml.predict(m1, SMALL.ml, x)[1], ml.predict(m2, SMALL.ml, x)[1])
    with pytest.raises(ConfigError, match="samples"):
        ml.fit(RC.ml, x[:10], y[:10])
    ident = ml.save_model(m1, tmp_path / "m.pkl", {"fingerprint": "f"})
    model, loaded = ml.load_model(tmp_path / "m.pkl")
    assert loaded["artifact_sha256"] == ident["artifact_sha256"]
    blob = (tmp_path / "m.pkl").read_bytes()
    (tmp_path / "m.pkl").write_bytes(blob[:-1] + bytes([blob[-1] ^ 1]))
    with pytest.raises(ConfigError, match="identity hash"):
        ml.load_model(tmp_path / "m.pkl")
    (tmp_path / "m.pkl").write_bytes(blob)
    doc = json.loads((tmp_path / "m.json").read_text())
    doc["features"] = doc["features"][:-1]
    (tmp_path / "m.json").write_text(json.dumps(doc))
    with pytest.raises(ConfigError, match="feature list"):
        ml.load_model(tmp_path / "m.pkl")


def test_regime_config_and_artifacts_are_part_of_the_configuration_identity(tmp_path):
    from src.ai.market_regime.confidence import BinnedCalibrator
    from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH
    from src.config import DEFAULT_CONFIG_PATH
    from src.strategy.vwap_pullback_config import DEFAULT_STRATEGY_CONFIG_PATH
    from src.ai.market_regime.config import DEFAULT_REGIME_CONFIG_PATH

    cal = tmp_path / "cal.json"
    BinnedCalibrator().fit(np.array([1.0] * 40), np.array([0.9] * 40), np.array([1.0] * 40)).save(cal)
    rpath = tmp_path / "regime.toml"
    rpath.write_text(DEFAULT_REGIME_CONFIG_PATH.read_text().replace('calibration_path = ""',
                                                                    f'calibration_path = "{cal.as_posix()}"'))
    t1 = read_config_texts(DEFAULT_CONFIG_PATH, DEFAULT_BACKTEST_CONFIG_PATH, DEFAULT_STRATEGY_CONFIG_PATH, rpath)
    assert json.loads(t1["regime_artifacts"])["calibration_path"]
    BinnedCalibrator().fit(np.array([1.0] * 40), np.array([0.9] * 40), np.array([-1.0] * 40)).save(cal)
    t2 = read_config_texts(DEFAULT_CONFIG_PATH, DEFAULT_BACKTEST_CONFIG_PATH, DEFAULT_STRATEGY_CONFIG_PATH, rpath)
    assert t1["regime_artifacts"] != t2["regime_artifacts"]
    cal.unlink()
    with pytest.raises(ConfigError, match="not found"):
        regime_artifact_hashes(rpath)


# ----------------------------------------------------------------- CLI safeguards

def test_cli_refuses_reveal_from_untrusted_code_or_without_ledger(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rrv, "code_identity", lambda: {"trusted": False, "untrusted_reasons": ["dirty"],
                                                       "source_sha256": "x", "commit": None})
    args = ["--synthetic-sessions", "26", "--oos-sessions", "3", "--output-dir", str(tmp_path / "o"),
            "--ledger", str(tmp_path / "ledger.jsonl")]
    assert rrv.main(args + ["--reveal-oos"]) == 6
    monkeypatch.setattr(rrv, "code_identity", lambda: {"trusted": True, "untrusted_reasons": [],
                                                       "source_sha256": "x", "commit": "a" * 40})
    assert rrv.main(args + ["--reveal-oos"]) == 5
    assert not (tmp_path / "ledger.jsonl").exists()


def test_cli_sealed_run_writes_reports_per_instrument(tmp_path, monkeypatch):
    monkeypatch.setattr(rrv, "code_identity", lambda: {"trusted": False, "untrusted_reasons": ["dirty"],
                                                       "source_sha256": "x", "commit": None})
    out = tmp_path / "o"
    assert rrv.main(["--synthetic-sessions", "26", "--oos-sessions", "3", "--output-dir", str(out),
                     "--ledger", str(tmp_path / "ledger.jsonl")]) == 0
    for key in ("MES_synthetic", "MNQ_synthetic"):
        rep = json.loads((out / key / "report.json").read_text())
        man = json.loads((out / key / "manifest.json").read_text())
        assert rep["final_oos"].startswith("SEALED") and man["synthetic"] and man["model_identity"]["fingerprint"]
    assert not (tmp_path / "ledger.jsonl").exists()  # a sealed run records nothing


# ----------------------------------------------------------------- end to end with the real strategy

@lru_cache(maxsize=1)
def e2e():
    """The bundled SYNTHETIC MNQ sample yields one strategy signal (the regime data above yields none)."""
    from src.pipeline import build_market_data
    from tests.conftest import SAMPLES

    md = build_market_data(SAMPLES / "SYNTHETIC_MNQ_1m.csv", APP)
    bt = load_backtest_config()
    plain = run_strategy(md, APP, bt, SC)
    gated = run_strategy(md, APP, bt, SC, regime=RC)
    observe = run_strategy(md, APP, bt, SC, regime=replace(RC, enforcement="observe"))
    return plain, gated, observe


def test_observe_mode_changes_nothing_and_gate_only_removes_entries():
    plain, gated, observe = e2e()
    cols = ["entry_time", "side", "quantity", "entry_price", "stop_loss", "exit_time", "net_pnl"]
    assert len(plain.result.trades) == 1
    assert observe.result.trades[cols].reset_index(drop=True).equals(plain.result.trades[cols].reset_index(drop=True))
    dec = gated.regime["decisions"]
    assert len(dec) == 1 and dec["signal_id"].notna().all()
    blocked = dec[~dec["allowed"]]
    assert blocked["suitability"].isin(["NO_TRADE", "UNFAVORABLE"]).all()
    assert len(gated.result.trades) == len(plain.result.trades) - len(blocked)  # nothing added, nothing resized
    assert gated.result.entry_checks.shape[0] == plain.result.entry_checks.shape[0] - len(blocked)
    snaps = gated.regime["snapshots"]
    assert (pd.to_datetime(snaps["bar_time"]) < pd.to_datetime(snaps["time"])).all()  # completed bars only
    assert gated.regime["errors"] == 0


def test_regime_outputs_and_performance_shape():
    _, gated, observe = e2e()
    assert gated.regime["performance"]["gate"]["blocked"] == 1
    assert json.dumps(gated.regime["dashboard"], default=str)
    trades = observe.regime["trades"]
    assert len(trades) == 1
    for col in ("suitability", "confidence_level", "trend", "volatility", "allowed", "trend_at_entry", "mae_dollars", "mfe_dollars"):
        assert col in trades.columns, col
    assert trades["mae_dollars"].iloc[0] >= 0 and trades["mfe_dollars"].iloc[0] >= 0
    assert trades["trend_at_entry"].iloc[0] not in (None, "unavailable")
    perf = observe.regime["performance"]
    assert perf["overall"]["trades"] == 1 and perf["by"]["suitability"]


def test_blocked_signal_keeps_the_real_strategy_lifecycle_consistent():
    from src.ai.market_regime.filters import RegimeGatedStrategy
    from src.ai.market_regime.regime import RegimeEngine
    from src.backtest.engine import BacktestEngine
    from src.backtest.runner import _frames, _regime_frames
    from src.pipeline import build_market_data
    from src.strategy.vwap_pullback import VwapTrendPullbackStrategy
    from tests.conftest import SAMPLES

    md = build_market_data(SAMPLES / "SYNTHETIC_MNQ_1m.csv", APP)
    _, execution, context = _frames(md, SC)
    signal, rctx = _regime_frames(md, APP, SC, RC)
    strat = VwapTrendPullbackStrategy(SC, APP.session.timezone)
    gate = RegimeGatedStrategy(strat, RegimeEngine(RC, "MNQ"), RC)
    res = BacktestEngine(load_backtest_config(), md.contract.spec, APP.session).run(
        gate, signal, execution, context_bars={**context, **rctx})
    assert res.trades.empty and len(gate.decisions) == 1 and not gate.decisions[0]["allowed"]
    assert strat.lifecycle_violations == []
    assert gate.decisions[0]["order_status"] == "blocked"
