"""Trading session assignment.

Each bar is mapped to the session it belongs to, identified by its *session
date* (the local calendar date on which the session ends, i.e. the CME trade
date for overnight sessions). Bars outside the session get NaT.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import SessionConfig


def _require_utc_index(index: pd.Index) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("A tz-aware DatetimeIndex is required (use UTC)")
    return index


def assign_session_dates(index: pd.DatetimeIndex, session: SessionConfig) -> pd.Series:
    """Return the session date for every timestamp.

    Timestamps are bar *open* times. The session window is ``[start, end)`` in
    the session's local timezone, so DST is handled by the timezone conversion.

    Args:
        index: tz-aware timestamps.
        session: Session definition.

    Returns:
        Series (naive datetime64 normalized to midnight, NaT outside the session)
        indexed like ``index`` and named ``session_date``.
    """
    idx = _require_utc_index(index)
    local = idx.tz_convert(session.timezone)
    tod = (local.hour * 3600 + local.minute * 60 + local.second).to_numpy()
    day = local.tz_localize(None).normalize()
    s, e = session.start_seconds, session.end_seconds

    if session.is_overnight:
        in_session = (tod >= s) | (tod < e)
        after_open = tod >= s
        dates = np.where(after_open, (day + pd.Timedelta(days=1)).to_numpy(), day.to_numpy())
    else:
        in_session = (tod >= s) & (tod < e)
        dates = day.to_numpy()

    result = pd.Series(dates, index=idx, name="session_date").astype("datetime64[ns]")
    result[~in_session] = pd.NaT
    return result


def time_of_day_seconds(index: pd.DatetimeIndex, timezone: str) -> pd.Series:
    """Seconds after local midnight in ``timezone`` for each timestamp."""
    idx = _require_utc_index(index)
    local = idx.tz_convert(timezone)
    tod = local.hour * 3600 + local.minute * 60 + local.second
    return pd.Series(np.asarray(tod), index=idx, name="time_of_day")
