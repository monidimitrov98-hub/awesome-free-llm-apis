import numpy as np
import pandas as pd
import pytest

from src.indicators.rsi import rsi


def test_hand_computed_wilder():
    # diffs: +1, -1, +2, +1, -1 ; period 3
    out = rsi(pd.Series([10.0, 11, 10, 12, 13, 12]), 3)
    assert out.iloc[:3].isna().all()
    assert out.iloc[3] == pytest.approx(75.0)  # ag=1, al=1/3
    assert out.iloc[4] == pytest.approx(100 - 100 / 5.5)  # ag=1, al=2/9
    assert out.iloc[5] == pytest.approx(100 * 18 / 31)  # ag=2/3, al=13/27


def test_matches_independent_implementation():
    rng = np.random.default_rng(1)
    x = 100 + rng.normal(size=300).cumsum()
    n = 14
    d = np.diff(x)
    g, l = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = g[:n].mean(), l[:n].mean()
    ref = [np.nan] * n + [100 - 100 / (1 + ag / al)]
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        ref.append(100 - 100 / (1 + ag / al))
    np.testing.assert_allclose(rsi(pd.Series(x), n).to_numpy(), ref)


def test_bounds_and_edge_cases():
    assert rsi(pd.Series(np.arange(1.0, 30)), 14).dropna().eq(100).all()
    assert rsi(pd.Series(np.arange(30.0, 1, -1)), 14).dropna().eq(0).all()
    assert rsi(pd.Series([5.0] * 30), 14).dropna().eq(50).all()


def test_warmup():
    assert rsi(pd.Series([1.0] * 14), 14).isna().all()  # needs period + 1 prices
    assert rsi(pd.Series([1.0] * 15), 14).notna().sum() == 1


def test_invalid_input():
    with pytest.raises(ValueError):
        rsi(pd.Series([1.0, np.nan, 2.0]), 2)
    with pytest.raises(ValueError):
        rsi(pd.Series([1.0, 2.0]), 0)
