"""Position sizing."""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.backtest.config import CostConfig, SizingConfig, SlippageConfig
from src.config import ContractSpec


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
) -> SizeResult:
    """Number of contracts for a new entry.

    * A positive ``requested`` quantity from the strategy is used as is.
    * ``fixed``: ``cfg.contracts``.
    * ``risk``: ``floor(risk_per_trade / risk_per_contract)`` where
      ``risk_per_contract = |entry - stop| * point_value`` plus, when
      ``include_costs_in_risk``, round-trip commission and the configured adverse
      slippage on entry and stop exit. Returns 0 with a reason when it does not fit.

    The risk engine still enforces ``max_position_size`` afterwards.
    """
    if requested is not None:
        if requested < 1:
            return SizeResult(0, "invalid_quantity")
        return SizeResult(int(requested))
    if cfg.method == "fixed":
        return SizeResult(cfg.contracts)
    if stop_loss is None:
        return SizeResult(0, "risk_sizing_requires_stop_loss")
    per_contract = abs(entry_price - stop_loss) * spec.point_value
    if cfg.include_costs_in_risk:
        if cost is not None:
            per_contract += 2 * cost.commission_per_contract_per_side
        if slippage is not None:
            per_contract += (slippage.market_ticks + slippage.stop_ticks) * spec.tick_value
    if per_contract <= 0:
        return SizeResult(0, "zero_risk_per_contract")
    qty = math.floor(cfg.risk_per_trade / per_contract + 1e-9)
    if qty < 1:
        return SizeResult(0, "size_below_one_contract")
    return SizeResult(qty)
