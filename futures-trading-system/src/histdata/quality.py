"""Normalization and validation of provider records (Sprint 12).

Nothing is ever filled, interpolated or repaired. Rows that cannot be used are EXCLUDED from the normalized
bars and every exclusion is counted in the validation report; anything that indicates a wrong price, a
wrong contract or a wrong clock is an ERROR and makes the dataset VALIDATION_FAILED.

Gap classification (per contract, consecutive bars further apart than one bar):

* ``EXPECTED_CLOSURE_WEEKEND`` / ``SCHEDULED_BREAK`` / ``EXPECTED_HOLIDAY`` -- the market was scheduled to
  be closed for the whole gap (weekly schedule, daily halt, VERIFIED calendar entries).
* ``HOLIDAY_CANDIDATE_UNVERIFIED`` -- open minutes are missing on a rule-based US holiday candidate date; it
  may be a CME holiday schedule, it is never silently treated as expected.
* ``NO_TRADE_INTERVAL`` -- short gaps (ETH <= 15 bars, RTH <= 2 bars) in data whose provider builds bars from
  trades and emits no bar without trades (e.g. Databento OHLCV). Information only.
* ``MISSING_DATA_RTH`` / ``MISSING_DATA_ETH`` -- open minutes without a bar that none of the above explains:
  missing vendor data (a warning; it counts against the VALIDATED gate on ACTIVE sessions).

Status gates (exact evidence; also written into every report):

* ``VALIDATION_FAILED``: at least one ERROR finding.
* ``VALIDATED``: no ERROR finding AND on active sessions (the contract's daily volume >= 10 % of its peak
  daily volume) the share of open RTH minutes classified MISSING_DATA_RTH is <= 0.5 % AND no single
  MISSING_DATA_RTH gap is longer than 30 minutes.
* ``INGESTED``: no ERROR finding but the coverage gate is not met (usable for exploration only).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.config import ContractSpec
from src.data.contracts import parse_contract_symbol
from src.data.errors import ContractError
from src.histdata.calendar import Calendar

GATES = {"max_rth_missing_fraction": 0.005, "max_rth_gap_minutes": 30, "active_session_volume_share": 0.10,
         "no_trade_max_bars_eth": 15, "no_trade_max_bars_rth": 2, "abnormal_volume_multiple": 50.0,
         "price_jump_fraction": 0.03}
_CONTINUOUS = ("!", "=F", ".C.", ".V.", ".N.")


class Report:
    def __init__(self) -> None:
        self.findings: list[dict[str, Any]] = []

    def add(self, severity: str, code: str, message: str, count: int = 1, examples: list | None = None) -> None:
        if count:
            self.findings.append({"severity": severity, "code": code, "message": message, "count": int(count),
                                  "examples": [str(x) for x in (examples or [])[:5]]})

    @property
    def errors(self) -> int:
        return sum(f["severity"] == "error" for f in self.findings)


def normalize(records: pd.DataFrame, specs: dict[str, ContractSpec], calendar: Calendar, *, resolution: str,
              report: Report) -> pd.DataFrame:
    """Raw records (ts_utc, symbol, OHLCV, source_ts, source_file) -> normalized per-contract bars."""
    df = records.copy().reset_index(drop=True)
    if df.empty:
        report.add("error", "EMPTY", "no usable records in the source files")
        return df.assign(root=[], contract=[])
    # contract identity: every row must name an individual, configured contract
    contracts, roots, bad = [], [], []
    cache: dict[tuple[str, int], tuple[str, str] | None] = {}
    years = pd.DatetimeIndex(df["ts_utc"]).year
    syms = df["symbol"].map(lambda x: "" if pd.isna(x) else str(x).strip())   # NA-safe in pandas 2 and 3
    for i, (sym, y) in enumerate(zip(syms, years)):
        key = (sym, int(y))
        if key not in cache:
            up = sym.upper()
            if any(m in up for m in _CONTINUOUS):
                cache[key] = None
            else:
                try:
                    info = parse_contract_symbol(sym, specs, reference_year=int(y))
                    cache[key] = None if info.is_continuous or info.spec is None else (info.label, info.root)
                except ContractError:
                    cache[key] = None
        v = cache[key]
        if v is None:
            bad.append(i)
            contracts.append(None)
            roots.append(None)
        else:
            contracts.append(v[0])
            roots.append(v[1])
    df["contract"], df["root"] = contracts, roots
    if bad:
        report.add("error", "UNKNOWN_OR_CONTINUOUS_SYMBOL",
                   "rows whose symbol is not an individual configured contract (excluded; contracts are never "
                   "guessed and continuous symbols are not raw data)", len(bad), df.loc[bad[:5], "symbol"].tolist())
        df = df.drop(index=bad)
    # values
    num = df[["open", "high", "low", "close", "volume"]]
    nan = num.isna().any(axis=1)
    report.add("error", "MISSING_VALUES", "rows with missing OHLCV values (excluded, never filled)", int(nan.sum()),
               df.loc[nan, "ts_utc"].head().tolist())
    df = df[~nan]
    step = pd.Timedelta(resolution)
    ts = pd.DatetimeIndex(df["ts_utc"])
    off = ts != ts.floor(step)
    report.add("error", "OFF_GRID_TIMESTAMP", f"timestamps not on the {resolution} grid (excluded)", int(off.sum()),
               ts[off][:5].tolist())
    df = df[~off]
    # order and duplicates
    order_ok = all(pd.DatetimeIndex(g["ts_utc"]).is_monotonic_increasing for _, g in df.groupby("contract"))
    if not order_ok:
        report.add("warning", "OUT_OF_ORDER", "timestamps out of order within a contract (sorted; values unchanged)")
    df = df.sort_values(["contract", "ts_utc", "source_file"], kind="stable")
    key = ["contract", "ts_utc"]
    dup = df.duplicated(key, keep=False)
    if dup.any():
        vals = ["open", "high", "low", "close", "volume"]
        grp = df[dup].groupby(key)[vals].nunique()
        conflicting = grp[(grp > 1).any(axis=1)].index
        exact = df[dup].duplicated(key + vals, keep="first")
        n_exact = int(exact.sum())
        report.add("warning", "EXACT_DUPLICATES", "identical duplicate bars (one kept)", n_exact)
        df = df.drop(index=df[dup][exact].index)
        if len(conflicting):
            mi = pd.MultiIndex.from_frame(df[key])
            conf = mi.isin(conflicting)
            report.add("error", "CONFLICTING_DUPLICATES", "the same contract and minute with different values "
                       "(all versions excluded; which one is right cannot be decided)", int(conf.sum()),
                       [f"{c} {t}" for c, t in list(conflicting)[:5]])
            df = df[~conf]
    df = df.reset_index(drop=True)
    return df[["ts_utc", "root", "contract", "open", "high", "low", "close", "volume", "source_ts", "source_file"]]


def validate(bars: pd.DataFrame, specs: dict[str, ContractSpec], calendar: Calendar, *, resolution: str,
             bars_from_trades: bool, report: Report, synthetic: bool) -> dict[str, Any]:
    """Checks on normalized bars. Returns the report body (findings, gaps, coverage, status)."""
    step = pd.Timedelta(resolution)
    gaps_rows: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    if not bars.empty:
        px = bars[["open", "high", "low", "close"]]
        e = (px <= 0).any(axis=1)
        report.add("error", "NON_POSITIVE_PRICE", "prices <= 0", int(e.sum()), bars.loc[e, "ts_utc"].head().tolist())
        e = bars["volume"] < 0
        report.add("error", "NEGATIVE_VOLUME", "negative volume", int(e.sum()), bars.loc[e, "ts_utc"].head().tolist())
        e = (bars["high"] < px[["open", "close", "low"]].max(axis=1)) | (bars["low"] > px[["open", "close", "high"]].min(axis=1))
        report.add("error", "OHLC_INCONSISTENT", "high below open/close/low or low above open/close/high",
                   int(e.sum()), bars.loc[e, "ts_utc"].head().tolist())
        tick = bars["root"].map(lambda r: specs[r].tick_size)
        ratio = px.div(tick, axis=0)
        e = ~np.isclose(ratio, np.round(ratio), rtol=0, atol=1e-6).all(axis=1)
        report.add("error", "OFF_TICK_PRICE", "prices not on the contract's tick grid", int(e.sum()),
                   bars.loc[e, "ts_utc"].head().tolist())
        closed = ~calendar.is_open(bars["ts_utc"]).to_numpy()
        report.add("error", "BAR_WHILE_MARKET_CLOSED", "bars during scheduled closures (weekend / daily halt / "
                   "verified holiday): usually a timezone or DST error in the source", int(closed.sum()),
                   bars.loc[closed, "ts_utc"].head().tolist())
        exp = bars["contract"].map(lambda c: _expiry(c, calendar))
        e = pd.DatetimeIndex(bars["ts_utc"]) >= pd.DatetimeIndex(exp)
        report.add("error", "BAR_AFTER_EXPIRY", "bars at or after the contract's termination of trading",
                   int(e.sum()), bars.loc[e, "ts_utc"].head().tolist())
        z = bars["volume"] == 0
        report.add("warning", "ZERO_VOLUME", "bars with zero volume", int(z.sum()), bars.loc[z, "ts_utc"].head().tolist())
        for c, g in bars.groupby("contract"):
            med = g.loc[g["volume"] > 0, "volume"].median()
            if med and np.isfinite(med):
                ab = g["volume"] > GATES["abnormal_volume_multiple"] * med
                report.add("warning", "ABNORMAL_VOLUME", f"{c}: bar volume > {GATES['abnormal_volume_multiple']:g}x "
                           "the median bar volume (kept; check the source)", int(ab.sum()), g.loc[ab, "ts_utc"].head().tolist())
            t = pd.DatetimeIndex(g["ts_utc"])
            close = g["close"].to_numpy()
            if len(close) > 1:
                jump = np.abs(close[1:] / close[:-1] - 1) > GATES["price_jump_fraction"]
                near = (t[1:] - t[:-1]) <= 5 * step
                j = jump & near
                report.add("warning", "PRICE_JUMP", f"{c}: close-to-close move > {GATES['price_jump_fraction']:.0%} "
                           "within 5 bars (possible bad print)", int(j.sum()), t[1:][j][:5].tolist())
            g_rows, cov = _gaps_and_coverage(c, g, calendar, step, bars_from_trades)
            gaps_rows += g_rows
            coverage[c] = cov
    gaps = pd.DataFrame(gaps_rows, columns=["contract", "after", "before", "open_minutes_missing",
                                            "rth_minutes_missing", "classification", "trade_date"])
    for cls, sev in (("MISSING_DATA_RTH", "warning"), ("MISSING_DATA_ETH", "warning"),
                     ("HOLIDAY_CANDIDATE_UNVERIFIED", "warning"), ("NO_TRADE_INTERVAL", "info"),
                     ("SCHEDULED_BREAK", "info"), ("EXPECTED_CLOSURE_WEEKEND", "info"), ("EXPECTED_HOLIDAY", "info")):
        sel = gaps[gaps["classification"] == cls]
        report.add(sev, cls, f"gaps classified {cls}", len(sel),
                   [f"{r.contract} {r.after} -> {r.before}" for r in sel.head().itertuples()])
    if synthetic:
        report.add("warning", "SYNTHETIC_DATA", "source marked SYNTHETIC: not market prices; never evidence")
    rth_missing = sum(v["active_rth_minutes_missing"] for v in coverage.values())
    rth_expected = sum(v["active_rth_minutes_expected"] for v in coverage.values())
    frac = (rth_missing / rth_expected) if rth_expected else None
    longest = int(gaps.loc[gaps["classification"] == "MISSING_DATA_RTH", "rth_minutes_missing"].max()) \
        if (gaps["classification"] == "MISSING_DATA_RTH").any() else 0
    gate = {"active_rth_missing_fraction": frac, "longest_missing_rth_gap_minutes": longest,
            "rth_expected_minutes_active": rth_expected, **GATES}
    if report.errors:
        status = "VALIDATION_FAILED"
    elif frac is not None and frac <= GATES["max_rth_missing_fraction"] and longest <= GATES["max_rth_gap_minutes"]:
        status = "VALIDATED"
    else:
        status = "INGESTED"
    return {"findings": report.findings, "gaps": gaps, "coverage": coverage, "gate": gate, "status": status}


def _expiry(contract: str, calendar: Calendar) -> pd.Timestamp:
    code, yy = contract[-3], int(contract[-2:])
    from src.config import MONTH_CODES

    return calendar.expiry(MONTH_CODES.index(code) + 1, 2000 + yy)


def _gaps_and_coverage(contract: str, g: pd.DataFrame, calendar: Calendar, step: pd.Timedelta,
                       bars_from_trades: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    t = pd.DatetimeIndex(g["ts_utc"])
    grid = pd.date_range(t[0], t[-1], freq=step)
    is_open = calendar.is_open(grid).to_numpy()
    is_rth = calendar.is_rth(grid).to_numpy()
    tdate = calendar.trade_dates(grid)
    present = grid.isin(t)
    years = sorted(set(grid.year))
    candidates = calendar.holiday_candidates(years)
    cand_dates = {pd.Timestamp(d) for d in candidates}
    # active sessions: the contract's daily volume >= share of its peak daily volume
    gtd = calendar.trade_dates(t)
    vol = g["volume"].groupby(gtd.to_numpy()).sum()
    # Timestamps on both sides: numpy datetime64 (pandas 2) and Timestamp (pandas 3) never compare equal in a set
    active = {pd.Timestamp(x) for x in vol[vol >= GATES["active_session_volume_share"] * vol.max()].index} \
        if len(vol) else set()
    missing_open = is_open & ~present
    td_arr = tdate.to_numpy()
    cls = np.full(len(grid), "", dtype=object)
    rows: list[dict[str, Any]] = []
    pos = np.searchsorted(grid, t)
    for k in range(1, len(t)):
        a, b = pos[k - 1], pos[k]
        if b - a <= 1:
            continue
        inner = slice(a + 1, b)
        n_open = int(missing_open[inner].sum())
        n_rth = int((missing_open & is_rth)[inner].sum())
        if n_open == 0:
            loc = calendar.local(grid[inner])
            if (loc.weekday == 5).any():
                c = "EXPECTED_CLOSURE_WEEKEND"
            elif any(h.verification == "VERIFIED" for h in calendar.holidays) and not calendar.is_open(grid[inner]).any():
                c = "SCHEDULED_BREAK" if (b - a - 1) * step <= pd.Timedelta(hours=1) else "EXPECTED_HOLIDAY"
            else:
                c = "SCHEDULED_BREAK"
        else:
            first = np.flatnonzero(missing_open[inner])[0] + a + 1
            day = td_arr[first]
            if pd.notna(day) and pd.Timestamp(day) in cand_dates:
                c = "HOLIDAY_CANDIDATE_UNVERIFIED"
            elif bars_from_trades and ((n_rth == 0 and n_open <= GATES["no_trade_max_bars_eth"])
                                       or (n_rth > 0 and n_open <= GATES["no_trade_max_bars_rth"])):
                c = "NO_TRADE_INTERVAL"
            elif n_rth > 0:
                c = "MISSING_DATA_RTH"
            else:
                c = "MISSING_DATA_ETH"
            cls[inner] = np.where(missing_open[inner], c, "")
        rows.append({"contract": contract, "after": t[k - 1], "before": t[k], "open_minutes_missing": n_open,
                     "rth_minutes_missing": n_rth, "classification": c,
                     "trade_date": None if pd.isna(td_arr[b]) else str(pd.Timestamp(td_arr[b]).date())})
    act = pd.Series(pd.DatetimeIndex(tdate)).isin(list(active)).to_numpy() if len(active) else np.zeros(len(grid), bool)
    rth_exp = int((is_rth & act).sum())
    rth_miss = int(((cls == "MISSING_DATA_RTH") & is_rth & act).sum())
    return rows, {
        "first_bar": str(t[0]), "last_bar": str(t[-1]), "bars": int(len(t)),
        "trade_dates": int(pd.Series(gtd).nunique()), "active_trade_dates": len(active),
        "rth_bars": int(calendar.is_rth(t).sum()), "eth_bars": int((~calendar.is_rth(t).to_numpy()).sum()),
        "open_minutes_expected": int(is_open.sum()), "open_minutes_present": int((present & is_open).sum()),
        "active_rth_minutes_expected": rth_exp, "active_rth_minutes_missing": rth_miss,
        "volume_total": float(g["volume"].sum()),
    }


__all__ = ["Report", "normalize", "validate", "GATES"]
