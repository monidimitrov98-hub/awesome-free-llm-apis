"""Governance-gated binding of a registry strategy version to the PAPER runtime (Sprint 11).

``run_paper.py --strategy ID@VERSION`` runs a registered, unmodified parameter version in the existing
PaperSession (PaperBroker only: no order can reach a broker) ONLY when a human has promoted exactly that
version: SHADOW_APPROVED (or PAPER_APPROVED) for replay / shadow sessions, PAPER_APPROVED for ``--mode paper``.
Every entry still goes through the full paper authorization chain (mode, system, request, position,
NewsGuard, sizing, session, AccountGuard, prop firm compliance, RiskManager) -- the binding only supplies
the strategy object and its causal signal frames.

The strategy's own trading session is honoured: positions are flattened and entries stop before the
EARLIER of the runtime session's and the strategy session's end (e.g. a RTH-only strategy is flat before
16:00 New York even though the runtime session is Globex).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import pandas as pd

from src.config import ConfigError, SessionConfig


@dataclass(frozen=True)
class PaperBinding:
    strategy_id: str
    version: str
    sha256: str
    signal_timeframe: str
    session: SessionConfig
    frames: Callable[[str, pd.DataFrame], tuple[pd.DataFrame, dict[str, pd.DataFrame]]]
    factory: Callable[[str], Any]
    regime: Any
    params: Any
    approval: str
    recheck: Callable[[], tuple[bool, str]] | None = None   # governance re-check while the session runs

    def identity(self) -> dict[str, Any]:
        return {"id": self.strategy_id, "version": self.version, "sha256": self.sha256,
                "signal_timeframe": self.signal_timeframe, "session": self.session.name, "approval": self.approval}


def _timeframes(params, regime) -> set[str]:
    tfs = set()
    scope = getattr(params, "scope", None)
    if scope is not None:
        tfs |= {scope.signal_timeframe, scope.execution_timeframe}
    else:
        tfs |= {params.signal_timeframe, params.execution_timeframe} | ({params.context_timeframe} - {"", None})
    if regime is not None and regime.enabled:
        tfs |= {regime.signal_timeframe} | ({regime.context_timeframe, regime.fast_timeframe} - {"", None})
    return tfs


def binding_for(strategy_id: str, version: str, app, regime_cfg, *, use: str,
                governance_path=None) -> PaperBinding:
    """Load and gate. Raises ConfigError when the version is unregistered / modified or not approved for ``use``."""
    from src.data.resample import resample_ohlcv
    from src.indicators.compute import add_indicators
    from src.research import governance
    from src.research.params import load_version
    from src.research.registry import get_spec, with_enforcement

    spec = get_spec(strategy_id)
    pv = load_version(strategy_id, version, app)       # registered and unmodified (never unregistered here)
    ok, why = governance.allows(strategy_id, version, pv.sha256, use, governance_path)
    if not ok:
        raise ConfigError(f"refused: {why}")
    params = pv.params
    regime = regime_cfg if regime_cfg is not None and regime_cfg.enabled else None
    if strategy_id == "vwap_mean_reversion":
        if regime is not None and params.regime.mode == "off":
            regime = None
        regime = with_enforcement(regime, "observe") if regime is not None else None
        session = app.sessions[params.scope.session]
        signal_tf = params.scope.signal_timeframe
    else:
        session = app.session
        signal_tf = params.signal_timeframe
    tfs = _timeframes(params, regime)
    freq = app.data.base_frequency
    # the paper runtime executes on its own 1-minute bars: the strategy's execution frame is only a placeholder
    # here, so it is not given indicators (cost); every frame the decision uses is computed exactly as in backtests
    decision_tfs = {signal_tf} | ({getattr(params, "context_timeframe", None)} - {"", None})
    if regime is not None:
        decision_tfs |= {regime.signal_timeframe} | ({regime.context_timeframe, regime.fast_timeframe} - {"", None})

    def frames(root: str, base: pd.DataFrame):
        fr = {tf: add_indicators(resample_ohlcv(base, tf, freq), app.indicators, app.session) for tf in decision_tfs}
        for tf in tfs - set(fr):
            fr[tf] = resample_ohlcv(base, tf, freq)
        prep = spec.prepare(SimpleNamespace(frames=fr, contract=SimpleNamespace(root=root)), app, params, regime)
        return prep.signal, prep.context

    def factory(root: str):
        return spec.build(params, app, session)

    def recheck() -> tuple[bool, str]:
        """A suspension / rejection (or a broken promotion store) blocks NEW entries of a running session."""
        return governance.allows(strategy_id, version, pv.sha256, use, governance_path)

    return PaperBinding(strategy_id, version, pv.sha256, signal_tf, session, frames, factory, regime, params, why,
                        recheck)


__all__ = ["PaperBinding", "binding_for"]
