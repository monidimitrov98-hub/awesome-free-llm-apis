"""Profile registry, versioning, differences and HUMAN activation.

* Every version of a profile is a separate file ``config/prop_firms/profiles/<id>/<version>.toml``;
  older versions are kept (history is never rewritten).
* A profile is OPERATIONAL only when (a) its computed status is VERIFIED and (b) an activation
  record approves EXACTLY that file content (its SHA-256). Changing one character of an active
  profile makes it non-operational until a human approves the new content: there is no silent update.
* Activation records are appended by a human through ``run_prop_firm.py activate`` (never by code
  paths that trade). The store is append-only JSON lines; a revocation is a new record. Each record is
  chained to the previous one (``seq``, ``prev``, ``hash``); with ``FTS_JOURNAL_KEY`` set (the same key as
  the paper journal) the hash is an HMAC. Any edit, deletion, reordering or replayed line breaks the chain
  and the store then approves NOTHING. Without the key the chain is tamper-evident but can be recomputed by
  someone with write access to the file.
* The selection used by a session names an exact ``id`` AND ``version``: nothing ever picks the
  "latest" version or substitutes another profile.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import tomllib
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError
from src.risk.prop_firms.profile import Profile, load_profile

PROFILES_DIR = PROJECT_ROOT / "config" / "prop_firms" / "profiles"
SELECTION_FILE = PROJECT_ROOT / "config" / "prop_firm.toml"
ACTIVATIONS_FILE = PROJECT_ROOT / "data" / "prop_firms" / "activations.jsonl"
_NAME = re.compile(r"^[\w .\-]{2,80}$")


class Registry:
    """All profile versions found under ``root`` (read-only)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or PROFILES_DIR)
        self.profiles: dict[str, Profile] = {}
        self.errors: dict[str, str] = {}
        if self.root.is_dir():
            for f in sorted(self.root.glob("*/*.toml")):
                try:
                    prof = load_profile(f)
                except ConfigError as exc:
                    self.errors[f"{f.parent.name}/{f.name}"] = str(exc)[:300]
                    continue
                if prof.id != f.parent.name or prof.version != f.stem:
                    self.errors[f"{f.parent.name}/{f.name}"] = "file location does not match [profile] id / version"
                    continue
                self.profiles[prof.key] = prof

    def get(self, profile_id: str, version: str) -> Profile:
        p = self.profiles.get(f"{profile_id}@{version}")
        if p is None:
            raise ConfigError(f"prop firm profile {profile_id}@{version} not found (exact id and version required)")
        return p

    def versions(self, profile_id: str) -> list[Profile]:
        return sorted((p for p in self.profiles.values() if p.id == profile_id), key=lambda p: p.version)


# ---------------------------------------------------------------------------------------------- activation
class Activations:
    """Append-only human activation / revocation records."""

    _lock = threading.Lock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or ACTIVATIONS_FILE)

    @staticmethod
    def _digest(rec: dict[str, Any]) -> str:
        from src.paper.journal import KEY_ENV

        body = json.dumps({k: v for k, v in rec.items() if k != "hash"}, sort_keys=True, separators=(",", ":")).encode()
        key = os.environ.get(KEY_ENV, "")
        if key:
            return "hmac:" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def records(self) -> list[dict[str, Any]]:
        """All records, chain-verified. Raises ConfigError on any break (the caller then approves nothing)."""
        if not self.path.is_file():
            return []
        out = []
        prev = "GENESIS"
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"activation record {i} is corrupt: {exc}") from exc
            if not isinstance(rec, dict) or rec.get("action") not in ("activate", "revoke"):
                raise ConfigError(f"activation record {i} is invalid")
            if rec.get("seq") != len(out) + 1 or rec.get("prev") != prev:
                raise ConfigError(f"activation record {i} is out of sequence (edited, removed, reordered or replayed)")
            if not hmac.compare_digest(str(rec.get("hash", "")), self._digest(rec)):
                raise ConfigError(f"activation record {i} fails its integrity check (edited, or a different key)")
            prev = rec["hash"]
            out.append(rec)
        return out

    def active_record(self, profile: Profile) -> dict[str, Any] | None:
        """The activation in force for exactly this content (None if never approved or revoked since)."""
        try:
            recs = self.records()
        except ConfigError:
            return None  # a corrupt approval store never approves anything
        cur = None
        for r in recs:
            if r.get("profile_id") != profile.id or r.get("version") != profile.version:
                continue
            if r["action"] == "activate" and r.get("profile_sha256") == profile.sha256:
                cur = r
            elif r["action"] == "revoke":
                cur = None
        return cur

    def activate(self, profile: Profile, *, approved_by: str, confirm_sha: str, statement: str) -> dict[str, Any]:
        """HUMAN action (CLI only). Refuses anything that is not a VERIFIED profile, or a wrong confirmation."""
        rep = profile.status_report()
        if rep["status"] != "VERIFIED":
            raise ConfigError(f"refused: {profile.key} is {rep['status']}, only a VERIFIED profile can be activated "
                              f"(unresolved: {rep['unverified_rules'] + rep['incomplete_rules'] + rep['problems']})")
        if confirm_sha != profile.sha256[:16]:
            raise ConfigError("refused: --confirm must be the first 16 characters of the profile SHA-256 "
                              "(shown by `run_prop_firm.py show`), proving the reviewed content")
        if not _NAME.match(approved_by or ""):
            raise ConfigError("refused: --approved-by must name the human approver")
        return self._append({"action": "activate", "profile_id": profile.id, "version": profile.version,
                             "profile_sha256": profile.sha256, "ruleset_sha256": profile.ruleset_sha256,
                             "approved_by": approved_by, "statement": statement[:500]})

    def revoke(self, profile: Profile, *, by: str, reason: str) -> dict[str, Any]:
        if not _NAME.match(by or ""):
            raise ConfigError("refused: --by must name the human")
        return self._append({"action": "revoke", "profile_id": profile.id, "version": profile.version,
                             "by": by, "reason": reason[:500]})

    def _append(self, rec: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a+", encoding="utf-8") as fh:
                try:  # serialize concurrent writers (other processes) where the OS supports it
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except ImportError:  # pragma: no cover - Windows: the in-process lock still applies
                    pass
                existing = self.records()  # refuses to extend a broken chain
                rec = {**rec, "at": pd.Timestamp.now(tz="UTC").isoformat(), "seq": len(existing) + 1,
                       "prev": existing[-1]["hash"] if existing else "GENESIS"}
                rec["hash"] = self._digest(rec)
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return rec


def operational_state(profile: Profile, activations: Activations | None = None) -> dict[str, Any]:
    rep = profile.status_report()
    act = (activations or Activations()).active_record(profile)
    operational = rep["status"] == "VERIFIED" and act is not None
    why = None
    if rep["status"] != "VERIFIED":
        why = f"profile status {rep['status']}"
    elif act is None:
        why = "no human activation record for this exact profile content"
    return {**rep, "operational": operational, "not_operational_reason": why,
            "activation": {k: act[k] for k in ("approved_by", "at", "profile_sha256")} if act else None}


# ---------------------------------------------------------------------------------------------- selection
def load_selection(path: Path | None = None) -> dict[str, Any]:
    """Which profile a session uses: exact id + version, or none ("" = prop firm engine off)."""
    p = Path(path or SELECTION_FILE)
    if not p.is_file():
        return {"profile": "", "version": ""}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Cannot read {p.name}: {exc}") from exc
    sel = raw.get("selection", {})
    if set(raw) - {"selection"} or set(sel) - {"profile", "version"}:
        raise ConfigError(f"{p.name}: only [selection] profile / version are allowed")
    prof, ver = str(sel.get("profile", "")), str(sel.get("version", ""))
    if bool(prof) != bool(ver):
        raise ConfigError(f"{p.name}: a profile needs an exact version (the latest is never picked implicitly)")
    return {"profile": prof, "version": ver}


# ---------------------------------------------------------------------------------------------- differences
def _looser(rule_type: str, key: str, old: Any, new: Any) -> bool | None:
    """True when the change weakens protection, False when it tightens, None when unknown."""
    if not (isinstance(old, (int, float)) and isinstance(new, (int, float))):
        return None
    if rule_type == "max_loss" and key == "amount":
        return new > old
    if rule_type == "daily_loss" and key == "amount":
        return new > old
    if rule_type == "max_contracts" and key == "limit_micro_equivalent":
        return new > old
    if rule_type == "news_restriction" and key in ("minutes_before", "minutes_after"):
        return new < old
    if rule_type == "max_loss" and key == "lock_at":
        return new < old  # a lower lock level leaves the floor lower
    return None


def _looser_text(rule_type: str, key: str, old: Any, new: Any) -> bool | None:
    if rule_type == "max_loss" and key == "lock_verification":
        # an unverified lock is not applied (floor keeps trailing): stricter; becoming VERIFIED loosens
        return (new or "VERIFIED") == "VERIFIED" and (old or "VERIFIED") != "VERIFIED"
    return None


def diff(old: Profile, new: Profile) -> dict[str, Any]:
    """Rule-by-rule differences; flags changes that may be UNSAFE (looser or unknown direction on a
    safety-critical rule). Used before any human activation of a new version."""
    changes, unsafe = [], []
    o_rules, n_rules = {r.id: r for r in old.rules}, {r.id: r for r in new.rules}
    for rid in sorted(set(o_rules) | set(n_rules)):
        a, b = o_rules.get(rid), n_rules.get(rid)
        if a is None:
            changes.append({"rule": rid, "change": "added", "type": b.type})
            continue
        if b is None:
            changes.append({"rule": rid, "change": "removed", "type": a.type})
            if a.safety_critical or a.severity == "block":
                unsafe.append(f"{rid}: protective rule removed")
            continue
        for fld in ("applicable", "mandatory", "safety_critical", "severity", "verification", "stages", "effective"):
            if getattr(a, fld) != getattr(b, fld):
                changes.append({"rule": rid, "change": fld, "old": getattr(a, fld), "new": getattr(b, fld)})
                if fld == "applicable" and a.applicable and not b.applicable and a.safety_critical:
                    unsafe.append(f"{rid}: safety-critical rule made not applicable")
                if fld == "severity" and a.severity == "block" and b.severity != "block":
                    unsafe.append(f"{rid}: severity lowered from block")
        for k in sorted(set(a.params) | set(b.params)):
            if a.params.get(k) != b.params.get(k):
                lo = _looser(a.type, k, a.params.get(k), b.params.get(k))
                if lo is None:
                    lo = _looser_text(a.type, k, a.params.get(k), b.params.get(k))
                changes.append({"rule": rid, "change": f"params.{k}", "old": a.params.get(k), "new": b.params.get(k),
                                "direction": {True: "LOOSER", False: "STRICTER", None: "UNKNOWN"}[lo]})
                if lo is True or (lo is None and a.safety_critical):
                    unsafe.append(f"{rid}: {k} {a.params.get(k)!r} -> {b.params.get(k)!r}"
                                  + (" (looser)" if lo else " (direction unknown on a safety-critical rule)"))
    for k in ("initial_balance", "stage", "timezone", "day_start"):
        if getattr(old, k) != getattr(new, k):
            changes.append({"rule": "[profile]", "change": k, "old": getattr(old, k), "new": getattr(new, k)})
            unsafe.append(f"profile {k} changed")
    for k, va in vars(old.internal).items():
        vb = getattr(new.internal, k)
        if va != vb:
            changes.append({"rule": "[internal]", "change": k, "old": va, "new": vb})
            if k in ("safety_reserve", "flatten_reserve", "uncertainty_reserve", "entry_cutoff_minutes",
                     "flatten_lead_minutes", "gap_reserve_ticks") and isinstance(va, (int, float)) and vb < va:
                unsafe.append(f"internal {k} reduced {va} -> {vb}")
            if k == "quantity_policy" and vb == "reduce":
                unsafe.append("internal quantity_policy changed to reduce")
            if k == "require_trade_risk_within_buffer" and not vb:
                unsafe.append("internal trade-risk check disabled")
    return {"from": old.key, "to": new.key, "changes": changes, "potentially_unsafe": unsafe,
            "requires_human_approval": True,
            "note": "A new version is never activated automatically; historical evaluations keep the version "
                    "they were made under."}


def affected_accounts(profile_id: str, activations: Activations | None = None) -> list[dict[str, Any]]:
    """Accounts (activation records) bound to any version of a profile."""
    try:
        recs = (activations or Activations()).records()
    except ConfigError:
        return [{"error": "activation store corrupt"}]
    return [{"version": r["version"], "approved_by": r.get("approved_by"), "at": r.get("at"), "action": r["action"]}
            for r in recs if r.get("profile_id") == profile_id]
