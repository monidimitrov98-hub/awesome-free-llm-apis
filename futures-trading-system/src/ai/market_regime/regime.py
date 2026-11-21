"""RegimeEngine: builds a RegimeSnapshot for one decision from the StrategyContext ONLY.

Inputs are the current signal bar's precomputed causal ``rf_*`` features and the last
COMPLETED context / 1-minute rows the engine exposes in ``ctx.context`` (already cut to
``available_at <= now``). Stale or missing inputs are never treated as current: they
make the snapshot invalid (-> NO_TRADE downstream) or drop the optional context.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.ai.market_regime import ml
from src.ai.market_regime.classifier import classify_row, predicted_class
from src.ai.market_regime.confidence import BinnedCalibrator, confidence_level, file_sha
from src.ai.market_regime.config import RegimeConfig
from src.ai.market_regime.features import ML_FEATURES
from src.ai.market_regime.models import Classification, ConfidenceLevel, RegimeSnapshot, TrendRegime
from src.config import PROJECT_ROOT
from src.strategy.base import StrategyContext

SNAPSHOT_FEATURES = ("rf_spread_atr", "rf_efficiency", "rf_vwap_dist_atr", "rf_atr_pct", "rf_atr_ratio",
                     "rf_range_atr", "rf_rvol", "rf_structure")


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


class RegimeEngine:
    def __init__(self, cfg: RegimeConfig, instrument: str) -> None:
        self.cfg = cfg
        self.instrument = instrument
        self.calibrator: BinnedCalibrator | None = None
        self.model = None
        self.model_identity: dict[str, Any] | None = None
        self.artifacts: dict[str, str] = {}
        if cfg.calibration_path:
            p = _resolve(cfg.calibration_path)
            self.calibrator = BinnedCalibrator.load(p)
            self.artifacts["calibration_sha256"] = file_sha(p)
        if cfg.classifier == "ml":
            self.model, self.model_identity = ml.load_model(_resolve(cfg.ml_model_path or ""))
            self.artifacts["ml_model_sha256"] = self.model_identity["artifact_sha256"]

    # ------------------------------------------------------------------ helpers
    def _context_row(self, ctx: StrategyContext, tf: str | None, max_minutes: float) -> tuple[pd.Series | None, str]:
        if not tf:
            return None, "disabled"
        frame = ctx.context.get(tf)
        if frame is None or frame.empty:
            return None, f"{tf}_unavailable"
        row = frame.iloc[-1]
        if row["available_at"] > ctx.now:  # defence in depth: never use an unfinished bar
            return None, f"{tf}_unfinished"
        if row["available_at"] < ctx.now - pd.Timedelta(minutes=max_minutes):
            return None, f"{tf}_stale"
        if pd.isna(row.get("session_date")) or row["session_date"] != ctx.session_date:
            return None, f"{tf}_other_session"
        if not bool(row.get("is_complete", False)):
            return None, f"{tf}_incomplete"
        return row, "ok"

    def _classify(self, row: pd.Series) -> Classification | None:
        rule = classify_row(row, self.cfg.classification)
        if rule is None or self.model is None:
            return rule
        x = ml.feature_matrix(row.to_frame().T)
        cls, prob = ml.predict(self.model, self.cfg.ml, x)
        direction = int(cls[0])
        trend = {1: TrendRegime.MODERATE_BULLISH, -1: TrendRegime.MODERATE_BEARISH, 0: TrendRegime.NONE}[direction]
        return Classification(trend, rule.range, rule.volatility, rule.volatility_change, rule.structure, direction,
                              float(prob[0]), rule.evidence + (f"ml class {direction:+d} p={prob[0]:.2f}",))

    # ------------------------------------------------------------------ snapshot
    def snapshot(self, ctx: StrategyContext) -> RegimeSnapshot:
        bar = ctx.bar
        base = dict(time=ctx.now, bar_time=bar.name, instrument=self.instrument, model_version=self.cfg.model_version)
        missing = [c for c in ("rf_valid", *SNAPSHOT_FEATURES) if c not in bar.index]
        if missing:
            raise ValueError(f"regime features missing on the signal bar: {missing}")
        reasons: list[str] = []
        if bar["available_at"] != ctx.now:
            reasons.append("signal_bar_stale")
        if not ctx.bar_is_complete:
            reasons.append("signal_bar_incomplete")
        if not bool(bar["rf_valid"]):
            reasons.append("insufficient_or_invalid_features")
        feats = {k: (float(bar[k]) if pd.notna(bar[k]) else math.nan) for k in SNAPSHOT_FEATURES}
        fast, fstatus = self._context_row(ctx, self.cfg.fast_timeframe, self.cfg.staleness.max_fast_staleness_minutes)
        if fast is not None and "rf_shock_ratio" in fast.index and pd.notna(fast["rf_shock_ratio"]):
            feats["rf_shock_ratio"] = float(fast["rf_shock_ratio"])
        elif self.cfg.fast_timeframe:
            reasons.append(f"fast_{fstatus}")  # informative; shock check simply unavailable
        if any(r for r in reasons if r.startswith("signal") or r.startswith("insufficient")):
            return RegimeSnapshot(**base, valid=False, classification=None, context=None, confidence=None,
                                  confidence_level=ConfidenceLevel.INSUFFICIENT, features=feats, reasons=tuple(reasons))
        cls = self._classify(bar)
        if cls is None:
            return RegimeSnapshot(**base, valid=False, classification=None, context=None, confidence=None,
                                  confidence_level=ConfidenceLevel.INSUFFICIENT, features=feats,
                                  reasons=tuple(reasons + ["classification_unavailable"]))
        crow, cstatus = self._context_row(ctx, self.cfg.context_timeframe,
                                          self.cfg.staleness.max_context_staleness_minutes)
        context = classify_row(crow, self.cfg.classification) if crow is not None else None
        if crow is not None and context is None:
            cstatus = "context_features_invalid"
        if context is None and self.cfg.context_timeframe:
            reasons.append(f"context_{cstatus}")
        prob, n = (None, 0)
        if self.calibrator is not None:
            prob, n = self.calibrator.predict(predicted_class(cls.direction, cls.trend.value), cls.trend_score)
        level = confidence_level(prob, n, self.cfg.confidence)
        if prob is None:
            reasons.append("uncalibrated_confidence" if self.calibrator is None else "calibration_bin_too_small")
        return RegimeSnapshot(**base, valid=True, classification=cls, context=context, confidence=prob,
                              confidence_level=level, features=feats, reasons=tuple(reasons))


def error_snapshot(ctx: StrategyContext, instrument: str, model_version: str, error: str) -> RegimeSnapshot:
    return RegimeSnapshot(time=ctx.now, bar_time=ctx.bar.name, instrument=instrument, model_version=model_version,
                          valid=False, classification=None, context=None, confidence=None,
                          confidence_level=ConfidenceLevel.INSUFFICIENT, reasons=("regime_engine_error",),
                          error=error[:300])


__all__ = ["RegimeEngine", "error_snapshot", "ML_FEATURES", "np"]
