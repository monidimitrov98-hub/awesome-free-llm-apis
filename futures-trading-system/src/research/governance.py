"""Human promotion gates for strategy parameter versions (Sprint 11).

States (per ``strategy@version`` AND its registered SHA-256)::

    RESEARCH -> BACKTESTED -> OOS_VALIDATED -> SHADOW_APPROVED -> PAPER_APPROVED
    any state except REJECTED -> SUSPENDED -> (reinstated to the state it was suspended from)
    any state -> REJECTED (final for that version: create a new version instead)

* There is NO live state. Nothing in this module (or anywhere in the system) can approve live trading.
* Every transition is a HUMAN action from the CLI (``run_research.py promote``): it needs the approver's name,
  the first 16 characters of the version's SHA-256 (proving which content was reviewed), a statement, and the
  evidence the target state requires. Research runs NEVER promote anything; there is no automatic promotion.
* Evidence is checked, hashed and recorded with the transition:
  - BACKTESTED: a research run manifest of exactly this version (purpose full / train / validation /
    walk_forward / purged_cv) produced by ``run_research.py``.
  - OOS_VALIDATED: a one-time OOS run manifest of exactly this version whose reveal is present in the
    validation ledger, with evidence label "POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE". Anything else
    (inconclusive, insufficient, negative) cannot be called validated.
  - SHADOW_APPROVED: a Monte Carlo report of this version and the run's compliance coverage (both read).
  - PAPER_APPROVED: a shadow session report (``run_paper.py --report``) of a SHADOW session of this version.
* A version must be REGISTERED and unmodified (src.research.params refuses anything else).
* The store is an append-only hash-chained JSON-lines file (``data/research/promotions.jsonl``); a broken
  store approves nothing (every version is then treated as RESEARCH-only and gated uses are refused).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT, ConfigError
from src.research.chain import ChainError, ChainStore

GOVERNANCE_FILE = PROJECT_ROOT / "data" / "research" / "promotions.jsonl"
STATES = ("RESEARCH", "BACKTESTED", "OOS_VALIDATED", "SHADOW_APPROVED", "PAPER_APPROVED", "REJECTED", "SUSPENDED")
ORDER = ("RESEARCH", "BACKTESTED", "OOS_VALIDATED", "SHADOW_APPROVED", "PAPER_APPROVED")
FORWARD = {"RESEARCH": "BACKTESTED", "BACKTESTED": "OOS_VALIDATED", "OOS_VALIDATED": "SHADOW_APPROVED",
           "SHADOW_APPROVED": "PAPER_APPROVED"}
FORBIDDEN = re.compile(r"live", re.IGNORECASE)
_NAME = re.compile(r"^[\w .\-]{2,80}$")
BACKTEST_PURPOSES = {"full", "train", "validation", "walk_forward", "purged_cv"}
POSITIVE = "POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE"


class GovernanceError(ConfigError):
    """A promotion request was refused."""


def store(path: Path | None = None) -> ChainStore:
    return ChainStore(Path(path or GOVERNANCE_FILE), kinds=("transition",))


def history(strategy: str, version: str, sha256: str, path: Path | None = None) -> list[dict[str, Any]]:
    return [r for r in store(path).records()
            if r["strategy"] == strategy and r["version"] == version and r["version_sha256"] == sha256]


def state(strategy: str, version: str, sha256: str, path: Path | None = None) -> dict[str, Any]:
    """Current state. Integrity failure of the store -> state RESEARCH with ``integrity_ok`` False (fail closed:
    nothing that needs an approval is allowed)."""
    try:
        hist = history(strategy, version, sha256, path)
    except ChainError as exc:
        return {"state": "RESEARCH", "integrity_ok": False, "error": str(exc), "history": []}
    cur, before_suspension = "RESEARCH", None
    for r in hist:
        if r["to"] == "SUSPENDED":
            before_suspension = cur
        cur = r["to"]
    return {"state": cur, "integrity_ok": True, "suspended_from": before_suspension if cur == "SUSPENDED" else None,
            "history": hist}


def allows(strategy: str, version: str, sha256: str, use: str, path: Path | None = None) -> tuple[bool, str]:
    """May this exact version be used for ``use`` ("shadow" or "paper")? Live is never allowed."""
    if use not in ("shadow", "paper"):
        return False, f"use {use!r} is not governed here (live trading is never approved by this system)"
    st = state(strategy, version, sha256, path)
    if not st["integrity_ok"]:
        return False, f"promotion store failed its integrity check: {st['error']}"
    need = ("SHADOW_APPROVED", "PAPER_APPROVED") if use == "shadow" else ("PAPER_APPROVED",)
    if st["state"] not in need:
        return False, f"{strategy}@{version} is {st['state']}; {use} needs {' or '.join(need)} (human promotion)"
    return True, st["state"]


def _load_json(p: Path, what: str) -> dict[str, Any]:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"{what} {p} cannot be read: {exc}") from exc


def _verified_run(p: Path | None, what: str) -> tuple[Path, dict[str, Any]]:
    """A run folder whose manifest and files still match their recorded hashes (an edited manifest -- e.g. a
    changed evidence label -- or an edited trade file is refused)."""
    from src.research.runs import verify_manifest

    if not p:
        raise GovernanceError(f"{what} is required")
    folder = Path(p).parent if Path(p).name == "manifest.json" else Path(p)
    v = verify_manifest(folder)
    if not v["ok"]:
        raise GovernanceError(f"{what} {folder} failed verification: {'; '.join(v['problems'])}")
    return folder, _load_json(folder / "manifest.json", what)


def _check_manifest(m: dict[str, Any], strategy: str, version: str, sha: str, what: str) -> None:
    ident = m.get("strategy") or {}
    if ident.get("id") != strategy or ident.get("version") != version or ident.get("sha256") != sha:
        raise GovernanceError(f"{what} is not a run of {strategy}@{version} ({sha[:12]}): found "
                              f"{ident.get('id')}@{ident.get('version')} ({str(ident.get('sha256'))[:12]})")
    if not ident.get("registered"):
        raise GovernanceError(f"{what} was produced from an unregistered version")
    if ident.get("overrides"):
        raise GovernanceError(f"{what} used parameter overrides (a derived configuration, not this version)")


def _evidence(target: str, strategy: str, version: str, sha: str, ev: dict[str, Path | None],
              ledger_path: Path | None) -> list[dict[str, Any]]:
    from src.backtest.manifest import file_sha256

    out = []
    if target == "BACKTESTED":
        folder, m = _verified_run(ev.get("run"), "BACKTESTED needs --run <research run folder>")
        _check_manifest(m, strategy, version, sha, "the run")
        if m.get("purpose") not in BACKTEST_PURPOSES:
            raise GovernanceError(f"the run's purpose {m.get('purpose')!r} is not a backtest purpose")
        out.append({"type": "research_run", "path": str(folder), "manifest_sha256": m["manifest_sha256"],
                    "purpose": m["purpose"], "evidence": (m.get("evaluation") or {}).get("evidence")})
    elif target == "OOS_VALIDATED":
        folder, m = _verified_run(ev.get("run"), "OOS_VALIDATED needs --run <one-time OOS run folder>")
        _check_manifest(m, strategy, version, sha, "the OOS run")
        if m.get("purpose") != "oos":
            raise GovernanceError("the run is not a one-time out-of-sample reveal (purpose oos)")
        rec_hash = (m.get("oos") or {}).get("ledger_record_hash")
        from src.backtest.validation import ValidationLedger, resolve_ledger_path

        lp, _ = resolve_ledger_path(ledger_path)
        recs = [r for r in ValidationLedger(lp).records()
                if r.get("record_hash") == rec_hash and r.get("type") == "oos_reveal"]
        if not recs:
            raise GovernanceError("the OOS reveal of this run is not in the validation ledger")
        rec = recs[0]
        if rec.get("strategy") != strategy or rec.get("version") != version or rec.get("version_sha256") != sha:
            raise GovernanceError("the ledger record of this reveal belongs to another configuration")
        if rec.get("results_fingerprint") != m.get("results_fingerprint"):
            raise GovernanceError("the run's results do not match the results recorded in the ledger at reveal time")
        hold = (m.get("oos") or {}).get("holdout")
        if hold:                             # Sprint 13 P1.1: a holdout result is valid only through its COMPLETED claim
            from src.research.holdout import completed_record

            if not resolve_ledger_path(ledger_path)[1]:
                raise GovernanceError("a holdout result is checked against the canonical validation ledger only")
            done = completed_record(hold.get("claim_id", ""))
            if done is None or done["results"].get("results_fingerprint") != m.get("results_fingerprint") \
                    or done["results"].get("run_manifest_sha256") != m["manifest_sha256"]:
                raise GovernanceError("the holdout claim of this run is not COMPLETED with these exact results")
        label = rec.get("evidence")          # from the hash-chained ledger, never from an editable file
        if label != POSITIVE:
            raise GovernanceError(f"the OOS evidence is {label!r}: only {POSITIVE!r} can be called validated "
                                  "(record REJECTED, or keep researching with a NEW version and NEW data)")
        out.append({"type": "oos_run", "path": str(folder), "manifest_sha256": m["manifest_sha256"],
                    "ledger_record_hash": rec_hash, "evidence": label})
    elif target == "SHADOW_APPROVED":
        p = ev.get("monte_carlo")
        if not p:
            raise GovernanceError("SHADOW_APPROVED needs --monte-carlo <report.json> and --run <run folder whose "
                                  "compliance coverage was reviewed>")
        folder, m = _verified_run(ev.get("run"), "SHADOW_APPROVED needs --run <run folder>")
        _check_manifest(m, strategy, version, sha, "the run")
        mc = _load_json(Path(p), "Monte Carlo report")
        if (mc.get("strategy") or {}).get("sha256") != sha:
            raise GovernanceError("the Monte Carlo report is not of this exact version")
        if (mc.get("run") or {}).get("manifest_sha256") != m["manifest_sha256"]:
            raise GovernanceError("the Monte Carlo report was not produced from this (verified) run")
        hold = (m.get("oos") or {}).get("holdout")
        if hold:                             # only the frozen, one-time final-evaluation Monte Carlo of the claim
            from src.research.holdout import completed_record

            done = completed_record(hold.get("claim_id", ""))
            want = None if done is None else done["results"].get("monte_carlo_sha256")
            if not want or file_sha256(Path(p)) != want:
                raise GovernanceError("for a holdout run only the Monte Carlo report frozen in the plan and recorded in "
                                      "its COMPLETED claim is accepted")
        cov = m.get("compliance_coverage")
        out.append({"type": "monte_carlo", "path": str(p), "sha256": file_sha256(Path(p)),
                    "mc_manifest_sha256": (mc.get("manifest") or {}).get("sha256")})
        out.append({"type": "compliance_coverage", "path": str(folder), "manifest_sha256": m["manifest_sha256"],
                    "verdict": (cov or {}).get("verdict", "NO PROP PROFILE IN THIS RUN")})
    elif target == "PAPER_APPROVED":
        p = ev.get("shadow_report")
        if not p:
            raise GovernanceError("PAPER_APPROVED needs --shadow-report <run_paper.py --report JSON of a SHADOW "
                                  "session of this version>")
        rep = _load_json(Path(p), "shadow report")
        sess = rep.get("session") or {}
        strat = sess.get("strategy") or {}
        if sess.get("session_type") != "SHADOW" or strat.get("sha256") != sha or strat.get("id") != strategy:
            raise GovernanceError("the report is not a SHADOW session of this exact version")
        out.append({"type": "shadow_report", "path": str(p), "sha256": file_sha256(Path(p))})
    return out


def transition(strategy: str, version: str, app, *, to: str, approved_by: str, confirm_sha: str, statement: str,
               evidence: dict[str, Path | None] | None = None, path: Path | None = None,
               ledger_path: Path | None = None) -> dict[str, Any]:
    """HUMAN action (CLI only). Validates the request, the version and the evidence, then appends a record."""
    from src.research.params import load_version

    if FORBIDDEN.search(to or ""):
        raise GovernanceError("refused: there is no live state; live trading is never approved by this system")
    if to not in STATES or to == "RESEARCH":
        raise GovernanceError(f"refused: unknown target state {to!r}; one of {[s for s in STATES if s != 'RESEARCH']}")
    pv = load_version(strategy, version, app)          # registered and unmodified, or raises
    if confirm_sha != pv.sha256[:16]:
        raise GovernanceError("refused: --confirm must be the first 16 characters of the version SHA-256 "
                              "(shown by `run_research.py versions`), proving the reviewed content")
    if not _NAME.match(approved_by or ""):
        raise GovernanceError("refused: --approved-by must name the human approver")
    if len((statement or "").strip()) < 10:
        raise GovernanceError("refused: --statement must explain the decision")
    st = state(strategy, version, pv.sha256, path)
    if not st["integrity_ok"]:
        raise GovernanceError(f"refused: promotion store integrity failure: {st['error']}")
    cur = st["state"]
    ev_rec: list[dict[str, Any]] = []
    if cur == "REJECTED":
        raise GovernanceError(f"refused: {strategy}@{version} is REJECTED (final); create a new version")
    if to == "REJECTED":
        pass
    elif to == "SUSPENDED":
        if cur == "SUSPENDED":
            raise GovernanceError("refused: already SUSPENDED")
    elif cur == "SUSPENDED":
        if to != st["suspended_from"]:
            raise GovernanceError(f"refused: a SUSPENDED version can only be reinstated to {st['suspended_from']}")
    else:
        if FORWARD.get(cur) != to:
            raise GovernanceError(f"refused: {cur} -> {to} is not allowed (next state: {FORWARD.get(cur)}); no step "
                                  "can be skipped")
        ev_rec = _evidence(to, strategy, version, pv.sha256, evidence or {}, ledger_path)
    return store(path).append({"kind": "transition", "strategy": strategy, "version": version,
                               "version_sha256": pv.sha256, "from": cur, "to": to, "approved_by": approved_by,
                               "statement": statement.strip()[:1000], "evidence": ev_rec})


def overview(app, path: Path | None = None) -> list[dict[str, Any]]:
    """Read-only: every version of every registered strategy with its promotion state."""
    from src.research.params import list_versions
    from src.research.registry import REGISTRY

    rows = []
    for sid in sorted(REGISTRY):
        for v in list_versions(sid):
            st = state(sid, v["version"], v["sha256"], path)
            rows.append({**v, "promotion_state": st["state"], "integrity_ok": st["integrity_ok"],
                         "transitions": len(st["history"])})
    return rows


__all__ = ["state", "allows", "transition", "history", "overview", "STATES", "GOVERNANCE_FILE", "GovernanceError"]
