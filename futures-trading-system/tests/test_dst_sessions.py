"""Regression tests for DST-aware session boundaries and completeness (Sprint 1 correction pass)."""

from datetime import time

import pandas as pd
import pytest

from src.config import SessionConfig
from src.data.sessions import assign_session_dates, expected_bars_per_session, session_bounds
from src.data.validation import validate_ohlcv
from tests.conftest import full_session_bars

NY = "America/New_York"
# Hypothetical overnight session that CONTAINS the US DST transition (02:00 local).
OVERNIGHT = SessionConfig("overnight", time(20, 0), time(4, 0), NY)
SPRING = "2025-03-09"  # clocks 02:00 -> 03:00, session is 7h instead of 8h
FALL = "2025-11-02"  # clocks 02:00 -> 01:00, session is 9h instead of 8h


def _incomplete(df, session):
    report, invalid = validate_ohlcv(df, base_frequency="1min", session=session)
    assert not invalid.any() and not report.errors
    return report.get("INCOMPLETE_SESSION")


# --- session_bounds -------------------------------------------------------------

def test_bounds_overnight_session_across_dst():
    b = session_bounds(pd.to_datetime([SPRING, FALL, "2025-03-12"]), OVERNIGHT)
    assert b.loc[SPRING, "duration"] == pd.Timedelta(hours=7)
    assert b.loc[FALL, "duration"] == pd.Timedelta(hours=9)
    assert b.loc["2025-03-12", "duration"] == pd.Timedelta(hours=8)
    assert b.loc[SPRING, "start"] == pd.Timestamp("2025-03-09 01:00", tz="UTC")  # 20:00 EST
    assert b.loc[SPRING, "end"] == pd.Timestamp("2025-03-09 08:00", tz="UTC")  # 04:00 EDT


def test_bounds_globex_real_trade_dates_are_23h(globex):
    # US DST switches on Sunday 02:00 while Globex is closed, so real Globex
    # sessions (Mon-Fri trade dates) are always 23h, also in DST weeks.
    b = session_bounds(pd.to_datetime(["2025-03-10", "2025-11-03"]), globex)
    assert (b["duration"] == pd.Timedelta(hours=23)).all()
    assert b.loc["2025-03-10", "start"] == pd.Timestamp("2025-03-09 22:00", tz="UTC")  # Sun 18:00 EDT
    assert b.loc["2025-11-03", "start"] == pd.Timestamp("2025-11-02 23:00", tz="UTC")  # Sun 18:00 EST


def test_bounds_globex_window_containing_transition(globex):
    # A Globex-hours window ending on the transition Sunday really is 22h / 24h long.
    b = session_bounds(pd.to_datetime([SPRING, FALL]), globex)
    assert b["duration"].tolist() == [pd.Timedelta(hours=22), pd.Timedelta(hours=24)]


def test_nonexistent_boundary_shifts_forward():
    s = SessionConfig("odd", time(2, 30), time(9, 0), NY)
    b = session_bounds(pd.to_datetime([SPRING]), s)
    assert b.iloc[0]["start"] == pd.Timestamp("2025-03-09 07:00", tz="UTC")  # 03:00 EDT
    assert b.iloc[0]["duration"] == pd.Timedelta(hours=6)


def test_ambiguous_boundary_uses_first_occurrence():
    s = SessionConfig("odd", time(0, 0), time(1, 30), NY)
    b = session_bounds(pd.to_datetime([FALL]), s)
    assert b.iloc[0]["end"] == pd.Timestamp("2025-11-02 05:30", tz="UTC")  # 01:30 EDT
    # 01:00 EST (second pass through 01:xx, 06:00 UTC) is after the close.
    idx = pd.DatetimeIndex(["2025-11-02 05:29", "2025-11-02 05:30", "2025-11-02 06:00"]).tz_localize("UTC")
    assert assign_session_dates(idx, s).isna().tolist() == [False, True, True]


def test_assignment_matches_bounds_on_dst_nights():
    for day in (SPRING, FALL):
        df = full_session_bars(OVERNIGHT, [day])
        sess = assign_session_dates(df.index, OVERNIGHT)
        assert sess.notna().all() and (sess == pd.Timestamp(day)).all()
        b = session_bounds([pd.Timestamp(day)], OVERNIGHT).iloc[0]
        just_outside = pd.DatetimeIndex([b.start - pd.Timedelta(minutes=1), b.end])
        assert assign_session_dates(just_outside, OVERNIGHT).isna().all()


def test_expected_bars():
    exp = expected_bars_per_session(pd.to_datetime([SPRING, FALL]), OVERNIGHT, "1min")
    assert exp.tolist() == [420, 540]


# --- completeness in validation --------------------------------------------------

def test_spring_forward_full_session_not_flagged():
    df = full_session_bars(OVERNIGHT, [SPRING])
    assert len(df) == 420  # old fixed 8h logic expected 480 and wrongly flagged this
    assert _incomplete(df, OVERNIGHT) is None


def test_fall_back_full_session_not_flagged():
    df = full_session_bars(OVERNIGHT, [FALL])
    assert len(df) == 540
    assert _incomplete(df, OVERNIGHT) is None


def test_fall_back_missing_bars_are_flagged():
    # 40 bars missing at the end of the 25h-day session. The old fixed 8h logic
    # expected only 480 bars and did NOT report this.
    df = full_session_bars(OVERNIGHT, [FALL]).iloc[:-40]
    issue = _incomplete(df, OVERNIGHT)
    assert issue is not None and issue.examples == [f"{FALL}: 500/540 bars"]


def test_spring_forward_missing_bar_is_flagged():
    df = full_session_bars(OVERNIGHT, [SPRING]).iloc[1:]
    issue = _incomplete(df, OVERNIGHT)
    assert issue is not None and issue.examples == [f"{SPRING}: 419/420 bars"]


@pytest.mark.parametrize("dates", [["2025-03-10", "2025-03-11"], ["2025-11-03", "2025-11-04"]])
def test_globex_dst_weeks_full_sessions_not_flagged(globex, dates):
    df = full_session_bars(globex, dates)
    assert len(df) == 2 * 1380
    assert _incomplete(df, globex) is None
    issue = _incomplete(df.iloc[:-1], globex)  # last bar of the second session missing
    assert issue.examples == [f"{dates[1]}: 1379/1380 bars"]


def test_off_grid_bars_do_not_count_towards_completeness(rth):
    df = full_session_bars(rth, ["2025-03-10"]).iloc[:-1]  # 389 of 390
    extra = df.iloc[[0]].copy()
    extra.index = extra.index + pd.Timedelta(seconds=30)
    df = pd.concat([df, extra]).sort_index()  # 390 rows but one is off-grid
    report, _ = validate_ohlcv(df, base_frequency="1min", session=rth)
    assert report.get("INCOMPLETE_SESSION").examples == ["2025-03-10: 389/390 bars"]
    assert "OFF_GRID_TIMESTAMP" in report.codes()
