"""Normalized internal bar schema (Sprint 12).

One row per bar of ONE individual futures contract (the primary source of truth)::

    ts_utc      bar OPEN time, UTC (tz-aware), on the resolution grid
    root        MES / MNQ
    contract    individual contract, e.g. MESH25 (never a continuous symbol in a per-contract dataset)
    open, high, low, close   float prices exactly as delivered (never adjusted in per-contract datasets)
    volume      float, contracts traded in the bar
    source_ts   the original timestamp text / value as delivered by the provider (provenance)
    source_file index of the raw file the row came from (into the manifest's raw file list)

Continuous series are DERIVED datasets with the same columns plus ``segment`` (roll segment number) and,
only for explicitly adjusted series, ``adjustment`` (the additive / multiplicative factor applied).
"""

from __future__ import annotations

import gzip
import hashlib
import io
from pathlib import Path

import numpy as np
import pandas as pd

SCHEMA_VERSION = "bars/1"
NORMALIZATION_VERSION = "2026-11-25.1"
COLUMNS = ["ts_utc", "root", "contract", "open", "high", "low", "close", "volume", "source_ts", "source_file"]
PRICE = ["open", "high", "low", "close"]


def write_bars(frame: pd.DataFrame, path: Path) -> str:
    """Deterministic gzip CSV (mtime 0, fixed column order, ISO UTC timestamps, repr floats). Returns SHA-256."""
    f = frame.copy()
    f["ts_utc"] = pd.to_datetime(f["ts_utc"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = f.to_csv(index=False, lineterminator="\n", float_format="%.10g")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as gz:
        gz.write(text.encode("utf-8"))
    data = buf.getvalue()
    Path(path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def read_bars(path: Path) -> pd.DataFrame:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        f = pd.read_csv(fh, dtype={"root": str, "contract": str, "source_ts": str})
    f["ts_utc"] = pd.to_datetime(f["ts_utc"], utc=True)
    for c in PRICE + ["volume"]:
        f[c] = f[c].astype("float64")
    if "source_file" in f.columns:
        f["source_file"] = f["source_file"].astype("int64")
    return f


def frame_sha256(frame: pd.DataFrame) -> str:
    """Content hash of a bar frame independent of file encoding (used to compare datasets)."""
    f = frame[[c for c in ("ts_utc", "contract", "open", "high", "low", "close", "volume") if c in frame]].copy()
    # nanoseconds explicitly: the internal resolution (ns / us) differs between pandas versions and inputs
    f["ts_utc"] = pd.to_datetime(f["ts_utc"], utc=True).dt.as_unit("ns").astype("int64")
    for c in ("open", "high", "low", "close", "volume"):
        if c in f:
            f[c] = f[c].astype("float64")
    f["contract"] = f["contract"].astype(str) if "contract" in f else f.get("contract")
    return hashlib.sha256(pd.util.hash_pandas_object(f, index=False).to_numpy().tobytes()).hexdigest()


def empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in zip(
        COLUMNS, ["datetime64[ns, UTC]", str, str, float, float, float, float, float, str, np.int64])})


__all__ = ["COLUMNS", "SCHEMA_VERSION", "NORMALIZATION_VERSION", "write_bars", "read_bars", "frame_sha256"]
