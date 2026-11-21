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


def _frames(md: MarketData, cfg: VwapPullbackConfig) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    needed = [cfg.signal_timeframe, cfg.execution_timeframe] + ([cfg.context_timeframe] if cfg.context_timeframe else [])
    missing = [tf for tf in needed if tf not in md.frames]
    if missing:
        raise ConfigError(f"Timeframes {missing} are not produced by the pipeline; add them to "
                          "config/settings.toml [resample] timeframes")
    context = {cfg.context_timeframe: md.frames[cfg.context_timeframe]} if cfg.context_timeframe else {}
    return md.frames[cfg.signal_timeframe], md.frames[cfg.execution_timeframe], context


def run_strategy(md: MarketData, app: AppConfig, bt: BacktestConfig, cfg: VwapPullbackConfig,
                 label: str = "") -> StrategyRun:
    """Backtest the VWAP Trend Pullback strategy on already-built market data."""
    if md.contract.spec is None:
        raise ConfigError(f"No contract specification for {md.contract.root}")
    signal, execution, context = _frames(md, cfg)
    strategy = VwapTrendPullbackStrategy(cfg, app.session.timezone)
    engine = BacktestEngine(bt, md.contract.spec, app.session)
    result = engine.run(strategy, signal, execution, context_bars=context)
    return StrategyRun(label or f"r_{cfg.target.r_multiple:g}", cfg, result, strategy.diagnostics_frame())


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


def write_outputs(run: StrategyRun, md: MarketData, app: AppConfig, bt: BacktestConfig, out_dir: str | Path) -> Path:
    """Write every artefact of one run to ``out_dir/<label>/`` and return that folder."""
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
