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
    return {
        "dataset": ds.identity(), "status": ds.status, "status_history": ds.status_history,
        "instruments": m["roots"], "contracts": m["contracts"], "resolution": m["resolution"],
        "first_bar_utc": m["first_bar_utc"], "last_bar_utc": m["last_bar_utc"],
        "trade_dates": tdates, "active_trade_dates": active, "session_coverage": cov,
        "missing_periods": missing, "validation_gate": v.get("gate"), "calendar": m["calendar"],
        "uses": uses, "planning_minimums": {"walk_forward_trade_dates": MIN_WF_TRADE_DATES,
                                            "oos_trade_dates": MIN_OOS_TRADE_DATES},
        "known_biases": BIASES,
        "note": "Readiness describes the DATA. It runs nothing, promotes nothing and is not evidence of an edge.",
    }


__all__ = ["assess", "MIN_WF_TRADE_DATES", "MIN_OOS_TRADE_DATES"]
