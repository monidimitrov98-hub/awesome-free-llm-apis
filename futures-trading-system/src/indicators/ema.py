"""Exponential moving average."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators._common import as_float_array, check_period


def ema(values: pd.Series, period: int) -> pd.Series:
    """Exponential moving average with ``alpha = 2 / (period + 1)``.

    The EMA is seeded with the simple average of the first ``period`` values,
    placed at position ``period - 1``. Earlier positions are NaN (warm-up).
    Each output uses only the current and previous inputs (no look-ahead).

    Args:
        values: Price series without NaN.
        period: Look-back length (>= 1).

    Returns:
        Series named ``ema_<period>`` aligned with ``values``.
    """
    period = check_period(period)
    x = as_float_array(values, "values")
    out = np.full(x.shape[0], np.nan)
    if x.shape[0] >= period:
        alpha = 2.0 / (period + 1.0)
        prev = x[:period].mean()
        out[period - 1] = prev
        for i in range(period, x.shape[0]):
            prev = alpha * x[i] + (1.0 - alpha) * prev
            out[i] = prev
    return pd.Series(out, index=values.index, name=f"ema_{period}")
