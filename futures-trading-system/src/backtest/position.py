"""Position accounting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    """Signed position in one contract with average entry price.

    ``quantity`` > 0 is long, < 0 short. Supports adding to and reducing a position
    (the engine currently allows at most one entry, but the accounting is general).
    PnL here is gross (before commissions), in dollars.
    """

    point_value: float
    quantity: int = 0
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    def apply_fill(self, is_buy: bool, quantity: int, price: float) -> float:
        """Apply a fill and return the realized gross PnL it produced."""
        if quantity <= 0:
            raise ValueError("fill quantity must be positive")
        signed = quantity if is_buy else -quantity
        realized = 0.0
        if self.quantity == 0 or (self.quantity > 0) == (signed > 0):
            new_qty = self.quantity + signed
            self.avg_price = (self.avg_price * abs(self.quantity) + price * quantity) / abs(new_qty)
            self.quantity = new_qty
            return 0.0
        closing = min(quantity, abs(self.quantity))
        direction = 1 if self.quantity > 0 else -1
        realized = (price - self.avg_price) * direction * closing * self.point_value
        remaining = signed + self.quantity  # sign tells what is left
        if remaining == 0:
            self.quantity, self.avg_price = 0, 0.0
        elif (remaining > 0) == (self.quantity > 0):
            self.quantity = remaining  # partial reduction, avg price unchanged
        else:
            self.quantity, self.avg_price = remaining, price  # reversal: remainder opens at price
        return realized

    def unrealized_pnl(self, mark_price: float) -> float:
        """Gross unrealized PnL at ``mark_price``."""
        if self.quantity == 0:
            return 0.0
        return (mark_price - self.avg_price) * self.quantity * self.point_value
