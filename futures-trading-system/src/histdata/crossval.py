"""Cross-source validation (Sprint 12): compare two independently sourced datasets bar by bar.

Evidence required for CROSS_SOURCE_VALIDATED (both datasets, recorded with the report hash):

1. Both datasets are VALIDATED (or better), unadjusted, per-contract, NOT synthetic.
2. They are independent: different provider adapters AND no shared source-file hash.
3. They overlap on at least ``min_common_trade_dates`` trade dates of the same contract(s).
4. Timestamps agree: the best alignment among shifts of -120..+120 minutes is zero (a non-zero best shift
   means a timezone / labelling error in one source).
5. On RTH minutes present in either source, at least ``min_rth_match_share`` are present in both.
6. On matched bars, at least ``min_ohlc_within_1_tick`` have every OHLC value within one tick.

Volume is compared and reported (vendors differ in how they count trades, e.g. spreads, implied volume),
but it does not decide the status. Every discrepancy example is listed for investigation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd

from src.histdata.calendar import Calendar
from src.histdata.store import RANK, Dataset

CRITERIA = {"min_common_trade_dates": 5, "min_rth_match_share": 0.99, "min_ohlc_within_1_tick": 0.995,
            "max_shift_minutes": 120}


def compare(a: Dataset, b: Dataset, cal: Calendar, specs, *, start: str | None = None, end: str | None = None
            ) -> dict[str, Any]:
    problems: list[str] = []
    for d in (a, b):
        if RANK[d.status] < RANK["VALIDATED"]:
            problems.append(f"{d.ref} is {d.status} (needs VALIDATED)")
        if d.manifest["synthetic"]:
            problems.append(f"{d.ref} is SYNTHETIC")
        if d.manifest["adjustment"] != "none" or d.manifest["kind"] != "per_contract":
            problems.append(f"{d.ref} is not an unadjusted per-contract dataset")
    if a.manifest["provider"]["adapter"] == b.manifest["provider"]["adapter"]:
        problems.append("both datasets come from the same provider adapter: not independent")
    shared = {s["sha256"] for s in a.manifest["source_files"]} & {s["sha256"] for s in b.manifest["source_files"]}
    if shared:
        problems.append(f"{len(shared)} identical source file(s) in both datasets: not independent")
    x, y = a.bars(), b.bars()
    if start or end:
        lo = pd.Timestamp(start or "1900-01-01", tz="UTC")
        hi = pd.Timestamp(end or "2262-01-01", tz="UTC") + pd.Timedelta(days=1)
        x = x[(x["ts_utc"] >= lo) & (x["ts_utc"] < hi)]
        y = y[(y["ts_utc"] >= lo) & (y["ts_utc"] < hi)]
    common_contracts = sorted(set(x["contract"]) & set(y["contract"]))
    contract_only = {"only_in_a": sorted(set(x["contract"]) - set(y["contract"])),
                     "only_in_b": sorted(set(y["contract"]) - set(x["contract"]))}
    x = x[x["contract"].isin(common_contracts)]
    y = y[y["contract"].isin(common_contracts)]
    # timestamp alignment
    shift = _best_shift(x, y)
    if shift != 0:
        problems.append(f"timestamps disagree: the best alignment shifts one source by {shift} minute(s) "
                        "(timezone / bar-labelling error)")
    m = x.merge(y, on=["contract", "ts_utc"], how="outer", suffixes=("_a", "_b"), indicator=True)
    rth = cal.is_rth(m["ts_utc"]).to_numpy()
    both = m["_merge"] == "both"
    rth_union = int(rth.sum())
    rth_both = int((rth & both.to_numpy()).sum())
    match_share = rth_both / rth_union if rth_union else None
    tdates = cal.trade_dates(m.loc[both, "ts_utc"]).dropna().nunique() if both.any() else 0
    mm = m[both].copy()
    tick = mm["contract"].str[:-3].map(lambda r: specs[r].tick_size)
    within = np.ones(len(mm), dtype=bool)
    diffs = {}
    for c in ("open", "high", "low", "close"):
        d = (mm[f"{c}_a"] - mm[f"{c}_b"]).abs() / tick
        diffs[c] = {"max_ticks": float(d.max()) if len(d) else None, "mean_ticks": float(d.mean()) if len(d) else None}
        within &= (d <= 1 + 1e-9).to_numpy()
    ohlc_share = float(within.mean()) if len(mm) else None
    va, vb = mm["volume_a"], mm["volume_b"]
    rel = ((va - vb).abs() / np.maximum(np.maximum(va, vb), 1.0)) if len(mm) else pd.Series(dtype=float)
    examples = mm.loc[~within, ["contract", "ts_utc"] + [f"{c}_{s}" for c in ("open", "high", "low", "close")
                                                           for s in "ab"]].head(20)
    if tdates < CRITERIA["min_common_trade_dates"]:
        problems.append(f"only {tdates} common trade date(s) (needs {CRITERIA['min_common_trade_dates']})")
    if match_share is None or match_share < CRITERIA["min_rth_match_share"]:
        problems.append(f"RTH bar match share {match_share} below {CRITERIA['min_rth_match_share']}")
    if ohlc_share is None or ohlc_share < CRITERIA["min_ohlc_within_1_tick"]:
        problems.append(f"OHLC within one tick on {ohlc_share} of matched bars (needs {CRITERIA['min_ohlc_within_1_tick']})")
    report = {
        "a": a.identity(), "b": b.identity(), "period": {"start": start, "end": end}, "criteria": CRITERIA,
        "contracts": {"common": common_contracts, **contract_only},
        "best_timestamp_shift_minutes": shift, "common_trade_dates": int(tdates),
        "bars": {"matched": int(both.sum()), "only_in_a": int((m["_merge"] == "left_only").sum()),
                 "only_in_b": int((m["_merge"] == "right_only").sum())},
        "rth": {"union": rth_union, "matched": rth_both, "match_share": match_share},
        "ohlc": {"within_1_tick_share": ohlc_share, "per_field": diffs},
        "volume": {"median_relative_difference": float(rel.median()) if len(rel) else None,
                   "p95_relative_difference": float(rel.quantile(0.95)) if len(rel) else None,
                   "note": "reported, not a criterion (vendor counting conventions differ)"},
        "discrepancy_examples": json.loads(examples.to_json(orient="records", date_format="iso")),
        "problems": problems,
        "result": "PASS" if not problems else "FAIL",
    }
    report["report_sha256"] = hashlib.sha256(json.dumps(report, sort_keys=True, default=str).encode()).hexdigest()
    return report


def _best_shift(x: pd.DataFrame, y: pd.DataFrame) -> int:
    """Minute shift of y that best matches x's closes (0 when the clocks agree)."""
    if x.empty or y.empty:
        return 0
    if len(x) > 5000:                                     # deterministic subsample (every k-th bar)
        x = x.iloc[:: len(x) // 5000 + 1]
    xa = x.set_index(["contract", "ts_utc"])["close"]
    ya = y.set_index(["contract", "ts_utc"])["close"]
    j0 = xa.index.intersection(ya.index)
    if len(j0) and float((xa.loc[j0].to_numpy() == ya.loc[j0].to_numpy()).mean()) >= 0.9 \
            and len(j0) >= 0.5 * len(xa):
        return 0                                          # clocks clearly agree
    best, best_score = 0, -1.0
    for s in [0] + [k for k in range(-CRITERIA["max_shift_minutes"], CRITERIA["max_shift_minutes"] + 1, 1) if k]:
        yb = y.assign(ts_utc=y["ts_utc"] + pd.Timedelta(minutes=s)).set_index(["contract", "ts_utc"])["close"]
        j = xa.index.intersection(yb.index)
        if not len(j):
            continue
        score = float((xa.loc[j].to_numpy() == yb.loc[j].to_numpy()).mean()) * len(j) / max(len(xa), 1)
        if score > best_score + 1e-12:
            best, best_score = s, score
    return best


__all__ = ["compare", "CRITERIA"]
