"""Order requests (from strategies), simulated orders and fills."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pandas as pd


class Side(StrEnum):
    """Position direction."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


class OrderType(StrEnum):
    MARKET = "market"
    STOP = "stop"
    LIMIT = "limit"


class OrderRole(StrEnum):
    ENTRY = "entry"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    EXIT = "exit"  # market exit requested by the strategy or forced by the engine


class OrderStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class OrderRequest:
    """What a strategy asks for. The engine validates, sizes and risk-checks it.

    Use the constructors :meth:`enter`, :meth:`exit` and :meth:`cancel_entry`.
    """

    action: str  # "entry" | "exit" | "cancel_entry"
    side: Side | None = None
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    quantity: int | None = None
    tag: str = ""
    client_id: str | None = None  # strategy's signal id; echoed in order events and logs
    # Fill-time limits (price points / reward-to-risk). The engine re-checks them against the
    # ACTUAL fill price and rejects the entry when violated, whatever the sizing method.
    max_stop_distance: float | None = None
    min_stop_distance: float | None = None
    min_reward_risk: float | None = None

    @classmethod
    def enter(
        cls,
        side: Side | str,
        order_type: OrderType | str = OrderType.MARKET,
        *,
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        quantity: int | None = None,
        tag: str = "",
        client_id: str | None = None,
        max_stop_distance: float | None = None,
        min_stop_distance: float | None = None,
        min_reward_risk: float | None = None,
    ) -> "OrderRequest":
        """Entry order (market, stop or limit) with optional bracket prices and fill-time limits."""
        return cls("entry", Side(side), OrderType(order_type), price, stop_loss, take_profit, quantity, tag,
                   client_id, max_stop_distance, min_stop_distance, min_reward_risk)

    @classmethod
    def exit(cls, tag: str = "") -> "OrderRequest":
        """Close the whole open position at market on the next eligible bar."""
        return cls("exit", tag=tag)

    @classmethod
    def cancel_entry(cls, tag: str = "") -> "OrderRequest":
        """Cancel a pending (unfilled) entry order."""
        return cls("cancel_entry", tag=tag)


@dataclass
class Order:
    """A simulated order inside the engine."""

    id: int
    role: OrderRole
    order_type: OrderType
    is_buy: bool
    quantity: int
    price: float | None
    created_at: pd.Timestamp
    active_from: pd.Timestamp
    signal_time: pd.Timestamp | None
    session_date: pd.Timestamp | None
    expire_at: pd.Timestamp | None = None
    status: OrderStatus = OrderStatus.PENDING
    reason: str = ""
    tag: str = ""
    stop_loss: float | None = None  # bracket prices carried by entry orders
    take_profit: float | None = None
    filled_at: pd.Timestamp | None = None
    fill_price: float | None = None
    risk_budget: float | None = None  # set when sized by the risk method; re-checked at fill
    requested_quantity: int | None = None  # quantity before any reduction at fill
    client_id: str | None = None
    reference_price: float | None = None  # price the bracket was planned from (signal close / order price)
    max_stop_distance: float | None = None
    min_stop_distance: float | None = None
    min_reward_risk: float | None = None

    @property
    def is_active(self) -> bool:
        return self.status is OrderStatus.PENDING


@dataclass(frozen=True)
class OrderEvent:
    """Lifecycle notification sent to the strategy for orders carrying a ``client_id``.

    ``status``: ``accepted`` (order created), ``rejected`` (``stage`` = signal or fill),
    ``filled`` or ``cancelled``.
    """

    client_id: str
    order_id: int | None
    status: str
    time: pd.Timestamp
    stage: str = ""
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Fill:
    """An executed (simulated) fill."""

    order_id: int
    role: OrderRole
    is_buy: bool
    quantity: int
    price: float
    bar_time: pd.Timestamp  # open time of the execution bar the fill happened in
    at_open: bool  # True if filled at the bar open, False if inside the bar
    commission: float
    slippage_cost: float
    bar_complete: bool
    base_price: float = float("nan")  # fill price before slippage
