"""Forward-looking regime labels -- for TRAINING and VALIDATION only, never features.

Label of bar ``t`` describes the realised path of the next ``horizon`` bars:
``efficiency = |close[t+H] - close[t]| / sum(|close changes| over t+1..t+H)`` and
``move = (close[t+H] - close[t]) / ATR[t]``.
``+1`` (trend up) when efficiency >= threshold and move >= +move_atr, ``-1`` mirrored,
``0`` (no trend / range) otherwise. The label is NaN when the horizon is not fully inside
the same session and contiguous in time. ``label_end`` is the timestamp of bar ``t+H``;
validation purges every training row whose ``label_end`` reaches the test period.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.ai.market_regime.config import LabelConfig


def forward_labels(bars: pd.DataFrame, cfg: LabelConfig, atr_col: str = "atr_14") -> pd.DataFrame:
    H = cfg.horizon_bars
    c = bars["close"].astype(float)
    atr = bars[atr_col].astype(float).where(bars[atr_col] > 0)
    net = c.shift(-H) - c
    path = c.diff().abs().iloc[::-1].rolling(H, min_periods=H).sum().iloc[::-1].shift(-1)
    eff = (net.abs() / path.where(path > 0)).clip(0, 1)
    move = net / atr
    label = np.where((eff >= cfg.trend_efficiency) & (move >= cfg.trend_move_atr), 1.0,
                     np.where((eff >= cfg.trend_efficiency) & (move <= -cfg.trend_move_atr), -1.0, 0.0))
    idx = pd.Series(bars.index, index=bars.index)
    end = idx.shift(-H)
    step = pd.Timedelta(bars["available_at"].iloc[0] - bars.index[0]) if len(bars) else pd.Timedelta(0)
    sd = pd.to_datetime(bars["session_date"])
    same_session = sd.eq(sd.shift(-H))
    contiguous = (end - idx).eq(step * H)
    ok = net.notna() & path.notna() & atr.notna() & same_session & contiguous
    out = pd.DataFrame({"label": pd.Series(label, index=bars.index).where(ok), "label_end": end.where(ok),
                        "fwd_efficiency": eff.where(ok), "fwd_move_atr": move.where(ok)}, index=bars.index)
    return out
