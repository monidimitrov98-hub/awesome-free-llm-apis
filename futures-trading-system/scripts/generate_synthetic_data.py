"""Generate SYNTHETIC 1-minute MES/MNQ sample files.

The values are random-walk numbers produced with a fixed seed. They are NOT
real market data and must never be used to judge a strategy.

Usage (from the project root):
    python scripts/generate_synthetic_data.py
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"
TRADE_DATES = pd.bdate_range("2025-03-10", "2025-03-18")  # 7 Globex trade dates
SPECS = {
    # root: (symbol, start price, tick, per-minute volatility in points, base volume)
    "MES": ("MESH25", 5750.0, 0.25, 0.9, 900),
    "MNQ": ("MNQH25", 20200.0, 0.25, 4.0, 700),
}
HEADER = (
    "# SYNTHETIC DATA - randomly generated for testing only. NOT real market prices.\n"
    "# Timestamps are naive America/New_York local times (bar open), CME Globex 18:00-17:00 ET.\n"
)


def session_minutes(trade_date: date) -> list[datetime]:
    """Minute bar open times for the Globex session ending on ``trade_date``."""
    prev = trade_date - timedelta(days=1)  # Monday session opens Sunday 18:00
    start = datetime.combine(prev, time(18, 0))
    end = datetime.combine(trade_date, time(17, 0))
    return list(pd.date_range(start, end, freq="1min", inclusive="left").to_pydatetime())


def intraday_volume_profile(ts: datetime) -> float:
    """Rough U-shaped activity curve, highest during the 09:30-16:00 ET RTH session."""
    minutes = ts.hour * 60 + ts.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        x = (minutes - 570) / 390.0
        return 3.0 + 4.0 * (x - 0.5) ** 2 * 4
    return 0.35


def generate(root: str, seed: int) -> pd.DataFrame:
    symbol, price, tick, vol, base_volume = SPECS[root]
    rng = np.random.default_rng(seed)
    rows = []
    for td in TRADE_DATES:
        for ts in session_minutes(td.date()):
            activity = intraday_volume_profile(ts)
            sigma = vol * np.sqrt(activity / 3.0)
            open_ = price
            path = open_ + np.cumsum(rng.normal(0.0, sigma / 2.0, size=4))
            close = path[-1]
            high = max(open_, close, path.max()) + abs(rng.normal(0, sigma / 4))
            low = min(open_, close, path.min()) - abs(rng.normal(0, sigma / 4))
            o, h, l, c = (round(v / tick) * tick for v in (open_, high, low, close))
            h, l = max(h, o, c), min(l, o, c)
            volume = int(rng.poisson(base_volume * activity / 3.0))
            if activity < 1 and rng.random() < 0.02:
                volume = 0  # occasional empty overnight minute
            rows.append((ts.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c, volume, symbol))
            price = c
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "symbol"])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for i, root in enumerate(SPECS):
        df = generate(root, seed=20250310 + i)
        path = OUT_DIR / f"SYNTHETIC_{root}_1m.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(HEADER)
            df.to_csv(fh, index=False, lineterminator="\n")
        print(f"wrote {path} ({len(df)} rows)")


if __name__ == "__main__":
    main()
