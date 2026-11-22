"""Validation status of the analysed evidence (read-only checks; nothing is revealed or unsealed).

Checks:
* each journal's hash chain verified (unreadable or broken journals are excluded and listed);
* a journal copied to a second location is the same run: it is analysed once (flagged);
* session types are never mixed (a journal with several types is flagged; reports require one type);
* strategy identity per run: a configuration change inside a run splits the evidence (flagged);
* code identity: results from uncommitted / unknown code are flagged as not reproducible;
* TEST strategies (terminal end-to-end hooks) are flagged: their results say nothing about the strategy;
* chronology: shadow sessions dated after every replay session are forward (out-of-sample) evidence
  relative to that replay data; overlaps are flagged;
* the Sprint 3 out-of-sample ledger (read-only): replay periods overlapping REVEALED out-of-sample periods are
  listed; sealed (never revealed) periods are unknown to this module and are never inferred or revealed;
* look-ahead: decisions use only journaled, decision-time values; MFE/MAE and counterfactual outcomes are
  computed AFTER the fact from later bars and are labelled ex-post / hypothetical.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.analytics.shadow.events import JournalSource


def validation_status(srcs: list[JournalSource], tables: dict[str, list[dict]], errors: dict[str, str],
                      duplicates: dict[str, str] | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    add("journal integrity", "FAIL" if errors else "PASS",
        {"verified_runs": len(srcs), "excluded": errors} if errors else f"{len(srcs)} journal(s) chain-verified")
    add("duplicate journals", "WARN" if duplicates else "PASS", duplicates or "no run is analysed twice")
    runs = []
    for s in srcs:
        ids = s.data.identities
        types = sorted({i.payload.get("session_type", "UNKNOWN") for i in ids})
        versions = sorted({(i.payload.get("strategy") or {}).get("version") for i in ids} - {None})
        codes = [i.payload.get("code") or {} for i in ids]
        test = any(i.payload.get("test_strategy") for i in ids)
        runs.append({"run": s.label, "run_id": s.data.run_id, "session_types": types or ["UNKNOWN (no run identity)"],
                     "strategy_versions": versions, "code_trusted": all(c.get("trusted") for c in codes) if codes else None,
                     "code_commits": sorted({c.get("commit") for c in codes} - {None}), "test_strategy": test,
                     "first": s.data.first_ts, "last": s.data.last_ts, "rows_verified": s.data.rows})
    mixed = [r["run"] for r in runs if len(r["session_types"]) > 1]
    add("session types not mixed", "WARN" if mixed else "PASS",
        f"journals with several session types: {mixed}" if mixed else "each journal has one session type")
    no_ident = [r["run"] for r in runs if r["session_types"] == ["UNKNOWN (no run identity)"]]
    add("run identity recorded", "WARN" if no_ident else "PASS",
        f"journals without run identity (pre-Sprint-8): {no_ident}; their results cannot be attributed to a strategy "
        "version" if no_ident else "every journal records its strategy / code / regime identity")
    changed = [r["run"] for r in runs if len(r["strategy_versions"]) > 1]
    add("strategy identity stable within runs", "WARN" if changed else "PASS",
        f"configuration changed inside: {changed} (evidence split by version)" if changed else "no in-run changes")
    untrusted = [r["run"] for r in runs if r["code_trusted"] is False]
    add("code reproducibility", "WARN" if untrusted else "PASS",
        f"runs from uncommitted or unknown code: {untrusted}" if untrusted else "all runs from committed code")
    tests = [r["run"] for r in runs if r["test_strategy"]]
    add("production strategy only", "WARN" if tests else "PASS",
        f"TEST-strategy runs (not evidence about the real strategy): {tests}" if tests else "no test strategies")
    by_type = defaultdict(set)
    for t in tables.get("sessions", []):
        by_type[t["session_type"]].add(t["trading_date"])
    rep, sh = by_type.get("REPLAY", set()), by_type.get("SHADOW", set())
    if rep and sh:
        overlap = rep & sh
        status = "WARN" if overlap or min(sh) <= max(rep) else "PASS"
        add("chronology replay vs shadow", status,
            f"shadow {min(sh)}..{max(sh)}, replay {min(rep)}..{max(rep)}"
            + (f"; overlapping dates {sorted(overlap)[:10]}" if overlap else "")
            + ("" if status == "PASS" else "; shadow evidence is not strictly after the replay data"))
    else:
        add("chronology replay vs shadow", "N/A", "needs both REPLAY and SHADOW sessions")
    add("out-of-sample ledger", *_oos(tables))
    add("look-ahead", "PASS (by construction)",
        "decision analytics use journaled decision-time values only; MFE/MAE and blocked-signal outcomes are ex-post "
        "(after the event) and labelled as such")
    worst = "FAIL" if any(c["status"] == "FAIL" for c in checks) else (
        "WARN" if any(c["status"] == "WARN" for c in checks) else "PASS")
    return {"overall": worst, "checks": checks, "runs": runs,
            "note": "Passing checks do not prove an edge: they only mean the evidence is not obviously contaminated."}


def _oos(tables) -> tuple[str, Any]:
    try:
        from src.backtest.validation import LedgerError, ValidationLedger, resolve_ledger_path

        path, _ = resolve_ledger_path(None)
        led = ValidationLedger(path)
        if not led.exists():
            return "N/A", "no out-of-sample ledger (nothing revealed yet); sealed periods are not known here"
        records = led.records()
    except (LedgerError, OSError) as exc:
        return "WARN", f"ledger unreadable: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001
        return "WARN", f"ledger check failed: {type(exc).__name__}"
    reveals = [r for r in records if r.get("type") == "oos_reveal"]
    hits = []
    for s in tables.get("sessions", []):
        if s["session_type"] != "REPLAY":
            continue
        for r in reveals:
            if r.get("instrument") == s["instrument"] and str(r.get("start"))[:10] <= s["trading_date"] <= str(r.get("end"))[:10]:
                hits.append((s["instrument"], s["trading_date"]))
    if hits:
        return "WARN", f"replay sessions inside revealed OOS periods: {sorted(set(hits))[:10]}"
    return "PASS", f"{len(reveals)} revealed OOS period(s); no replay session overlaps them"
