"""Relative Strength Index with Wilder's smoothing."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators._common import as_float_array, check_period


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 50.0 if avg_gain == 0.0 else 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI.

    * First average gain/loss = simple mean of the first ``period`` price changes;
      the first RSI value is at position ``period`` (needs ``period + 1`` prices).
    * Then ``avg = (prev_avg * (period - 1) + current) / period``.
    * If the average loss is 0, RSI is 100 (or 50 when the average gain is also 0,
      i.e. a completely flat market).

    Args:
        close: Close prices without NaN.
        period: Look-back length (>= 1).

    Returns:
        Series named ``rsi_<period>`` in [0, 100], NaN during warm-up.
    """
    period = check_period(period)
    x = as_float_array(close, "close")
    n = x.shape[0]
    out = np.full(n, np.nan)
    if n > period:
        diff = np.diff(x)
        gains = np.where(diff > 0, diff, 0.0)
        losses = np.where(diff < 0, -diff, 0.0)
        avg_gain = gains[:period].mean()
        avg_loss = losses[:period].mean()
        out[period] = _rsi_from_averages(avg_gain, avg_loss)
        for i in range(period + 1, n):
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
            out[i] = _rsi_from_averages(avg_gain, avg_loss)
    return pd.Series(out, index=close.index, name=f"rsi_{period}")
