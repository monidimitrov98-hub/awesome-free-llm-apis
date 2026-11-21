"""Entry authorization: the ONLY way a new paper entry order comes into existence.

Signal time (after the strategy and the regime gate produced an entry request):

  1. run mode            infrastructure validation never authorizes entries
  2. system state        reconciliation OK, persistence OK, not shutting down, no restart block
  3. market data health  connected, fresh, recovered after any gap
  4. request validity    side, quantity, finite prices; stop-loss required when AccountGuard is on
  5. position state      flat (or an explicitly permitted reversal), no pending entry
  6. NewsGuard           operational mode in paper trading (CALENDAR INCOMPLETE => blocked)
  7. sizing + bracket    existing position_size / bracket validity at the reference price
  8. session cutoff      no entry at/after the configured pre-session-end flatten time
  9. AccountGuard        existing drawdown/lock/safety-buffer checks
 10. RiskManager         existing daily limits, size, windows, emergency stop

Fill time (before any entry fill is executed): system state, NewsGuard at the fill time,
bracket validity and fill-quality limits at the ACTUAL fill price, risk budget (reduce or
reject), AccountGuard with the actual trade risk + gap reserve, RiskManager. Any failure
rejects the order. Exits and protective orders never pass through here.

Order note: the backtest engine asks RiskManager before AccountGuard; here AccountGuard is asked
first as specified for Sprint 5. Both are independent vetoes, so the outcome is identical --
only the first recorded rejection reason can differ.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from src.backtest.config import BacktestConfig
from src.backtest.execution import apply_slippage, conservative_order_price
from src.backtest.orders import OrderRequest, OrderType, Side
from src.backtest.sizing import entry_slippage_ticks, position_size, risk_per_contract
from src.config import ContractSpec
from src.paper.orders import PaperOrder


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    stages: list[dict[str, Any]] = field(default_factory=list)

    def add(self, stage: str, ok: bool, reason: str = "", **kw: Any) -> bool:
        self.stages.append({"stage": stage, "ok": ok, "reason": "" if ok else reason, **kw})
        if not ok and self.allowed:
            self.allowed, self.reason = False, reason
        return ok


class EntryAuthorizer:
    def __init__(self, *, mode: str, bt: BacktestConfig, specs: dict[str, ContractSpec], account, broker,
                 news_guards: dict[str, Any], system_state: Callable[[str, pd.Timestamp], tuple[bool, str]],
                 flatten_time: Callable[[pd.Timestamp], pd.Timestamp], allow_reversal: bool = False) -> None:
        self.mode = mode
        self.bt = bt
        self.specs = specs
        self.account = account
        self.broker = broker
        self.news = news_guards
        self.system_state = system_state
        self.flatten_time = flatten_time
        self.allow_reversal = allow_reversal
        self._issued: dict[str, tuple] = {}

    # ------------------------------------------------------------------ authorization tokens
    @staticmethod
    def _fingerprint(o: PaperOrder) -> tuple:
        return (o.root, o.role, o.order_type, o.is_buy, o.quantity, o.limit_price, o.stop_price, o.stop_loss,
                o.take_profit, o.risk_budget, o.session_date)

    def issue(self, o: PaperOrder) -> None:
        """Record that THIS entry order (with exactly these terms) passed authorize_signal."""
        self._issued[o.order_id] = self._fingerprint(o)

    def check_token(self, o: PaperOrder) -> bool:
        """Broker hook: an entry is accepted only once, only if issued here, only with unchanged terms."""
        fp = self._issued.pop(o.order_id, None)
        return fp is not None and fp == self._fingerprint(o)

    # ------------------------------------------------------------------ signal time
    def authorize_signal(self, req: OrderRequest, root: str, now: pd.Timestamp, session_date: pd.Timestamp | None,
                         ref_close: float) -> tuple[Decision, dict[str, Any]]:
        d = Decision(True)
        out: dict[str, Any] = {}
        if not d.add("mode", self.mode != "infrastructure_validation", "infrastructure_validation_mode"):
            return d, out
        ok, why = self.system_state(root, now)
        if not d.add("system", ok, why):
            return d, out
        if req.action != "entry" or req.side is None:
            d.add("request", False, "not_an_entry")
            return d, out
        if req.quantity is not None and (isinstance(req.quantity, bool) or not isinstance(req.quantity, int)
                                         or req.quantity < 1):
            d.add("request", False, "invalid_quantity")
            return d, out
        for name in ("price", "stop_loss", "take_profit"):
            v = getattr(req, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0):
                d.add("request", False, "invalid_order_price", field=name)
                return d, out
        if self.account.guard.enabled and req.stop_loss is None:
            d.add("request", False, "account_requires_stop_loss")
            return d, out
        d.add("request", True)
        pos = self.account.position_qty(root)
        sgn = 1 if req.side is Side.LONG else -1
        if pos != 0:
            if (pos > 0) == (sgn > 0) or not self.allow_reversal:
                d.add("position", False, "position_open" if (pos > 0) == (sgn > 0) else "reversal_not_permitted")
                return d, out
            out["reverse_first"] = True
        if self.broker.working(root, ("entry",)):
            d.add("position", False, "entry_pending")
            return d, out
        d.add("position", True)
        nd = self.news[root].check_entry(now, purpose="entry_signal", client_id=req.client_id, side=req.side.value)
        if not d.add("news", nd.allowed, f"news_{nd.status.state.value.lower()}", explanation=nd.record.get("explanation")):
            return d, out
        spec = self.specs[root]
        tick = spec.tick_size
        is_buy = sgn > 0
        price = None
        if req.order_type is not OrderType.MARKET:
            if req.price is None:
                d.add("sizing", False, "missing_order_price")
                return d, out
            price = conservative_order_price(req.price, tick, req.order_type, is_buy)
        ref = price if price is not None else ref_close
        stop = conservative_order_price(req.stop_loss, tick, OrderType.STOP, not is_buy) if req.stop_loss else None
        target = conservative_order_price(req.take_profit, tick, OrderType.LIMIT, not is_buy) if req.take_profit else None
        if stop is not None and (stop - ref) * sgn >= 0:
            d.add("sizing", False, "invalid_bracket")
            return d, out
        if target is not None and (target - ref) * sgn <= 0:
            d.add("sizing", False, "invalid_bracket")
            return d, out
        size = position_size(self.bt.sizing, spec, entry_price=ref, stop_loss=stop, requested=req.quantity,
                             cost=self.bt.cost_for(root), slippage=self.bt.slippage, order_type=req.order_type)
        if not d.add("sizing", size.quantity >= 1, size.reason or "size_below_one_contract", quantity=size.quantity):
            return d, out
        if session_date is None or now >= self.flatten_time(session_date):
            d.add("session", False, "session_end_cutoff" if session_date is not None else "outside_session")
            return d, out
        d.add("session", True)
        equity = self.account.equity()
        blocked = self.account.guard.entry_block_reason(equity, session_date)
        if not d.add("account_guard", blocked is None, blocked or "", equity=equity if math.isfinite(equity) else None):
            return d, out
        rd = self.account.risk.check_entry(when=now, session_date=session_date, quantity=size.quantity)
        if not d.add("risk_manager", rd.approved, rd.reason):
            return d, out
        out.update(price=price, stop=stop, target=target, quantity=size.quantity, reference=ref,
                   risk_budget=(self.bt.sizing.risk_per_trade if self.bt.sizing.method == "risk" and req.quantity is None
                                else None))
        return d, out

    # ------------------------------------------------------------------ fill time
    def authorize_fill(self, o: PaperOrder, base_price: float, qty: int, ev) -> tuple[Decision, int | None]:
        d = Decision(True)
        if not d.add("mode", self.mode != "infrastructure_validation", "infrastructure_validation_mode"):
            return d, None
        ok, why = self.system_state(o.root, ev.ts)
        if not d.add("system", ok, why):
            return d, None
        if o.session_date is None or ev.ts >= self.flatten_time(o.session_date):
            d.add("session", False, "entry_cutoff_reached_at_fill")
            return d, None
        nd = self.news[o.root].check_entry(ev.ts, purpose="entry_fill", client_id=o.client_id,
                                           side="long" if o.is_buy else "short")
        if not d.add("news", nd.allowed, f"news_{nd.status.state.value.lower()}"):
            return d, None
        spec = self.specs[o.root]
        otype = OrderType(o.order_type)
        fill = apply_slippage(base_price, o.is_buy, entry_slippage_ticks(otype, self.bt.slippage), spec.tick_size)
        sgn = 1 if o.is_buy else -1
        if o.stop_loss is not None and (o.stop_loss - fill) * sgn >= 0:
            d.add("bracket", False, "invalid_bracket_at_fill")
            return d, None
        if o.take_profit is not None and (o.take_profit - fill) * sgn <= 0:
            d.add("bracket", False, "invalid_bracket_at_fill")
            return d, None
        if o.stop_loss is not None:
            dist = abs(fill - o.stop_loss)
            if o.max_stop_distance is not None and dist > o.max_stop_distance + 1e-9:
                d.add("quality", False, "stop_too_wide_at_fill")
                return d, None
            if o.min_stop_distance is not None and dist < o.min_stop_distance - 1e-9:
                d.add("quality", False, "stop_too_tight_at_fill")
                return d, None
            if o.min_reward_risk is not None and o.take_profit is not None and \
                    abs(o.take_profit - fill) / dist < o.min_reward_risk - 1e-9:
                d.add("quality", False, "reward_risk_below_min_at_fill")
                return d, None
        total = o.filled_qty + qty
        per = risk_per_contract(spec, entry_price=fill, stop_loss=o.stop_loss, cost=self.bt.cost_for(o.root),
                                slippage=self.bt.slippage, include_costs=True, entry_slippage_ticks=0) \
            if o.stop_loss is not None else math.nan
        if o.risk_budget is not None and math.isfinite(per) and total * per > o.risk_budget + 1e-9:
            if self.bt.sizing.fill_risk_policy == "reject":
                d.add("risk_budget", False, "risk_budget_exceeded_at_fill")
                return d, None
            allowed_total = math.floor(o.risk_budget / per + 1e-9)
            qty = allowed_total - o.filled_qty
            if qty < 1:
                d.add("risk_budget", False, "risk_budget_exceeded_at_fill")
                return d, None
            total = allowed_total
        rules = self.bt.account_rules
        account_risk = total * per + total * rules.gap_reserve_ticks * spec.tick_value if math.isfinite(per) else None
        equity = self.account.equity()
        blocked = self.account.guard.fill_block_reason(equity, o.session_date, account_risk)
        if not d.add("account_guard", blocked is None, blocked or "", account_risk=account_risk):
            return d, None
        rd = self.account.risk.check_entry(when=ev.ts, session_date=o.session_date, quantity=total)
        if not d.add("risk_manager", rd.approved, rd.reason):
            return d, None
        return d, qty
