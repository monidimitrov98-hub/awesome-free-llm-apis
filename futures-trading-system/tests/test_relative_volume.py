import numpy as np
import pandas as pd
import pytest

from src.indicators.relative_volume import relative_volume_rolling, relative_volume_time_of_day

NY = "America/New_York"


def test_rolling_excludes_current_bar():
    out = relative_volume_rolling(pd.Series([10.0, 20, 30, 40]), lookback=2)
    np.testing.assert_allclose(out, [np.nan, np.nan, 2.0, 1.6])


def test_rolling_zero_baseline_is_nan():
    out = relative_volume_rolling(pd.Series([0.0, 0, 5]), lookback=2)
    assert out.isna().all()


def test_time_of_day_hand_computed():
    local = []
    vols = []
    data = {1: (100, 10), 2: (200, 20), 3: (300, 30), 4: (0, 60)}
    for day, (v930, v931) in data.items():
        local += [f"2025-03-{9 + day:02d} 09:30", f"2025-03-{9 + day:02d} 09:31"]
        vols += [v930, v931]
    idx = pd.DatetimeIndex(pd.to_datetime(local)).tz_localize(NY).tz_convert("UTC")
    out = relative_volume_time_of_day(pd.Series(vols, index=idx, dtype=float), NY, lookback=2, min_periods=2)
    # 09:30: d3 = 300/150, d4 = 0/250 ; 09:31: d3 = 30/15, d4 = 60/25
    np.testing.assert_allclose(out, [np.nan, np.nan, np.nan, np.nan, 2.0, 2.0, 0.0, 2.4])


def test_time_of_day_uses_local_clock_across_dst():
    # 09:30 NY in winter (14:30 UTC) and after DST (13:30 UTC) are the same slot.
    idx = pd.DatetimeIndex(["2025-03-07 14:30", "2025-03-10 13:30"]).tz_localize("UTC")
    out = relative_volume_time_of_day(pd.Series([100.0, 50.0], index=idx), NY, lookback=1, min_periods=1)
    assert out.iloc[1] == 0.5


def test_validation():
    idx = pd.date_range("2025-01-01", periods=3, freq="1min", tz="UTC")
    with pytest.raises(ValueError):
        relative_volume_time_of_day(pd.Series([1.0, -1, 1], index=idx), NY)
    with pytest.raises(ValueError):
        relative_volume_time_of_day(pd.Series([1.0, 1, 1], index=idx[::-1]), NY, 2, 1)
    with pytest.raises(ValueError):
        relative_volume_rolling(pd.Series([1.0]), lookback=2, min_periods=3)
