"""VWAP Trend Pullback: rule-level tests on hand-designed bars (see strategy_fixtures.py)."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from src.backtest.orders import Fill, OrderRole, OrderType, Side
from src.config import ConfigError
from src.strategy.vwap_pullback import Reason, VwapTrendPullbackStrategy
from src.strategy.vwap_pullback_config import (
    DEFAULT_STRATEGY_CONFIG_PATH,
    FilterConfig,
    VwapPullbackConfig,
    load_strategy_config,
)
from tests.strategy_fixtures import (
    LONG_POSITION,
    MES,
    MNQ,
    NY,
    STOP,
    TARGET,
    context_bars,
    ctx_for,
    long_bars,
    mirror,
    set_at,
    short_bars,
    short_context,
)

CFG = load_strategy_config()
LAST, PREV, PREV2 = -1, -2, -3


def decide(bars, context="default", cfg=CFG, contract=MES, strategy=None, **kw):
    if isinstance(context, str):
        context = context_bars()
    s = strategy or VwapTrendPullbackStrategy(cfg, NY)
    if strategy is None:
        s.on_start(contract)
    reqs = s.on_bar(ctx_for(bars, context, contract, **kw))
    d = s.diagnostics_frame().set_index("direction")
    return reqs, d, s


def reason(d, direction="long"):
    return d.loc[direction, "rejection_reason"]


def reasons(d, direction="long"):
    return d.loc[direction, "reasons"].split(";")


# ----------------------------------------------------------------- 1-2 valid setups

def test_both_directions_signalling_on_one_bar_emit_one_entry():
    # Cannot happen with the VWAP filter on; forced here by stubbing the evaluation.
    s = VwapTrendPullbackStrategy(CFG, NY)
    s._evaluate = lambda ctx, d, w, ms, info: info.update(stop_price=1.0, target_price=2.0, pullback_id=d.value) or []
    s.on_start(MES)
    reqs = s.on_bar(ctx_for(long_bars(), context_bars()))
    assert len(reqs) == 1
    assert s.diagnostics_frame()["rejection_reason"].tolist() == ["", Reason.ONE_ENTRY_PER_BAR]


def test_valid_long_hand_calculated():
    reqs, d, _ = decide(long_bars())
    assert len(reqs) == 1
    r = reqs[0]
    assert (r.side, r.order_type, r.stop_loss, r.take_profit) == (Side.LONG, OrderType.MARKET, STOP, TARGET)
    row = d.loc["long"]
    assert row.decision == "signal" and row.rejection_reason == ""
    assert (row.stop_distance, row.target_distance) == (22.0, 44.0)
    assert row.trend_strength_atr == pytest.approx(0.75)  # (5028 - 5022) / 8
    assert row.structure == "bullish" and row.pullback_status == "touched:ema_fast"
    assert d.loc["short", "decision"] == "reject"


@pytest.mark.parametrize("contract,c", [(MES, 10060.0), (MNQ, 40060.0)])
def test_valid_short_mirrors_long(contract, c):
    reqs, d, _ = decide(short_bars(c), short_context(c), contract=contract)
    r = reqs[0]
    assert r.side is Side.SHORT
    assert r.stop_loss == c - 5009 + 0.5 and r.take_profit == c - TARGET  # mirrored hand values
    assert d.loc["short", "decision"] == "signal" and d.loc["short", "structure"] == "bearish"


def test_direction_switches():
    reqs, d, _ = decide(long_bars(), cfg=replace(CFG, enable_long=False))
    assert reqs == [] and list(d.index) == ["short"]
    with pytest.raises(ConfigError):
        replace(CFG, enable_long=False, enable_short=False)


# ----------------------------------------------------------------- 3-6 trend rules

def test_long_rejected_below_vwap():
    _, d, _ = decide(set_at(long_bars(), LAST, "vwap", 5031.0))
    assert reason(d) == Reason.PRICE_VS_VWAP and d.loc["long", "vwap_relation"] == "below"


def test_short_rejected_above_vwap():
    bars = set_at(short_bars(), LAST, "vwap", 10060 - 5031.0 - 2.0)  # VWAP below the short's close
    _, d, _ = decide(bars, short_context())
    assert reason(d, "short") == Reason.PRICE_VS_VWAP and d.loc["short", "vwap_relation"] == "above"


def test_ema_trend_mismatch():
    _, d, _ = decide(set_at(long_bars(), LAST, "ema_9", 5021.0))
    assert reason(d) == Reason.EMA_ALIGNMENT and d.loc["long", "ema_relation"] == "fast_below"


def test_trend_too_weak():
    cfg = replace(CFG, trend=replace(CFG.trend, min_trend_strength_atr=1.0))  # strength is 0.75
    _, d, _ = decide(long_bars(), cfg=cfg)
    assert reason(d) == Reason.TREND_TOO_WEAK


def test_invalid_market_structure():
    bars = set_at(long_bars(), 14, "low", 4999.0)  # L2 4999 < L1 5001: no higher low
    _, d, _ = decide(bars)
    assert reason(d) == Reason.STRUCTURE and d.loc["long", "structure"] == "mixed"
    cfg = replace(CFG, trend=replace(CFG.trend, require_structure=False),
                  stop=replace(CFG.stop, method="atr"))
    _, d, _ = decide(bars, cfg=cfg)
    assert d.loc["long", "decision"] == "signal"  # structure filter independently configurable


# ----------------------------------------------------------------- 7-8 pullback and confirmation

def test_pullback_not_reaching_zone():
    cfg = replace(CFG, pullback=replace(CFG.pullback, levels=("ema_slow",)))  # lows stay >= 5.5 above
    _, d, _ = decide(long_bars(), cfg=cfg)
    assert reason(d) == Reason.PULLBACK_NOT_REACHED and d.loc["long", "pullback_status"] == "not_reached"


def test_pullback_too_deep():
    cfg = replace(CFG, pullback=replace(CFG.pullback, levels=("ema_fast",)))
    bars = set_at(long_bars(), [-3, -2, -1], "ema_9", 5033.0)  # lows 5+ points (> 0.5 ATR) below
    _, d, _ = decide(bars, cfg=cfg)
    assert reason(d) == Reason.PULLBACK_TOO_DEEP


def test_pullback_breaking_structure():
    cfg = replace(CFG, pullback=replace(CFG.pullback, max_penetration_atr=5.0))
    bars = set_at(long_bars(), PREV, "low", 5008.0)  # below the last confirmed swing low 5009
    _, d, _ = decide(bars, cfg=cfg)
    assert reason(d) == Reason.PULLBACK_BROKE_STRUCTURE


def test_pullback_without_confirmation_never_enters():
    bars = set_at(long_bars(), LAST, "open", 5031.0)  # close 5030.5 < open: bearish candle
    reqs, d, _ = decide(bars)
    assert reqs == [] and reason(d) == Reason.NO_DIRECTIONAL_CLOSE
    assert d.loc["long", "pullback_status"] == "touched:ema_fast"  # pullback alone is not enough


def test_optional_close_beyond_previous_high():
    cfg = replace(CFG, confirmation=replace(CFG.confirmation, require_close_beyond_prev_extreme=True))
    _, d, _ = decide(long_bars(), cfg=cfg)  # close 5030.5 vs previous high 5029: passes
    assert d.loc["long", "decision"] == "signal"
    _, d, _ = decide(set_at(long_bars(), PREV, "high", 5031.0), cfg=cfg)
    assert Reason.NO_CLOSE_BEYOND_PREV_EXTREME in reasons(d)


def test_extension_filters():
    _, d, _ = decide(set_at(long_bars(), LAST, "vwap", 5016.0))  # (5030.5 - 5016) / 8 = 1.81 > 1.5
    assert reason(d) == Reason.EXTENDED_FROM_VWAP
    cfg = replace(CFG, extension=replace(CFG.extension, max_ema_distance_atr=0.25))  # 2.5 / 8 = 0.31
    _, d, _ = decide(long_bars(), cfg=cfg)
    assert reason(d) == Reason.EXTENDED_FROM_EMA


# ----------------------------------------------------------------- 9-10 RSI and RVOL

@pytest.mark.parametrize("window,code", [
    ([58.0, 55.0, 56.0], Reason.RSI_NO_PULLBACK),     # never dipped to <= 50
    ([58.0, 48.0, 49.0], Reason.RSI_NOT_RECOVERED),   # still below 50
    ([49.0, 56.0, 55.0], Reason.RSI_NOT_TURNING),     # falling on the confirmation bar
    ([58.0, 48.0, 80.0], Reason.RSI_OVEREXTENDED),    # above long_entry_max 75
])
def test_long_rsi_failures(window, code):
    _, d, _ = decide(set_at(long_bars(), [-3, -2, -1], "rsi_14", window))
    assert reasons(d) == [code]


@pytest.mark.parametrize("window,code", [
    ([42.0, 45.0, 44.0], Reason.RSI_NO_PULLBACK),
    ([42.0, 52.0, 51.0], Reason.RSI_NOT_RECOVERED),
    ([51.0, 44.0, 45.0], Reason.RSI_NOT_TURNING),
    ([42.0, 52.0, 20.0], Reason.RSI_OVEREXTENDED),
])
def test_short_rsi_failures(window, code):
    bars = set_at(short_bars(), [-3, -2, -1], "rsi_14", window)
    _, d, _ = decide(bars, short_context())
    assert reasons(d, "short") == [code]


def test_rsi_filter_can_be_disabled():
    cfg = replace(CFG, rsi=replace(CFG.rsi, enabled=False))
    _, d, _ = decide(set_at(long_bars(), [-3, -2, -1], "rsi_14", [58.0, 55.0, 56.0]), cfg=cfg)
    assert d.loc["long", "decision"] == "signal" and d.loc["long", "rsi_status"] == "disabled"


def test_rvol_filters():
    _, d, _ = decide(set_at(long_bars(), LAST, "rvol", 0.8))
    assert reasons(d) == [Reason.RVOL_TOO_LOW]
    _, d, _ = decide(set_at(long_bars(), LAST, "rvol", np.nan))
    assert reasons(d) == [Reason.RVOL_UNAVAILABLE]  # insufficient history never passes
    off = replace(CFG, rvol=replace(CFG.rvol, enabled=False))
    _, d, _ = decide(set_at(long_bars(), LAST, "rvol", np.nan), cfg=off)
    assert d.loc["long", "decision"] == "signal" and d.loc["long", "rvol_status"] == "disabled"


# ----------------------------------------------------------------- 11-12 missing / incomplete data

@pytest.mark.parametrize("pos,col", [(PREV, "rsi_14"), (LAST, "atr_14"), (PREV2, "ema_9"), (LAST, "vwap")])
def test_missing_indicator_values_reject(pos, col):
    reqs, d, _ = decide(set_at(long_bars(), pos, col, np.nan))
    assert reqs == [] and reason(d) == Reason.MISSING_INDICATOR


def test_insufficient_history_rejects():
    reqs, d, _ = decide(long_bars().iloc[-1:])  # 10:50 bar alone: no previous bar for RSI/candle checks
    assert reqs == [] and reason(d) == Reason.MISSING_INDICATOR


def test_incomplete_signal_bar_is_skipped():
    reqs, d, _ = decide(set_at(long_bars(), LAST, "is_complete", False))
    assert reqs == [] and reason(d) == Reason.INCOMPLETE_SIGNAL_BAR and d.loc["long", "decision"] == "skip"


def test_incomplete_or_gapped_window_rejects():
    reqs, d, _ = decide(set_at(long_bars(), PREV, "is_complete", False))
    assert reqs == [] and reason(d) == Reason.INCOMPLETE_WINDOW
    gapped = long_bars().drop(long_bars().index[-2])  # missing 5m bar inside the window
    reqs, d, _ = decide(gapped)
    assert reqs == [] and reason(d) == Reason.INCOMPLETE_WINDOW


def test_gates_position_and_pending_entry():
    _, d, _ = decide(long_bars(), position=LONG_POSITION)
    assert reason(d) == Reason.POSITION_OPEN and d.loc["long", "decision"] == "skip"
    _, d, _ = decide(long_bars(), pending=True)
    assert reason(d) == Reason.ENTRY_PENDING


# ----------------------------------------------------------------- 13-14 higher timeframe (strategy side)

def test_context_rules():
    ctx = context_bars()
    # now = 10:55: the last COMPLETED 15m candle is 10:30 (complete at 10:45) = position 6
    _, d, _ = decide(long_bars(), set_at(ctx, 6, "vwap", 6000.0))
    assert reason(d) == Reason.CONTEXT_VWAP
    _, d, _ = decide(long_bars(), set_at(ctx, 6, "ema_9", 4000.0))
    assert reason(d) == Reason.CONTEXT_EMA
    _, d, _ = decide(long_bars(), set_at(ctx, 6, "is_complete", False))
    assert reason(d) == Reason.CONTEXT_INCOMPLETE
    _, d, _ = decide(long_bars(), set_at(ctx, 6, "ema_21", np.nan))
    assert reason(d) == Reason.CONTEXT_MISSING_INDICATOR
    _, d, _ = decide(long_bars(), set_at(ctx, 5, "vwap", 6000.0))  # an older candle does not matter
    assert d.loc["long", "decision"] == "signal"


def test_context_last_visible_bar_is_the_completed_one():
    # now = 10:55: the 10:30 15m candle (complete at 10:45) is the last visible one; the
    # 10:45 candle completes at 11:00 and must not be used even if it is bearish.
    ctx = set_at(context_bars(), 7, "vwap", 9999.0)
    reqs, d, _ = decide(long_bars(), ctx)
    assert d.loc["long", "decision"] == "signal" and d.loc["long", "context_status"] == "ok"
    ctx_view = ctx_for(long_bars(), ctx).context["15min"]
    assert ctx_view.index[-1] == pd.Timestamp("2025-03-10 14:30", tz="UTC")


def test_context_stale_or_unavailable():
    ctx = context_bars()
    _, d, _ = decide(long_bars(), ctx.drop(ctx.index[6]))  # 10:30 candle missing -> last visible is 10:15
    assert reason(d) == Reason.CONTEXT_STALE
    _, d, _ = decide(long_bars(), ctx.iloc[:0])
    assert reason(d) == Reason.CONTEXT_UNAVAILABLE
    no_ctx = replace(CFG, context_timeframe=None)
    _, d, _ = decide(long_bars(), None, cfg=no_ctx)
    assert d.loc["long", "decision"] == "signal" and d.loc["long", "context_status"] == "disabled"
    with pytest.raises(ValueError, match="not supplied"):
        decide(long_bars(), None)


# ----------------------------------------------------------------- 16 confirmed vs unconfirmed pivots

def test_structure_uses_confirmed_pivots_only():
    # Up to bar 19 the high of bar 18 has only one confirmation bar: the last
    # confirmed swing high is still H2 (bar 11, 09:55 New York = 13:55 UTC).
    bars = long_bars()
    _, d, _ = decide(bars.iloc[:20])
    assert d.loc["long", "pullback_id"] == "2025-03-10 13:55:00+00:00"
    _, d, _ = decide(bars)  # bar 18 confirmed on bar 20
    assert d.loc["long", "pullback_id"] == "2025-03-10 14:30:00+00:00"


# ----------------------------------------------------------------- 17-20 stops, targets, rounding

def test_stop_unavailable_and_wrong_side():
    s = VwapTrendPullbackStrategy(CFG, NY)
    s.on_start(MES)
    ctx = ctx_for(long_bars(), context_bars())
    info = {}
    no_pivot = pd.Series({"last_low": np.nan, "last_high": np.nan})
    assert s._stop_and_target(ctx, Side.LONG, 5030.5, 8.0, no_pivot, info) == [Reason.STOP_UNAVAILABLE]
    above = pd.Series({"last_low": 5035.0, "last_high": 5040.0})
    assert s._stop_and_target(ctx, Side.LONG, 5030.5, 8.0, above, info) == [Reason.STOP_WRONG_SIDE]
    below = pd.Series({"last_low": 5020.0, "last_high": 5025.0})
    assert s._stop_and_target(ctx, Side.SHORT, 5030.5, 8.0, below, info) == [Reason.STOP_WRONG_SIDE]


def test_stop_distance_limits():
    _, d, _ = decide(set_at(long_bars(), list(range(23)), "atr_14", 6.0))  # 22 > 3 x 6 = 18
    assert Reason.STOP_TOO_WIDE in reasons(d)
    tight = replace(CFG, stop=replace(CFG.stop, min_distance_atr=2.9))  # 22 < 2.9 x 8 = 23.2
    _, d, _ = decide(long_bars(), cfg=tight)
    assert reasons(d) == [Reason.STOP_TOO_TIGHT]
    ticks = replace(CFG, stop=replace(CFG.stop, max_distance_ticks=80))  # 22 pts = 88 ticks
    _, d, _ = decide(long_bars(), cfg=ticks)
    assert reasons(d) == [Reason.STOP_TOO_WIDE]


@pytest.mark.parametrize("contract", [MES, MNQ])
def test_atr_stop_and_target_tick_rounding_hand_calculated(contract):
    cfg = replace(CFG, stop=replace(CFG.stop, method="atr", atr_multiplier=1.5),
                  target=replace(CFG.target, r_multiple=1.7))
    bars = set_at(long_bars(), list(range(23)), "atr_14", 7.3)
    reqs, d, _ = decide(bars, cfg=cfg, contract=contract)
    r = reqs[0]
    # raw stop 5030.5 - 1.5 x 7.3 = 5019.55 -> sell stop rounded DOWN to 5019.5 (distance 11)
    # raw target 5030.5 + 1.7 x 11 = 5049.2 -> sell limit rounded UP to 5049.25
    assert (r.stop_loss, r.take_profit) == (5019.5, 5049.25)
    for p in (r.stop_loss, r.take_profit):
        assert (p / contract.tick_size) % 1 == 0
    s_reqs, _, _ = decide(mirror(bars), short_context(), cfg=cfg, contract=contract)
    # short: buy stop rounded UP, buy limit rounded DOWN
    assert (s_reqs[0].stop_loss, s_reqs[0].take_profit) == (10060 - 5019.5, 10060 - 5049.25)


@pytest.mark.parametrize("r", [0.5, 1.0, 1.5, 2.0, 3.0])
def test_take_profit_always_beyond_entry(r):
    cfg = replace(CFG, target=replace(CFG.target, r_multiple=r))
    long_req = decide(long_bars(), cfg=cfg)[0][0]
    short_req = decide(short_bars(), short_context(), cfg=cfg)[0][0]
    assert long_req.stop_loss < 5030.5 < long_req.take_profit
    assert short_req.take_profit < 10060 - 5030.5 < short_req.stop_loss
    assert long_req.take_profit == 5030.5 + r * 22  # all on the 0.25 grid here


# ----------------------------------------------------------------- 21-23 trade-management filters

def _fill(role, t):
    return Fill(1, role, True, 1, 5030.0, t, True, 1.0, 0.0, True, 5030.0)


def test_cooldown_after_exit():
    s = VwapTrendPullbackStrategy(CFG, NY)
    s.on_start(MES)
    s.on_fill(_fill(OrderRole.TAKE_PROFIT, pd.Timestamp("2025-03-10 14:45", tz="UTC")))  # 10:45 NY
    _, d, _ = decide(long_bars(), strategy=s)  # now 10:55 < 10:45 + 15 min
    assert reasons(d) == [Reason.COOLDOWN]
    s2 = VwapTrendPullbackStrategy(CFG, NY)
    s2.on_start(MES)
    s2.on_fill(_fill(OrderRole.STOP_LOSS, pd.Timestamp("2025-03-10 14:40", tz="UTC")))  # exactly 15 min
    assert decide(long_bars(), strategy=s2)[1].loc["long", "decision"] == "signal"


def test_max_entries_per_session():
    cfg = replace(CFG, filters=replace(CFG.filters, max_entries_per_session=1, one_entry_per_pullback=False))
    s = VwapTrendPullbackStrategy(cfg, NY)
    s.on_start(MES)
    assert decide(long_bars(), strategy=s)[0]  # first signal
    s.on_fill(_fill(OrderRole.ENTRY, pd.Timestamp("2025-03-10 14:55", tz="UTC")))
    reqs, d, _ = decide(long_bars(), strategy=s)
    assert reqs == [] and d.iloc[-2:]["rejection_reason"].tolist()[0] == Reason.MAX_ENTRIES_PER_SESSION


def test_same_pullback_not_entered_twice():
    s = VwapTrendPullbackStrategy(CFG, NY)
    s.on_start(MES)
    assert decide(long_bars(), strategy=s)[0]
    reqs, d, _ = decide(long_bars(), strategy=s)  # same swing high anchor
    assert reqs == []
    last_long = d.loc["long"].iloc[-1]
    assert last_long.rejection_reason == Reason.SAME_PULLBACK
    off = replace(CFG, filters=replace(CFG.filters, one_entry_per_pullback=False))
    s2 = VwapTrendPullbackStrategy(off, NY)
    s2.on_start(MES)
    assert decide(long_bars(), strategy=s2)[0] and decide(long_bars(), strategy=s2)[0]


def test_strategy_session_window():
    _, d, _ = decide(long_bars("08:00"), context_bars("08:00"))  # now 09:55 > 09:45: inside
    assert d.loc["long", "decision"] == "signal"
    _, d, _ = decide(long_bars("07:30"), context_bars("07:30"))  # now 09:25: before 09:45
    assert reason(d) == Reason.OUTSIDE_STRATEGY_WINDOW
    anytime = replace(CFG, filters=replace(CFG.filters, session_windows=()))
    _, d, _ = decide(long_bars("07:30"), context_bars("07:30"), cfg=anytime)
    assert d.loc["long", "decision"] == "signal"


def test_window_crossing_session_boundary_rejected():
    bars = long_bars("15:05")  # bars 15:05 .. 16:55; shift the last two into the next session
    t = bars.index.tolist()
    t[-2:] = [pd.Timestamp("2025-03-10 22:00", tz="UTC"), pd.Timestamp("2025-03-10 22:05", tz="UTC")]
    bars.index = pd.DatetimeIndex(t, name="timestamp")
    from tests.strategy_fixtures import GLOBEX
    from src.data.sessions import assign_session_dates

    bars["available_at"] = bars.index + pd.Timedelta(minutes=5)
    bars["session_date"] = assign_session_dates(bars.index, GLOBEX)
    anytime = replace(CFG, filters=replace(CFG.filters, session_windows=()), context_timeframe=None)
    reqs, d, _ = decide(bars, None, cfg=anytime)
    assert reqs == [] and reason(d) == Reason.INCOMPLETE_WINDOW


# ----------------------------------------------------------------- diagnostics and configuration

def test_diagnostics_schema_and_codes():
    _, d, s = decide(set_at(long_bars(), LAST, "vwap", 5031.0))
    df = s.diagnostics_frame()
    for col in ("time", "instrument", "direction", "vwap_relation", "ema_relation", "structure",
                "pullback_status", "rsi_status", "rvol_status", "stop_distance", "target_distance",
                "decision", "rejection_reason"):
        assert col in df.columns
    codes = {v for k, v in vars(Reason).items() if not k.startswith("_")}
    for rs in df["reasons"]:
        assert set(filter(None, rs.split(";"))) <= codes
    assert set(df["instrument"]) == {"MES"} and set(df["direction"]) == {"long", "short"}


def test_default_config_values():
    c = load_strategy_config()
    assert (c.signal_timeframe, c.context_timeframe, c.execution_timeframe) == ("5min", "15min", "1min")
    assert (c.ema_fast_col, c.ema_slow_col, c.rsi_col, c.atr_col) == ("ema_9", "ema_21", "rsi_14", "atr_14")
    assert c.stop.method == "swing" and c.target.r_multiple == 2.0
    assert c.filters.max_entries_per_session == 3 and c.filters.cooldown_minutes == 15
    assert c.to_dict()["filters"]["session_windows"] == ["09:45-15:30"]
    assert c.with_r_multiple(3).target.r_multiple == 3.0


@pytest.mark.parametrize("old,new", [
    ('signal_timeframe = "5min"', 'signal_timeframe = "1min"\nexecution_timeframe_x = 1'),
    ("ema_fast = 9", "ema_fast = 30"),
    ('method = "swing"', 'method = "chandelier"'),
    ('levels = ["ema_fast", "ema_slow", "vwap"]', 'levels = ["sma"]'),
    ("long_entry_max = 75.0", "long_entry_max = 40.0"),
    ("max_distance_atr = 3.0", "max_distance_atr = 0.2"),
    ("r_multiple = 2.0", "r_multiple = 0"),
    ("cooldown_minutes = 15", "cooldown_minutes = -1"),
    ('session_windows = ["09:45-15:30"]', 'session_windows = ["morning"]'),
    ("swing_right_bars = 2", "swing_right_bars = 2\nunknown_key = 1"),
])
def test_invalid_strategy_config(tmp_path, old, new):
    text = DEFAULT_STRATEGY_CONFIG_PATH.read_text(encoding="utf-8")
    assert old in text
    p = tmp_path / "s.toml"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_strategy_config(p)


def test_missing_columns_raise():
    with pytest.raises(ValueError, match="lack columns"):
        decide(long_bars().drop(columns=["rvol"]))
