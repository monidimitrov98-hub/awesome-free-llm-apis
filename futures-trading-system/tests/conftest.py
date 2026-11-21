"""Shared fixtures and helpers."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import AppConfig, SessionConfig, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = PROJECT_ROOT / "data" / "samples"


@pytest.fixture(scope="session")
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def globex() -> SessionConfig:
    return SessionConfig("globex", time(18, 0), time(17, 0), "America/New_York")


@pytest.fixture
def rth() -> SessionConfig:
    return SessionConfig("rth", time(9, 30), time(16, 0), "America/New_York")


def make_bars(start_utc: str, closes: list[float], freq: str = "1min", volume: float | list[float] = 100.0) -> pd.DataFrame:
    """Simple valid OHLCV bars around the given closes (high/low = close +/- 1)."""
    idx = pd.date_range(start_utc, periods=len(closes), freq=freq, tz="UTC", name="timestamp")
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    vol = np.full(len(c), float(volume)) if np.isscalar(volume) else np.asarray(volume, dtype=float)
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, c) + 1, "low": np.minimum(o, c) - 1, "close": c, "volume": vol},
        index=idx,
    )


def write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def full_session_bars(session: SessionConfig, session_dates: list[str], freq: str = "1min") -> pd.DataFrame:
    """Every bar of each session's real (DST-aware) window, built from exact UTC bounds."""
    from src.data.sessions import session_bounds

    bounds = session_bounds(pd.to_datetime(session_dates), session)
    parts = [
        pd.date_range(b.start, b.end, freq=freq, inclusive="left", name="timestamp") for b in bounds.itertuples()
    ]
    idx = parts[0].append(parts[1:]) if len(parts) > 1 else parts[0]
    n = len(idx)
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": np.full(n, 10.0)}, index=idx
    )
