"""Confirmed swing pivots and market structure (non-repainting)."""

import numpy as np
import pandas as pd
import pytest

from src.indicators.swings import confirmed_swings, market_structure

H = pd.Series([1, 2, 5, 3, 2, 4, 7, 4, 3, 5, 6], dtype=float)
L = H - 1


def test_pivot_reported_only_after_confirmation_bars():
    sw = confirmed_swings(H, L, left=2, right=2)
    # Swing high 5 at bar 2 needs bars 3 and 4 to close: reported on row 4, not before.
    assert np.isnan(sw["swing_high"].iloc[:4]).all() and sw["swing_high"].iloc[4] == 5.0
    assert sw["swing_high_time"].iloc[4] == 2
    assert sw["swing_low"].iloc[6] == 1.0 and sw["swing_low_time"].iloc[6] == 4  # low of bar 4 (2 - 1)
    assert sw["swing_high"].iloc[8] == 7.0 and sw["swing_low"].iloc[10] == 2.0


def test_unconfirmed_pivot_not_visible():
    # With only one bar after the 7 at bar 6, it is not a pivot yet.
    assert confirmed_swings(H.iloc[:8], L.iloc[:8], 2, 2)["swing_high"].dropna().tolist() == [5.0]
    assert confirmed_swings(H.iloc[:9], L.iloc[:9], 2, 2)["swing_high"].dropna().tolist() == [5.0, 7.0]


def test_ties_are_not_pivots():
    h = pd.Series([1, 2, 5, 5, 2, 1], dtype=float)
    assert confirmed_swings(h, h - 1, 2, 2)["swing_high"].isna().all()


def test_structure_labels_and_hand_values():
    ms = market_structure(H, L, 2, 2)
    assert ms["structure"].iloc[9] == "insufficient_pivots"  # only one swing low so far
    last = ms.iloc[-1]
    assert (last.last_high, last.prev_high, last.last_low, last.prev_low) == (7, 5, 2, 1)
    assert last.structure == "bullish"
    down = market_structure(-H, -L - 2, 2, 2).iloc[-1]  # mirrored series -> LH + LL
    assert down.structure == "bearish"


def test_non_repainting():
    h = pd.Series(np.random.default_rng(1).normal(size=300).cumsum() + 100)
    l = h - 1
    full = market_structure(h, l, 3, 2)
    for cut in (50, 120, 299):
        part = market_structure(h.iloc[:cut], l.iloc[:cut], 3, 2)
        pd.testing.assert_frame_equal(full.iloc[:cut], part)


def test_invalid_parameters():
    with pytest.raises(ValueError):
        confirmed_swings(H, L, 0, 2)
    with pytest.raises(ValueError):
        confirmed_swings(H, L.iloc[:5], 2, 2)
