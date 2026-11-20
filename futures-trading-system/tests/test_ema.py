import numpy as np
import pandas as pd
import pytest

from src.indicators.ema import ema


def test_hand_computed_values():
    out = ema(pd.Series([1.0, 2, 3, 4, 5]), 3)  # alpha = 0.5, seed = SMA(1,2,3) = 2
    np.testing.assert_allclose(out.to_numpy(), [np.nan, np.nan, 2.0, 3.0, 4.0])
    assert out.name == "ema_3"


def test_matches_independent_recursion():
    rng = np.random.default_rng(0)
    x = pd.Series(100 + rng.normal(size=200).cumsum())
    out = ema(x, 9)
    alpha = 2 / 10
    ref = [np.nan] * 8 + [x.iloc[:9].mean()]
    for v in x.iloc[9:]:
        ref.append(alpha * v + (1 - alpha) * ref[-1])
    np.testing.assert_allclose(out.to_numpy(), ref)


def test_constant_series():
    out = ema(pd.Series([5.0] * 30), 21)
    assert out.iloc[:20].isna().all()
    np.testing.assert_allclose(out.iloc[20:], 5.0)


def test_insufficient_warmup_is_all_nan():
    out = ema(pd.Series([1.0, 2.0]), 9)
    assert out.isna().all() and len(out) == 2
    assert ema(pd.Series([], dtype=float), 9).empty


def test_period_one_is_identity():
    x = pd.Series([3.0, 1.0, 4.0])
    np.testing.assert_allclose(ema(x, 1), x)


@pytest.mark.parametrize("period", [0, -1, 2.5, True])
def test_invalid_period(period):
    with pytest.raises(ValueError):
        ema(pd.Series([1.0, 2.0]), period)


def test_nan_input_rejected():
    with pytest.raises(ValueError):
        ema(pd.Series([1.0, np.nan, 3.0]), 2)


def test_index_preserved():
    idx = pd.date_range("2025-01-01", periods=4, freq="1min", tz="UTC")
    assert ema(pd.Series([1.0, 2, 3, 4], index=idx), 2).index.equals(idx)
