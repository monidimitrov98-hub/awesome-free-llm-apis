"""Historical-validation controls: date ranges, in-sample / out-of-sample periods,
walk-forward folds and an out-of-sample ledger.

Rules enforced here (the CLI builds on them):

* Periods are selected by **session (trade) date**, inclusive. Indicators are still
  computed on the whole file first; they are causal (Sprint 1 tests), so earlier data
  only serves as warm-up and never leaks future information.
* An out-of-sample period must start after its in-sample period ends (no overlap).
* Out-of-sample results are only produced when explicitly revealed, for exactly one
  configuration, from committed code (clean git tree, known commit). Every reveal is
  appended to the canonical, hash-chained validation ledger (see ValidationLedger),
  whose location does not depend on the output directory.
* The validation fingerprint covers the configuration texts, the R-multiple AND a
  SHA-256 of the Python source. Any run (in-sample, range, full or OOS) whose dates
  overlap previously revealed OOS dates of the same instrument under a DIFFERENT
  fingerprint is refused unless explicitly acknowledged (the manifest then marks the
  result as contaminated). Same-fingerprint re-runs are allowed (no new selection).
* Walk-forward folds evaluate one fixed configuration on rolling windows; there is no
  optimizer, so no parameter is fitted per fold. Out-of-sample windows may not overlap.
* Each period starts a fresh simulated account (risk state and AccountGuard reset); fold
  results are NOT a continuous funded-account survival simulation.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError


class OosReuseError(ConfigError):
    """The out-of-sample period was already evaluated with another configuration."""


@dataclass(frozen=True)
class Period:
    """A named evaluation period of session dates (inclusive). None = open-ended."""

    label: str
    kind: str  # "full" | "range" | "in_sample" | "out_of_sample" | "wf_in_sample" | "wf_out_of_sample"
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    fold: int | None = None

    @property
    def is_out_of_sample(self) -> bool:
        return self.kind in ("out_of_sample", "wf_out_of_sample")

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "kind": self.kind, "fold": self.fold,
                "start": None if self.start is None else str(self.start.date()),
                "end": None if self.end is None else str(self.end.date())}


def parse_date(value: str | None) -> pd.Timestamp | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return pd.Timestamp(str(value)).normalize()
    except ValueError as exc:
        raise ConfigError(f"Invalid date {value!r}; use YYYY-MM-DD") from exc


def make_period(label: str, kind: str, start: str | None, end: str | None, fold: int | None = None) -> Period:
    s, e = parse_date(start), parse_date(end)
    if s is not None and e is not None and s > e:
        raise ConfigError(f"{label}: start {s.date()} is after end {e.date()}")
    return Period(label, kind, s, e, fold)


def check_no_overlap(in_sample: Period, out_of_sample: Period) -> None:
    """The out-of-sample period must begin strictly after the in-sample period ends."""
    if in_sample.end is None or out_of_sample.start is None:
        raise ConfigError("In-sample needs an end date and out-of-sample a start date")
    if out_of_sample.start <= in_sample.end:
        raise ConfigError(f"Out-of-sample ({out_of_sample.start.date()}) must start after the in-sample end "
                          f"({in_sample.end.date()})")


def slice_frame(frame: pd.DataFrame, period: Period) -> pd.DataFrame:
    """Rows whose session date lies inside the period (rows outside any session are dropped)."""
    sd = pd.to_datetime(frame["session_date"])
    mask = sd.notna()
    if period.start is not None:
        mask &= sd >= period.start
    if period.end is not None:
        mask &= sd <= period.end
    return frame[mask.to_numpy()]


def session_dates(frame: pd.DataFrame) -> list[pd.Timestamp]:
    return sorted(pd.to_datetime(frame["session_date"]).dropna().unique())


def walk_forward_periods(dates: list[pd.Timestamp], train: int, test: int, step: int | None = None
                         ) -> list[tuple[Period, Period]]:
    """Rolling (in-sample, out-of-sample) folds over session dates: ``train`` sessions
    followed by the next ``test`` sessions, advancing by ``step`` (default ``test``)."""
    if train < 1 or test < 1:
        raise ConfigError("walk-forward train and test must be >= 1 session")
    step = step or test
    if step < test:
        raise ConfigError(f"walk-forward step {step} < test {test} would make out-of-sample windows overlap "
                          "(the same OOS observations would be counted in several folds)")
    folds = []
    i, k = 0, 1
    while i + train + test <= len(dates):
        tr, te = dates[i:i + train], dates[i + train:i + train + test]
        folds.append((Period(f"wf{k:02d}_in_sample", "wf_in_sample", tr[0], tr[-1], k),
                      Period(f"wf{k:02d}_out_of_sample", "wf_out_of_sample", te[0], te[-1], k)))
        i += step
        k += 1
    if not folds:
        raise ConfigError(f"Not enough sessions ({len(dates)}) for walk-forward {train}+{test}")
    return folds


LEDGER_ENV = "FTS_LEDGER_PATH"
CANONICAL_LEDGER = PROJECT_ROOT / "validation_history" / "oos_ledger.jsonl"


class LedgerError(ConfigError):
    """The validation ledger is missing, damaged or failed verification."""


class LedgerMissingError(LedgerError):
    """No validation ledger exists at the expected location."""


def resolve_ledger_path(cli_path: str | Path | None = None) -> tuple[Path, bool]:
    """Ledger location: ``--ledger`` > environment ``FTS_LEDGER_PATH`` > canonical project path.

    Returns ``(path, is_canonical)``. The location does NOT depend on ``--output-dir``.
    """
    raw = cli_path or os.environ.get(LEDGER_ENV)
    path = Path(raw).resolve() if raw else CANONICAL_LEDGER
    return path, path == CANONICAL_LEDGER


def _canonical_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _record_hash(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "record_hash"}
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


class ValidationLedger:
    """Append-only, hash-chained record of out-of-sample reveals (JSON Lines).

    Each line holds ``seq``, ``prev_hash`` and ``record_hash`` (SHA-256 of the record).
    A separate head file stores the record count and the last hash, so editing,
    deleting, reordering or truncating records is detected. The ledger is never
    recreated silently: a missing ledger must be initialised explicitly and a damaged
    one blocks every run until it is restored from backup.

    Limitation: this is tamper-EVIDENT against accidents and casual edits, not
    tamper-proof; someone who rewrites every record and the head file consistently,
    or deletes both files, cannot be detected without an external copy (keep backups,
    e.g. commit the ledger to a separate repository).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_name(self.path.name + ".head")

    def exists(self) -> bool:
        return self.path.exists() or self.head_path.exists()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def is_canonical(self) -> bool:
        return self.path.resolve() == Path(CANONICAL_LEDGER).resolve()

    def initialise(self, note: str = "") -> None:
        """Create a NEW ledger. The CANONICAL ledger can be created once only (Sprint 13 P1.1): its identity is recorded
        in the holdout anchor, and re-initialising it after a deletion (which would erase its audit history) is
        refused - restore it from backup instead."""
        from src.backtest.filelock import exclusive

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive(self.lock_path):
            if self.exists():
                raise LedgerError(f"Ledger already exists at {self.path}; refusing to overwrite it")
            anchored = False
            if self.is_canonical():
                from src.research import holdout_ledger as HL

                anchored = HL.anchor_present()        # this machine's anchor is authoritative when it exists
                if anchored:
                    try:
                        known = HL.validation_ledger_genesis()
                    except HL.HoldoutLedgerError as exc:
                        raise LedgerError(f"the canonical validation ledger cannot be created: {exc}") from exc
                    if known is not None:
                        raise LedgerError("the canonical validation ledger existed before (its identity is recorded in "
                                          "the holdout anchor) and was deleted: it is never re-initialised. Restore it "
                                          "from backup")
            genesis = {"seq": 0, "type": "genesis", "prev_hash": "0" * 64, "note": note,
                       "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            genesis["record_hash"] = _record_hash(genesis)
            with self.path.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write(_canonical_json(genesis) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._write_head(1, genesis["record_hash"])
            if anchored:
                HL.record_validation_ledger(genesis["record_hash"], created=True)

    def _write_head(self, count: int, last_hash: str) -> None:
        tmp = self.head_path.with_name(self.head_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(_canonical_json({"count": count, "last_hash": last_hash}))
            fh.flush()
            os.fsync(fh.fileno())
        from src.backtest.filelock import durable_replace

        durable_replace(tmp, self.head_path)

    def recover_head(self) -> list[str]:
        """HUMAN, extend-only recovery after an interrupted append (Sprint 13 P1.1): a head that lags a fully verified
        chain is moved forward; an incomplete last line is moved to a side file (preserved). Anything that would need
        a verified record to be discarded is refused. Never re-initialises."""
        from src.backtest.filelock import exclusive

        report: list[str] = []
        with exclusive(self.lock_path):
            if not self.path.exists() or not self.head_path.exists():
                raise LedgerError(f"Ledger incomplete at {self.path}: restore it from backup (never re-initialised)")
            data = self.path.read_bytes()
            cut = data.rfind(b"\n") + 1
            if cut < len(data):
                keep = self.path.with_name(f"{self.path.name}.partial-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}")
                keep.write_bytes(data[cut:])
                with self.path.open("r+b") as fh:
                    fh.truncate(cut)
                    fh.flush()
                    os.fsync(fh.fileno())
                report.append(f"incomplete last line ({len(data) - cut} bytes) preserved in {keep.name}")
            recs, prev = [], "0" * 64
            for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
                rec = json.loads(line)
                if rec.get("seq") != i or rec.get("prev_hash") != prev or rec.get("record_hash") != _record_hash(rec):
                    raise LedgerError(f"Ledger {self.path} record {i} fails verification: not repairable, restore it")
                prev = rec["record_hash"]
                recs.append(rec)
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
            n = head.get("count")
            if not isinstance(n, int) or n < 1 or n > len(recs) or head.get("last_hash") != recs[n - 1]["record_hash"]:
                raise LedgerError("the ledger head is not a prefix of its records: not repairable, restore from backup")
            if n < len(recs):
                self._write_head(len(recs), recs[-1]["record_hash"])
                report.append(f"head moved forward from {n} to {len(recs)} verified records")
        self.records()
        return report

    def records(self) -> list[dict[str, Any]]:
        """Verify the whole ledger and return its records (raises LedgerError on any problem)."""
        if not self.path.exists() and not self.head_path.exists():
            raise LedgerMissingError(f"No validation ledger at {self.path}")
        if not self.path.exists() or not self.head_path.exists():
            raise LedgerError(f"Ledger incomplete: {'records' if not self.path.exists() else 'head'} file "
                              f"missing at {self.path}; restore both files from backup")
        text = self.path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            raise LedgerError(f"Ledger {self.path} is truncated (last record incomplete)")
        records, prev = [], "0" * 64
        for i, line in enumerate(text.splitlines()):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"Ledger {self.path} line {i + 1} is not valid JSON: {exc}") from exc
            if not isinstance(rec, dict) or rec.get("seq") != i or rec.get("prev_hash") != prev \
                    or rec.get("record_hash") != _record_hash(rec):
                raise LedgerError(f"Ledger {self.path} failed verification at record {i} "
                                  "(edited, deleted or reordered record)")
            prev = rec["record_hash"]
            records.append(rec)
        if not records or records[0].get("type") != "genesis":
            raise LedgerError(f"Ledger {self.path} has no genesis record")
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LedgerError(f"Ledger head {self.head_path} is not valid JSON") from exc
        if head.get("count") != len(records) or head.get("last_hash") != prev:
            raise LedgerError(f"Ledger {self.path} does not match its head file "
                              f"({len(records)} records vs head {head.get('count')}): records were removed or added")
        return records

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Append under a cross-process lock (Sprint 13 P1.1): concurrent writers are serialised, and the whole
        ledger is re-verified INSIDE the lock, so two processes can never write the same sequence number."""
        from src.backtest.filelock import exclusive

        with exclusive(self.lock_path):
            records = self.records()  # verify before writing
            rec = dict(record, seq=len(records), prev_hash=records[-1]["record_hash"],
                       time=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            rec["record_hash"] = _record_hash(rec)
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(_canonical_json(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._write_head(len(records) + 1, rec["record_hash"])
        return rec


def _overlaps(a_start: Any, a_end: Any, b_start: Any, b_end: Any) -> bool:
    lo = lambda v: pd.Timestamp.min if v in (None, "") else pd.Timestamp(v)  # noqa: E731
    hi = lambda v: pd.Timestamp.max if v in (None, "") else pd.Timestamp(v)  # noqa: E731
    return lo(a_start) <= hi(b_end) and lo(b_start) <= hi(a_end)


def revealed_conflicts(records: list[dict[str, Any]], instrument: str, period: Period, fingerprint: str
                       ) -> list[dict[str, Any]]:
    """Revealed OOS periods of the same instrument that overlap ``period`` (any overlap, not only
    exact matches) and were evaluated with a DIFFERENT validation fingerprint (config or code)."""
    p = period.to_dict()
    return [r for r in records if r.get("type") == "oos_reveal" and r.get("instrument") == instrument
            and r.get("fingerprint") != fingerprint and _overlaps(p["start"], p["end"], r["start"], r["end"])]
