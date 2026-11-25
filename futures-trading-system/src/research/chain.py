"""Append-only, hash-chained JSON-lines store (Sprint 11): the pattern of the prop firm activation store
(:class:`src.risk.prop_firms.versions.Activations`) for research records.

Each record carries ``seq``, ``prev`` and ``hash``; with ``FTS_JOURNAL_KEY`` set the hash is an HMAC. An edited,
removed, reordered or replayed line breaks the chain and :meth:`ChainStore.records` raises. Writers are
serialised (in-process lock + ``flock``) and every append re-verifies the chain first, so a broken store is
never extended. Without the key the chain is tamper-EVIDENT, not tamper-proof (keep backups).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import ConfigError


class ChainError(ConfigError):
    """The store is corrupt, out of sequence or fails its integrity check."""


class ChainStore:
    _locks: dict[str, threading.Lock] = {}
    _guard = threading.Lock()

    def __init__(self, path: str | Path, *, kinds: tuple[str, ...]) -> None:
        self.path = Path(path)
        self.kinds = kinds
        with ChainStore._guard:
            self._lock = ChainStore._locks.setdefault(str(self.path.resolve()), threading.Lock())

    @staticmethod
    def _digest(rec: dict[str, Any]) -> str:
        from src.paper.journal import KEY_ENV

        body = json.dumps({k: v for k, v in rec.items() if k != "hash"}, sort_keys=True,
                          separators=(",", ":"), default=str).encode()
        key = os.environ.get(KEY_ENV, "")
        if key:
            return "hmac:" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        prev = "GENESIS"
        text = self.path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            raise ChainError(f"{self.path.name}: last record is truncated")
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                raise ChainError(f"{self.path.name}: blank line {i} (records were removed or edited)")
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ChainError(f"{self.path.name}: record {i} is corrupt: {exc}") from exc
            if not isinstance(rec, dict) or rec.get("kind") not in self.kinds:
                raise ChainError(f"{self.path.name}: record {i} is invalid")
            if rec.get("seq") != len(out) + 1 or rec.get("prev") != prev:
                raise ChainError(f"{self.path.name}: record {i} is out of sequence (edited, removed, reordered "
                                 "or replayed)")
            if not hmac.compare_digest(str(rec.get("hash", "")), self._digest(rec)):
                raise ChainError(f"{self.path.name}: record {i} fails its integrity check (edited, or a different "
                                 "key)")
            prev = rec["hash"]
            out.append(rec)
        return out

    def append(self, rec: dict[str, Any]) -> dict[str, Any]:
        if rec.get("kind") not in self.kinds:
            raise ChainError(f"invalid record kind {rec.get('kind')!r}")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a+", encoding="utf-8") as fh:
                try:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except ImportError:  # pragma: no cover - Windows: the in-process lock still applies
                    pass
                existing = self.records()          # refuses to extend a broken chain
                rec = {**rec, "at": pd.Timestamp.now(tz="UTC").isoformat(), "seq": len(existing) + 1,
                       "prev": existing[-1]["hash"] if existing else "GENESIS"}
                rec["hash"] = self._digest(rec)
                fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return rec


__all__ = ["ChainStore", "ChainError"]
