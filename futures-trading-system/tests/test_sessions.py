import pandas as pd
import pytest

from src.data.sessions import assign_session_dates, time_of_day_seconds


def _utc(*local: str, tz="America/New_York") -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(list(local))).tz_localize(tz).tz_convert("UTC")


def test_globex_trade_date(globex):
    idx = _utc(
        "2025-03-09 18:00",  # Sunday open -> Monday 03-10 session
        "2025-03-10 09:30",
        "2025-03-10 16:59",
        "2025-03-10 17:00",  # maintenance break -> outside
        "2025-03-10 17:59",
        "2025-03-10 18:00",  # Tuesday session
        "2025-03-10 23:59",
        "2025-03-11 00:00",
    )
    s = assign_session_dates(idx, globex)
    expected = ["2025-03-10"] * 3 + [None, None] + ["2025-03-11"] * 3
    assert [None if pd.isna(v) else str(v.date()) for v in s] == expected


def test_rth_window(rth):
    idx = _utc("2025-03-10 09:29", "2025-03-10 09:30", "2025-03-10 15:59", "2025-03-10 16:00")
    s = assign_session_dates(idx, rth)
    assert s.isna().tolist() == [True, False, False, True]


def test_dst_respected(rth):
    # 09:30 New York is 14:30 UTC in winter and 13:30 UTC in summer
    idx = pd.DatetimeIndex(["2025-01-15 14:30", "2025-07-15 13:30", "2025-07-15 14:30"]).tz_localize("UTC")
    s = assign_session_dates(idx, rth)
    assert s.notna().all()
    assert time_of_day_seconds(idx, "America/New_York").tolist() == [34200, 34200, 37800]


def test_requires_tz_aware(globex):
    with pytest.raises(ValueError):
        assign_session_dates(pd.DatetimeIndex(["2025-01-01"]), globex)
