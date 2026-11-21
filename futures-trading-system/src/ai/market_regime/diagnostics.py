"""Dashboard-ready state: current regime, suitability, reasons and recent transitions."""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from src.ai.market_regime.models import RegimeSnapshot, SuitabilityDecision


class DashboardState:
    def __init__(self, max_transitions: int = 20) -> None:
        self.current: dict[str, Any] | None = None
        self.transitions: deque[dict[str, Any]] = deque(maxlen=max_transitions)
        self._last_label: tuple[str, str] | None = None

    def update(self, snapshot: RegimeSnapshot, decisions: dict[str, SuitabilityDecision],
               explanation: str | None = None) -> dict[str, Any]:
        c = snapshot.classification
        label = (c.trend.value, c.range.value) if c else ("unavailable", "unavailable")
        if self._last_label is not None and label != self._last_label:
            self.transitions.append({"time": str(snapshot.time), "from": list(self._last_label), "to": list(label)})
        self._last_label = label
        self.current = {
            "time": str(snapshot.time), "bar_time": str(snapshot.bar_time), "instrument": snapshot.instrument,
            "model_version": snapshot.model_version, "regime": snapshot.to_dict(),
            "bias": snapshot.direction_label,
            "trend_strength_atr": snapshot.features.get("rf_spread_atr"),
            "volatility": c.volatility.value if c else "unknown",
            "confidence": snapshot.confidence, "confidence_level": snapshot.confidence_level.value,
            "suitability": {k: v.to_dict() for k, v in decisions.items()},
            "explanation": explanation, "recent_transitions": list(self.transitions),
        }
        return self.current

    def to_json(self) -> str:
        return json.dumps(self.current, indent=2, default=str)
