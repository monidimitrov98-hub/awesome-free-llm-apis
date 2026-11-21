"""OHLCV resampling.

Bars are **left-labelled**: a 5-minute bar stamped 09:30 contains source bars
09:30 to 09:34 and is only fully known at 09:35. Each output bar carries that
instant in ``available_at`` (= ``timestamp + timeframe``).

Empty bins are dropped, never filled, and no valid bar is removed. Each output
bar records how many source bars it contains (``n_source_bars``) and whether that
equals the nominal count (``is_complete``).

RULE FOR DOWNSTREAM CODE (strategy, backtest, dashboard):
    Never generate a signal from a bar with ``is_complete == False``, and never act
    on a bar before ``available_at``. An incomplete bar is missing source data (a
    gap, a dropped invalid row, or the edge of the file) and its high/low/close and
    volume understate the real bar. Indicators are still computed through such bars
    (they are real observations), so a signal on a later complete bar may be
    influenced by an earlier incomplete one; ``report.gaps`` shows where.
"""

from __future__ import annotations

import pandas as pd

from src.config import SessionConfig
from src.data.errors import DataError

AGGREGATION: dict[str, str] = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _to_timedelta(freq: str) -> pd.Timedelta:
    return pd.Timedelta(pd.tseries.frequencies.to_offset(freq))


def check_session_alignment(session: SessionConfig, timeframe: str) -> None:
    """Ensure bins of ``timeframe`` never straddle the session open or close.

    Raises:
        DataError: If session start/end are not multiples of the timeframe.
    """
    step = int(_to_timedelta(timeframe).total_seconds())
    if 86400 % step != 0:
        raise DataError(f"Timeframe {timeframe} must divide 24h evenly")
    for label, sec in (("start", session.start_seconds), ("end", session.end_seconds)):
        if sec % step != 0:
            raise DataError(
                f"Session '{session.name}' {label} is not aligned to {timeframe}; bars would straddle it"
            )


def check_bin_alignment(index: pd.DatetimeIndex, session: SessionConfig, timeframe: str) -> None:
    """Check, for the sessions actually present in ``index``, that every session
    start and end instant (DST-aware, in UTC) falls on a ``timeframe`` bin edge.

    Bins are aligned to UTC midnight, so this catches timezones whose UTC offset
    is not a multiple of the timeframe (e.g. +05:30 with 1h bars), which the purely
    local check in :func:`check_session_alignment` cannot see.

    Raises:
        DataError: If a session boundary falls inside a bin.
    """
    from src.data.sessions import assign_session_dates, session_bounds

    if len(index) == 0:
        return
    bounds = session_bounds(assign_session_dates(index, session), session)
    for col in ("start", "end"):
        edges = bounds[col]
        misaligned = edges[edges != edges.dt.floor(timeframe)]
        if not misaligned.empty:
            raise DataError(
                f"Session '{session.name}' {col} {misaligned.iloc[0]} (UTC) is not on a {timeframe} bin "
                "edge; resampled bars would straddle the session boundary"
            )


def resample_ohlcv(df: pd.DataFrame, timeframe: str, base_frequency: str) -> pd.DataFrame:
    """Aggregate OHLCV bars to ``timeframe``.

    Bins are aligned to UTC midnight. For whole-hour-offset timezones such as
    America/New_York this equals local-clock alignment for timeframes up to 1h.

    Args:
        df: Validated frame with UTC DatetimeIndex and OHLCV columns.
        timeframe: Target timeframe, e.g. ``"5min"``.
        base_frequency: Frequency of the input bars.

    Returns:
        Frame with ``open, high, low, close, volume, n_source_bars, is_complete,
        available_at``.

    Raises:
        DataError: If the timeframe is not a multiple of the base frequency or
            the index is invalid.
    """
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise DataError("Index must be a tz-aware DatetimeIndex")
    if df.index.has_duplicates:
        raise DataError("Cannot resample with duplicate timestamps; validate first")
    tf, base = _to_timedelta(timeframe), _to_timedelta(base_frequency)
    if tf < base or tf % base != pd.Timedelta(0):
        raise DataError(f"Timeframe {timeframe} must be a multiple of base frequency {base_frequency}")
    expected = int(tf / base)

    src = df[list(AGGREGATION)].astype("float64")
    bins = src.resample(timeframe, label="left", closed="left", origin="start_day")
    out = bins.agg(AGGREGATION)
    out["n_source_bars"] = bins["close"].count()
    out = out[out["n_source_bars"] > 0].copy()
    out["n_source_bars"] = out["n_source_bars"].astype(int)
    out["is_complete"] = out["n_source_bars"] == expected
    out["available_at"] = out.index + tf
    out.index.name = "timestamp"
    return out
