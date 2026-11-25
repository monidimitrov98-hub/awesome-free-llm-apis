"""Reproducible shadow / replay session report (Sprint 10) -- one session type at a time.

It collects, for the selected runs:

* identity: run ids, session type, data provenance (provider, data label), strategy version and
  configuration hash, code identity, regime identity, risk-configuration hash, account profile, prop firm
  profile and rule set;
* execution assumptions: the cost model, slippage model and fill basis that produced the SIMULATED fills;
* metrics: signals (total / accepted / rejected), simulated trades, win rate, gross / net P&L, profit factor,
  average win / loss, expectancy, maximum drawdown, fees, slippage, exposure, Sharpe and Sortino (only when
  statistically meaningful), with sample-size levels and intervals;
* blocks by protection layer (AccountGuard, NewsGuard, prop firm, RiskManager, data health, ...);
* realized vs unrealized vs simulated distinction;
* data-quality warnings and a COMPLETENESS verdict that is INCOMPLETE whenever evidence is missing or
  inconsistent (it is never "complete" by default).

Simulated results describe the recorded period only; they do not predict or guarantee real performance.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

import pandas as pd

from src.analytics.shadow import reports
from src.analytics.shadow.stats import trade_stats

MIN_DAYS_FOR_RATIOS = 20
DISCLAIMER = ("SIMULATED results of recorded sessions (paper fills, modelled costs). They describe the recorded "
              "period only and do not establish or guarantee future or real-money performance.")


def _ratios(daily: list[float], capital: float | None) -> dict[str, Any]:
    n = len(daily)
    if n < MIN_DAYS_FOR_RATIOS or not capital:
        return {"sharpe": None, "sortino": None,
                "note": f"not meaningful: {n} trading day(s) with P&L (needs >= {MIN_DAYS_FOR_RATIOS}) "
                        + ("" if capital else "and a known account size")}
    r = [x / capital for x in daily]
    m = sum(r) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1))
    dn = [min(0.0, x) for x in r]
    dsd = math.sqrt(sum(x * x for x in dn) / (n - 1))
    return {"sharpe": (m / sd * math.sqrt(252)) if sd > 0 else None,
            "sortino": (m / dsd * math.sqrt(252)) if dsd > 0 else None,
            "note": f"annualised from {n} daily returns on {capital:,.0f} (sqrt(252)); zero risk-free rate; "
                    "days without trades are not included"}


def _max_concurrent(trades: list[dict]) -> int:
    ev = []
    for t in trades:
        q = int(t.get("quantity") or 0)
        ev.append((t["entry_time"], 1, q))
        ev.append((t["exit_time"], 0, -q))
    cur = peak = 0
    for _, _, q in sorted(ev):
        cur += q
        peak = max(peak, cur)
    return peak


def session_report(lib, f) -> dict[str, Any]:
    from src.analytics.shadow.validation import validation_status

    srcs = [s for s in lib.refresh() if lib._src_matches(s, f)]
    t = lib._tables(srcs)
    trades = [x for x in t["trades"] if f.match(x)]
    decisions = [x for x in t["decisions"] if f.without_regime().match(x)]
    signals = [x for x in t["signals"] if f.match(x)]
    types = sorted({x.get("session_type") for x in trades + decisions + signals} | {lib._type(s) for s in srcs})
    if f.session_type is None and len(types) > 1:
        return {"refused": f"several session types {types}: choose one (results of different session types are "
                           "never combined)", "filters": f.describe()}
    # ---------------------------------------------------------------- identity / provenance / assumptions
    runs = []
    for s in srcs:
        ids = s.data.identities
        last = ids[-1].payload if ids else {}
        runs.append({
            "run": s.label, "run_id": s.data.run_id, "session_type": lib._type(s),
            "first_event": s.data.first_ts, "last_event": s.data.last_ts, "rows_verified": s.data.rows,
            "provider": last.get("provider"), "data_label": last.get("data_label"),
            "strategy_versions": sorted({(i.payload.get("strategy") or {}).get("version") for i in ids} - {None}),
            "strategy_config_sha256": sorted({(i.payload.get("strategy") or {}).get("config_sha256") for i in ids} - {None}),
            "code": last.get("code"), "regime": last.get("regime"), "risk_sha256": last.get("risk_sha256"),
            "account_profile": last.get("account_profile"), "prop_firm": last.get("prop_firm"),
            "session_rules_sha256": last.get("session_rules_sha256"), "test_strategy": bool(last.get("test_strategy")),
            "identity_records": len(ids),
            "execution_assumptions": {"label": reports.SIMULATED, "costs": last.get("costs"),
                                      "contracts": last.get("contracts")},
            "initial_balance": s.data.created.get("initial_balance"),
        })
    # ---------------------------------------------------------------- metrics
    st = trade_stats(sorted(trades, key=lambda x: x["exit_time"]), n_boot=int(lib.cfg["statistics"].get(
        "bootstrap_iterations", 2000)), seed=int(lib.cfg["statistics"].get("bootstrap_seed", 8)))
    daily: dict[str, float] = defaultdict(float)
    for x in trades:
        daily[x["day"]] += x["net_pnl"]
    capitals = {r["initial_balance"] for r in runs if r["initial_balance"]}
    capital = capitals.pop() if len(capitals) == 1 else None
    exposure_min = sum(float(x.get("duration_min") or 0.0) for x in trades)
    observed = sum(((r["last_event"] - r["first_event"]).total_seconds() / 60.0) for r in runs
                   if r["first_event"] is not None and r["last_event"] is not None)
    accepted = sum(1 for d in decisions if d["allowed"])
    layers: Counter = Counter()
    for d in decisions:
        if not d["allowed"]:
            layers[reports.categorize(d["reason"], reports.BLOCK_CATEGORIES)] += 1
    fill_blocks: Counter = Counter()
    for s in srcs:
        for fa in s.data.fill_auth:
            if not fa.payload.get("allowed"):
                fill_blocks[reports.categorize(fa.payload.get("reason") or "", reports.BLOCK_CATEGORIES)] += 1
    unrealized_end = {}
    for s in srcs:
        last = s.data.account_updates[-1].payload if s.data.account_updates else {}
        unrealized_end[s.label] = {"unrealized": last.get("unrealized"),
                                   "positions": {k: v for k, v in (last.get("positions") or {}).items() if v}}
    metrics = {
        "signals_total": len(signals), "entry_decisions": len(decisions), "accepted": accepted,
        "rejected": len(decisions) - accepted, "simulated_trades": st["trades"], "sample": st["sample"],
        "win_rate": st["win_rate"], "win_rate_ci95": st["win_rate_ci95"],
        "gross_pnl": st["gross_pnl"], "net_pnl": st["net_pnl"], "fees": st["commissions"],
        "slippage_cost": st["slippage_cost"], "profit_factor": st["profit_factor"],
        "average_win": st["average_winner"], "average_loss": st["average_loser"], "expectancy": st["expectancy"],
        "expectancy_ci95": st["expectancy_ci95"], "max_drawdown_closed_trades": st["max_drawdown"],
        "max_consecutive_losses": st["max_consecutive_losses"], "trading_days_with_trades": len(daily),
        **_ratios([daily[d] for d in sorted(daily)], capital),
        "exposure": {"time_in_market_minutes": exposure_min,
                     "share_of_observed_time": (exposure_min / observed) if observed > 0 else None,
                     "max_concurrent_contracts": _max_concurrent(trades)},
        "results_basis": {"realized": "closed simulated trades (trade_closed records)",
                          "unrealized_at_end": unrealized_end,
                          "simulated": "every fill is a PaperBroker simulation; nothing was executed at a broker"},
    }
    blocks = {"at_signal": dict(layers.most_common()), "at_fill": dict(fill_blocks.most_common())}
    # ---------------------------------------------------------------- data quality and completeness
    rel = {s.label: reports.reliability(s) for s in srcs}
    warnings = []
    for label, r in rel.items():
        if r["stale_or_gap_incidents"]:
            warnings.append(f"{label}: {r['stale_or_gap_incidents']} in-session data gap(s)")
        if r["invalid_events"]:
            warnings.append(f"{label}: invalid events rejected {r['invalid_events']}")
        if r["disconnections"]:
            warnings.append(f"{label}: {r['disconnections']} feed disconnection(s)")
        if r["session_interruptions"]:
            warnings.append(f"{label}: {r['session_interruptions']} system error(s)")
    if any(r["data_label"] == "SYNTHETIC" for r in runs):
        warnings.append("synthetic data: results say nothing about real markets")
    val = validation_status(srcs, t, lib.errors, lib.duplicates)
    incomplete = [f"{c['check']}: {c['status']}" for c in val["checks"] if c["status"] in ("FAIL", "WARN")
                  and c["check"] in ("journal integrity", "duplicate journals", "recovery integrity",
                                     "run identity recorded", "strategy identity stable within runs",
                                     "code reproducibility", "production strategy only")]
    if lib.errors:
        incomplete.append(f"journals excluded: {sorted(lib.errors)}")
    if not srcs:
        incomplete.append("no journal in scope")
    for label, u in unrealized_end.items():
        if u["positions"]:
            incomplete.append(f"{label}: positions still open at the end (unrealized result not final)")
    if any(r["identity_records"] == 0 for r in runs):
        incomplete.append("runs without run identity (strategy / configuration cannot be traced)")
    return {
        "report": "shadow session report", "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "filters": f.describe(), "session_type": f.session_type or (types[0] if types else None),
        "disclaimer": DISCLAIMER, "runs": runs, "metrics": metrics, "blocks_by_layer": blocks,
        "data_quality": {"warnings": warnings, "reliability": rel},
        "completeness": {"status": "COMPLETE" if not incomplete else "INCOMPLETE", "reasons": incomplete,
                         "validation_overall": val["overall"]},
    }


def trace(lib, run_id: str, seq: int) -> dict[str, Any] | None:
    """Everything that explains one decision: the identity in force, the market events right before it, the
    strategy signal and regime record, and what followed (fill authorization, order, execution, trade)."""
    import json

    from src.analytics.shadow.model import identity_fields

    src = next((s for s in lib.refresh() if s.data.run_id == run_id), None)
    if src is None or not 0 < seq <= src.data.rows:
        return None
    run = src.data
    dec = next((d for d in run.decisions if d.seq == seq), None)
    if dec is None:
        return None
    p = dec.payload
    sid, root = p.get("signal_id"), p.get("root")
    ident = run.identity_at(seq) or {}
    with src._lock:  # rows up to the verified sequence only
        from src.analytics.shadow.events import ReadOnlyJournal

        j = ReadOnlyJournal(src.path)
        try:
            rows = j.db.execute("SELECT seq, ts, payload FROM journal WHERE kind = 'market_event' AND seq < ? "
                                "ORDER BY seq DESC LIMIT 200", (seq,)).fetchall()
        finally:
            j.close()
    market = [{"seq": s_, "time": ts, **{k: v for k, v in json.loads(pl).items()
                                         if k in ("root", "kind", "last", "bid", "ask", "event_id", "synthetic")}}
              for s_, ts, pl in rows if json.loads(pl).get("root") == root][:3]
    order = next((o for o in run.orders.values() if (o.get("details") or {}).get("client_id") == sid), None) if sid else None
    oid = order["order_id"] if order else None
    return {
        "run_id": run_id, "run": src.label, "decision_seq": seq, "time": dec.ts, "root": root, "signal_id": sid,
        "allowed": p.get("allowed"), "reason": p.get("reason"), "stages": p.get("stages"),
        "identity": {**identity_fields(run, seq), "risk_sha256": ident.get("risk_sha256"),
                     "prop_firm": ident.get("prop_firm"), "code": ident.get("code"),
                     "session_rules_sha256": ident.get("session_rules_sha256")},
        "market_events_before": market,
        "strategy_signal": next((s.payload for s in run.signals if s.payload.get("signal_id") == sid), None),
        "regime_record": next((r.payload for r in run.regime if r.payload.get("signal_id") == sid), None),
        "order": ({"order_id": oid, "events": order["events"]} if order else None),
        "fill_authorizations": [fa.payload for fa in run.fill_auth if oid and fa.payload.get("order_id") == oid],
        "executions": [e.payload for e in run.executions if oid and e.payload.get("order_id") == oid],
        "trade": next((tr.payload for tr in run.trades if sid and tr.payload.get("signal_id") == sid), None),
    }
