"""M2: every filter is independently switchable (fail when on, no effect when off).

Each case modifies the valid LONG fixture so that exactly ONE check fails, then shows
that switching off just that filter turns the setup into a signal.
"""

from dataclasses import replace

import pytest

from src.config import ConfigError
from src.strategy.vwap_pullback import Reason, VwapTrendPullbackStrategy
from src.strategy.vwap_pullback_config import load_strategy_config
from tests.strategy_fixtures import MES, NY, context_bars, ctx_for, long_bars, set_at

CFG = load_strategy_config()
ATR_STOP = replace(CFG, stop=replace(CFG.stop, method="atr"))


def sec(cfg, name, **kw):
    return replace(cfg, **{name: replace(getattr(cfg, name), **kw)})


def evaluate(bars, cfg, ctx=None):
    s = VwapTrendPullbackStrategy(cfg, NY)
    s.on_start(MES)
    s.on_bar(ctx_for(bars, ctx if ctx is not None else context_bars(), MES))
    return s.diagnostics_frame().set_index("direction").loc["long"]


B = long_bars
CASES = [
    # (name, bars, base cfg, failing code, cfg with only that filter disabled)
    ("vwap", set_at(B(), -1, "vwap", 5031.0), CFG, Reason.PRICE_VS_VWAP,
     sec(CFG, "trend", require_price_vs_vwap=False)),
    ("ema_alignment", set_at(B(), -1, "ema_21", 5029.0), CFG, Reason.EMA_ALIGNMENT,
     sec(CFG, "trend", require_ema_alignment=False)),
    ("trend_strength", set_at(B(), -1, "ema_21", 5027.5), CFG, Reason.TREND_TOO_WEAK,
     sec(CFG, "trend", min_trend_strength_atr=0.0)),
    ("structure", set_at(B(), 14, "low", 4999.0), ATR_STOP, Reason.STRUCTURE,
     sec(ATR_STOP, "trend", require_structure=False)),
    ("pullback_structure_intact", set_at(B(), -2, "low", 5008.0), sec(CFG, "pullback", max_penetration_atr=5.0),
     Reason.PULLBACK_BROKE_STRUCTURE,
     sec(sec(CFG, "pullback", max_penetration_atr=5.0), "pullback", require_structure_intact=False)),
    ("extension_vwap", set_at(B(), -1, "vwap", 5016.0), CFG, Reason.EXTENDED_FROM_VWAP,
     sec(CFG, "extension", max_vwap_distance_atr=0.0)),
    ("extension_ema", B(), sec(CFG, "extension", max_ema_distance_atr=0.25), Reason.EXTENDED_FROM_EMA,
     sec(CFG, "extension", max_ema_distance_atr=0.0)),
    ("directional_close", set_at(B(), -1, "open", 5031.0), CFG, Reason.NO_DIRECTIONAL_CLOSE,
     sec(CFG, "confirmation", require_directional_close=False)),
    ("close_beyond_prev", set_at(B(), -2, "high", 5031.0),
     sec(CFG, "confirmation", require_close_beyond_prev_extreme=True), Reason.NO_CLOSE_BEYOND_PREV_EXTREME, CFG),
    ("close_beyond_ema", set_at(B(), -1, "ema_9", 5031.0), CFG, Reason.NO_CLOSE_BEYOND_EMA,
     sec(CFG, "confirmation", require_close_beyond_ema_fast=False)),
    ("rsi", set_at(B(), [-3, -2, -1], "rsi_14", [58.0, 55.0, 56.0]), CFG, Reason.RSI_NO_PULLBACK,
     sec(CFG, "rsi", enabled=False)),
    ("rsi_turn", set_at(B(), [-3, -2, -1], "rsi_14", [49.0, 56.0, 55.0]), CFG, Reason.RSI_NOT_TURNING,
     sec(CFG, "rsi", require_turn=False)),
    ("rvol", set_at(B(), -1, "rvol", 0.8), CFG, Reason.RVOL_TOO_LOW, sec(CFG, "rvol", enabled=False)),
    ("stop_max", set_at(B(), list(range(23)), "atr_14", 6.0), CFG, Reason.STOP_TOO_WIDE,
     sec(CFG, "stop", max_distance_atr=0.0)),
    ("stop_min", B(), sec(CFG, "stop", min_distance_atr=2.9), Reason.STOP_TOO_TIGHT,
     sec(CFG, "stop", min_distance_atr=0.0, min_distance_ticks=0)),
    ("session_window", long_bars("07:30"), CFG, Reason.OUTSIDE_STRATEGY_WINDOW,
     sec(CFG, "filters", session_windows=())),
]


@pytest.mark.parametrize("name,bars,base,code,off", CASES, ids=[c[0] for c in CASES])
def test_filter_fails_when_on_and_is_removed_when_off(name, bars, base, code, off):
    ctx = context_bars("07:30") if name == "session_window" else None
    on = evaluate(bars, base, ctx)
    assert on.decision in ("reject", "skip") and on.reasons.split(";") == [code], on.reasons
    assert evaluate(bars, off, ctx).decision == "signal"


@pytest.mark.parametrize("flag,code", [("require_price_vs_vwap", Reason.CONTEXT_VWAP),
                                       ("require_ema_alignment", Reason.CONTEXT_EMA)])
def test_context_filters_independent(flag, code):
    col = "vwap" if flag == "require_price_vs_vwap" else "ema_9"
    ctx = set_at(context_bars(), 6, col, 6000.0 if col == "vwap" else 4000.0)
    assert evaluate(B(), CFG, ctx).reasons.split(";") == [code]
    assert evaluate(B(), sec(CFG, "context", **{flag: False}), ctx).decision == "signal"
    assert evaluate(B(), replace(CFG, context_timeframe=None), None).decision == "signal"


def test_disabling_ema_alignment_removes_the_direction_requirement():
    # EMA9 5028 below EMA21 5029: misaligned but |gap| = 0.125 ATR >= 0.10 (strength is direction-free).
    off = sec(CFG, "trend", require_ema_alignment=False)
    row = evaluate(set_at(B(), -1, "ema_21", 5029.0), off)
    assert row.decision == "signal" and row.trend_strength_atr == pytest.approx(0.125)


def test_trend_strength_is_separate_from_alignment():
    weak = set_at(B(), -1, "ema_21", 5027.5)  # aligned, gap 0.0625 ATR
    row = evaluate(weak, sec(CFG, "trend", require_ema_alignment=False))
    assert row.reasons.split(";") == [Reason.TREND_TOO_WEAK]  # strength still active on its own


@pytest.mark.parametrize("section,key", [("extension", "max_vwap_distance_atr"), ("extension", "max_ema_distance_atr"),
                                         ("stop", "max_distance_atr"), ("trend", "min_trend_strength_atr"),
                                         ("target", "min_actual_r_fraction")])
def test_zero_is_accepted_as_disabled(section, key):
    sec(CFG, section, **{key: 0.0})  # must not raise


def test_negative_or_out_of_range_values_still_rejected():
    with pytest.raises(ConfigError):
        sec(CFG, "extension", max_vwap_distance_atr=-1.0)
    with pytest.raises(ConfigError):
        sec(CFG, "target", min_actual_r_fraction=1.5)
