"""Sprint 4: Layer-1 rule classification (every category, symmetry) and calibrated confidence."""

import json
import math

import numpy as np
import pytest

from src.ai.market_regime.classifier import classify_row, predicted_class
from src.ai.market_regime.confidence import BinnedCalibrator, calibration_report, confidence_level
from src.ai.market_regime.models import (
    ConfidenceLevel,
    RangeRegime as Rg,
    StructureState as St,
    TrendRegime as Tr,
    VolatilityChange as Vc,
    VolatilityRegime as Vr,
)
from src.config import ConfigError
from tests.regime_helpers import RC, mirror_row, row

K = RC.classification
STRONG = row(rf_spread_atr=1.0, rf_efficiency=0.6, rf_vwap_dist_atr=0.8, rf_structure=1.0, rf_slope_atr=0.3)
MODERATE = row(rf_spread_atr=0.4, rf_efficiency=0.35, rf_vwap_dist_atr=0.5, rf_slope_atr=0.2)


def c(**kw):
    return classify_row(row(**kw), K)


@pytest.mark.parametrize("r,trend,direction", [
    (STRONG, Tr.STRONG_BULLISH, 1), (MODERATE, Tr.MODERATE_BULLISH, 1),
    (mirror_row(STRONG), Tr.STRONG_BEARISH, -1), (mirror_row(MODERATE), Tr.MODERATE_BEARISH, -1),
    (row(rf_spread_atr=0.5, rf_efficiency=0.1, rf_vwap_dist_atr=0.4, rf_slope_atr=-0.1), Tr.WEAK, 1),
    (row(rf_spread_atr=0.1), Tr.NONE, 0),
])
def test_trend_regimes(r, trend, direction):
    out = classify_row(r, K)
    assert out.trend is trend and out.direction == direction


def test_bullish_bearish_symmetry_everywhere():
    for r in (STRONG, MODERATE, row(rf_choch=1.0), row(rf_bos=1.0, rf_structure=1.0), row(rf_failed_breakout=1.0)):
        up, down = classify_row(r, K), classify_row(mirror_row(r), K)
        assert up.direction == -down.direction and up.volatility is down.volatility
        assert up.trend_score == pytest.approx(down.trend_score)
        assert up.trend.value.replace("bullish", "X") == down.trend.value.replace("bearish", "X")


@pytest.mark.parametrize("kw,expected", [
    (dict(rf_range_atr=2.0), Rg.TIGHT_CONSOLIDATION),
    (dict(rf_range_atr=9.0), Rg.WIDE_RANGE),
    (dict(rf_efficiency=0.1, rf_dir_changes=0.8), Rg.CHOPPY),
    (dict(rf_failed_breakout=1.0), Rg.FAILED_BREAKOUT),
    (dict(rf_breakout=1.0, rf_prev_range_atr=2.5, rf_rvol=2.0), Rg.RANGE_TO_TREND),
    (dict(rf_breakout=1.0, rf_prev_range_atr=2.5, rf_rvol=0.9), Rg.TIGHT_CONSOLIDATION if False else Rg.NONE),
])
def test_range_regimes(kw, expected):
    assert c(**kw).range is expected


def test_trending_market_is_not_a_range():
    assert classify_row(STRONG, K).range is Rg.NONE


@pytest.mark.parametrize("pct,n,expected", [
    (0.05, 60, Vr.VERY_LOW), (0.5, 60, Vr.NORMAL), (0.85, 60, Vr.HIGH), (0.99, 60, Vr.EXTREME),
    (0.99, 10, Vr.HIGH),        # 10 observations cannot resolve a 97th percentile
    (0.05, 5, Vr.NORMAL),       # nor a 10th percentile with 5
    (math.nan, 0, Vr.UNKNOWN),
])
def test_volatility_regimes_respect_history_resolution(pct, n, expected):
    assert c(rf_atr_pct=pct, rf_atr_hist_n=n).volatility is expected


@pytest.mark.parametrize("ratio,expected", [(1.8, Vc.EXPANSION), (0.6, Vc.CONTRACTION), (1.0, Vc.STABLE),
                                            (math.nan, Vc.UNKNOWN)])
def test_volatility_change(ratio, expected):
    assert c(rf_atr_ratio=ratio).volatility_change is expected


@pytest.mark.parametrize("kw,expected", [
    (dict(rf_structure=1.0), St.HH_HL), (dict(rf_structure=-1.0), St.LH_LL),
    (dict(rf_bos=1.0), St.BREAK_OF_STRUCTURE_UP), (dict(rf_bos=-1.0), St.BREAK_OF_STRUCTURE_DOWN),
    (dict(rf_choch=1.0), St.CHANGE_OF_CHARACTER_UP), (dict(rf_choch=-1.0), St.CHANGE_OF_CHARACTER_DOWN),
    (dict(rf_struct_failed=1.0), St.FAILED_BREAK), (dict(rf_range_atr=2.0, rf_atr_ratio=0.6), St.COMPRESSION),
    (dict(rf_structure=0.0), St.MIXED), (dict(rf_structure=math.nan), St.INSUFFICIENT),
])
def test_structure_states(kw, expected):
    assert c(**kw).structure is expected


def test_invalid_rows_are_not_classified():
    assert classify_row(row(rf_valid=False), K) is None
    assert classify_row({}, K) is None


def test_predicted_class_mapping():
    assert predicted_class(1, Tr.STRONG_BULLISH.value) == 1 and predicted_class(-1, Tr.MODERATE_BEARISH.value) == -1
    assert predicted_class(1, Tr.WEAK.value) == 0 and math.isnan(predicted_class(0, None))


# ----------------------------------------------------------------- confidence

def test_calibrator_hand_calculated_and_levels(tmp_path):
    pred = np.array([1.0] * 40 + [0.0] * 10)
    score = np.array([0.95] * 40 + [0.15] * 10)
    label = np.array([1.0] * 30 + [-1.0] * 10 + [0.0] * 10)
    cal = BinnedCalibrator().fit(pred, score, label)
    assert cal.predict(1.0, 0.92) == (0.75, 40)              # 30 of 40 agreed
    assert cal.predict(0.0, 0.11) == (1.0, 10)
    assert cal.predict(-1.0, 0.5) == (None, 0)               # empty bin
    q = RC.confidence
    assert confidence_level(0.75, 40, q) is ConfidenceLevel.HIGH
    assert confidence_level(0.55, 40, q) is ConfidenceLevel.MEDIUM
    assert confidence_level(0.40, 40, q) is ConfidenceLevel.LOW
    assert confidence_level(1.0, 10, q) is ConfidenceLevel.LOW     # too few samples in the bin
    assert confidence_level(None, 0, q) is ConfidenceLevel.LOW     # uncalibrated: conservative
    assert confidence_level(0.9, 99, q, valid=False) is ConfidenceLevel.INSUFFICIENT
    path = tmp_path / "cal.json"
    cal.save(path)
    assert BinnedCalibrator.load(path).predict(1.0, 0.92) == (0.75, 40)
    doc = json.loads(path.read_text())
    doc["calibrator"]["table"]["1"][9] = [40.0, 40.0]                # tamper: claim 100%
    path.write_text(json.dumps(doc))
    with pytest.raises(ConfigError, match="integrity"):
        BinnedCalibrator.load(path)


def test_calibration_report_hand_calculated():
    rep = calibration_report(np.array([0.8, 0.8, 0.2, 0.2]), np.array([1.0, 0.0, 0.0, 0.0]))
    assert rep["brier"] == pytest.approx((0.04 + 0.64 + 0.04 + 0.04) / 4)
    assert rep["ece"] == pytest.approx(0.5 * abs(0.8 - 0.5) + 0.5 * abs(0.2 - 0.0))
