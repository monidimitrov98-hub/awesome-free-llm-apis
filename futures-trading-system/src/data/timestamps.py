"""Timestamp normalization to UTC."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.data.errors import TimestampError

# Trailing 'Z' or '+HH:MM' / '-HHMM' after a time component.
_OFFSET_PATTERN = r"\d:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:Z|[+-]\d{2}:?\d{2})$"


@dataclass(frozen=True)
class NormalizedTimestamps:
    """Result of :func:`normalize_to_utc`.

    Attributes:
        utc: tz-aware (UTC) timestamps aligned with the input; NaT where parsing failed.
        n_unparseable: Number of non-empty inputs that could not be parsed.
        n_missing: Number of empty inputs.
        source_was_tz_aware: True when the input carried its own UTC offsets.
    """

    utc: pd.Series
    n_unparseable: int
    n_missing: int
    source_was_tz_aware: bool


def normalize_to_utc(
    values: pd.Series,
    source_timezone: str,
    *,
    fmt: str | None = None,
    unit: str | None = None,
    ambiguous: str = "raise",
    nonexistent: str = "raise",
) -> NormalizedTimestamps:
    """Convert raw timestamp values to tz-aware UTC.

    * Text with an explicit offset (``+00:00``, ``-05:00``, ``Z``) is converted directly.
    * Naive text is interpreted in ``source_timezone`` (DST-aware) then converted.
    * With ``unit`` set, values are integer epochs in UTC.

    Unparseable values become NaT and are counted; they are never guessed.

    Args:
        values: Raw timestamp column.
        source_timezone: IANA timezone of naive timestamps.
        fmt: Optional strptime format.
        unit: Epoch unit ("s", "ms", "us", "ns") or None for text.
        ambiguous: "raise", "infer" or "NaT" for repeated DST fall-back times.
        nonexistent: "raise" or "NaT" for skipped DST spring-forward times.

    Raises:
        TimestampError: On ambiguous/nonexistent local times when set to "raise".
    """
    raw = pd.Series(values, copy=False)
    is_missing = raw.isna() | (raw.astype("string").str.strip() == "")
    n_missing = int(is_missing.sum())

    if unit is not None:
        numeric = pd.to_numeric(raw, errors="coerce")
        utc = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
        aware = True
    else:
        text = raw.astype("string").str.strip()
        has_offset = text.str.contains(_OFFSET_PATTERN, regex=True, na=False)
        if has_offset.any():
            if not has_offset[~is_missing.to_numpy()].all():
                raise TimestampError(
                    "File mixes timestamps with and without UTC offsets; cannot interpret them consistently"
                )
            # Explicit offsets (possibly mixed across DST): parse straight to UTC.
            parsed = pd.to_datetime(text, format=fmt, errors="coerce", utc=True)
        else:
            parsed = pd.to_datetime(text, format=fmt, errors="coerce")
        if isinstance(parsed.dtype, pd.DatetimeTZDtype):
            utc = parsed.dt.tz_convert("UTC")
            aware = True
        else:
            aware = False
            try:
                localized = parsed.dt.tz_localize(
                    source_timezone, ambiguous=ambiguous, nonexistent=nonexistent
                )
            except Exception as exc:  # pandas raises different classes across versions
                raise TimestampError(
                    f"Cannot localize timestamps to {source_timezone}: {exc}. "
                    "This usually means a DST transition (ambiguous or nonexistent local time). "
                    "Set data.ambiguous_time / data.nonexistent_time, or export UTC timestamps."
                ) from exc
            utc = localized.dt.tz_convert("UTC")

    n_bad_total = int(utc.isna().sum())
    return NormalizedTimestamps(
        utc=utc.rename("timestamp"),
        n_unparseable=n_bad_total - n_missing,
        n_missing=n_missing,
        source_was_tz_aware=aware,
    )
