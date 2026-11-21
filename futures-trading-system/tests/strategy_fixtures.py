"""Deterministic, hand-designed bars for the VWAP Trend Pullback tests.

Valid LONG setup on 5-minute bars, 2025-03-10 (EDT), bars 09:00 ... 10:50 New York
time; the current (confirmation) bar is 10:50, decided at now = 10:55.

Confirmed pivots (left = right = 2):
    H1 5012 (bar 4)  L1 5001 (bar 7)  H2 5018 (bar 11)  L2 5009 (bar 14)  H3 5032 (bar 18, confirmed on bar 20)
    -> last high 5032 > 5018 and last low 5009 > 5001: bullish (HH + HL).
Pullback: bar 21 low 5027.5 vs EMA fast 5028 (0.5 below, within 0.5 x ATR penetration).
Confirmation bar 22: open 5028, close 5030.5 > open, > EMA fast 5028.
Indicators on bar 22: EMA9 5028, EMA21 5022, VWAP 5024, ATR 8, RSI 56 (prev 48), RVOL 1.3.
Hand-calculated bracket (swing stop, 2 ticks buffer, R = 2):
    stop   = 5009 - 2 x 0.25 = 5008.5        distance = 5030.5 - 5008.5 = 22 (<= 3 x ATR = 24)
    target = 5030.5 + 2 x 22  = 5074.5
The SHORT setup is the exact mirror image (price -> C - price, RSI -> 100 - RSI).
"""

from __future__ import annotations

import pandas as pd

from src.backtest.orders import Side
from src.config import ContractSpec
from src.data.sessions import assign_session_dates
from src.strategy.base import PositionSnapshot, StrategyContext
from tests.backtest_helpers import GLOBEX, MES, MNQ, NY, ts  # noqa: F401  (re-exported)

HL = [(5000, 4996), (5003, 4999), (5006, 5002), (5009, 5005), (5012, 5008), (5010, 5006), (5008, 5004),
      (5006, 5001), (5009, 5004), (5012, 5007), (5015, 5010), (5018, 5013), (5016, 5013), (5015, 5011),
      (5014, 5009), (5017, 5012), (5020, 5015), (5024, 5019), (5032, 5026), (5031, 5027), (5030, 5028),
      (5029, 5027.5), (5031, 5027.75)]
N = len(HL)  # 23 bars: 09:00 .. 10:50
NOW = ts("10:55")
STOP, TARGET = 5008.5, 5074.5


def _times(start: str, n: int, minutes: int, day: str = "2025-03-10") -> pd.DatetimeIndex:
    t0 = pd.Timestamp(f"{day} {start}").tz_localize(NY)
    return pd.DatetimeIndex([t0 + pd.Timedelta(minutes=minutes * i) for i in range(n)]).tz_convert("UTC")


def _finish(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    df.index.name = "timestamp"
    df["available_at"] = df.index + pd.Timedelta(minutes=minutes)
    df["session_date"] = assign_session_dates(df.index, GLOBEX)
    df["is_complete"] = True
    df["volume"] = 100.0
    return df


def long_bars(start: str = "09:00") -> pd.DataFrame:
    """5-minute signal bars with hand-set indicator columns (valid LONG on the last bar)."""
    idx = _times(start, N, 5)
    high = [h for h, _ in HL]
    low = [l for _, l in HL]
    close = [(h + l) / 2 for h, l in HL]
    opn = [c - 0.5 for c in close]
    df = pd.DataFrame({"open": opn, "high": high, "low": low, "close": close}, index=idx, dtype=float)
    df.iloc[-1, df.columns.get_loc("open")] = 5028.0
    df.iloc[-1, df.columns.get_loc("close")] = 5030.5
    df["ema_9"] = [c - 2 for c in close]
    df["ema_21"] = [c - 6 for c in close]
    df["vwap"] = [c - 4 for c in close]
    df.loc[df.index[-3:], "ema_9"] = [5026.0, 5028.0, 5028.0]
    df.loc[df.index[-3:], "ema_21"] = [5020.0, 5021.0, 5022.0]
    df.loc[df.index[-3:], "vwap"] = [5022.0, 5023.0, 5024.0]
    df["atr_14"] = 8.0
    df["rsi_14"] = 60.0
    df.loc[df.index[-3:], "rsi_14"] = [58.0, 48.0, 56.0]
    df["rvol"] = 1.3
    return _finish(df, 5)


def context_bars(start: str = "09:00") -> pd.DataFrame:
    """15-minute bars 09:00 .. 10:45 in a bullish state (close > VWAP, EMA9 > EMA21)."""
    idx = _times(start, 8, 15)
    base = [5005.0 + 3 * i for i in range(8)]
    df = pd.DataFrame({"open": base, "high": [b + 3 for b in base], "low": [b - 3 for b in base],
                       "close": [b + 1 for b in base]}, index=idx)
    df["ema_9"] = [b - 1 for b in base]
    df["ema_21"] = [b - 4 for b in base]
    df["vwap"] = [b - 3 for b in base]
    df["atr_14"] = 9.0
    df["rsi_14"] = 60.0
    df["rvol"] = 1.0
    return _finish(df, 15)


def mirror(df: pd.DataFrame, c: float = 10060.0) -> pd.DataFrame:
    """Mirror image of a frame: prices p -> c - p (high/low swap), RSI -> 100 - RSI."""
    out = df.copy()
    out["high"], out["low"] = c - df["low"], c - df["high"]
    out["open"], out["close"] = c - df["open"], c - df["close"]
    for col in ("ema_9", "ema_21", "vwap"):
        out[col] = c - df[col]
    out["rsi_14"] = 100.0 - df["rsi_14"]
    return out


def short_bars(c: float = 10060.0, start: str = "09:00") -> pd.DataFrame:
    return mirror(long_bars(start), c)


def short_context(c: float = 10060.0, start: str = "09:00") -> pd.DataFrame:
    return mirror(context_bars(start), c)


def set_at(df: pd.DataFrame, pos: int | list[int], col: str, value) -> pd.DataFrame:
    """Copy of ``df`` with ``col`` set at integer position(s) ``pos``."""
    out = df.copy()
    out.iloc[pos, out.columns.get_loc(col)] = value
    return out


def ctx_for(history: pd.DataFrame, context: pd.DataFrame | None, contract: ContractSpec = MES,
            position: PositionSnapshot | None = None, pending: bool = False) -> StrategyContext:
    """Strategy context for the last row of ``history`` with the context cut at ``now``."""
    bar = history.iloc[-1]
    now = bar["available_at"]
    ctx_frames = {}
    if context is not None:
        ctx_frames["15min"] = context[context["available_at"] <= now]
    return StrategyContext(
        now=now, bar=bar, history=history, bar_is_complete=bool(bar["is_complete"]),
        session_date=bar["session_date"] if pd.notna(bar["session_date"]) else None,
        position=position or PositionSnapshot(None, 0, 0.0, 0.0), has_pending_entry=pending,
        contract=contract, context=ctx_frames,
    )


LONG_POSITION = PositionSnapshot(Side.LONG, 1, 5000.0, 0.0)
