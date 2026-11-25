"""READ-ONLY research views for the terminal (Sprint 11): registry, versions, promotion states, experiments,
OOS reveals, research runs, Monte Carlo summaries and compliance coverage.

Nothing here writes, promotes, registers, reveals or runs anything. Runs are found by scanning manifests under
the research output folder and are addressed by their attempt id (16 hex characters) -- never by a path
supplied by the client.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT

RESEARCH_ROOT = PROJECT_ROOT / "output" / "research"
_ATTEMPT = re.compile(r"^[0-9a-f]{16}$")
MAX_RUNS = 500
LIMITATIONS = [
    "Research results are historical simulations; none is evidence of future profitability.",
    "VWAP Mean Reversion V1 is a RESEARCH candidate: confirmation methods C1-C4 and the stop methods are "
    "experimental; the user's original confirmation and stop rules are not known.",
    "FundedNext profiles 2026-11-25.1 are UNVERIFIED and non-operational: research runs with them block every "
    "entry (fail closed) and their compliance coverage is INCOMPLETE -- RESEARCH ONLY.",
    "Promotion is a human action from run_research.py; there is no live state and no automatic promotion.",
    "This page is read-only: it cannot run research, reveal out-of-sample data, promote or trade.",
]


def _manifests(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    out = []
    if not root.is_dir():
        return out
    for mp in sorted(root.glob("*/*/*/*/manifest.json"))[-MAX_RUNS:]:
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("kind") == "research_run" and _ATTEMPT.match(str(m.get("attempt", ""))):
            out.append((mp.parent, m))
    return out


def _mc_summary(folder: Path) -> list[dict[str, Any]]:
    rows = []
    for p in sorted(folder.glob("monte_carlo_*.json"))[:10]:
        try:
            mc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for method, scen in (mc.get("methods") or {}).items():
            base = scen.get("as_backtested") or {}
            stress = scen.get("stress_+2_ticks_commission_x2") or {}
            rows.append({"file": p.name, "method": method, "paths": base.get("paths"),
                         "prob_final_negative": base.get("prob_final_pnl_negative"),
                         "max_dd_p50": (base.get("max_drawdown") or {}).get("p50"),
                         "max_dd_p95": (base.get("max_drawdown") or {}).get("p95"),
                         "risk_of_ruin": base.get("risk_of_ruin"),
                         "prob_final_negative_stress": stress.get("prob_final_pnl_negative"),
                         "prop_breach": base.get("prop_breach_probability"),
                         "prop_label": (mc.get("prop") or {}).get("label"), "seed": (mc.get("manifest") or {}).get("seed")})
    return rows


def overview(app, *, root: Path | None = None, governance_path: Path | None = None,
             ledger_path: Path | None = None, experiments_path: Path | None = None) -> dict[str, Any]:
    from src.research import governance
    from src.research.experiments import ExperimentRegistry
    from src.research.registry import REGISTRY

    out: dict[str, Any] = {"strategies": [s.info() for s in REGISTRY.values()], "limitations": LIMITATIONS,
                           "read_only": True}
    try:
        out["versions"] = governance.overview(app, governance_path)
    except Exception as exc:  # noqa: BLE001 -- a view never fails the page
        out["versions"], out["versions_error"] = [], type(exc).__name__
    try:
        att = ExperimentRegistry(experiments_path).attempts()
        out["experiments"] = {"total": len(att), "failed": sum(a["status"] in ("failed", "refused") for a in att),
                              "unfinished": sum(a["status"] == "unfinished" for a in att),
                              "recent": [{k: a[k] for k in ("attempt", "at", "strategy", "version", "instrument",
                                                            "purpose", "status", "period")} | {
                                  "evidence": a["summary"].get("evidence"), "registered": a["registered"],
                                  "error": a["error"][:200]} for a in att[-50:]][::-1],
                              "integrity_ok": True}
    except Exception as exc:  # noqa: BLE001
        out["experiments"] = {"total": None, "recent": [], "integrity_ok": False, "error": str(exc)[:200]}
    try:
        from src.backtest.validation import ValidationLedger, resolve_ledger_path

        lp, _ = resolve_ledger_path(ledger_path)
        recs = ValidationLedger(lp).records()
        out["oos"] = {"ledger_ok": True, "reveals": [
            {k: r.get(k) for k in ("seq", "time", "instrument", "start", "end", "strategy", "version", "evidence",
                                   "source")} for r in recs if r.get("type") == "oos_reveal"]}
    except Exception as exc:  # noqa: BLE001
        out["oos"] = {"ledger_ok": False, "reveals": [], "error": str(exc)[:200]}
    runs = []
    for folder, m in _manifests(Path(root or RESEARCH_ROOT)):
        ev = m.get("evaluation") or {}
        runs.append({"attempt": m["attempt"], "strategy": m["strategy"]["id"], "version": m["strategy"]["version"],
                     "overrides": m["strategy"].get("overrides"), "registered": m["strategy"].get("registered"),
                     "purpose": m.get("purpose"), "instrument": m.get("instrument"), "period": m.get("period"),
                     "synthetic": m.get("synthetic_data"), "evidence": ev.get("evidence"),
                     "trades": (ev.get("sample") or {}).get("trades"), "mean_daily": ev.get("mean_daily_pnl"),
                     "mean_daily_ci95": ev.get("mean_daily_pnl_ci95_day_bootstrap"),
                     "p_holm": (ev.get("tests") or {}).get("p_holm"),
                     "family_size": (ev.get("tests") or {}).get("family_size"),
                     "compliance": (m.get("compliance_coverage") or {}).get("verdict") or "NO PROP PROFILE",
                     "code_trusted": (m.get("code") or {}).get("trusted"),
                     "monte_carlo": bool(list(folder.glob("monte_carlo_*.json")))})
    out["runs"] = runs[::-1]
    return out


def run_detail(attempt: str, *, root: Path | None = None) -> dict[str, Any] | None:
    if not _ATTEMPT.match(attempt or ""):
        return None
    for folder, m in _manifests(Path(root or RESEARCH_ROOT)):
        if m["attempt"] != attempt:
            continue
        from src.research.runs import verify_manifest

        v = verify_manifest(folder)
        try:
            ev = json.loads((folder / "evaluation.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ev = None
        cov = None
        if (folder / "compliance_coverage.json").is_file():
            try:
                cov = json.loads((folder / "compliance_coverage.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cov = None
        return {"manifest": {k: m.get(k) for k in ("attempt", "purpose", "strategy", "instrument", "period", "data",
                                                   "config", "code", "prop", "cost_sensitivity", "oos",
                                                   "results_fingerprint", "synthetic_data", "disclaimer")},
                "integrity": {"ok": v["ok"], "problems": v["problems"]}, "evaluation": ev,
                "compliance_coverage": cov, "monte_carlo": _mc_summary(folder)}
    return None


__all__ = ["overview", "run_detail", "RESEARCH_ROOT", "LIMITATIONS"]
