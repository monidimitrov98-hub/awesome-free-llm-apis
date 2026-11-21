"""Position sizing."""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.backtest.config import CostConfig, SizingConfig, SlippageConfig
from src.backtest.orders import OrderType
from src.config import ContractSpec


def entry_slippage_ticks(order_type: OrderType, slippage: SlippageConfig) -> int:
    """Configured adverse slippage (ticks) for an entry of ``order_type``."""
    return {OrderType.MARKET: slippage.market_ticks, OrderType.STOP: slippage.stop_ticks,
            OrderType.LIMIT: slippage.limit_ticks}[order_type]


def risk_per_contract(
    spec: ContractSpec,
    *,
    entry_price: float,
    stop_loss: float,
    cost: CostConfig | None,
    slippage: SlippageConfig | None,
    include_costs: bool,
    entry_slippage_ticks: int,
) -> float:
    """Dollar loss per contract if the stop-loss is hit.

    ``|entry_price - stop_loss| * point_value`` plus, when ``include_costs``:
    round-trip commission, ``entry_slippage_ticks`` and the configured stop-exit
    slippage. At signal time ``entry_price`` is a reference price and
    ``entry_slippage_ticks`` the configured entry slippage; at fill time
    ``entry_price`` is the actual fill (slippage already inside it), so
    ``entry_slippage_ticks`` must be 0 to avoid counting it twice.

    A gap through the stop at exit can still lose more; this is a model, not a bound.
    """
    per = abs(entry_price - stop_loss) * spec.point_value
    if include_costs:
        if cost is not None:
            per += 2 * cost.commission_per_contract_per_side
        if slippage is not None:
            per += (entry_slippage_ticks + slippage.stop_ticks) * spec.tick_value
    return per


@dataclass(frozen=True)
class SizeResult:
    quantity: int
    reason: str = ""  # non-empty when the size could not be computed


def position_size(
    cfg: SizingConfig,
    spec: ContractSpec,
    *,
    entry_price: float,
    stop_loss: float | None,
    requested: int | None = None,
    cost: CostConfig | None = None,
    slippage: SlippageConfig | None = None,
    order_type: OrderType = OrderType.MARKET,
) -> SizeResult:
    """Number of contracts for a new entry.

    * A positive ``requested`` quantity from the strategy is used as is.
    * ``fixed``: ``cfg.contracts``.
    * ``risk``: ``floor(risk_per_trade / risk_per_contract)`` (see :func:`risk_per_contract`;
      the entry slippage is the one configured for ``order_type``). Returns 0 with a
      reason when not even one contract fits.

    This is the signal-time estimate. The engine recomputes the risk at the actual
    fill price and applies ``sizing.fill_risk_policy`` (see engine docs). The risk
    engine still enforces ``max_position_size``.
    """
    if requested is not None:
        if requested < 1:
            return SizeResult(0, "invalid_quantity")
        return SizeResult(int(requested))
    if cfg.method == "fixed":
        return SizeResult(cfg.contracts)
    if stop_loss is None:
        return SizeResult(0, "risk_sizing_requires_stop_loss")
    per_contract = risk_per_contract(
        spec, entry_price=entry_price, stop_loss=stop_loss, cost=cost, slippage=slippage,
        include_costs=cfg.include_costs_in_risk,
        entry_slippage_ticks=entry_slippage_ticks(order_type, slippage) if slippage else 0,
    )
    if per_contract <= 0:
        return SizeResult(0, "zero_risk_per_contract")
    qty = math.floor(cfg.risk_per_trade / per_contract + 1e-9)
    if qty < 1:
        return SizeResult(0, "size_below_one_contract")
    return SizeResult(qty)
