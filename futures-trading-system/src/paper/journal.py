"""Persistence: an append-only, hash-chained audit journal plus atomic state snapshots (SQLite).

* Every processing step (a batch of events) is ONE SQLite transaction: its journal entries, the
  completed bars and the state snapshot commit together or not at all (WAL, synchronous=FULL).
  A crash loses at most the uncommitted batch -- never half of it.
* Each journal row carries ``hash = H(prev_hash, seq, ts, kind, payload)``. With the environment
  variable ``FTS_JOURNAL_KEY`` set, H is HMAC-SHA256 (tampering without the key is detectable);
  otherwise SHA-256 (detects accidental corruption and naive edits, not a determined attacker
  who recomputes the chain).
* :meth:`verify` re-checks the whole chain; recovery refuses a broken chain.
* Files are created with owner-only permissions. Payloads are JSON only (no pickle).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

KEY_ENV = "FTS_JOURNAL_KEY"
SCHEMA = 1


class JournalError(RuntimeError):
    """The journal or snapshot is missing, corrupted or inconsistent."""


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Journal:
    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        exists = self.path.exists()
        if not exists and not create:
            raise JournalError(f"no paper account at {self.path} (create one explicitly)")
        if exists and create:
            raise JournalError(f"a paper account already exists at {self.path}; refusing to overwrite it")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not exists:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if not exists:
                self.db.executescript("""
                    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
                    CREATE TABLE journal (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
                                          payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
                    CREATE TABLE snapshot (id INTEGER PRIMARY KEY, journal_seq INTEGER NOT NULL, ts TEXT NOT NULL,
                                           state TEXT NOT NULL, hash TEXT NOT NULL);
                    CREATE TABLE bars (root TEXT NOT NULL, ts TEXT NOT NULL, open REAL, high REAL, low REAL,
                                       close REAL, volume REAL, PRIMARY KEY (root, ts));
                """)
                self.db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
                self.db.execute("INSERT INTO meta VALUES ('keyed', ?)", ("1" if os.environ.get(KEY_ENV) else "0",))
            keyed = self.db.execute("SELECT v FROM meta WHERE k='keyed'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise JournalError(f"paper state database {self.path} is unreadable: {exc}") from exc
        if keyed is None:
            raise JournalError("paper state database has no metadata")
        self.keyed = keyed[0] == "1"
        if self.keyed and not os.environ.get(KEY_ENV):
            raise JournalError(f"this journal is HMAC-protected: set {KEY_ENV} to open it")
        self._key = os.environ.get(KEY_ENV, "").encode() if self.keyed else b""
        row = self.db.execute("SELECT seq, hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        self.seq, self.last_hash = (row[0], row[1]) if row else (0, "GENESIS")
        self._pending: list[tuple] = []
        self._pending_bars: list[tuple] = []
        self._pending_snapshot: tuple | None = None

    # ------------------------------------------------------------------ hashing
    def _hash(self, prev: str, seq: int, ts: str, kind: str, payload: str) -> str:
        msg = f"{prev}|{seq}|{ts}|{kind}|{payload}".encode()
        if self._key:
            return hmac.new(self._key, msg, hashlib.sha256).hexdigest()
        return hashlib.sha256(msg).hexdigest()

    # ------------------------------------------------------------------ writing (buffered, atomic commit)
    def append(self, kind: str, ts: pd.Timestamp | str, payload: dict[str, Any]) -> int:
        self.seq += 1
        t = ts.isoformat() if isinstance(ts, pd.Timestamp) else str(ts)
        body = _canon(payload)
        h = self._hash(self.last_hash, self.seq, t, kind, body)
        self._pending.append((self.seq, t, kind, body, self.last_hash, h))
        self.last_hash = h
        return self.seq

    def add_bar(self, root: str, ts: pd.Timestamp, row: tuple) -> None:
        self._pending_bars.append((root, ts.isoformat(), *row))

    def set_snapshot(self, ts: pd.Timestamp, state: dict[str, Any]) -> None:
        body = _canon(state)
        self._pending_snapshot = (self.seq, ts.isoformat(), body, hashlib.sha256(body.encode()).hexdigest())

    def commit(self) -> None:
        if not (self._pending or self._pending_bars or self._pending_snapshot):
            return
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.executemany("INSERT INTO journal VALUES (?,?,?,?,?,?)", self._pending)
                self.db.executemany("INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?)", self._pending_bars)
                if self._pending_snapshot is not None:
                    self.db.execute("INSERT INTO snapshot (journal_seq, ts, state, hash) VALUES (?,?,?,?)",
                                    self._pending_snapshot)
                    self.db.execute("DELETE FROM snapshot WHERE id NOT IN (SELECT id FROM snapshot ORDER BY id DESC LIMIT 5)")
        except sqlite3.Error as exc:
            # nothing was written (transaction rolled back); the pending batch is KEPT and retried by
            # the next commit, so no execution that happened in memory is ever lost from the audit trail
            raise JournalError(f"persistence failed: {exc}") from exc
        self._pending, self._pending_bars, self._pending_snapshot = [], [], None

    def discard_pending(self) -> None:
        row = self.db.execute("SELECT seq, hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        self.seq, self.last_hash = (row[0], row[1]) if row else (0, "GENESIS")
        self._pending, self._pending_bars, self._pending_snapshot = [], [], None

    # ------------------------------------------------------------------ reading / verification
    def entries(self, kind: str | None = None) -> Iterator[dict[str, Any]]:
        q = "SELECT seq, ts, kind, payload FROM journal" + (" WHERE kind=?" if kind else "") + " ORDER BY seq"
        for seq, ts, k, payload in self.db.execute(q, (kind,) if kind else ()):
            yield {"seq": seq, "ts": ts, "kind": k, "payload": json.loads(payload)}

    def verify(self) -> int:
        prev = "GENESIS"
        n = 0
        try:
            rows = self.db.execute("SELECT seq, ts, kind, payload, prev_hash, hash FROM journal ORDER BY seq")
            for seq, ts, kind, payload, ph, h in rows:
                n += 1
                if seq != n:
                    raise JournalError(f"journal sequence gap at {seq} (expected {n})")
                if ph != prev or self._hash(prev, seq, ts, kind, payload) != h:
                    raise JournalError(f"journal hash chain broken at seq {seq}")
                prev = h
        except sqlite3.DatabaseError as exc:
            raise JournalError(f"journal unreadable: {exc}") from exc
        return n

    def latest_snapshot(self) -> tuple[int, dict[str, Any]] | None:
        row = self.db.execute("SELECT journal_seq, state, hash FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        seq, body, h = row
        if hashlib.sha256(body.encode()).hexdigest() != h:
            raise JournalError("latest snapshot is corrupted (hash mismatch)")
        try:
            return seq, json.loads(body)
        except ValueError as exc:
            raise JournalError(f"latest snapshot is not valid JSON: {exc}") from exc

    def bars(self, root: str) -> pd.DataFrame:
        rows = self.db.execute("SELECT ts, open, high, low, close, volume FROM bars WHERE root=? ORDER BY ts",
                               (root,)).fetchall()
        idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows], name="timestamp")
        return pd.DataFrame([r[1:] for r in rows], index=idx, columns=["open", "high", "low", "close", "volume"],
                            dtype="float64")

    def close(self) -> None:
        self.db.close()
