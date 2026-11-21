"""Regression tests: is_complete must require the exact expected base-frequency grid.

Bug: is_complete compared only the NUMBER of source rows with the nominal count, so
an off-grid timestamp (09:30:30) could stand in for a missing minute (09:31).
"""

import numpy as np
import pandas as pd
import pytest

from src.data.resample import resample_ohlcv
from src.pipeline import build_market_data
from tests.conftest import write_csv

DAY = "2025-03-10"
UTC_OFFSET = pd.Timedelta(hours=4)  # 2025-03-10 is EDT: New York = UTC - 4h


def bars_at(local_times: list[str]) -> pd.DataFrame:
    """Bars at the given New York wall times (HH:MM or HH:MM:SS) with distinct values per row."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{DAY} {t}") + UTC_OFFSET for t in local_times]).tz_localize("UTC")
    n = len(idx)
    base = 100.0 + np.arange(n)
    return pd.DataFrame(
        {"open": base, "high": base + 1, "low": base - 1, "close": base + 0.5, "volume": np.arange(1.0, n + 1)},
        index=idx,
    )


def minutes(start: str, count: int) -> list[str]:
    t0 = pd.Timestamp(f"{DAY} {start}")
    return [(t0 + pd.Timedelta(minutes=i)).strftime("%H:%M:%S") for i in range(count)]


def utc(local: str) -> pd.Timestamp:
    return (pd.Timestamp(f"{DAY} {local}") + UTC_OFFSET).tz_localize("UTC")


def assert_preserved(src: pd.DataFrame, out: pd.DataFrame) -> None:
    """Every source row is aggregated exactly once; nothing fabricated or dropped."""
    assert out["n_source_bars"].sum() == len(src)
    assert out["volume"].sum() == src["volume"].sum()
    assert out["high"].max() == src["high"].max() and out["low"].min() == src["low"].min()
    assert (out["n_grid_bars"] + out["n_off_grid_bars"] == out["n_source_bars"]).all()


def assert_available_at(out: pd.DataFrame, minutes_: int) -> None:
    assert (out["available_at"] - out.index == pd.Timedelta(minutes=minutes_)).all()


# 1. Missing source bar replaced by an off-grid timestamp (the reported case) -------

def test_missing_minute_replaced_by_off_grid_timestamp():
    src = bars_at(["09:30:00", "09:30:30", "09:32:00", "09:33:00", "09:34:00"])
    out = resample_ohlcv(src, "5min", "1min")
    bar = out.iloc[0]
    assert bar.n_source_bars == 5  # count matches nominal...
    assert bar.n_grid_bars == 4 and bar.n_off_grid_bars == 1
    assert not bar.is_complete  # ...but the grid is not satisfied
    assert_preserved(src, out)  # the off-grid row's data is kept, not deleted
    assert bar.open == src["open"].iloc[0] and bar.close == src["close"].iloc[-1]
    assert_available_at(out, 5)


def test_missing_minute_replaced_by_off_grid_15min():
    times = [t for t in minutes("09:30", 15) if t != "09:37:00"] + ["09:40:30"]
    src = bars_at(sorted(times))
    out = resample_ohlcv(src, "15min", "1min")
    assert out.iloc[0].n_source_bars == 15 and not out.iloc[0].is_complete
    assert_preserved(src, out)
    assert_available_at(out, 15)


# 2. Correct number of bars but incorrect timestamp alignment ------------------------

def test_all_bars_shifted_off_grid():
    src = bars_at(["09:30:30", "09:31:30", "09:32:30", "09:33:30", "09:34:30"])
    out = resample_ohlcv(src, "5min", "1min")
    bar = out.iloc[0]
    assert bar.n_source_bars == 5 and bar.n_grid_bars == 0 and bar.n_off_grid_bars == 5
    assert not bar.is_complete
    assert_preserved(src, out)


def test_full_grid_plus_extra_off_grid_row_is_not_complete():
    src = bars_at(sorted(minutes("09:30", 5) + ["09:32:15"]))
    out = resample_ohlcv(src, "5min", "1min")
    bar = out.iloc[0]
    assert bar.n_grid_bars == 5 and bar.n_off_grid_bars == 1 and not bar.is_complete
    assert_preserved(src, out)


def test_off_grid_row_makes_1min_bar_incomplete_but_keeps_its_data():
    src = bars_at(["09:30:00", "09:30:30", "09:31:00"])
    out = resample_ohlcv(src, "1min", "1min")
    assert out["is_complete"].tolist() == [False, True]
    assert out["n_source_bars"].tolist() == [2, 1]
    assert out.iloc[0].high == src["high"].iloc[:2].max()  # off-grid row aggregated, not dropped
    assert_available_at(out, 1)


# 3. Normal complete 5m and 15m bars -------------------------------------------------

@pytest.mark.parametrize("tf,size,n_bins", [("5min", 5, 6), ("15min", 15, 2)])
def test_normal_complete_bars(tf, size, n_bins):
    src = bars_at(minutes("09:30", 30))
    out = resample_ohlcv(src, tf, "1min")
    assert len(out) == n_bins
    assert out["is_complete"].all()
    assert (out["n_grid_bars"] == size).all() and (out["n_off_grid_bars"] == 0).all()
    assert_preserved(src, out)
    assert_available_at(out, size)
    assert out.index[0] == utc("09:30") and out["available_at"].iloc[-1] == utc("10:00")


# 4. Incomplete bars at the beginning and end of a dataset ---------------------------

def test_incomplete_first_and_last_bars_of_dataset():
    src = bars_at(minutes("09:32", 10))  # 09:32 .. 09:41
    five = resample_ohlcv(src, "5min", "1min")
    assert five.index.tolist() == [utc("09:30"), utc("09:35"), utc("09:40")]
    assert five["n_grid_bars"].tolist() == [3, 5, 2]
    assert five["is_complete"].tolist() == [False, True, False]
    assert_preserved(src, five)
    assert_available_at(five, 5)

    fifteen = resample_ohlcv(src, "15min", "1min")
    assert fifteen["n_grid_bars"].tolist() == [10]
    assert fifteen["is_complete"].tolist() == [False]
    assert_available_at(fifteen, 15)


# 5. Multiple missing source bars ----------------------------------------------------

def test_multiple_missing_source_bars():
    missing = {"09:31:00", "09:33:00", "09:36:00", "09:37:00", "09:38:00", "09:39:00", "09:44:00"}
    src = bars_at([t for t in minutes("09:30", 30) if t not in missing])
    five = resample_ohlcv(src, "5min", "1min")
    assert five["n_grid_bars"].tolist() == [3, 1, 4, 5, 5, 5]
    assert five["is_complete"].tolist() == [False, False, False, True, True, True]
    fifteen = resample_ohlcv(src, "15min", "1min")
    assert fifteen["n_grid_bars"].tolist() == [8, 15]
    assert fifteen["is_complete"].tolist() == [False, True]
    for out, m in ((five, 5), (fifteen, 15)):
        assert_preserved(src, out)
        assert_available_at(out, m)


def test_whole_bin_missing_is_not_fabricated():
    src = bars_at(minutes("09:30", 5) + minutes("09:40", 5))  # 09:35 bin absent
    out = resample_ohlcv(src, "5min", "1min")
    assert out.index.tolist() == [utc("09:30"), utc("09:40")]
    assert out["is_complete"].all()


# Pipeline level: an off-grid row passes validation (warning) and the bar is flagged --

def test_pipeline_off_grid_row_flags_bar(tmp_path, config):
    times = ["09:30:00", "09:30:30", "09:32:00", "09:33:00", "09:34:00"] + minutes("09:35", 5)
    rows = "".join(f"{DAY} {t},100,101,99,100,10,MESH25\n" for t in times)
    md = build_market_data(write_csv(tmp_path / "MESH25.csv", "timestamp,open,high,low,close,volume,symbol\n" + rows), config)
    assert "OFF_GRID_TIMESTAMP" in md.report.codes()
    assert len(md.base) == 10  # off-grid row preserved, missing 09:31 not fabricated
    five = md.frames["5min"]
    assert five["is_complete"].tolist() == [False, True]
    assert five["n_source_bars"].tolist() == [5, 5]
    assert_available_at(five, 5)
    one = md.frames["1min"]
    assert not one.loc[utc("09:30"), "is_complete"]  # 09:30:00 + 09:30:30 merged
    issues = [i for i in md.report.issues if i.code == "INCOMPLETE_BARS"]
    assert {i.message.split(":")[0] for i in issues} == {"1min", "5min", "15min"}
