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

Safeguards (Sprint 13 P0). The builder REFUSES instead of guessing when:

* the contracts are not consecutive listed contracts of the calendar (``HMUZ``): a missing quarterly contract
  would otherwise make the series jump to a far contract and silently lose the trade dates in between.
  Build one continuous series per contiguous block instead;
* a contract code is not a listed month, or bars carry no trade date (bars outside the exchange schedule are
  validation errors and are never dropped silently here);
* the contract selected for a trade date has no bar on that date (no trade date is lost silently);
* a trade date would map to more than one contract (no duplicate sessions).

Trade dates before an expiry are counted with the calendar's VERIFIED full-day closures removed, so a
holiday inside the roll window moves the calendar roll to the right session. Every input bar is accounted
for in ``report`` (selected, or not selected because another contract was chosen that trade date).
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


def check_contract_sequence(contracts: list[str], cal: Calendar) -> list[str]:
    """The contracts sorted by expiry; refuses unlisted months and gaps in the listed-contract sequence."""
    months = cal.months
    for c in contracts:
        if len(c) < 4 or c[-3] not in MONTH_CODES or not c[-2:].isdigit():
            raise ConfigError(f"{c}: not an individual contract code (ROOT + month code + 2-digit year)")
        if c[-3] not in months:
            raise ConfigError(f"{c}: month {c[-3]} is not a listed month of the calendar ({months})")
    ordered = sorted(contracts, key=lambda c: _expiry_of(c, cal))

    def pos(c: str) -> int:
        return (2000 + int(c[-2:])) * len(months) + months.index(c[-3])

    missing = []
    for a, z in zip(ordered, ordered[1:]):
        for p in range(pos(a) + 1, pos(z)):
            y, k = divmod(p, len(months))
            missing.append(f"{a[:-3]}{months[k]}{y % 100:02d}")
    if missing:
        shown = ", ".join(missing[:8]) + (f" ... ({len(missing)} in total)" if len(missing) > 8 else "")
        raise ConfigError(f"contracts are not consecutive: missing {shown} between {ordered[0]} and {ordered[-1]}. "
                          "A continuous series never jumps over a missing contract (it would lose the trade dates "
                          "in between silently): build one series per contiguous block of contracts")
    return ordered


def build_continuous(bars: pd.DataFrame, cal: Calendar, *, rule: str = "calendar", days_before_expiry: int = 5,
                     confirm_days: int = 1, adjustment: str = "none",
                     report: dict[str, Any] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(continuous bars, roll table) from per-contract bars of ONE root.

    ``report``: if a dict is given it is filled with the accounting of every input bar (see module docs)."""
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
    outside = b["trade_date"].isna()
    if outside.any():
        ex = [str(t) for t in pd.DatetimeIndex(b.loc[outside, "ts_utc"])[:5]]
        raise ConfigError(f"{int(outside.sum())} bar(s) fall outside the exchange schedule (no trade date), e.g. {ex}: "
                          "a continuous series never drops bars silently; validate or remediate the source first")
    contracts = check_contract_sequence(list(b["contract"].unique()), cal)
    expiry = {c: _expiry_of(c, cal) for c in contracts}
    exp_td = {c: cal.trade_dates(pd.DatetimeIndex([expiry[c] - pd.Timedelta(minutes=1)])).iloc[0] for c in contracts}
    closed_days = _verified_closed_days(cal)
    dates = sorted(b["trade_date"].unique())
    vol = b.groupby(["trade_date", "contract"])["volume"].sum().unstack(fill_value=0.0)
    chosen, rolls, front, in_window = {}, [], 0, []
    for i, d in enumerate(dates):
        # hard (calendar) limit: never hold a contract within `days_before_expiry` trade dates of its expiry
        while front < len(contracts) - 1 and \
                _td_between(d, exp_td[contracts[front]], closed_days) <= days_before_expiry:
            rolls.append(_roll(d, contracts[front], contracts[front + 1], "calendar", None))
            front += 1
        if front == len(contracts) - 1 and _td_between(d, exp_td[contracts[front]], closed_days) <= days_before_expiry:
            in_window.append(str(pd.Timestamp(d).date()))   # last contract: no next contract to roll into
        if rule == "volume" and front < len(contracts) - 1:
            prev = [x for x in dates[:i]][-confirm_days:]
            nxt = contracts[front + 1]
            if len(prev) == confirm_days and all(vol.at[p, nxt] > vol.at[p, contracts[front]] if nxt in vol.columns
                                                 and contracts[front] in vol.columns else False for p in prev):
                rolls.append(_roll(d, contracts[front], nxt, "volume", str(pd.Timestamp(prev[-1]).date())))
                front += 1
        chosen[d] = contracts[front]
    have = set(zip(b["trade_date"], b["contract"]))
    lost = [str(pd.Timestamp(d).date()) for d in dates if (d, chosen[d]) not in have]
    if lost:
        raise ConfigError(f"the selected contract has no bars on {len(lost)} trade date(s) {lost[:5]}"
                          f"{' ...' if len(lost) > 5 else ''}: refused, a continuous series never loses a trade "
                          "date silently (check the source exports around those dates)")
    pick = b["contract"].to_numpy() == b["trade_date"].map(chosen).to_numpy()
    out = b[pick].sort_values("ts_utc").reset_index(drop=True)
    if out.groupby("trade_date")["contract"].nunique().max() > 1 or out.duplicated("ts_utc").any():
        raise ConfigError("internal check failed: a trade date or minute maps to more than one contract")
    seg = {c: k for k, c in enumerate(contracts)}
    out["segment"] = out["contract"].map(seg).astype(int)
    if report is not None:
        not_sel = b.loc[~pick, "contract"].value_counts()
        report.clear()
        report.update({
            "contracts": contracts, "listed_months": cal.months, "trade_dates": len(dates),
            "bars_in": int(len(b)), "bars_selected": int(len(out)), "bars_not_selected": int((~pick).sum()),
            "bars_not_selected_by_contract": {c: int(not_sel.get(c, 0)) for c in contracts},
            "trade_dates_by_contract": {c: sum(1 for d in dates if chosen[d] == c) for c in contracts},
            "first_trade_date": str(pd.Timestamp(dates[0]).date()) if dates else None,
            "last_trade_date": str(pd.Timestamp(dates[-1]).date()) if dates else None,
            "last_contract_inside_roll_window": in_window,
            "verified_closed_days_used": [str(x) for x in closed_days],
        })
        if report["bars_selected"] + report["bars_not_selected"] != report["bars_in"]:
            raise ConfigError("internal check failed: bar accounting does not add up")
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


def _verified_closed_days(cal: Calendar) -> list:
    """Trade dates the calendar lists as VERIFIED full-day closures (no session on that trade date)."""
    return sorted(h.date for h in cal.holidays if h.verification == "VERIFIED" and h.kind == "closed")


def _td_between(d, e, closed_days=()) -> int:
    """Trade dates (Mon-Fri minus VERIFIED full-day closures) from d (inclusive) to e (exclusive); negative when
    e is before d."""
    d, e = pd.Timestamp(d), pd.Timestamp(e)
    if e <= d:
        return -1
    return int(np.busday_count(d.date(), e.date(), holidays=list(closed_days)))


def _roll(d, a: str, b: str, rule: str, decided_on: str | None) -> dict[str, Any]:
    return {"trade_date": d, "from_contract": a, "to_contract": b, "rule": rule, "decided_on": decided_on}


__all__ = ["build_continuous", "check_contract_sequence", "RULES", "ADJUSTMENTS"]
