"""Auditable compliance reports from a paper journal (read-only, chain-verified).

Every row keeps its journal ``seq`` and UTC timestamp, and the profile / rule-set identity that was in
force (from the run identity and the decision stages). Exports go through the formula-safe CSV / JSON
writers of the analytics package.

:class:`ComplianceJournalReader` reads INCREMENTALLY: each refresh verifies and parses only the rows
appended since the previous one, and keeps bounded row buffers. A live journal with millions of market
events is therefore not rescanned on every request (Sprint 10 audit: the previous full rescan per API
call was a denial-of-service vector).
"""

from __future__ import annotations

import collections
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from src.analytics.shadow.events import ReadOnlyJournal
from src.paper.journal import JournalError

MAX_ROWS = 20_000
_KINDS = ("entry_decision", "fill_authorization", "prop_firm_eod", "prop_firm_breach", "flatten_requested",
          "account_update", "run_identity")


class ComplianceJournalReader:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._seq, self._prev = 0, "GENESIS"
        self.rows_verified = 0
        self.profiles: dict[str, Any] = {}
        self.evaluations: collections.deque = collections.deque(maxlen=MAX_ROWS)
        self.eod: collections.deque = collections.deque(maxlen=MAX_ROWS)
        self.breaches: list[dict[str, Any]] = []
        self.flattens: collections.deque = collections.deque(maxlen=MAX_ROWS)
        self.account: collections.deque = collections.deque(maxlen=MAX_ROWS)
        self.block_reasons: Counter = Counter()
        self.n_evaluations = self.n_blocks = 0
        self.error: str | None = None

    def refresh(self) -> "ComplianceJournalReader":
        with self._lock:
            if self.error:
                raise JournalError(self.error)
            j = ReadOnlyJournal(self.path)
            try:
                cur = j.db.execute("SELECT seq, ts, kind, payload, prev_hash, hash FROM journal WHERE seq > ? ORDER BY seq",
                                   (self._seq,))
                for seq, ts, kind, payload, ph, h in cur:
                    if seq != self._seq + 1 or ph != self._prev or j._hash(ph, seq, ts, kind, payload) != h:
                        self.error = f"journal chain broken at seq {seq}"
                        raise JournalError(self.error)
                    self._seq, self._prev = seq, h
                    self.rows_verified += 1
                    if kind in _KINDS:
                        self._ingest(seq, ts, kind, json.loads(payload))
            finally:
                j.close()
            return self

    def _ingest(self, seq: int, ts: str, kind: str, p: dict[str, Any]) -> None:
        if kind == "run_identity" and p.get("prop_firm"):
            self.profiles[p["prop_firm"]["profile"]] = p["prop_firm"]
        elif kind in ("entry_decision", "fill_authorization"):
            st = next((s for s in p.get("stages") or [] if s.get("stage") == "prop_firm"), None)
            if st is not None:
                self.n_evaluations += 1
                if st.get("ok") is False:
                    self.n_blocks += 1
                    self.block_reasons[st.get("reason")] += 1
                self.evaluations.append({"seq": seq, "time": ts, "scope": "pre_trade" if kind == "entry_decision" else "fill",
                                         "root": p.get("root"), "allowed": st.get("ok"), "reason": st.get("reason"),
                                         "reasons": st.get("reasons"), "profile": st.get("profile"),
                                         "ruleset": st.get("ruleset"), "official_buffer": st.get("official_buffer"),
                                         "estimated_buffer": st.get("estimated_buffer"), "state": st.get("state")})
        elif kind == "prop_firm_eod":
            self.eod.append({"seq": seq, "time": ts, **p})
        elif kind == "prop_firm_breach":
            if len(self.breaches) < MAX_ROWS:
                self.breaches.append({"seq": seq, "time": ts, **p})
        elif kind == "flatten_requested" and str(p.get("reason", "")).startswith("prop_firm"):
            self.flattens.append({"seq": seq, "time": ts, "root": p.get("root"), "reason": p.get("reason")})
        elif kind == "account_update":
            self.account.append({"seq": seq, "time": ts, "equity": p.get("equity"),
                                 "net_realized": p.get("net_realized"), "unrealized": p.get("unrealized")})

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {"profiles": dict(self.profiles),
                    "counts": {"evaluations": self.n_evaluations, "blocks": self.n_blocks, "eod": len(self.eod),
                               "breaches": len(self.breaches), "protective_flattens": len(self.flattens)},
                    "block_reasons": dict(self.block_reasons.most_common()),
                    "evaluations": list(self.evaluations), "drawdown_history": list(self.eod),
                    "breaches": list(self.breaches), "protective_flattens": list(self.flattens),
                    "account_history": list(self.account), "rows_verified": self.rows_verified,
                    "timezone": "all times UTC (ISO 8601)"}


def journal_report(path: Path) -> dict[str, Any]:
    return ComplianceJournalReader(path).refresh().report()


def rows_for_export(rep: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    key = {"evaluations": "evaluations", "drawdown": "drawdown_history", "breaches": "breaches",
           "flattens": "protective_flattens", "account": "account_history"}[kind]
    return list(rep.get(key) or [])[:MAX_ROWS]
