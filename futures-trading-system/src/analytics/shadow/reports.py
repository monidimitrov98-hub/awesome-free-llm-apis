"""Sprint 8 analyses over normalised run tables. Pure functions; nothing here can change trading state.

Labelling rules applied everywhere:
* execution metrics derived from the PaperBroker / OHLC paths are SIMULATED;
* counterfactual outcomes of blocked signals are HYPOTHETICAL and never added to any P&L;
* cost scenarios are RESEARCH and kept apart from recorded results;
* a value that cannot be derived is None, with a reason where useful.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

import pandas as pd

from src.analytics.shadow.events import WALL_CLOCK_KINDS, JournalSource, Rec
from src.analytics.shadow.model import _num, decisions, identity_fields, session_date, signals, trades
from src.analytics.shadow.stats import mean, sample_level, summary, trade_stats

SIMULATED = "SIMULATED (PaperBroker fills; not exchange executions)"
HYPOTHETICAL = "HYPOTHETICAL (never traded; not part of any P&L)"


# ---------------------------------------------------------------------------------------------- sessions
def session_records(src: JournalSource, tr: list[dict], cache: dict) -> list[dict[str, Any]]:
    run = src.data
    keys: dict[tuple[str, str], dict[str, Any]] = {}

    def rec(day: str | None, root: str | None, seq: int) -> dict[str, Any] | None:
        if not day or not root:
            return None
        k = (day, root)
        if k not in keys:
            keys[k] = {"session_id": f"{run.run_id}:{day}:{root}", "run_id": run.run_id, "run": run.label,
                       "trading_date": day, "instrument": root, "contract": None, **identity_fields(run, seq),
                       "first_seq": seq, "last_seq": seq, "start": None, "end": None, "signals": 0,
                       "authorized_entries": 0, "rejected_signals": 0, "blocked_at_fill": 0, "simulated_fills": 0,
                       "completed_trades": 0, "cancelled_orders": 0, "expired_orders": 0, "rejected_orders": 0,
                       "net_pnl": 0.0, "gross_pnl": 0.0, "costs": 0.0, "pnl_series": [], "market_events": 0,
                       "data_gaps": 0, "data_rejected": 0, "news_blocks": 0, "account_guard_blocks": 0,
                       "account_guard_events": 0, "status": "complete"}
        r = keys[k]
        r["first_seq"], r["last_seq"] = min(r["first_seq"], seq), max(r["last_seq"], seq)
        return r

    for s in signals(src):
        r = rec(s["day"], s["root"], s["journal_seq"])
        if r:
            r["signals"] += 1
    for d in decisions(src):
        r = rec(d["day"], d["root"], d["journal_seq"])
        if not r:
            continue
        if d["allowed"]:
            r["authorized_entries"] += 1
        else:
            r["rejected_signals"] += 1
            r["news_blocks"] += d["reason"].startswith("news_")
            r["account_guard_blocks"] += d["reason"].startswith("account_")
    for f in run.fill_auth:
        if not f.payload.get("allowed"):
            r = rec(session_date(f.ts), f.payload.get("root"), f.seq)
            if r:
                r["blocked_at_fill"] += 1
    for e in run.executions:
        r = rec(session_date(e.ts), e.payload.get("root"), e.seq)
        if r:
            r["simulated_fills"] += 1
    for o in run.orders.values():
        det = o.get("details") or {}
        root = det.get("root")
        for ev in o["events"]:
            if ev["kind"] in ("cancelled", "expired", "rejected"):
                r = rec(session_date(ev["ts"]), root, ev["seq"])
                if r:
                    r[f"{ev['kind']}_orders"] += 1
    for t in tr:
        r = rec(t["day"], t["root"], t["journal_seq"])
        if r:
            r["completed_trades"] += 1
            r["net_pnl"] += t["net_pnl"]
            r["gross_pnl"] += t["gross_pnl"]
            r["costs"] += t["commission"] + t["slippage_cost"]
            r["pnl_series"].append(t["net_pnl"])
    for root, m in run.market.items():
        for day, n in m.per_day.items():
            r = rec(day, root, run.identities[0].seq if run.identities else 0)
            if r:
                r["market_events"] += n
                r["start"], r["end"] = m.day_span[day]
                cs = m.day_contracts.get(day)
                r["contract"] = " / ".join(sorted(cs)) if cs else None  # None = not reported by the feed (e.g. bar replay)
        for a, b in m.gaps:
            sa, sb = session_date(a), session_date(b)
            if sa is not None and sa == sb:
                r = keys.get((sa, root))
                if r:
                    r["data_gaps"] += 1
    for (day, reason), n in run.data_rejected.items():
        for r in keys.values():
            if r["trading_date"] == day:
                r["data_rejected"] += n
    for p in run.protection:
        if p.kind == "account_guard":
            for r in keys.values():
                if r["trading_date"] == session_date(p.ts):
                    r["account_guard_events"] += 1
    interrupted = {session_date(f.ts) for f in run.feed if f.kind in ("system_error",)
                   or (f.kind == "shutdown" and f.payload.get("reason") in ("system_error", "feed_unavailable"))}
    last_day = session_date(run.last_ts) if run.last_ts is not None else None
    for r in keys.values():
        from src.analytics.shadow.stats import max_drawdown

        r["session_drawdown"] = max_drawdown(r.pop("pnl_series"))
        r["session_drawdown_basis"] = "closed-trade sequence (intra-trade excursions not included)"
        if r["trading_date"] in interrupted:
            r["status"] = "interrupted"
        elif r["trading_date"] == last_day:
            r["status"] = "in progress or last recorded session"
        r["news_readiness"] = (run.created.get("news_readiness") or {}).get("verdict") or \
            (run.created.get("news_readiness") or {}).get("mode")
        r["market_data_quality"] = ("no market events recorded" if r["market_events"] == 0 else
                                    "gaps detected" if r["data_gaps"] else "no in-session gaps detected")
        r["account_guard_status"] = "events recorded" if r["account_guard_events"] else "no AccountGuard events"
    return sorted(keys.values(), key=lambda r: (r["trading_date"], r["instrument"]))


# ---------------------------------------------------------------------------------------------- execution
def execution_quality(src: JournalSource) -> dict[str, Any]:
    run = src.data
    rows = []
    sig_by_id = {s.payload.get("signal_id"): s for s in run.signals if s.payload.get("signal_id")}
    dec_by_id = {d.payload.get("signal_id"): d for d in run.decisions if d.payload.get("signal_id")}
    by_order: dict[str, list] = defaultdict(list)
    for e in run.executions:
        by_order[e.payload.get("order_id")].append(e)
    for oid, o in run.orders.items():
        det = o.get("details") or {}
        execs = by_order.get(oid, [])
        role = det.get("role")
        q_sub = o.get("quote_at_submit") or {}
        row = {"order_id": oid, "run_id": run.run_id, "journal_seq": o.get("submitted_seq"), "root": det.get("root"),
               "role": role, "type": det.get("order_type"), "side": "BUY" if det.get("is_buy") else "SELL",
               "quantity": det.get("quantity"), "filled": sum(e.payload.get("quantity", 0) for e in execs),
               "executions": len(execs), "partial": len(execs) > 1 or (execs and sum(e.payload.get("quantity", 0) for e in execs) < (det.get("quantity") or 0)),
               "final_state": o["events"][-1]["kind"] if o["events"] else None,
               "final_reason": o["events"][-1].get("reason") if o["events"] else None,
               "day": session_date(o.get("submitted_ts")), **identity_fields(run, o.get("submitted_seq") or 0),
               "label": SIMULATED}
        sub_ts = o.get("submitted_ts")
        if role == "entry":
            sid = det.get("client_id")
            d = dec_by_id.get(sid)
            s = sig_by_id.get(sid)
            row["signal_to_order_s"] = ((sub_ts - d.ts).total_seconds() if d and sub_ts is not None else None)
            row["expected_price"] = _num(det.get("reference_price"))
            row["spread_at_signal"] = _spread((s.quote if s else None) or (d.quote if d else None))
        row["spread_at_order"] = _spread(q_sub)
        if execs:
            e0 = execs[0]
            p = e0.payload
            row["fill_time"] = p.get("ts")
            row["order_to_fill_s"] = (pd.Timestamp(p["ts"]) - sub_ts).total_seconds() if sub_ts is not None else None
            row["fill_price"] = _num(p.get("fill_price"))
            row["fill_reference"] = _num(p.get("reference_price"))
            row["requested_price"] = _num(p.get("requested_price"))
            row["fill_reason"] = p.get("reason")
            row["quote_based"] = p.get("quote_based")
            row["synthetic_data"] = p.get("synthetic_data")
            row["slippage_ticks_model"] = p.get("slippage_ticks")
            row["slippage_cost"] = sum(_num(e.payload.get("slippage_cost")) or 0.0 for e in execs)
            row["spread_at_fill"] = _spread(e0.quote)
            tick = _tick(run, row["root"])
            if row.get("expected_price") is not None and row["fill_price"] is not None and tick:
                adverse = (row["fill_price"] - row["expected_price"]) * (1 if det.get("is_buy") else -1)
                row["fill_vs_expected_ticks"] = adverse / tick
            if row["requested_price"] is not None and row["fill_price"] is not None and tick and role in ("stop_loss",):
                row["stop_slippage_ticks"] = (row["requested_price"] - row["fill_price"]) * (1 if not det.get("is_buy") else -1) / tick
        rows.append(row)
    count = Counter(r["final_state"] for r in rows)
    entries = [r for r in rows if r["role"] == "entry"]
    stops = [r for r in rows if r["role"] == "stop_loss" and r.get("fill_price") is not None]
    targets = [r for r in rows if r["role"] == "take_profit" and r.get("fill_price") is not None]
    return {
        "label": SIMULATED,
        "orders": len(rows), "by_final_state": dict(count),
        "partial_fill_orders": sum(1 for r in rows if r["partial"]),
        "entry": {"signal_to_order_s": summary([r.get("signal_to_order_s") for r in entries]),
                  "order_to_fill_s": summary([r.get("order_to_fill_s") for r in entries]),
                  "fill_vs_expected_ticks": summary([r.get("fill_vs_expected_ticks") for r in entries]),
                  "spread_at_signal": summary([r.get("spread_at_signal") for r in entries]),
                  "spread_at_fill": summary([r.get("spread_at_fill") for r in entries]),
                  "slippage_cost": summary([r.get("slippage_cost") for r in entries if r.get("fill_price") is not None])},
        "exits": {"stop_fills": len(stops), "stop_slippage_ticks": summary([r.get("stop_slippage_ticks") for r in stops]),
                  "stop_gap_fills": sum(1 for r in stops if r.get("fill_reason") == "stop_gap_through"),
                  "target_fills": len(targets), "spread_at_exit": summary([r.get("spread_at_fill") for r in rows
                                                                            if r["role"] in ("stop_loss", "take_profit", "exit")])},
        "notes": _exec_notes(rows),
        "rows": rows,
    }


def _spread(q: dict | None) -> float | None:
    if not q or q.get("bid") is None or q.get("ask") is None:
        return None
    return float(q["ask"]) - float(q["bid"])


def _tick(run, root) -> float | None:
    for r in reversed(run.identities):
        c = (r.payload.get("contracts") or {}).get(root)
        if c:
            return c.get("tick_size")
    return None


def _exec_notes(rows) -> list[str]:
    notes = ["All fills are SIMULATED by the PaperBroker; they are not exchange executions and include no queue position."]
    if any(r.get("synthetic_data") for r in rows):
        notes.append("Some fills used synthetic OHLC paths (bar replay): intrabar order is assumed, spreads are UNAVAILABLE.")
    if not any(r.get("spread_at_fill") is not None for r in rows):
        notes.append("Spreads UNAVAILABLE: the data feed delivered no quotes for these orders (e.g. bar replay).")
    notes.append("Signal-to-order latency is 0 by design (orders are created while processing the decision event).")
    return notes


# ---------------------------------------------------------------------------------------------- performance
def performance(tr: list[dict], cfg: dict) -> dict[str, Any]:
    tr = sorted(tr, key=lambda t: t["exit_time"])
    nb, seed = cfg.get("bootstrap_iterations", 2000), cfg.get("bootstrap_seed", 8)

    def grp(key):
        g = defaultdict(list)
        for t in tr:
            g[str(t.get(key) or "unavailable")].append(t)
        return {k: trade_stats(v, n_boot=nb, seed=seed) for k, v in sorted(g.items())}
    return {"label": "SIMULATED results of recorded sessions", "overall": trade_stats(tr, n_boot=nb, seed=seed),
            "long": trade_stats([t for t in tr if t["side"] == "LONG"], n_boot=nb, seed=seed),
            "short": trade_stats([t for t in tr if t["side"] == "SHORT"], n_boot=nb, seed=seed),
            "by_instrument": grp("root"), "by_segment": grp("segment"), "by_weekday": grp("weekday"),
            "by_regime": grp("regime_trend"), "by_session_type": grp("session_type"),
            "exploratory_note": "Breakdowns are exploratory: subgroup intervals are not corrected for multiple "
                                "comparisons and small subgroups are not evidence of anything.",
            "caveats": ["Trades can overlap in time and MES / MNQ move together: trades are not independent. The "
                        "expectancy interval is the wider of a Student-t interval and a trading-day cluster bootstrap; "
                        "the win-rate (Wilson) interval assumes independence and is optimistic.",
                        "Looking at many breakdowns and filters is a multiple-comparisons exercise: a 'good' subgroup "
                        "is expected by chance.",
                        "Simulated fills (no queue position, modelled slippage); real fills can be worse.",
                        "Results describe the recorded period only and do not establish a future edge."],
            "trades": tr}


# ---------------------------------------------------------------------------------------------- cost scenarios
def cost_scenarios(tr: list[dict], scenarios: list[dict], ticks: dict[str, dict], account: dict | None) -> dict[str, Any]:
    """Re-price RECORDED trades under worse assumptions. Research only: never written anywhere else."""
    out = []
    base_nets = [t["net_pnl"] for t in sorted(tr, key=lambda t: t["exit_time"])]
    for sc in scenarios:
        adj = []
        for t in sorted(tr, key=lambda t: t["exit_time"]):
            c = ticks.get(t["root"]) or {}
            tick_value = (c.get("tick_size") or 0) * (c.get("point_value") or 0)
            q = t["quantity"] or 0
            extra_ticks = sc.get("extra_slippage_ticks_per_side", 0) * 2 + sc.get("extra_spread_ticks", 0)
            if t.get("exit_role") == "stop_loss":
                extra_ticks += sc.get("extra_stop_ticks", 0)
            extra = extra_ticks * tick_value * q + (sc.get("commission_multiplier", 1.0) - 1.0) * t["commission"]
            net = t["net_pnl"] - extra
            risk = (abs(t["net_pnl"] / t["r_multiple"]) if t.get("r_multiple") else None)
            adj.append({**t, "net_pnl": net, "r_multiple": (net / risk) if risk else None})
        s = trade_stats(adj, n_boot=300)
        breaches = _account_breaches([t["net_pnl"] for t in adj], [t["day"] for t in adj], account)
        out.append({"scenario": sc.get("name"), "assumptions": {k: v for k, v in sc.items() if k != "name"},
                    "net_pnl": s["net_pnl"], "expectancy": s["expectancy"], "profit_factor": s["profit_factor"],
                    "max_drawdown": s["max_drawdown"], "average_r": s["average_r"], **breaches})
    return {"label": "RESEARCH SCENARIOS (recorded trades re-priced; production configuration unchanged)",
            "baseline_net_pnl": float(sum(base_nets)), "scenarios": out,
            "method": "Each trade's recorded net P&L minus extra ticks x tick value x quantity (entry+exit slippage, "
                      "spread, extra stop slippage) and extra commission. Trade selection is unchanged, so effects "
                      "that would change WHICH trades happen (e.g. AccountGuard blocking earlier) are not modelled."}


def _account_breaches(nets: list[float], days: list[str], account: dict | None) -> dict[str, Any]:
    if not account or not account.get("enabled"):
        return {"account_limit_check": "AccountGuard disabled or profile unavailable"}
    bal = peak = account["initial_balance"]
    floor_hits = 0
    max_dd = account.get("max_drawdown")
    daily: dict[str, float] = defaultdict(float)
    min_buffer = None
    for x, d in zip(nets, days):
        bal += x
        daily[d] += x
        if account.get("drawdown_type") != "static":
            peak = max(peak, bal)
        ref = peak if account.get("drawdown_type") != "static" else account["initial_balance"]
        buf = bal - (ref - max_dd) if max_dd else None
        if buf is not None:
            min_buffer = buf if min_buffer is None else min(min_buffer, buf)
            if buf <= 0:
                floor_hits += 1
    mdl = account.get("max_daily_loss")
    return {"min_drawdown_buffer": min_buffer, "drawdown_floor_touches": floor_hits,
            "daily_loss_limit_days": (sum(1 for v in daily.values() if v <= -mdl) if mdl else None),
            "account_check_basis": "closed-trade equity only (intraday excursions and gaps make real risk larger)"}


# ---------------------------------------------------------------------------------------------- regimes
REGIME_BUCKETS = [
    ("strong_trend", "Strong trend", lambda r: str(r.get("trend")).startswith("strong")),
    ("moderate_trend", "Moderate trend", lambda r: str(r.get("trend")).startswith("moderate")),
    ("weak_trend", "Weak trend", lambda r: r.get("trend") == "weak_or_deteriorating"),
    ("range", "Range", lambda r: r.get("range") not in (None, "not_ranging", "unavailable")),
    ("high_volatility", "High volatility", lambda r: r.get("volatility") in ("high", "extreme")),
    ("vol_expansion", "Volatility expansion", lambda r: r.get("volatility_change") == "expansion"),
    ("vol_contraction", "Volatility contraction", lambda r: r.get("volatility_change") == "contraction"),
    ("unfavorable", "Unfavorable", lambda r: r.get("suitability") == "UNFAVORABLE"),
    ("no_trade", "No-trade", lambda r: r.get("suitability") == "NO_TRADE"),
    ("unavailable", "Regime unavailable / invalid", lambda r: r.get("regime_valid") is False or r.get("trend") in (None, "unavailable")),
]


def _trade_regime(t: dict) -> dict:
    if t.get("regime_source", "").startswith("UNAVAILABLE"):
        return {"trend": None, "regime_valid": None}
    return {"trend": t.get("regime_trend"), "range": t.get("regime_range"), "volatility": t.get("regime_volatility"),
            "volatility_change": t.get("regime_volatility_change"), "regime_valid": t.get("regime_valid"),
            "suitability": t.get("suitability")}


def regimes(src_list: list[JournalSource], tr: list[dict], keep=None) -> dict[str, Any]:
    obs = []
    for src in src_list:
        for r in src.data.regime:
            o = {**r.payload, **identity_fields(src.data, r.seq), "seq": r.seq, "day": session_date(r.ts),
                 "regime_trend": r.payload.get("trend")}
            if keep is None or keep(o):
                obs.append(o)
    out = []
    for key, label, pred in REGIME_BUCKETS:
        sel = [o for o in obs if pred(o)]
        # trades are bucketed by the regime record of their own signal; a trade without one is "unavailable"
        t_sel = [t for t in tr if pred(_trade_regime(t))]
        s = trade_stats(t_sel, n_boot=500)
        out.append({"bucket": key, "label": label, "observations": len(sel), "signals": len(sel), "trades": len(t_sel),
                    "win_rate": s["win_rate"], "expectancy": s["expectancy"], "net_pnl": s["net_pnl"],
                    "profit_factor": s["profit_factor"], "average_mae_r": s["average_mae_r"],
                    "average_mfe_r": s["average_mfe_r"], "max_drawdown": s["max_drawdown"], "sample": s["sample"]})
    return {"buckets": out, "observations": len(obs), "trades_without_regime_record":
            sum(1 for t in tr if str(t.get("regime_source", "")).startswith("UNAVAILABLE")),
            "note": "Observations are the regime engine's records for strategy signals (the engine evaluates the regime "
                    "when the strategy proposes an entry). Buckets overlap (a bar can be a strong trend AND high volatility). "
                    "An unavailable or invalid regime is reported as such, never inferred."}


# ---------------------------------------------------------------------------------------------- blocked signals
BLOCK_CATEGORIES = [
    ("NewsGuard", ("news_",)), ("AccountGuard", ("account_",)),
    ("RiskManager", ("max_daily_loss", "max_daily_trades", "max_position_size", "outside_trading_window",
                     "max_consecutive_losses", "daily_profit_target", "emergency_stop", "outside_session")),
    ("Data health", ("data_", "market_data_stale", "feed_disconnected", "no_market_data", "account_equity_unknown")),
    ("Market regime", ("regime_",)), ("Session cutoff", ("session_end_cutoff", "entry_cutoff")),
    ("Position limits", ("position_open", "entry_pending", "reversal_not_permitted")),
    ("Duplicate prevention", ("duplicate_signal",)),
    ("Invalid stop / sizing", ("invalid_bracket", "size_below_one_contract", "account_requires_stop_loss",
                               "invalid_order_price", "invalid_quantity", "missing_order_price")),
    ("Run mode", ("infrastructure_validation_mode",)),
    ("System state", ("shutting_down", "reconciliation_failed", "persistence_unhealthy", "restart_strategy_state_unverified")),
]
STRATEGY_CATEGORIES = [
    ("Cooldown", ("cooldown",)), ("Invalid stop / target", ("stop_", "target_invalid")),
    ("Position limits", ("position_open", "entry_pending", "max_entries_per_session")),
    ("Duplicate prevention", ("same_pullback", "one_entry_per_bar")),
    ("Session restrictions", ("outside_session", "outside_strategy_window", "incomplete_signal_bar", "incomplete_or_gapped_window")),
    ("Missing / stale data", ("missing_indicator", "context_unavailable", "context_stale", "context_incomplete",
                              "context_missing_indicator", "structure_data_invalid", "rvol_unavailable")),
]


def categorize(reason: str, table) -> str:
    for cat, prefixes in table:
        if any(reason == p or (p.endswith("_") and reason.startswith(p)) or reason.startswith(p) for p in prefixes):
            return cat
    return "Strategy conditions" if table is STRATEGY_CATEGORIES else "Other"


MAX_COUNTERFACTUALS = 2000  # most recent blocked signals evaluated (bounded CPU)


def blocked(src_list: list[JournalSource], counterfactual: bool, cache: dict, horizon_bars: int, keep=None) -> dict[str, Any]:
    """``keep(row)`` applies the report filters (instrument / dates / type / version) BEFORE aggregation."""
    keep = keep or (lambda row: True)
    authz = []
    strat: Counter = Counter()
    by_reason: Counter = Counter()
    candidates = []
    for src in src_list:
        run = src.data
        sig = {s.payload.get("signal_id"): s.payload for s in run.signals if s.payload.get("signal_id")}
        for d in decisions(src):
            if d["allowed"] or not keep(d):
                continue
            cat = categorize(d["reason"], BLOCK_CATEGORIES)
            authz.append({**{k: d[k] for k in ("run_id", "journal_seq", "root", "time", "day", "signal_id", "side",
                                               "reason", "session_type", "strategy_version")}, "category": cat})
            by_reason[(cat, d["reason"])] += 1
            sp = sig.get(d["signal_id"])
            if counterfactual and sp and sp.get("stop_price") is not None and sp.get("target_price") is not None:
                candidates.append((src, d, sp))
        for (date, root, reason), n in run.strategy_rejects.items():
            row = {"day": date, "root": root, **identity_fields(run, run.identities[-1].seq if run.identities else 0)}
            if keep(row):
                strat[(categorize(reason, STRATEGY_CATEGORIES), reason)] += n
        for f in run.fill_auth:
            row = {"day": session_date(f.ts), "root": f.payload.get("root"), **identity_fields(run, f.seq)}
            if not f.payload.get("allowed") and keep(row):
                reason = f.payload.get("reason") or ""
                by_reason[(categorize(reason, BLOCK_CATEGORIES) + " (at fill)", reason)] += 1
    hypo = []
    memo = cache.setdefault("counterfactual", {})
    for src, d, sp in candidates[-MAX_COUNTERFACTUALS:]:
        k = (str(src.path), d["journal_seq"], horizon_bars)
        h = memo.get(k)
        if h is None:
            h = _hypothetical(src, d, sp, cache, horizon_bars)
            if h["outcome"] in ("target_first", "stop_first", "ambiguous_bar"):
                memo[k] = h  # decided outcomes depend only on past bars: never recomputed
        hypo.append(h)
    cats = Counter()
    for (c, _), n in by_reason.items():
        cats[c] += n
    resolved = [h for h in hypo if h["outcome"] in ("target_first", "stop_first", "open_at_horizon")]
    return {
        "authorization_blocks": len(authz),
        "by_category": dict(cats), "by_reason": [{"category": c, "reason": r, "count": n} for (c, r), n in by_reason.most_common()],
        "strategy_condition_failures": [{"category": c, "reason": r, "count": n} for (c, r), n in strat.most_common(60)],
        "rows": authz[-2000:],
        "counterfactual": {"label": HYPOTHETICAL, "evaluated": len(hypo), "resolved": len(resolved),
                           "not_evaluated_older": max(0, len(candidates) - MAX_COUNTERFACTUALS),
                           "target_first": sum(1 for h in hypo if h["outcome"] == "target_first"),
                           "stop_first": sum(1 for h in hypo if h["outcome"] == "stop_first"),
                           "ambiguous_bar": sum(1 for h in hypo if h["outcome"] == "ambiguous_bar"),
                           "unavailable": sum(1 for h in hypo if h["outcome"] == "unavailable"),
                           "average_r_hypothetical": mean(h.get("r") for h in resolved),
                           "rows": hypo[-500:],
                           "method": "Entry assumed at the open of the first 1-minute bar after the decision; stop and "
                                     "target from the strategy's own signal; first level touched by 1-minute OHLC wins; "
                                     "a bar touching both is AMBIGUOUS (not counted as a win); no costs, no fill check. "
                                     "These entries were BLOCKED and are never added to any P&L."},
    }


def _hypothetical(src: JournalSource, d: dict, sp: dict, cache: dict, horizon: int) -> dict[str, Any]:
    from src.analytics.shadow.model import bars_window

    root = d["root"]
    t0 = pd.Timestamp(d["time"])
    bars = bars_window(src, root, t0 + pd.Timedelta(minutes=1), t0 + pd.Timedelta(minutes=horizon), cache)
    base = {"root": root, "signal_id": d["signal_id"], "reason": d["reason"], "time": t0, "label": HYPOTHETICAL}
    bars = bars[bars.index > t0.floor("min")] if len(bars) else bars
    if not len(bars):
        return {**base, "outcome": "unavailable", "why": "no journaled bars after the blocked signal"}
    entry = float(bars.iloc[0]["open"])
    stop, target = float(sp["stop_price"]), float(sp["target_price"])
    long = str(sp.get("direction")) == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return {**base, "outcome": "unavailable", "why": "hypothetical entry at or beyond the stop"}
    for ts, b in bars.iterrows():
        hit_stop = b["low"] <= stop if long else b["high"] >= stop
        hit_tgt = b["high"] >= target if long else b["low"] <= target
        if hit_stop and hit_tgt:
            return {**base, "outcome": "ambiguous_bar", "at": ts, "entry": entry}
        if hit_stop:
            return {**base, "outcome": "stop_first", "at": ts, "entry": entry, "r": -1.0}
        if hit_tgt:
            return {**base, "outcome": "target_first", "at": ts, "entry": entry, "r": abs(target - entry) / risk}
    last = float(bars.iloc[-1]["close"])
    return {**base, "outcome": "open_at_horizon", "entry": entry, "r": ((last - entry) if long else (entry - last)) / risk}


# ---------------------------------------------------------------------------------------------- data reliability
def reliability(src: JournalSource) -> dict[str, Any]:
    run = src.data
    events = sorted(run.feed, key=lambda r: r.seq)
    connected_since = None
    connected = 0.0
    disconnects = reconnects = connects = 0
    reasons: Counter = Counter()
    blocks: Counter = Counter()
    open_blocks: dict[tuple, pd.Timestamp] = {}
    degraded = 0.0
    recov: list[float] = []
    heartbeat = overload = sub_fail = provider_down = 0
    start = run.first_ts
    for r in events:
        if r.kind in WALL_CLOCK_KINDS or start is None:
            continue
        r = Rec(r.seq, max(r.ts, start), r.kind, r.payload)  # events stamped before the market clock started
        if r.kind == "feed_connected":
            connects += 1
            if connects > 1:
                reconnects += 1
            connected_since = connected_since or r.ts
        elif r.kind in ("feed_disconnected", "shutdown"):
            if connected_since is not None:
                connected += (r.ts - connected_since).total_seconds()
                connected_since = None
            if r.kind == "feed_disconnected":
                disconnects += 1
                err = str(r.payload.get("error", ""))
                reasons[err.split(":")[-1].strip()[:60] or "unknown"] += 1
                heartbeat += "heartbeat_timeout" in err
        elif r.kind == "feed_health":
            reason = str(r.payload.get("reason"))
            key = (r.payload.get("root"), reason)
            act = r.payload.get("action")
            if act in ("block", "flag"):
                blocks[reason] += 1
                overload += reason == "backpressure_overflow"
                sub_fail += reason == "not_subscribed" and act == "block"
                provider_down += reason.startswith("provider_not_connected") and act == "block"
                if act == "block":
                    open_blocks.setdefault(key, r.ts)
            elif act == "unblock" and key in open_blocks:
                dur = (r.ts - open_blocks.pop(key)).total_seconds()
                degraded += dur
                recov.append(dur)
    end = run.last_ts
    if connected_since is not None and end is not None:
        connected += (end - connected_since).total_seconds()
    total = (end - run.first_ts).total_seconds() if end is not None and run.first_ts is not None else None
    gaps_in = []
    for root, m in run.market.items():
        for a, b in m.gaps:
            if session_date(a) is not None and session_date(a) == session_date(b):
                gaps_in.append((root, a, b))
    gap_time = sum((b - a).total_seconds() for _, a, b in gaps_in)
    rejected = Counter()
    for (_, reason), n in run.data_rejected.items():
        rejected[reason] += n
    delayed = sum(1 for r in events if r.kind == "data_delayed")
    bridge = any(f.payload.get("kind") == "live" or "bridge" in str(f.payload.get("feed")) for f in events if f.kind == "feed_connected")
    unavailable = (total - connected) if total is not None else None
    avail = None
    if total and total > 0:
        avail = max(0.0, (connected - degraded - gap_time)) / total * 100
    return {
        "run_id": run.run_id, "observation_seconds": total, "connected_seconds": connected,
        "degraded_seconds": degraded + gap_time, "unavailable_seconds": unavailable,
        "data_availability_pct": avail,
        "availability_basis": "connected time minus provider/subscription blocks and in-session gaps, over the whole run "
                              "(includes closed-market hours, where no data is expected)",
        "connections": connects, "disconnections": disconnects, "reconnections": reconnects,
        "disconnect_reasons": dict(reasons), "health_blocks": dict(blocks),
        "average_recovery_seconds": mean(recov), "stale_or_gap_incidents": len(gaps_in),
        "gap_seconds_in_session": gap_time, "delayed_incidents": delayed, "invalid_events": dict(rejected),
        "heartbeat_failures": heartbeat, "queue_overload_incidents": overload, "subscription_failures": sub_fail,
        "provider_disconnect_blocks": provider_down,
        "session_interruptions": sum(1 for r in events if r.kind == "system_error"),
        "states": {
            "provider_connected": "from provider status messages (bridge feeds only)" if bridge else "not applicable (no live provider)",
            "bridge_connected": f"{connects} connection(s), {disconnects} disconnection(s)",
            "valid_events": {r: m.events for r, m in run.market.items()},
            "data_fresh": f"{len(gaps_in)} in-session gap(s) longer than the stale threshold",
            "system_operational": "errors recorded" if any(r.kind == "system_error" for r in events) else "no system errors",
        },
        "sequence_gap_detection": ("bridge event-counter gaps only (dropped inside the bridge); exchange-level packet loss "
                                   "is NOT detectable: NinjaTrader exposes no exchange sequence numbers"),
        "per_instrument": {r: {"events": m.events, "trades": m.trades, "quotes": m.quotes, "synthetic_events": m.synthetic,
                               "first": m.first, "last": m.last, "average_spread": (m.spread_sum / m.spreads_seen) if m.spreads_seen else None,
                               "gaps_recorded": len(m.gaps), "gaps_not_recorded": m.gaps_dropped}
                           for r, m in run.market.items()},
    }


# ---------------------------------------------------------------------------------------------- account survival
def account_survival(src: JournalSource, tr: list[dict], rules: dict, stress: list[dict]) -> dict[str, Any]:
    run = src.data
    ups = run.account_updates
    last = ups[-1].payload if ups else {}
    first = run.created.get("initial_balance")
    eqs = [u.payload.get("equity") for u in ups if isinstance(u.payload.get("equity"), (int, float))]
    prot = Counter(p.kind for p in run.protection)
    flat = Counter(p.payload.get("reason") for p in run.protection if p.kind == "flatten_requested")
    blocks = sum(1 for d in run.decisions if str(d.payload.get("reason", "")).startswith("account_"))
    cur = last.get("equity")
    buf = last.get("remaining_buffer")
    losses = [t["net_pnl"] for t in tr if t["net_pnl"] < 0]
    avg_loss = mean(losses)
    scen = []
    for s in stress:
        per = None
        if s.get("loss_per_trade") == "average_loss":
            per = -avg_loss if avg_loss is not None else None
        elif s.get("loss_per_trade") == "risk_per_trade":
            per = rules.get("risk_per_trade")
        elif isinstance(s.get("loss_per_trade"), (int, float)):
            per = float(s["loss_per_trade"])
        extra = (s.get("gap_ticks", 0) * rules.get("max_tick_value", 0)) + s.get("extra_cost_per_trade", 0.0)
        if per is None or buf is None:
            why = ("no drawdown buffer recorded yet (no simulated execution)" if buf is None else
                   "no losing trade recorded yet" if s.get("loss_per_trade") == "average_loss" else
                   "no configured risk per trade (position sizing is not risk-based)"
                   if s.get("loss_per_trade") == "risk_per_trade" else "no loss basis")
            scen.append({"scenario": s.get("name"), "result": "UNAVAILABLE", "why": why})
            continue
        per_total = per + extra
        n = s.get("consecutive_losses", 1)
        after = buf - n * per_total
        scen.append({"scenario": s.get("name"), "assumptions": {k: v for k, v in s.items() if k != "name"},
                     "loss_per_trade_used": per_total, "buffer_after": after,
                     "losses_to_safety_buffer": (math.floor((buf - (rules.get("safety_buffer") or 0)) / per_total)
                                                 if per_total > 0 else None),
                     "losses_to_floor": math.floor(buf / per_total) if per_total > 0 else None,
                     "breach": after <= 0})
    return {
        "label": "Account analytics of SIMULATED results; AccountGuard / RiskManager remain authoritative",
        "profile": rules.get("profile"), "rules_source": "configured account profile (config/backtest.toml -> profile file)",
        "initial_balance": first, "current_equity": cur, "high_water_mark": last.get("high_water"),
        "remaining_drawdown_buffer": buf, "drawdown_floor": last.get("drawdown_floor"),
        "max_observed_drawdown": last.get("max_drawdown"), "unrealized_pnl": last.get("unrealized"),
        "daily_realized": last.get("daily_net"), "min_equity_recorded": min(eqs) if eqs else None,
        "safety_reserve": rules.get("safety_buffer"), "flatten_buffer": rules.get("flatten_buffer"),
        "max_drawdown_rule": rules.get("max_drawdown"), "drawdown_type": rules.get("drawdown_type"),
        "risk_per_trade": rules.get("risk_per_trade"), "max_daily_loss": rules.get("max_daily_loss"),
        "max_consecutive_losses_observed": trade_stats(sorted(tr, key=lambda t: t["exit_time"]), n_boot=0)["max_consecutive_losses"] if tr else 0,
        "account_guard_blocks": blocks, "protection_events": dict(prot), "flatten_reasons": dict(flat),
        "emergency_flatten_events": prot.get("emergency_stop", 0), "guard_flags": last.get("guard"),
        "equity_basis": "last journaled account update (written at each simulated execution)",
        "stress": {"label": "STRESS SCENARIOS (illustrative; a simulated drawdown never guarantees a maximum real loss)",
                   "rows": scen},
    }


# ---------------------------------------------------------------------------------------------- live vs replay
COMPAT_KEYS = ("strategy_config_sha256", "regime_identity")


def compare(a_sigs: list[dict], a_tr: list[dict], b_sigs: list[dict], b_tr: list[dict], a_label: str, b_label: str,
            a_ident: list[dict], b_ident: list[dict]) -> dict[str, Any]:
    """Distributional comparison of two session sets of ONE instrument. Refuses when identities differ."""
    problems = []
    for k in COMPAT_KEYS:
        av = {i.get(k) for i in a_ident} - {None}
        bv = {i.get(k) for i in b_ident} - {None}
        if not av or not bv:
            problems.append(f"{k} unknown for one side (journal without run identity)")
        elif av != bv or len(av) > 1:
            problems.append(f"{k} differs: {sorted(av)} vs {sorted(bv)}")
    for k in ("contracts", "session_rules_sha256", "signal_timeframe"):
        av = {repr(i.get(k)) for i in a_ident} - {"None"}
        bv = {repr(i.get(k)) for i in b_ident} - {"None"}
        if av and bv and av != bv:
            problems.append(f"{k} differs between the two sets")
    if problems:
        return {"comparable": False, "reasons": problems,
                "note": "Not compared: observations are only compared under identical strategy, regime, contract, "
                        "timeframe and session definitions."}

    def side(sigs, trs):
        days = {s["day"] for s in sigs} | {t["day"] for t in trs}
        n_days = len(days)
        return {
            "sessions": n_days, "signals": len(sigs), "signals_per_session": (len(sigs) / n_days) if n_days else None,
            "long_share": (sum(1 for s in sigs if s["direction"] == "long") / len(sigs)) if sigs else None,
            "signal_hour_ny": dict(sorted(Counter(s["hour_ny"] for s in sigs).items())),
            "stop_distance": summary([s["stop_distance"] for s in sigs]),
            "target_distance": summary([s["target_distance"] for s in sigs]),
            "trend_strength_atr": summary([s["trend_strength_atr"] for s in sigs]),
            "rsi": summary([s["rsi"] for s in sigs]), "rvol": summary([s["rvol"] for s in sigs]),
            "vwap_relation": dict(Counter(str(s["vwap_relation"]) for s in sigs)),
            "ema_relation": dict(Counter(str(s["ema_relation"]) for s in sigs)),
            "regime_trend": dict(Counter(str(s["regime_trend"]) for s in sigs)),
            "rejection_reasons": dict(Counter(s["decision_reason"] for s in sigs if s["authorized"] is False)),
            "trades": len(trs), "entry_price": summary([t["entry_price"] for t in trs]),
            "duration_min": summary([t["duration_min"] for t in trs]), "mfe_r": summary([t["mfe_r"] for t in trs]),
            "mae_r": summary([t["mae_r"] for t in trs]), "r_multiple": summary([t["r_multiple"] for t in trs]),
            "synthetic_fill_share": (sum(1 for t in trs if t.get("synthetic_data")) / len(trs)) if trs else None,
            "sample": sample_level(len(trs)),
        }
    a, b = side(a_sigs, a_tr), side(b_sigs, b_tr)
    notes = ["Different market periods: distributions differ because markets differ, not only because of the system.",
             "Replay fills come from 1-minute OHLC paths (SIMULATED, intrabar order assumed); shadow fills from live "
             "level-1 events (SIMULATED, no queue model). Execution differences are expected.",
             "No replay trade is claimed to 'should have happened' in shadow: the inputs differ."]
    if (a.get("synthetic_fill_share") or 0) != (b.get("synthetic_fill_share") or 0):
        notes.append("Feed characteristics differ (synthetic OHLC path share differs between the sets).")
    return {"comparable": True, a_label: a, b_label: b, "notes": notes}
