"""Session-anchored VWAP."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import SessionConfig
from src.data.sessions import assign_session_dates
from src.indicators._common import as_float_array, check_same_index


def session_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    session: SessionConfig,
    price: str = "typical",
) -> pd.Series:
    """Volume-weighted average price, reset at every session open.

    ``VWAP_t = sum(price_i * volume_i) / sum(volume_i)`` over bars ``i`` of the
    same session up to and including ``t`` (cumulative, so no look-ahead).

    * ``price="typical"`` uses ``(high + low + close) / 3``; ``"close"`` uses close.
    * While the session's cumulative volume is 0 the VWAP is undefined -> NaN.
    * Bars outside the session get NaN.

    This is a bar-based approximation; an exact VWAP needs trade-level data.

    Args:
        high, low, close, volume: Series sharing a tz-aware DatetimeIndex.
        session: Session that defines the reset points.
        price: ``"typical"`` or ``"close"``.

    Returns:
        Series named ``vwap``.
    """
    check_same_index(high, low, close, volume)
    h, l, c = as_float_array(high, "high"), as_float_array(low, "low"), as_float_array(close, "close")
    v = as_float_array(volume, "volume")
    if np.any(v < 0):
        raise ValueError("volume must be non-negative")
    if price == "typical":
        p = (h + l + c) / 3.0
    elif price == "close":
        p = c
    else:
        raise ValueError("price must be 'typical' or 'close'")

    sessions = assign_session_dates(close.index, session)
    n = c.shape[0]
    frame = pd.DataFrame({"pv": p * v, "v": v, "s": sessions.to_numpy()})  # positional index
    grouped = frame.groupby("s", sort=False, dropna=True)
    cum_pv = grouped["pv"].cumsum().reindex(range(n))
    cum_v = grouped["v"].cumsum().reindex(range(n))
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(cum_v.to_numpy() > 0, cum_pv.to_numpy() / cum_v.to_numpy(), np.nan)
    result[sessions.isna().to_numpy()] = np.nan
    return pd.Series(result, index=close.index, name="vwap")
