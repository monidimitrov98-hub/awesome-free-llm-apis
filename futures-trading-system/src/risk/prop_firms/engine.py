"""Prop Firm Compliance Engine: deterministic, auditable evaluation of a profile's rules.

Authority (see :mod:`src.risk.prop_firms.authority`): emergency protections > AccountGuard > THIS
ENGINE > RiskManager > strategy / AI > simulated execution. The engine is an independent veto:
nothing below it (strategy, regime analysis, sizing, execution requests) can override a BLOCK.

Results per rule: PASS, WARNING, BLOCK, UNVERIFIED (rule not verified; a safety-critical or mandatory
blocking rule then fails closed) and UNAVAILABLE (inputs missing; fails closed for entries).
Protective exits are never evaluated here: the engine only gates NEW entries and REQUESTS protective
flattening (breach, flatten reserve, flat-by time) which the runtime executes without any gate.

Money terms (all in account currency):

* balance          initial balance + realized net P&L (commissions included)
* equity           balance + unrealized P&L of open positions (unknown if a position is unmarked)
* official floor   the firm's loss threshold under the profile's model (static / trailing intraday /
                   trailing end-of-day, optional lock level). It never moves down.
* official buffer  equity - official floor (an equity AT the floor is a breach when breach_when = at_or_below)
* uncertainty      ``internal.uncertainty_reserve``: our allowance for fee / fill / data differences
                   between this simulation and the firm's own figures
* estimated buffer official buffer - uncertainty
* safety reserve   ``internal.safety_reserve``: new entries need estimated buffer > safety reserve
* flatten reserve  ``internal.flatten_reserve``: open positions are flattened at or below it

A buffer of exactly 0 is shown as 0 (a breach), never as "unavailable". No software guard can
guarantee a maximum real loss: gaps, outages and fills worse than modelled can exceed any reserve.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import threading
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import time as dtime
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT
from src.risk.prop_firms.profile import Profile, RuleSpec

PASS, WARNING, BLOCK, UNVERIFIED, UNAVAILABLE = "PASS", "WARNING", "BLOCK", "UNVERIFIED", "UNAVAILABLE"
MAX_DAYS = 400
MAX_HISTORY = 500
MAX_EXEC_IDS = 20_000
EPS = 1e-9


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _hhmm(s: str) -> dtime:
    h, m = str(s).split(":")
    return dtime(int(h), int(m))


@dataclass
class AccountSnapshot:
    """What the engine needs from the (backend) account. Never taken from a client."""

    balance: float                       # initial + realized net
    equity: float                        # balance + unrealized (NaN when a position is unmarked)
    positions: dict[str, int]            # root -> signed quantity
    pending_entries: dict[str, int] = field(default_factory=dict)  # root -> working entry quantity
    data_fresh: bool = True              # market data healthy for every instrument with a position / entry
    initial_balance: float | None = None
    known_equity: float | None = None    # balance + open P&L of the MARKED positions only (protective use)

    @property
    def unrealized(self) -> float:
        return self.equity - self.balance if _finite(self.equity) else math.nan

    @property
    def any_position(self) -> bool:
        return any(q != 0 for q in self.positions.values())


@dataclass
class RuleResult:
    rule_id: str
    rule_type: str
    result: str
    reason: str = ""
    explanation: str = ""
    threshold: Any = None
    current: Any = None
    severity: str = "block"
    safety_critical: bool = False
    verification: str = "VERIFIED"
    sources: tuple[str, ...] = ()
    effective: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComplianceDecision:
    scope: str
    allowed: bool
    reasons: list[str]
    results: list[RuleResult]
    evaluated_at: str
    profile: str
    profile_sha256: str
    ruleset_sha256: str
    account_state_sha256: str
    official_floor: float | None
    official_buffer: float | None
    estimated_buffer: float | None
    allowed_quantity: int | None = None

    @property
    def reason(self) -> str:
        return self.reasons[0] if self.reasons else ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["results"] = [r.to_dict() for r in self.results]
        return d


class ComplianceEngine:
    def __init__(self, profile: Profile, *, operational: dict[str, Any], account_guard_rules: Any = None,
                 holiday_dir: Path | None = None, tick_values: dict[str, float] | None = None,
                 operational_check: Any = None) -> None:
        self.profile = profile
        # re-checked before every recorded decision: a human revocation takes effect immediately
        self.operational_check = operational_check
        self.tick_values = dict(tick_values or {})   # $ per tick per contract (from the contract specifications)
        self.operational = dict(operational)
        self.ag_rules = account_guard_rules
        self.holiday_dir = holiday_dir or PROJECT_ROOT / "config" / "prop_firms" / "holidays"
        self.tz = profile.timezone
        self.day_start = _hhmm(profile.day_start)
        self._lock = threading.RLock()
        ib = profile.initial_balance
        self.eod_high = ib                 # highest end-of-day reference value (trailing EOD)
        self.intraday_high = ib            # highest intraday reference value (trailing intraday)
        self.floor_max = -math.inf         # the official floor never moves down
        self.breached = False
        self.breach: dict[str, Any] | None = None
        self.current_day: str | None = None
        self.day_start_balance: float | None = None
        self.last_balance: float | None = None
        self.last_equity: float | None = None
        self.last_ts: pd.Timestamp | None = None
        self.days: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
        self.exec_ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_HISTORY)
        self.violations: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_HISTORY)
        self.out_of_order = 0
        self._pending_journal: list[dict[str, Any]] = []   # records produced outside observe(), returned by it
        self.recovery_ok = True
        self.recovery_problems: list[str] = []
        self._holiday_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------ calendar
    def trading_date(self, ts: pd.Timestamp) -> str:
        """Trading date of ``ts`` under the profile's day start (e.g. 17:00 America/Chicago starts the next
        trading date; weekend times belong to Monday). DST-safe (local wall-clock times)."""
        local = pd.Timestamp(ts).tz_convert(self.tz)
        d = local.normalize()
        if self.day_start != dtime(0, 0) and local.time() >= self.day_start:
            d = d + pd.Timedelta(days=1)
        if d.weekday() == 5:
            d = d + pd.Timedelta(days=2)
        elif d.weekday() == 6:
            d = d + pd.Timedelta(days=1)
        return str(d.date())

    def _entry_window(self, ts: pd.Timestamp) -> tuple[bool, str]:
        """(entries allowed, why not) under flat_by / trading_hours (local wall clock)."""
        fb = self.profile.rule("flat_by")
        if fb is None:
            return True, ""
        local = pd.Timestamp(ts).tz_convert(self.tz)
        flat = _hhmm(fb.params["time"])
        cutoff_dt = pd.Timestamp.combine(local.date(), flat).tz_localize(self.tz, ambiguous=True, nonexistent="shift_forward") \
            - pd.Timedelta(minutes=self.profile.internal.entry_cutoff_minutes)
        cutoff = cutoff_dt.time()
        th = self.profile.rule("trading_hours")
        start = _hhmm(th.params["day_start"]) if th else self.day_start
        t, wd = local.time(), local.weekday()
        if wd == 5:
            return False, "weekend"
        if wd == 6:
            return (t >= start, "weekend" if t < start else "")
        if wd == 4 and t >= cutoff:
            return False, "after_flat_by_cutoff_friday"
        blocked = (cutoff <= t < start) if start > cutoff else (t >= cutoff or t < start)
        return (not blocked, "outside_permitted_entry_hours" if blocked else "")

    def _must_be_flat(self, ts: pd.Timestamp) -> bool:
        fb = self.profile.rule("flat_by")
        if fb is None:
            return False
        local = pd.Timestamp(ts).tz_convert(self.tz)
        flat = pd.Timestamp.combine(local.date(), _hhmm(fb.params["time"])).tz_localize(
            self.tz, ambiguous=True, nonexistent="shift_forward") - pd.Timedelta(minutes=self.profile.internal.flatten_lead_minutes)
        th = self.profile.rule("trading_hours")
        start = _hhmm(th.params["day_start"]) if th else self.day_start
        t = local.time()
        if local.weekday() == 5 or (local.weekday() == 6 and t < start):
            return True
        return flat.time() <= t < start if start > flat.time() else (t >= flat.time() or t < start)

    # ------------------------------------------------------------------ drawdown
    def _reference(self, snap: AccountSnapshot) -> float | None:
        ml = self.profile.rule("max_loss")
        ref = (ml.params.get("reference", "balance") if ml else "balance")
        v = snap.equity if ref == "equity" else snap.balance
        return v if _finite(v) else None

    def lock_level(self) -> float | None:
        """The level at which the trailing floor stops, ONLY when both the loss rule and its lock are VERIFIED.
        An unverified / conflicting lock is never applied: without it the floor keeps trailing, which can only
        be stricter than any interpretation with a lock."""
        ml = self.profile.rule("max_loss")
        if ml is None or ml.params.get("lock_at") is None or ml.params.get("model") == "static":
            return None
        if ml.verification != "VERIFIED" or ml.params.get("lock_verification", "VERIFIED") != "VERIFIED":
            return None
        return float(ml.params["lock_at"])

    def official_floor(self) -> float | None:
        ml = self.profile.rule("max_loss")
        if ml is None or ml.missing_params:
            return None
        amount, model = float(ml.params["amount"]), ml.params["model"]
        base = {"static": self.profile.initial_balance, "trailing_eod": self.eod_high,
                "trailing_intraday": self.intraday_high}[model]
        floor = base - amount
        lock = self.lock_level()
        if lock is not None:
            floor = min(floor, lock)
        self.floor_max = max(self.floor_max, floor)
        return self.floor_max

    def _equity_for_loss(self, snap: AccountSnapshot) -> float | None:
        ml = self.profile.rule("max_loss")
        v = snap.equity if (ml is None or ml.params.get("includes_unrealized", True)) else snap.balance
        return v if _finite(v) else None

    def buffers(self, snap: AccountSnapshot) -> dict[str, float | None]:
        floor = self.official_floor()
        eq = self._equity_for_loss(snap)
        if floor is None or eq is None:
            return {"official_floor": floor, "official_buffer": None, "estimated_buffer": None}
        off = eq - floor
        return {"official_floor": floor, "official_buffer": off,
                "estimated_buffer": off - self.profile.internal.uncertainty_reserve}

    # ------------------------------------------------------------------ observation (runtime hooks)
    def observe(self, ts: pd.Timestamp, snap: AccountSnapshot) -> list[dict[str, Any]]:
        """Called on every processed market event and after every execution (backend values only).
        Returns actions: ``{"action": "flatten", "reason": ...}`` and journal records
        ``{"action": "journal", "kind": ..., "payload": ...}``. Out-of-order calls are ignored."""
        with self._lock:
            ts = pd.Timestamp(ts)
            out: list[dict[str, Any]] = self._pending_journal
            self._pending_journal = []
            if self.last_ts is not None and ts < self.last_ts:
                # never move days / trailing backwards, but ALWAYS keep checking the loss limit below
                self.out_of_order += 1
                day = self.current_day or self.trading_date(ts)
            else:
                self.last_ts = ts
                day = self.trading_date(ts)
                self._roll(day, snap.balance, ts, out)
            ml = self.profile.rule("max_loss")
            ref = self._reference(snap)
            if ml is not None and ml.params.get("model") == "trailing_intraday" and ref is not None:
                self.intraday_high = max(self.intraday_high, ref)
            if _finite(snap.balance):
                self.last_balance = snap.balance
            if _finite(snap.equity):
                self.last_equity = snap.equity
            d = self._day(day)
            if _finite(snap.balance) and self.day_start_balance is not None:
                d["net"] = snap.balance - self.day_start_balance
                d["end_balance"] = snap.balance
            exp = self._exposure(snap.positions)
            if exp is not None:
                d["max_micro_exposure"] = max(d.get("max_micro_exposure", 0), exp)
            b = self.buffers(snap)
            if b["official_buffer"] is not None:
                d["min_official_buffer"] = min(d.get("min_official_buffer", math.inf), b["official_buffer"])
                breach_at = ml.params.get("breach_when", "at_or_below") if ml else "at_or_below"
                hit = b["official_buffer"] <= EPS if breach_at == "at_or_below" else b["official_buffer"] < -EPS
                if hit and not self.breached:
                    self.breached = True
                    self.breach = {"time": ts.isoformat(), "equity": self._equity_for_loss(snap),
                                   "floor": b["official_floor"], "rule": ml.id}
                    self._violation(ts, ml.id, "max_loss_breached", b)
                    out.append({"action": "journal", "kind": "prop_firm_breach", "payload": dict(self.breach)})
                if snap.any_position and (self.breached or b["estimated_buffer"] <= self.profile.internal.flatten_reserve + EPS):
                    out.append({"action": "flatten", "reason": "prop_firm_max_loss_protection"})
            elif snap.any_position and ml is not None and b["official_floor"] is not None \
                    and _finite(snap.known_equity):
                # Full equity unknown (an open position has no valid mark, e.g. after a restart). The losses that
                # ARE known must still protect the account even when AccountGuard is disabled: flatten when they
                # alone reach the flatten reserve. No breach is recorded from partial information; entries stay
                # blocked (prop_equity_unavailable).
                known = snap.known_equity - b["official_floor"] - self.profile.internal.uncertainty_reserve
                if known <= self.profile.internal.flatten_reserve + EPS:
                    out.append({"action": "flatten", "reason": "prop_firm_max_loss_protection_partial_equity"})
            if snap.any_position and self._must_be_flat(ts):
                out.append({"action": "flatten", "reason": "prop_firm_flat_by"})
            return out

    def _roll(self, day: str, balance: float, ts: pd.Timestamp, out: list[dict[str, Any]]) -> None:
        if self.current_day is None:
            self.current_day, self.day_start_balance = day, balance if _finite(balance) else None
        elif day > self.current_day:
            out += self._close_day(ts)
            self.current_day, self.day_start_balance = day, balance if _finite(balance) else None

    def _close_day(self, ts: pd.Timestamp) -> list[dict[str, Any]]:
        ml = self.profile.rule("max_loss")
        out = []
        prev = self.days.get(self.current_day, {})
        ref = self.last_balance if (ml is None or ml.params.get("reference", "balance") == "balance") else self.last_equity
        before = self.eod_high
        if ml is not None and ml.params.get("model") == "trailing_eod" and ref is not None and _finite(ref):
            self.eod_high = max(self.eod_high, ref)
        prev["closed"] = True
        out.append({"action": "journal", "kind": "prop_firm_eod",
                    "payload": {"trading_date": self.current_day, "eod_reference": ref, "eod_high_before": before,
                                "eod_high": self.eod_high, "official_floor": self.official_floor(),
                                "day_net": prev.get("net"), "profile": self.profile.key}})
        return out

    def end_of_day(self, ts: pd.Timestamp) -> list[dict[str, Any]]:
        """Explicit end-of-day (e.g. at shutdown after the flat-by time); idempotent per trading date."""
        with self._lock:
            if self.current_day is None or self.days.get(self.current_day, {}).get("closed"):
                return []
            return self._close_day(ts)

    def on_execution(self, exec_id: str, ts: pd.Timestamp) -> bool:
        """Count an execution once (duplicate exec ids are ignored). Returns False for a duplicate."""
        with self._lock:
            if exec_id in self.exec_ids:
                return False
            self.exec_ids[exec_id] = None
            while len(self.exec_ids) > MAX_EXEC_IDS:
                self.exec_ids.popitem(last=False)
            d = self._day(self.trading_date(ts))
            d["executions"] = d.get("executions", 0) + 1
            return True

    def _day(self, day: str) -> dict[str, Any]:
        d = self.days.get(day)
        if d is None:
            d = self.days[day] = {"net": 0.0, "executions": 0}
            while len(self.days) > MAX_DAYS:
                self.days.popitem(last=False)
        return d

    def _exposure(self, positions: dict[str, int], extra: dict[str, int] | None = None) -> float | None:
        mc = self.profile.rule("max_contracts")
        if mc is None:
            return None
        w = mc.params.get("weights") or {}
        total = 0.0
        for src in (positions, extra or {}):
            for r, q in src.items():
                if q and r not in w:
                    return math.inf  # unknown instrument weight: treated as over the limit
                total += abs(q) * float(w.get(r, 0))
        return total

    def _violation(self, ts, rule_id, code, detail) -> None:
        self.violations.append({"time": pd.Timestamp(ts).isoformat(), "rule": rule_id, "code": code,
                                "detail": {k: v for k, v in detail.items() if not isinstance(v, (dict, list))}})

    # ------------------------------------------------------------------ evaluation
    def _res(self, rule: RuleSpec | None, rtype: str, result: str, reason: str = "", explanation: str = "",
             threshold=None, current=None) -> RuleResult:
        if rule is None:  # the engine's own integrity checks (not firm rules)
            return RuleResult(f"engine:{rtype}", rtype, result, reason, explanation, threshold, current,
                              "block", True, "ENGINE")
        if rule.verification != "VERIFIED" and result in (PASS, WARNING, BLOCK):
            # a computed outcome of an unverified rule is shown but labelled UNVERIFIED
            explanation = f"{explanation} [rule {rule.verification}: {rule.ambiguity or 'not verified'}]".strip()
            if result != BLOCK:
                result, reason = UNVERIFIED, reason or f"prop_rule_unverified:{rule.id}"
        return RuleResult(rule.id, rule.type, result, reason, explanation, threshold, current, rule.severity,
                          rule.safety_critical, rule.verification, rule.sources, rule.effective)

    def evaluate_entry(self, ts: pd.Timestamp, root: str, quantity: int, snap: AccountSnapshot, *,
                       trade_risk: float | None = None, news: dict[str, Any] | None = None,
                       scope: str = "pre_trade", record: bool = True) -> ComplianceDecision:
        """Pre-trade (``trade_risk`` optional) or fill-time (``trade_risk`` REQUIRED) evaluation of a NEW entry."""
        with self._lock:
            ts = pd.Timestamp(ts)
            if record and (self.last_ts is None or ts >= self.last_ts):
                # a new trading date starts the moment it begins, even before the first market event of it:
                # the end-of-day trailing is applied BEFORE the entry is evaluated
                self._roll(self.trading_date(ts), snap.balance, ts, self._pending_journal)
            p, it = self.profile, self.profile.internal
            res: list[RuleResult] = []
            if record:
                self.refresh_operational()
            op = self.operational
            res.append(RuleResult("engine:profile_operational", "profile", PASS if op.get("operational") else BLOCK,
                                  "" if op.get("operational") else "prop_profile_not_operational",
                                  op.get("not_operational_reason") or "profile VERIFIED and human-activated",
                                  "VERIFIED + activation", op.get("status"), "block", True, "ENGINE"))
            if snap.initial_balance is not None and abs(snap.initial_balance - p.initial_balance) > EPS:
                res.append(self._res(None, "account_size", BLOCK, "prop_account_size_mismatch",
                                     "the paper account's initial balance differs from the profile's", p.initial_balance,
                                     snap.initial_balance))
            if not self.recovery_ok:
                res.append(self._res(None, "state", BLOCK, "prop_state_untrusted", "; ".join(self.recovery_problems)))
            if not snap.data_fresh:
                res.append(self._res(None, "data", BLOCK, "prop_market_data_unavailable",
                                     "market data not healthy: rules that need prices cannot be evaluated"))
            if quantity is None or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                res.append(self._res(None, "request", BLOCK, "prop_invalid_quantity", "", 1, quantity))
            if scope == "fill" and not (_finite(trade_risk) and trade_risk > 0):
                res.append(self._res(None, "request", BLOCK, "prop_trade_risk_unknown",
                                     "fill-time evaluation requires the trade's actual risk"))
            b = self.buffers(snap)
            res += self._max_loss_results(snap, b, trade_risk, quantity, root)
            res += self._daily_loss_results(snap, trade_risk)
            allowed_q, cres = self._contract_results(root, quantity, snap)
            res += cres
            al = p.rule("instrument_allowlist")
            if al is not None:
                ok = root in (al.params.get("roots") or [])
                res.append(self._res(al, al.type, PASS if ok else BLOCK, "" if ok else "prop_instrument_not_allowed",
                                     f"{root} {'is' if ok else 'is not'} on the profile's instrument list",
                                     al.params.get("roots"), root))
            fb = p.rule("flat_by")
            if fb is not None:
                ok, why = self._entry_window(ts)
                res.append(self._res(fb, fb.type, PASS if ok else BLOCK, "" if ok else f"prop_session_{why}",
                                     f"positions must be flat by {fb.params['time']} {self.tz}; new entries stop "
                                     f"{it.entry_cutoff_minutes:g} min earlier and resume at the next trading-day start",
                                     fb.params["time"], pd.Timestamp(ts).tz_convert(self.tz).strftime("%a %H:%M")))
            res += self._holiday_results(ts)
            nr = p.rule("news_restriction")
            if nr is not None:
                if news is None or news.get("available") is False:
                    res.append(self._res(nr, nr.type, UNAVAILABLE, "prop_news_state_unavailable",
                                         "the firm restricts news trading but NewsGuard's state is unavailable"))
                elif not news.get("allowed"):
                    res.append(self._res(nr, nr.type, BLOCK, "prop_news_restricted", str(news.get("reason", ""))))
                else:
                    res.append(self._res(nr, nr.type, PASS, "", "NewsGuard allows entries now"))
            res += self._consistency_results(snap)
            for r in p.rules:  # rules no generic evaluator checks: an unresolved safety-critical one fails closed
                if r.type == "other" and r.applies_to(p.stage) and r.safety_critical and r.verification != "VERIFIED":
                    res.append(RuleResult(r.id, r.type, UNVERIFIED, f"prop_rule_unverified:{r.id}",
                                          r.ambiguity or r.description, None, None, r.severity, True, r.verification,
                                          r.sources, r.effective))
            res += self._account_guard_consistency()
            if self.breached:
                res.append(self._res(p.rule("max_loss"), "max_loss", BLOCK, "prop_max_loss_breached",
                                     f"official loss limit breached at {self.breach.get('time') if self.breach else '?'}"))
            reasons = []
            for r in res:
                fails_closed = r.result in (UNVERIFIED, UNAVAILABLE) and (r.safety_critical or (r.severity == "block"
                                                                                                 and r.result == UNAVAILABLE))
                if r.result == BLOCK or fails_closed:
                    code = r.reason or f"prop_{r.result.lower()}:{r.rule_id}"
                    if code not in reasons:
                        reasons.append(code)
            dec = ComplianceDecision(scope, not reasons, reasons, res, ts.isoformat(), p.key, p.sha256, p.ruleset_sha256,
                                     self._state_sha(snap), b["official_floor"], b["official_buffer"], b["estimated_buffer"],
                                     allowed_q if not reasons else None)
            if record:
                self.history.append({"time": ts.isoformat(), "scope": scope, "root": root, "quantity": quantity,
                                     "allowed": dec.allowed, "reasons": reasons, "profile": p.key,
                                     "ruleset_sha256": p.ruleset_sha256, "official_buffer": b["official_buffer"]})
            return dec

    def _max_loss_results(self, snap, b, trade_risk, quantity, root) -> list[RuleResult]:
        ml = self.profile.rule("max_loss")
        it = self.profile.internal
        if ml is None:
            return [self._res(None, "max_loss", UNAVAILABLE, "prop_max_loss_rule_missing",
                              "the profile defines no maximum loss rule")]
        if ml.missing_params:
            return [self._res(ml, ml.type, UNAVAILABLE, "prop_max_loss_incomplete", f"missing {ml.missing_params}")]
        if b["official_buffer"] is None:
            return [self._res(ml, ml.type, UNAVAILABLE, "prop_equity_unavailable",
                              "equity unknown (an open position has no valid mark)", b["official_floor"], None)]
        est = b["estimated_buffer"]
        out = []
        if est <= it.safety_reserve + EPS:
            out.append(self._res(ml, ml.type, BLOCK, "prop_buffer_below_safety_reserve",
                                 f"estimated buffer {est:.2f} <= internal safety reserve {it.safety_reserve:.2f}",
                                 it.safety_reserve, est))
        elif trade_risk is not None and _finite(trade_risk) and it.require_trade_risk_within_buffer:
            gap = 0.0
            if it.gap_reserve_ticks:
                tv = self.tick_values.get(root)
                if not _finite(tv) or tv <= 0:
                    return [self._res(ml, ml.type, UNAVAILABLE, "prop_tick_value_unknown",
                                      f"no tick value for {root}: the gap allowance cannot be computed")]
                gap = it.gap_reserve_ticks * quantity * tv
            after = est - trade_risk - gap
            ok = after > it.safety_reserve + EPS
            out.append(self._res(ml, ml.type, PASS if ok else BLOCK, "" if ok else "prop_trade_risk_exceeds_buffer",
                                 f"a full stop-out ({trade_risk:.2f} + gap allowance {gap:.2f}) would leave {after:.2f}; "
                                 f"needs > {it.safety_reserve:.2f}", it.safety_reserve, after))
        else:
            warn = est <= 2 * it.safety_reserve
            out.append(self._res(ml, ml.type, WARNING if warn else PASS, "",
                                 f"official buffer {b['official_buffer']:.2f}, estimated {est:.2f} "
                                 f"(floor {b['official_floor']:.2f})", b["official_floor"], b["official_buffer"]))
        return out

    def _daily_loss_results(self, snap, trade_risk) -> list[RuleResult]:
        dl = self.profile.rule("daily_loss")
        if dl is None:
            return []
        if dl.missing_params or self.day_start_balance is None or not _finite(snap.balance):
            return [self._res(dl, dl.type, UNAVAILABLE, "prop_daily_loss_unavailable", "day start balance unknown")]
        day_pnl = snap.balance - self.day_start_balance
        if dl.params.get("includes_unrealized", True):
            if not _finite(snap.equity):
                return [self._res(dl, dl.type, UNAVAILABLE, "prop_daily_loss_unavailable", "open P&L unknown")]
            day_pnl += snap.unrealized
        remaining = float(dl.params["amount"]) + day_pnl - self.profile.internal.uncertainty_reserve
        need = (trade_risk if _finite(trade_risk) else 0.0)
        ok = remaining - need > EPS
        res = PASS if ok else BLOCK
        if ok and remaining <= 0.25 * float(dl.params["amount"]):
            res = WARNING
        return [self._res(dl, dl.type, res, "" if ok else "prop_daily_loss_limit",
                          f"day P&L {day_pnl:.2f}; estimated remaining {remaining:.2f}", dl.params["amount"], day_pnl)]

    def _contract_results(self, root, quantity, snap) -> tuple[int | None, list[RuleResult]]:
        mc = self.profile.rule("max_contracts")
        if mc is None or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            return (quantity if isinstance(quantity, int) else None), []
        w = (mc.params.get("weights") or {})
        if root not in w:
            return None, [self._res(mc, mc.type, BLOCK, "prop_instrument_not_in_contract_table",
                                    f"no contract weight for {root}")]
        limit = float(mc.params["limit_micro_equivalent"])
        cur = self._exposure(snap.positions, snap.pending_entries)
        if cur is None or math.isinf(cur):
            return None, [self._res(mc, mc.type, BLOCK, "prop_exposure_unknown",
                                    "a position or pending entry is in an instrument without a contract weight")]
        wt = float(w[root])
        room = math.floor((limit - cur) / wt + EPS)
        if quantity <= room:
            return quantity, [self._res(mc, mc.type, PASS, "", f"exposure {cur:g} + {quantity * wt:g} <= {limit:g} "
                                        "micro-equivalent", limit, cur + quantity * wt)]
        if self.profile.internal.quantity_policy == "reduce" and room >= 1:
            return room, [self._res(mc, mc.type, WARNING, "",
                                    f"quantity reduced {quantity} -> {room} by explicit policy (never increased)",
                                    limit, cur + room * wt)]
        return None, [self._res(mc, mc.type, BLOCK, "prop_contract_limit",
                                f"exposure {cur:g} + {quantity * wt:g} > {limit:g} micro-equivalent", limit,
                                cur + quantity * wt)]

    def _holiday_results(self, ts) -> list[RuleResult]:
        hs = self.profile.rule("holiday_schedule")
        if hs is None:
            return []
        cal = self._holidays(str(hs.params.get("calendar", "")))
        day = self.trading_date(ts)
        if cal is None or not cal.get("covered_until") or day > str(cal["covered_until"]):
            return [self._res(hs, hs.type, UNAVAILABLE, "prop_holiday_schedule_unavailable",
                              "no firm-published holiday / early-close schedule covering this date")]
        entry = cal["days"].get(day)
        if entry == "closed":
            return [self._res(hs, hs.type, BLOCK, "prop_holiday_closed", f"{day} is a firm holiday")]
        if entry:
            local = pd.Timestamp(ts).tz_convert(self.tz)
            ok = local.time() < _hhmm(entry)
            return [self._res(hs, hs.type, PASS if ok else BLOCK, "" if ok else "prop_holiday_early_close",
                              f"early close {entry} on {day}", entry, local.strftime("%H:%M"))]
        return [self._res(hs, hs.type, PASS, "", "regular trading day")]

    def _holidays(self, name: str) -> dict[str, Any] | None:
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        if name in self._holiday_cache:
            return self._holiday_cache[name]
        f = self.holiday_dir / f"{name}.toml"
        out = None
        try:
            raw = tomllib.loads(f.read_text(encoding="utf-8"))
            out = {"covered_until": str(raw.get("covered_until", "")),
                   "days": {str(d["date"]): str(d["close"]) for d in raw.get("days", [])}}
        except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
            out = None
        self._holiday_cache[name] = out
        return out

    def _consistency_results(self, snap) -> list[RuleResult]:
        c = self.profile.rule("consistency")
        if c is None:
            return []
        rep = self.consistency_report(snap)
        if rep["status"] == "UNAVAILABLE":
            return [self._res(c, c.type, UNAVAILABLE, "", rep["explanation"])]
        res = WARNING if rep["today_share_of_threshold"] is not None and rep["today_share_of_threshold"] >= 0.9 else PASS
        return [self._res(c, c.type, res, "", rep["explanation"], rep["daily_threshold"], rep["best_day"])]

    def consistency_report(self, snap: AccountSnapshot | None = None) -> dict[str, Any]:
        """Consistency rule (as defined by the profile). Numerator: the best trading day's net balance change
        (realized, fees included; open P&L excluded). Denominator: ``max_day_share`` x the profit target.
        Observation period: every recorded trading day of this account. Consequence per the profile."""
        c = self.profile.rule("consistency")
        if c is None:
            return {"status": "NOT_APPLICABLE"}
        tgt = self.profile.rule(c.params.get("of", "profit_target"))
        if tgt is None or "amount" not in tgt.params:
            return {"status": "UNAVAILABLE", "explanation": "the profit target the rule refers to is not defined"}
        share, target = float(c.params["max_day_share"]), float(tgt.params["amount"])
        nets = {d: v.get("net", 0.0) for d, v in self.days.items()}
        best_day = max(nets.values(), default=0.0)
        total = sum(nets.values())
        threshold = share * target
        today = nets.get(self.current_day, 0.0) if self.current_day else 0.0
        adjusted = max(target, best_day / share) if best_day > 0 else target
        return {"status": "OK", "rule": c.id, "verification": c.verification, "ambiguity": c.ambiguity,
                "daily_threshold": threshold, "best_day": best_day, "total_profit": total, "today": today,
                "today_share_of_threshold": (today / threshold) if threshold > 0 else None,
                "profit_target": target, "adjusted_profit_target": adjusted,
                "consequence": c.params.get("consequence", ""),
                "definition": "numerator = best trading day's net balance change (realized, fees included, open "
                              "P&L excluded); denominator = max_day_share x profit target; period = all recorded days",
                "explanation": f"best day {best_day:.2f} vs {share:.0%} x {target:.2f} = {threshold:.2f}; "
                               f"target {'adjusted to ' + format(adjusted, '.2f') if adjusted > target else 'unchanged'}"}

    def _account_guard_consistency(self) -> list[RuleResult]:
        ml = self.profile.rule("max_loss")
        ag = self.ag_rules
        if ml is None or ml.missing_params:
            return []
        if ag is None or not getattr(ag, "enabled", False):
            return [self._res(None, "account_guard", WARNING, "", "AccountGuard is disabled: only this engine "
                              "enforces the firm's loss limit (AccountGuard remains the survival authority when on)")]
        model_map = {"static": "static", "trailing_eod": "trailing_eod", "trailing_intraday": "trailing_intraday"}
        looser = ag.max_drawdown > float(ml.params["amount"]) + EPS or ag.drawdown_type != model_map[ml.params["model"]]
        lock = self.lock_level()
        if not looser and ml.params["model"] != "static" and ag.floor_lock_offset is not None:
            # AccountGuard's floor stops trailing at initial + offset: looser when below the firm's (applied) lock
            # level, or when the firm floor is modelled without a lock at all
            looser = lock is None or self.profile.initial_balance + float(ag.floor_lock_offset) < lock - EPS
        if looser:
            return [self._res(None, "account_guard", WARNING, "",
                              f"AccountGuard ({ag.drawdown_type}, {ag.max_drawdown:g}) is not at least as strict as the "
                              f"firm rule ({ml.params['model']}, {ml.params['amount']}); this engine still enforces the "
                              "firm threshold for entries and requests protective flattening")]
        return [self._res(None, "account_guard", PASS, "", "AccountGuard is at least as strict as the firm loss rule")]

    def refresh_operational(self) -> dict[str, Any]:
        """Re-read the human activation state (a revocation is visible immediately, also in read-only views)."""
        with self._lock:
            if self.operational_check is not None:
                try:
                    self.operational = dict(self.operational_check())
                except Exception as exc:  # noqa: BLE001 -- an unreadable approval store approves nothing
                    self.operational = {"operational": False, "status": "UNKNOWN",
                                        "not_operational_reason": f"activation state unreadable ({type(exc).__name__})"}
            return self.operational

    @staticmethod
    def classify(dec: ComplianceDecision, breached: bool) -> str:
        """Display state. SAFE only when every rule passed AND the profile is operational: an unresolved
        mandatory rule is never shown as SAFE."""
        res = {r.result for r in dec.results}
        hard = [x for x in dec.reasons if x not in ("prop_profile_not_operational",)
                and not x.startswith("prop_rule_unverified") and not x.startswith("prop_unavailable")
                and x not in ("prop_holiday_schedule_unavailable", "prop_equity_unavailable",
                              "prop_market_data_unavailable", "prop_news_state_unavailable",
                              "prop_daily_loss_unavailable", "prop_tick_value_unknown")]
        if breached or hard:
            return "BLOCKED"
        if "prop_profile_not_operational" in dec.reasons or UNVERIFIED in res:
            return "UNVERIFIED"
        if UNAVAILABLE in res:
            return "DATA UNAVAILABLE"
        if WARNING in res:
            return "WARNING"
        return "SAFE" if dec.allowed else "BLOCKED"

    # ------------------------------------------------------------------ identity / persistence
    def _state_sha(self, snap: AccountSnapshot) -> str:
        body = {"balance": round(snap.balance, 6) if _finite(snap.balance) else None,
                "equity": round(snap.equity, 6) if _finite(snap.equity) else None,
                "positions": dict(sorted(snap.positions.items())), "eod_high": self.eod_high,
                "intraday_high": self.intraday_high, "floor": self.floor_max if math.isfinite(self.floor_max) else None,
                "day": self.current_day, "breached": self.breached}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"profile": self.profile.key, "profile_sha256": self.profile.sha256,
                    "ruleset_sha256": self.profile.ruleset_sha256, "eod_high": self.eod_high,
                    "intraday_high": self.intraday_high,
                    "floor_max": self.floor_max if math.isfinite(self.floor_max) else None,
                    "breached": self.breached, "breach": self.breach, "current_day": self.current_day,
                    "day_start_balance": self.day_start_balance, "last_balance": self.last_balance,
                    "last_equity": self.last_equity, "last_ts": self.last_ts.isoformat() if self.last_ts is not None else None,
                    "days": dict(self.days), "exec_ids": list(self.exec_ids)[-MAX_EXEC_IDS:],
                    "violations": list(self.violations)}

    def restore(self, st: dict[str, Any]) -> None:
        """Restore persisted state. A state written under another profile, version or content is NEVER
        adopted: the engine stays untrusted (entries blocked) instead of silently resetting anything."""
        with self._lock:
            if st.get("profile") != self.profile.key or st.get("ruleset_sha256") != self.profile.ruleset_sha256:
                self.recovery_ok = False
                self.recovery_problems.append(f"stored compliance state belongs to {st.get('profile')} "
                                              f"(rule set {str(st.get('ruleset_sha256'))[:12]}), not {self.profile.key} "
                                              f"({self.profile.ruleset_sha256[:12]}); refusing to substitute or reset")
                return
            try:
                self.eod_high = float(st["eod_high"])
                self.intraday_high = float(st["intraday_high"])
                self.floor_max = float(st["floor_max"]) if st.get("floor_max") is not None else -math.inf
                self.breached, self.breach = bool(st["breached"]), st.get("breach")
                self.current_day = st.get("current_day")
                self.day_start_balance = st.get("day_start_balance")
                self.last_balance, self.last_equity = st.get("last_balance"), st.get("last_equity")
                self.last_ts = pd.Timestamp(st["last_ts"]) if st.get("last_ts") else None
                self.days = collections.OrderedDict(st.get("days") or {})
                self.exec_ids = collections.OrderedDict((e, None) for e in st.get("exec_ids") or [])
                self.violations = collections.deque(st.get("violations") or [], maxlen=MAX_HISTORY)
            except (KeyError, TypeError, ValueError) as exc:
                self.recovery_ok = False
                self.recovery_problems.append(f"stored compliance state invalid: {type(exc).__name__}")
                return
            ib = self.profile.initial_balance
            if self.eod_high < ib - EPS or self.intraday_high < ib - EPS or not all(
                    _finite(v) for v in (self.eod_high, self.intraday_high)):
                self.recovery_ok = False
                self.recovery_problems.append("stored high-water mark below the initial balance (rollback)")

    def check_journal_consistency(self, journaled_eod_highs: list[float], journaled_breach: bool) -> None:
        """Rollback protection: the restored state must not be behind what the journal recorded."""
        with self._lock:
            if journaled_eod_highs and self.eod_high < max(journaled_eod_highs) - 1e-6:
                self.recovery_ok = False
                self.recovery_problems.append("restored end-of-day high-water mark is below a journaled value (rollback)")
            if journaled_breach and not self.breached:
                self.recovery_ok = False
                self.recovery_problems.append("journal records a breach the restored state lost")

    # ------------------------------------------------------------------ views
    def summary(self, snap: AccountSnapshot | None, ts: pd.Timestamp | None = None) -> dict[str, Any]:
        with self._lock:
            p = self.profile
            b = self.buffers(snap) if snap is not None else {"official_floor": self.official_floor(),
                                                             "official_buffer": None, "estimated_buffer": None}
            dl = p.rule("daily_loss")
            mc = p.rule("max_contracts")
            exposure = self._exposure(snap.positions) if snap is not None and mc is not None else None
            today = self.days.get(self.current_day or "", {})
            ml = p.rule("max_loss")
            return {
                "profile": p.key, "firm": p.firm, "product": p.product, "stage": p.stage, "account_type": p.account_type,
                "account_size": p.account_size, "initial_balance": p.initial_balance, "timezone": p.timezone,
                "profile_sha256": p.sha256, "ruleset_sha256": p.ruleset_sha256,
                "operational": self.operational, "balance": snap.balance if snap else None,
                "equity": snap.equity if snap is not None and _finite(snap.equity) else None,
                "unrealized": snap.unrealized if snap is not None and _finite(snap.equity) else None,
                "eod_high_water": self.eod_high, "intraday_high_water": self.intraday_high,
                "drawdown_model": ml.params.get("model") if ml else None,
                "official_threshold": {"floor": b["official_floor"], "amount": ml.params.get("amount") if ml else None,
                                       "lock_at": ml.params.get("lock_at") if ml else None,
                                       "lock_applied": self.lock_level() is not None,
                                       "lock_verification": (ml.params.get("lock_verification", ml.verification)
                                                             if ml and ml.params.get("lock_at") is not None else None),
                                       "rule": ml.id if ml else None, "verification": ml.verification if ml else None,
                                       "locked": bool(self.lock_level() is not None and b["official_floor"] is not None
                                                      and b["official_floor"] >= self.lock_level() - EPS)},
                "official_buffer": b["official_buffer"], "uncertainty_reserve": p.internal.uncertainty_reserve,
                "estimated_buffer": b["estimated_buffer"], "internal_safety_reserve": p.internal.safety_reserve,
                "internal_flatten_reserve": p.internal.flatten_reserve,
                "available_risk_budget": (b["estimated_buffer"] - p.internal.safety_reserve)
                if b["estimated_buffer"] is not None else None,
                "daily_loss": ({"limit": dl.params.get("amount"), "day_net": today.get("net"),
                                "used_pct": (max(0.0, -today.get("net", 0.0)) / float(dl.params["amount"]) * 100)
                                if dl.params.get("amount") else None} if dl else
                               {"limit": None, "note": "no daily loss rule in this profile"}),
                "position_limits": {"limit_micro_equivalent": mc.params.get("limit_micro_equivalent") if mc else None,
                                    "weights": mc.params.get("weights") if mc else None,
                                    "current_micro_equivalent": exposure if exposure is not None and math.isfinite(exposure)
                                    else None, "positions": dict(snap.positions) if snap else {}},
                "trading_date": self.current_day, "breached": self.breached, "breach": self.breach,
                "recovery_ok": self.recovery_ok, "recovery_problems": list(self.recovery_problems),
                "consistency": self.consistency_report(snap), "violations": list(self.violations)[-50:],
                "recent_decisions": list(self.history)[-50:], "out_of_order_ignored": self.out_of_order,
                "days": [{"trading_date": d, **v} for d, v in list(self.days.items())[-60:]],
            }
