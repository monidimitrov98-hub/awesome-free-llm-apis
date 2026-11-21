"""Shared helpers for Sprint 4 regime tests."""

from __future__ import annotations

import math
from functools import lru_cache

import pandas as pd

from src.ai.market_regime.config import RegimeConfig, load_regime_config
from src.ai.market_regime.features import add_regime_features, add_shock_features
from src.ai.market_regime.synthetic import generate, write_csv
from src.config import load_config

RC = load_regime_config()
APP = load_config()


@lru_cache(maxsize=4)
def regime_market(root: str = "MES", sessions: int = 8, seed: int = 3, tmp: str = "/tmp"):
    """Pipeline output (MarketData) of a small SYNTHETIC regime-switching dataset."""
    import tempfile
    from pathlib import Path

    from src.pipeline import build_market_data

    bars, truth = generate(root, sessions, seed=seed)
    d = Path(tempfile.mkdtemp(prefix="regime_"))
    path = d / f"SYNTHETIC_REGIME_{root}_1m.csv"
    write_csv(bars, path)
    return build_market_data(path, APP), truth, path


def features(frame: pd.DataFrame, cfg: RegimeConfig = RC) -> pd.DataFrame:
    return add_regime_features(frame, cfg.features, APP.session)


def row(**kw) -> dict:
    """A valid feature row describing a quiet, no-trend market; override fields per test."""
    base = {"rf_valid": True, "rf_spread_atr": 0.0, "rf_slope_atr": 0.0, "rf_vwap_dist_atr": 0.0,
            "rf_ema_dist_atr": 0.0, "rf_efficiency": 0.25, "rf_dir_changes": 0.4, "rf_range_atr": 5.0,
            "rf_prev_range_atr": 5.0, "rf_atr_pct": 0.5, "rf_atr_hist_n": 60.0, "rf_atr_ratio": 1.0,
            "rf_body_mean": 0.5, "rf_rsi": 50.0, "rf_rvol": 1.0, "rf_breakout": 0.0, "rf_failed_breakout": 0.0,
            "rf_structure": 0.0, "rf_bos": 0.0, "rf_choch": 0.0, "rf_struct_failed": 0.0}
    base.update(kw)
    return base


def mirror_row(r: dict) -> dict:
    """Bearish mirror of a bullish row (signed features flipped)."""
    out = dict(r)
    for k in ("rf_spread_atr", "rf_slope_atr", "rf_vwap_dist_atr", "rf_ema_dist_atr", "rf_breakout",
              "rf_failed_breakout", "rf_structure", "rf_bos", "rf_choch"):
        if k in out and isinstance(out[k], (int, float)) and not math.isnan(out[k]):
            out[k] = -out[k]
    return out


__all__ = ["RC", "APP", "regime_market", "features", "row", "mirror_row", "add_shock_features"]
