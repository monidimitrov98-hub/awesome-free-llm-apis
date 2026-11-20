import pandas as pd
import pytest

from src.config import SessionConfig
from src.data.errors import DataError
from src.data.resample import check_session_alignment, resample_ohlcv
from tests.conftest import make_bars


def test_5min_aggregation_values():
    df = make_bars("2025-03-10 13:30", [10, 11, 12, 13, 14, 15, 16, 17, 18, 19], volume=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    out = resample_ohlcv(df, "5min", "1min")
    assert list(out.index) == list(pd.DatetimeIndex(["2025-03-10 13:30", "2025-03-10 13:35"], tz="UTC"))
    first = out.iloc[0]
    assert first.open == df.open.iloc[0]
    assert first.close == 14
    assert first.high == df.high.iloc[:5].max()
    assert first.low == df.low.iloc[:5].min()
    assert first.volume == 15
    assert first.n_source_bars == 5 and first.is_complete


def test_bar_contains_only_its_own_window():
    """Left-labelled bar 13:30 must not include the 13:35 bar (no look-ahead)."""
    df = make_bars("2025-03-10 13:30", [10] * 5 + [999])
    out = resample_ohlcv(df, "5min", "1min")
    assert out.loc["2025-03-10 13:30+00:00", "high"] < 999


def test_missing_minutes_flag_incomplete_and_no_fabricated_bins():
    df = make_bars("2025-03-10 13:30", list(range(1, 31)))
    df = df.drop(df.index[[2]])  # one minute missing in first bin
    df = df.drop(df.index[10:20])  # whole 13:40-13:49 region (incl. 2 bins) missing
    out = resample_ohlcv(df, "5min", "1min")
    assert out.iloc[0].n_source_bars == 4 and not out.iloc[0].is_complete
    assert pd.Timestamp("2025-03-10 13:45", tz="UTC") not in out.index  # empty bin dropped, not filled
    assert not out[["open", "high", "low", "close"]].isna().any().any()


def test_identity_1min():
    df = make_bars("2025-03-10 13:30", [1, 2, 3])
    out = resample_ohlcv(df, "1min", "1min")
    pd.testing.assert_frame_equal(out[["open", "high", "low", "close", "volume"]], df, check_freq=False)
    assert out.is_complete.all()


def test_15min_alignment_to_session_clock():
    df = make_bars("2025-03-10 22:00", [1.0] * 30)  # 18:00 ET onward
    out = resample_ohlcv(df, "15min", "1min")
    assert out.index[0] == pd.Timestamp("2025-03-10 22:00", tz="UTC")
    assert out.n_source_bars.tolist() == [15, 15]


def test_invalid_inputs():
    df = make_bars("2025-03-10 13:30", [1, 2, 3])
    with pytest.raises(DataError):
        resample_ohlcv(df, "90s", "1min")
    with pytest.raises(DataError):
        resample_ohlcv(pd.concat([df, df]), "5min", "1min")


def test_session_alignment_check(globex):
    check_session_alignment(globex, "15min")
    from datetime import time

    odd = SessionConfig("odd", time(9, 35), time(16, 0), "America/New_York")
    with pytest.raises(DataError):
        check_session_alignment(odd, "15min")
