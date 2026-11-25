"""Evaluation report of one research run (Sprint 11).

The report describes what happened in ONE sample. Its evidence label never says that a strategy is
profitable; at most it says that a positive expectancy is supported IN THIS SAMPLE after the multiple-testing
adjustment, and the scope (in-sample / validation / walk-forward / one-time OOS) is always stated.

Evidence labels (no performance pass mark is invented; the only fixed numbers are the project's existing
sample-size convention -- fewer than 30 trades is INSUFFICIENT, see src.analytics.shadow.stats -- and the
conventional 5 % one-sided significance level, both stated in the report):

* INSUFFICIENT EVIDENCE: fewer than 30 trades or fewer than 2 trading days.
* POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE: Holm-adjusted p < alpha AND the day-bootstrap interval of the
  mean daily net P&L is entirely above zero.
* NEGATIVE EXPECTANCY IN THIS SAMPLE: the day-bootstrap interval is entirely below zero.
* INCONCLUSIVE: everything else (the default).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from src.analytics.shadow import stats as sst
from src.research import stats as rst

INSUFFICIENT_TRADES = sst.INSUFFICIENT
LABELS = ("INSUFFICIENT EVIDENCE", "INCONCLUSIVE", "NEGATIVE EXPECTANCY IN THIS SAMPLE",
          "POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE")
SCOPES = {"full": "FULL SAMPLE (in-sample: no out-of-sample claim)",
          "train": "TRAINING PERIOD (in-sample)",
          "validation": "VALIDATION PERIOD (used for research decisions: not out of sample once used)",
          "wf_out_of_sample": "WALK-FORWARD TEST FOLDS (pseudo out-of-sample; not the one-time holdout)",
          "purged_cv": "PURGED CROSS-VALIDATION TEST BLOCKS (research data overall; not out of sample)",
          "oos": "ONE-TIME OUT-OF-SAMPLE HOLDOUT (ledger-recorded reveal)"}


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def daily_pnl(trades: pd.DataFrame) -> pd.Series:
    """Net P&L per trading day (exit session date). Days without trades are NOT included (no zero padding)."""
    if trades is None or trades.empty:
        return pd.Series(dtype=float)
    key = "exit_session_date" if "exit_session_date" in trades.columns else "session_date"
    return trades.groupby(pd.to_datetime(trades[key]))["net_pnl"].sum().sort_index()


def evidence_label(n_trades: int, n_days: int, daily_ci: tuple[float, float] | None, p_adj: float | None,
                   alpha: float = rst.ALPHA) -> str:
    if n_trades < INSUFFICIENT_TRADES or n_days < 2 or daily_ci is None:
        return "INSUFFICIENT EVIDENCE"
    if p_adj is not None and p_adj < alpha and daily_ci[0] > 0:
        return "POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE"
    if daily_ci[1] < 0:
        return "NEGATIVE EXPECTANCY IN THIS SAMPLE"
    return "INCONCLUSIVE"


def _group(trades: pd.DataFrame, key: pd.Series, name: str) -> list[dict[str, Any]]:
    rows = []
    for k, g in trades.groupby(key, dropna=False):
        nets = g["net_pnl"].astype(float).tolist()
        rows.append({name: str(k), "trades": len(g), "net": float(sum(nets)), "mean": float(np.mean(nets)),
                     "win_rate": float(np.mean([x > 0 for x in nets])), "sample": sst.sample_level(len(g))})
    return rows


def breakdowns(trades: pd.DataFrame, tz: str) -> dict[str, Any]:
    """EXPLORATORY subgroup tables (many subgroups invite false discoveries; none is called significant)."""
    if trades.empty:
        return {"note": "no trades"}
    t = trades.copy()
    et = pd.to_datetime(t["entry_time"], utc=True).dt.tz_convert(tz)
    out: dict[str, Any] = {
        "note": "EXPLORATORY: subgroup results are not adjusted for multiple comparisons and prove nothing",
        "direction": _group(t, t["side"], "direction"),
        "entry_hour_local": _group(t, et.dt.hour, "hour"),
        "weekday": _group(t, et.dt.day_name(), "weekday"),
        "exit_reason": _group(t, t["exit_reason"], "exit_reason"),
    }
    for col in ("trend", "volatility", "suitability", "regime_at_entry"):
        if col in t.columns and t[col].notna().any():
            out[f"regime_{col}"] = _group(t, t[col].astype(str), col)
    return out


def evaluate(trades: pd.DataFrame, *, capital: float, scope: str, tz: str, equity: pd.DataFrame | None = None,
             sessions_in_period: int | None = None, family_size: int = 1, family_pvalues: list[float | None] | None = None,
             family_index: int | None = None, n_boot: int = 5000, seed: int = 11) -> dict[str, Any]:
    """Full evaluation of one run. ``family_*`` describe every configuration attempted in the experiment family
    (failures included) for the multiple-testing adjustment; by default the run is a family of one."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    t = trades if trades is not None else pd.DataFrame()
    n = len(t)
    nets = t["net_pnl"].astype(float).tolist() if n else []
    daily = daily_pnl(t)
    dv = daily.tolist()
    days = [str(pd.Timestamp(d).date()) for d in (t["exit_session_date"] if n else [])]
    recs = [{"net_pnl": float(r.net_pnl), "gross_pnl": float(r.gross_pnl), "commission": float(r.commission),
             "slippage_cost": float(r.slippage_cost), "r_multiple": _f(r.r_multiple), "day": d}
            for r, d in zip(t.itertuples(), days)] if n else []
    core = sst.trade_stats(recs, n_boot=2000, seed=8) if n else {}
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    p_raw = rst.one_sided_p(dv)
    if family_pvalues is not None and family_index is not None:
        holm, bh = rst.holm(family_pvalues)[family_index], rst.benjamini_hochberg(family_pvalues)[family_index]
        m = len(family_pvalues)
    else:
        m = max(1, family_size)
        fam = [p_raw] + [None] * (m - 1)
        holm, bh = rst.holm(fam)[0], rst.benjamini_hochberg(fam)[0]
    dci = rst.day_bootstrap_mean(dv, n_boot=n_boot, seed=seed)
    label = evidence_label(n, len(dv), dci, holm)
    mdd = None
    if equity is not None and len(equity):
        eq = equity["equity"].astype(float)
        mdd = float((eq.cummax() - eq).max())
    exc = {}
    for c in ("mae_dollars", "mfe_dollars"):
        if c in t.columns:
            exc[c] = sst.summary(t[c].astype(float).tolist())
    if "mae_dollars" in t.columns and "initial_risk" in t.columns and n:
        risk_d = (t["actual_risk_dollars"] if "actual_risk_dollars" in t.columns else t["initial_risk"]).astype(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            exc["mae_r"] = sst.summary((t["mae_dollars"] / risk_d).replace([np.inf, -np.inf], np.nan).tolist())
            exc["mfe_r"] = sst.summary((t["mfe_dollars"] / risk_d).replace([np.inf, -np.inf], np.nan).tolist())
    gross = float(t["gross_pnl"].sum()) if n else 0.0
    costs = float(t["total_costs"].sum()) if n else 0.0
    return {
        "scope": scope, "scope_text": SCOPES[scope],
        "sample": {"trades": n, "trading_days_with_trades": len(dv), "sessions_in_period": sessions_in_period,
                   "level": sst.sample_level(n)},
        "net_profit": float(sum(nets)), "ev_per_trade": float(np.mean(nets)) if n else None,
        "ev_per_trade_ci95_t": core.get("expectancy_ci95_t"),
        "ev_per_trade_ci95_day_bootstrap": core.get("expectancy_ci95_day_bootstrap"),
        "ev_per_trade_ci95_conservative": core.get("expectancy_ci95"), "average_r": core.get("average_r"),
        "mean_daily_pnl": float(np.mean(dv)) if dv else None, "mean_daily_pnl_ci95_day_bootstrap": dci,
        "win_rate": (len(wins) / n) if n else None, "win_rate_ci95_wilson": sst.wilson(len(wins), n),
        "average_win": float(np.mean(wins)) if wins else None, "average_loss": float(np.mean(losses)) if losses else None,
        "profit_factor": sst.profit_factor(nets), "profit_factor_ci95_day_bootstrap": core.get("profit_factor_ci95"),
        "max_drawdown_equity": mdd, "max_drawdown_trade_sequence": sst.max_drawdown(nets),
        "max_consecutive_losses": sst.max_consecutive_losses(nets),
        "ratios": rst.ratios(dv, capital),
        "excursions": exc or {"note": "MAE/MFE not computed for this run"},
        "costs": {"gross_before_costs": gross, "total_costs": costs,
                  "costs_share_of_gross_abs": (costs / abs(gross)) if gross else None,
                  "commission": float(t["commission"].sum()) if n else 0.0,
                  "slippage": float(t["slippage_cost"].sum()) if n else 0.0},
        "tests": {"unit": "trading day (daily net P&L)", "h0": "mean daily net P&L <= 0 (one-sided t)",
                  "p_raw": p_raw, "family_size": m, "p_holm": holm, "p_bh": bh, "alpha": rst.ALPHA},
        "power": {"days_needed_80pct_at_observed_effect": rst.required_days(dv, alpha=rst.ALPHA / m),
                  "note": "normal approximation at the multiple-testing adjusted level; information, not a pass mark"},
        "breakdowns": breakdowns(t, tz),
        "evidence": label,
        "limitations": [
            "Historical simulation; future results can differ materially.",
            "Days without trades are not counted as zero-P&L observations.",
            "Subgroup tables are exploratory and unadjusted.",
            "No claim of profitability is made by this report.",
        ],
    }


__all__ = ["evaluate", "evidence_label", "daily_pnl", "breakdowns", "LABELS", "SCOPES"]
