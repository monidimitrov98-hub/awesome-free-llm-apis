"""Central strategy registry (Sprint 11): stable ids -> how to load parameters, prepare causal features and
build the Strategy object. Every registered strategy runs through the SAME event-driven
:class:`~src.backtest.engine.BacktestEngine` (no parallel backtester) and the same protection layers.

A spec never holds a broker, an order router, an authorization token or a risk object: it only turns market
data and parameters into a :class:`~src.strategy.base.Strategy` that proposes requests.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.config import ConfigError, SessionConfig


@dataclass(frozen=True)
class Prepared:
    signal: pd.DataFrame
    execution: pd.DataFrame
    context: dict[str, pd.DataFrame]
    session: SessionConfig
    regime_enforcement: str | None = None      # effective regime enforcement in this run (None = no regime)


@dataclass(frozen=True)
class StrategySpec:
    id: str
    title: str
    description: str
    implementation: str
    load_params: Callable[..., Any]
    prepare: Callable[..., Prepared]
    build: Callable[[Any, Any, SessionConfig], Any]
    summary: Callable[[Any], dict[str, Any]]
    research_status: str
    derive: Callable[..., Any] | None = None        # (params, overrides, app) -> validated derived params
    to_dict: Callable[[Any], dict[str, Any]] | None = None

    def info(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "description": self.description,
                "implementation": self.implementation, "research_status": self.research_status}


# ---------------------------------------------------------------------------------------------- shared
def _regime_frames(md, app, signal_tf: str, context_tf: str | None, cols: dict[str, str], regime
                   ) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Signal frame and context frames with causal regime features (whole file, before period slicing)."""
    from src.ai.market_regime.features import add_regime_features, add_shock_features

    if regime.signal_timeframe != signal_tf:
        raise ConfigError(f"regime signal_timeframe {regime.signal_timeframe} != strategy {signal_tf}")
    if regime.context_timeframe and regime.context_timeframe != context_tf:
        raise ConfigError(f"regime context_timeframe {regime.context_timeframe} != strategy {context_tf}")
    signal = add_regime_features(md.frames[signal_tf], regime.features, app.session, **cols)
    context = {}
    if context_tf:
        if context_tf not in md.frames:
            raise ConfigError(f"context timeframe {context_tf} is not produced by the pipeline")
        context[context_tf] = add_regime_features(md.frames[context_tf], regime.features, app.session, **cols)
    if regime.fast_timeframe:
        if regime.fast_timeframe not in md.frames:
            raise ConfigError(f"regime fast_timeframe {regime.fast_timeframe} is not produced by the pipeline")
        context[regime.fast_timeframe] = add_shock_features(md.frames[regime.fast_timeframe], regime.features,
                                                            atr_col=cols["atr_col"])
    return signal, context


# ---------------------------------------------------------------------------------------------- VWAP trend pullback
def _tp_load(path: Path, app, expected_version: str | None = None):
    from src.strategy.vwap_pullback_config import load_strategy_config

    return load_strategy_config(path)


def _tp_prepare(md, app, cfg, regime=None) -> Prepared:
    """Exactly the Sprint 2-4 preparation (unchanged behaviour; see the golden regression test)."""
    needed = [cfg.signal_timeframe, cfg.execution_timeframe] + ([cfg.context_timeframe] if cfg.context_timeframe else [])
    missing = [tf for tf in needed if tf not in md.frames]
    if missing:
        raise ConfigError(f"Timeframes {missing} are not produced by the pipeline; add them to "
                          "config/settings.toml [resample] timeframes")
    context = {cfg.context_timeframe: md.frames[cfg.context_timeframe]} if cfg.context_timeframe else {}
    signal, execution = md.frames[cfg.signal_timeframe], md.frames[cfg.execution_timeframe]
    if regime is not None and regime.enabled:
        cols = dict(ema_fast=cfg.ema_fast_col, ema_slow=cfg.ema_slow_col, atr_col=cfg.atr_col, rsi_col=cfg.rsi_col)
        signal, rctx = _regime_frames(md, app, cfg.signal_timeframe, cfg.context_timeframe, cols, regime)
        # strategy context first, then the regime context (same keys -> the regime-featured frame, as before)
        context = {**context, **rctx}
        return Prepared(signal, execution, context, app.session, regime.enforcement)
    return Prepared(signal, execution, context, app.session, None)


def _tp_build(cfg, app, session: SessionConfig):
    from src.strategy.vwap_pullback import VwapTrendPullbackStrategy

    return VwapTrendPullbackStrategy(cfg, app.session.timezone)


def _tp_summary(cfg) -> dict[str, Any]:
    return {"signal_timeframe": cfg.signal_timeframe, "context_timeframe": cfg.context_timeframe,
            "r_multiple": cfg.target.r_multiple, "stop": cfg.stop.method}


# ---------------------------------------------------------------------------------------------- VWAP mean reversion
def _mr_load(path: Path, app, expected_version: str | None = None):
    from src.strategy.vwap_mean_reversion_config import parse_config

    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {Path(path).name}: {exc}") from exc
    cfg = parse_config(raw, sessions=tuple(app.sessions), timeframes=tuple(app.timeframes))
    if expected_version is not None and cfg.version != expected_version:
        raise ConfigError(f"{Path(path).name}: [version] version {cfg.version!r} does not match the file name")
    return cfg


def _mr_prepare(md, app, cfg, regime=None) -> Prepared:
    from src.data.sessions import assign_session_dates
    from src.strategy.vwap_mean_reversion import add_mean_reversion_features

    root = md.contract.root
    if root not in cfg.scope.instruments:
        raise ConfigError(f"{cfg.strategy}@{cfg.version} is scoped to {list(cfg.scope.instruments)}, not {root}")
    session = app.sessions[cfg.scope.session]
    sig_tf, ex_tf = cfg.scope.signal_timeframe, cfg.scope.execution_timeframe
    signal = md.frames[sig_tf]
    context: dict[str, pd.DataFrame] = {}
    enforcement = None
    if regime is not None and regime.enabled and cfg.regime.mode == "observe":
        ind = app.indicators
        cols = dict(ema_fast=f"ema_{ind.ema_fast}", ema_slow=f"ema_{ind.ema_slow}", atr_col=f"atr_{ind.atr_period}",
                    rsi_col=f"rsi_{ind.rsi_period}")
        # the regime engine's own context timeframe (labels only); the strategy itself uses no context frame
        signal, context = _regime_frames(md, app, sig_tf, regime.context_timeframe or None, cols, regime)
        enforcement = "observe"   # labels only: V1 is never gated by the regime engine
    signal = add_mean_reversion_features(signal, session, cfg)
    execution = md.frames[ex_tf].copy()
    execution["session_date"] = assign_session_dates(execution.index, session)
    return Prepared(signal, execution, context, session, enforcement)


def _mr_derive(cfg, overrides: dict[str, Any], app):
    """A derived (UNREGISTERED, exploration-only) configuration: ``overrides`` maps "section.key" to a value.
    The result is re-validated exactly like a version file."""
    from src.strategy.vwap_mean_reversion_config import parse_config

    d = cfg.to_dict()
    raw: dict[str, Any] = {"version": {"strategy": d["strategy"], "version": d["version"], "label": d["label"],
                                       "notes": d["notes"]}}
    for sec in ("scope", "indicators", "confirmation", "stop", "target", "execution", "regime"):
        raw[sec] = dict(d[sec])
    for key, value in overrides.items():
        sec, _, name = key.partition(".")
        if sec not in raw or sec == "version" or not name or name not in raw[sec]:
            raise ConfigError(f"unknown parameter {key!r}")
        raw[sec][name] = value
    return parse_config(raw, sessions=tuple(app.sessions), timeframes=tuple(app.timeframes))


def _mr_build(cfg, app, session: SessionConfig):
    from src.strategy.vwap_mean_reversion import VwapMeanReversionStrategy

    return VwapMeanReversionStrategy(cfg, session)


def _mr_summary(cfg) -> dict[str, Any]:
    return {"session": cfg.scope.session, "signal_timeframe": cfg.scope.signal_timeframe,
            "band_k": cfg.indicators.band_k, "rsi": [cfg.indicators.rsi_period, cfg.indicators.rsi_oversold,
                                                     cfg.indicators.rsi_overbought],
            "confirmation": cfg.confirmation.method, "stop": cfg.stop.method, "target_fraction": cfg.target.fraction,
            "experimental": True}


REGISTRY: dict[str, StrategySpec] = {
    "vwap_trend_pullback": StrategySpec(
        "vwap_trend_pullback", "VWAP Trend Pullback", "Sprint 2-4 rule-based trend pullback (existing strategy).",
        "src.strategy.vwap_pullback.VwapTrendPullbackStrategy", _tp_load, _tp_prepare, _tp_build, _tp_summary,
        "existing strategy; research status per parameter version", to_dict=lambda c: c.to_dict()),
    "vwap_mean_reversion": StrategySpec(
        "vwap_mean_reversion", "VWAP Mean Reversion (V1 research candidate)",
        "VWAP-centred volume-weighted bands + RSI + one experimental price-action confirmation; target = "
        "reversion toward VWAP. The user's original confirmation / stop rules are NOT known: C1-C4 and the stop "
        "methods are experimental variants.",
        "src.strategy.vwap_mean_reversion.VwapMeanReversionStrategy", _mr_load, _mr_prepare, _mr_build, _mr_summary,
        "RESEARCH candidate", derive=_mr_derive, to_dict=lambda c: c.to_dict()),
}


def get_spec(strategy_id: str) -> StrategySpec:
    spec = REGISTRY.get(strategy_id)
    if spec is None:
        raise ConfigError(f"unknown strategy id {strategy_id!r}; registered: {sorted(REGISTRY)}")
    return spec


def with_enforcement(regime, enforcement: str | None):
    """Regime config for a Prepared run (V1 mean reversion only ever labels: enforcement observe)."""
    if regime is None or enforcement is None or regime.enforcement == enforcement:
        return regime
    return replace(regime, enforcement=enforcement)
