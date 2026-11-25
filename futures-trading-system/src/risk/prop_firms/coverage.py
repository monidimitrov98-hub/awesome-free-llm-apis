"""Compliance coverage of a simulation run (Sprint 11): which of the selected profile's rules were applied,
which were verified, which could not be evaluated, and which restrictions cannot be simulated at all.

A run's compliance coverage is COMPLETE only when the profile is operational (VERIFIED + human-activated) and
every applicable mandatory rule is verified AND evaluated in this environment. Otherwise it is
"INCOMPLETE -- RESEARCH ONLY": the run may still be useful research, but it says nothing definitive about
compliance with the firm's rules. Passing tests never establish real-world compliance either.
"""

from __future__ import annotations

from typing import Any

EVALUATED = {"max_loss", "daily_loss", "max_contracts", "instrument_allowlist", "flat_by", "trading_hours",
             "holiday_schedule", "news_restriction", "consistency"}
INFORMATIONAL = {"profit_target", "min_trading_days", "payout_benchmark_days", "payout_min_profit",
                 "payout_min_withdrawal", "payout_max_withdrawal"}


def coverage_report(engine, *, environment: str, news_guard: bool) -> dict[str, Any]:
    p = engine.profile
    rows = []
    for r in p.rules:
        if not r.applicable:
            status = "NOT_APPLICABLE"
            why = "the firm states this restriction does not exist for the product"
        elif not r.applies_to(p.stage):
            status, why = "OTHER_STAGE", f"applies to {list(r.stages)}, profile stage is {p.stage}"
        elif r.type in EVALUATED:
            status, why = "APPLIED", f"evaluated by the compliance engine in the {environment}"
            if r.type == "holiday_schedule" and engine._holidays(str(r.params.get("calendar", ""))) is None:
                status, why = "UNAVAILABLE", "no firm holiday / early-close schedule file: entries fail closed"
            elif r.type == "news_restriction" and not news_guard:
                status, why = "UNAVAILABLE", "no NewsGuard in this run: entries fail closed"
            elif r.type == "consistency":
                why += " (reported as a warning; it changes the profit target, it never blocks)"
        elif r.type in INFORMATIONAL:
            status, why = "REPORTED_NOT_ENFORCED", "target / payout information; nothing to enforce on entries"
        else:
            status, why = "NOT_SIMULATABLE", r.ambiguity or r.description or "no evaluator for this rule type"
        if r.type == "max_loss" and status == "APPLIED" and r.params.get("lock_at") is not None \
                and engine.lock_level() is None:
            why += f"; lock at {r.params['lock_at']} NOT applied (lock {r.params.get('lock_verification', r.verification)})"
        rows.append({"rule": r.id, "type": r.type, "status": status, "verification": r.verification,
                     "mandatory": r.mandatory, "safety_critical": r.safety_critical, "detail": why})
    op = engine.operational or {}
    issues = []
    if not op.get("operational"):
        issues.append(f"profile not operational: {op.get('not_operational_reason') or op.get('status')}")
    for x in rows:
        if x["status"] == "APPLIED" and x["verification"] != "VERIFIED" and x["mandatory"]:
            issues.append(f"{x['rule']} applied but {x['verification']}")
        if x["status"] == "UNAVAILABLE" and x["mandatory"]:
            issues.append(f"{x['rule']} unavailable in this {environment}")
        if x["status"] == "NOT_SIMULATABLE" and x["safety_critical"]:
            issues.append(f"{x['rule']} cannot be simulated ({x['verification']})")
    return {
        "environment": environment, "profile": p.key, "ruleset_sha256": p.ruleset_sha256,
        "rules": rows,
        "applied": [x["rule"] for x in rows if x["status"] == "APPLIED"],
        "verified": [x["rule"] for x in rows if x["verification"] == "VERIFIED"],
        "unavailable": [x["rule"] for x in rows if x["status"] == "UNAVAILABLE"],
        "not_simulatable": [x["rule"] for x in rows if x["status"] == "NOT_SIMULATABLE"],
        "issues": issues,
        "verdict": "COMPLETE FOR SUPPORTED RULES" if not issues else "INCOMPLETE -- RESEARCH ONLY",
        "note": "Simulation coverage only. It does not establish compliance with the firm's real, current rules.",
    }
