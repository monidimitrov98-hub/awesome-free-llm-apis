"""Normalised, traceable tables built from one run's journal data.

Every row carries ``run_id``, ``session_type`` (MOCK / REPLAY / SHADOW / MONITOR / UNKNOWN), the strategy
identity in force when its event was journaled, the session (trading) date and the journal ``seq`` of
its originating event(s). Values that cannot be derived are ``None`` (shown as UNAVAILABLE).
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

import pandas as pd

from src.analytics.shadow.events import JournalSource, Rec, RunData

UNKNOWN = "UNKNOWN"


@lru_cache(maxsize=1)
def _calendar():
    from src.config import load_config
    from src.paper.validation import SessionCalendar

    return SessionCalendar(load_config().session)


def session_date(ts: pd.Timestamp | None) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    sd = _calendar()(ts)
    return str(sd.date()) if sd is not None else None


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def identity_fields(run: RunData, seq: int) -> dict[str, Any]:
    ident = run.identity_at(seq) or {}
    strat = ident.get("strategy") or {}
    reg = ident.get("regime") or {}
    return {"session_type": ident.get("session_type", UNKNOWN) if ident else UNKNOWN,
            "data_label": ident.get("data_label"), "provider": ident.get("provider"),
            "strategy_version": strat.get("version"), "strategy_config_sha256": strat.get("config_sha256"),
            "code_sha256": (ident.get("code") or {}).get("source_sha256"),
            "regime_identity": (f"{reg.get('model_version')}@{str(reg.get('config_sha256'))[:12]}" if reg.get("enabled")
                                else ("disabled" if ident else None)),
            "identity_known": bool(ident)}


def bars_window(src: JournalSource, root: str, start: pd.Timestamp, end: pd.Timestamp, cache: dict) -> pd.DataFrame:
    key = (str(src.path), root)
    hit = cache.get(key)  # one entry per journal and root, replaced when the journal grew (bounded memory)
    if hit is None or hit[0] != src.data.rows:
        hit = cache[key] = (src.data.rows, src.bars(root))
    b = hit[1]
    if not len(b):
        return b
    return b[(b.index >= start.floor("min")) & (b.index <= end)]


def trades(src: JournalSource, cache: dict) -> list[dict[str, Any]]:
    run = src.data
    orders_by_client = {}
    for o in run.orders.values():
        det = o.get("details") or {}
        if det.get("role") == "entry" and det.get("client_id"):
            orders_by_client[det["client_id"]] = o
    regime_by_signal = {r.payload.get("signal_id"): r.payload for r in run.regime if r.payload.get("signal_id")}
    signal_by_id = {r.payload.get("signal_id"): r for r in run.signals if r.payload.get("signal_id")}
    out = []
    for i, t in enumerate(run.trades, 1):
        p = t.payload
        root = p.get("root")
        sid = p.get("signal_id")
        order = orders_by_client.get(sid) if sid else None
        det = (order or {}).get("details") or {}
        stop = _num(det.get("stop_loss"))
        target = _num(det.get("take_profit"))
        entry_ts, exit_ts = pd.Timestamp(p["entry_time"]), pd.Timestamp(p["exit_time"])
        entry_px, exit_px = _num(p.get("entry_price")), _num(p.get("exit_price"))
        risk_pts = abs(entry_px - stop) if entry_px is not None and stop is not None else None
        long = str(p.get("side")).lower() == "long"
        mfe = mae = None
        bars = bars_window(src, root, entry_ts, exit_ts, cache) if root else pd.DataFrame()
        if len(bars) and entry_px is not None:
            hi, lo = float(bars["high"].max()), float(bars["low"].min())
            mfe = (hi - entry_px) if long else (entry_px - lo)
            mae = (entry_px - lo) if long else (hi - entry_px)
            mfe, mae = max(0.0, mfe), max(0.0, mae)
        reg = regime_by_signal.get(sid) or {}
        sig = signal_by_id.get(sid)
        ambiguous = None
        if len(bars) and stop is not None and target is not None:
            last = bars.iloc[-1]
            ambiguous = bool(last["low"] <= max(stop, target) and last["high"] >= min(stop, target)
                             and last["low"] <= min(stop, target) and last["high"] >= max(stop, target))
        out.append({
            "trade_id": f"{run.run_id}:T{i:05d}", "run_id": run.run_id, "run": run.label, "journal_seq": t.seq,
            **identity_fields(run, t.seq),
            "root": root, "contract": (sig.payload.get("instrument") if sig else None) or det.get("root") or root,
            "side": "LONG" if long else "SHORT", "quantity": p.get("quantity"),
            "entry_time": entry_ts, "exit_time": exit_ts, "day": str(p.get("session_date") or session_date(entry_ts)),
            "weekday": entry_ts.tz_convert("America/New_York").day_name(),
            "segment": _segment(entry_ts), "entry_price": entry_px, "exit_price": exit_px,
            "stop_loss": stop, "take_profit": target, "risk_points": risk_pts,
            "gross_pnl": float(p["gross_pnl"]), "commission": float(p["commission"]),
            "slippage_cost": float(p["slippage_cost"]), "net_pnl": float(p["net_pnl"]),
            "r_multiple": _num(p.get("r_multiple")), "exit_reason": p.get("exit_reason"), "exit_role": p.get("exit_role"),
            "duration_min": (exit_ts - entry_ts).total_seconds() / 60,
            "mfe_points": mfe, "mae_points": mae,
            "mfe_r": (mfe / risk_pts) if mfe is not None and risk_pts else None,
            "mae_r": (mae / risk_pts) if mae is not None and risk_pts else None,
            "mfe_mae_basis": "1-minute OHLC bars from entry minute to exit time (approximation)" if mfe is not None
            else "UNAVAILABLE: no journaled bars for the trade window",
            "ohlc_ambiguous_exit_bar": ambiguous,
            # regime fields come ONLY from the regime engine's journaled record for this signal; nothing is inferred
            "regime_source": "regime engine record" if reg else "UNAVAILABLE (no regime record for this signal)",
            "regime_trend": reg.get("trend"), "regime_range": reg.get("range"),
            "regime_volatility": reg.get("volatility"), "regime_volatility_change": reg.get("volatility_change"),
            "regime_valid": reg.get("regime_valid") if reg else None, "suitability": reg.get("suitability"),
            "confidence_level": reg.get("confidence_level"),
            "strategy_context_trend": p.get("ctx_trend"), "strategy_context_suitability": p.get("ctx_suitability"),
            "signal_id": sid, "synthetic_data": None,
        })
    synth = {e.payload.get("order_id"): e.payload.get("synthetic_data") for e in run.executions}
    for tr in out:
        o = orders_by_client.get(tr["signal_id"]) if tr["signal_id"] else None
        if o is not None:
            tr["synthetic_data"] = synth.get(o["order_id"])
    return out


def _segment(ts: pd.Timestamp) -> str:
    t = ts.tz_convert("America/New_York")
    m = t.hour * 60 + t.minute
    return "RTH" if 9 * 60 + 30 <= m < 16 * 60 else "ETH"


def signals(src: JournalSource) -> list[dict[str, Any]]:
    run = src.data
    dec = {d.payload.get("signal_id"): d for d in run.decisions if d.payload.get("signal_id")}
    reg = {r.payload.get("signal_id"): r.payload for r in run.regime if r.payload.get("signal_id")}
    out = []
    for r in run.signals:
        p = r.payload
        d = dec.get(p.get("signal_id"))
        rg = reg.get(p.get("signal_id")) or {}
        q = r.quote or {}
        stop_d, tgt_d = _num(p.get("stop_distance")), _num(p.get("target_distance"))
        out.append({
            "run_id": run.run_id, "journal_seq": r.seq, **identity_fields(run, r.seq),
            "root": p.get("root") or p.get("instrument"), "signal_id": p.get("signal_id"),
            "time": pd.Timestamp(p["time"]) if p.get("time") else r.ts,
            "bar_time": p.get("bar_time"), "day": str(p.get("session_date") or session_date(r.ts))[:10],
            "direction": p.get("direction"), "close": _num(p.get("close")),
            "stop_distance": stop_d, "target_distance": tgt_d,
            "reward_risk": (tgt_d / stop_d) if stop_d and tgt_d else None,
            "vwap_relation": p.get("vwap_relation"), "ema_relation": p.get("ema_relation"),
            "trend_strength_atr": _num(p.get("trend_strength_atr")), "rsi": _num(p.get("rsi")), "rvol": _num(p.get("rvol")),
            "authorized": (d.payload.get("allowed") if d else None),
            "decision_reason": (d.payload.get("reason") if d else None),
            "regime_trend": rg.get("trend"), "regime_valid": rg.get("regime_valid"), "suitability": rg.get("suitability"),
            "spread_at_signal": ((q["ask"] - q["bid"]) if q.get("ask") is not None and q.get("bid") is not None else None),
            "hour_ny": (pd.Timestamp(p["time"]) if p.get("time") else r.ts).tz_convert("America/New_York").hour,
        })
    return out


def decisions(src: JournalSource) -> list[dict[str, Any]]:
    run = src.data
    out = []
    for d in run.decisions:
        p = d.payload
        out.append({"run_id": run.run_id, "journal_seq": d.seq, **identity_fields(run, d.seq), "root": p.get("root"),
                    "time": d.ts, "day": session_date(d.ts), "signal_id": p.get("signal_id"), "side": p.get("side"),
                    "allowed": bool(p.get("allowed")), "reason": p.get("reason") or "",
                    "stages": p.get("stages") or [], "regime": p.get("regime")})
    return out
