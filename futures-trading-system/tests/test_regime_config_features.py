"""Sprint 4: regime configuration and causal features."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from src.ai.market_regime.config import DEFAULT_REGIME_CONFIG_PATH, load_regime_config
from src.ai.market_regime.features import ML_FEATURES, add_shock_features
from src.config import ConfigError
from tests.regime_helpers import APP, RC, features, regime_market

COLS = list(ML_FEATURES) + ["rf_valid", "rf_atr_hist_n"]


def same(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    return bool(((a == b) | (a.isna() & b.isna())).all().all())


# ----------------------------------------------------------------- configuration

def test_default_config():
    assert RC.enabled and RC.enforcement == "block_unfavorable" and RC.classifier == "rules"
    assert RC.features.volatility_reference == "time_of_day" and RC.validation.purge_bars >= RC.labels.horizon_bars


@pytest.mark.parametrize("extra", ["[account_rules]\nenabled = false\n", "[risk]\nmax_daily_loss = 0\n",
                                   "[backtest]\nx = 1\n"])
def test_regime_config_cannot_touch_account_or_risk(tmp_path, extra):
    p = tmp_path / "r.toml"
    p.write_text(DEFAULT_REGIME_CONFIG_PATH.read_text() + "\n" + extra, encoding="utf-8")
    with pytest.raises(ConfigError, match="Unknown section"):
        load_regime_config(p)


@pytest.mark.parametrize("old,new", [
    ('enforcement = "block_unfavorable"', 'enforcement = "auto_trade"'),
    ('classifier = "rules"', 'classifier = "ml"'),                 # ml without a model path
    ("purge_bars = 12", "purge_bars = 3"),                         # purge shorter than the label horizon
    ("strong_trend_spread_atr = 0.75", "strong_trend_spread_atr = 0.1"),
    ("vol_extreme_pct = 0.97", "vol_extreme_pct = 0.5"),
    ("high = 0.65", "high = 0.4"),
    ('min_confidence_for_favorable = "MEDIUM"', 'min_confidence_for_favorable = "SURE"'),
    ("efficiency_window = 12", "efficiency_window = 0"),
    ("max_recent_stopouts = 2", "max_recent_stopouts = 2\nallow_override_accountguard = true"),
])
def test_invalid_regime_config(tmp_path, old, new):
    text = DEFAULT_REGIME_CONFIG_PATH.read_text()
    assert old in text
    p = tmp_path / "r.toml"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_regime_config(p)


# ----------------------------------------------------------------- causality

@pytest.mark.parametrize("tf", ["5min", "15min"])
def test_features_truncation_invariant(tf):
    md, _, _ = regime_market()
    frame = md.frames[tf]
    full = features(frame)
    for cut in (len(frame) // 3, len(frame) // 2, len(frame) - 1):
        assert same(full[COLS].iloc[:cut], features(frame.iloc[:cut])[COLS]), cut


def test_features_ignore_future_perturbation():
    md, _, _ = regime_market()
    frame = md.frames["5min"].copy()
    cut = len(frame) // 2
    shocked = frame.copy()
    tail = shocked.index[cut:]
    for col in ("open", "high", "low", "close", "ema_9", "ema_21", "vwap"):
        shocked.loc[tail, col] = shocked.loc[tail, col] * 1.05
    shocked.loc[tail, "atr_14"] = shocked.loc[tail, "atr_14"] * 3
    assert same(features(frame)[COLS].iloc[:cut], features(shocked)[COLS].iloc[:cut])


def test_shock_feature_causal():
    md, _, _ = regime_market()
    f1 = md.frames["1min"]
    full = add_shock_features(f1, RC.features)["rf_shock_ratio"]
    part = add_shock_features(f1.iloc[:3000], RC.features)["rf_shock_ratio"]
    assert ((full.iloc[:3000] == part) | (full.iloc[:3000].isna() & part.isna())).all()


def test_time_of_day_volatility_uses_previous_sessions_only():
    md, _, _ = regime_market(sessions=8)
    f = features(md.frames["5min"])
    sessions = sorted(pd.to_datetime(f["session_date"]).dropna().unique())
    first5 = f[pd.to_datetime(f["session_date"]).isin(sessions[:5])]
    assert first5["rf_atr_pct"].isna().all()  # fewer than 5 earlier sessions at that time of day
    later = f[pd.to_datetime(f["session_date"]).isin(sessions[5:])]
    assert later["rf_atr_pct"].notna().mean() > 0.9 and later["rf_atr_hist_n"].max() <= len(sessions) - 1


# ----------------------------------------------------------------- data quality

def test_incomplete_bar_and_gap_invalidate_features():
    md, _, _ = regime_market()
    frame = md.frames["5min"].copy()
    i = len(frame) - 10
    frame.iloc[i, frame.columns.get_loc("is_complete")] = False
    f = features(frame)
    span = max(RC.features.range_window, RC.features.efficiency_window, RC.features.body_window)
    assert not f["rf_valid"].iloc[i:i + span].any()
    gapped = md.frames["5min"].drop(md.frames["5min"].index[i])
    g = features(gapped)
    assert not g["rf_valid"].iloc[i:i + span - 1].any() and not g["rf_contiguous"].iloc[i]


def test_invalid_indicators_give_invalid_features():
    md, _, _ = regime_market()
    frame = md.frames["5min"].copy()
    frame.iloc[-1, frame.columns.get_loc("atr_14")] = np.nan
    frame.iloc[-2, frame.columns.get_loc("atr_14")] = 0.0
    f = features(frame)
    assert not f["rf_valid"].iloc[-1] and not f["rf_valid"].iloc[-2]


def test_out_of_order_or_duplicate_timestamps_rejected():
    md, _, _ = regime_market()
    frame = md.frames["5min"]
    with pytest.raises(ValueError, match="increasing"):
        features(frame.iloc[::-1])
    with pytest.raises(ValueError):
        features(pd.concat([frame.iloc[:5], frame.iloc[4:6]]))


def test_insufficient_history_is_invalid():
    md, _, _ = regime_market()
    f = features(md.frames["5min"].iloc[:RC.features.min_history_bars - 2])
    assert not f["rf_valid"].any()


def test_session_transitions():
    md, _, _ = regime_market()
    f = features(md.frames["5min"])
    sd = pd.to_datetime(f["session_date"])
    first_bars = f[sd.ne(sd.shift(1))]
    assert (first_bars["rf_session_minutes"].dropna() == 0).all()
    s0 = sd.dropna().unique()[0]
    assert f.loc[sd == s0, "rf_prev_session_trend"].isna().all()  # no previous session in the file
    assert f.loc[sd == sd.dropna().unique()[1], "rf_prev_session_trend"].notna().all()


def test_mes_and_mnq_features():
    for root in ("MES", "MNQ"):
        md, _, _ = regime_market(root=root)
        f = features(md.frames["5min"])
        assert f["rf_valid"].sum() > 0 and md.contract.root == root
