"""Reproducible strategy backtest: CSV -> Sprint 1 pipeline -> Sprint 2 engine -> files.

Historical simulation only. The same inputs and configuration files always give the
same outputs (the engine and the strategy are deterministic).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from src.backtest.config import BacktestConfig
from src.backtest.engine import BacktestEngine, BacktestResult
from src.backtest.manifest import build_manifest, snapshot, write_manifest
from src.backtest.validation import Period, session_dates, slice_frame
from src.config import AppConfig, ConfigError
from src.pipeline import MarketData, build_market_data
from src.strategy.vwap_pullback import VwapTrendPullbackStrategy
from src.strategy.vwap_pullback_config import VwapPullbackConfig


@dataclass
class StrategyRun:
    """One backtest of the strategy with one configuration."""

    label: str
    strategy_config: VwapPullbackConfig
    result: BacktestResult
    diagnostics: pd.DataFrame
    signals: pd.DataFrame | None = None
    period: Period | None = None
    regime: dict[str, Any] | None = None  # decisions, snapshots, trades, performance, dashboard, artifacts


def _frames(md: MarketData, cfg: VwapPullbackConfig) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    needed = [cfg.signal_timeframe, cfg.execution_timeframe] + ([cfg.context_timeframe] if cfg.context_timeframe else [])
    missing = [tf for tf in needed if tf not in md.frames]
    if missing:
        raise ConfigError(f"Timeframes {missing} are not produced by the pipeline; add them to "
                          "config/settings.toml [resample] timeframes")
    context = {cfg.context_timeframe: md.frames[cfg.context_timeframe]} if cfg.context_timeframe else {}
    return md.frames[cfg.signal_timeframe], md.frames[cfg.execution_timeframe], context


def _regime_frames(md: MarketData, app: AppConfig, cfg: VwapPullbackConfig, regime) -> tuple[pd.DataFrame, dict]:
    """Signal frame and context frames with causal regime features (computed on the whole
    file before any period slicing; features are causal, earlier data is warm-up only)."""
    from src.ai.market_regime.features import add_regime_features, add_shock_features

    if regime.signal_timeframe != cfg.signal_timeframe:
        raise ConfigError(f"regime signal_timeframe {regime.signal_timeframe} != strategy {cfg.signal_timeframe}")
    if regime.context_timeframe and regime.context_timeframe != cfg.context_timeframe:
        raise ConfigError(f"regime context_timeframe {regime.context_timeframe} != strategy {cfg.context_timeframe}")
    kw = dict(ema_fast=cfg.ema_fast_col, ema_slow=cfg.ema_slow_col, atr_col=cfg.atr_col, rsi_col=cfg.rsi_col)
    signal = add_regime_features(md.frames[cfg.signal_timeframe], regime.features, app.session, **kw)
    context = {}
    if cfg.context_timeframe:
        context[cfg.context_timeframe] = add_regime_features(md.frames[cfg.context_timeframe], regime.features,
                                                             app.session, **kw)
    if regime.fast_timeframe:
        if regime.fast_timeframe not in md.frames:
            raise ConfigError(f"regime fast_timeframe {regime.fast_timeframe} is not produced by the pipeline")
        context[regime.fast_timeframe] = add_shock_features(md.frames[regime.fast_timeframe], regime.features,
                                                            atr_col=cfg.atr_col)
    return signal, context


def run_strategy(md: MarketData, app: AppConfig, bt: BacktestConfig, cfg: VwapPullbackConfig,
                 label: str = "", period: Period | None = None, regime=None, explainer=None) -> StrategyRun:
    """Backtest the VWAP Trend Pullback strategy on already-built market data.

    With ``period`` only sessions inside it are traded (indicators were computed on the
    whole file and are causal, so earlier data is warm-up only). Each period starts from
    the configured initial capital with a fresh risk manager and account guard.
    """
    if md.contract.spec is None:
        raise ConfigError(f"No contract specification for {md.contract.root}")
    signal, execution, context = _frames(md, cfg)
    use_regime = regime is not None and regime.enabled
    if use_regime:
        signal, regime_context = _regime_frames(md, app, cfg, regime)
        context = {**context, **regime_context}
    if period is not None:
        signal, execution = slice_frame(signal, period), slice_frame(execution, period)
        context = {k: slice_frame(v, period) for k, v in context.items()}
        if signal.empty or execution.empty:
            raise ConfigError(f"No data inside period {period.to_dict()}")
    strategy = VwapTrendPullbackStrategy(cfg, app.session.timezone)
    gate = None
    if use_regime:
        from src.ai.market_regime.filters import RegimeGatedStrategy
        from src.ai.market_regime.regime import RegimeEngine

        gate = RegimeGatedStrategy(strategy, RegimeEngine(regime, md.contract.root), regime, explainer)
    engine = BacktestEngine(bt, md.contract.spec, app.session)
    result = engine.run(gate or strategy, signal, execution, context_bars=context)
    regime_out = None
    if gate is not None:
        from src.ai.market_regime.performance import add_excursions, enrich_trades, regime_performance

        decisions = gate.decisions_frame()
        trades = add_excursions(enrich_trades(result.trades, decisions), execution, md.contract.spec.point_value)
        regime_out = {"decisions": decisions, "snapshots": gate.snapshots_frame(), "trades": trades,
                      "performance": regime_performance(trades, decisions),
                      "dashboard": gate.dashboard.current, "artifacts": dict(gate.engine.artifacts),
                      "config": regime.to_dict(), "errors": gate.errors}
    return StrategyRun(label or f"r_{cfg.target.r_multiple:g}", cfg, result, strategy.diagnostics_frame(),
                       strategy.signals_frame(), period, regime_out)


def run_from_csv(csv_path: str | Path, app: AppConfig, bt: BacktestConfig, cfg: VwapPullbackConfig,
                 r_multiples: Sequence[float] | None = None) -> tuple[MarketData, list[StrategyRun]]:
    """Build market data once, then run the strategy for each R-multiple (or the configured one).

    Several R-multiples are run side by side for inspection only: nothing is ranked
    or selected automatically.
    """
    md = build_market_data(csv_path, app)
    values = list(r_multiples) if r_multiples else [cfg.target.r_multiple]
    runs = [run_strategy(md, app, bt, cfg.with_r_multiple(r)) for r in values]
    return md, runs


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return str(obj)
    if isinstance(obj, (pd.Timestamp, pd.Timedelta)):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def write_outputs(run: StrategyRun, md: MarketData, app: AppConfig, bt: BacktestConfig, out_dir: str | Path,
                  *, manifest: dict[str, Any] | None = None) -> Path:
    """Write every artefact of one run to ``out_dir/<label>/`` and return that folder.

    ``manifest``: when given (keys ``csv_path``, ``data_sha256``, ``config_texts`` and
    optionally ``command_line``, ``oos``), a reproducibility ``manifest.json`` is written.
    """
    folder = Path(out_dir) / run.label
    folder.mkdir(parents=True, exist_ok=True)
    r = run.result
    r.trades.to_csv(folder / "trades.csv", index=False)
    r.equity.to_csv(folder / "equity.csv")
    r.daily_pnl.to_csv(folder / "daily_pnl.csv")
    run.diagnostics.to_csv(folder / "signal_diagnostics.csv", index=False)
    r.rejections.to_csv(folder / "engine_rejections.csv", index=False)
    r.adjustments.to_csv(folder / "adjustments.csv", index=False)
    r.orders.to_csv(folder / "orders.csv", index=False)
    r.entry_checks.to_csv(folder / "entry_checks.csv", index=False)
    if run.signals is not None:
        run.signals.to_csv(folder / "signals.csv", index=False)
    (folder / "account_protection.json").write_text(json.dumps(snapshot(r.account), indent=2), encoding="utf-8")
    (folder / "metrics.json").write_text(json.dumps(_jsonable(r.metrics), indent=2), encoding="utf-8")
    config_used = {
        "source": md.report.source,
        "synthetic_data": md.is_synthetic,
        "contract": {"label": md.contract.label, **_jsonable(vars(md.contract.spec))},
        "session": {"name": app.session.name, "start": str(app.session.start), "end": str(app.session.end),
                    "timezone": app.session.timezone},
        "strategy": run.strategy_config.to_dict(),
        "backtest": _jsonable(_dataclass_dict(bt)),
        "config_files": {"settings": str(app.source_path)},
    }
    (folder / "config_used.json").write_text(json.dumps(_jsonable(config_used), indent=2, default=str),
                                             encoding="utf-8")
    (folder / "warnings.txt").write_text("\n".join(r.warnings) + "\n", encoding="utf-8")
    (folder / "data_quality.txt").write_text(md.report.summary() + "\n", encoding="utf-8")
    if run.regime is not None:
        rg = run.regime
        rg["decisions"].to_csv(folder / "regime_decisions.csv", index=False)
        rg["snapshots"].to_csv(folder / "regime_snapshots.csv", index=False)
        rg["trades"].to_csv(folder / "regime_trades.csv", index=False)
        (folder / "regime_performance.json").write_text(json.dumps(_jsonable(rg["performance"]), indent=2,
                                                                   default=str), encoding="utf-8")
        (folder / "regime_dashboard.json").write_text(json.dumps(_jsonable(rg["dashboard"]), indent=2, default=str),
                                                      encoding="utf-8")
    if manifest is not None:
        period = run.period.to_dict() if run.period else {"label": "full", "kind": "full", "fold": None,
                                                           "start": None, "end": None}
        sessions = session_dates(r.equity.reset_index()) if len(r.equity) else []
        write_manifest(folder, build_manifest(
            md=md, app=app, bt=bt, strategy_config=run.strategy_config, result=r, label=run.label, period=period,
            sessions_in_period=[str(sessions[0].date()), str(sessions[-1].date()), len(sessions)] if sessions else None,
            **manifest))
    return folder


def _dataclass_dict(obj: Any) -> Any:
    from dataclasses import fields, is_dataclass

    if is_dataclass(obj):
        return {f.name: _dataclass_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: _dataclass_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dataclass_dict(v) for v in obj]
    return obj


def summary_table(runs: Sequence[StrategyRun]) -> pd.DataFrame:
    """Side-by-side key figures per run (for inspection only; no ranking)."""
    keys = ["total_trades", "net_profit", "gross_pnl_before_costs", "total_costs", "win_rate", "profit_factor",
            "expectancy", "average_r_multiple", "max_drawdown", "ambiguous_trades"]
    rows = {run.label: {k: run.result.metrics.get(k) for k in keys} for run in runs}
    return pd.DataFrame(rows)
