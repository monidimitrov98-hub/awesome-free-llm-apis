"""SYNTHETIC regime-switching 1-minute data with known ground truth (tests and method checks).

Blocks of 30-150 minutes are drawn from trend-up, trend-down, range (mean reverting) and
chop (strongly mean reverting, noisy) regimes, each with a volatility level (low, normal,
high, extreme) and an intraday seasonality (RTH busier than overnight). The generator is
seeded and deterministic. The data is NOT real market data: results on it validate the
machinery (labels, splits, metrics, leakage), never the market.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

REGIMES = ("trend_up", "trend_down", "range", "chop")
REGIME_P = (0.22, 0.22, 0.32, 0.24)
VOL_LEVELS = (("low", 0.6), ("normal", 1.0), ("high", 1.8), ("extreme", 3.0))
VOL_P = (0.25, 0.5, 0.2, 0.05)
SPECS = {"MES": ("MESH5", 5900.0, 0.25, 0.6, 900), "MNQ": ("MNQH5", 21000.0, 0.25, 2.6, 700)}
HEADER = ("# SYNTHETIC REGIME DATA - randomly generated with known regimes for testing only. "
          "NOT real market prices.\n")


def _sessions(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def generate(root: str, n_sessions: int, seed: int, start: date = date(2025, 1, 6)) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(bars, truth)``: bars as CSV-ready columns (naive New York local time, bar
    open) and the ground-truth regime / volatility level of every minute."""
    symbol, price, tick, sigma0, vol0 = SPECS[root]
    rng = np.random.default_rng(seed)
    rows, truth = [], []
    for td in _sessions(start, n_sessions):
        t = datetime.combine(td - timedelta(days=1), time(18, 0))
        end = datetime.combine(td, time(17, 0))
        regime, vol_name, vol_mult, left, anchor = None, "normal", 1.0, 0, price
        while t < end:
            if left <= 0:
                regime = REGIMES[rng.choice(len(REGIMES), p=REGIME_P)]
                vi = rng.choice(len(VOL_LEVELS), p=VOL_P)
                vol_name, vol_mult = VOL_LEVELS[vi]
                left, anchor = int(rng.integers(30, 151)), price
            minutes = t.hour * 60 + t.minute
            season = 2.0 if 570 <= minutes < 960 else 0.7
            sigma = sigma0 * vol_mult * season
            o = price
            path = [o]
            for _ in range(4):
                p = path[-1]
                if regime == "trend_up":
                    step = 0.30 * sigma / 2 + rng.normal(0, sigma / 2)
                elif regime == "trend_down":
                    step = -0.30 * sigma / 2 + rng.normal(0, sigma / 2)
                elif regime == "range":
                    step = -0.08 * (p - anchor) + rng.normal(0, 0.8 * sigma / 2)
                else:  # chop
                    step = -0.45 * (p - anchor) + rng.normal(0, 1.2 * sigma / 2)
                path.append(p + step)
            c = path[-1]
            h, l = max(path) + abs(rng.normal(0, sigma / 6)), min(path) - abs(rng.normal(0, sigma / 6))
            o, h, l, c = (round(v / tick) * tick for v in (o, h, l, c))
            h, l = max(h, o, c), min(l, o, c)
            vol = int(rng.poisson(vol0 * season * vol_mult * (1.3 if regime.startswith("trend") else 1.0)))
            rows.append((t.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c, vol, symbol))
            truth.append((t, regime, vol_name))
            price = c
            left -= 1
            t += timedelta(minutes=1)
    bars = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "symbol"])
    tr = pd.DataFrame(truth, columns=["local_time", "regime", "volatility"])
    tr.index = pd.DatetimeIndex(tr.pop("local_time")).tz_localize("America/New_York").tz_convert("UTC")
    return bars, tr


def write_csv(bars: pd.DataFrame, path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(HEADER)
        bars.to_csv(fh, index=False, lineterminator="\n")


def truth_on_bars(truth: pd.DataFrame, bar_index: pd.DatetimeIndex, minutes: int) -> pd.Series:
    """Ground truth per bar: the regime of the bar's LAST minute, as label space {+1, -1, 0}."""
    last_minute = bar_index + pd.Timedelta(minutes=minutes - 1)
    reg = truth["regime"].reindex(last_minute)
    mapped = reg.map({"trend_up": 1.0, "trend_down": -1.0, "range": 0.0, "chop": 0.0})
    return pd.Series(mapped.to_numpy(), index=bar_index)
