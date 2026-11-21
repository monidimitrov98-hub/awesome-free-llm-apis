import pandas as pd
import pytest

from src.data.errors import TimestampError
from src.data.timestamps import normalize_to_utc

NY = "America/New_York"


def test_naive_local_converted_with_dst():
    s = pd.Series(["2025-01-15 09:30:00", "2025-07-15 09:30:00"])
    r = normalize_to_utc(s, NY)
    assert list(r.utc) == [pd.Timestamp("2025-01-15 14:30", tz="UTC"), pd.Timestamp("2025-07-15 13:30", tz="UTC")]
    assert not r.source_was_tz_aware and r.n_unparseable == 0


def test_offset_aware_strings_ignore_source_tz():
    s = pd.Series(["2025-01-15T14:30:00Z", "2025-01-15T09:31:00-05:00"])
    r = normalize_to_utc(s, "Asia/Tokyo")
    assert list(r.utc) == [pd.Timestamp("2025-01-15 14:30", tz="UTC"), pd.Timestamp("2025-01-15 14:31", tz="UTC")]
    assert r.source_was_tz_aware


def test_mixed_offsets_across_dst():
    s = pd.Series(["2025-03-07 09:30:00-05:00", "2025-03-10 09:30:00-04:00"])
    r = normalize_to_utc(s, NY)
    assert list(r.utc) == [pd.Timestamp("2025-03-07 14:30", tz="UTC"), pd.Timestamp("2025-03-10 13:30", tz="UTC")]


def test_unparseable_and_missing_reported_not_guessed():
    s = pd.Series(["2025-01-15 09:30:00", "garbage", None, ""])
    r = normalize_to_utc(s, NY)
    assert r.utc.isna().tolist() == [False, True, True, True]
    assert r.n_unparseable == 1 and r.n_missing == 2


def test_ambiguous_fall_back_raises_by_default():
    s = pd.Series(["2025-11-02 01:30:00"])
    with pytest.raises(TimestampError):
        normalize_to_utc(s, NY)


def test_ambiguous_nat_option():
    r = normalize_to_utc(pd.Series(["2025-11-02 01:30:00", "2025-11-02 03:00:00"]), NY, ambiguous="NaT")
    assert r.utc.isna().tolist() == [True, False]


def test_ambiguous_infer_on_ordered_sequence():
    s = pd.Series(["2025-11-02 00:59:00", "2025-11-02 01:30:00", "2025-11-02 01:30:00", "2025-11-02 02:00:00"])
    r = normalize_to_utc(s, NY, ambiguous="infer")
    assert r.utc.iloc[1] == pd.Timestamp("2025-11-02 05:30", tz="UTC")  # EDT
    assert r.utc.iloc[2] == pd.Timestamp("2025-11-02 06:30", tz="UTC")  # EST


def test_nonexistent_spring_forward():
    s = pd.Series(["2025-03-09 02:30:00"])
    with pytest.raises(TimestampError):
        normalize_to_utc(s, NY)
    assert normalize_to_utc(s, NY, nonexistent="NaT").utc.isna().all()


def test_epoch_seconds():
    r = normalize_to_utc(pd.Series(["1736951400", "x"]), NY, unit="s")
    assert r.utc.iloc[0] == pd.Timestamp("2025-01-15 14:30", tz="UTC")
    assert r.n_unparseable == 1


def test_explicit_format():
    r = normalize_to_utc(pd.Series(["20250115 093000"]), NY, fmt="%Y%m%d %H%M%S")
    assert r.utc.iloc[0] == pd.Timestamp("2025-01-15 14:30", tz="UTC")


def test_mixed_naive_and_offset_rejected():
    with pytest.raises(TimestampError):
        normalize_to_utc(pd.Series(["2025-01-15 09:30:00Z", "2025-01-15 09:31:00"]), NY)


def test_utc_suffix_parsed_and_mixing_with_naive_rejected():
    r = normalize_to_utc(pd.Series(["2025-01-15 14:30:00 UTC", "2025-01-15 14:31:00 UTC"]), NY)
    assert list(r.utc) == [pd.Timestamp("2025-01-15 14:30", tz="UTC"), pd.Timestamp("2025-01-15 14:31", tz="UTC")]
    # Previously the naive row silently became NaT; now the inconsistency is an explicit error.
    with pytest.raises(TimestampError):
        normalize_to_utc(pd.Series(["2025-01-15 14:30:00 UTC", "2025-01-15 09:31:00"]), NY)
    # Lower-case zone names: pandas 2.x parses them, pandas 3.x does not. Either way
    # the result is the correct instant, NaT (counted as unparseable) or a
    # TimestampError -- never a wrong value and never a raw library exception.
    for values in (["2025-01-15 14:30:00 utc"], ["2025-01-15 14:30:00 UTC", "2025-01-15 14:30:00 utc"]):
        try:
            r = normalize_to_utc(pd.Series(values), NY)
        except TimestampError:
            continue
        for ts in r.utc:
            assert pd.isna(ts) or ts == pd.Timestamp("2025-01-15 14:30", tz="UTC")
        assert r.n_unparseable == int(r.utc.isna().sum())
