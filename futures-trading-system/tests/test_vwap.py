import numpy as np
import pandas as pd
import pytest

from src.indicators.vwap import session_vwap


def _frame(local_times, close, volume, high=None, low=None, tz="America/New_York"):
    idx = pd.DatetimeIndex(pd.to_datetime(local_times)).tz_localize(tz).tz_convert("UTC")
    c = pd.Series(close, index=idx, dtype=float)
    h = pd.Series(high if high is not None else close, index=idx, dtype=float)
    l = pd.Series(low if low is not None else close, index=idx, dtype=float)
    return h, l, c, pd.Series(volume, index=idx, dtype=float)


def test_cumulative_and_reset_each_session(rth):
    h, l, c, v = _frame(
        ["2025-03-10 09:30", "2025-03-10 09:31", "2025-03-11 09:30", "2025-03-11 09:31"],
        [10, 20, 30, 40],
        [100, 300, 50, 50],
    )
    out = session_vwap(h, l, c, v, rth, price="close")
    np.testing.assert_allclose(out, [10.0, 17.5, 30.0, 35.0])  # resets on 03-11


def test_globex_resets_at_18_00(globex):
    h, l, c, v = _frame(
        ["2025-03-10 16:58", "2025-03-10 16:59", "2025-03-10 18:00", "2025-03-10 18:01"],
        [10, 20, 100, 200],
        [1, 1, 1, 3],
    )
    out = session_vwap(h, l, c, v, globex, price="close")
    np.testing.assert_allclose(out, [10.0, 15.0, 100.0, 175.0])


def test_typical_price():
    from datetime import time

    from src.config import SessionConfig

    s = SessionConfig("rth", time(9, 30), time(16, 0), "America/New_York")
    h, l, c, v = _frame(["2025-03-10 09:30"], [10], [5], high=[13], low=[7])
    assert session_vwap(h, l, c, v, s).iloc[0] == pytest.approx(10.0)  # (13+7+10)/3


def test_zero_volume_is_nan_until_volume_arrives(rth):
    h, l, c, v = _frame(["2025-03-10 09:30", "2025-03-10 09:31", "2025-03-10 09:32"], [10, 20, 30], [0, 0, 10])
    out = session_vwap(h, l, c, v, rth, price="close")
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2] == 30.0


def test_outside_session_is_nan(rth):
    h, l, c, v = _frame(["2025-03-10 09:29", "2025-03-10 09:30", "2025-03-10 16:00"], [1, 2, 3], [5, 5, 5])
    out = session_vwap(h, l, c, v, rth, price="close")
    assert out.isna().tolist() == [True, False, True]
    assert out.iloc[1] == 2.0  # the 09:29 bar did not leak into the session


def test_invalid_inputs(rth):
    h, l, c, v = _frame(["2025-03-10 09:30"], [10], [-1])
    with pytest.raises(ValueError):
        session_vwap(h, l, c, v, rth)
    h, l, c, v = _frame(["2025-03-10 09:30"], [10], [1])
    with pytest.raises(ValueError):
        session_vwap(h, l, c, v, rth, price="median")
