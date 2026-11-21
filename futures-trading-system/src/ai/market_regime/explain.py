"""Layer 3: human-readable explanations (optional LLM; deterministic fallback).

The explainer only produces TEXT for diagnostics. Its output is never parsed into
actions, cannot reach orders, sizing, risk checks, AccountGuard or strategy parameters,
and it only ever sees the snapshot (it cannot invent data into the engine). If the LLM
is unavailable, slow, raises or returns invalid output, the deterministic template
explanation is used.
"""

from __future__ import annotations

from typing import Callable, Protocol

from src.ai.market_regime.models import RegimeSnapshot, SuitabilityDecision

MAX_CHARS = 2000


class Explainer(Protocol):
    def explain(self, snapshot: RegimeSnapshot, decision: SuitabilityDecision | None) -> str: ...


class TemplateExplainer:
    """Deterministic explanation built only from the snapshot's own fields."""

    def explain(self, snapshot: RegimeSnapshot, decision: SuitabilityDecision | None) -> str:
        if not snapshot.valid or snapshot.classification is None:
            return f"No regime classification: {', '.join(snapshot.reasons) or 'insufficient data'}."
        c = snapshot.classification
        text = (f"{snapshot.instrument} at {snapshot.bar_time}: {c.trend.value.replace('_', ' ')}, "
                f"{c.range.value.replace('_', ' ')}, volatility {c.volatility.value} ({c.volatility_change.value}), "
                f"structure {c.structure.value.replace('_', ' ')}; confidence {snapshot.confidence_level.value}.")
        if decision is not None:
            text += f" VWAP Trend Pullback ({decision.direction}): {decision.suitability.value}"
            text += f" because {', '.join(decision.reasons)}." if decision.reasons else "."
        return text


class LLMExplainer:
    """Wraps any text-generation callable ``prompt -> str`` with strict fallbacks."""

    def __init__(self, generate: Callable[[str], str] | None, fallback: Explainer | None = None) -> None:
        self.generate = generate
        self.fallback = fallback or TemplateExplainer()
        self.failures = 0

    def explain(self, snapshot: RegimeSnapshot, decision: SuitabilityDecision | None) -> str:
        base = self.fallback.explain(snapshot, decision)
        if self.generate is None:
            return base
        prompt = ("Explain this futures market regime classification to a trader in plain language. "
                  "Use ONLY these facts; do not add prices or data that are not listed.\n" + base)
        try:
            text = self.generate(prompt)
        except Exception:  # noqa: BLE001 - any LLM failure falls back
            self.failures += 1
            return base
        if not isinstance(text, str) or not text.strip():
            self.failures += 1
            return base
        return text.strip()[:MAX_CHARS]
