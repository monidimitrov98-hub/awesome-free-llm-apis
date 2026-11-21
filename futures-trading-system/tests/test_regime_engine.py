"""Sprint 4: RegimeEngine snapshots -- timeframe sync, staleness, missing data, confidence."""

from dataclasses import replace
from functools import lru_cache
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.ai.market_regime.confidence import BinnedCalibrator
from src.ai.market_regime.features import add_shock_features
from src.ai.market_regime.models import ConfidenceLevel
from src.ai.market_regime.regime import RegimeEngine
from src.config import ConfigError
from tests.regime_helpers import RC, features, regime_market


@lru_cache(maxsize=1)
def frames():
    md, _, _ = regime_market(sessions=8)
    return features(md.frames["5min"]), features(md.frames["15min"]), add_shock_features(md.frames["1min"], RC.features)


def ctx_at(i, *, sig=None, c15=None, c1=None, complete=True):
    s5, s15, s1 = frames()
    s5 = s5 if sig is None else sig
    bar = s5.iloc[i]
    now = bar["available_at"]
    c15 = s15 if c15 is None else c15
    c1 = s1 if c1 is None else c1
    return SimpleNamespace(now=now, bar=bar, bar_is_complete=complete, session_date=bar["session_date"],
                           context={"15min": c15[c15["available_at"] <= now], "1min": c1[c1["available_at"] <= now]})


def valid_index():
    s5 = frames()[0]
    ok = np.flatnonzero(s5["rf_valid"].to_numpy())
    return int(ok[len(ok) // 2])


def test_valid_snapshot_uses_only_completed_bars():
    i = valid_index()
    snap = RegimeEngine(RC, "MES").snapshot(ctx_at(i))
    assert snap.valid and snap.classification is not None
    assert snap.bar_time == frames()[0].index[i] and snap.time == frames()[0]["available_at"].iloc[i]
    assert snap.confidence is None and snap.confidence_level is ConfidenceLevel.LOW  # uncalibrated -> conservative
    assert "uncalibrated_confidence" in snap.reasons


def test_snapshot_ignores_future_rows():
    i = valid_index()
    eng = RegimeEngine(RC, "MES")
    a = eng.snapshot(ctx_at(i))
    s5, s15, s1 = frames()
    noisy15, noisy1 = s15.copy(), s1.copy()
    later15 = noisy15["available_at"] > s5["available_at"].iloc[i]
    noisy15.loc[later15, ["rf_spread_atr", "rf_efficiency"]] = [-9.0, 0.99]
    noisy1.loc[noisy1["available_at"] > s5["available_at"].iloc[i], "rf_shock_ratio"] = 50.0
    b = eng.snapshot(ctx_at(i, c15=noisy15, c1=noisy1))
    assert a.to_dict() == b.to_dict()


def test_unfinished_stale_and_other_session_context_is_never_used():
    i = valid_index()
    eng = RegimeEngine(RC, "MES")
    s5, s15, _ = frames()
    now = s5["available_at"].iloc[i]
    # an unfinished 15-min bar leaking into the context must be refused
    leak = s15[s15.index <= now].copy()
    leak.iloc[-1, leak.columns.get_loc("available_at")] = now + pd.Timedelta(minutes=10)
    ctx = ctx_at(i)
    ctx.context["15min"] = leak
    snap = eng.snapshot(ctx)
    assert snap.context is None and "context_15min_unfinished" in snap.reasons
    # stale context (last row > 30 minutes old)
    ctx = ctx_at(i)
    ctx.context["15min"] = s15[s15["available_at"] <= now - pd.Timedelta(minutes=45)]
    snap = eng.snapshot(ctx)
    assert snap.context is None and any(r.startswith("context_15min_stale") or r.startswith("context_15min_other")
                                        for r in snap.reasons)
    # incomplete context bar
    ctx = ctx_at(i)
    inc = ctx.context["15min"].copy()
    inc.iloc[-1, inc.columns.get_loc("is_complete")] = False
    ctx.context["15min"] = inc
    assert "context_15min_incomplete" in eng.snapshot(ctx).reasons
    # stale 1-minute data: shock check unavailable (reason recorded)
    ctx = ctx_at(i)
    ctx.context["1min"] = ctx.context["1min"].iloc[:-10]
    assert "fast_1min_stale" in eng.snapshot(ctx).reasons


def test_invalid_inputs_give_insufficient_snapshot():
    i = valid_index()
    eng = RegimeEngine(RC, "MES")
    snap = eng.snapshot(ctx_at(i, complete=False))
    assert not snap.valid and snap.confidence_level is ConfidenceLevel.INSUFFICIENT
    s5 = frames()[0].copy()
    s5.iloc[i, s5.columns.get_loc("rf_valid")] = False
    assert not eng.snapshot(ctx_at(i, sig=s5)).valid
    ctx = ctx_at(i)
    ctx.now = ctx.now + pd.Timedelta(minutes=5)  # bar is not the latest completed bar
    assert "signal_bar_stale" in eng.snapshot(ctx).reasons
    with pytest.raises(ValueError, match="missing"):
        eng.snapshot(ctx_at(i, sig=frames()[0].drop(columns=["rf_spread_atr"])))


def test_calibrated_confidence_levels(tmp_path):
    i = valid_index()
    s5 = frames()[0]
    snap0 = RegimeEngine(RC, "MES").snapshot(ctx_at(i))
    from src.ai.market_regime.classifier import predicted_class

    p = predicted_class(snap0.classification.direction, snap0.classification.trend.value)
    score = snap0.classification.trend_score
    good = BinnedCalibrator().fit(np.full(100, p), np.full(100, score), np.r_[np.full(80, p), np.full(20, 9.0)])
    path = tmp_path / "cal.json"
    good.save(path)
    eng = RegimeEngine(replace(RC, calibration_path=str(path)), "MES")
    snap = eng.snapshot(ctx_at(i))
    assert snap.confidence == pytest.approx(0.8) and snap.confidence_level is ConfidenceLevel.HIGH
    assert "calibration_sha256" in eng.artifacts
    import json

    doc = json.loads(path.read_text())
    for key, cells in doc["calibrator"]["table"].items():
        doc["calibrator"]["table"][key] = [[c[1], c[1]] for c in cells]
    path.write_text(json.dumps(doc))  # tampered: claims 100% agreement
    with pytest.raises(ConfigError):
        RegimeEngine(replace(RC, calibration_path=str(path)), "MES")


def test_mnq_snapshot():
    md, _, _ = regime_market(root="MNQ")
    s5 = features(md.frames["5min"])
    i = int(np.flatnonzero(s5["rf_valid"].to_numpy())[-1])
    ctx = SimpleNamespace(now=s5["available_at"].iloc[i], bar=s5.iloc[i], bar_is_complete=True,
                          session_date=s5["session_date"].iloc[i], context={})
    snap = RegimeEngine(replace(RC, context_timeframe="", fast_timeframe=""), "MNQ").snapshot(ctx)
    assert snap.valid and snap.instrument == "MNQ"
