"""Canonical holdout CLAIM ledger and its independent ANCHOR (Sprint 13 P1.1).

Every final out-of-sample evaluation of reserved (holdout) data goes through one claim with this lifecycle::

    PREPARED --> CLAIMED --> CONSUMED --> COMPLETED
        |           |
        +-----------+--> ABORTED      (only while CONSUMED was never written)

* PREPARED  the request and its full binding are recorded (metadata only: nothing has been read from the data).
* CLAIMED   no other non-aborted claim overlaps the segment: the segment is now exclusively this claim's.
* CONSUMED  written durably BEFORE the first byte of the dataset is read. From here on the reservation is used up
            for good: no abort, no recovery and no second evaluation can release it.
* COMPLETED the result, the run manifest hash and the frozen Monte Carlo report hash are recorded.
* ABORTED   only from PREPARED or CLAIMED, i.e. only when the ledgers prove CONSUMED was never written.

Two hash-chained ledgers, written in this order for every transition:

1. the ANCHOR ``validation_history/holdout_anchor.jsonl`` (+ ``.head.json``): git-IGNORED, so no Git checkout,
   restore, reset or stash touches it. Each transition is mirrored there in full;
2. the CLAIM LEDGER ``config/research/holdout_ledger.jsonl`` (+ ``.head.json``): Git-tracked for review and
   history. Each record names the anchor record that mirrors it; its genesis names the anchor's genesis.

Every read verifies both chains, both heads, the claim lifecycle and that the claim ledger equals the anchor's
mirrors record for record. A claim ledger that is BEHIND the anchor (rolled back by Git, or interrupted between the
two writes) or an anchor that is behind / missing / replaced fails closed. :func:`repair` only ever EXTENDS history:
it completes a head that lags a fully verified chain, preserves (never deletes) a partially written last line, and
replays anchor mirrors that are missing from the claim ledger. It refuses anything that would need history to be
discarded. New claims are refused while the claim ledger has uncommitted changes, so its Git history stays complete.

Nothing here creates, resets or re-initialises a ledger except :func:`_create_new` (private, refuses to overwrite,
not exposed by any CLI). Integrity and its limits: the chains detect edits, removals, reordering and truncation;
the anchor detects a rollback of the Git-tracked file; Git history detects a later rewrite of committed records.
Someone who deletes or rewrites BOTH ledgers consistently, or edits the code, is not stopped: this is an audit and
coordination mechanism, not a defence against a hostile local user. Keep backups of ``validation_history/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backtest import filelock
from src.config import PROJECT_ROOT, ConfigError

LEDGER_FILE = PROJECT_ROOT / "config" / "research" / "holdout_ledger.jsonl"
HEAD_FILE = PROJECT_ROOT / "config" / "research" / "holdout_ledger.head.json"
ANCHOR_FILE = PROJECT_ROOT / "validation_history" / "holdout_anchor.jsonl"
ANCHOR_HEAD = PROJECT_ROOT / "validation_history" / "holdout_anchor.head.json"
LOCK_DIR = PROJECT_ROOT / "validation_history" / "locks"

PREPARED, CLAIMED, CONSUMED, COMPLETED, ABORTED = "PREPARED", "CLAIMED", "CONSUMED", "COMPLETED", "ABORTED"
STATES = (PREPARED, CLAIMED, CONSUMED, COMPLETED, ABORTED)
_KIND = {"claim_prepared": PREPARED, "claim_claimed": CLAIMED, "claim_consumed": CONSUMED,
         "claim_completed": COMPLETED, "claim_aborted": ABORTED}
KIND_OF = {v: k for k, v in _KIND.items()}
NEXT = {None: {PREPARED}, PREPARED: {CLAIMED, ABORTED}, CLAIMED: {CONSUMED, ABORTED}, CONSUMED: {COMPLETED},
        COMPLETED: set(), ABORTED: set()}
BINDING_KEYS = ("root", "reservation", "start", "end", "plan", "plan_sha256", "dataset_ref", "dataset_manifest_sha256",
                "bars_content_sha256", "data_sha256", "strategy", "version", "version_sha256", "params_sha256",
                "fingerprint", "eval_config_sha256", "policy_sha256")
_CHAIN_KEYS = ("seq", "prev_hash", "record_hash", "anchor_seq", "anchor_hash")


class HoldoutError(ConfigError):
    """An operation would touch reserved (holdout) data, or the holdout state cannot be trusted."""


class HoldoutLedgerError(HoldoutError):
    """A holdout ledger is missing, damaged, behind or inconsistent: every holdout operation is refused."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash(rec: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in rec.items() if k != "record_hash"}).encode()).hexdigest()


def _body(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rec.items() if k not in _CHAIN_KEYS}


class _Chain:
    """One append-only, hash-chained JSON-lines file with a head file (record count + last hash)."""

    def __init__(self, path: Path, head: Path, name: str) -> None:
        self.path, self.head, self.name = path, head, name

    def scan(self) -> tuple[list[dict[str, Any]], bytes, dict[str, Any] | None]:
        """(verified complete records, trailing partial bytes, head) - the chain is verified, the head is not."""
        if not self.path.is_file() or not self.head.is_file():
            missing = [p.name for p in (self.path, self.head) if not p.is_file()]
            raise HoldoutLedgerError(f"holdout {self.name} incomplete: {', '.join(missing)} missing. It is never "
                                     "recreated automatically: restore it from backup. Holdout operations are refused "
                                     "until then")
        data = self.path.read_bytes()
        cut = data.rfind(b"\n") + 1
        complete, partial = data[:cut], data[cut:]
        out, prev = [], "0" * 64
        for i, line in enumerate(complete.decode("utf-8").splitlines()):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HoldoutLedgerError(f"{self.path.name}: record {i} is not valid JSON: {exc}") from exc
            if not isinstance(rec, dict) or rec.get("seq") != i or rec.get("prev_hash") != prev \
                    or rec.get("record_hash") != _hash(rec):
                raise HoldoutLedgerError(f"{self.path.name}: record {i} fails verification (edited, removed or "
                                         "reordered)")
            prev = rec["record_hash"]
            out.append(rec)
        if not out or out[0].get("type") != "genesis":
            raise HoldoutLedgerError(f"{self.path.name}: no genesis record")
        try:
            head = json.loads(self.head.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            head = None
        return out, partial, head

    def read(self) -> list[dict[str, Any]]:
        recs, partial, head = self.scan()
        if partial:
            raise HoldoutLedgerError(f"{self.path.name}: the last record is truncated (interrupted write): refused. "
                                     "Run the holdout repair procedure (run_research.py holdout-repair)")
        if not isinstance(head, dict) or head.get("count") != len(recs) or head.get("last_hash") != recs[-1]["record_hash"]:
            raise HoldoutLedgerError(f"{self.path.name} does not match {self.head.name} ({len(recs)} records vs head "
                                     f"{None if not isinstance(head, dict) else head.get('count')}): refused. If the "
                                     "head lags (interrupted write) run run_research.py holdout-repair")
        return recs

    def append(self, recs: list[dict[str, Any]], body: dict[str, Any]) -> dict[str, Any]:
        full = dict(body, seq=len(recs), prev_hash=recs[-1]["record_hash"])
        full["record_hash"] = _hash(full)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(_canonical(full) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.write_head(len(recs) + 1, full["record_hash"])
        return full

    def write_head(self, count: int, last_hash: str) -> None:
        tmp = self.head.with_name(self.head.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(_canonical({"count": count, "last_hash": last_hash}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        filelock.durable_replace(tmp, self.head)

    def create(self, genesis: dict[str, Any]) -> dict[str, Any]:
        if self.path.exists() or self.head.exists():
            raise HoldoutLedgerError(f"holdout {self.name} already exists: it is never overwritten or re-initialised")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        g = dict(genesis, seq=0, prev_hash="0" * 64)
        g["record_hash"] = _hash(g)
        with self.path.open("x", encoding="utf-8", newline="\n") as fh:
            fh.write(_canonical(g) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        filelock.fsync_dir(self.path.parent)
        self.write_head(1, g["record_hash"])
        return g

    def repair(self, report: list[str]) -> None:
        """Extend-only repair: preserve a partial last line in a side file; move a lagging head forward to a fully
        verified chain. Refuses a head that is AHEAD of the records (history missing) or that is not a prefix."""
        recs, partial, head = self.scan()
        if partial:
            keep = self.path.with_name(f"{self.path.name}.partial-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}")
            keep.write_bytes(partial)
            data = self.path.read_bytes()
            with self.path.open("r+b") as fh:
                fh.truncate(len(data) - len(partial))
                fh.flush()
                os.fsync(fh.fileno())
            report.append(f"{self.path.name}: an interrupted, incomplete last line ({len(partial)} bytes) was moved to "
                          f"{keep.name} (preserved, never part of the verified history)")
        if isinstance(head, dict) and head.get("count") == len(recs) and head.get("last_hash") == recs[-1]["record_hash"]:
            return
        count = head.get("count") if isinstance(head, dict) else None
        if not isinstance(count, int) or count < 1 or count > len(recs) or \
                head.get("last_hash") != recs[count - 1]["record_hash"]:
            raise HoldoutLedgerError(f"{self.head.name} is not a prefix of {self.path.name} (records missing or head "
                                     "replaced): not repairable without discarding history; restore from backup")
        self.write_head(len(recs), recs[-1]["record_hash"])
        report.append(f"{self.head.name}: head moved forward from {count} to {len(recs)} verified records")


def _ledger() -> _Chain:
    return _Chain(LEDGER_FILE, HEAD_FILE, "claim ledger")


def _anchor() -> _Chain:
    return _Chain(ANCHOR_FILE, ANCHOR_HEAD, "claim anchor")


def lock_path() -> Path:
    return LOCK_DIR / "holdout_ledger.lock"


def claim_lock_path(claim_id: str) -> Path:
    return LOCK_DIR / f"claim_{claim_id}.lock"


def locked(timeout: float = 60.0):
    """The cross-process ledger lock (context manager)."""
    return filelock.exclusive(lock_path(), timeout=timeout)


def _cross_check(led: list[dict[str, Any]], anc: list[dict[str, Any]], *, allow_ledger_behind: bool = False) -> int:
    """Number of anchor mirrors missing from the claim ledger (0 when consistent)."""
    if led[0].get("anchor_genesis_hash") != anc[0]["record_hash"]:
        raise HoldoutLedgerError("the claim ledger and the anchor do not belong together (anchor replaced or recreated): "
                                 "refused. Restore the original anchor from backup")
    mirrors = [a for a in anc[1:] if a.get("type") == "mirror"]
    claims_ = led[1:]
    if len(claims_) > len(mirrors):
        raise HoldoutLedgerError(f"the anchor is behind the claim ledger ({len(mirrors)} vs {len(claims_)} transitions): "
                                 "anchor deleted, rolled back or restored from an old backup: refused")
    for c, m in zip(claims_, mirrors):
        if c.get("anchor_hash") != m["record_hash"] or c.get("anchor_seq") != m["seq"] or _body(c) != m.get("body"):
            raise HoldoutLedgerError(f"claim ledger record {c.get('seq')} does not match its anchor mirror: refused")
    behind = len(mirrors) - len(claims_)
    if behind and not allow_ledger_behind:
        raise HoldoutLedgerError(f"the claim ledger is BEHIND its anchor by {behind} transition(s): it was rolled back "
                                 "(e.g. git checkout / restore / reset / stash) or an append was interrupted. Refused: "
                                 "run run_research.py holdout-repair (it replays the anchor, it never discards history)")
    return behind


def records() -> list[dict[str, Any]]:
    """Verify both ledgers (chains, heads, mutual consistency, lifecycle) and return the claim ledger records."""
    led, anc = _ledger().read(), _anchor().read()
    _cross_check(led, anc)
    _lifecycle(led)
    return led


def anchor_records() -> list[dict[str, Any]]:
    led, anc = _ledger().read(), _anchor().read()
    _cross_check(led, anc)
    return anc


def _lifecycle(recs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    claims_: dict[str, dict[str, Any]] = {}
    for r in recs[1:]:
        state = _KIND.get(r.get("type"))
        cid = r.get("claim_id")
        if state is None or not isinstance(cid, str) or not cid:
            raise HoldoutLedgerError(f"holdout ledger record {r.get('seq')} is not a claim record")
        cur = claims_.get(cid, {}).get("state")
        if state not in NEXT[cur]:
            raise HoldoutLedgerError(f"holdout ledger record {r.get('seq')}: claim {cid} cannot go {cur} -> {state}")
        if state == PREPARED:
            b = r.get("binding")
            if not isinstance(b, dict) or set(BINDING_KEYS) - set(b):
                raise HoldoutLedgerError(f"holdout ledger record {r.get('seq')}: incomplete binding")
            claims_[cid] = {"claim_id": cid, "state": state, "binding": b, "history": [r]}
        else:
            claims_[cid]["state"] = state
            claims_[cid]["history"].append(r)
    return claims_


def claims() -> dict[str, dict[str, Any]]:
    """Every claim with its current state, binding and history (both ledgers are verified first)."""
    return _lifecycle(records())


def append(rec: dict[str, Any]) -> dict[str, Any]:
    """Append one claim transition: first mirrored to the anchor, then to the claim ledger. The caller MUST hold
    :func:`locked`; both ledgers are re-verified first and an impossible transition is refused before any write."""
    if not filelock.held_here(lock_path()):
        raise HoldoutLedgerError("internal: holdout ledger append without the ledger lock")
    L, A = _ledger(), _anchor()
    led, anc = L.read(), A.read()
    _cross_check(led, anc)
    body = dict(rec, at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
    _lifecycle(led + [dict(body, seq=len(led))])
    m = A.append(anc, {"type": "mirror", "claim_id": body.get("claim_id"), "body": body})
    return L.append(led, dict(body, anchor_seq=m["seq"], anchor_hash=m["record_hash"]))


def repair(*, by: str, reason: str) -> list[str]:
    """HUMAN, extend-only recovery after an interrupted write or a rollback of the claim ledger (see module docs)."""
    if not str(by).strip() or not str(reason).strip():
        raise HoldoutError("--by and --reason are required: a ledger repair is a recorded human decision")
    report: list[str] = []
    L, A = _ledger(), _anchor()
    with locked():
        A.repair(report)
        L.repair(report)
        led, anc = L.read(), A.read()
        behind = _cross_check(led, anc, allow_ledger_behind=True)
        mirrors = [a for a in anc[1:] if a.get("type") == "mirror"]
        for m in mirrors[len(led) - 1:]:
            led.append(L.append(led, dict(m["body"], anchor_seq=m["seq"], anchor_hash=m["record_hash"])))
        if behind:
            report.append(f"{L.path.name}: {behind} transition(s) replayed from the anchor (rollback or interrupted "
                          "append); commit the claim ledger now")
        if report:
            A.append(A.read(), {"type": "repair", "by": str(by)[:200], "reason": str(reason)[:500], "actions": report,
                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        records()                                     # everything verifies again, or this raises
    return report


def uncommitted_reason() -> str | None:
    """Why a new claim must wait: the claim ledger files are untracked or have uncommitted changes (or Git cannot
    tell). Refusing then keeps every earlier claim in the Git history before a new one starts."""
    files = [str(LEDGER_FILE), str(HEAD_FILE)]
    try:
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", *files], cwd=LEDGER_FILE.parent,
                                 capture_output=True, text=True, timeout=30)
        status = subprocess.run(["git", "status", "--porcelain", "--", *files], cwd=LEDGER_FILE.parent,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git is unavailable ({exc})"
    if tracked.returncode != 0:
        return "the claim ledger files are not tracked by Git"
    if status.returncode != 0 or status.stdout.strip():
        return "the claim ledger has uncommitted changes (commit the previous claim's records first)"
    return None


def owner_info() -> dict[str, Any]:
    return {"pid": os.getpid(), "host": socket.gethostname()}


# ----------------------------------------------------------------------------- the canonical validation ledger
def anchor_present() -> bool:
    """True when this machine has an anchor (either file): it is then authoritative and must verify."""
    return ANCHOR_FILE.exists() or ANCHOR_HEAD.exists()


def validation_ledger_genesis() -> str | None:
    """The genesis hash of the canonical validation ledger as recorded in the anchor (None if never recorded)."""
    rec = [a for a in anchor_records()[1:] if a.get("type") == "validation_ledger_created"]
    return rec[0]["genesis_hash"] if rec else None


def record_validation_ledger(genesis_hash: str, *, created: bool) -> None:
    """Record the canonical validation ledger's identity in the anchor, once. Refuses a second identity: a deleted and
    re-initialised canonical ledger would erase its audit history."""
    with locked():
        A = _anchor()
        anc = anchor_records()
        known = [a for a in anc[1:] if a.get("type") == "validation_ledger_created"]
        if known:
            if known[0]["genesis_hash"] != genesis_hash:
                raise HoldoutLedgerError("the canonical validation ledger was replaced (a different genesis than the one "
                                         "recorded): refused. Restore the original from backup")
            return
        A.append(anc, {"type": "validation_ledger_created", "genesis_hash": genesis_hash, "created_here": created,
                       "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def _create_new(note: str) -> None:
    """Write the genesis of a NEW anchor and a NEW claim ledger bound to it. Refuses if any of the four files exists:
    never a reset. Not exposed by any CLI; used once to create the project's ledgers and by tests on temporary paths."""
    for p in (LEDGER_FILE, HEAD_FILE, ANCHOR_FILE, ANCHOR_HEAD):
        if p.exists():
            raise HoldoutLedgerError(f"{p.name} already exists: holdout ledgers are never overwritten or re-initialised")
    a = _anchor().create({"type": "genesis", "note": f"claim anchor (git-ignored, append-only): {note}"})
    _ledger().create({"type": "genesis", "note": note, "anchor_genesis_hash": a["record_hash"]})


__all__ = ["LEDGER_FILE", "HEAD_FILE", "ANCHOR_FILE", "ANCHOR_HEAD", "LOCK_DIR", "STATES", "PREPARED", "CLAIMED",
           "CONSUMED", "COMPLETED", "ABORTED", "BINDING_KEYS", "HoldoutError", "HoldoutLedgerError", "records",
           "anchor_records", "claims", "append", "repair", "locked", "lock_path", "claim_lock_path", "owner_info",
           "uncommitted_reason", "anchor_present", "validation_ledger_genesis", "record_validation_ledger"]
