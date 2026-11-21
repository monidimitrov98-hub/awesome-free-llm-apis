"""Confirmed swing pivots and market structure (non-repainting).

A bar ``i`` is a swing high when its high is strictly greater than the highs of the
``left`` bars before it and the ``right`` bars after it (ties are not pivots);
swing lows are mirrored. Because ``right`` later bars are needed, the pivot is only
**confirmed on bar i + right** and is reported on that row, never earlier. A value
on row ``j`` therefore depends only on rows ``<= j`` and never changes when later
bars arrive.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators._common import as_float_array, check_period, check_same_index

STRUCTURE_BULLISH = "bullish"  # higher high and higher low
STRUCTURE_BEARISH = "bearish"  # lower high and lower low
STRUCTURE_MIXED = "mixed"
STRUCTURE_INSUFFICIENT = "insufficient_pivots"


def confirmed_swings(high: pd.Series, low: pd.Series, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Swing pivots reported on the row where they become confirmed.

    Returns:
        DataFrame indexed like the input with ``swing_high``/``swing_low`` (pivot
        price, NaN on other rows) and ``swing_high_time``/``swing_low_time`` (the
        timestamp of the pivot bar itself, NaT on other rows).
    """
    left = check_period(left, "left")
    right = check_period(right, "right")
    check_same_index(high, low)
    h, l = as_float_array(high, "high"), as_float_array(low, "low")
    n = h.shape[0]
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    sh_t = np.full(n, -1, dtype=np.int64)
    sl_t = np.full(n, -1, dtype=np.int64)
    for i in range(left, n - right):
        others_h = np.concatenate([h[i - left:i], h[i + 1:i + right + 1]])
        others_l = np.concatenate([l[i - left:i], l[i + 1:i + right + 1]])
        if h[i] > others_h.max():
            sh[i + right], sh_t[i + right] = h[i], i
        if l[i] < others_l.min():
            sl[i + right], sl_t[i + right] = l[i], i
    idx = high.index
    to_time = lambda pos: pd.Series([idx[p] if p >= 0 else pd.NaT for p in pos], index=idx)  # noqa: E731
    return pd.DataFrame(
        {"swing_high": sh, "swing_high_time": to_time(sh_t), "swing_low": sl, "swing_low_time": to_time(sl_t)},
        index=idx,
    )


def market_structure(high: pd.Series, low: pd.Series, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Last two confirmed swing highs/lows as of every row, and the structure label.

    Columns: ``last_high, prev_high, last_high_time, last_low, prev_low,
    last_low_time, structure`` where structure is ``bullish`` (last high > previous
    high and last low > previous low), ``bearish`` (both lower), ``mixed`` or
    ``insufficient_pivots``.
    """
    sw = confirmed_swings(high, low, left, right)
    out = pd.DataFrame(index=high.index)
    for kind in ("high", "low"):
        price = sw[f"swing_{kind}"]
        times = sw[f"swing_{kind}_time"]
        confirmed = price.notna()
        last = price.where(confirmed).ffill()
        # previous confirmed pivot: shift the sequence of confirmed values by one
        prev_values = price[confirmed].shift(1)
        prev = pd.Series(np.nan, index=high.index)
        prev[confirmed] = prev_values
        out[f"last_{kind}"] = last
        out[f"prev_{kind}"] = prev.where(confirmed).ffill()
        # forward-fill the POSITION of the pivot bar (numeric), then map it to its timestamp
        pos = pd.Series(np.where(confirmed, np.arange(len(times)), np.nan)).ffill().to_numpy()
        source = times.to_numpy()
        out[f"last_{kind}_time"] = pd.Series(
            [source[int(p)] if not np.isnan(p) else pd.NaT for p in pos], index=high.index
        )
    up = (out["last_high"] > out["prev_high"]) & (out["last_low"] > out["prev_low"])
    down = (out["last_high"] < out["prev_high"]) & (out["last_low"] < out["prev_low"])
    known = out[["last_high", "prev_high", "last_low", "prev_low"]].notna().all(axis=1)
    out["structure"] = np.select([~known, up, down], [STRUCTURE_INSUFFICIENT, STRUCTURE_BULLISH, STRUCTURE_BEARISH],
                                 default=STRUCTURE_MIXED)
    return out
