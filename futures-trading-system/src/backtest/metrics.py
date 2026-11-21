"""Performance metrics. All money values are net of commissions and slippage
unless stated otherwise (slippage is inside the fill prices)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _side_stats(trades: pd.DataFrame) -> dict[str, Any]:
    n = len(trades)
    wins = trades[trades["net_pnl"] > 0]
    return {
        "trades": n,
        "net_profit": float(trades["net_pnl"].sum()),
        "win_rate": len(wins) / n if n else math.nan,
    }


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    """Largest peak-to-trough decline of an equity curve: ``(amount, fraction_of_peak)``."""
    if equity.empty:
        return 0.0, 0.0
    peak = equity.cummax()
    drawdown = peak - equity
    i = int(np.argmax(drawdown.to_numpy()))
    amount = float(drawdown.iloc[i])
    pct = amount / float(peak.iloc[i]) if peak.iloc[i] > 0 else math.nan
    return amount, pct


def compute_metrics(
    trades: pd.DataFrame, equity: pd.DataFrame, initial_capital: float
) -> tuple[dict[str, Any], pd.Series]:
    """Compute performance metrics.

    Definitions (per trade, ``net_pnl`` = gross PnL - commissions; slippage is
    already in the fill prices):

    * win: ``net_pnl > 0``; loss: ``net_pnl < 0``; breakeven trades count in totals only
    * gross_profit = sum of winning ``net_pnl``; gross_loss = sum of losing ``net_pnl`` (<= 0)
    * profit_factor = gross_profit / |gross_loss| (inf if no losses, NaN if no trades)
    * expectancy = mean ``net_pnl`` per trade
    * average R = mean ``r_multiple`` over trades with a stop-loss
    * max drawdown from the mark-to-market equity curve, starting at initial capital
    * daily PnL = sum of ``net_pnl`` by exit session date

    Returns:
        ``(metrics, daily_pnl)``.
    """
    n = len(trades)
    pnl = trades["net_pnl"] if n else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_profit, gross_loss = float(wins.sum()), float(losses.sum())
    if n == 0:
        pf = math.nan
    elif gross_loss == 0:
        pf = math.inf if gross_profit > 0 else math.nan
    else:
        pf = gross_profit / abs(gross_loss)

    curve = pd.concat([pd.Series([initial_capital]), equity["equity"].reset_index(drop=True)]) if len(equity) else pd.Series([initial_capital])
    dd_amount, dd_pct = max_drawdown(curve.reset_index(drop=True))

    r = trades["r_multiple"].dropna() if n else pd.Series(dtype=float)
    daily = (trades.groupby("exit_session_date")["net_pnl"].sum().rename("net_pnl")
             if n else pd.Series(dtype=float, name="net_pnl"))

    metrics: dict[str, Any] = {
        "total_trades": n,
        "net_profit": float(pnl.sum()),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "win_rate": len(wins) / n if n else math.nan,
        "profit_factor": pf,
        "average_win": float(wins.mean()) if len(wins) else math.nan,
        "average_loss": float(losses.mean()) if len(losses) else math.nan,
        "expectancy": float(pnl.mean()) if n else math.nan,
        "average_r_multiple": float(r.mean()) if len(r) else math.nan,
        "max_drawdown": dd_amount,
        "max_drawdown_pct": dd_pct,
        "total_commission": float(trades["commission"].sum()) if n else 0.0,
        "total_slippage_cost": float(trades["slippage_cost"].sum()) if n else 0.0,
        "gross_pnl_before_costs": float((trades["gross_pnl"] + trades["slippage_cost"]).sum()) if n else 0.0,
        "ambiguous_trades": int(trades["ambiguous"].sum()) if n else 0,
        "final_equity": float(curve.iloc[-1]),
        "long": _side_stats(trades[trades["side"] == "long"]) if n else _side_stats(pd.DataFrame({"net_pnl": []})),
        "short": _side_stats(trades[trades["side"] == "short"]) if n else _side_stats(pd.DataFrame({"net_pnl": []})),
    }
    return metrics, daily
