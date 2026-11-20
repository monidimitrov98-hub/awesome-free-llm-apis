"""Attach all Sprint 1 indicators to an OHLCV frame."""

from __future__ import annotations

import pandas as pd

from src.config import IndicatorConfig, SessionConfig
from src.data.sessions import assign_session_dates
from src.indicators.atr import atr
from src.indicators.ema import ema
from src.indicators.relative_volume import relative_volume_rolling, relative_volume_time_of_day
from src.indicators.rsi import rsi
from src.indicators.vwap import session_vwap


def add_indicators(bars: pd.DataFrame, cfg: IndicatorConfig, session: SessionConfig) -> pd.DataFrame:
    """Return a copy of ``bars`` with indicator columns added.

    Columns: ``session_date, ema_<fast>, ema_<slow>, rsi_<n>, atr_<n>, vwap, rvol``.
    EMA/RSI/ATR run continuously across sessions (no reset); VWAP resets each session.
    """
    out = bars.copy()
    out["session_date"] = assign_session_dates(out.index, session)
    out[f"ema_{cfg.ema_fast}"] = ema(out["close"], cfg.ema_fast)
    out[f"ema_{cfg.ema_slow}"] = ema(out["close"], cfg.ema_slow)
    out[f"rsi_{cfg.rsi_period}"] = rsi(out["close"], cfg.rsi_period)
    out[f"atr_{cfg.atr_period}"] = atr(out["high"], out["low"], out["close"], cfg.atr_period)
    out["vwap"] = session_vwap(out["high"], out["low"], out["close"], out["volume"], session, cfg.vwap_price)
    if cfg.rvol_method == "time_of_day":
        out["rvol"] = relative_volume_time_of_day(
            out["volume"], session.timezone, cfg.rvol_lookback, cfg.rvol_min_periods
        )
    else:
        out["rvol"] = relative_volume_rolling(out["volume"], cfg.rvol_lookback, cfg.rvol_min_periods)
    return out
