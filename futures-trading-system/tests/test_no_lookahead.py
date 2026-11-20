"""Generic causality tests: changing or removing future bars must not change past values."""

import numpy as np
import pandas as pd
import pytest

from src.indicators import atr, ema, relative_volume_rolling, relative_volume_time_of_day, rsi, session_vwap

N = 3000  # ~2 Globex sessions of 1-minute bars


@pytest.fixture
def bars():
    rng = np.random.default_rng(7)
    idx = pd.date_range("2025-03-10 22:00", periods=N, freq="1min", tz="UTC")
    c = 5000 + rng.normal(size=N).cumsum()
    h = c + rng.uniform(0, 2, N)
    l = c - rng.uniform(0, 2, N)
    v = rng.integers(0, 500, N).astype(float)
    return pd.DataFrame({"high": h, "low": l, "close": c, "volume": v}, index=idx)


def _indicators(df, globex):
    return {
        "ema": ema(df.close, 9),
        "rsi": rsi(df.close, 14),
        "atr": atr(df.high, df.low, df.close, 14),
        "vwap": session_vwap(df.high, df.low, df.close, df.volume, globex),
        "rvol_roll": relative_volume_rolling(df.volume, 20, 5),
        "rvol_tod": relative_volume_time_of_day(df.volume, "America/New_York", 20, 1),
    }


@pytest.mark.parametrize("cut", [50, 1000, 1500, 2999])
def test_truncation_invariance(bars, globex, cut):
    full = _indicators(bars, globex)
    part = _indicators(bars.iloc[:cut], globex)
    for name in full:
        pd.testing.assert_series_equal(full[name].iloc[:cut], part[name], obj=name)


def test_future_perturbation_does_not_change_past(bars, globex):
    base = _indicators(bars, globex)
    shocked = bars.copy()
    shocked.iloc[2000:, :3] *= 1.5
    shocked.iloc[2000:, 3] *= 10
    after = _indicators(shocked, globex)
    for name in base:
        pd.testing.assert_series_equal(base[name].iloc[:2000], after[name].iloc[:2000], obj=name)
