"""News filter in backtests: setup from a historical calendar, and analysis outputs.

The historical calendar is a point-in-time store (versions with ``known_at`` and a refresh
log). The engine evaluates NewsGuard only with information available at each simulated
decision time. Missing historical calendar coverage is reported and, by the fail-closed
rule, blocks entries -- it is never treated as "no news".

The outcome of a blocked signal is UNKNOWN (it never traded); it is not counted as a loss
or a win. Compare with ``mode = "observe"`` (identical trades, decisions recorded) to see
what the blocked signals would have done in that run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import PROJECT_ROOT, ConfigError
from src.risk.news.blackout import Window
from src.risk.news.calendar import CalendarStore
from src.risk.news.classifier import make_classifier
from src.risk.news.config import NewsConfig
from src.risk.news.guard import NewsGuard


@dataclass(frozen=True)
class NewsSetup:
    cfg: NewsConfig
    store_text: str | None          # exact calendar JSON (embedded in manifests); None = no calendar
    calendar_path: str
    calendar_sha256: str | None

    def store(self) -> CalendarStore:
        if self.store_text is None:
            return CalendarStore()  # empty: no refresh ever recorded -> CALENDAR_STALE -> entries blocked
        return CalendarStore.from_json(self.store_text, make_classifier(self.cfg), self.calendar_path)

    def guard(self, instrument: str) -> NewsGuard:
        return NewsGuard(self.cfg, self.store(), instrument)

    def with_mode(self, mode: str) -> "NewsSetup":
        return replace(self, cfg=replace(self.cfg, mode=mode))


def load_setup(cfg: NewsConfig, calendar_path: str | Path | None) -> NewsSetup:
    path = calendar_path or cfg.backtest_calendar_path
    if not path:
        return NewsSetup(cfg, None, "", None)
    p = Path(path) if Path(path).is_absolute() else PROJECT_ROOT / path
    if not p.is_file():
        raise ConfigError(f"news calendar not found: {p}")
    text = p.read_text(encoding="utf-8")
    setup = NewsSetup(cfg, text, str(p), hashlib.sha256(text.encode("utf-8")).hexdigest())
    setup.store()  # validate now (raises ConfigError on malformed data)
    return setup


def _windows_final(guard: NewsGuard, end: pd.Timestamp) -> list[Window]:
    """Windows as known at the END of the run -- for post-hoc analysis only, never for decisions."""
    return guard.windows_at(end)


def _window_of(t: pd.Timestamp, windows: list[Window]) -> Window | None:
    return next((w for w in windows if w.start <= t < w.end), None)


def _category(w: Window | None) -> str:
    if w is None:
        return "none"
    cats = sorted({e.category for e in w.events}) or [w.kind]
    return "+".join(cats)


def news_report(guard: NewsGuard, result, signals: pd.DataFrame | None = None) -> dict[str, Any]:
    dec = guard.decisions_frame()
    trades = result.trades
    eq = result.equity
    end = eq.index[-1] if len(eq) else pd.Timestamp.now(tz="UTC")
    start = eq.index[0] if len(eq) else end
    wins = _windows_final(guard, end)
    rel = [w for w in wins if w.end >= start and w.start <= end]
    report: dict[str, Any] = {
        "mode": guard.cfg.mode if guard.cfg.enabled else "disabled",
        "instrument": guard.instrument,
        "note": ("Blocked signals never traded: their outcome is unknown and is not counted as a loss or a win. "
                 "Results on synthetic data or synthetic calendars say nothing about real markets."),
        "calendar": guard.store.coverage_report(start, end) | {"expected_providers": sorted(guard.store.expected_providers)},
        "decisions": int(len(dec)),
    }
    if len(dec):
        sig = dec[dec["purpose"] == "entry_signal"]
        blocked = sig[sig["decision"] == "BLOCK"]
        report["entry_signals_evaluated"] = int(len(sig))
        report["entry_signals_blocked"] = int(len(blocked)) if guard.cfg.enforcing else 0
        report["entry_signals_would_block"] = int(len(blocked))
        report["blocked_by_state"] = blocked["state"].value_counts().to_dict()
        fills = dec[(dec["purpose"] == "entry_fill") & (dec["decision"] == "BLOCK")]
        report["fills_blocked" if guard.cfg.enforcing else "fills_would_block"] = int(len(fills))
        cats = [_category(_window_of(t, rel)) for t in blocked["time"]]
        report["blocked_by_event_category"] = pd.Series(cats, dtype=object).value_counts().to_dict() if cats else {}
    report["trades_allowed"] = int(len(trades))
    report["flatten_events"] = [{k: str(v) for k, v in e.items()} for e in guard.flatten_events]
    # PnL and drawdown around event windows (windows as finally known; analysis only)
    if len(trades):
        t = trades.copy()
        t["event_window"] = [
            _category(_window_of(pd.Timestamp(a), rel) or _overlap(pd.Timestamp(a), pd.Timestamp(b), rel))
            for a, b in zip(t["entry_time"], t["exit_time"])]
        by = {}
        for cat, g in t.groupby("event_window"):
            pnl = g["net_pnl"].to_numpy(float)
            cum = np.cumsum(pnl)
            by[cat] = {"trades": int(len(g)), "net_pnl": float(pnl.sum()), "win_rate": float((pnl > 0).mean()),
                       "max_drawdown_of_these_trades": float((np.maximum.accumulate(np.r_[0, cum]) - np.r_[0, cum]).max()),
                       "sample_warning": len(g) < 30}
        report["trades_by_event_window"] = by
    rows = []
    for w in rel:
        seg = eq[(eq.index >= w.start) & (eq.index <= w.end)]["equity"] if len(eq) else pd.Series(dtype=float)
        if seg.empty:
            continue
        dd = float((seg.cummax() - seg).max())
        rows.append({"start": w.start.isoformat(), "end": w.end.isoformat(), "kind": w.kind, "category": _category(w),
                     "equity_change": float(seg.iloc[-1] - seg.iloc[0]), "max_drawdown": dd})
    report["equity_in_windows"] = rows
    return report


def _overlap(a: pd.Timestamp, b: pd.Timestamp, windows: list[Window]) -> Window | None:
    return next((w for w in windows if w.start < b and a < w.end), None)


def paired_comparison(off, observe, enforce) -> dict[str, Any]:
    """Same data, same strategy configuration: without the filter / observe / enforce."""
    def summ(run) -> dict[str, Any]:
        m = run.result.metrics
        return {k: m.get(k) for k in ("total_trades", "net_profit", "win_rate", "profit_factor", "max_drawdown",
                                      "expectancy")}
    out = {"without_filter": summ(off), "observe": summ(observe), "enforce": summ(enforce),
           "observe_identical_to_without_filter": bool(
               observe.result.trades.drop(columns=[], errors="ignore").equals(off.result.trades))}
    if observe.news is not None and len(enforce.result.trades) < len(off.result.trades):
        kept = set(enforce.result.trades["signal_id"].dropna())
        removed = off.result.trades[~off.result.trades["signal_id"].isin(kept)]
        out["trades_removed_by_enforce"] = {
            "count": int(len(removed)), "net_pnl_in_unfiltered_run": float(removed["net_pnl"].sum()),
            "note": "outcomes in the UNFILTERED run; path dependence means enforce mode could differ further"}
    out["warning"] = ("Do not conclude that the filter improves profitability without sufficient real "
                      "historical data and calendar coverage.")
    return out
