"""Calibrated confidence.

Raw classifier scores are NOT probabilities. A :class:`BinnedCalibrator` is fitted on
HISTORICAL (training) data only: for each predicted class and score bin it stores the
empirical frequency with which the forward label agreed with the prediction. At decision
time the bin's frequency is the calibrated probability. Bins with fewer than
``min_bin_samples`` observations -- and an uncalibrated engine -- give LOW confidence
(conservative), and invalid inputs give INSUFFICIENT_DATA.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from src.ai.market_regime.config import ConfidenceConfig
from src.ai.market_regime.models import ConfidenceLevel
from src.config import ConfigError

CLASSES = ("1", "-1", "0")


@dataclass
class BinnedCalibrator:
    n_bins: int = 10
    table: dict[str, list[list[float]]] = field(default_factory=dict)  # class -> [[hits, total] per bin]
    meta: dict[str, Any] = field(default_factory=dict)

    def _bin(self, score: float) -> int:
        return int(min(max(score, 0.0), 1.0 - 1e-12) * self.n_bins)

    def fit(self, predicted: np.ndarray, scores: np.ndarray, labels: np.ndarray) -> "BinnedCalibrator":
        self.table = {c: [[0.0, 0.0] for _ in range(self.n_bins)] for c in CLASSES}
        for p, s, y in zip(predicted, scores, labels):
            if not (math.isfinite(p) and math.isfinite(s) and math.isfinite(y)):
                continue
            cell = self.table[str(int(p))][self._bin(float(s))]
            cell[1] += 1
            cell[0] += float(p == y)
        return self

    def predict(self, predicted: float, score: float) -> tuple[float | None, int]:
        if not self.table or not (math.isfinite(predicted) and math.isfinite(score)):
            return None, 0
        hits, total = self.table[str(int(predicted))][self._bin(score)]
        return (hits / total if total else None), int(total)

    # --- persistence with integrity
    def to_json(self) -> str:
        body = {"n_bins": self.n_bins, "table": self.table, "meta": self.meta}
        payload = json.dumps(body, sort_keys=True)
        return json.dumps({"calibrator": body, "sha256": hashlib.sha256(payload.encode()).hexdigest()}, indent=2)

    def save(self, path: str | Path) -> str:
        Path(path).write_text(self.to_json(), encoding="utf-8")
        return file_sha(path)

    @classmethod
    def load(cls, path: str | Path) -> "BinnedCalibrator":
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            body = doc["calibrator"]
        except (OSError, ValueError, KeyError) as exc:
            raise ConfigError(f"Cannot read calibration {path}: {exc}") from exc
        if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != doc.get("sha256"):
            raise ConfigError(f"Calibration file {path} failed its integrity check")
        return cls(int(body["n_bins"]), body["table"], body.get("meta", {}))


def file_sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def confidence_level(prob: float | None, n: int, cfg: ConfidenceConfig, valid: bool = True) -> ConfidenceLevel:
    if not valid:
        return ConfidenceLevel.INSUFFICIENT
    if prob is None or n < cfg.min_bin_samples:
        return ConfidenceLevel.LOW
    if prob >= cfg.high:
        return ConfidenceLevel.HIGH
    if prob >= cfg.medium:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def calibration_report(probs: np.ndarray, correct: np.ndarray, n_bins: int = 10) -> dict[str, Any]:
    """Reliability table, Brier score and expected calibration error (ECE)."""
    mask = np.isfinite(probs) & np.isfinite(correct)
    p, y = probs[mask], correct[mask].astype(float)
    if p.size == 0:
        return {"n": 0, "brier": None, "ece": None, "bins": []}
    bins = []
    ece = 0.0
    edges = np.linspace(0, 1, n_bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        if m.any():
            bins.append({"lo": float(lo), "hi": float(hi), "n": int(m.sum()), "mean_prob": float(p[m].mean()),
                         "observed": float(y[m].mean())})
            ece += m.sum() / p.size * abs(p[m].mean() - y[m].mean())
    return {"n": int(p.size), "brier": float(np.mean((p - y) ** 2)), "ece": float(ece), "bins": bins}
