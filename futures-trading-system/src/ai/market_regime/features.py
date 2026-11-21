"""Causal regime features (``rf_*`` columns) computed from Sprint 1 bars and indicators.

Every value on row ``t`` uses only rows ``<= t`` of the same frame (rolling windows look
backwards, histories are shifted by one bar, swing pivots are confirmed/non-repainting),
so a bar's features are known at its ``available_at``. Causality is verified by
truncation and future-perturbation tests. Missing inputs give NaN; ``rf_valid`` is True
only when every core feature is finite, the bar is complete and the feature windows are
contiguous in time (data gaps are never treated as adjacent bars).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.ai.market_regime.config import FeatureConfig
from src.config import SessionConfig
from src.data.sessions import session_bounds
from src.indicators.atr import true_range
from src.indicators.swings import STRUCTURE_BEARISH, STRUCTURE_BULLISH, market_structure

CORE_FEATURES = (
    "rf_spread_atr", "rf_slope_atr", "rf_vwap_dist_atr", "rf_ema_dist_atr", "rf_efficiency",
    "rf_dir_changes", "rf_range_atr", "rf_atr_pct", "rf_atr_ratio", "rf_body_mean", "rf_rsi",
)
ML_FEATURES = CORE_FEATURES + (
    "rf_rvol", "rf_upper_wick_mean", "rf_lower_wick_mean", "rf_breakout", "rf_failed_breakout",
    "rf_structure", "rf_bos", "rf_choch", "rf_struct_failed", "rf_session_minutes", "rf_prev_session_trend",
)


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    b = b.where(b != 0)
    return a / b


def add_regime_features(
    bars: pd.DataFrame,
    cfg: FeatureConfig,
    session: SessionConfig,
    *,
    ema_fast: str = "ema_9",
    ema_slow: str = "ema_21",
    atr_col: str = "atr_14",
    rsi_col: str = "rsi_14",
) -> pd.DataFrame:
    """Return a copy of ``bars`` (Sprint 1 frame) with causal ``rf_*`` feature columns."""
    out = bars.copy()
    n = len(out)
    if not out.index.is_monotonic_increasing or out.index.has_duplicates:
        raise ValueError("regime features need strictly increasing timestamps (validate/sort the bars first)")
    if n == 0:
        for col in ML_FEATURES + ("rf_valid",):
            out[col] = pd.Series(dtype=float)
        return out
    o, h, l, c = (out[k].astype(float) for k in ("open", "high", "low", "close"))
    atr = out[atr_col].astype(float).where(out[atr_col] > 0)
    ef, es = out[ema_fast].astype(float), out[ema_slow].astype(float)

    out["rf_spread_atr"] = _safe_div(ef - es, atr)
    out["rf_slope_atr"] = _safe_div(es - es.shift(cfg.slope_bars), atr)
    out["rf_vwap_dist_atr"] = _safe_div(c - out["vwap"].astype(float), atr)
    out["rf_ema_dist_atr"] = _safe_div(c - ef, atr)

    diff = c.diff()
    path = diff.abs().rolling(cfg.efficiency_window, min_periods=cfg.efficiency_window).sum()
    out["rf_efficiency"] = _safe_div((c - c.shift(cfg.efficiency_window)).abs(), path).clip(0, 1)
    sign = np.sign(diff)
    flips = ((sign != sign.shift(1)) & (sign != 0) & (sign.shift(1) != 0)).astype(float).where(diff.notna())
    out["rf_dir_changes"] = flips.rolling(cfg.efficiency_window, min_periods=cfg.efficiency_window).mean()

    hi_r = h.rolling(cfg.range_window, min_periods=cfg.range_window).max()
    lo_r = l.rolling(cfg.range_window, min_periods=cfg.range_window).min()
    out["rf_range_atr"] = _safe_div(hi_r - lo_r, atr)
    out["rf_prev_range_atr"] = out["rf_range_atr"].shift(1)

    # Volatility vs ITS OWN past only (history shifted by one bar).
    prev_atr = atr.shift(1)
    if cfg.volatility_reference == "time_of_day":
        # Intraday volatility is seasonal (RTH >> overnight): compare with the SAME local time
        # of day in previous sessions only.
        local = pd.DatetimeIndex(out.index).tz_convert(session.timezone)
        key = pd.Series(local.hour * 60 + local.minute, index=out.index)
        out["rf_atr_pct"], out["rf_atr_hist_n"] = _time_of_day_percentile(
            atr, key, cfg.atr_history_sessions, cfg.atr_history_sessions_min)
    else:
        out["rf_atr_pct"] = _rolling_percentile(atr, cfg.atr_history, cfg.atr_history_min)
        out["rf_atr_hist_n"] = atr.shift(1).rolling(cfg.atr_history, min_periods=1).count().clip(upper=cfg.atr_history)
    out["rf_atr_ratio"] = _safe_div(atr, prev_atr.rolling(cfg.atr_ratio_window, min_periods=cfg.atr_ratio_window).mean())
    out["rf_rvol"] = out["rvol"].astype(float) if "rvol" in out else np.nan
    out["rf_rsi"] = out[rsi_col].astype(float)

    rng = (h - l)
    body = _safe_div((c - o).abs(), rng).fillna(0.0).where(rng.notna())
    upper = _safe_div(h - np.maximum(o, c), rng).fillna(0.0).where(rng.notna())
    lower = _safe_div(np.minimum(o, c) - l, rng).fillna(0.0).where(rng.notna())
    out["rf_body_mean"] = body.rolling(cfg.body_window, min_periods=cfg.body_window).mean()
    out["rf_upper_wick_mean"] = upper.rolling(cfg.body_window, min_periods=cfg.body_window).mean()
    out["rf_lower_wick_mean"] = lower.rolling(cfg.body_window, min_periods=cfg.body_window).mean()

    # Breakouts from the PRIOR range (excluding the current bar) and failed breakouts.
    prior_hi = h.shift(1).rolling(cfg.range_window, min_periods=cfg.range_window).max()
    prior_lo = l.shift(1).rolling(cfg.range_window, min_periods=cfg.range_window).min()
    up, down = c > prior_hi, c < prior_lo
    out["rf_breakout"] = np.where(up, 1.0, np.where(down, -1.0, 0.0))
    out.loc[prior_hi.isna(), "rf_breakout"] = np.nan
    k = cfg.failed_break_bars
    lvl_up = prior_hi.where(up).shift(1).ffill(limit=k - 1) if k > 1 else prior_hi.where(up).shift(1)
    lvl_dn = prior_lo.where(down).shift(1).ffill(limit=k - 1) if k > 1 else prior_lo.where(down).shift(1)
    failed_up = lvl_up.notna() & (c < lvl_up) & ~up
    failed_dn = lvl_dn.notna() & (c > lvl_dn) & ~down
    out["rf_failed_breakout"] = np.where(failed_up, 1.0, np.where(failed_dn, -1.0, 0.0))

    # Market structure from confirmed, data-valid pivots (Sprint 3 swings).
    step = pd.Timedelta(out["available_at"].iloc[0] - out.index[0])
    ms = market_structure(h, l, cfg.swing_left, cfg.swing_right, valid=out["is_complete"].astype(bool), step=step)
    code = pd.Series(np.select([ms["structure"] == STRUCTURE_BULLISH, ms["structure"] == STRUCTURE_BEARISH,
                                ms["structure"] == "mixed"], [1.0, -1.0, 0.0], default=np.nan), index=out.index)
    out["rf_structure"] = code
    prev_code = code.shift(1)
    brk_up, brk_dn = c > ms["last_high"], c < ms["last_low"]
    out["rf_bos"] = np.where(brk_up & (prev_code == 1), 1.0, np.where(brk_dn & (prev_code == -1), -1.0, 0.0))
    out["rf_choch"] = np.where(brk_up & (prev_code == -1), 1.0, np.where(brk_dn & (prev_code == 1), -1.0, 0.0))
    brk_lvl_up = ms["last_high"].where(brk_up).shift(1).ffill(limit=max(k - 1, 1))
    brk_lvl_dn = ms["last_low"].where(brk_dn).shift(1).ffill(limit=max(k - 1, 1))
    out["rf_struct_failed"] = ((brk_lvl_up.notna() & (c < brk_lvl_up) & ~brk_up)
                               | (brk_lvl_dn.notna() & (c > brk_lvl_dn) & ~brk_dn)).astype(float)
    out["rf_last_swing_high"] = ms["last_high"]
    out["rf_last_swing_low"] = ms["last_low"]

    _session_features(out, session)

    # Contiguity of the longest backward window (in TIME, not rows) and completeness.
    span = max(cfg.range_window, cfg.efficiency_window, cfg.body_window)
    gap = pd.Series((pd.Series(out.index).diff() != step).to_numpy(dtype=float), index=out.index)
    gap.iloc[0] = 1.0
    # gap[t] = 1 when bar t does not follow bar t-1 by exactly one period; a window of `span`
    # bars contains span - 1 such transitions, all of which must be regular.
    out["rf_contiguous"] = gap.rolling(span - 1, min_periods=span - 1).sum().eq(0)
    complete_window = out["is_complete"].astype(float).rolling(span, min_periods=span).min().eq(1.0)
    finite = np.isfinite(out[list(CORE_FEATURES)].astype(float)).all(axis=1)
    out["rf_valid"] = finite & out["rf_contiguous"] & complete_window
    return out


def _rolling_percentile(series: pd.Series, history: int, min_history: int) -> pd.Series:
    """Share of the previous ``history`` values strictly below the current one (NaN if < min)."""
    values = series.to_numpy(dtype=float)
    out = np.full(values.shape[0], np.nan)
    for i in range(values.shape[0]):
        cur = values[i]
        if not np.isfinite(cur):
            continue
        past = values[max(0, i - history):i]
        past = past[np.isfinite(past)]
        if past.shape[0] >= min_history:
            out[i] = float((past < cur).mean())
    return pd.Series(out, index=series.index)


def _time_of_day_percentile(series: pd.Series, key: pd.Series, history: int, min_history: int
                            ) -> tuple[pd.Series, pd.Series]:
    """Share of the previous ``history`` values AT THE SAME KEY (time of day) strictly below
    the current one; only earlier rows are used (NaN with fewer than ``min_history``).
    Also returns how many past values were used (the percentile's resolution)."""
    values = series.to_numpy(dtype=float)
    out = np.full(values.shape[0], np.nan)
    count = np.zeros(values.shape[0])
    for _, positions in key.groupby(key, sort=False).indices.items():
        past: list[float] = []
        for i in positions:  # positions are in time order
            cur = values[i]
            count[i] = min(len(past), history)
            if np.isfinite(cur) and len(past) >= min_history:
                window = np.asarray(past[-history:])
                out[i] = float((window < cur).mean())
            if np.isfinite(cur):
                past.append(cur)
    return pd.Series(out, index=series.index), pd.Series(count, index=series.index)


def _session_features(out: pd.DataFrame, session: SessionConfig) -> None:
    sd = pd.to_datetime(out["session_date"])
    dates = sd.dropna().unique()
    if len(dates):
        bounds = session_bounds(dates, session)
        start = sd.map(bounds["start"])
        out["rf_session_minutes"] = (pd.DatetimeIndex(out.index) - pd.DatetimeIndex(start)).total_seconds() / 60.0
    else:
        out["rf_session_minutes"] = np.nan
    # Previous session (complete by definition): its last close vs its last VWAP.
    last = out.groupby(sd)[["close", "vwap"]].last()
    prev = np.sign(last["close"] - last["vwap"]).shift(1)
    out["rf_prev_session_trend"] = sd.map(prev).astype(float)
    out.loc[sd.isna().to_numpy(), "rf_session_minutes"] = np.nan


def add_shock_features(bars_1m: pd.DataFrame, cfg: FeatureConfig, atr_col: str = "atr_14") -> pd.DataFrame:
    """1-minute volatility shock: max true range of the last ``shock_window`` bars / 1m ATR."""
    out = bars_1m.copy()
    if out.empty:
        out["rf_shock_ratio"] = pd.Series(dtype=float)
        return out
    tr = true_range(out["high"].astype(float), out["low"].astype(float), out["close"].astype(float))
    atr = out[atr_col].astype(float).where(out[atr_col] > 0)
    out["rf_shock_ratio"] = _safe_div(tr.rolling(cfg.shock_window, min_periods=cfg.shock_window).max(), atr)
    return out
