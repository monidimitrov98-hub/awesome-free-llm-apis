"""InstrumentResolver: exact mapping between provider contract names and internal roots.

Only the configured contract of each root is accepted (e.g. "MES 12-26" -> MES). Anything else --
another expiry, another product, a continuous name -- is an instrument mismatch and is refused,
so a roll or a typo can never feed one instrument's prices into another's account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from src.config import ContractSpec
from src.data.contracts import parse_contract_symbol
from src.data.errors import ContractError


class InstrumentError(ValueError):
    """A provider instrument is unknown, ambiguous or not the configured contract."""


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


@dataclass(frozen=True)
class ResolvedContract:
    root: str
    name: str        # exact provider name, e.g. "MES 12-26"
    year: int
    month: int

    @property
    def last_trade_date(self) -> date:
        """Equity-index futures (MES/MNQ) expire on the third Friday of the contract month."""
        return third_friday(self.year, self.month)


class InstrumentResolver:
    def __init__(self, contracts: dict[str, str], specs: dict[str, ContractSpec]) -> None:
        self._by_name: dict[str, ResolvedContract] = {}
        self.by_root: dict[str, ResolvedContract] = {}
        for root, name in contracts.items():
            try:
                info = parse_contract_symbol(name, {root: specs[root]})
            except (ContractError, KeyError) as exc:
                raise InstrumentError(f"{name!r} is not a valid {root} contract: {exc}") from None
            if info.root != root or info.is_continuous or info.year is None or info.month is None:
                raise InstrumentError(f"{name!r} must name one {root} contract month, e.g. '{root} 12-26'")
            if name != name.strip() or "  " in name:
                raise InstrumentError(f"{name!r}: use the exact NinjaTrader name, e.g. '{root} 12-26'")
            rc = ResolvedContract(root, name, info.year, info.month)
            self._by_name[name] = rc
            self.by_root[root] = rc

    def resolve(self, name: str) -> ResolvedContract:
        try:
            return self._by_name[name]
        except (KeyError, TypeError):
            raise InstrumentError(f"instrument {str(name)[:40]!r} is not a configured contract") from None

    def roll_warnings(self, today: date, warn_days: int = 10) -> list[str]:
        out = []
        for rc in self.by_root.values():
            left = (rc.last_trade_date - today).days
            if left < 0:
                out.append(f"{rc.name} expired on {rc.last_trade_date}: configure the next contract")
            elif left <= warn_days:
                out.append(f"{rc.name} expires on {rc.last_trade_date} ({left} days): plan the roll")
        return out
