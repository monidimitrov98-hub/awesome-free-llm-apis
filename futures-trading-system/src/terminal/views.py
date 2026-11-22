"""Read-only views of a running PaperSession, as plain JSON-compatible data.

Every function here READS state (call it while holding ``session.state_lock``); none changes the
session. Values come from the backend's own objects (account, guards, RiskManager, NewsGuard,
strategy diagnostics, regime engine, broker orders, metrics). Unknown values are ``None`` --
never estimated -- and the GUI shows them as UNAVAILABLE.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.data.resample import resample_ohlcv
from src.indicators.compute import add_indicators
from src.paper.metrics import _segment, _stats

TIMEFRAMES = ("1min", "5min", "15min")
SAMPLE_WARNING_TRADES = 30


# ---------------------------------------------------------------------------------------------- utils
def clean(v: Any) -> Any:
    """JSON-safe: NaN/inf -> None, numpy/pandas scalars -> Python, timestamps -> ISO UTC."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else (v.tz_convert("UTC") if v.tzinfo else v).isoformat()
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [clean(x) for x in v]
    if hasattr(v, "value") and isinstance(getattr(v, "value"), str):  # enums
        return v.value
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _inner(session, root: str):
    return session.inner.get(root)


def _gate(session, root: str):
    s = session.strategies.get(root)
    return s if s is not None and hasattr(s, "dashboard") else None


# ---------------------------------------------------------------------------------------------- market
def market(session, contracts: dict[str, str]) -> dict[str, Any]:
    st = session.status()  # Sprint 6 status snapshot (health, quotes, latency, validation, feed)
    now = session.now()
    out = {}
    for r, v in st["instruments"].items():
        sd = session.session_of(now) if now.value > 0 else None
        allowed, reason = session.system_state(r, now)
        rc = getattr(getattr(session.feed, "server", None), "resolver", None)
        expiry = None
        if rc is not None and r in rc.by_root:
            expiry = str(rc.by_root[r].last_trade_date)
        out[r] = {**v, "contract": contracts.get(r), "contract_expiry": expiry,
                  "session_date": str(sd.date()) if sd is not None else None,
                  "entries_allowed": allowed, "entry_block_reason": "" if allowed else reason}
    return {"time": clean(now), "instruments": out, "feed": st["feed"], "latency": st["latency"],
            "validation": st["validation"], "market_session": st["market_session"]}


# ---------------------------------------------------------------------------------------------- account / risk
def account(session) -> dict[str, Any]:
    a = session.account
    eq = a.equity()
    unreal = a.unrealized()
    sd = session.account_session
    day_key = str(sd.date()) if sd is not None else None
    trades = a.closed_trades
    stats = _stats(trades)
    return clean({
        "initial_balance": a.initial_balance, "equity": eq, "known_equity": a.known_equity(),
        "equity_complete": math.isfinite(eq), "realized_gross": a.realized_gross, "commissions": a.commissions,
        "net_realized": a.net_realized, "unrealized": unreal, "slippage": a.slippage,
        "total_costs": a.commissions + a.slippage, "high_water": a.high_water, "max_drawdown": a.max_drawdown,
        "current_drawdown": (a.high_water - eq) if math.isfinite(eq) else None,
        "session_date": day_key, "session_realized_net": a.daily_net.get(day_key) if day_key else None,
        "session_pnl": ((a.daily_net.get(day_key, 0.0) + unreal) if day_key and math.isfinite(unreal) else None),
        "positions": {r: p.quantity for r, p in a.positions.items()},
        "trades": stats.get("trades", 0), "wins": sum(1 for t in trades if t["net_pnl"] > 0),
        "losses": sum(1 for t in trades if t["net_pnl"] <= 0), "win_rate": stats.get("win_rate"),
        "profit_factor": stats.get("profit_factor") if stats.get("trades", 0) >= 5 else None,
        "sample_warning": stats.get("trades", 0) < SAMPLE_WARNING_TRADES,
    })


def risk(session) -> dict[str, Any]:
    a, now = session.account, session.now()
    g, r = a.guard, a.risk
    rules, rc = g.rules, r.config
    eq = a.equity()
    sd = session.session_of(now) if now.value > 0 else session.account_session
    sd = sd or session.account_session
    paid = sum(t.commission for t in a.open_trades.values())
    open_pnl = (a.unrealized() - paid) if math.isfinite(a.unrealized()) else None
    day = r._days.get(pd.Timestamp(sd)) if sd is not None else None
    daily = r.daily_pnl(sd, open_pnl or 0.0) if sd is not None else None
    max_loss = rc.max_daily_loss or None
    guard_block = g.entry_block_reason(eq, sd) if g.enabled else None
    bt = session.bt
    news = {}
    for root, guard in session.news.items():
        try:
            st = guard.status(now)
            nxt = st.next_window
            ev = nxt.events[0] if nxt and nxt.events else None
            news[root] = {
                "state": st.state.value, "safe": st.safe, "reasons": st.reasons[:10],
                "active_windows": [{"start": w.start, "end": w.end, "kind": w.kind,
                                    "events": [e.name for e in w.events][:5]} for w in st.active[:3]],
                "next_event": ({"name": ev.name, "category": getattr(ev, "category", None),
                                "scheduled_at": ev.scheduled_at, "window_start": nxt.start, "window_end": nxt.end,
                                "minutes_until": (nxt.start - now).total_seconds() / 60 if now.value > 0 else None}
                               if ev else None),
                "calendar_age_minutes": st.calendar_age_minutes,
                "provider_health": {k: {kk: vv for kk, vv in v.items() if kk in ("data_class", "status", "ok", "age_minutes",
                                                                                   "last_success", "error")}
                                    for k, v in list(st.provider_health.items())[:10]},
                "operational": guard.operational, "mode": guard.cfg.mode if guard.cfg.enabled else "disabled",
            }
        except Exception as exc:  # noqa: BLE001 -- a broken calendar is shown, never hidden
            news[root] = {"state": "ERROR", "safe": False, "reasons": [f"{type(exc).__name__}"]}
    entries = {}
    for root in session.roots:
        ok, why = session.system_state(root, now)
        entries[root] = {"data_and_system_ok": ok, "reason": why}
    return clean({
        "time": now,
        "account_guard": {
            "enabled": g.enabled, "profile": getattr(rules, "profile", None), "drawdown_type": rules.drawdown_type,
            "account_size": a.initial_balance, "max_drawdown": rules.max_drawdown, "safety_buffer": rules.safety_buffer,
            "flatten_buffer": rules.flatten_buffer, "gap_reserve_ticks": rules.gap_reserve_ticks,
            "lock_after_flatten": rules.lock_after_flatten, "peak": g.peak if g.enabled else None,
            "floor": g.floor if g.enabled else None, "equity": eq,
            "remaining_buffer": g.remaining_buffer(eq) if g.enabled and math.isfinite(eq) else None,
            "high_water": a.high_water, "current_drawdown": (a.high_water - eq) if math.isfinite(eq) else None,
            "breached": g.breached, "locked_permanently": g.locked_permanently,
            "locked_session": g.locked_session, "entry_block_reason": guard_block,
            "state": ("DISABLED" if not g.enabled else "BREACHED" if g.breached else
                      "LOCKED" if g.locked_permanently or (sd is not None and g.is_locked(sd)) else
                      "BLOCKING" if guard_block else "OK"),
            "events": g.events[-20:],
        },
        "risk_manager": {
            "risk_per_trade": bt.sizing.risk_per_trade if bt.sizing.method == "risk" else None,
            "sizing_method": bt.sizing.method, "max_daily_loss": max_loss, "max_daily_trades": rc.max_daily_trades or None,
            "max_position_size": rc.max_position_size or None,
            "max_consecutive_losses": rc.max_consecutive_losses or None,
            "daily_loss_basis": rc.daily_loss_basis, "daily_loss_action": rc.daily_loss_action,
            "session_date": sd, "daily_pnl": daily, "daily_realized": day.realized_pnl if day else 0.0,
            "daily_trades": day.trades if day else 0, "daily_loss_breached": bool(day and day.loss_limit_breached),
            "daily_loss_used_pct": (max(0.0, -daily) / max_loss * 100 if max_loss and daily is not None else None),
            "daily_loss_remaining": (max_loss + daily if max_loss and daily is not None else None),
            "consecutive_losses": r.consecutive_losses(sd) if sd is not None else r._consecutive_losses,
            "emergency_stop": r.emergency_active, "emergency_reason": r.emergency_reason,
            "exposure": {root: p.quantity for root, p in a.positions.items()},
        },
        "news_guard": {"readiness": session.news_readiness, "instruments": news},
        "entries": {"accepting_entries": session.accepting_entries, "reconciliation_ok": session.reconciliation_ok,
                    "persistence_ok": session.persistence_ok, "mode": session.pcfg.mode,
                    "mode_authorizes_entries": session.pcfg.mode != "infrastructure_validation",
                    "instruments": entries},
        "timeline": [x for x in activity(session, 0, kinds=PROTECTION_KINDS)][-60:],
    })


PROTECTION_KINDS = ("account_guard", "daily_loss_limit", "emergency_stop", "flatten_requested", "feed_health",
                    "data_delayed", "price_level_reset", "feed_disconnected", "system_error")


# ---------------------------------------------------------------------------------------------- activity
def activity(session, since: int = 0, *, kinds: Iterable[str] | None = None, limit: int = 500) -> list[dict]:
    ks = set(kinds) if kinds else None
    out = [a for a in session.activity if a["seq"] > since and (ks is None or a["kind"] in ks)]
    return [clean({"seq": a["seq"], "kind": a["kind"], "time": a["time"], "summary": summarize(a),
                   "root": a["payload"].get("root"), "payload": _small(a["payload"])}) for a in out[-limit:]]


def _small(p: dict) -> dict:
    return {k: v for k, v in p.items() if k not in ("stages", "provider_health", "details") or k == "stages"}


def summarize(a: dict) -> str:
    k, p = a["kind"], a["payload"]
    if k == "entry_decision":
        return (f"{p.get('root')} {p.get('side') or ''} signal "
                + ("AUTHORIZED (paper order)" if p.get("allowed") else f"REJECTED: {p.get('reason')}"))
    if k == "execution":
        return (f"SIMULATED fill {p.get('root')} {p.get('role')} {'BUY' if p.get('is_buy') else 'SELL'} "
                f"{p.get('quantity')} @ {p.get('fill_price')}")
    if k == "trade_closed":
        return f"Paper trade closed {p.get('root')} {p.get('side')} net {round(p.get('net_pnl') or 0, 2)} ({p.get('exit_reason')})"
    if k == "feed_health":
        return f"Data health {p.get('action')}: {p.get('reason')}" + (f" ({p.get('root')})" if p.get("root") else "")
    if k == "flatten_requested":
        return f"Flatten {p.get('root')}: {p.get('reason')}"
    if k == "data_rejected":
        return f"Market event rejected: {p.get('reason')}"
    if k == "account_guard":
        return f"AccountGuard: {p.get('state')}"
    if k.startswith("order_"):
        return f"Paper order {k[6:]} {p.get('order_id')} {p.get('role')}" + (f": {p.get('reason')}" if p.get("reason") else "")
    if k == "system_error":
        return f"System error: {p.get('error')}"
    return k.replace("_", " ")


# ---------------------------------------------------------------------------------------------- bars / chart
def bars_inputs(session, root: str, tf: str) -> dict[str, Any]:
    """Everything the chart needs from the live session (call under the session lock; cheap)."""
    b = session.builders[root]
    forming = None
    if b._cur is not None and tf == "1min":
        c = b._cur
        forming = {"time": int(c.start.value // 10**9), "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                   "volume": c.volume}
    base = b.frame()  # immutable cached frame object (replaced, never mutated, when a bar completes)
    first = base.index[0] if len(base) else None
    return {"n": len(b._index), "base": base, "forming": forming, "markers": markers(session, root, first),
            "levels": levels(session, root)}


def bars_from_inputs(session, inp: dict, root: str, tf: str, limit: int, cache: dict, lock) -> dict[str, Any]:
    key = (root, tf, inp["n"])
    with lock:
        frame = cache.get(key)
    if frame is None:
        frame = pd.DataFrame() if inp["n"] == 0 else add_indicators(
            resample_ohlcv(inp["base"], tf, session.app.data.base_frequency), session.app.indicators, session.app.session)
        with lock:
            for k in [k for k in cache if k[0] == root and k[1] == tf]:
                del cache[k]
            cache[key] = frame
    cfg = session.scfg
    f = frame.iloc[-limit:] if len(frame) else frame
    cols = {"vwap": "vwap", "ema_fast": cfg.ema_fast_col, "ema_slow": cfg.ema_slow_col}
    series = {"time": [int(t.value // 10**9) for t in f.index]}
    for c in ("open", "high", "low", "close", "volume"):
        series[c] = [clean(x) for x in f[c]] if len(f) else []
    for name, col in cols.items():
        series[name] = [clean(x) for x in f[col]] if len(f) and col in f else []
    series["complete"] = [bool(x) for x in f["is_complete"]] if len(f) and "is_complete" in f else [True] * len(f)
    first = f.index[0] if len(f) else None
    mk = [m for m in inp["markers"] if first is None or m.get("time") is None or str(m.get("bar_time") or m["time"]) >= first.isoformat()[:19]]
    return clean({"root": root, "timeframe": tf, "bars": series, "forming": inp["forming"],
                  "indicator_names": {"ema_fast": cfg.ema_fast_col, "ema_slow": cfg.ema_slow_col, "vwap": "session VWAP"},
                  "markers": mk, "levels": inp["levels"]})


def markers(session, root: str, since: pd.Timestamp | None) -> list[dict]:
    """Strategy signals, authorization decisions and SIMULATED fills -- each with its own kind."""
    out = []
    inner = _inner(session, root)
    for row in (getattr(inner, "diagnostics", []) or [])[-400:]:
        if row.get("decision") != "signal":
            continue
        t = row.get("bar_time")
        if since is not None and t is not None and pd.Timestamp(t) < since:
            continue
        out.append({"kind": "signal", "time": row.get("time"), "bar_time": t, "direction": row.get("direction"),
                    "signal_id": row.get("signal_id"), "reference": row.get("close"), "stop": row.get("stop_price"),
                    "target": row.get("target_price"),
                    "reward_risk": (_num(row.get("target_distance")) / _num(row.get("stop_distance"))
                                    if _num(row.get("target_distance")) and _num(row.get("stop_distance")) else None),
                    "order_status": row.get("order_status"), "order_reason": row.get("order_reason")})
    for d in session.decisions[-400:]:
        if d["root"] != root:
            continue
        out.append({"kind": "authorized" if d["allowed"] else "rejected", "time": d["time"], "direction": d["side"],
                    "signal_id": d["signal_id"], "reason": d["reason"]})
    for o in list(session.broker.orders.values())[-600:]:
        if o.root != root or o.filled_qty <= 0:
            continue
        fill_t = next((h["time"] for h in reversed(o.history) if h["to"] in ("filled", "partially_filled")), None)
        out.append({"kind": "fill_entry" if o.role == "entry" else "fill_exit", "time": fill_t, "role": o.role,
                    "direction": ("long" if o.is_buy else "short") if o.role == "entry" else None,
                    "side": "BUY" if o.is_buy else "SELL", "price": o.avg_fill_price, "quantity": o.filled_qty,
                    "order_id": o.order_id, "simulated": True})
    return clean(sorted(out, key=lambda m: str(m.get("time"))))


def levels(session, root: str) -> list[dict]:
    out = []
    for o in session.broker.working(root):
        px = o.stop_price if o.order_type == "stop" else o.limit_price
        if px is not None:
            out.append({"role": o.role, "price": px, "order_id": o.order_id, "quantity": o.remaining,
                        "side": "BUY" if o.is_buy else "SELL"})
    return clean(out)


# ---------------------------------------------------------------------------------------------- strategy
STEP_CODES = [
    ("session", "Session / bar valid", ("outside_session", "outside_strategy_window", "incomplete_signal_bar",
                                        "incomplete_or_gapped_window", "missing_indicator")),
    ("limits", "Position / entry limits", ("position_open", "entry_pending", "cooldown", "max_entries_per_session",
                                           "same_pullback", "one_entry_per_bar")),
    ("trend", "Trend alignment (VWAP, EMA, strength, structure)", ("price_vs_vwap", "ema_alignment", "trend_too_weak",
                                                                    "structure_not_aligned", "structure_data_invalid")),
    ("context", "Higher-timeframe confirmation", ("context_",)),
    ("pullback", "Pullback detected", ("pullback_", "extended_from_vwap", "extended_from_ema")),
    ("candle", "Confirmation candle", ("no_directional_close", "no_close_beyond_prev_extreme", "no_close_beyond_ema_fast")),
    ("rsi", "RSI confirmation", ("rsi_",)),
    ("rvol", "Relative volume", ("rvol_",)),
    ("stop_target", "Stop / target valid", ("stop_", "target_invalid")),
]
AUTH_STAGE_LABELS = {"mode": "Run mode authorizes entries", "system": "Market data & system healthy",
                     "request": "Order request valid", "position": "Position / pending check", "news": "NewsGuard",
                     "sizing": "Risk sizing", "session": "Session cutoff", "account_guard": "AccountGuard",
                     "risk_manager": "RiskManager", "regime": "Market regime gate"}


def pipeline(row: dict) -> list[dict]:
    fails = [x for x in str(row.get("reasons") or "").split(";") if x]
    skip = row.get("decision") == "skip"
    steps = []
    for key, label, prefixes in STEP_CODES:
        hit = [f for f in fails if any(f == p or (p.endswith("_") and f.startswith(p)) for p in prefixes)]
        status = "fail" if hit else ("not_evaluated" if skip else "pass")
        steps.append({"step": key, "label": label, "status": status, "codes": hit})
    return steps


def strategy(session, root: str) -> dict[str, Any]:
    inner = _inner(session, root)
    rows = list(getattr(inner, "diagnostics", []) or [])
    latest = {}
    for row in reversed(rows):
        d = row.get("direction")
        if d and d not in latest:
            latest[d] = row
        if len(latest) == 2:
            break
    signals = [r for r in rows if r.get("decision") == "signal"]
    decisions = [d for d in session.decisions if d["root"] == root]
    by_signal = {d["signal_id"]: d for d in decisions if d.get("signal_id")}
    now = session.now()
    ok, why = session.system_state(root, now)
    gate = _gate(session, root)
    cfg = session.scfg
    out_latest = {}
    for d, row in latest.items():
        dec = by_signal.get(row.get("signal_id"))
        out_latest[d] = {"row": {k: v for k, v in row.items() if k != "structure_data"}, "pipeline": pipeline(row),
                         "authorization": [{"stage": s["stage"], "label": AUTH_STAGE_LABELS.get(s["stage"], s["stage"]),
                                            "ok": s["ok"], "reason": s.get("reason", "")} for s in (dec or {}).get("stages", [])]}
    rec = [{"time": r.get("time"), "bar_time": r.get("bar_time"), "direction": r.get("direction"),
            "decision": r.get("decision"), "rejection_reason": r.get("rejection_reason"), "reasons": r.get("reasons"),
            "signal_id": r.get("signal_id"), "order_status": r.get("order_status"), "order_reason": r.get("order_reason")}
           for r in rows[-60:]]
    return clean({
        "root": root, "strategy": getattr(inner, "name", type(inner).__name__),
        "status": ("ACTIVE" if session.stopped_reason is None else "STOPPED"),
        "signal_timeframe": cfg.signal_timeframe, "context_timeframe": cfg.context_timeframe,
        "regime_gate": gate is not None, "evaluations": len(rows), "signals": len(signals),
        "last_signal": ({k: v for k, v in signals[-1].items() if k != "structure_data"} if signals else None),
        "latest": out_latest, "recent": rec[::-1],
        "decisions": [{k: d.get(k) for k in ("time", "signal_id", "side", "allowed", "reason", "stages")}
                      for d in decisions[-30:]][::-1],
        "data_ok_now": ok, "data_reason_now": why,
        "config": {"ema_fast": cfg.ema_fast_col, "ema_slow": cfg.ema_slow_col, "rsi": cfg.rsi_col, "atr": cfg.atr_col},
    })


# ---------------------------------------------------------------------------------------------- regime
def analysis(session, root: str) -> dict[str, Any]:
    gate = _gate(session, root)
    if gate is None:
        return {"root": root, "available": False,
                "reason": "the market-regime gate is disabled in config/ai/market_regime.toml"}
    cur = gate.dashboard.current
    snaps = gate.snapshots[-200:]
    trades = [t for t in session.account.closed_trades if t["root"] == root]
    by_regime = {k: _stats([t for t in trades if str(t.get("ctx_trend")) == k])
                 for k in sorted({str(t.get("ctx_trend")) for t in trades})}
    duration = None
    if snaps:
        last_label = (snaps[-1].get("trend"), snaps[-1].get("range"))
        start = snaps[-1]["time"]
        for s in reversed(snaps):
            if (s.get("trend"), s.get("range")) != last_label:
                break
            start = s["time"]
        duration = (pd.Timestamp(snaps[-1]["time"]) - pd.Timestamp(start)).total_seconds() / 60
    return clean({
        "root": root, "available": cur is not None, "reason": None if cur is not None else
        "no completed signal bar has been evaluated yet", "current": cur, "errors": gate.errors,
        "enforcement": gate.cfg.enforcement, "classifier": gate.cfg.classifier,
        "regime_duration_minutes": duration, "history": snaps[-60:][::-1], "performance_by_regime": by_regime,
        "note": "Regime classification describes the current market; it is not a forecast.",
    })


# ---------------------------------------------------------------------------------------------- positions
def positions(session) -> dict[str, Any]:
    a = session.account
    out = []
    for r, p in a.positions.items():
        if p.is_flat:
            continue
        t = a.open_trades.get(r)
        mark = a.marks.get(r)
        stops = [o for o in session.broker.working(r, ("stop_loss",))]
        targets = [o for o in session.broker.working(r, ("take_profit",))]
        out.append({"root": r, "direction": "LONG" if p.quantity > 0 else "SHORT", "quantity": abs(p.quantity),
                    "entry_price": p.avg_price, "reference_price": mark,
                    "unrealized": p.unrealized_pnl(mark) if mark is not None else None,
                    "stop_loss": stops[0].stop_price if stops else None,
                    "take_profit": targets[0].limit_price if targets else None,
                    "entry_time": t.entry_time if t else None, "signal_id": t.signal_id if t else None,
                    "entry_commission": t.commission if t else None, "status": "OPEN (SIMULATED)"})
    orders = [{"order_id": o.order_id, "root": o.root, "role": o.role, "type": o.order_type,
               "side": "BUY" if o.is_buy else "SELL", "quantity": o.quantity, "filled": o.filled_qty,
               "price": o.stop_price if o.order_type == "stop" else o.limit_price, "state": o.state.value,
               "created_at": o.created_at} for o in session.broker.working()]
    closed = [{**_trade_row(i, t)} for i, t in list(enumerate(a.closed_trades, 1))[-20:]][::-1]
    return clean({"label": "PAPER / SIMULATED -- no broker connection", "positions": out, "working_orders": orders,
                  "recent_closed": closed})


# ---------------------------------------------------------------------------------------------- journal
def _trade_row(i: int, t: dict) -> dict:
    sig = str(t.get("signal_id") or "")
    return {"trade_id": f"T{i:05d}", "root": t["root"], "direction": str(t["side"]).upper(), "entry_time": t["entry_time"],
            "exit_time": t["exit_time"], "entry_price": t["entry_price"], "exit_price": t["exit_price"],
            "quantity": t["quantity"], "gross_pnl": t["gross_pnl"], "total_costs": t["commission"] + t["slippage_cost"],
            "commission": t["commission"], "slippage_cost": t["slippage_cost"], "net_pnl": t["net_pnl"],
            "r_multiple": t.get("r_multiple"), "exit_reason": t.get("exit_reason"), "exit_role": t.get("exit_role"),
            "strategy": sig.split(":")[0] if sig else None, "regime": t.get("ctx_trend"),
            "suitability": t.get("ctx_suitability"), "session_date": t.get("session_date"), "signal_id": sig or None}


JOURNAL_SORT = ("trade_id", "root", "direction", "entry_time", "exit_time", "net_pnl", "gross_pnl", "r_multiple",
                "quantity", "exit_reason", "regime")


def journal_rows(session, *, root=None, direction=None, outcome=None, start=None, end=None, strategy=None,
                 regime=None, exit_reason=None, q=None, sort="exit_time", order="desc") -> list[dict]:
    rows = [_trade_row(i, t) for i, t in enumerate(session.account.closed_trades, 1)]
    if root:
        rows = [r for r in rows if r["root"] == root]
    if direction:
        rows = [r for r in rows if r["direction"] == direction.upper()]
    if outcome == "win":
        rows = [r for r in rows if (r["net_pnl"] or 0) > 0]
    elif outcome == "loss":
        rows = [r for r in rows if (r["net_pnl"] or 0) <= 0]
    if start:
        rows = [r for r in rows if str(r["exit_time"])[:10] >= start]
    if end:
        rows = [r for r in rows if str(r["exit_time"])[:10] <= end]
    if strategy:
        rows = [r for r in rows if r["strategy"] == strategy]
    if regime:
        rows = [r for r in rows if str(r["regime"]) == regime]
    if exit_reason:
        rows = [r for r in rows if r["exit_reason"] == exit_reason]
    if q:
        ql = q.lower()
        rows = [r for r in rows if any(ql in str(v).lower() for v in r.values())]
    rows.sort(key=lambda r: (r.get(sort) is None, r.get(sort) if r.get(sort) is not None else 0), reverse=(order == "desc"))
    return rows


def journal(session, page: int, page_size: int, **filters) -> dict[str, Any]:
    rows = journal_rows(session, **filters)
    all_rows = [_trade_row(i, t) for i, t in enumerate(session.account.closed_trades, 1)]
    total = len(rows)
    page = max(1, min(page, max(1, math.ceil(total / page_size))))
    return clean({"total": total, "page": page, "page_size": page_size,
                  "rows": rows[(page - 1) * page_size: page * page_size],
                  "facets": {"root": sorted({r["root"] for r in all_rows}),
                             "strategy": sorted({r["strategy"] for r in all_rows if r["strategy"]}),
                             "regime": sorted({str(r["regime"]) for r in all_rows}),
                             "exit_reason": sorted({str(r["exit_reason"]) for r in all_rows})},
                  "label": f"PAPER / SIMULATED trades of this {session.run_kind} run"})


# ---------------------------------------------------------------------------------------------- performance
def curves(trades: list[dict], initial: float) -> dict[str, Any]:
    pts, eq, peak, dd = [], initial, initial, []
    daily: dict[str, float] = {}
    for t in sorted(trades, key=lambda t: t["exit_time"]):
        eq += t["net_pnl"]
        peak = max(peak, eq)
        ts = int(pd.Timestamp(t["exit_time"]).value // 10**9)
        pts.append({"time": ts, "equity": eq, "cumulative": eq - initial})
        dd.append({"time": ts, "drawdown": eq - peak})
        k = str(t.get("session_date") or str(t["exit_time"])[:10])
        daily[k] = daily.get(k, 0.0) + t["net_pnl"]
    return {"equity": pts, "drawdown": dd, "daily": [{"date": k, "net": v} for k, v in sorted(daily.items())]}


def performance_from_trades(trades: list[dict], initial: float, tz: str, label: str, run_kind: str) -> dict:
    s = _stats(trades)
    n = s.get("trades", 0)
    wins = [t for t in trades if t["net_pnl"] > 0]
    groups = lambda key: {k: _stats([t for t in trades if key(t) == k]) for k in sorted({key(t) for t in trades})}  # noqa: E731
    return clean({
        "source": run_kind, "label": label, "trades": n, "overall": s,
        "sample_status": ("NO TRADES" if n == 0 else "INSUFFICIENT SAMPLE SIZE" if n < SAMPLE_WARNING_TRADES
                          else "SAMPLE OK (not proof of future results)"),
        "wins": len(wins), "losses": n - len(wins),
        "long": _stats([t for t in trades if str(t["side"]).lower() == "long"]),
        "short": _stats([t for t in trades if str(t["side"]).lower() == "short"]),
        "by_instrument": groups(lambda t: t["root"]),
        "by_regime": groups(lambda t: str(t.get("ctx_trend"))),
        "by_segment": groups(lambda t: _segment(t["entry_time"], tz)),
        "by_hour": groups(lambda t: f"{pd.Timestamp(t['entry_time']).tz_convert(tz).hour:02d}:00"),
        "curves": curves(trades, initial),
    })


def performance(session) -> dict[str, Any]:
    labels = {"infrastructure_validation": "Infrastructure validation (no entries are authorized)",
              "replay": "Historical REPLAY (simulated fills)", "simulated_realtime": "Simulated real-time replay",
              "paper": "Live SHADOW simulation (simulated fills on live data)"}
    perf = performance_from_trades(session.account.closed_trades, session.account.initial_balance,
                                   session.session.timezone, labels.get(session.run_kind, session.run_kind),
                                   session.run_kind)
    perf["max_drawdown_account"] = clean(session.account.max_drawdown)
    return perf


def backtest_performance(folder) -> dict[str, Any]:
    """A finished backtest's own outputs (trades.csv / metrics.json), read-only, never merged."""
    import json
    from pathlib import Path

    if folder is None:
        return {"source": "backtest", "available": False,
                "reason": "no backtest folder configured (start the terminal with --backtest-dir output/<run>)"}
    f = Path(folder)
    tp = f / "trades.csv"
    if not tp.is_file():
        return {"source": "backtest", "available": False, "reason": "trades.csv not found in the backtest folder"}
    df = pd.read_csv(tp)
    need = {"net_pnl", "gross_pnl", "commission", "slippage_cost", "exit_time", "entry_time", "side"}
    if not need <= set(df.columns):
        return {"source": "backtest", "available": False, "reason": "unrecognised trades.csv format"}
    trades = []
    root = None
    mp = f / "manifest.json"
    if mp.is_file():
        try:
            root = json.loads(mp.read_text())["contract"]["label"][:3]
        except (KeyError, ValueError, TypeError):
            root = None
    for r in df.to_dict("records"):
        trades.append({"root": root or "?", "side": r["side"], "entry_time": str(r["entry_time"]),
                       "exit_time": str(r["exit_time"]), "net_pnl": float(r["net_pnl"]), "gross_pnl": float(r["gross_pnl"]),
                       "commission": float(r["commission"]), "slippage_cost": float(r["slippage_cost"]),
                       "r_multiple": r.get("r_multiple"), "session_date": str(r.get("session_date", ""))[:10] or None,
                       "ctx_trend": r.get("regime_at_entry") or r.get("trend_at_entry")})
    metrics = {}
    if (f / "metrics.json").is_file():
        try:
            metrics = json.loads((f / "metrics.json").read_text())
        except ValueError:
            metrics = {}
    perf = performance_from_trades(trades, float(metrics.get("initial_capital", 10_000.0) or 10_000.0),
                                   "America/New_York", "Historical BACKTEST (separate run; not combined)", "backtest")
    perf["available"] = True
    perf["backtest_metrics"] = clean(metrics)
    return perf
