"""Conflict register for datasets that disagree on the same contract minutes (Sprint 13 P0).

Two versions of the same contract's history can disagree, for example two exports of the same NinjaTrader
contract made on different days. Conflicting bars are NEVER merged and neither version is changed:

* :func:`compare_datasets` lists every minute of a common contract, inside the period both datasets cover,
  that is present in only one of them or whose OHLCV differs. The report is deterministic (the pair is put in
  reference order) and identified by its SHA-256.
* :class:`ConflictRegister` records a report as a CONFLICT (hash-chained ``conflicts.jsonl`` next to the dataset
  registry, with the full report kept read-only under ``_conflicts/``). Both datasets are then refused for
  research, plans and continuous series until the conflict is resolved.
* A conflict is resolved only by a human SUPERSEDED record: one of the two datasets is declared superseded
  by the other, with a name and a reason. The superseded dataset stays refused for good; the other one is
  usable again. Nothing is deleted, merged or downgraded.

The register is a separate chained file, so the dataset registry keeps its record kinds and older code keeps
reading it. Like every store here it is tamper-evident, not tamper-proof: a modified or missing report file
makes every affected dataset refuse to load (fail closed), but deleting the whole register is only visible from
backups.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pandas as pd

from src.backtest.manifest import file_sha256, text_sha256
from src.config import ConfigError
from src.research.chain import ChainStore

CONFLICTS_FILE = "conflicts.jsonl"
REPORTS_DIR = "_conflicts"
REPORT_VERSION = 1
COLUMNS = ("open", "high", "low", "close", "volume")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _side(ds) -> dict[str, Any]:
    return {"ref": ds.ref, "manifest_sha256": ds.manifest["manifest_sha256"], "status": ds.status,
            "provider": ds.manifest["provider"]["adapter"],
            "source_files": [{"name": s["name"], "sha256": s["sha256"]} for s in ds.manifest.get("source_files", [])]}


def _bar(row) -> dict[str, float]:
    return {c: float(row[c]) for c in COLUMNS}


def compare_datasets(a, b) -> dict[str, Any]:
    """Minute-by-minute comparison of the contracts two per-contract datasets have in common."""
    if a.ref == b.ref:
        raise ConfigError("a dataset cannot conflict with itself")
    a, b = sorted((a, b), key=lambda d: d.ref)
    for d in (a, b):
        if d.manifest["kind"] != "per_contract" or d.manifest["adjustment"] != "none":
            raise ConfigError(f"{d.ref}: conflicts are recorded between unadjusted per-contract datasets only")
    if a.manifest["resolution"] != b.manifest["resolution"]:
        raise ConfigError(f"{a.ref} and {b.ref} have different resolutions")
    common = sorted(set(a.manifest["contracts"]) & set(b.manifest["contracts"]))
    if not common:
        raise ConfigError(f"{a.ref} and {b.ref} have no contract in common")
    ba, bb = a.bars(), b.bars()
    per_contract, conflicts, only_a, only_b = {}, [], [], []
    for c in common:
        xa = ba[ba["contract"] == c].set_index("ts_utc")[list(COLUMNS)]
        xb = bb[bb["contract"] == c].set_index("ts_utc")[list(COLUMNS)]
        start, end = max(xa.index.min(), xb.index.min()), min(xa.index.max(), xb.index.max())
        if start > end:
            per_contract[c] = {"overlap": None, "common_minutes": 0, "only_in_a": 0, "only_in_b": 0, "differing": 0}
            continue
        wa, wb = xa.loc[start:end], xb.loc[start:end]
        both = wa.index.intersection(wb.index)
        oa, ob = wa.index.difference(wb.index), wb.index.difference(wa.index)
        same = (wa.loc[both] == wb.loc[both]).all(axis=1)
        diff = list(both[~same.to_numpy()])
        for t in diff:
            fields = [k for k in COLUMNS if wa.loc[t, k] != wb.loc[t, k]]
            conflicts.append({"contract": c, "ts_utc": pd.Timestamp(t).isoformat(), "fields": fields,
                              "a": _bar(wa.loc[t]), "b": _bar(wb.loc[t])})
        only_a += [{"contract": c, "ts_utc": pd.Timestamp(t).isoformat(), "a": _bar(wa.loc[t])} for t in oa]
        only_b += [{"contract": c, "ts_utc": pd.Timestamp(t).isoformat(), "b": _bar(wb.loc[t])} for t in ob]
        per_contract[c] = {"overlap": [pd.Timestamp(start).isoformat(), pd.Timestamp(end).isoformat()],
                           "common_minutes": int(len(both)), "only_in_a": int(len(oa)), "only_in_b": int(len(ob)),
                           "differing": len(diff),
                           "volume_a": float(wa["volume"].sum()), "volume_b": float(wb["volume"].sum())}
    report = {
        "kind": "dataset_conflict_report", "version": REPORT_VERSION, "a": _side(a), "b": _side(b),
        "contracts": per_contract,
        "totals": {"differing": len(conflicts), "only_in_a": len(only_a), "only_in_b": len(only_b),
                   "common_minutes": sum(x["common_minutes"] for x in per_contract.values())},
        "conflicts": conflicts, "only_in_a": only_a, "only_in_b": only_b,
        "note": "a = the reference that sorts first. Values are copied from each dataset unchanged; nothing is "
                "merged. Minutes outside the period both datasets cover are not compared.",
    }
    report["report_sha256"] = text_sha256(_canonical(report))
    return report


def has_conflict(report: dict[str, Any]) -> bool:
    t = report["totals"]
    return bool(t["differing"] or t["only_in_a"] or t["only_in_b"])


class ConflictRegister:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.chain = ChainStore(self.root / CONFLICTS_FILE, kinds=("conflict", "superseded"))

    def records(self) -> list[dict[str, Any]]:
        return self.chain.records()

    def _report_path(self, sha: str) -> Path:
        return self.root / REPORTS_DIR / f"{sha}.json"

    def report(self, conflict_id: str) -> dict[str, Any]:
        rec = next((r for r in self.records() if r["kind"] == "conflict" and r["conflict_id"] == conflict_id), None)
        if rec is None:
            raise ConfigError(f"no recorded conflict {conflict_id}")
        return self._load_report(rec)

    def _load_report(self, rec: dict[str, Any]) -> dict[str, Any]:
        p = self._report_path(rec["report_sha256"])
        if not p.is_file():
            raise ConfigError(f"conflict {rec['conflict_id']}: report file {p.name} is missing (restore it from "
                              "backup); the datasets it names are refused")
        if file_sha256(p) != rec["report_file_sha256"]:
            raise ConfigError(f"conflict {rec['conflict_id']}: report file {p.name} was modified")
        rep = json.loads(p.read_text(encoding="utf-8"))
        body = {k: v for k, v in rep.items() if k != "report_sha256"}
        if rep.get("report_sha256") != rec["report_sha256"] or text_sha256(_canonical(body)) != rec["report_sha256"]:
            raise ConfigError(f"conflict {rec['conflict_id']}: report content does not match its hash")
        return rep

    def record(self, report: dict[str, Any], *, by: str, note: str = "") -> dict[str, Any]:
        """Record a comparison report as a CONFLICT between its two datasets (refused if they agree)."""
        if not str(by).strip():
            raise ConfigError("--by (the person recording the conflict) is required")
        body = {k: v for k, v in report.items() if k != "report_sha256"}
        sha = report.get("report_sha256")
        if report.get("kind") != "dataset_conflict_report" or text_sha256(_canonical(body)) != sha:
            raise ConfigError("not an unmodified conflict report")
        if not has_conflict(report):
            raise ConfigError(f"{report['a']['ref']} and {report['b']['ref']} agree on every compared minute: "
                              "there is no conflict to record")
        cid = sha[:16]
        if any(r["kind"] == "conflict" and r["conflict_id"] == cid for r in self.records()):
            raise ConfigError(f"conflict {cid} is already recorded")
        p = self._report_path(sha)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
            os.chmod(p, stat.S_IREAD)
        t = report["totals"]
        return self.chain.append({
            "kind": "conflict", "conflict_id": cid, "report_sha256": sha, "report_file_sha256": file_sha256(p),
            "a": report["a"]["ref"], "b": report["b"]["ref"],
            "a_manifest_sha256": report["a"]["manifest_sha256"], "b_manifest_sha256": report["b"]["manifest_sha256"],
            "differing": t["differing"], "only_in_a": t["only_in_a"], "only_in_b": t["only_in_b"],
            "by": str(by)[:200], "note": str(note)[:1000]})

    def supersede(self, old_ref: str, new_ref: str, *, by: str, reason: str) -> dict[str, Any]:
        """HUMAN decision: ``old_ref`` is superseded by ``new_ref``. Needs a recorded conflict between them."""
        if not str(by).strip() or not str(reason).strip():
            raise ConfigError("--by and --reason are required: superseding a dataset is a recorded human decision")
        if old_ref == new_ref:
            raise ConfigError("a dataset cannot supersede itself")
        recs = self.records()
        conf = [r for r in recs if r["kind"] == "conflict" and {r["a"], r["b"]} == {old_ref, new_ref}]
        if not conf:
            raise ConfigError(f"no recorded conflict between {old_ref} and {new_ref}: record the comparison first "
                              "(`run_data.py conflicts A B --record`)")
        for r in conf:
            self._load_report(r)                           # the evidence must still be intact
        sup = {r["dataset"]: r for r in recs if r["kind"] == "superseded"}
        if old_ref in sup:
            raise ConfigError(f"{old_ref} is already superseded by {sup[old_ref]['superseded_by']}")
        if new_ref in sup:
            raise ConfigError(f"{new_ref} is itself superseded by {sup[new_ref]['superseded_by']}: it cannot be "
                              "the canonical version")
        return self.chain.append({"kind": "superseded", "dataset": old_ref, "superseded_by": new_ref,
                                  "conflict_ids": [r["conflict_id"] for r in conf], "by": str(by)[:200],
                                  "reason": str(reason)[:1000]})

    def state(self, ref: str) -> dict[str, Any]:
        """``superseded_by`` and the open / resolved conflicts of one dataset (verifies every report it names)."""
        recs = self.records()
        sup = next((r for r in recs if r["kind"] == "superseded" and r["dataset"] == ref), None)
        resolved_ids = {cid for r in recs if r["kind"] == "superseded" for cid in r["conflict_ids"]}
        open_, resolved = [], []
        for r in recs:
            if r["kind"] != "conflict" or ref not in (r["a"], r["b"]):
                continue
            self._load_report(r)
            item = {"conflict_id": r["conflict_id"], "with": r["b"] if r["a"] == ref else r["a"],
                    "differing": r["differing"], "only_in_a": r["only_in_a"], "only_in_b": r["only_in_b"]}
            (resolved if r["conflict_id"] in resolved_ids else open_).append(item)
        return {"superseded_by": sup["superseded_by"] if sup else None,
                "superseded": None if sup is None else {k: sup[k] for k in ("by", "reason", "at", "conflict_ids")},
                "open_conflicts": open_, "resolved_conflicts": resolved}


def usage_refusal(state: dict[str, Any] | None) -> str | None:
    """Why a dataset with this conflict state must not be used for research, plans or continuous series."""
    if not state:
        return None
    if state.get("superseded_by"):
        return f"superseded by {state['superseded_by']}"
    if state.get("open_conflicts"):
        ids = ", ".join(f"{c['conflict_id']} (with {c['with']})" for c in state["open_conflicts"])
        return f"unresolved data conflict {ids}: resolve it with `run_data.py supersede` first"
    return None


__all__ = ["compare_datasets", "has_conflict", "ConflictRegister", "usage_refusal", "CONFLICTS_FILE", "REPORTS_DIR"]
