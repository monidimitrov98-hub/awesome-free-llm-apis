"""Immutable, versioned strategy parameter sets (Sprint 11).

Layout: ``config/strategies/<strategy id>/<version>.toml`` plus ``config/strategies/<strategy id>/VERSIONS.toml``,
the REGISTRATION INDEX::

    [versions."V1-C2-SWING"]
    sha256 = "<SHA-256 of the version file, LF line endings>"
    registered = "2026-11-25"

* A version is identified by ``<strategy id>@<version>`` and its content SHA-256.
* A REGISTERED version whose file no longer matches its registered SHA-256 is refused ("modified after
  registration: create a new version"): the meaning of a version never changes silently.
* Registration only ever ADDS an entry; re-registering a version id is refused.
* Unregistered versions can be loaded only explicitly (``allow_unregistered``) for exploration; they can
  never be revealed out of sample or promoted (see :mod:`src.research.governance`).
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError

STRATEGIES_DIR = PROJECT_ROOT / "config" / "strategies"
_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-_]{0,47}$")
RESERVED = {"VERSIONS", "BOUNDS"}            # index and declared research bounds: never parameter versions


def content_sha256(path: Path) -> str:
    data = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ParameterVersion:
    strategy_id: str
    version: str
    path: Path
    sha256: str
    registered: bool
    registered_on: str | None
    params: Any

    @property
    def key(self) -> str:
        return f"{self.strategy_id}@{self.version}"

    def identity(self) -> dict[str, Any]:
        return {"strategy": self.strategy_id, "version": self.version, "sha256": self.sha256,
                "registered": self.registered, "registered_on": self.registered_on}


def _dir(strategy_id: str, root: Path | None) -> Path:
    if not _ID.match(strategy_id or ""):
        raise ConfigError(f"invalid strategy id {strategy_id!r}")
    return Path(root or STRATEGIES_DIR) / strategy_id


def load_index(strategy_id: str, root: Path | None = None) -> dict[str, dict[str, Any]]:
    p = _dir(strategy_id, root) / "VERSIONS.toml"
    if not p.is_file():
        return {}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{strategy_id}/VERSIONS.toml is not valid TOML: {exc}") from exc
    if set(raw) - {"versions"}:
        raise ConfigError(f"{strategy_id}/VERSIONS.toml may only contain [versions]")
    out = {}
    for ver, entry in (raw.get("versions") or {}).items():
        if not _VERSION.match(ver) or not isinstance(entry, dict) or set(entry) - {"sha256", "registered", "note"} \
                or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise ConfigError(f"{strategy_id}/VERSIONS.toml entry {ver!r} is malformed")
        out[ver] = entry
    return out


def list_versions(strategy_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    d = _dir(strategy_id, root)
    index = load_index(strategy_id, root)
    rows = []
    for f in sorted(d.glob("*.toml")) if d.is_dir() else []:
        if f.stem in RESERVED:
            continue
        sha = content_sha256(f)
        reg = index.get(f.stem)
        state = "UNREGISTERED" if reg is None else ("REGISTERED" if reg["sha256"] == sha else "MODIFIED_AFTER_REGISTRATION")
        rows.append({"strategy": strategy_id, "version": f.stem, "sha256": sha, "state": state,
                     "registered": reg.get("registered") if reg else None})
    for ver in sorted(set(index) - {r["version"] for r in rows}):
        rows.append({"strategy": strategy_id, "version": ver, "sha256": index[ver]["sha256"], "state": "FILE_MISSING",
                     "registered": index[ver].get("registered")})
    return rows


def load_version(strategy_id: str, version: str, app, *, allow_unregistered: bool = False,
                 root: Path | None = None) -> ParameterVersion:
    from src.research.registry import get_spec

    spec = get_spec(strategy_id)
    if not _VERSION.match(version or "") or version in RESERVED:
        raise ConfigError(f"invalid version {version!r}")
    path = _dir(strategy_id, root) / f"{version}.toml"
    if not path.is_file():
        raise ConfigError(f"no parameter version {strategy_id}@{version} (never substituted by another version)")
    sha = content_sha256(path)
    reg = load_index(strategy_id, root).get(version)
    if reg is not None and reg["sha256"] != sha:
        raise ConfigError(f"{strategy_id}@{version} was modified after registration (registered "
                          f"{reg['sha256'][:12]}, file {sha[:12]}): versions are immutable -- create a new version")
    if reg is None and not allow_unregistered:
        raise ConfigError(f"{strategy_id}@{version} is not registered in VERSIONS.toml (register it, or load it "
                          "explicitly as an unregistered exploration version)")
    params = spec.load_params(path, app, expected_version=version)
    return ParameterVersion(strategy_id, version, path, sha, reg is not None,
                            reg.get("registered") if reg else None, params)


def load_bounds(strategy_id: str, root: Path | None = None) -> dict[str, tuple[float, float]]:
    """Declared research bounds (``BOUNDS.toml``): the only parameters a sweep may vary, and their ranges."""
    p = _dir(strategy_id, root) / "BOUNDS.toml"
    if not p.is_file():
        return {}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{strategy_id}/BOUNDS.toml is not valid TOML: {exc}") from exc
    if set(raw) - {"bounds"}:
        raise ConfigError(f"{strategy_id}/BOUNDS.toml may only contain [bounds]")
    out = {}
    for k, v in (raw.get("bounds") or {}).items():
        if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                            for x in v) and v[0] <= v[1]):
            raise ConfigError(f"{strategy_id}/BOUNDS.toml: {k} must be [low, high]")
        out[k] = (v[0], v[1])
    return out


def check_bounds(strategy_id: str, overrides: dict[str, Any], root: Path | None = None) -> None:
    """Refuse any override that is not declared in BOUNDS.toml or lies outside its declared range."""
    bounds = load_bounds(strategy_id, root)
    for k, v in overrides.items():
        if k not in bounds:
            raise ConfigError(f"{k} is not a declared research parameter of {strategy_id} (BOUNDS.toml: "
                              f"{sorted(bounds)})")
        lo, hi = bounds[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ConfigError(f"{k} = {v!r} is outside the declared research bounds [{lo}, {hi}]")


def register_version(strategy_id: str, version: str, app, *, note: str = "", root: Path | None = None) -> dict[str, Any]:
    """Validate a version file and ADD it to the index. Never overwrites an existing registration."""
    index = load_index(strategy_id, root)
    if version in index:
        raise ConfigError(f"{strategy_id}@{version} is already registered: versions are immutable")
    pv = load_version(strategy_id, version, app, allow_unregistered=True, root=root)
    entry = {"sha256": pv.sha256, "registered": str(pd.Timestamp.now(tz="UTC").date()),
             "note": note.replace('"', "'")[:200]}
    p = _dir(strategy_id, root) / "VERSIONS.toml"
    text = p.read_text(encoding="utf-8") if p.is_file() else (
        "# Registered (immutable) parameter versions. Entries are only ever appended.\n")
    if not text.endswith("\n"):
        text += "\n"
    text += f'\n[versions."{version}"]\nsha256 = "{entry["sha256"]}"\nregistered = "{entry["registered"]}"\n'
    if entry["note"]:
        text += f'note = "{entry["note"]}"\n'
    p.write_text(text, encoding="utf-8")
    return {"strategy": strategy_id, "version": version, **entry}
