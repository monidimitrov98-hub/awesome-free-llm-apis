"""Helpers for backtest tests: hand-made bar frames and a scripted strategy."""

from __future__ import annotations

from dataclasses import replace
from datetime import time
from typing import Callable, Sequence

import pandas as pd

from src.backtest.config import BacktestConfig, CostConfig, ExecutionConfig, SizingConfig, SlippageConfig
from src.backtest.engine import BacktestEngine, BacktestResult
from src.backtest.orders import OrderRequest
from src.config import ContractSpec, SessionConfig
from src.data.sessions import assign_session_dates
from src.risk.manager import RiskConfig, RiskManager
from src.strategy.base import Strategy, StrategyContext

NY = "America/New_York"
GLOBEX = SessionConfig("globex", time(18, 0), time(17, 0), NY)
MES = ContractSpec("MES", "Micro E-mini S&P 500", 0.25, 5.0, "HMUZ")
MNQ = ContractSpec("MNQ", "Micro E-mini Nasdaq-100", 0.25, 2.0, "HMUZ")
DAY = "2025-03-10"  # Monday, EDT


def ts(local: str, day: str = DAY) -> pd.Timestamp:
    """New York wall time -> UTC timestamp."""
    return pd.Timestamp(f"{day} {local}").tz_localize(NY).tz_convert("UTC")


def frame(rows: Sequence[tuple], minutes: int = 1, day: str = DAY, complete: Sequence[bool] | None = None) -> pd.DataFrame:
    """Bars from ``(HH:MM, open, high, low, close)`` tuples (New York time, bar open)."""
    idx = pd.DatetimeIndex([ts(r[0], day) for r in rows], name="timestamp")
    df = pd.DataFrame([r[1:5] for r in rows], columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 100.0
    df["is_complete"] = list(complete) if complete is not None else True
    df["available_at"] = idx + pd.Timedelta(minutes=minutes)
    df["session_date"] = assign_session_dates(idx, GLOBEX)
    return df


def flat_bars(start: str, n: int, price: float = 5000.0, minutes: int = 1, day: str = DAY) -> list[tuple]:
    """``n`` quiet bars (high/low = price +/- 0.5) starting at ``start``."""
    t0 = pd.Timestamp(f"{day} {start}")
    return [((t0 + pd.Timedelta(minutes=minutes * i)).strftime("%H:%M"), price, price + 0.5, price - 0.5, price)
            for i in range(n)]


def make_config(
    *,
    commission: float = 1.0,
    market_ticks: int = 1,
    stop_ticks: int = 1,
    limit_ticks: int = 0,
    risk: RiskConfig | None = None,
    sizing: SizingConfig | None = None,
    **execution: object,
) -> BacktestConfig:
    ex = replace(ExecutionConfig(flatten_before_session_end=pd.Timedelta(minutes=5)), **execution)
    return BacktestConfig(
        initial_capital=10_000.0,
        costs={"MES": CostConfig(commission), "MNQ": CostConfig(commission)},
        slippage=SlippageConfig(market_ticks, stop_ticks, limit_ticks),
        execution=ex,
        sizing=sizing or SizingConfig(),
        risk=risk or RiskConfig(),
    )


class ScriptedStrategy(Strategy):
    """Issues pre-defined requests at given signal-bar open times (New York HH:MM)
    and records every context it receives."""

    name = "scripted"

    def __init__(self, script: dict[str, Sequence[OrderRequest]] | None = None,
                 decide: Callable[[StrategyContext], Sequence[OrderRequest] | None] | None = None,
                 day: str = DAY) -> None:
        self.script = {ts(k, day): list(v) for k, v in (script or {}).items()}
        self.decide = decide
        self.contexts: list[StrategyContext] = []
        self.fills: list = []

    def on_bar(self, ctx: StrategyContext):
        self.contexts.append(ctx)
        if self.decide is not None:
            return self.decide(ctx)
        return self.script.get(ctx.bar.name, [])

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


def run(rows_or_frame, script=None, *, contract: ContractSpec = MES, config: BacktestConfig | None = None,
        execution=None, risk_manager: RiskManager | None = None, strategy: Strategy | None = None,
        minutes: int = 1) -> tuple[BacktestResult, Strategy]:
    bars = rows_or_frame if isinstance(rows_or_frame, pd.DataFrame) else frame(rows_or_frame, minutes)
    strat = strategy or ScriptedStrategy(script)
    engine = BacktestEngine(config or make_config(), contract, GLOBEX)
    return engine.run(strat, bars, execution, risk_manager=risk_manager), strat
