"""Historical-validation controls: date ranges, in-sample / out-of-sample periods,
walk-forward folds and an out-of-sample ledger.

Rules enforced here (the CLI builds on them):

* Periods are selected by **session (trade) date**, inclusive. Indicators are still
  computed on the whole file first; they are causal (Sprint 1 tests), so earlier data
  only serves as warm-up and never leaks future information.
* An out-of-sample period must start after its in-sample period ends (no overlap).
* Out-of-sample results are only produced when explicitly revealed, for exactly one
  pre-registered configuration. Every reveal is written to an OOS ledger; evaluating the
  same data and OOS period again with a DIFFERENT configuration is refused unless
  explicitly acknowledged (the manifest then records the reuse). This makes it hard to
  tune parameters against the out-of-sample period by accident.
* Walk-forward folds evaluate one fixed configuration on rolling windows; there is no
  optimizer, so no parameter is fitted per fold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import ConfigError


class OosReuseError(ConfigError):
    """The out-of-sample period was already evaluated with another configuration."""


@dataclass(frozen=True)
class Period:
    """A named evaluation period of session dates (inclusive). None = open-ended."""

    label: str
    kind: str  # "full" | "range" | "in_sample" | "out_of_sample" | "wf_in_sample" | "wf_out_of_sample"
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    fold: int | None = None

    @property
    def is_out_of_sample(self) -> bool:
        return self.kind in ("out_of_sample", "wf_out_of_sample")

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "kind": self.kind, "fold": self.fold,
                "start": None if self.start is None else str(self.start.date()),
                "end": None if self.end is None else str(self.end.date())}


def parse_date(value: str | None) -> pd.Timestamp | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return pd.Timestamp(str(value)).normalize()
    except ValueError as exc:
        raise ConfigError(f"Invalid date {value!r}; use YYYY-MM-DD") from exc


def make_period(label: str, kind: str, start: str | None, end: str | None, fold: int | None = None) -> Period:
    s, e = parse_date(start), parse_date(end)
    if s is not None and e is not None and s > e:
        raise ConfigError(f"{label}: start {s.date()} is after end {e.date()}")
    return Period(label, kind, s, e, fold)


def check_no_overlap(in_sample: Period, out_of_sample: Period) -> None:
    """The out-of-sample period must begin strictly after the in-sample period ends."""
    if in_sample.end is None or out_of_sample.start is None:
        raise ConfigError("In-sample needs an end date and out-of-sample a start date")
    if out_of_sample.start <= in_sample.end:
        raise ConfigError(f"Out-of-sample ({out_of_sample.start.date()}) must start after the in-sample end "
                          f"({in_sample.end.date()})")


def slice_frame(frame: pd.DataFrame, period: Period) -> pd.DataFrame:
    """Rows whose session date lies inside the period (rows outside any session are dropped)."""
    sd = pd.to_datetime(frame["session_date"])
    mask = sd.notna()
    if period.start is not None:
        mask &= sd >= period.start
    if period.end is not None:
        mask &= sd <= period.end
    return frame[mask.to_numpy()]


def session_dates(frame: pd.DataFrame) -> list[pd.Timestamp]:
    return sorted(pd.to_datetime(frame["session_date"]).dropna().unique())


def walk_forward_periods(dates: list[pd.Timestamp], train: int, test: int, step: int | None = None
                         ) -> list[tuple[Period, Period]]:
    """Rolling (in-sample, out-of-sample) folds over session dates: ``train`` sessions
    followed by the next ``test`` sessions, advancing by ``step`` (default ``test``)."""
    if train < 1 or test < 1:
        raise ConfigError("walk-forward train and test must be >= 1 session")
    step = step or test
    folds = []
    i, k = 0, 1
    while i + train + test <= len(dates):
        tr, te = dates[i:i + train], dates[i + train:i + train + test]
        folds.append((Period(f"wf{k:02d}_in_sample", "wf_in_sample", tr[0], tr[-1], k),
                      Period(f"wf{k:02d}_out_of_sample", "wf_out_of_sample", te[0], te[-1], k)))
        i += step
        k += 1
    if not folds:
        raise ConfigError(f"Not enough sessions ({len(dates)}) for walk-forward {train}+{test}")
    return folds


class OosLedger:
    """Append-only record of out-of-sample evaluations (JSON file next to the outputs)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def prior(self, data_sha256: str, period: Period) -> list[dict[str, Any]]:
        p = period.to_dict()
        return [e for e in self._load()
                if e["data_sha256"] == data_sha256 and e["start"] == p["start"] and e["end"] == p["end"]]

    def check_and_record(self, data_sha256: str, period: Period, config_hash: str, allow_reuse: bool
                         ) -> dict[str, Any]:
        """Refuse a second OOS evaluation with a different configuration unless allowed."""
        prior = self.prior(data_sha256, period)
        others = sorted({e["config_hash"] for e in prior if e["config_hash"] != config_hash})
        if others and not allow_reuse:
            raise OosReuseError(
                f"Out-of-sample period {period.to_dict()['start']}..{period.to_dict()['end']} of this data was "
                f"already evaluated with {len(others)} other configuration(s). Re-testing changed settings on the "
                "same OOS data turns it into in-sample data. Choose a new, untouched OOS period, or pass "
                "--allow-oos-reuse to proceed (recorded in the manifest)."
            )
        entries = self._load()
        entries.append({**period.to_dict(), "data_sha256": data_sha256, "config_hash": config_hash,
                        "time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return {"prior_evaluations": len(prior), "other_configurations": len(others), "reuse_acknowledged":
                bool(others)}
