"""Experiment registry (Sprint 11): EVERY attempted research configuration, including failures.

Each attempt is appended BEFORE its result is known ("started") and closed by a second record ("finished"
with status ok / failed / refused). An attempt that never finishes (crash) still counts. The number of
attempts of a strategy on an instrument is the family size of the multiple-testing adjustment: trying more
configurations makes every individual result weaker, and deleting records breaks the hash chain.

Location: ``validation_history/experiments.jsonl`` (next to the OOS ledger) or ``FTS_EXPERIMENTS_PATH``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT
from src.research.chain import ChainStore

EXPERIMENTS_ENV = "FTS_EXPERIMENTS_PATH"
CANONICAL = PROJECT_ROOT / "validation_history" / "experiments.jsonl"


def params_sha256(params_dict: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params_dict, sort_keys=True, default=str).encode()).hexdigest()


class ExperimentRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        raw = path or os.environ.get(EXPERIMENTS_ENV)
        self.path = Path(raw).resolve() if raw else CANONICAL
        self.store = ChainStore(self.path, kinds=("started", "finished"))

    def records(self) -> list[dict[str, Any]]:
        return self.store.records()

    def identity(self) -> dict[str, Any]:
        """Where attempts were recorded (shown in every manifest / reveal: a non-canonical or fresh registry
        would understate the multiple-testing family, and a reviewer must be able to see that)."""
        recs = self.records()
        return {"path": str(self.path), "canonical": self.path == CANONICAL,
                "attempts": sum(r["kind"] == "started" for r in recs),
                "last_hash": recs[-1]["hash"] if recs else None}

    def start(self, *, strategy: str, version: str, version_sha256: str, registered: bool, params_sha256: str,
              instrument: str, data_sha256: str, period: dict[str, Any], purpose: str, family: str,
              detail: dict[str, Any] | None = None) -> str:
        attempt = uuid.uuid4().hex[:16]
        self.store.append({"kind": "started", "attempt": attempt, "strategy": strategy, "version": version,
                           "version_sha256": version_sha256, "registered": registered,
                           "params_sha256": params_sha256, "instrument": instrument, "data_sha256": data_sha256,
                           "period": period, "purpose": purpose, "family": family, "detail": detail or {}})
        return attempt

    def finish(self, attempt: str, *, status: str, summary: dict[str, Any] | None = None, error: str = "") -> None:
        if status not in ("ok", "failed", "refused"):
            raise ValueError("status must be ok / failed / refused")
        self.store.append({"kind": "finished", "attempt": attempt, "status": status, "summary": summary or {},
                           "error": error[:500]})

    def attempts(self, *, strategy: str | None = None, instrument: str | None = None) -> list[dict[str, Any]]:
        """Started attempts merged with their outcome (``status`` "unfinished" if none was recorded)."""
        recs = self.records()
        fin = {r["attempt"]: r for r in recs if r["kind"] == "finished"}
        out = []
        for r in recs:
            if r["kind"] != "started":
                continue
            if strategy and r["strategy"] != strategy or instrument and r["instrument"] != instrument:
                continue
            f = fin.get(r["attempt"])
            out.append({**{k: r[k] for k in ("attempt", "at", "strategy", "version", "version_sha256", "registered",
                                             "params_sha256", "instrument", "data_sha256", "period", "purpose",
                                             "family")},
                        "status": f["status"] if f else "unfinished", "summary": f["summary"] if f else {},
                        "error": f["error"] if f else ""})
        return out

    def family_size(self, strategy: str, instrument: str) -> int:
        """Distinct configurations (parameter hashes) attempted for a strategy on an instrument, failures and
        unfinished attempts included."""
        return len({a["params_sha256"] for a in self.attempts(strategy=strategy, instrument=instrument)})


__all__ = ["ExperimentRegistry", "params_sha256", "EXPERIMENTS_ENV"]
