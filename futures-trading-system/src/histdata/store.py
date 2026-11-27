"""Immutable, versioned dataset store (Sprint 12).

Layout (``data/datasets`` or ``FTS_DATASETS_ROOT``)::

    registry.jsonl                       append-only, hash-chained (HMAC with FTS_JOURNAL_KEY) registry
    <dataset_id>/<version>/manifest.json identity, provenance, hashes of every file (written LAST)
    <dataset_id>/<version>/raw/...       the provider files exactly as received (byte copies)
    <dataset_id>/<version>/bars.csv.gz   normalized bars (deterministic gzip CSV)
    <dataset_id>/<version>/validation.json, gaps.csv
    .staging/<uuid>/                     work in progress (never read as a dataset)

Rules:

* A version is written into ``.staging``, reserved in the registry (manifest hash), then moved into place
  with one rename. A crash leaves either a staging folder (removed by :func:`DatasetStore.recover`) or a
  reserved version whose staging folder is completed by ``recover`` only if its manifest hash matches.
* An existing version is NEVER overwritten or modified. New source data or corrections create a new version.
  Re-ingesting the identical source files returns the existing version.
* Every load re-verifies: registry record, manifest self-hash, manifest hash == registry hash, every file
  hash, and that no file path leaves the version folder. Any mismatch (corruption, missing file, tampered or
  substituted manifest) refuses the dataset.
* Status changes (cross-source validation, a human downgrade) are NEW registry records with evidence; the
  data never changes.
* Data conflicts between datasets (Sprint 13 P0) live in a separate chained register (``conflicts.jsonl``, see
  :mod:`src.histdata.conflicts`). Every load attaches the dataset's conflict state; a superseded dataset or one
  with an unresolved conflict is refused for research, plans and continuous series (:meth:`Dataset.usage_refusal`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.backtest.manifest import file_sha256, text_sha256
from src.config import PROJECT_ROOT, ConfigError
from src.histdata import schema
from src.histdata.calendar import Calendar
from src.histdata.conflicts import ConflictRegister, compare_datasets, usage_refusal
from src.histdata.providers import ProviderAdapter
from src.histdata.quality import Report, normalize, validate
from src.research.chain import ChainError, ChainStore

DATASETS_ENV = "FTS_DATASETS_ROOT"
DEFAULT_ROOT = PROJECT_ROOT / "data" / "datasets"
STATUSES = ("UNVERIFIED", "INGESTED", "VALIDATION_FAILED", "VALIDATED", "CROSS_SOURCE_VALIDATED")
RANK = {"VALIDATION_FAILED": 0, "UNVERIFIED": 1, "INGESTED": 2, "VALIDATED": 3, "CROSS_SOURCE_VALIDATED": 4}
_ID = re.compile(r"^[a-z0-9][a-z0-9_.\-]{2,79}$")
_VER = re.compile(r"^v[1-9][0-9]{0,5}$")
_FILE = re.compile(r"^(raw/)?[A-Za-z0-9][A-Za-z0-9_.\- ]{0,150}$")


class DatasetError(ConfigError):
    """A dataset request was refused (integrity, immutability or identity problem)."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def parse_ref(ref: str) -> tuple[str, str]:
    did, _, ver = str(ref).partition("@")
    if not _ID.match(did) or not _VER.match(ver):
        raise DatasetError(f"invalid dataset reference {ref!r} (expected <id>@v<N>)")
    return did, ver


@dataclass
class Dataset:
    id: str
    version: str
    path: Path
    manifest: dict[str, Any]
    status: str
    status_history: list[dict[str, Any]]
    conflict_state: dict[str, Any] | None = None
    files_verified: bool = True              # False: metadata-only load (no data file was read)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def usage_refusal(self) -> str | None:
        """Why this dataset must not be used for research / plans / continuous series (None when usable)."""
        why = usage_refusal(self.conflict_state)
        return f"{self.ref} is refused: {why}" if why else None

    def _require_files(self) -> None:
        if not self.files_verified:
            raise DatasetError(f"{self.ref} was loaded from its manifest only: load() it (verifying every file) before "
                               "reading its data")

    def bars(self) -> pd.DataFrame:
        self._require_files()
        return schema.read_bars(self.path / "bars.csv.gz")

    def validation(self) -> dict[str, Any]:
        self._require_files()
        return json.loads((self.path / "validation.json").read_text(encoding="utf-8"))

    def identity(self) -> dict[str, Any]:
        m = self.manifest
        return {"ref": self.ref, "manifest_sha256": m["manifest_sha256"], "status": self.status,
                "provider": m["provider"]["adapter"], "kind": m["kind"], "adjustment": m["adjustment"],
                "synthetic": m["synthetic"], "contracts": sorted(m["contracts"]), "resolution": m["resolution"],
                "bars_content_sha256": m["bars_content_sha256"]}


class DatasetStore:
    def __init__(self, root: str | Path | None = None) -> None:
        raw = root or os.environ.get(DATASETS_ENV)
        self.root = Path(raw).resolve() if raw else DEFAULT_ROOT
        self.registry = ChainStore(self.root / "registry.jsonl", kinds=("reserved", "status"))
        self.conflicts = ConflictRegister(self.root)

    # ------------------------------------------------------------------ registry
    def records(self) -> list[dict[str, Any]]:
        return self.registry.records()

    def versions(self, dataset_id: str | None = None) -> list[dict[str, Any]]:
        out = {}
        for r in self.records():
            if dataset_id and r["dataset_id"] != dataset_id:
                continue
            key = (r["dataset_id"], r["version"])
            if r["kind"] == "reserved":
                out[key] = {"dataset_id": r["dataset_id"], "version": r["version"],
                            "manifest_sha256": r["manifest_sha256"], "status": r["status"], "created": r["at"],
                            "synthetic": r.get("synthetic"), "kind": r.get("dataset_kind")}
            elif key in out:
                out[key]["status"] = r["status"]
        return list(out.values())

    def _next_version(self, dataset_id: str) -> str:
        n = [int(v["version"][1:]) for v in self.versions(dataset_id)]
        return f"v{max(n) + 1 if n else 1}"

    # ------------------------------------------------------------------ load / verify
    def load(self, ref: str) -> Dataset:
        """Load and verify a dataset: registry record, manifest self-hash and EVERY data file hash."""
        return self._load(ref, files=True)

    def load_manifest(self, ref: str) -> Dataset:
        """METADATA-ONLY load (Sprint 13 P1.1): registry record, manifest self-hash and registry match, status and
        conflict state. No data file is opened or hashed; the returned dataset refuses ``bars()`` and
        ``validation()``. Used for decisions that must be taken before any data byte is read."""
        return self._load(ref, files=False)

    def _load(self, ref: str, *, files: bool) -> Dataset:
        did, ver = parse_ref(ref)
        recs = [r for r in self.records() if r["dataset_id"] == did and r["version"] == ver]
        res = [r for r in recs if r["kind"] == "reserved"]
        if not res:
            raise DatasetError(f"dataset {ref} is not registered")
        if len(res) > 1:
            raise DatasetError(f"dataset {ref} was reserved {len(res)} times (concurrent ingestion): ambiguous, refused")
        path = self.root / did / ver
        if not (path / "manifest.json").is_file():
            raise DatasetError(f"dataset {ref} is registered but its folder is missing or incomplete "
                               "(run `run_data.py recover`, or restore it from backup)")
        m = self._verify(path, res[0]["manifest_sha256"], did, ver, verify_files=files)
        status = res[0]["status"]
        hist = [{"at": res[0]["at"], "status": status, "by": "ingestion", "evidence": None}]
        for r in recs:
            if r["kind"] == "status":
                status = r["status"]
                hist.append({"at": r["at"], "status": r["status"], "by": r.get("by"), "evidence": r.get("evidence")})
        try:
            conflict_state = self.conflicts.state(f"{did}@{ver}")
        except ConfigError as exc:                    # broken register or evidence: fail closed
            raise DatasetError(f"{did}@{ver}: conflict register integrity failure: {exc}") from exc
        return Dataset(did, ver, path, m, status, hist, conflict_state, files_verified=files)

    def _verify(self, path: Path, expected_sha: str, did: str, ver: str, *, verify_files: bool = True) -> dict[str, Any]:
        try:
            m = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetError(f"{did}@{ver}: manifest unreadable: {exc}") from exc
        claimed = m.get("manifest_sha256")
        body = {k: v for k, v in m.items() if k != "manifest_sha256"}
        if claimed != text_sha256(_canonical(body)):
            raise DatasetError(f"{did}@{ver}: manifest was modified (self-hash mismatch)")
        if claimed != expected_sha:
            raise DatasetError(f"{did}@{ver}: manifest does not match the registry (substituted or replaced)")
        if m.get("dataset_id") != did or m.get("version") != ver:
            raise DatasetError(f"{did}@{ver}: manifest names another dataset ({m.get('dataset_id')}@{m.get('version')})")
        files = dict(m.get("files") or {})
        for raw in m.get("source_files", []):
            files[raw["stored_as"]] = raw["sha256"]
        for name, sha in files.items():
            if not _FILE.match(name) or ".." in name:
                raise DatasetError(f"{did}@{ver}: unsafe file name {name!r} in manifest")
            if not verify_files:                      # metadata-only: no data file is opened, stat'ed or hashed
                continue
            p = path / name
            if not p.is_file():
                raise DatasetError(f"{did}@{ver}: file {name} is missing")
            if file_sha256(p) != sha:
                raise DatasetError(f"{did}@{ver}: file {name} was modified (hash mismatch)")
        return m

    # ------------------------------------------------------------------ write path
    def _stage(self) -> Path:
        st = self.root / ".staging" / uuid.uuid4().hex
        st.mkdir(parents=True)
        (st / "raw").mkdir()
        return st

    def _finalize(self, st: Path, manifest: dict[str, Any], status: str) -> str:
        did, ver = manifest["dataset_id"], manifest["version"]
        body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
        manifest["manifest_sha256"] = text_sha256(_canonical(body))
        (st / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
        final = self.root / did / ver
        if final.exists():
            raise DatasetError(f"{did}@{ver} already exists: versions are never overwritten")
        if any(r["kind"] == "reserved" and r["dataset_id"] == did and r["version"] == ver for r in self.records()):
            raise DatasetError(f"{did}@{ver} is already reserved (another ingestion is running?): retry")
        self.registry.append({"kind": "reserved", "dataset_id": did, "version": ver,
                              "manifest_sha256": manifest["manifest_sha256"], "status": status,
                              "synthetic": manifest["synthetic"], "dataset_kind": manifest["kind"],
                              "staging": st.name})
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(st, final)
        _read_only(final)
        return manifest["manifest_sha256"]

    def ingest(self, dataset_id: str, adapter: ProviderAdapter, files: list[Path], *, specs, calendar: Calendar,
               resolution: str = "1min", description: str = "", base: str | None = None,
               allow_corrections: bool = False, bars_from_trades: bool | None = None,
               provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a new version from provider files (or return the identical existing version).

        ``provenance``: optional human-recorded facts about the source (e.g. how the source timezone was
        established and its limitations). It is written verbatim into the manifest and the validation report.
        It never changes parsing (the adapter parameters do) and is not part of the identity used for
        idempotency: the same files with the same adapter return the existing version."""
        if not _ID.match(dataset_id or ""):
            raise DatasetError(f"invalid dataset id {dataset_id!r}")
        provenance = _check_provenance(provenance)
        files = [Path(f) for f in files]
        base_ds = self.load(base) if base else None
        if base_ds is not None and base_ds.id != dataset_id:
            raise DatasetError("an incremental update must extend the same dataset id")
        all_files: list[tuple[Path, str]] = []
        if base_ds is not None:
            if base_ds.manifest["kind"] != "per_contract":
                raise DatasetError("only per-contract datasets can be extended")
            all_files += [(base_ds.path / s["stored_as"], s["name"]) for s in base_ds.manifest["source_files"]]
        for f in files:
            if not f.is_file():
                raise DatasetError(f"source file not found: {f}")
            all_files.append((f, f.name))
        hashes = [file_sha256(p) for p, _ in all_files]
        if len(set(hashes)) != len(hashes):
            raise DatasetError("the same source file was given twice")
        changed: list[dict[str, Any]] = []
        n_base = len(all_files) - len(files)
        new_files = {name: sha for (_, name), sha in list(zip(all_files, hashes))[n_base:]}   # not carried base files
        for v in self.versions(dataset_id):
            ds = self.load(f"{dataset_id}@{v['version']}")
            if sorted(s["sha256"] for s in ds.manifest["source_files"]) == sorted(hashes) \
                    and ds.manifest["provider"] == adapter.identity():
                return {"created": False, "ref": ds.ref, "status": ds.status, "reason": "identical source files"}
            for s in ds.manifest["source_files"]:
                if s["name"] in new_files and new_files[s["name"]] != s["sha256"]:
                    changed.append({"name": s["name"], "previous_ref": ds.ref, "previous_sha256": s["sha256"],
                                    "sha256": new_files[s["name"]]})
        trades_built = adapter.name == "databento_ohlcv_csv" if bars_from_trades is None else bars_from_trades
        st = self._stage()
        try:
            rep = Report()
            if changed:
                rep.add("warning", "SOURCE_FILE_CHANGED", "a source file with the same name but different content "
                        "(SHA-256) was ingested before: this is a NEW version, the earlier one is unchanged",
                        len(changed), [f"{c['name']}: {c['previous_ref']} {c['previous_sha256'][:12]} -> "
                                       f"{c['sha256'][:12]}" for c in changed])
            quarantine: list[dict[str, Any]] = []
            records, sources, synthetic = [], [], False
            for i, ((p, name), sha) in enumerate(zip(all_files, hashes)):
                stored = f"raw/{sha[:16]}_{re.sub(r'[^A-Za-z0-9_.-]', '_', name)[:100]}"
                shutil.copyfile(p, st / stored)
                parsed = adapter.parse(st / stored, name=name)
                synthetic |= parsed.synthetic or "SYNTHETIC" in name.upper()
                r = parsed.records.assign(source_file=i)
                records.append(r)
                if parsed.rejected:
                    rep.add("error", "BAD_SOURCE_ROWS", f"{name}: rows that could not be parsed (excluded; every "
                            "one is listed in rejected.csv)",
                            len(parsed.rejected), [x.get("value", x["reason"]) for x in parsed.rejected])
                    quarantine += [{"source_file": i, "name": name, "row": x.get("row"), "reason": x["reason"],
                                    "value": x.get("value", "")} for x in parsed.rejected]
                first = parsed.records["source_ts"].iloc[0] if len(parsed.records) else None
                last = parsed.records["source_ts"].iloc[-1] if len(parsed.records) else None
                sources.append({"name": name, "stored_as": stored, "sha256": sha, "bytes": (st / stored).stat().st_size,
                                "rows": int(len(parsed.records)), "rejected": len(parsed.rejected),
                                "timestamp_semantics": parsed.timestamp_semantics,
                                "source_timezone": parsed.source_timezone, "notes": parsed.notes,
                                "parse_stats": parsed.stats, "first_source_ts": first, "last_source_ts": last,
                                "from_base": base_ds is not None and i < len(base_ds.manifest["source_files"])})
            recs = pd.concat(records, ignore_index=True) if records else pd.DataFrame()
            if base_ds is not None:
                recs = self._apply_corrections(recs, len(base_ds.manifest["source_files"]), base_ds,
                                               allow_corrections, rep)
            bars = normalize(recs, specs, calendar, resolution=resolution, report=rep)
            if base_ds is not None:
                self._check_extension(base_ds, bars, allow_corrections, rep)
            body = validate(bars, specs, calendar, resolution=resolution, bars_from_trades=trades_built, report=rep,
                            synthetic=synthetic)
            extra = {}
            if quarantine:
                pd.DataFrame(quarantine, columns=["source_file", "name", "row", "reason", "value"]).to_csv(
                    st / "rejected.csv", index=False, lineterminator="\n")
                extra["rejected.csv"] = file_sha256(st / "rejected.csv")
            body["provenance"] = provenance
            body["changed_sources"] = changed
            ver = self._next_version(dataset_id)
            out = self._write(st, dataset_id, ver, bars, body, sources, adapter, calendar, resolution, description,
                              synthetic, base_ds, trades_built, extra_files=extra, provenance=provenance)
            out["rejected_rows"] = len(quarantine)
            out["changed_sources"] = changed
            return out
        except BaseException:
            shutil.rmtree(st, ignore_errors=True)
            raise

    @staticmethod
    def _apply_corrections(recs: pd.DataFrame, n_base: int, base: Dataset, allow: bool, rep: Report) -> pd.DataFrame:
        """New source rows that DIFFER from base rows for the same symbol and minute are corrections: refused
        unless explicitly allowed; when allowed, the new row replaces the base row and the change is recorded."""
        vals = ["open", "high", "low", "close", "volume"]
        key = ["symbol", "ts_utc"]
        old = recs[recs["source_file"] < n_base]
        new = recs[recs["source_file"] >= n_base]
        m = old.reset_index().merge(new[key + vals], on=key, suffixes=("", "_new"))
        if m.empty:
            return recs
        diff = pd.Series(False, index=m.index)
        for c in vals:
            diff |= m[c] != m[f"{c}_new"]
        if not diff.any():
            return recs
        ex = [f"{r.symbol} {r.ts_utc}" for r in m[diff].head().itertuples()]
        if not allow:
            raise DatasetError(f"{int(diff.sum())} existing bar(s) of {base.ref} would change (e.g. {ex}); pass "
                               "--allow-corrections to create a corrected version (recorded)")
        rep.add("warning", "CORRECTIONS", f"bars of {base.ref} replaced by corrected source data (recorded)",
                int(diff.sum()), ex)
        return recs.drop(index=m.loc[diff, "index"].to_numpy())

    def _check_extension(self, base: Dataset, bars: pd.DataFrame, allow_corrections: bool, rep: Report) -> None:
        old = base.bars()
        key = ["contract", "ts_utc"]
        m = old.merge(bars, on=key, how="left", suffixes=("_old", ""), indicator=True)
        lost = m["_merge"] == "left_only"
        if lost.any():
            raise DatasetError(f"the new version would drop {int(lost.sum())} bar(s) of {base.ref}; history is never "
                               "removed")
        diff = pd.Series(False, index=m.index)
        for c in ("open", "high", "low", "close", "volume"):
            diff |= m[f"{c}_old"] != m[c]
        if diff.any():
            ex = [f"{r.contract} {r.ts_utc}" for r in m[diff].head().itertuples()]
            if not allow_corrections:
                raise DatasetError(f"{int(diff.sum())} existing bar(s) of {base.ref} would change (e.g. {ex}); "
                                   "pass --allow-corrections to create a corrected version (recorded)")
            rep.add("warning", "CORRECTIONS", f"bars of {base.ref} changed by the new source data (recorded)",
                    int(diff.sum()), ex)

    def _write(self, st: Path, did: str, ver: str, bars: pd.DataFrame, body: dict[str, Any], sources: list,
               adapter: ProviderAdapter, calendar: Calendar, resolution: str, description: str, synthetic: bool,
               base: Dataset | None, trades_built: bool, *, kind: str = "per_contract", adjustment: str = "none",
               lineage: dict[str, Any] | None = None, extra_files: dict[str, str] | None = None,
               provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        files = {"bars.csv.gz": schema.write_bars(bars, st / "bars.csv.gz")}
        gaps = body.pop("gaps")
        gaps.to_csv(st / "gaps.csv", index=False)
        files["gaps.csv"] = file_sha256(st / "gaps.csv")
        status = body["status"]
        (st / "validation.json").write_text(json.dumps(body, indent=2, sort_keys=True, default=str), encoding="utf-8")
        files["validation.json"] = file_sha256(st / "validation.json")
        for name, sha in (extra_files or {}).items():
            files[name] = sha
        contracts = {c: {"first_bar": str(g["ts_utc"].min()), "last_bar": str(g["ts_utc"].max()), "bars": int(len(g))}
                     for c, g in bars.groupby("contract")} if len(bars) else {}
        manifest = {
            "schema": schema.SCHEMA_VERSION, "normalization_version": schema.NORMALIZATION_VERSION,
            "dataset_id": did, "version": ver, "kind": kind, "adjustment": adjustment, "description": description[:500],
            "created_at": pd.Timestamp.now(tz="UTC").isoformat(), "provider": adapter.identity(),
            "bars_built_from_trades": trades_built, "source_files": sources,
            "roots": sorted(set(bars["root"])) if len(bars) else [], "contracts": contracts,
            "first_bar_utc": str(bars["ts_utc"].min()) if len(bars) else None,
            "last_bar_utc": str(bars["ts_utc"].max()) if len(bars) else None, "rows": int(len(bars)),
            "resolution": resolution, "timestamps": "UTC, bar open", "calendar": calendar.identity(),
            "validation": {"status": status, "errors": sum(f["severity"] == "error" for f in body["findings"]),
                           "warnings": sum(f["severity"] == "warning" for f in body["findings"]), "gate": body["gate"]},
            "synthetic": bool(synthetic), "files": files, "bars_content_sha256": schema.frame_sha256(bars),
            "lineage": lineage or ({"base": base.ref, "base_manifest_sha256": base.manifest["manifest_sha256"]}
                                   if base is not None else None),
        }
        if provenance is not None:
            manifest["provenance"] = provenance
        sha = self._finalize(st, manifest, status)
        return {"created": True, "ref": f"{did}@{ver}", "status": status, "manifest_sha256": sha,
                "errors": manifest["validation"]["errors"], "warnings": manifest["validation"]["warnings"]}

    def create_continuous(self, dataset_id: str, sources: list[str], *, specs, calendar: Calendar,
                          rule: str = "calendar", days_before_expiry: int = 5, confirm_days: int = 1,
                          adjustment: str = "none", description: str = "") -> dict[str, Any]:
        """A DERIVED continuous series from per-contract datasets (see src.histdata.rollover)."""
        from src.histdata.rollover import build_continuous

        if not _ID.match(dataset_id or ""):
            raise DatasetError(f"invalid dataset id {dataset_id!r}")
        loaded = [self.load(r) for r in sources]
        if not loaded:
            raise DatasetError("a continuous series needs at least one per-contract source dataset")
        for d in loaded:
            if d.manifest["kind"] != "per_contract" or d.manifest["adjustment"] != "none":
                raise DatasetError(f"{d.ref} is not an unadjusted per-contract dataset")
            if RANK[d.status] < RANK["VALIDATED"]:
                raise DatasetError(f"{d.ref} is {d.status}: a continuous series needs VALIDATED sources (fix or "
                                   "remediate the source first; nothing is dropped or repaired here)")
            why = d.usage_refusal()
            if why:
                raise DatasetError(why)
        res = {d.manifest["resolution"] for d in loaded}
        if len(res) != 1:
            raise DatasetError(f"sources have different resolutions {sorted(res)}")
        frames = [d.bars() for d in loaded]
        bars = pd.concat(frames, ignore_index=True)
        key = ["contract", "ts_utc"]
        dup = bars.duplicated(key, keep=False)
        if dup.any():
            vals = ["open", "high", "low", "close", "volume"]
            if bars[dup].groupby(key)[vals].nunique().gt(1).any(axis=None):
                raise DatasetError("source datasets disagree on the same contract and minute: resolve with a "
                                   "cross-source comparison first")
            bars = bars.drop_duplicates(key, keep="first")
        accounting: dict[str, Any] = {}
        cont, rolls = build_continuous(bars, calendar, rule=rule, days_before_expiry=days_before_expiry,
                                       confirm_days=confirm_days, adjustment=adjustment, report=accounting)
        st = self._stage()
        try:
            rolls.to_csv(st / "rolls.csv", index=False)
            rep = Report()
            synthetic = any(d.manifest["synthetic"] for d in loaded)
            body = validate(cont, specs, calendar, resolution=res.pop(), bars_from_trades=any(
                d.manifest["bars_built_from_trades"] for d in loaded), report=rep, synthetic=synthetic)
            worst = min([body["status"]] + [d.status for d in loaded], key=lambda x: RANK[x])
            if worst == "CROSS_SOURCE_VALIDATED":
                worst = "VALIDATED"       # cross-source evidence is per dataset; a derived series is re-checked
            body["status"] = worst
            body["inherited_from"] = {d.ref: d.status for d in loaded}
            source = _Derived(rule, days_before_expiry, confirm_days, adjustment)
            ver = self._next_version(dataset_id)
            return self._write(st, dataset_id, ver, cont, body, [], source, calendar, loaded[0].manifest["resolution"],
                               description, synthetic, None, any(d.manifest["bars_built_from_trades"] for d in loaded),
                               kind="continuous", adjustment=adjustment,
                               lineage={"sources": [{"ref": d.ref, "manifest_sha256": d.manifest["manifest_sha256"],
                                                     "status": d.status} for d in loaded],
                                        "roll_rule": source.identity()["params"], "rolls": len(rolls),
                                        "roll_accounting": accounting},
                               extra_files={"rolls.csv": file_sha256(st / "rolls.csv")})
        except BaseException:
            shutil.rmtree(st, ignore_errors=True)
            raise

    # ------------------------------------------------------------------ status changes (evidence-based)
    def set_status(self, ref: str, status: str, *, by: str, evidence: dict[str, Any]) -> dict[str, Any]:
        ds = self.load(ref)
        if status not in STATUSES or status == "UNVERIFIED":
            raise DatasetError(f"invalid status {status!r}")
        if status in ("VALIDATED", "INGESTED"):
            raise DatasetError("VALIDATED / INGESTED are computed by the validation gate at ingestion, never set")
        if status == "CROSS_SOURCE_VALIDATED" and RANK[ds.status] < RANK["VALIDATED"]:
            raise DatasetError(f"{ref} is {ds.status}: only a VALIDATED dataset can be cross-source validated")
        return self.registry.append({"kind": "status", "dataset_id": ds.id, "version": ds.version, "status": status,
                                     "by": by, "evidence": evidence})

    # ------------------------------------------------------------------ data conflicts (Sprint 13 P0)
    def compare(self, ref_a: str, ref_b: str) -> dict[str, Any]:
        """Read-only conflict report between two per-contract datasets (nothing is recorded)."""
        return compare_datasets(self.load(ref_a), self.load(ref_b))

    def record_conflict(self, report: dict[str, Any], *, by: str, note: str = "") -> dict[str, Any]:
        """Record a conflict report. Both datasets must still match the manifests the report was made from."""
        for side in ("a", "b"):
            ds = self.load(report[side]["ref"])
            if ds.manifest["manifest_sha256"] != report[side]["manifest_sha256"]:
                raise DatasetError(f"{ds.ref}: the report was made from another manifest; compare again")
        return self.conflicts.record(report, by=by, note=note)

    def supersede(self, old_ref: str, new_ref: str, *, by: str, reason: str) -> dict[str, Any]:
        """HUMAN decision recorded with evidence: ``old_ref`` is superseded by ``new_ref`` (nothing is deleted)."""
        self.load(old_ref)
        self.load(new_ref)
        return self.conflicts.supersede(old_ref, new_ref, by=by, reason=reason)

    # ------------------------------------------------------------------ recovery
    def recover(self) -> dict[str, Any]:
        """Complete reserved versions whose staging folder is intact; remove orphan staging folders."""
        done, removed, problems = [], [], []
        stage_root = self.root / ".staging"
        try:
            recs = self.records()
        except ChainError as exc:
            return {"completed": [], "removed": [], "problems": [f"registry integrity failure: {exc}"]}
        pending = {r["staging"]: r for r in recs if r["kind"] == "reserved" and not (self.root / r["dataset_id"] / r["version"]).exists()}
        if stage_root.is_dir():
            for st in sorted(stage_root.iterdir()):
                r = pending.get(st.name)
                if r is not None and (st / "manifest.json").is_file():
                    try:
                        m = json.loads((st / "manifest.json").read_text(encoding="utf-8"))
                        if m.get("manifest_sha256") == r["manifest_sha256"]:
                            final = self.root / r["dataset_id"] / r["version"]
                            final.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(st, final)
                            self._verify(final, r["manifest_sha256"], r["dataset_id"], r["version"])
                            _read_only(final)
                            done.append(f"{r['dataset_id']}@{r['version']}")
                            continue
                    except (OSError, json.JSONDecodeError, DatasetError) as exc:
                        problems.append(f"{st.name}: {exc}")
                        continue
                shutil.rmtree(st, ignore_errors=True)
                removed.append(st.name)
        for name, r in pending.items():
            if f"{r['dataset_id']}@{r['version']}" not in done and not (self.root / r["dataset_id"] / r["version"]).exists():
                problems.append(f"{r['dataset_id']}@{r['version']} is reserved but its data is lost (re-ingest as a "
                                "new version)")
        return {"completed": done, "removed": removed, "problems": problems}


class _Derived:
    name = "derived_continuous"

    def __init__(self, rule: str, days: int, confirm: int, adjustment: str) -> None:
        self.params = {"rule": rule, "days_before_expiry": days, "confirm_days": confirm, "adjustment": adjustment}

    def identity(self) -> dict[str, Any]:
        return {"adapter": self.name, "params": dict(sorted(self.params.items()))}


_SECRET_KEY = re.compile(r"pass(word)?|secret|token|api[_-]?key|credential|account[_-]?(id|number)|auth", re.I)


def _check_provenance(prov: dict[str, Any] | None) -> dict[str, Any] | None:
    """Provenance is free-form but bounded, JSON-serialisable and free of credential-like keys (manifests and
    reports are shareable; secrets never belong in them)."""
    if prov is None:
        return None
    if not isinstance(prov, dict):
        raise DatasetError("provenance must be a JSON object")
    try:
        text = json.dumps(prov, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise DatasetError(f"provenance is not JSON-serialisable: {exc}") from exc
    if len(text) > 20000:
        raise DatasetError("provenance is larger than 20000 characters")

    def keys(o: Any) -> list[str]:
        if isinstance(o, dict):
            return [str(k) for k in o] + [x for v in o.values() for x in keys(v)]
        if isinstance(o, list):
            return [x for v in o for x in keys(v)]
        return []

    bad = [k for k in keys(prov) if _SECRET_KEY.search(k)]
    if bad:
        raise DatasetError(f"provenance contains credential-like keys {bad}: secrets never go into manifests")
    return json.loads(text)


def _read_only(path: Path) -> None:
    for p in sorted(path.rglob("*"), reverse=True):
        try:
            os.chmod(p, 0o444 if p.is_file() else 0o555)
        except OSError:
            pass
    try:
        os.chmod(path, 0o555)
    except OSError:
        pass


__all__ = ["DatasetStore", "Dataset", "DatasetError", "parse_ref", "STATUSES", "RANK", "DATASETS_ENV"]
