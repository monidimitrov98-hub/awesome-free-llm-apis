"""Shared input checks for indicator functions."""

from __future__ import annotations

import numpy as np
import pandas as pd


def check_period(period: int, name: str = "period") -> int:
    """Validate that ``period`` is a positive integer."""
    if not isinstance(period, (int, np.integer)) or isinstance(period, bool) or period < 1:
        raise ValueError(f"{name} must be a positive integer, got {period!r}")
    return int(period)


def as_float_array(series: pd.Series, name: str) -> np.ndarray:
    """Return ``series`` as float64 numpy array, rejecting NaN/inf.

    Indicators refuse missing input instead of silently propagating or filling it:
    missing data must be dealt with (and reported) upstream.
    """
    if not isinstance(series, pd.Series):
        raise TypeError(f"{name} must be a pandas Series")
    values = series.to_numpy(dtype="float64")
    if not np.all(np.isfinite(values)):
        raise ValueError(f"{name} contains NaN or infinite values; clean/validate the data first")
    return values


def check_same_index(*series: pd.Series) -> None:
    """Ensure all series share the same index."""
    first = series[0].index
    for s in series[1:]:
        if not s.index.equals(first):
            raise ValueError("All input series must share the same index")
