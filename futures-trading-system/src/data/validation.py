"""OHLCV validation.

Checks are split into:

* **errors** – rows that cannot be used (NaNs, non-positive prices, negative
  volume, broken OHLC relationships, duplicate timestamps). They are flagged in
  the returned ``invalid`` mask.
* **warnings/info** – usable but noteworthy (zero volume, gaps, off-grid
  timestamps, prices not on the tick grid, bars outside the session,
  incomplete sessions).

Nothing is repaired or filled here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import SessionConfig
from src.data.errors import DataError
from src.data.report import DataQualityReport
from src.data.sessions import assign_session_dates

PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
REQUIRED_COLUMNS: tuple[str, ...] = PRICE_COLUMNS + ("volume",)


def _examples(index: pd.Index, mask: np.ndarray) -> list[str]:
    return [str(t) for t in index[mask][:5]]


def find_gaps(
    index: pd.DatetimeIndex,
    session_dates: pd.Series,
    frequency: str,
) -> pd.DataFrame:
    """Find missing bars *inside* sessions.

    A gap is two consecutive bars of the same session further apart than
    ``frequency``. Session breaks (e.g. 17:00-18:00 ET, weekends) are not gaps.
    Missing bars at the very start or end of a session cannot be detected this
    way; see the ``INCOMPLETE_SESSION`` check.

    Returns:
        DataFrame with ``session_date, gap_start, gap_end, missing_bars`` where
        ``gap_start`` is the last bar before the gap and ``gap_end`` the first after.
    """
    freq = pd.Timedelta(pd.tseries.frequencies.to_offset(frequency))
    frame = pd.DataFrame({"ts": index, "session_date": session_dates.to_numpy()})
    frame = frame.dropna(subset=["session_date"]).drop_duplicates("ts")
    frame["prev"] = frame.groupby("session_date")["ts"].shift(1)
    delta = frame["ts"] - frame["prev"]
    is_gap = delta > freq
    gaps = frame.loc[is_gap, ["session_date", "prev", "ts"]].rename(columns={"prev": "gap_start", "ts": "gap_end"})
    gaps["missing_bars"] = (np.round(delta[is_gap] / freq) - 1).astype(int).to_numpy()
    return gaps.reset_index(drop=True)


def validate_ohlcv(
    df: pd.DataFrame,
    *,
    base_frequency: str,
    session: SessionConfig | None = None,
    tick_size: float | None = None,
    report: DataQualityReport | None = None,
) -> tuple[DataQualityReport, np.ndarray]:
    """Validate an OHLCV frame with a UTC DatetimeIndex.

    Args:
        df: Frame with columns ``open, high, low, close, volume``.
        base_frequency: Expected bar spacing (e.g. ``"1min"``).
        session: Session used for gap/outside-session checks (optional).
        tick_size: Contract tick size for the price-grid check (optional).
        report: Report to append to.

    Returns:
        ``(report, invalid)`` where ``invalid`` is a boolean numpy array marking
        rows with errors (positional, same length as ``df``).

    Raises:
        DataError: If required columns are missing or the index is not tz-aware.
    """
    report = report if report is not None else DataQualityReport()
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"Missing required columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise DataError("Index must be a tz-aware DatetimeIndex (UTC)")

    idx = df.index
    n = len(df)
    invalid = np.zeros(n, dtype=bool)
    if n == 0:
        report.add("error", "EMPTY", "No rows to validate")
        return report, invalid

    prices = df[list(PRICE_COLUMNS)]
    volume = df["volume"]

    def flag(mask: np.ndarray, severity: str, code: str, message: str) -> None:
        nonlocal invalid
        if mask.any():
            report.add(severity, code, message, count=int(mask.sum()), examples=_examples(idx, mask))  # type: ignore[arg-type]
            if severity == "error":
                invalid |= mask

    # --- errors -----------------------------------------------------------
    flag(df[list(REQUIRED_COLUMNS)].isna().any(axis=1).to_numpy(), "error", "MISSING_VALUES",
         "Rows with missing OHLCV values (not filled)")
    flag((prices <= 0).any(axis=1).to_numpy(), "error", "NON_POSITIVE_PRICE", "Prices <= 0")
    flag((volume < 0).to_numpy(), "error", "NEGATIVE_VOLUME", "Negative volume")
    hi_bad = df["high"] < prices[["open", "close", "low"]].max(axis=1)
    lo_bad = df["low"] > prices[["open", "close", "high"]].min(axis=1)
    flag((hi_bad | lo_bad).to_numpy(), "error", "OHLC_INCONSISTENT",
         "High below open/close/low or low above open/close/high")
    dup = idx.duplicated(keep="first")
    flag(np.asarray(dup), "error", "DUPLICATE_TIMESTAMP",
         "Duplicate timestamps (first occurrence kept if dropping)")

    # --- warnings / info ---------------------------------------------------
    flag((volume == 0).to_numpy(), "warning", "ZERO_VOLUME",
         "Bars with zero volume (kept; VWAP/RVOL handle them safely)")
    off_grid = np.asarray(idx != idx.floor(base_frequency))
    flag(off_grid, "warning", "OFF_GRID_TIMESTAMP", f"Timestamps not aligned to {base_frequency}")

    if tick_size:
        ratio = prices.to_numpy(dtype=float) / tick_size
        off_tick = ~np.isclose(ratio, np.round(ratio), rtol=0, atol=1e-6)
        off_tick &= ~np.isnan(ratio)
        flag(off_tick.any(axis=1), "warning", "OFF_TICK_PRICE", f"Prices not multiples of tick size {tick_size}")

    if session is not None:
        sess = assign_session_dates(idx, session)
        outside = sess.isna().to_numpy()
        flag(outside, "warning", "OUTSIDE_SESSION", f"Bars outside the '{session.name}' session")

        usable = ~invalid & ~outside
        gaps = find_gaps(idx[usable], sess[usable], base_frequency)
        report.gaps = gaps
        if not gaps.empty:
            report.add(
                "warning",
                "GAPS",
                f"{len(gaps)} gap(s) inside sessions, {int(gaps['missing_bars'].sum())} missing bar(s) "
                "(reported, not filled; see report.gaps)",
                count=len(gaps),
                examples=[f"{r.gap_start} -> {r.gap_end}" for r in gaps.head().itertuples()],
            )

        freq = pd.Timedelta(pd.tseries.frequencies.to_offset(base_frequency))
        expected = int(session.duration / freq)
        counts = sess[usable].value_counts().sort_index()
        short = counts[counts < expected]
        if not short.empty:
            report.add(
                "info",
                "INCOMPLETE_SESSION",
                f"Sessions with fewer than the nominal {expected} bars (partial file edges, "
                "early closes, holidays or missing data)",
                count=len(short),
                examples=[f"{d.date()}: {c} bars" for d, c in short.items()],
            )
    return report, invalid
