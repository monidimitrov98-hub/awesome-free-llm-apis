"""READ-ONLY payout eligibility tracker.

Evaluates the profile's payout rules against the account's RECORDED trading days. It never submits,
requests or promises a payout: the firm decides. Overall states:

* RULES SATISFIED                     every applicable, VERIFIED payout rule is met by recorded data
* RULES NOT SATISFIED                 at least one rule is not met
* DATA INCOMPLETE                     a rule cannot be computed from recorded data
* MANUAL FIRM CONFIRMATION REQUIRED   a rule is not verified, or the profile is not operational

Even "RULES SATISFIED" only means the recorded simulation meets the rules as configured; the firm
applies its own records, reviews and terms.
"""

from __future__ import annotations

import math
from typing import Any

from src.risk.prop_firms.engine import ComplianceEngine

SATISFIED, NOT_SATISFIED, INCOMPLETE, MANUAL = ("RULES SATISFIED", "RULES NOT SATISFIED", "DATA INCOMPLETE",
                                                "MANUAL FIRM CONFIRMATION REQUIRED")
PAYOUT_TYPES = ("payout_benchmark_days", "payout_min_profit", "payout_min_withdrawal", "payout_max_withdrawal",
                "min_trading_days")


def payout_eligibility(engine: ComplianceEngine, cycle_start: str | None = None) -> dict[str, Any]:
    p = engine.profile
    rules = [r for r in p.rules if r.type in PAYOUT_TYPES]
    applicable = [r for r in rules if r.applies_to(p.stage)]
    if not applicable:
        return {"status": "NOT APPLICABLE", "stage": p.stage,
                "explanation": f"the profile defines no payout rules for the {p.stage} stage",
                "requirements": [], "disclaimer": DISCLAIMER}
    days = {d: v for d, v in engine.days.items() if cycle_start is None or d >= cycle_start}
    profit = sum(float(v.get("net", 0.0)) for v in days.values())
    traded = [d for d, v in days.items() if v.get("executions", 0) > 0]
    reqs = []
    for r in applicable:
        cur = req = None
        status = INCOMPLETE
        prm = r.params
        try:
            if r.type == "payout_benchmark_days":
                need, minp = int(prm["days"]), float(prm["min_day_profit"])
                cur = sum(1 for v in days.values() if float(v.get("net", 0.0)) >= minp - 1e-9)
                req = f"{need} days with net >= {minp:g}"
                status = SATISFIED if cur >= need else NOT_SATISFIED
            elif r.type == "min_trading_days":
                need = int(prm["days"])
                cur, req = len(traded), need
                status = SATISFIED if cur >= need else NOT_SATISFIED
            elif r.type == "payout_min_profit":
                cur, req = profit, float(prm["amount"])
                status = SATISFIED if profit >= req - 1e-9 else NOT_SATISFIED
            elif r.type == "payout_min_withdrawal":
                share = _share(p)
                cap = _cap(p)
                available = min(profit * share, cap) if share is not None and cap is not None and profit > 0 else 0.0
                cur, req = available, float(prm["amount"])
                status = SATISFIED if available >= req - 1e-9 else NOT_SATISFIED
            elif r.type == "payout_max_withdrawal":
                share, cap = float(prm["share_of_profit"]), float(prm["cap"])
                cur = min(max(profit, 0.0) * share, cap)
                req = f"<= {share:.0%} of profit, capped at {cap:g}"
                status = SATISFIED  # a ceiling, not a requirement: shows the maximum the rules would allow
        except (KeyError, TypeError, ValueError):
            status = INCOMPLETE
        if r.verification != "VERIFIED" and status != INCOMPLETE:
            status = MANUAL
        reqs.append({"requirement": r.description or r.id, "rule": r.id, "current": _round(cur), "required": req,
                     "status": status, "sources": [_src(p, s) for s in r.sources],
                     "last_verification": p.verification.get("retrieved") or None,
                     "verification": r.verification, "ambiguity": r.ambiguity})
    states = {x["status"] for x in reqs}
    if INCOMPLETE in states:
        overall = INCOMPLETE
    elif MANUAL in states or not engine.operational.get("operational"):
        overall = MANUAL
    elif NOT_SATISFIED in states:
        overall = NOT_SATISFIED
    else:
        overall = SATISFIED
    return {"status": overall, "stage": p.stage, "cycle_start": cycle_start, "cycle_profit": round(profit, 2),
            "recorded_days": len(days), "requirements": reqs, "profile_operational": engine.operational.get("operational"),
            "disclaimer": DISCLAIMER}


DISCLAIMER = ("Read-only estimate from recorded simulated days. It does not request, submit or promise a payout; "
              "the firm's own records, reviews and terms decide.")


def _share(p) -> float | None:
    r = p.rule("payout_max_withdrawal")
    return float(r.params["share_of_profit"]) if r and "share_of_profit" in r.params else None


def _cap(p) -> float | None:
    r = p.rule("payout_max_withdrawal")
    return float(r.params["cap"]) if r and "cap" in r.params else None


def _src(p, sid: str) -> dict[str, str]:
    for s in p.sources:
        if s.id == sid:
            return {"id": s.id, "title": s.title, "url": s.url, "retrieved": s.retrieved}
    return {"id": sid}


def _round(v):
    return round(v, 2) if isinstance(v, float) and math.isfinite(v) else v
