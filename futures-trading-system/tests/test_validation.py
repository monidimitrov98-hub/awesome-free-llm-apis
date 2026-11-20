import numpy as np
import pandas as pd
import pytest

from src.data.errors import DataError
from src.data.validation import find_gaps, validate_ohlcv
from tests.conftest import make_bars

# 2025-03-10 13:30 UTC = 09:30 New York (EDT), inside Globex and RTH.
START = "2025-03-10 13:30"


def _validate(df, **kw):
    kw.setdefault("base_frequency", "1min")
    return validate_ohlcv(df, **kw)


def test_clean_data_has_no_issues(globex):
    df = make_bars(START, [100, 101, 102, 101, 100])
    report, invalid = _validate(df, session=globex, tick_size=0.25)
    assert not report.errors and not report.warnings and not invalid.any()
    assert report.codes() == {"INCOMPLETE_SESSION"}  # only 5 of 1380 bars: informational


def test_missing_values_flagged_not_filled():
    df = make_bars(START, [100, 101, 102])
    df.iloc[1, df.columns.get_loc("close")] = np.nan
    report, invalid = _validate(df)
    assert report.get("MISSING_VALUES").count == 1
    assert invalid.tolist() == [False, True, False]
    assert np.isnan(df["close"].iloc[1])  # untouched


def test_ohlc_relationships():
    df = make_bars(START, [100, 101, 102, 103])
    df.iloc[1, df.columns.get_loc("high")] = 50  # high below low/open/close
    df.iloc[2, df.columns.get_loc("low")] = 200  # low above high
    report, invalid = _validate(df)
    assert report.get("OHLC_INCONSISTENT").count == 2
    assert invalid.tolist() == [False, True, True, False]


def test_price_and_volume_sign():
    df = make_bars(START, [100, 101, 102])
    df.iloc[0, df.columns.get_loc("low")] = 0
    df.iloc[1, df.columns.get_loc("volume")] = -5
    df.iloc[2, df.columns.get_loc("volume")] = 0
    report, invalid = _validate(df)
    assert {"NON_POSITIVE_PRICE", "NEGATIVE_VOLUME", "ZERO_VOLUME"} <= report.codes()
    assert invalid.tolist() == [True, True, False]  # zero volume is only a warning
    assert report.get("ZERO_VOLUME").severity == "warning"


def test_duplicates():
    df = make_bars(START, [100, 101, 102])
    df = pd.concat([df, df.iloc[[1]]]).sort_index(kind="stable")
    report, invalid = _validate(df)
    assert report.get("DUPLICATE_TIMESTAMP").count == 1
    assert invalid.sum() == 1 and not invalid[1]


def test_off_grid_and_off_tick():
    df = make_bars(START, [100, 101, 102])
    df.index = df.index + pd.to_timedelta([0, 30, 0], unit="s")
    df.iloc[2, df.columns.get_loc("close")] = 101.1
    report, invalid = _validate(df, tick_size=0.25)
    assert report.get("OFF_GRID_TIMESTAMP").count == 1
    assert report.get("OFF_TICK_PRICE").count == 1
    assert not invalid.any()


def test_gaps_reported_and_session_break_ignored(globex):
    # 20:55..20:59 UTC (16:55..16:59 ET), then 22:00 UTC (18:00 ET new session): no gap.
    a = make_bars("2025-03-10 20:55", [100] * 5)
    b = make_bars("2025-03-10 22:00", [100] * 3)
    # Remove 22:01 -> gap of 1 bar inside the new session.
    df = pd.concat([a, b.drop(b.index[1])])
    report, _ = _validate(df, session=globex)
    assert report.get("GAPS").count == 1
    gap = report.gaps.iloc[0]
    assert gap.missing_bars == 1
    assert gap.gap_start == pd.Timestamp("2025-03-10 22:00", tz="UTC")
    assert gap.gap_end == pd.Timestamp("2025-03-10 22:02", tz="UTC")
    assert len(df) == 7  # nothing was inserted


def test_outside_session_and_incomplete(rth):
    df = make_bars("2025-03-10 13:28", [100] * 5)  # 09:28..09:32 ET
    report, invalid = _validate(df, session=rth)
    assert report.get("OUTSIDE_SESSION").count == 2
    assert report.get("INCOMPLETE_SESSION").severity == "info"
    assert not invalid.any()


def test_find_gaps_multi_bar(rth):
    idx = pd.DatetimeIndex(["2025-03-10 13:30", "2025-03-10 13:31", "2025-03-10 13:36"]).tz_localize("UTC")
    from src.data.sessions import assign_session_dates

    gaps = find_gaps(idx, assign_session_dates(idx, rth), "1min")
    assert gaps["missing_bars"].tolist() == [4]


def test_bad_inputs():
    df = make_bars(START, [1, 2])
    with pytest.raises(DataError):
        validate_ohlcv(df.drop(columns="volume"), base_frequency="1min")
    with pytest.raises(DataError):
        validate_ohlcv(df.tz_localize(None), base_frequency="1min")
