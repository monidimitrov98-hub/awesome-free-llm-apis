"""Relative volume (RVOL)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.sessions import time_of_day_seconds
from src.indicators._common import as_float_array, check_period


def _ratio(volume: np.ndarray, baseline: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(baseline > 0, volume / baseline, np.nan)


def relative_volume_rolling(volume: pd.Series, lookback: int = 20, min_periods: int | None = None) -> pd.Series:
    """Volume divided by the mean volume of the previous ``lookback`` bars.

    The current bar is excluded from its own baseline (shifted window), so the
    value never uses future or same-bar information in the denominator.
    NaN when fewer than ``min_periods`` prior bars exist or the baseline is 0.
    """
    lookback = check_period(lookback, "lookback")
    min_periods = check_period(min_periods if min_periods is not None else lookback, "min_periods")
    if min_periods > lookback:
        raise ValueError("min_periods cannot exceed lookback")
    v = as_float_array(volume, "volume")
    if np.any(v < 0):
        raise ValueError("volume must be non-negative")
    baseline = pd.Series(v).shift(1).rolling(lookback, min_periods=min_periods).mean().to_numpy()
    return pd.Series(_ratio(v, baseline), index=volume.index, name="rvol")


def relative_volume_time_of_day(
    volume: pd.Series,
    timezone: str,
    lookback: int = 20,
    min_periods: int = 5,
) -> pd.Series:
    """Volume divided by the mean volume at the same local time of day in earlier sessions.

    Intraday volume has a strong U-shape, so comparing a 09:35 bar with previous
    09:35 bars is more meaningful than with the previous few bars. For every bar
    the baseline is the mean of the last ``lookback`` *earlier* bars that share its
    local time of day (the current bar is excluded). NaN when fewer than
    ``min_periods`` such bars exist or the baseline is 0.

    Args:
        volume: Volume series with a tz-aware, ascending DatetimeIndex.
        timezone: Timezone used to define "time of day" (exchange/session tz).
        lookback: Number of previous occurrences averaged.
        min_periods: Minimum previous occurrences required.
    """
    lookback = check_period(lookback, "lookback")
    min_periods = check_period(min_periods, "min_periods")
    if min_periods > lookback:
        raise ValueError("min_periods cannot exceed lookback")
    v = as_float_array(volume, "volume")
    if np.any(v < 0):
        raise ValueError("volume must be non-negative")
    if not volume.index.is_monotonic_increasing:
        raise ValueError("volume index must be sorted ascending")
    tod = time_of_day_seconds(volume.index, timezone).to_numpy()
    frame = pd.DataFrame({"v": v, "tod": tod})
    baseline = (
        frame.groupby("tod", sort=False)["v"]
        .transform(lambda s: s.shift(1).rolling(lookback, min_periods=min_periods).mean())
        .to_numpy()
    )
    return pd.Series(_ratio(v, baseline), index=volume.index, name="rvol")
