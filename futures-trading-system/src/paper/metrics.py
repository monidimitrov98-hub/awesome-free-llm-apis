"""Paper trading reports. Run kinds are never mixed: infrastructure validation produces no
trade statistics, and rejected opportunities are counted separately from executed trades.
Nothing here is evidence of profitability; replay and simulated results use historical or
synthetic data and approximated intrabar paths."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

import pandas as pd

DISCLAIMER = {
    "infrastructure_validation": "Infrastructure validation: no entries were authorized; no trading statistics.",
    "replay": "Historical replay (bar data converted to approximate trade paths). Not paper trading; "
              "not evidence of profitability.",
    "simulated_realtime": "Simulated real-time replay of historical data. Not paper trading.",
    "paper": "Paper trading on live market data (simulated fills). Not evidence of future results.",
}


def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    net = pd.Series([t["net_pnl"] for t in trades], dtype=float)
    wins, losses = net[net > 0], net[net <= 0]
    r = pd.Series([t.get("r_multiple") for t in trades], dtype=float).dropna()
    gross_win, gross_loss = wins.sum(), -losses.sum()
    cum = net.cumsum()
    dd = float((cum.cummax().clip(lower=0) - cum).max()) if len(cum) else 0.0
    return {
        "trades": int(len(net)), "win_rate": float(len(wins) / len(net)),
        "average_win": float(wins.mean()) if len(wins) else None,
        "average_loss": float(losses.mean()) if len(losses) else None,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else None),
        "net_expectancy": float(net.mean()), "average_r_multiple": float(r.mean()) if len(r) else None,
        "gross_pnl": float(sum(t["gross_pnl"] for t in trades)), "net_pnl": float(net.sum()),
        "commissions": float(sum(t["commission"] for t in trades)),
        "slippage_cost": float(sum(t["slippage_cost"] for t in trades)),
        "max_drawdown_of_trade_sequence": dd, "sample_warning": len(net) < 30,
    }


def _segment(iso: str, tz: str) -> str:
    t = pd.Timestamp(iso).tz_convert(tz)
    minutes = t.hour * 60 + t.minute
    return "RTH" if 9 * 60 + 30 <= minutes < 16 * 60 else "ETH"


def paper_report(session) -> dict[str, Any]:
    kind = session.run_kind
    dec = session.decisions
    rejected = [d for d in dec if not d["allowed"]]
    entries = [o for o in session.broker.orders.values() if o.role == "entry"]
    filled = [o for o in entries if o.filled_qty > 0]
    report: dict[str, Any] = {
        "run_kind": kind, "disclaimer": DISCLAIMER[kind],
        "signals": {"strategy_signals": session.signals_seen, "entry_decisions": len(dec),
                    "accepted": len(dec) - len(rejected), "rejected": len(rejected),
                    "rejection_reasons": dict(Counter(d["reason"] for d in rejected))},
        "orders": {"entry_orders": len(entries), "entry_orders_filled": len(filled),
                   "fill_rate": (len(filled) / len(entries)) if entries else None,
                   "by_state": dict(Counter(o.state.value for o in session.broker.orders.values()))},
        "account": {k: v for k, v in session.account.summary().items()},
    }
    execs = [e["payload"] for e in session.journal.entries("execution")]
    synth = sum(1 for e in execs if e.get("synthetic_data"))
    report["execution_basis"] = {
        "executions": len(execs), "from_synthetic_bar_paths": synth,
        "quote_based": sum(1 for e in execs if e.get("quote_based")),
        "note": ("fills on synthetic paths built from 1-minute bars: the intrabar order is assumed; a stop and a "
                 "target inside one bar are resolved stop-first (worst case)") if synth else "",
    }
    open_pos = {r: q for r, q in report["account"]["positions"].items() if q}
    report["open_positions_excluded_from_trade_stats"] = open_pos
    if kind == "infrastructure_validation":
        report["trading"] = None
        return report
    trades = session.account.closed_trades
    tz = session.session.timezone
    report["trading"] = {
        "overall": _stats(trades),
        "max_drawdown_account": session.account.max_drawdown,
        "by_instrument": {r: _stats([t for t in trades if t["root"] == r]) for r in sorted({t["root"] for t in trades})},
        "by_regime": {k: _stats([t for t in trades if str(t.get("ctx_trend")) == k])
                      for k in sorted({str(t.get("ctx_trend")) for t in trades})},
        "by_session_segment": {k: _stats([t for t in trades if _segment(t["entry_time"], tz) == k])
                               for k in sorted({_segment(t["entry_time"], tz) for t in trades})},
        "by_session_date": {k: _stats([t for t in trades if t.get("session_date") == k])
                            for k in sorted({str(t.get("session_date")) for t in trades})},
    }
    return report
