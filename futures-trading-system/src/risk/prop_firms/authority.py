"""Account safety hierarchy and the unified (read-only) safety decision.

Order of authority (a higher layer's BLOCK can never be overridden by a lower one):

  1. Emergency safety protections  (RiskManager emergency stop, system state, shutdown)
  2. AccountGuard                  (account survival: drawdown floor, flatten buffer, locks)
  3. Prop Firm Compliance Engine   (firm-specific rules of the selected, verified, activated profile)
  4. RiskManager                   (daily limits, size, windows, consecutive losses)
  5. Strategy and AI analysis      (signals, regime gate)
  6. Simulated execution           (PaperBroker; exits and protective orders are never gated)

The layers stay independent authorities: each keeps its own state and its own veto. The unified
decision only REPORTS them together (for the terminal / audits) in that order; it does not replace
the authorization pipeline, which asks each layer in turn (see ``src/paper/authorization.py``).
"""

from __future__ import annotations

from typing import Any

HIERARCHY = ("emergency", "account_guard", "prop_firm", "risk_manager", "strategy", "execution")


def unified_decision(*, emergency: tuple[bool, str], account_guard: str | None, prop_firm: dict[str, Any] | None,
                     risk_manager: tuple[bool, str] | None) -> dict[str, Any]:
    """Combine the layers' CURRENT entry verdicts (computed by each authority) into one report.

    ``prop_firm`` is a :class:`ComplianceDecision` dict or None (no profile selected)."""
    layers = [
        {"layer": "emergency", "allowed": emergency[0], "reason": emergency[1]},
        {"layer": "account_guard", "allowed": account_guard is None, "reason": account_guard or ""},
        {"layer": "prop_firm", "allowed": prop_firm["allowed"] if prop_firm else True,
         "reason": (prop_firm["reasons"][0] if prop_firm and prop_firm["reasons"] else ""),
         "selected": prop_firm is not None},
        {"layer": "risk_manager", "allowed": risk_manager[0] if risk_manager else None,
         "reason": risk_manager[1] if risk_manager else "not evaluated"},
    ]
    first = next((x for x in layers if x["allowed"] is False), None)
    return {"entries_allowed": first is None and all(x["allowed"] is not None for x in layers),
            "decided_by": first["layer"] if first else None, "reason": first["reason"] if first else "",
            "layers": layers, "hierarchy": list(HIERARCHY),
            "note": "Protective exits and emergency flattening are never blocked by entry restrictions."}
