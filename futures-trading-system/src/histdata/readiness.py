"""Research-readiness assessment of a dataset (Sprint 12). Read-only: it runs and promotes nothing.

Each use gets READY / READY_WITH_LIMITATIONS / NOT_READY and the exact reasons:

* indicators      -- usable bars (status INGESTED or better), not synthetic for any claim.
* backtesting     -- VALIDATED or better, unadjusted prices, real contract specs.
* walk_forward    -- backtesting + at least ``MIN_WF_TRADE_DATES`` active trade dates.
* oos_validation  -- walk_forward + CROSS_SOURCE_VALIDATED recommended (VALIDATED = with limitations) +
                     at least ``MIN_OOS_TRADE_DATES`` trade dates reserved for the holdout.
* monte_carlo     -- inherits backtesting (Monte Carlo only resamples a run's own trades).

Synthetic data is NEVER ready for any evidential use. The fixed numbers are planning minimums for this
project (stated in the report), not proof of adequacy: the statistical power of an actual study depends on
the strategy's trade frequency and effect size (see the Sprint 11 power calculation).
"""

from __future__ import annotations

from typing import Any

from src.histdata.store import RANK, Dataset

MIN_WF_TRADE_DATES = 250          # ~ one year of Globex trade dates
MIN_OOS_TRADE_DATES = 120         # ~ six months held out
BIASES = [
    "Bar data only: no bid/ask quotes, so spread and queue position are modelled (fixed slippage), not observed.",
    "Bar timing: a bar's high and low order is unknown; the engine resolves ambiguous bars pessimistically.",
    "Provider bar construction (e.g. bars built from trades with no bar when nothing traded) shapes gaps.",
    "Roll handling: indicators that carry across sessions (EMA, RSI with all_bars scope) see roll gaps in a "
    "continuous series; per-session indicators (VWAP, RTH-only RSI/ATR) reset daily.",
    "Holiday schedules are not verified in the calendar: holiday-candidate gaps are reported, not explained.",
    "Survivorship is not a concern for listed futures contracts, but contract coverage depends on what the "
    "provider delivered.",
]


def assess(ds: Dataset) -> dict[str, Any]:
    m = ds.manifest
    v = ds.validation()
    cov = v.get("coverage", {})
    active = max([c.get("active_trade_dates", 0) for c in cov.values()] or [0])
    tdates = max([c.get("trade_dates", 0) for c in cov.values()] or [0])
    missing = [f for f in v.get("findings", []) if f["code"] in ("MISSING_DATA_RTH", "MISSING_DATA_ETH",
                                                                    "HOLIDAY_CANDIDATE_UNVERIFIED")]
    base_block = []
    if m["synthetic"]:
        base_block.append("SYNTHETIC data: never evidence of market behaviour")
    if RANK[ds.status] < RANK["INGESTED"]:
        base_block.append(f"status {ds.status}")
    conflict_refusal = ds.usage_refusal()
    if conflict_refusal:
        base_block.append(conflict_refusal)
    uses: dict[str, dict[str, Any]] = {}

    def put(use: str, blocks: list[str], limits: list[str]) -> None:
        uses[use] = {"verdict": "NOT_READY" if blocks else ("READY_WITH_LIMITATIONS" if limits else "READY"),
                     "blocking": blocks, "limitations": limits}

    put("indicators", list(base_block), [] if RANK[ds.status] >= RANK["VALIDATED"] else ["not VALIDATED"])
    bt = list(base_block)
    if RANK[ds.status] < RANK["VALIDATED"]:
        bt.append(f"status {ds.status}: backtests need VALIDATED or better")
    if m["adjustment"] != "none":
        bt.append(f"{m['adjustment']}-adjusted prices: simulated fills need real contract prices")
    bt_lim = [f"{f['code']}: {f['count']} gap(s)" for f in missing]
    if m["kind"] == "continuous":
        bt_lim.append("continuous series: see the roll table for price discontinuities at rolls")
    put("backtesting", bt, bt_lim)
    wf = list(bt)
    if active < MIN_WF_TRADE_DATES:
        wf.append(f"{active} active trade dates < {MIN_WF_TRADE_DATES} planned minimum for walk-forward")
    put("walk_forward", wf, bt_lim)
    oos = list(wf)
    if tdates < MIN_WF_TRADE_DATES + MIN_OOS_TRADE_DATES:
        oos.append(f"{tdates} trade dates < {MIN_WF_TRADE_DATES + MIN_OOS_TRADE_DATES} (research + held-out OOS)")
    oos_lim = list(bt_lim)
    if ds.status != "CROSS_SOURCE_VALIDATED":
        oos_lim.append("not cross-source validated: a single provider's errors would go unnoticed")
    put("oos_validation", oos, oos_lim)
    put("monte_carlo", list(bt), bt_lim)
    dims = dimensions(ds, v, uses)
    return {
        "dimensions": dims,
        "dataset": ds.identity(), "status": ds.status, "status_history": ds.status_history,
        "conflict_state": ds.conflict_state,
        "instruments": m["roots"], "contracts": m["contracts"], "resolution": m["resolution"],
        "first_bar_utc": m["first_bar_utc"], "last_bar_utc": m["last_bar_utc"],
        "trade_dates": tdates, "active_trade_dates": active, "session_coverage": cov,
        "missing_periods": missing, "validation_gate": v.get("gate"), "calendar": m["calendar"],
        "uses": uses, "planning_minimums": {"walk_forward_trade_dates": MIN_WF_TRADE_DATES,
                                            "oos_trade_dates": MIN_OOS_TRADE_DATES},
        "known_biases": BIASES,
        "note": "Readiness describes the DATA. It runs nothing, promotes nothing and is not evidence of an edge.",
    }


_DIM_CODES = {
    "structural_validity": ("BAD_SOURCE_ROWS", "MISSING_VALUES", "OFF_GRID_TIMESTAMP", "UNKNOWN_OR_CONTINUOUS_SYMBOL",
                            "CONFLICTING_DUPLICATES", "EXACT_DUPLICATES", "OUT_OF_ORDER", "EMPTY", "SOURCE_FILE_CHANGED"),
    "ohlcv_validity": ("NON_POSITIVE_PRICE", "NEGATIVE_VOLUME", "OHLC_INCONSISTENT", "OFF_TICK_PRICE", "ZERO_VOLUME",
                       "ABNORMAL_VOLUME", "PRICE_JUMP", "CORRECTIONS"),
    "timestamp_timezone": ("BAR_WHILE_MARKET_CLOSED", "BAR_AFTER_EXPIRY", "CLOCK_MISALIGNMENT_SUSPECTED"),
    "session_calendar": ("SCHEDULED_BREAK", "EXPECTED_CLOSURE_WEEKEND", "EXPECTED_HOLIDAY",
                         "HOLIDAY_CANDIDATE_UNVERIFIED"),
    "completeness": ("MISSING_DATA_RTH", "MISSING_DATA_ETH", "NO_TRADE_INTERVAL"),
}


def dimensions(ds: Dataset, v: dict[str, Any], uses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Eight SEPARATE verdicts (PASS / PASS_WITH_LIMITATIONS / FAIL / NOT_PERFORMED), so that no single green
    light hides a limitation. ``overall_research_readiness`` is BLOCKED by any FAIL; otherwise it says which
    research uses the data supports and which it does not."""
    m = ds.manifest
    findings = v.get("findings", [])
    prov = m.get("provenance") or {}
    out: dict[str, Any] = {}

    def dim(name: str, extra_limits: list[str] | None = None, extra_details: list[str] | None = None) -> None:
        fs = [f for f in findings if f["code"] in _DIM_CODES[name]]
        errs = [f"{f['code']}: {f['count']}" for f in fs if f["severity"] == "error"]
        warns = [f"{f['code']}: {f['count']}" for f in fs if f["severity"] == "warning"] + (extra_limits or [])
        info = [f"{f['code']}: {f['count']}" for f in fs if f["severity"] == "info"] + (extra_details or [])
        out[name] = {"status": "FAIL" if errs else ("PASS_WITH_LIMITATIONS" if warns else "PASS"),
                     "errors": errs, "limitations": warns, "details": info}

    out["file_integrity"] = {
        "status": "PASS", "errors": [], "limitations": [],
        "details": ["registry record, manifest self-hash and every stored file hash verified on load"]
        + [f"{s['name']}: sha256 {s['sha256']}, {s['bytes']} bytes, {s['rows']} rows accepted, {s['rejected']} rejected"
           for s in m.get("source_files", [])]}
    dim("structural_validity",
        extra_details=[f"{s['name']}: {s.get('parse_stats')}" for s in m.get("source_files", []) if s.get("parse_stats")])
    dim("ohlcv_validity")
    tz_limits, tz_details = [], []
    tzp = prov.get("timezone") if isinstance(prov, dict) else None
    srcs = sorted({s.get("source_timezone") for s in m.get("source_files", [])} - {None})
    if srcs:
        tz_details.append(f"declared source timezone(s): {srcs}; {m.get('timestamps')}")
    if m.get("kind") == "per_contract" and m.get("source_files"):
        if not tzp:
            tz_limits.append("no timezone provenance recorded")
        elif tzp.get("basis") != "explicit_export_setting":
            tz_limits.append(f"source timezone established by {tzp.get('basis')}, not by an explicit, verified "
                             "export setting")
    clock = v.get("clock_alignment") or {}
    if clock:
        tz_details.append(f"clock check: {clock.get('declared_closed_bars')} bar(s) in closures at the declared "
                          f"clock; shifts with none: {clock.get('zero_closed_shifts_hours')}")
        if clock.get("declared_closed_bars") == 0 and not clock.get("unique"):
            tz_limits.append("the exchange schedule does not pin the clock uniquely in this sample")
    dim("timestamp_timezone", tz_limits, tz_details)
    cal = m.get("calendar", {})
    dim("session_calendar", [] if cal.get("verified_holidays") else
        ["calendar has no VERIFIED holidays: holiday sessions cannot be distinguished from missing data"],
        [f"calendar {cal.get('id')}@{cal.get('version')} ({cal.get('timezone')})"])
    g = v.get("gate") or {}
    dim("completeness", [] if ds.status != "INGESTED" else ["coverage gate not met (status INGESTED)"],
        [f"active RTH missing fraction {g.get('active_rth_missing_fraction')}, longest missing RTH gap "
         f"{g.get('longest_missing_rth_gap_minutes')} min"]
        + [f"{c}: {x.get('bars')} bars, {x.get('trade_dates')} trade dates, {x.get('open_minutes_present')}/"
           f"{x.get('open_minutes_expected')} open minutes present" for c, x in (v.get("coverage") or {}).items()])
    if ds.status == "CROSS_SOURCE_VALIDATED":
        ev = [h.get("evidence") for h in ds.status_history if h["status"] == "CROSS_SOURCE_VALIDATED"]
        out["cross_source_validation"] = {"status": "PASS", "errors": [], "limitations": [], "details": [str(ev[-1])]}
    else:
        out["cross_source_validation"] = {
            "status": "NOT_PERFORMED", "errors": [], "limitations": [
                "no independent second source has been compared: single-provider errors would go unnoticed"],
            "details": []}
    fails = [k for k, d in out.items() if d["status"] == "FAIL"]
    ready = [u for u, x in uses.items() if x["verdict"] != "NOT_READY"]
    not_ready = {u: x["blocking"] for u, x in uses.items() if x["verdict"] == "NOT_READY"}
    if fails or m.get("synthetic") or ds.usage_refusal():
        verdict = "BLOCKED"
    elif uses["walk_forward"]["verdict"] == "NOT_READY":
        verdict = "NOT_READY_FOR_STRATEGY_RESEARCH"
    elif any(d["status"] != "PASS" for d in out.values()) or len(ready) < len(uses):
        verdict = "READY_WITH_LIMITATIONS"
    else:
        verdict = "READY"
    out["overall_research_readiness"] = {
        "status": verdict, "blocking_dimensions": fails, "supported_uses": ready, "unsupported_uses": not_ready,
        "data_conflict": ds.usage_refusal(),
        "note": "describes the data only; a structurally valid dataset is not automatically suitable for research"}
    return out


__all__ = ["assess", "dimensions", "MIN_WF_TRADE_DATES", "MIN_OOS_TRADE_DATES"]
