"""Strategy interface.

A strategy only ever sees bars whose ``available_at`` is at or before the current
time. It returns :class:`~src.backtest.orders.OrderRequest` objects; the engine
decides whether and when they execute (never earlier than the next eligible
execution bar).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from src.backtest.orders import Fill, OrderRequest, Side
from src.config import ContractSpec


@dataclass(frozen=True)
class PositionSnapshot:
    """Read-only view of the current position."""

    side: Side | None
    quantity: int
    avg_price: float
    unrealized_pnl: float

    @property
    def is_flat(self) -> bool:
        return self.side is None


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy may use for one decision.

    Attributes:
        now: Decision time = ``available_at`` of the current signal bar.
        bar: The current (just completed) signal bar, including indicator columns.
        history: Signal bars up to and including ``bar`` (never later ones). Treat as read-only.
        bar_is_complete: ``bar["is_complete"]``; see the incomplete-bar policy.
        session_date: Trade date of the bar (NaT outside the session).
        position: Current position snapshot.
        has_pending_entry: True while an entry order waits to be filled.
        contract: Contract specification (tick size, point value).
    """

    now: pd.Timestamp
    bar: pd.Series
    history: pd.DataFrame
    bar_is_complete: bool
    session_date: pd.Timestamp | None
    position: PositionSnapshot
    has_pending_entry: bool
    contract: ContractSpec


class Strategy(ABC):
    """Base class for strategies. Subclasses implement :meth:`on_bar`."""

    name: str = "strategy"

    def on_start(self, contract: ContractSpec) -> None:  # noqa: B027 - optional hook
        """Called once before the first bar."""

    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> Sequence[OrderRequest] | None:
        """Decide on the just-completed signal bar. Return requests or None."""

    def on_fill(self, fill: Fill) -> None:  # noqa: B027 - optional hook
        """Called after each simulated fill (informational)."""
