"""Auditable compliance reports from a paper journal (read-only, chain-verified).

Every row keeps its journal ``seq`` and UTC timestamp, and the profile / rule-set identity that was in
force (from the run identity and the decision stages). Rows are bounded; exports go through the
formula-safe CSV / JSON writers of the analytics package.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from src.analytics.shadow.events import ReadOnlyJournal
from src.paper.journal import JournalError

MAX_ROWS = 20_000


def journal_report(path: Path) -> dict[str, Any]:
    j = ReadOnlyJournal(path)
    try:
        rows = j.db.execute("SELECT seq, ts, kind, payload, prev_hash, hash FROM journal ORDER BY seq").fetchall()
        prev, expected = "GENESIS", 1
        import json

        evaluations, eod, breaches, flattens, account, profiles = [], [], [], [], [], {}
        for seq, ts, kind, payload, ph, h in rows:
            if seq != expected or ph != prev or j._hash(ph, seq, ts, kind, payload) != h:
                raise JournalError(f"journal chain broken at seq {seq}")
            prev, expected = h, seq + 1
            if kind not in ("entry_decision", "fill_authorization", "prop_firm_eod", "prop_firm_breach",
                            "flatten_requested", "account_update", "run_identity"):
                continue
            p = json.loads(payload)
            if kind == "run_identity" and p.get("prop_firm"):
                profiles[p["prop_firm"]["profile"]] = p["prop_firm"]
            elif kind in ("entry_decision", "fill_authorization"):
                st = next((s for s in p.get("stages") or [] if s.get("stage") == "prop_firm"), None)
                if st is not None:
                    evaluations.append({"seq": seq, "time": ts, "scope": "pre_trade" if kind == "entry_decision" else "fill",
                                        "root": p.get("root"), "allowed": st.get("ok"), "reason": st.get("reason"),
                                        "reasons": st.get("reasons"), "profile": st.get("profile"),
                                        "ruleset": st.get("ruleset"), "official_buffer": st.get("official_buffer"),
                                        "estimated_buffer": st.get("estimated_buffer"), "state": st.get("state")})
            elif kind == "prop_firm_eod":
                eod.append({"seq": seq, "time": ts, **p})
            elif kind == "prop_firm_breach":
                breaches.append({"seq": seq, "time": ts, **p})
            elif kind == "flatten_requested" and str(p.get("reason", "")).startswith("prop_firm"):
                flattens.append({"seq": seq, "time": ts, "root": p.get("root"), "reason": p.get("reason")})
            elif kind == "account_update":
                account.append({"seq": seq, "time": ts, "equity": p.get("equity"),
                                "net_realized": p.get("net_realized"), "unrealized": p.get("unrealized")})
    finally:
        j.close()
    blocks = [e for e in evaluations if e["allowed"] is False]
    return {"profiles": profiles,
            "counts": {"evaluations": len(evaluations), "blocks": len(blocks), "eod": len(eod), "breaches": len(breaches),
                       "protective_flattens": len(flattens)},
            "block_reasons": dict(Counter(e["reason"] for e in blocks).most_common()),
            "evaluations": evaluations[-MAX_ROWS:], "drawdown_history": eod[-MAX_ROWS:], "breaches": breaches,
            "protective_flattens": flattens[-MAX_ROWS:], "account_history": account[-MAX_ROWS:],
            "timezone": "all times UTC (ISO 8601)"}


def rows_for_export(rep: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    key = {"evaluations": "evaluations", "drawdown": "drawdown_history", "breaches": "breaches",
           "flattens": "protective_flattens", "account": "account_history"}[kind]
    return list(rep.get(key) or [])[:MAX_ROWS]
