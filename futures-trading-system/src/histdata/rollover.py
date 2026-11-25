"""Contract rollover and continuous series (Sprint 12).

Individual expiration contracts stay the primary source of truth. A continuous series is a DERIVED
dataset that records, for every Globex trade date, which individual contract's bars it uses; prices and
timestamps are copied unchanged (``adjustment = "none"``). Rolls happen only at a trade-date boundary (never
inside a session), and the decision for trade date ``d`` uses only information available before ``d``:

* ``calendar``: roll ``days_before_expiry`` trade dates before the front contract's expiry date. Depends only
  on the published expiry schedule (no market data, no look-ahead).
* ``volume``: roll at the start of trade date ``d`` once the next contract's total volume exceeded the
  front's on the previous ``confirm_days`` COMPLETED trade dates (strictly before ``d``); the calendar rule
  remains a hard limit, so an expiring contract is never held into its expiry.

The roll table lists each roll with the price gap between the two contracts at the last minute both traded
before the roll (the discontinuity a naive splice would create). Optional back-adjustment (``difference`` or
``ratio``) produces an ADJUSTED series, flagged in the manifest; the research engine refuses adjusted
series because simulated fills, ticks and P&L must use real contract prices.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.config import MONTH_CODES, ConfigError
from src.histdata.calendar import Calendar

RULES = ("calendar", "volume")
ADJUSTMENTS = ("none", "difference", "ratio")


def _expiry_of(contract: str, cal: Calendar) -> pd.Timestamp:
    return cal.expiry(MONTH_CODES.index(contract[-3]) + 1, 2000 + int(contract[-2:]))


def build_continuous(bars: pd.DataFrame, cal: Calendar, *, rule: str = "calendar", days_before_expiry: int = 5,
                     confirm_days: int = 1, adjustment: str = "none") -> tuple[pd.DataFrame, pd.DataFrame]:
    """(continuous bars, roll table) from per-contract bars of ONE root."""
    if rule not in RULES:
        raise ConfigError(f"rule must be one of {RULES}")
    if adjustment not in ADJUSTMENTS:
        raise ConfigError(f"adjustment must be one of {ADJUSTMENTS}")
    if days_before_expiry < 1 or confirm_days < 1:
        raise ConfigError("days_before_expiry and confirm_days must be >= 1")
    if bars["root"].nunique() != 1:
        raise ConfigError("a continuous series is built from ONE root")
    b = bars.copy()
    b["trade_date"] = cal.trade_dates(b["ts_utc"]).to_numpy()
    b = b[b["trade_date"].notna()]
    contracts = sorted(b["contract"].unique(), key=lambda c: _expiry_of(c, cal))
    expiry = {c: _expiry_of(c, cal) for c in contracts}
    exp_td = {c: cal.trade_dates(pd.DatetimeIndex([expiry[c] - pd.Timedelta(minutes=1)])).iloc[0] for c in contracts}
    dates = sorted(b["trade_date"].unique())
    vol = b.groupby(["trade_date", "contract"])["volume"].sum().unstack(fill_value=0.0)
    chosen, rolls, front = {}, [], 0
    for i, d in enumerate(dates):
        # hard (calendar) limit: never hold a contract within `days_before_expiry` trade dates of its expiry
        while front < len(contracts) - 1 and _td_between(d, exp_td[contracts[front]]) <= days_before_expiry:
            rolls.append(_roll(d, contracts[front], contracts[front + 1], "calendar", None))
            front += 1
        if rule == "volume" and front < len(contracts) - 1:
            prev = [x for x in dates[:i]][-confirm_days:]
            nxt = contracts[front + 1]
            if len(prev) == confirm_days and all(vol.at[p, nxt] > vol.at[p, contracts[front]] if nxt in vol.columns
                                                 and contracts[front] in vol.columns else False for p in prev):
                rolls.append(_roll(d, contracts[front], nxt, "volume", str(pd.Timestamp(prev[-1]).date())))
                front += 1
        chosen[d] = contracts[front]
    pick = b["contract"].to_numpy() == b["trade_date"].map(chosen).to_numpy()
    out = b[pick].sort_values("ts_utc").reset_index(drop=True)
    seg = {c: k for k, c in enumerate(contracts)}
    out["segment"] = out["contract"].map(seg).astype(int)
    roll = pd.DataFrame(rolls, columns=["trade_date", "from_contract", "to_contract", "rule", "decided_on"])
    gaps = []
    for r in roll.itertuples():
        before = b[(b["trade_date"] < r.trade_date)]
        a = before[before["contract"] == r.from_contract].set_index("ts_utc")["close"]
        z = before[before["contract"] == r.to_contract].set_index("ts_utc")["close"]
        common = a.index.intersection(z.index)
        if len(common):
            t = common.max()
            gaps.append((str(t), float(a[t]), float(z[t]), float(z[t] - a[t]), float(z[t] / a[t])))
        else:
            gaps.append((None, np.nan, np.nan, np.nan, np.nan))
    gap_cols = ["gap_measured_at", "from_close", "to_close", "gap_points", "gap_ratio"]
    roll = pd.concat([roll, pd.DataFrame(gaps, index=roll.index, columns=gap_cols)], axis=1)
    roll["trade_date"] = roll["trade_date"].map(lambda x: str(pd.Timestamp(x).date()))
    roll["note"] = np.where(roll["gap_points"].isna(), "no common minute before the roll: gap unknown", "")
    if adjustment != "none":
        if roll["gap_points"].isna().any():
            raise ConfigError("cannot back-adjust: a roll gap is unknown (no common minute before the roll)")
        out["adjustment"] = 0.0 if adjustment == "difference" else 1.0
        for r in roll.itertuples():
            earlier = out["segment"] < seg[r.to_contract]      # every bar before this roll
            if adjustment == "difference":
                out.loc[earlier, ["open", "high", "low", "close"]] += r.gap_points
                out.loc[earlier, "adjustment"] += r.gap_points
            else:
                out.loc[earlier, ["open", "high", "low", "close"]] *= r.gap_ratio
                out.loc[earlier, "adjustment"] *= r.gap_ratio
    return out.drop(columns=["trade_date"]), roll


def _td_between(d, e) -> int:
    """Trade dates (Mon-Fri) from d (inclusive) to e (exclusive); negative when e is before d."""
    d, e = pd.Timestamp(d), pd.Timestamp(e)
    if e <= d:
        return -1
    return int(np.busday_count(d.date(), e.date()))


def _roll(d, a: str, b: str, rule: str, decided_on: str | None) -> dict[str, Any]:
    return {"trade_date": d, "from_contract": a, "to_contract": b, "rule": rule, "decided_on": decided_on}


__all__ = ["build_continuous", "RULES", "ADJUSTMENTS"]
