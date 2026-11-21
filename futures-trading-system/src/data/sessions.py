"""Trading session assignment.

Each bar is mapped to the session it belongs to, identified by its *session
date* (the local calendar date on which the session ends, i.e. the CME trade
date for overnight sessions). Bars outside the session get NaT.

Session boundaries are resolved to exact UTC instants per session date with
:func:`session_bounds`, so sessions that contain a DST transition have their
real elapsed length (e.g. 22h or 24h instead of a nominal 23h). Wall-clock
boundary rules:

* A boundary that does not exist locally (spring-forward gap) is moved forward
  to the first valid instant after the gap.
* A boundary that occurs twice locally (fall-back hour) uses its **first**
  occurrence ("the session opens/closes when the clock first shows that time").
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import SessionConfig


def _require_utc_index(index: pd.Index) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("A tz-aware DatetimeIndex is required (use UTC)")
    return index


def _localize_wall_times(wall: pd.DatetimeIndex, timezone: str) -> pd.DatetimeIndex:
    """Localize naive wall-clock times with the boundary rules described above."""
    first_occurrence = np.ones(len(wall), dtype=bool)  # True = DST side = earlier instant
    return wall.tz_localize(timezone, ambiguous=first_occurrence, nonexistent="shift_forward")


def session_bounds(session_dates: pd.Series | pd.Index | list, session: SessionConfig) -> pd.DataFrame:
    """Exact UTC start/end instants for each session date.

    Args:
        session_dates: Session dates (naive, midnight). NaT/duplicates are ignored.
        session: Session definition.

    Returns:
        DataFrame indexed by unique session date (ascending) with tz-aware UTC
        columns ``start`` (inclusive), ``end`` (exclusive) and ``duration``.
    """
    dates = pd.DatetimeIndex(pd.Series(pd.to_datetime(list(session_dates))).dropna().unique()).sort_values()
    start_day = dates - pd.Timedelta(days=1) if session.is_overnight else dates
    start_wall = start_day + pd.Timedelta(seconds=session.start_seconds)
    end_wall = dates + pd.Timedelta(seconds=session.end_seconds)
    start = _localize_wall_times(start_wall, session.timezone).tz_convert("UTC")
    end = _localize_wall_times(end_wall, session.timezone).tz_convert("UTC")
    out = pd.DataFrame({"start": start, "end": end}, index=dates)
    out.index.name = "session_date"
    out["duration"] = out["end"] - out["start"]
    return out


def assign_session_dates(index: pd.DatetimeIndex, session: SessionConfig) -> pd.Series:
    """Return the session date for every timestamp.

    Timestamps are bar *open* times. A bar belongs to session ``d`` when
    ``start(d) <= t < end(d)`` with the exact UTC bounds from :func:`session_bounds`
    (DST-aware). The candidate date is taken from the local wall clock.

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

    if session.is_overnight:
        candidate = np.where(tod >= session.start_seconds, (day + pd.Timedelta(days=1)).to_numpy(), day.to_numpy())
    else:
        candidate = day.to_numpy()
    candidate = pd.DatetimeIndex(candidate).as_unit("ns")

    result = pd.Series(candidate, index=idx, name="session_date").astype("datetime64[ns]")
    if len(idx) == 0:
        return result
    bounds = session_bounds(candidate, session)
    start = bounds["start"].reindex(candidate).to_numpy()
    end = bounds["end"].reindex(candidate).to_numpy()
    ts = idx.tz_convert("UTC")
    in_session = (ts >= start) & (ts < end)
    result[~np.asarray(in_session)] = pd.NaT
    return result


def expected_bars_per_session(session_dates: pd.Series | pd.Index | list, session: SessionConfig, frequency: str) -> pd.Series:
    """Number of bars of ``frequency`` that fit in each session's real (DST-aware) length."""
    freq = pd.Timedelta(pd.tseries.frequencies.to_offset(frequency))
    bounds = session_bounds(session_dates, session)
    return (bounds["duration"] // freq).astype(int).rename("expected_bars")


def time_of_day_seconds(index: pd.DatetimeIndex, timezone: str) -> pd.Series:
    """Seconds after local midnight in ``timezone`` for each timestamp."""
    idx = _require_utc_index(index)
    local = idx.tz_convert(timezone)
    tod = local.hour * 3600 + local.minute * 60 + local.second
    return pd.Series(np.asarray(tod), index=idx, name="time_of_day")
