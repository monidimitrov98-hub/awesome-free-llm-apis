import numpy as np
import pandas as pd
import pytest

from src.indicators.atr import atr, true_range

H = pd.Series([10.0, 11, 15, 14, 13])
L = pd.Series([8.0, 9, 13, 12, 12])
C = pd.Series([9.0, 10.5, 14, 12.5, 12.5])


def test_true_range_hand_computed():
    np.testing.assert_allclose(true_range(H, L, C), [2.0, 2.0, 4.5, 2.0, 1.0])


def test_atr_wilder_hand_computed():
    out = atr(H, L, C, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(17 / 6)
    assert out.iloc[3] == pytest.approx(23 / 9)
    assert out.iloc[4] == pytest.approx(55 / 27)
    assert out.name == "atr_3"


def test_matches_independent_implementation():
    rng = np.random.default_rng(2)
    c = 100 + rng.normal(size=200).cumsum()
    h = c + rng.uniform(0, 2, 200)
    l = c - rng.uniform(0, 2, 200)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, 200)]
    ref = [np.nan] * 13 + [np.mean(tr[:14])]
    for v in tr[14:]:
        ref.append((ref[-1] * 13 + v) / 14)
    np.testing.assert_allclose(atr(pd.Series(h), pd.Series(l), pd.Series(c), 14), ref)


def test_warmup_and_validation():
    assert atr(H[:2], L[:2], C[:2], 3).isna().all()
    with pytest.raises(ValueError):
        atr(L, H, C, 3)  # high < low
    with pytest.raises(ValueError):
        atr(H, L, C.reset_index(drop=True).set_axis(range(5, 10)), 3)  # misaligned
    with pytest.raises(ValueError):
        atr(H, L, pd.Series([1.0, np.nan, 1, 1, 1]), 3)
