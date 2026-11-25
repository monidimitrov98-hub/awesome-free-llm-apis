"""Deterministic SYNTHETIC fixtures for the Sprint 12 data-layer tests (never evidence of market behaviour).

``bars()`` produces 1-minute bars for two MES contracts (MESH25 front, MESM25 next) on every scheduled-open
minute of the CME calendar between two dates, with the deferred contract priced at a fixed premium and the
volume migrating from the front to the next contract around the roll week. The writers emit the same bars
in three provider formats (Databento OHLCV CSV, generic project CSV, NinjaTrader export).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.histdata.calendar import load_calendar

CAL = load_calendar()


def bars(start="2025-03-05 22:00", end="2025-03-20 20:00", contracts=("MESH25", "MESM25"), seed=7,
         premium=30.0, roll_day="2025-03-13") -> pd.DataFrame:
    grid = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"), freq="1min", inclusive="left")
    grid = grid[CAL.is_open(grid).to_numpy()]
    rng = np.random.default_rng(seed)
    steps = rng.choice([-1, 0, 0, 1], size=len(grid)) * 0.25
    base = 5700.0 + np.cumsum(steps)
    td = CAL.trade_dates(grid)
    rows = []
    for k, c in enumerate(contracts):
        px = base + premium * k
        rel = rng.integers(0, 3, size=len(grid)) * 0.25
        o = np.round(px / 0.25) * 0.25
        cl = np.round((px + steps) / 0.25) * 0.25
        hi = np.maximum(o, cl) + rel
        lo = np.minimum(o, cl) - rel
        after = (td >= pd.Timestamp(roll_day)).to_numpy()
        vol = np.where(after if k == 1 else ~after, rng.integers(50, 400, size=len(grid)), rng.integers(1, 20, size=len(grid)))
        rows.append(pd.DataFrame({"ts_utc": grid, "contract": c, "open": o, "high": hi, "low": lo, "close": cl,
                                  "volume": vol.astype(float)}))
    return pd.concat(rows, ignore_index=True)


def _short(contract: str) -> str:
    return contract[:-2] + contract[-1]                     # MESH25 -> MESH5 (Databento raw symbol style)


def write_databento(b: pd.DataFrame, path: Path, *, fixed_point=True, iso=False) -> Path:
    ts = pd.DatetimeIndex(b["ts_utc"])
    f = pd.DataFrame({"ts_event": ts.strftime("%Y-%m-%dT%H:%M:%S.000000000Z") if iso else ts.as_unit("ns").asi8.astype(str),
                      "rtype": 33, "publisher_id": 1, "instrument_id": b["contract"].map(hash).abs() % 100000})
    for c in ("open", "high", "low", "close"):
        f[c] = (b[c] * 1e9).round().astype("int64") if fixed_point else b[c]
    f["volume"] = b["volume"].astype("int64")
    f["symbol"] = b["contract"].map(_short)
    f.to_csv(path, index=False)
    return path


def write_generic(b: pd.DataFrame, path: Path, tz="America/New_York") -> Path:
    loc = pd.DatetimeIndex(b["ts_utc"]).tz_convert(tz).tz_localize(None)
    f = pd.DataFrame({"timestamp": loc.strftime("%Y-%m-%d %H:%M:%S"), "open": b["open"], "high": b["high"],
                      "low": b["low"], "close": b["close"], "volume": b["volume"].astype("int64"),
                      "symbol": b["contract"]})
    path.write_text("# SYNTHETIC fixture: not market prices\n" + f.to_csv(index=False), encoding="utf-8")
    return path


def write_nt(b: pd.DataFrame, path: Path, tz="America/Chicago") -> Path:
    end = (pd.DatetimeIndex(b["ts_utc"]) + pd.Timedelta(minutes=1)).tz_convert(tz).tz_localize(None)
    lines = [f"{t:%Y%m%d %H%M%S};{o:g};{h:g};{lo:g};{c:g};{int(v)}"
             for t, o, h, lo, c, v in zip(end, b["open"], b["high"], b["low"], b["close"], b["volume"])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
