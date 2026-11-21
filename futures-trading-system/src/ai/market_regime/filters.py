"""RegimeGatedStrategy: the ONLY connection between the regime engine and orders.

It wraps the deterministic strategy and, per enforcement mode, can only REMOVE entry
requests the strategy produced. It never creates, enlarges, re-prices or re-sizes a
request (the returned requests are the strategy's own objects), never touches exits, and
cannot reach the risk engine or AccountGuard -- the backtest engine still applies every
risk check to whatever passes. If the regime engine fails or returns invalid output,
the snapshot is NO_TRADE and entries are blocked (fail closed), unless enforcement is
"observe" (which never blocks and is explicitly chosen).

A blocked entry is reported to the wrapped strategy as a ``rejected`` order event (stage
``regime_gate``) so its lifecycle and pullback reservation stay consistent.
"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd

from src.ai.market_regime.config import RegimeConfig
from src.ai.market_regime.diagnostics import DashboardState
from src.ai.market_regime.explain import Explainer, TemplateExplainer
from src.ai.market_regime.models import RegimeSnapshot, Suitability, SuitabilityDecision
from src.ai.market_regime.regime import RegimeEngine, error_snapshot
from src.ai.market_regime.suitability import evaluate_suitability
from src.backtest.orders import Fill, OrderEvent, OrderRequest, OrderRole
from src.config import ContractSpec
from src.strategy.base import Strategy, StrategyContext

BLOCKED = {"observe": set(), "block_no_trade": {Suitability.NO_TRADE},
           "block_unfavorable": {Suitability.NO_TRADE, Suitability.UNFAVORABLE},
           "require_favorable": {Suitability.NO_TRADE, Suitability.UNFAVORABLE, Suitability.NEUTRAL}}


class RegimeGatedStrategy(Strategy):
    def __init__(self, inner: Strategy, engine: RegimeEngine, cfg: RegimeConfig,
                 explainer: Explainer | None = None) -> None:
        self.inner = inner
        self.engine = engine
        self.cfg = cfg
        self.explainer = explainer or TemplateExplainer()
        self.name = f"{inner.name}+regime"
        self._reset()

    def _reset(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.dashboard = DashboardState(self.cfg.diagnostics.max_transitions)
        self._by_signal: dict[str, dict[str, Any]] = {}
        self._stopouts: list[pd.Timestamp] = []
        self._last_snapshot: RegimeSnapshot | None = None
        self.errors = 0

    # passthrough of the wrapped strategy's diagnostics
    def __getattr__(self, item: str) -> Any:
        if item in ("diagnostics_frame", "signals_frame", "diagnostics", "lifecycle_violations"):
            return getattr(self.inner, item)
        raise AttributeError(item)

    def on_start(self, contract: ContractSpec) -> None:
        self._reset()
        self.inner.on_start(contract)

    def _recent_stopouts(self, now: pd.Timestamp) -> int:
        window = pd.Timedelta(minutes=self.cfg.suitability.stopout_window_minutes)
        return sum(1 for t in self._stopouts if now - window <= t <= now)

    def on_bar(self, ctx: StrategyContext) -> Sequence[OrderRequest] | None:
        requests = list(self.inner.on_bar(ctx) or [])
        try:
            snap = self.engine.snapshot(ctx)
        except Exception as exc:  # noqa: BLE001 - any engine failure fails closed
            self.errors += 1
            snap = error_snapshot(ctx, self.engine.instrument, self.cfg.model_version, f"{type(exc).__name__}: {exc}")
        self._last_snapshot = snap
        stopouts = self._recent_stopouts(ctx.now)
        try:
            decisions = {d: evaluate_suitability(snap, d, self.cfg.suitability, stopouts) for d in ("long", "short")}
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            decisions = {d: SuitabilityDecision(Suitability.NO_TRADE, _env_no_trade(), d,
                                                (f"suitability_error:{type(exc).__name__}",)) for d in ("long", "short")}
        if not isinstance(decisions.get("long"), SuitabilityDecision):  # invalid output -> fail closed
            decisions = {d: SuitabilityDecision(Suitability.NO_TRADE, _env_no_trade(), d, ("invalid_suitability",))
                         for d in ("long", "short")}
        explanation = None
        entries = [r for r in requests if r.action == "entry"]
        if entries:
            try:
                explanation = self.explainer.explain(snap, decisions[entries[0].side.value])
            except Exception:  # noqa: BLE001 - explanations are optional
                explanation = TemplateExplainer().explain(snap, decisions[entries[0].side.value])
        self.dashboard.update(snap, decisions, explanation)
        self.snapshots.append({"time": ctx.now, "bar_time": ctx.bar.name, **_flat(snap),
                               "long": decisions["long"].suitability.value,
                               "short": decisions["short"].suitability.value})
        passed: list[OrderRequest] = []
        for req in requests:
            if req.action != "entry":
                passed.append(req)  # exits / cancels always pass (they only reduce risk)
                continue
            dec = decisions[req.side.value]
            blocked = dec.suitability in BLOCKED[self.cfg.enforcement]
            record = {"signal_id": req.client_id, "time": ctx.now, "bar_time": ctx.bar.name, "side": req.side.value,
                      **_flat(snap), "suitability": dec.suitability.value, "environment": dec.environment.value,
                      "suitability_reasons": ";".join(dec.reasons), "enforcement": self.cfg.enforcement,
                      "allowed": not blocked, "block_reason": ";".join(dec.reasons) if blocked else "",
                      "explanation": explanation, "order_status": "blocked" if blocked else "submitted",
                      "regime_at_entry": None, "trend_at_entry": None}
            self.decisions.append(record)
            if req.client_id:
                self._by_signal[req.client_id] = record
            if blocked:
                if req.client_id:  # keep the strategy's lifecycle consistent (releases its pullback)
                    self.inner.on_order_event(OrderEvent(req.client_id, None, "rejected", ctx.now, "regime_gate",
                                                         f"regime_{dec.suitability.value}", record["block_reason"]))
                continue
            passed.append(req)  # the strategy's own object, unmodified
        return passed

    def on_fill(self, fill: Fill) -> None:
        if fill.role is OrderRole.STOP_LOSS:
            self._stopouts.append(fill.bar_time)
        self.inner.on_fill(fill)

    def on_order_event(self, event: OrderEvent) -> None:
        rec = self._by_signal.get(event.client_id)
        if rec is not None:
            rec["order_status"] = event.status
            if event.status == "filled" and self._last_snapshot is not None:
                snap = self._last_snapshot  # latest completed-bar regime known at the fill
                rec["regime_at_entry"] = str(snap.bar_time)
                rec["trend_at_entry"] = snap.classification.trend.value if snap.classification else "unavailable"
        self.inner.on_order_event(event)

    def decisions_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.decisions)

    def snapshots_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.snapshots)


def _env_no_trade():
    from src.ai.market_regime.models import Environment

    return Environment.NO_TRADE


def _flat(snap: RegimeSnapshot) -> dict[str, Any]:
    c = snap.classification
    return {"regime_valid": snap.valid, "trend": c.trend.value if c else "unavailable",
            "range": c.range.value if c else "unavailable", "volatility": c.volatility.value if c else "unavailable",
            "volatility_change": c.volatility_change.value if c else "unavailable",
            "structure": c.structure.value if c else "unavailable", "direction": snap.direction_label,
            "trend_strength_atr": snap.features.get("rf_spread_atr"), "trend_score": c.trend_score if c else None,
            "confidence": snap.confidence, "confidence_level": snap.confidence_level.value,
            "context_trend": snap.context.trend.value if snap.context else "unavailable",
            "regime_reasons": ";".join(snap.reasons), "regime_error": snap.error,
            "model_version": snap.model_version}
