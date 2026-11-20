"""True Range and Average True Range with Wilder's smoothing."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators._common import as_float_array, check_period, check_same_index


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range per bar.

    ``TR_t = max(high_t - low_t, |high_t - close_{t-1}|, |low_t - close_{t-1}|)``.
    The first bar has no previous close, so ``TR_0 = high_0 - low_0``.

    Raises:
        ValueError: On NaN input, mismatched indexes or ``high < low``.
    """
    check_same_index(high, low, close)
    h, l, c = as_float_array(high, "high"), as_float_array(low, "low"), as_float_array(close, "close")
    if np.any(h < l):
        raise ValueError("high must be >= low for every bar")
    tr = h - l
    if tr.shape[0] > 1:
        prev_close = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)])
    return pd.Series(tr, index=high.index, name="true_range")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder ATR.

    The first ATR (position ``period - 1``) is the simple mean of the first
    ``period`` true ranges; afterwards ``ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period``.

    Returns:
        Series named ``atr_<period>``, NaN during warm-up.
    """
    period = check_period(period)
    tr = true_range(high, low, close).to_numpy()
    out = np.full(tr.shape[0], np.nan)
    if tr.shape[0] >= period:
        prev = tr[:period].mean()
        out[period - 1] = prev
        for i in range(period, tr.shape[0]):
            prev = (prev * (period - 1) + tr[i]) / period
            out[i] = prev
    return pd.Series(out, index=high.index, name=f"atr_{period}")
