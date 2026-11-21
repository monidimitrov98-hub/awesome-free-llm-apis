"""Paper orders with an explicit, enforced lifecycle and an auditable history."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

import pandas as pd


class PaperOrderState(StrEnum):
    ACCEPTED = "accepted"            # working
    TRIGGERED = "triggered"          # stop order whose trigger was hit, fill pending
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


TERMINAL = {PaperOrderState.FILLED, PaperOrderState.CANCELLED, PaperOrderState.REJECTED, PaperOrderState.EXPIRED}
TRANSITIONS = {
    PaperOrderState.ACCEPTED: {PaperOrderState.TRIGGERED, PaperOrderState.PARTIALLY_FILLED, PaperOrderState.FILLED,
                               PaperOrderState.CANCELLED, PaperOrderState.REJECTED, PaperOrderState.EXPIRED},
    PaperOrderState.TRIGGERED: {PaperOrderState.PARTIALLY_FILLED, PaperOrderState.FILLED, PaperOrderState.CANCELLED,
                                PaperOrderState.REJECTED},
    PaperOrderState.PARTIALLY_FILLED: {PaperOrderState.PARTIALLY_FILLED, PaperOrderState.FILLED,
                                       PaperOrderState.CANCELLED, PaperOrderState.EXPIRED},
}


class LifecycleError(RuntimeError):
    """An impossible order state transition was attempted (a bug, never silently ignored)."""


@dataclass
class PaperOrder:
    order_id: str
    root: str
    role: str                 # entry | stop_loss | take_profit | exit
    order_type: str           # market | limit | stop
    is_buy: bool
    quantity: int
    created_at: pd.Timestamp
    active_after_seq: int     # only events with a LATER sequence number can fill it (no same-event fills)
    limit_price: float | None = None
    stop_price: float | None = None
    expire_at: pd.Timestamp | None = None
    parent_id: str | None = None
    oco_group: str | None = None
    client_id: str | None = None
    tag: str = ""
    stop_loss: float | None = None       # bracket prices carried by entries
    take_profit: float | None = None
    reference_price: float | None = None
    max_stop_distance: float | None = None
    min_stop_distance: float | None = None
    min_reward_risk: float | None = None
    risk_budget: float | None = None
    session_date: pd.Timestamp | None = None
    signal_time: pd.Timestamp | None = None
    auth_token: str | None = None        # entries: one-time EntryAuthorizer approval (never persisted as valid)
    state: PaperOrderState = PaperOrderState.ACCEPTED
    filled_qty: int = 0
    avg_fill_price: float | None = None
    posted: bool = False                 # limit: rested at least one event (no longer a taker)
    reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_qty

    @property
    def is_working(self) -> bool:
        return self.state not in TERMINAL

    def transition(self, new: PaperOrderState, when: pd.Timestamp, reason: str = "") -> None:
        if new not in TRANSITIONS.get(self.state, set()):
            raise LifecycleError(f"order {self.order_id}: {self.state.value} -> {new.value} is not allowed")
        self.history.append({"time": when.isoformat(), "from": self.state.value, "to": new.value, "reason": reason})
        self.state = new
        if reason:
            self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, pd.Timestamp):
                d[k] = v.isoformat()
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaperOrder":
        d = dict(d)
        for k in ("created_at", "expire_at", "session_date", "signal_time"):
            if d.get(k) is not None:
                d[k] = pd.Timestamp(d[k])
        d["state"] = PaperOrderState(d["state"])
        return cls(**d)


@dataclass(frozen=True)
class Execution:
    """One simulated fill. Every position/balance change is traceable to one of these."""

    exec_id: str
    order_id: str
    root: str
    role: str
    order_type: str
    is_buy: bool
    quantity: int
    requested_price: float | None   # limit/stop price, None for market
    reference_price: float          # bid/ask/trade the fill was based on (before slippage)
    fill_price: float
    slippage_ticks: int
    slippage_cost: float
    commission: float
    ts: pd.Timestamp
    event_id: str
    reason: str                     # e.g. "market_at_ask", "limit_trade_through", "stop_triggered_gap"
    quote_based: bool
    synthetic_data: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Execution":
        d = dict(d)
        d["ts"] = pd.Timestamp(d["ts"])
        return cls(**d)
