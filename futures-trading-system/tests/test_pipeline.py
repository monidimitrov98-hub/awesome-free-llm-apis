import dataclasses

import pandas as pd
import pytest

from src.data.errors import ContractError, DataValidationError
from src.pipeline import build_market_data
from tests.conftest import SAMPLES, write_csv

HEADER = "timestamp,open,high,low,close,volume,symbol\n"


@pytest.mark.parametrize("root,point_value", [("MES", 5.0), ("MNQ", 2.0)])
def test_sample_files_end_to_end(config, root, point_value):
    md = build_market_data(SAMPLES / f"SYNTHETIC_{root}_1m.csv", config)
    assert md.is_synthetic and "SYNTHETIC_DATA" in md.report.codes()
    assert md.contract.label == f"{root}H25" and md.contract_source == "symbol_column"
    assert md.contract.spec.point_value == point_value
    assert not md.report.has_errors
    assert set(md.frames) == {"1min", "5min", "15min"}
    assert md.frames["1min"].index.tz is not None and str(md.frames["1min"].index.tz) == "UTC"
    assert len(md.frames["1min"]) == 7 * 1380
    assert len(md.frames["5min"]) == 7 * 276 and md.frames["5min"].is_complete.all()
    for tf, f in md.frames.items():
        assert f.index.is_monotonic_increasing and not f.index.has_duplicates
        assert f["session_date"].notna().all()
        assert f["ema_9"].iloc[8:].notna().all() and f["ema_9"].iloc[:8].isna().all()
        assert f["rsi_14"].dropna().between(0, 100).all()
        assert (f["atr_14"].dropna() > 0).all()
    # The first bar is Sunday 2025-03-09 18:00 EDT = 22:00 UTC (DST began that morning).
    assert md.frames["1min"].index[0] == pd.Timestamp("2025-03-09 22:00", tz="UTC")


def _bad_csv(tmp_path):
    return write_csv(
        tmp_path / "MESM25_test.csv",
        HEADER
        + "2025-03-10 09:30:00,100,101,99,100.5,10,MESM25\n"
        + "2025-03-10 09:31:00,100,90,99,100.5,10,MESM25\n"  # high < low -> error
        + "2025-03-10 09:32:00,100,101,99,,10,MESM25\n"  # missing close -> error
        + "2025-03-10 09:35:00,100,101,99,100.5,0,MESM25\n",  # gap + zero volume
    )


def test_raise_mode_stops_with_report(tmp_path, config):
    with pytest.raises(DataValidationError) as exc:
        build_market_data(_bad_csv(tmp_path), config)
    assert {"OHLC_INCONSISTENT", "MISSING_VALUES"} <= exc.value.report.codes()


def test_drop_mode_removes_and_reports(tmp_path, config):
    cfg = dataclasses.replace(config, data=dataclasses.replace(config.data, on_error="drop"))
    md = build_market_data(_bad_csv(tmp_path), cfg)
    base = md.base
    assert len(base) == 2  # nothing fabricated in place of the dropped rows
    assert md.report.rows_dropped == 2
    assert {"ROWS_DROPPED", "ZERO_VOLUME"} <= md.report.codes()
    # After dropping, 09:30 -> 09:35 is a gap of 4 missing bars in validation of the
    # original data (09:31, 09:32 were invalid; 09:33, 09:34 absent).
    assert md.report.gaps["missing_bars"].sum() == 4
    # VWAP of the zero-volume 09:35 bar equals the session VWAP so far (first bar's typical price).
    vwap = md.frames["1min"]["vwap"]
    assert vwap.iloc[0] == pytest.approx((101 + 99 + 100.5) / 3)
    assert vwap.iloc[1] == pytest.approx(vwap.iloc[0])


def test_multi_contract_requires_filter(tmp_path, config):
    p = write_csv(
        tmp_path / "roll.csv",
        HEADER
        + "2025-03-10 09:30:00,100,101,99,100,10,MESH25\n"
        + "2025-03-10 09:30:00,200,201,199,200,10,MESM25\n",
    )
    with pytest.raises(ContractError):
        build_market_data(p, config)
    cfg = dataclasses.replace(config, data=dataclasses.replace(config.data, symbol_filter="MESM25"))
    md = build_market_data(p, cfg)
    assert md.contract.label == "MESM25" and len(md.base) == 1 and md.base.close.iloc[0] == 200


def test_contract_from_filename_without_symbol_column(tmp_path, config):
    p = write_csv(tmp_path / "MNQ 06-25.Last.csv", "timestamp,open,high,low,close,volume\n2025-03-10 09:30:00,1,1,1,1,1\n")
    md = build_market_data(p, config)
    assert md.contract.label == "MNQM25" and md.contract_source == "filename"
    assert not md.is_synthetic


def test_pipeline_frames_mark_incomplete_bars(tmp_path, config):
    rows = [f"2025-03-10 09:{m:02d}:00,100,101,99,100,10,MESH25\n" for m in range(30, 45) if m != 33]
    md = build_market_data(write_csv(tmp_path / "MESH25_x.csv", HEADER + "".join(rows)), config)
    five = md.frames["5min"]
    assert five["is_complete"].tolist() == [False, True, True]
    assert five["n_source_bars"].tolist() == [4, 5, 5]
    assert (five["available_at"] - five.index == pd.Timedelta(minutes=5)).all()
    assert md.report.get("INCOMPLETE_BARS").count == 1
    assert len(md.base) == 14  # the missing 09:33 bar was not fabricated
