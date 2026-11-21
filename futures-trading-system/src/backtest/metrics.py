"""Performance metrics.

"Gross" always means before commission AND slippage; "net" means after both.
See :func:`compute_metrics` for every definition.
"""

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

    Per-trade columns used (all in dollars):

    * ``gross_pnl``     – price PnL at the un-slipped prices, BEFORE all costs
    * ``slippage_cost`` – adverse slippage of entry + exit
    * ``commission``    – commission of entry + exit
    * ``net_pnl``       – ``gross_pnl - slippage_cost - commission`` (AFTER all costs)

    Definitions:

    * gross_profit / gross_loss – sum of ``gross_pnl`` over trades with positive /
      negative ``gross_pnl`` (genuinely gross; gross_loss <= 0)
    * gross_pnl_before_costs    – sum of ``gross_pnl`` (= gross_profit + gross_loss)
    * net_profit                – sum of ``net_pnl``
    * win / loss                – ``net_pnl > 0`` / ``net_pnl < 0`` (breakeven counts in totals only)
    * net_profit_of_winners / net_loss_of_losers – sum of ``net_pnl`` of wins / losses
    * profit_factor             – net_profit_of_winners / |net_loss_of_losers| (after costs);
      inf if there are wins and no losses, NaN if no trades
    * gross_profit_factor       – gross_profit / |gross_loss| (before costs)
    * win_rate                  – wins / total trades
    * average_win / average_loss – mean ``net_pnl`` of wins / losses
    * expectancy                – net_profit / total trades (mean ``net_pnl``)
    * average_r_multiple        – mean ``r_multiple`` over trades with a stop-loss
    * max_drawdown              – largest peak-to-trough fall of the mark-to-market
      equity curve, starting at the initial capital
    * daily_pnl                 – sum of ``net_pnl`` by exit session date

    Returns:
        ``(metrics, daily_pnl)``.
    """
    n = len(trades)
    pnl = trades["net_pnl"].astype(float) if n else pd.Series(dtype=float)
    gross = trades["gross_pnl"].astype(float) if n else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]

    def factor(up: float, down: float) -> float:
        if n == 0:
            return math.nan
        if down == 0:
            return math.inf if up > 0 else math.nan
        return up / abs(down)

    curve = (pd.concat([pd.Series([initial_capital]), equity["equity"].reset_index(drop=True)])
             if len(equity) else pd.Series([initial_capital]))
    dd_amount, dd_pct = max_drawdown(curve.reset_index(drop=True))

    r = trades["r_multiple"].dropna() if n else pd.Series(dtype=float)
    daily = (trades.groupby("exit_session_date")["net_pnl"].sum().rename("net_pnl")
             if n else pd.Series(dtype=float, name="net_pnl"))
    gross_profit, gross_loss = float(gross[gross > 0].sum()), float(gross[gross < 0].sum())
    net_won, net_lost = float(wins.sum()), float(losses.sum())
    commission = float(trades["commission"].sum()) if n else 0.0
    slippage = float(trades["slippage_cost"].sum()) if n else 0.0
    empty = pd.DataFrame({"net_pnl": []})

    metrics: dict[str, Any] = {
        "total_trades": n,
        "gross_pnl_before_costs": float(gross.sum()),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "gross_profit_factor": factor(gross_profit, gross_loss),
        "total_commission": commission,
        "total_slippage_cost": slippage,
        "total_costs": commission + slippage,
        "net_profit": float(pnl.sum()),
        "net_profit_of_winners": net_won,
        "net_loss_of_losers": net_lost,
        "profit_factor": factor(net_won, net_lost),
        "win_rate": len(wins) / n if n else math.nan,
        "average_win": float(wins.mean()) if len(wins) else math.nan,
        "average_loss": float(losses.mean()) if len(losses) else math.nan,
        "expectancy": float(pnl.sum()) / n if n else math.nan,
        "average_r_multiple": float(r.mean()) if len(r) else math.nan,
        "max_drawdown": dd_amount,
        "max_drawdown_pct": dd_pct,
        "ambiguous_trades": int(trades["ambiguous"].sum()) if n else 0,
        "final_equity": float(curve.iloc[-1]),
        "long": _side_stats(trades[trades["side"] == "long"]) if n else _side_stats(empty),
        "short": _side_stats(trades[trades["side"] == "short"]) if n else _side_stats(empty),
    }
    return metrics, daily
