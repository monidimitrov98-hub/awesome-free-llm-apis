"""Provider adapters (Sprint 12): parse files obtained from a data provider into raw records.

The adapter interface is the ONLY provider-specific code. Every adapter returns the same raw-record frame::

    ts_utc (bar OPEN, UTC), symbol (as delivered), open, high, low, close, volume, source_ts (original text)

plus metadata (timestamp semantics, source timezone, synthetic marker). Normalization, validation, dataset
versioning and everything downstream are provider-neutral, so changing provider never touches strategies or
the backtester.

Adapters never fetch anything over the network and never read credentials: files are downloaded with the
provider's own tools (or exported from a platform) and then ingested. An adapter cannot write a dataset; it
only returns records that the store then validates.

Adapters:
* ``generic_csv``          -- configurable columns, timezone and open/close labelling (the project's CSV format).
* ``databento_ohlcv_csv``  -- Databento OHLCV CSV (``ts_event`` = interval START; integer nanoseconds or ISO;
                              fixed-point 1e-9 or decimal prices). Databento emits no bar for an interval
                              without trades (documented by the provider), so missing minutes are expected
                              in thin periods.
* ``ninjatrader_export``   -- NinjaTrader 8 "Export" text: ``yyyyMMdd HHmmss;open;high;low;close;volume``,
                              stamped at the bar END in the exporting PC's timezone (which must be given).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import ConfigError
from src.data.timestamps import normalize_to_utc

RAW_COLUMNS = ["ts_utc", "symbol", "open", "high", "low", "close", "volume", "source_ts"]


@dataclass
class ParsedFile:
    records: pd.DataFrame
    timestamp_semantics: str          # "bar_open" (after conversion) + what the source used
    source_timezone: str
    synthetic: bool = False
    rejected: list[dict[str, Any]] = field(default_factory=list)   # unparseable rows (never guessed)
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)            # file-level parsing facts (provenance)


class ProviderAdapter(ABC):
    name: str = ""
    description: str = ""
    params_allowed: tuple[str, ...] = ()

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.params_allowed)
        if unknown:
            raise ConfigError(f"adapter {self.name}: unknown parameter(s) {sorted(unknown)}")
        self.params = dict(params)

    @abstractmethod
    def parse(self, path: Path, name: str | None = None) -> ParsedFile:
        """``name``: the ORIGINAL file name (the store parses its own byte-identical copy)."""

    def identity(self) -> dict[str, Any]:
        return {"adapter": self.name, "params": dict(sorted(self.params.items()))}


def _synthetic_marker(path: Path, name: str | None = None) -> bool:
    if "SYNTHETIC" in (name or path.name).upper():
        return True
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for _ in range(5):
            line = fh.readline()
            if "SYNTHETIC" in line.upper():
                return True
    return False


def _numeric(df: pd.DataFrame, rejected: list, label: str) -> pd.DataFrame:
    for c in ("open", "high", "low", "close", "volume"):
        raw = df[c].map(lambda x: "" if pd.isna(x) else str(x).strip())   # NA-safe in pandas 2 and 3
        df[c] = pd.to_numeric(raw, errors="coerce").astype("float64")
        bad = (df[c].isna() & raw.ne("")).to_numpy()
        for i in df.index[bad]:
            rejected.append({"row": int(i), "reason": f"non-numeric {c}", "value": raw[i], "file": label})
    return df


class GenericCsvAdapter(ProviderAdapter):
    name = "generic_csv"
    description = "CSV with configurable columns; naive timestamps in `timezone`; bar open or close stamped"
    params_allowed = ("timezone", "label", "timestamp_format", "symbol", "delimiter", "columns",
                      "ambiguous", "nonexistent")

    def parse(self, path: Path, name: str | None = None) -> ParsedFile:
        p = self.params
        tz = p.get("timezone")
        if not tz:
            raise ConfigError("generic_csv needs `timezone` for naive timestamps (never assumed)")
        label = p.get("label", "open")
        if label not in ("open", "close"):
            raise ConfigError("label must be 'open' or 'close'")
        cols = {"timestamp": "timestamp", "open": "open", "high": "high", "low": "low", "close": "close",
                "volume": "volume", "symbol": "symbol", **(p.get("columns") or {})}
        raw = pd.read_csv(path, sep=p.get("delimiter", ","), comment="#", dtype=str, skipinitialspace=True)
        raw.columns = [c.strip() for c in raw.columns]
        lookup = {c.lower(): c for c in raw.columns}
        missing = [k for k in ("timestamp", "open", "high", "low", "close", "volume") if cols[k].lower() not in lookup]
        if missing:
            raise ConfigError(f"{path.name}: missing column(s) {missing}")
        df = pd.DataFrame({k: raw[lookup[cols[k].lower()]] for k in ("timestamp", "open", "high", "low", "close",
                                                                     "volume")})
        if p.get("symbol"):
            df["symbol"] = str(p["symbol"])
        elif cols["symbol"].lower() in lookup:
            df["symbol"] = raw[lookup[cols["symbol"].lower()]].map(lambda x: x if pd.isna(x) else str(x).strip())
        else:
            raise ConfigError(f"{path.name}: no symbol column and no `symbol` parameter (contract never guessed)")
        rejected: list = []
        df = _numeric(df, rejected, path.name)
        norm = normalize_to_utc(df["timestamp"], tz, fmt=p.get("timestamp_format") or None,
                                ambiguous=p.get("ambiguous", "raise"), nonexistent=p.get("nonexistent", "raise"))
        ts = norm.utc
        return _finish(df, ts, label, path, tz, rejected, self, name=name)


class DatabentoOhlcvCsvAdapter(ProviderAdapter):
    name = "databento_ohlcv_csv"
    description = "Databento OHLCV CSV (ts_event = interval start; ns epoch or ISO; fixed-point or decimal prices)"
    params_allowed = ("resolution",)
    RTYPES = {"1s": 32, "1min": 33, "1h": 34, "1d": 35}

    def parse(self, path: Path, name: str | None = None) -> ParsedFile:
        raw = pd.read_csv(path, dtype=str)
        need = {"ts_event", "open", "high", "low", "close", "volume", "symbol"}
        if need - set(raw.columns):
            raise ConfigError(f"{path.name}: not a Databento OHLCV CSV (missing {sorted(need - set(raw.columns))})")
        res = self.params.get("resolution", "1min")
        rejected: list = []
        if "rtype" in raw.columns:
            want = str(self.RTYPES.get(res))
            bad = (raw["rtype"].map(lambda x: "" if pd.isna(x) else str(x).strip()) != want).to_numpy()
            for i in raw.index[bad]:
                rejected.append({"row": int(i), "reason": f"rtype {raw.at[i, 'rtype']} is not {res} OHLCV ({want})",
                                 "file": path.name})
            raw = raw[~bad]
        df = raw[["ts_event", "open", "high", "low", "close", "volume", "symbol"]].rename(columns={"ts_event": "timestamp"})
        df = _numeric(df, rejected, path.name)
        px = df[["open", "high", "low", "close"]]
        if len(px) and float(np.nanmax(np.abs(px.to_numpy()))) > 1e8:      # fixed-point 1e-9 prices
            df[["open", "high", "low", "close"]] = px / 1e9
        t = df["timestamp"].map(lambda x: "" if pd.isna(x) else str(x).strip())
        if t.str.fullmatch(r"\d+").all():
            if not t.str.len().eq(19).all():        # ns since 1970: 19 digits from 2001 to 2286
                raise ConfigError(f"{path.name}: integer ts_event values are not nanosecond epochs (wrong unit "
                                  "would shift every timestamp); export with nanosecond or ISO timestamps")
            ts = pd.to_datetime(pd.to_numeric(t), unit="ns", utc=True)
        else:
            ts = pd.to_datetime(t, utc=True, errors="coerce", format="ISO8601")
        df["symbol"] = df["symbol"].map(lambda x: x if pd.isna(x) else str(x).strip())
        out = _finish(df, ts, "open", path, "UTC", rejected, self, name=name)
        out.notes.append("Databento OHLCV: no record is emitted for an interval without trades (provider "
                         "documentation); missing minutes in thin trading are expected, not errors")
        return out


class NinjaTraderExportAdapter(ProviderAdapter):
    name = "ninjatrader_export"
    description = "NinjaTrader 8 export 'yyyyMMdd HHmmss;o;h;l;c;v' stamped at bar END in the exporting PC timezone"
    params_allowed = ("timezone", "symbol", "ambiguous", "nonexistent", "resolution")
    _NT_FILE = re.compile(r"^(?P<root>[A-Z]{2,4}) (?P<mm>\d{2})-(?P<yy>\d{2})")

    def parse(self, path: Path, name: str | None = None) -> ParsedFile:
        tz = self.params.get("timezone")
        if not tz:
            raise ConfigError("ninjatrader_export needs `timezone` (the exporting PC's timezone; never assumed)")
        sym = self.params.get("symbol")
        if not sym:
            m = self._NT_FILE.match(name or path.name)
            if not m:
                raise ConfigError(f"{name or path.name}: contract not in the file name; pass `symbol`")
            sym = f"{m['root']} {m['mm']}-{m['yy']}"
        label = name or path.name
        df, rejected, stats, notes = self._read_lines(path, label)
        df["symbol"] = sym
        df = _numeric(df, rejected, label)
        norm = normalize_to_utc(df["timestamp"], tz, fmt="%Y%m%d %H%M%S",
                                ambiguous=self.params.get("ambiguous", "raise"),
                                nonexistent=self.params.get("nonexistent", "raise"))
        res = pd.Timedelta(self.params.get("resolution", "1min"))
        out = _finish(df, norm.utc, "close", path, tz, rejected, self, step=res, name=name)
        stats["rejected_rows"] = len(out.rejected)
        stats["accepted_rows"] = int(len(out.records))
        out.stats, out.notes = stats, notes + out.notes
        return out

    _TS = re.compile(r"^\d{8} \d{6}$")

    def _read_lines(self, path: Path, label: str) -> tuple[pd.DataFrame, list, dict[str, Any], list[str]]:
        """Strict line-based reading: every line is a data row, blank, a ``#`` comment or (first line only) a
        header. Anything else is REJECTED with its line number and reason; nothing is skipped silently.
        The DataFrame index is the 1-based line number, so every rejection names the source line."""
        data = path.read_bytes()
        bom = data.startswith(b"\xef\xbb\xbf")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{label}: not ASCII / UTF-8 text ({exc}); refused, nothing guessed") from exc
        lines = text.splitlines()
        crlf, lf = data.count(b"\r\n"), data.count(b"\n")
        stats: dict[str, Any] = {
            "bytes": len(data), "encoding": "ascii" if data.isascii() else ("utf-8-sig" if bom else "utf-8"),
            "bom": bom, "line_endings": "CRLF" if crlf and crlf == lf else ("LF" if not crlf else "mixed"),
            "delimiter": ";", "lines": len(lines), "blank_lines": 0, "comment_lines": 0, "header_lines": 0,
            "data_rows": 0}
        rows: list[list[Any]] = []
        rejected: list[dict[str, Any]] = []
        notes: list[str] = []
        for n, line in enumerate(lines, start=1):
            s = line.strip()
            if not s:
                stats["blank_lines"] += 1
                continue
            if s.startswith("#"):
                stats["comment_lines"] += 1
                continue
            parts = [p.strip() for p in s.split(";")]
            if len(parts) != 6:
                rejected.append({"row": n, "reason": f"expected 6 ';'-separated fields, got {len(parts)}",
                                 "value": s[:120], "file": label})
                continue
            if not self._TS.match(parts[0]):
                if not rows and not rejected and not stats["header_lines"] and \
                        all(re.fullmatch(r"[A-Za-z][A-Za-z _.]*", p) for p in parts):
                    stats["header_lines"] += 1
                    notes.append(f"line {n}: header line skipped ({s[:80]})")
                    continue
                rejected.append({"row": n, "reason": "malformed timestamp (expected yyyyMMdd HHmmss)",
                                 "value": s[:120], "file": label})
                continue
            rows.append([n] + parts)
        stats["data_rows"] = len(rows)
        if stats["blank_lines"]:
            notes.append(f"{stats['blank_lines']} blank line(s) ignored")
        cols = ["line", "timestamp", "open", "high", "low", "close", "volume"]
        df = pd.DataFrame(rows, columns=cols).set_index("line")
        df.index.name = None
        return df, rejected, stats, notes


def _finish(df: pd.DataFrame, ts: pd.Series, label: str, path: Path, tz: str, rejected: list,
            adapter: ProviderAdapter, step: pd.Timedelta | None = None, name: str | None = None) -> ParsedFile:
    ts = pd.Series(pd.DatetimeIndex(ts), index=df.index)
    bad = ts.isna()
    for i in df.index[bad]:
        rejected.append({"row": int(i), "reason": "unparseable or ambiguous timestamp",
                         "value": str(df.at[i, "timestamp"]), "file": path.name})
    if label == "close":
        step = step or pd.Timedelta(adapter.params.get("resolution", "1min"))
        ts = ts - step
    out = pd.DataFrame({"ts_utc": ts, "symbol": df["symbol"], "open": df["open"], "high": df["high"],
                        "low": df["low"], "close": df["close"], "volume": df["volume"],
                        "source_ts": df["timestamp"].astype(str)})
    out = out[~bad.to_numpy()].reset_index(drop=True)
    sem = "bar_open (source stamped at bar " + ("open" if label == "open" else "close; shifted back one bar") + ")"
    return ParsedFile(out, sem, tz, _synthetic_marker(path, name), rejected,
                      [f"{int(bad.sum())} row(s) with unusable timestamps rejected"] if bad.any() else [])


ADAPTERS: dict[str, type[ProviderAdapter]] = {
    a.name: a for a in (GenericCsvAdapter, DatabentoOhlcvCsvAdapter, NinjaTraderExportAdapter)}


def make_adapter(name: str, **params: Any) -> ProviderAdapter:
    cls = ADAPTERS.get(name)
    if cls is None:
        raise ConfigError(f"unknown provider adapter {name!r}; available: {sorted(ADAPTERS)}")
    return cls(**params)


__all__ = ["ProviderAdapter", "ParsedFile", "ADAPTERS", "make_adapter", "RAW_COLUMNS"]
