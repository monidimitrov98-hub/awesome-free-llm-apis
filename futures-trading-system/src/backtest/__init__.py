"""Event-driven backtesting engine (historical simulation only; no broker, no real orders).

Exports are resolved lazily (PEP 562) so that ``src.strategy`` can import
``src.backtest.orders`` without a circular import through the engine.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "BacktestConfig": "src.backtest.config",
    "load_backtest_config": "src.backtest.config",
    "BacktestEngine": "src.backtest.engine",
    "BacktestResult": "src.backtest.engine",
    "OrderRequest": "src.backtest.orders",
    "OrderType": "src.backtest.orders",
    "Side": "src.backtest.orders",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        return getattr(import_module(_EXPORTS[name]), name)
    raise AttributeError(f"module 'src.backtest' has no attribute {name!r}")
