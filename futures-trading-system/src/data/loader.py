"""CSV loading.

Reads a vendor CSV, maps its columns to canonical names, converts timestamps
to UTC and numeric columns to floats. Problems are recorded in a
:class:`DataQualityReport`; rows are never invented.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.config import CANONICAL_COLUMNS, DataConfig
from src.data.errors import DataError
from src.data.report import DataQualityReport
from src.data.timestamps import normalize_to_utc

NUMERIC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def _resolve_columns(available: list[str], wanted: dict[str, str], symbol_column: str | None) -> dict[str, str]:
    """Map csv column name -> canonical name, matching case-insensitively."""
    lookup = {c.strip().lower(): c for c in available}
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for canonical, csv_name in wanted.items():
        actual = lookup.get(csv_name.strip().lower())
        if actual is None:
            missing.append(f"{canonical} (expected column '{csv_name}')")
        else:
            mapping[actual] = canonical
    if missing:
        raise DataError(
            "CSV is missing required columns: "
            + "; ".join(missing)
            + f". Available columns: {available}. Adjust [data.columns] in the config."
        )
    if symbol_column:
        actual = lookup.get(symbol_column.strip().lower())
        if actual is not None:
            mapping[actual] = "symbol"
    return mapping


def load_csv(
    path: str | Path,
    data_cfg: DataConfig,
    report: DataQualityReport | None = None,
) -> tuple[pd.DataFrame, DataQualityReport]:
    """Load an OHLCV CSV file.

    Args:
        path: CSV file path.
        data_cfg: Data section of the configuration.
        report: Optional report to append to.

    Returns:
        ``(df, report)`` where ``df`` has a UTC ``DatetimeIndex`` named ``timestamp``,
        float columns ``open, high, low, close, volume`` and an optional ``symbol``
        column, sorted by time (stable). Rows with unparseable timestamps are removed
        and reported; rows with non-numeric values are kept as NaN so validation
        reports them.

    Raises:
        DataError: If the file does not exist or required columns are missing.
    """
    csv_path = Path(path)
    report = report if report is not None else DataQualityReport()
    report.source = report.source or str(csv_path)
    if not csv_path.is_file():
        raise DataError(f"CSV file not found: {csv_path}")

    raw = pd.read_csv(
        csv_path,
        sep=data_cfg.delimiter,
        comment=data_cfg.comment_char,
        dtype=str,
        skipinitialspace=True,
        keep_default_na=True,
    )
    raw.columns = [str(c).strip() for c in raw.columns]
    report.rows_read = len(raw)
    if raw.empty:
        raise DataError(f"CSV file contains no data rows: {csv_path}")

    mapping = _resolve_columns(list(raw.columns), data_cfg.columns, data_cfg.symbol_column)
    df = raw[list(mapping)].rename(columns=mapping)

    # Numeric columns: coerce and report anything that was not a number.
    for col in NUMERIC_COLUMNS:
        original = df[col].str.strip()
        df[col] = pd.to_numeric(original, errors="coerce").astype("float64")
        bad = original.notna() & (original != "") & df[col].isna()
        if bad.any():
            report.add(
                "error",
                "NON_NUMERIC_VALUE",
                f"Column '{col}' contains non-numeric values (set to NaN, not guessed)",
                count=int(bad.sum()),
                examples=original[bad].head().tolist(),
            )
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()

    # Timestamps -> UTC.
    norm = normalize_to_utc(
        df["timestamp"],
        data_cfg.source_timezone,
        fmt=data_cfg.timestamp_format,
        unit=data_cfg.timestamp_unit,
        ambiguous=data_cfg.ambiguous_time,
        nonexistent=data_cfg.nonexistent_time,
    )
    if norm.source_was_tz_aware:
        report.add("info", "TZ_FROM_SOURCE", "Timestamps carry UTC offsets; data.source_timezone ignored")
    bad_ts = norm.utc.isna()
    if bad_ts.any():
        report.add(
            "error",
            "BAD_TIMESTAMP",
            "Rows with missing/unparseable/ambiguous timestamps were removed",
            count=int(bad_ts.sum()),
            examples=df.loc[bad_ts, "timestamp"].head().tolist(),
        )
        report.rows_dropped += int(bad_ts.sum())

    df = df.assign(timestamp=norm.utc)[~bad_ts.to_numpy()]
    df = df.set_index("timestamp")
    if data_cfg.timestamp_label == "close":
        df.index = df.index - pd.Timedelta(pd.tseries.frequencies.to_offset(data_cfg.base_frequency))
        report.add("info", "CLOSE_STAMPED", f"Timestamps shifted back by {data_cfg.base_frequency} to bar open")
    if not df.index.is_monotonic_increasing:
        report.add("warning", "UNSORTED", "Timestamps were not in ascending order; rows were sorted")
        df = df.sort_index(kind="stable")

    ordered = [c for c in CANONICAL_COLUMNS if c != "timestamp"]
    if "symbol" in df.columns:
        ordered.append("symbol")
    return df[ordered], report
