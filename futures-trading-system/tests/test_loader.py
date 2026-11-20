import dataclasses

import pandas as pd
import pytest

from src.data.errors import DataError
from src.data.loader import load_csv
from tests.conftest import write_csv


def test_column_mapping_case_insensitive_and_comments(tmp_path, config):
    cfg = dataclasses.replace(
        config.data,
        columns={"timestamp": "Date Time", "open": "O", "high": "H", "low": "L", "close": "C", "volume": "Vol"},
        symbol_column=None,
    )
    p = write_csv(
        tmp_path / "x.csv",
        "# a comment line\ndate time,o,h,l,c,vol\n2025-01-15 09:30:00,10,11,9,10.5,5\n",
    )
    df, report = load_csv(p, cfg)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index[0] == pd.Timestamp("2025-01-15 14:30", tz="UTC")
    assert df.index.name == "timestamp"
    assert report.rows_read == 1 and not report.issues


def test_missing_column_raises(tmp_path, config):
    p = write_csv(tmp_path / "x.csv", "timestamp,open,high,low,close\n2025-01-15 09:30:00,1,1,1,1\n")
    with pytest.raises(DataError, match="volume"):
        load_csv(p, config.data)


def test_missing_file_raises(tmp_path, config):
    with pytest.raises(DataError):
        load_csv(tmp_path / "missing.csv", config.data)


def test_non_numeric_and_bad_timestamp_reported(tmp_path, config):
    p = write_csv(
        tmp_path / "x.csv",
        "timestamp,open,high,low,close,volume\n"
        "2025-01-15 09:31:00,10,11,9,abc,5\n"
        "not-a-date,10,11,9,10,5\n"
        "2025-01-15 09:30:00,10,11,9,10,5\n",
    )
    df, report = load_csv(p, config.data)
    assert {"NON_NUMERIC_VALUE", "BAD_TIMESTAMP", "UNSORTED"} <= report.codes()
    assert len(df) == 2 and report.rows_dropped == 1
    assert df.index.is_monotonic_increasing
    assert pd.isna(df["close"].iloc[1])  # kept as NaN for validation to flag, not fabricated


def test_close_stamped_bars_shifted_to_open(tmp_path, config):
    cfg = dataclasses.replace(config.data, timestamp_label="close")
    p = write_csv(tmp_path / "x.csv", "timestamp,open,high,low,close,volume\n2025-01-15 09:31:00,1,1,1,1,1\n")
    df, report = load_csv(p, cfg)
    assert df.index[0] == pd.Timestamp("2025-01-15 14:30", tz="UTC")  # 09:30 ET bar open
    assert "CLOSE_STAMPED" in report.codes()


def test_symbol_column_uppercased(tmp_path, config):
    p = write_csv(tmp_path / "x.csv", "timestamp,open,high,low,close,volume,symbol\n2025-01-15 09:30:00,1,1,1,1,1, mesh25\n")
    df, _ = load_csv(p, config.data)
    assert df["symbol"].iloc[0] == "MESH25"
