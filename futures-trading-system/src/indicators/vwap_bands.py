"""Session VWAP with volume-weighted deviation bands and a VWAP z-score (Sprint 11).

Definitions (one session; bars ``j = 1..i`` of the session up to and including bar ``i``):

    W_i     = sum_j v_j                                     cumulative volume
    VWAP_i  = sum_j v_j * p_j / W_i                         volume-weighted average price
    Var_i   = sum_j v_j * (p_j - VWAP_i)^2 / W_i            volume-weighted (population) variance about VWAP_i
    sigma_i = sqrt(Var_i)
    band_i(k) = VWAP_i +/- k * sigma_i
    z_i     = (x_i - VWAP_i) / sigma_i                      x = the bar's close (deviation price)

* ``p`` is the bar price used for VWAP (``typical`` = (H + L + C) / 3 like :func:`session_vwap`, or ``close``).
  Bar-based VWAP is an approximation of a tick VWAP: each bar's volume is placed at one price.
* CAUSAL: every value on bar ``i`` uses bars ``<= i`` of the same session only (cumulative sums), so a
  value never changes when later bars arrive. The session is explicit (``SessionConfig``): RTH and ETH are
  never mixed -- bars outside the selected session get NaN and nothing accumulates across sessions.
* Zero volume: a bar with ``v = 0`` contributes nothing (it does not move VWAP or the variance). While a
  session's cumulative volume is 0 every output is NaN. Negative volume or NaN / infinite input is refused.
* sigma = 0 (e.g. the first priced bar of a session, or a perfectly flat session): bands collapse onto VWAP
  and the z-score is NaN -- a deviation is undefined, never infinite. ``min_sigma`` raises this floor.
* Numerical stability: the batch version uses sums of ``p - p0`` (``p0`` = the session's first price), which
  avoids the catastrophic cancellation of ``E[p^2] - E[p]^2`` at index levels around 5,000-20,000; the
  incremental version uses West's (1979) weighted update. Both are tested against each other and against
  direct two-pass computation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.config import SessionConfig
from src.data.sessions import assign_session_dates
from src.indicators._common import as_float_array, check_same_index


def _price(high: np.ndarray, low: np.ndarray, close: np.ndarray, price: str) -> np.ndarray:
    if price == "typical":
        return (high + low + close) / 3.0
    if price == "close":
        return close
    raise ValueError("price must be 'typical' or 'close'")


def vwap_bands(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, session: SessionConfig,
               *, k: tuple[float, ...] = (1.0, 2.0, 3.0), price: str = "typical", min_sigma: float = 0.0,
               prefix: str = "vb") -> pd.DataFrame:
    """Batch computation. Returns columns ``<prefix>_vwap, <prefix>_sigma, <prefix>_z, <prefix>_session``
    and ``<prefix>_upper_<k>`` / ``<prefix>_lower_<k>`` for each k (k formatted with ``%g``)."""
    check_same_index(high, low, close, volume)
    h, lo, c = as_float_array(high, "high"), as_float_array(low, "low"), as_float_array(close, "close")
    v = as_float_array(volume, "volume")
    if np.any(v < 0):
        raise ValueError("volume must be non-negative")
    if not (math.isfinite(min_sigma) and min_sigma >= 0):
        raise ValueError("min_sigma must be a finite number >= 0")
    for kk in k:
        if not (math.isfinite(kk) and kk > 0):
            raise ValueError("band multipliers must be finite and > 0")
    p = _price(h, lo, c, price)
    sessions = assign_session_dates(close.index, session)
    s = sessions.to_numpy()
    n = len(c)
    in_session = ~pd.isna(s)
    # session's first price (shift for numerical stability), positional groups
    frame = pd.DataFrame({"s": s, "p": p, "v": v})
    first_p = frame.groupby("s", sort=False, dropna=True)["p"].transform("first").to_numpy(dtype="float64")
    q = p - np.where(in_session, first_p, 0.0)
    frame["wq"] = v * q
    frame["wq2"] = v * q * q
    g = frame.groupby("s", sort=False, dropna=True)
    cw = g["v"].cumsum().reindex(range(n)).to_numpy(dtype="float64")
    cwq = g["wq"].cumsum().reindex(range(n)).to_numpy(dtype="float64")
    cwq2 = g["wq2"].cumsum().reindex(range(n)).to_numpy(dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_q = np.where(cw > 0, cwq / cw, np.nan)
        var = np.where(cw > 0, cwq2 / cw - mean_q * mean_q, np.nan)
    var = np.where(np.isnan(var), np.nan, np.maximum(var, 0.0))   # rounding can give -1e-17
    vwap = np.where(in_session, first_p + mean_q, np.nan)
    sigma = np.where(in_session, np.sqrt(var), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sigma > max(min_sigma, 0.0), (c - vwap) / sigma, np.nan)
    if min_sigma > 0:
        z = np.where(sigma > min_sigma, z, np.nan)
    out = pd.DataFrame({f"{prefix}_vwap": vwap, f"{prefix}_sigma": sigma, f"{prefix}_z": z}, index=close.index)
    for kk in k:
        out[f"{prefix}_upper_{kk:g}"] = vwap + kk * sigma
        out[f"{prefix}_lower_{kk:g}"] = vwap - kk * sigma
    out[f"{prefix}_session"] = sessions.to_numpy()
    return out


@dataclass
class IncrementalVwapBands:
    """Bar-by-bar computation (West's weighted algorithm). Used to prove the batch values are causal:
    feeding bars one at a time must reproduce the batch output on every bar."""

    session: SessionConfig
    price: str = "typical"
    min_sigma: float = 0.0
    _session_date: object = field(default=None, init=False)
    _w: float = field(default=0.0, init=False)
    _mean: float = field(default=math.nan, init=False)
    _s: float = field(default=0.0, init=False)

    def update(self, ts: pd.Timestamp, high: float, low: float, close: float, volume: float) -> dict[str, float]:
        for name, x in (("high", high), ("low", low), ("close", close), ("volume", volume)):
            if not math.isfinite(x):
                raise ValueError(f"{name} must be finite")
        if volume < 0:
            raise ValueError("volume must be non-negative")
        sd = assign_session_dates(pd.DatetimeIndex([ts]), self.session).iloc[0]
        if pd.isna(sd):
            self._session_date, self._w, self._mean, self._s = None, 0.0, math.nan, 0.0
            return {"vwap": math.nan, "sigma": math.nan, "z": math.nan}
        if sd != self._session_date:  # session reset
            self._session_date, self._w, self._mean, self._s = sd, 0.0, math.nan, 0.0
        p = float(_price(np.array([high]), np.array([low]), np.array([close]), self.price)[0])
        if volume > 0:
            self._w += volume
            if math.isnan(self._mean):
                self._mean = p
            else:
                delta = p - self._mean
                self._mean += (volume / self._w) * delta
                self._s += volume * delta * (p - self._mean)
        if self._w <= 0:
            return {"vwap": math.nan, "sigma": math.nan, "z": math.nan}
        sigma = math.sqrt(max(self._s / self._w, 0.0))
        z = (close - self._mean) / sigma if sigma > self.min_sigma and sigma > 0 else math.nan
        return {"vwap": self._mean, "sigma": sigma, "z": z}
