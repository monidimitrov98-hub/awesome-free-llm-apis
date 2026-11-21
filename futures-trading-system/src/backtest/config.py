"""Backtest configuration loaded from ``config/backtest.toml``."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError
from src.risk.manager import RiskConfig

DEFAULT_BACKTEST_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "backtest.toml"


@dataclass(frozen=True)
class CostConfig:
    """Per-contract, per-side cost (commission + fees) for one contract root."""

    commission_per_contract_per_side: float


@dataclass(frozen=True)
class SlippageConfig:
    """Adverse slippage in ticks by order type."""

    market_ticks: int = 1
    stop_ticks: int = 1
    limit_ticks: int = 0


@dataclass(frozen=True)
class ExecutionConfig:
    """Fill-simulation and data-quality policies."""

    incomplete_signal_bar_policy: str = "skip"
    entries_on_incomplete_bars: bool = False
    max_fill_delay: pd.Timedelta = pd.Timedelta(minutes=2)
    limit_fill_requires_trade_through: bool = True
    flatten_before_session_end: pd.Timedelta = pd.Timedelta(minutes=5)


@dataclass(frozen=True)
class SizingConfig:
    """Position sizing."""

    method: str = "fixed"
    contracts: int = 1
    risk_per_trade: float = 0.0
    include_costs_in_risk: bool = True


@dataclass(frozen=True)
class BacktestConfig:
    """Complete backtest configuration."""

    initial_capital: float
    costs: dict[str, CostConfig]
    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    def cost_for(self, root: str) -> CostConfig:
        """Cost configuration for ``root``; raises if none is configured."""
        try:
            return self.costs[root.upper()]
        except KeyError as exc:
            raise ConfigError(f"No [costs.{root}] section in the backtest configuration") from exc


def _number(raw: dict[str, Any], key: str, where: str, *, kind: type = float, minimum: float = 0.0) -> Any:
    if key not in raw:
        raise ConfigError(f"Missing '{key}' in [{where}]")
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"{where}.{key} must be a number >= {minimum}, got {value!r}")
    if kind is int and int(value) != value:
        raise ConfigError(f"{where}.{key} must be an integer")
    return kind(value)


def load_backtest_config(path: str | Path | None = None) -> BacktestConfig:
    """Load and validate the backtest TOML file."""
    cfg_path = Path(path) if path is not None else DEFAULT_BACKTEST_CONFIG_PATH
    if not cfg_path.is_file():
        raise ConfigError(f"Backtest configuration not found: {cfg_path}")
    with cfg_path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Cannot parse {cfg_path}: {exc}") from exc

    account = raw.get("account", {})
    capital = _number(account, "initial_capital", "account", minimum=0.0)
    if capital <= 0:
        raise ConfigError("account.initial_capital must be positive")

    costs = {
        root.upper(): CostConfig(_number(c, "commission_per_contract_per_side", f"costs.{root}"))
        for root, c in raw.get("costs", {}).items()
    }
    if not costs:
        raise ConfigError("At least one [costs.<ROOT>] section is required")

    s = raw.get("slippage", {})
    slippage = SlippageConfig(
        market_ticks=_number(s, "market_ticks", "slippage", kind=int),
        stop_ticks=_number(s, "stop_ticks", "slippage", kind=int),
        limit_ticks=_number(s, "limit_ticks", "slippage", kind=int),
    )

    e = raw.get("execution", {})
    policy = str(e.get("incomplete_signal_bar_policy", "skip"))
    if policy not in {"skip", "allow"}:
        raise ConfigError("execution.incomplete_signal_bar_policy must be 'skip' or 'allow'")
    execution = ExecutionConfig(
        incomplete_signal_bar_policy=policy,
        entries_on_incomplete_bars=bool(e.get("entries_on_incomplete_bars", False)),
        max_fill_delay=pd.Timedelta(minutes=_number(e, "max_fill_delay_minutes", "execution")),
        limit_fill_requires_trade_through=bool(e.get("limit_fill_requires_trade_through", True)),
        flatten_before_session_end=pd.Timedelta(
            minutes=_number(e, "flatten_minutes_before_session_end", "execution")
        ),
    )

    z = raw.get("sizing", {})
    method = str(z.get("method", "fixed"))
    if method not in {"fixed", "risk"}:
        raise ConfigError("sizing.method must be 'fixed' or 'risk'")
    sizing = SizingConfig(
        method=method,
        contracts=_number(z, "contracts", "sizing", kind=int, minimum=1),
        risk_per_trade=_number(z, "risk_per_trade", "sizing"),
        include_costs_in_risk=bool(z.get("include_costs_in_risk", True)),
    )
    if method == "risk" and sizing.risk_per_trade <= 0:
        raise ConfigError("sizing.risk_per_trade must be positive when method = 'risk'")

    return BacktestConfig(
        initial_capital=capital,
        costs=costs,
        slippage=slippage,
        execution=execution,
        sizing=sizing,
        risk=RiskConfig.from_dict(raw.get("risk", {})),
    )
