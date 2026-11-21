"""Paper execution engine: simulated fills against validated market events. NO NETWORK.

Fill rules (``src/paper`` docs, conservative by design):

* MARKET: on the first event AFTER submission. Buy at the ask, sell at the bid; without a known
  quote, at the event's trade price (flagged ``quote_based = False``). Plus adverse slippage
  (``slippage.market_ticks``). Full quantity (micro contracts; no depth model).
* STOP (stop-market, incl. stop-loss): triggers when a trade prints at/through the stop or the
  quote reaches it (buy: ask >= stop, sell: bid <= stop). Fills at the WORSE of the stop and the
  current market (gaps fill at the market), plus ``slippage.stop_ticks``.
* LIMIT (incl. take-profit): a marketable limit fills at the ask/bid on the first event after
  submission (taker). A resting limit fills ONLY when the market trades THROUGH it (trade strictly
  better than the limit, or the opposite quote strictly through it) -- a touch never fills. Fill at
  the limit price; quantity limited to ``participation`` x the trade size (or the quote size):
  partial fills are possible.
* Brackets: an entry fill creates/extends a stop-loss and take-profit (OCO). A partial exit
  reduces the sibling; a complete exit cancels it.
* Orders are only filled by events with a higher sequence number than the event during which
  they were created: no fill can use information that preceded the decision.

Entry fills are authorized by a callback (NewsGuard, bracket/quality checks, risk budget,
AccountGuard, RiskManager at the actual fill price) BEFORE anything is executed; a refusal
rejects the order -- it is never converted into a fill.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from src.backtest.config import BacktestConfig
from src.backtest.execution import apply_slippage, round_to_tick
from src.config import ContractSpec
from src.paper.events import MarketEvent
from src.paper.orders import Execution, LifecycleError, PaperOrder, PaperOrderState as St


@dataclass
class Book:
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last: float | None = None
    ts: pd.Timestamp | None = None


@dataclass(frozen=True)
class FillDecision:
    approved: bool
    reason: str = ""
    quantity: int | None = None   # possibly reduced quantity


FillAuthorizer = Callable[[PaperOrder, float, int, MarketEvent], FillDecision]


class PaperBroker:
    """Simulated order book per instrument. It never transmits anything anywhere."""

    TRANSMITS_REAL_ORDERS = False

    def __init__(self, bt: BacktestConfig, specs: dict[str, ContractSpec], *, participation: float = 1.0,
                 quote_max_age: pd.Timedelta = pd.Timedelta(seconds=5),
                 entry_token_check: Callable[[PaperOrder], bool] | None = None) -> None:
        if not 0 < participation <= 1:
            raise ValueError("participation must be in (0, 1]")
        self.quote_max_age = quote_max_age
        # entries are accepted ONLY with an authorization token issued by the EntryAuthorizer
        self.entry_token_check = entry_token_check or (lambda o: False)
        self.bt = bt
        self.specs = specs
        self.participation = participation
        self.orders: dict[str, PaperOrder] = {}
        self._entry_terms: dict[str, tuple] = {}  # terms of each accepted entry, re-verified before every fill
        self.books: dict[str, Book] = {r: Book() for r in specs}
        self.seq = 0            # sequence number of the event being processed
        self._next = 1
        self._exec_next = 1
        self.log: list[dict[str, Any]] = []   # order lifecycle events (journaled by the runtime)

    # ------------------------------------------------------------------ ids / logging
    def new_id(self, prefix: str = "P") -> str:
        oid = f"{prefix}{self._next:07d}"
        self._next += 1
        return oid

    def _log(self, kind: str, order: PaperOrder, when: pd.Timestamp, **kw: Any) -> None:
        self.log.append({"kind": kind, "order_id": order.order_id, "root": order.root, "role": order.role,
                         "time": when.isoformat(), "state": order.state.value, **kw})

    # ------------------------------------------------------------------ order entry
    def submit(self, order: PaperOrder, when: pd.Timestamp) -> PaperOrder:
        if order.order_id in self.orders:
            raise LifecycleError(f"duplicate order id {order.order_id}")
        if order.quantity < 1:
            raise ValueError("order quantity must be >= 1")
        if order.active_after_seq < self.seq:
            raise LifecycleError(f"order {order.order_id} would be active for an already processed event; refused")
        if order.role == "entry" and not self.entry_token_check(order):
            raise LifecycleError(f"entry {order.order_id} was not issued by the EntryAuthorizer; refused")
        spec = self.specs[order.root]
        tick = spec.tick_size
        for name in ("limit_price", "stop_price"):
            v = getattr(order, name)
            if v is not None and (not math.isfinite(v) or v <= 0 or abs(v / tick - round(v / tick)) > 1e-6):
                raise ValueError(f"{name} {v} is not a positive price on the tick grid")
        self.orders[order.order_id] = order
        if order.role == "entry":
            self._entry_terms[order.order_id] = (self._terms(order), order.quantity)
        self._log("submitted", order, when, details=order.to_dict())
        return order

    @staticmethod
    def _terms(o: PaperOrder) -> tuple:
        return (o.root, o.is_buy, o.order_type, o.limit_price, o.stop_price, o.stop_loss, o.take_profit,
                o.risk_budget, o.session_date, o.max_stop_distance, o.min_stop_distance, o.min_reward_risk,
                o.active_after_seq, o.expire_at, o.created_at)

    def _entry_unchanged(self, o: PaperOrder) -> bool:
        """The entry still has exactly the terms it was accepted with (its quantity may only shrink)."""
        got = self._entry_terms.get(o.order_id)
        return got is not None and got[0] == self._terms(o) and o.quantity <= got[1]

    def cancel(self, order_id: str, when: pd.Timestamp, reason: str) -> bool:
        o = self.orders.get(order_id)
        if o is None or not o.is_working:
            return False
        o.transition(St.CANCELLED, when, reason)
        self._log("cancelled", o, when, reason=reason)
        return True

    def reject(self, order: PaperOrder, when: pd.Timestamp, reason: str) -> None:
        order.transition(St.REJECTED, when, reason)
        self._log("rejected", order, when, reason=reason)

    def _refuse(self, order: PaperOrder, when: pd.Timestamp, reason: str) -> None:
        """A fill-time refusal: an untouched entry is rejected; a partially filled one keeps its filled
        part (already protected by its bracket) and the remainder is cancelled -- never filled."""
        if order.filled_qty > 0:
            self.cancel(order.order_id, when, f"remainder_refused:{reason}")
        else:
            self.reject(order, when, reason)

    def working(self, root: str | None = None, roles: tuple[str, ...] | None = None) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.is_working and (root is None or o.root == root)
                and (roles is None or o.role in roles)]

    def expire(self, now: pd.Timestamp) -> None:
        for o in self.working():
            if o.expire_at is not None and now >= o.expire_at:
                o.transition(St.EXPIRED, now, "expired")
                self._log("expired", o, now)

    # ------------------------------------------------------------------ market events
    def on_event(self, ev: MarketEvent, seq: int, position_qty: Callable[[str], int],
                 authorize_entry: FillAuthorizer) -> list[Execution]:
        self.seq = seq
        b = self.books[ev.root]
        if ev.kind == "quote":
            b.bid, b.ask, b.bid_size, b.ask_size = ev.bid, ev.ask, ev.bid_size, ev.ask_size
            b.ts = ev.ts  # quote time: a quote older than quote_max_age is never used for a fill
        elif ev.kind == "trade":
            b.last = ev.last
        self.expire(ev.ts)
        execs: list[Execution] = []
        # protective / exit orders first, then entries
        order = {"stop_loss": 0, "exit": 0, "take_profit": 1, "entry": 2}
        pos = position_qty(ev.root)  # tracked locally: fills inside this event change it immediately
        for o in sorted(self.working(ev.root), key=lambda o: (order.get(o.role, 3), o.created_at, o.order_id)):
            if not o.is_working or seq <= o.active_after_seq:
                continue
            if o.role == "entry" and pos != 0 and o.filled_qty == 0:
                continue  # an entry only fills from flat (reversals exit first)
            if o.role == "take_profit" and self._bar_ambiguous(o, ev):
                continue  # stop and target both inside this synthetic bar: worst case, the stop first
            res = self._match(o, ev, b)
            if res is None:
                continue
            qty, ref, base, reason, quote_based = res
            if o.role == "entry":
                if not self._entry_unchanged(o):
                    self._refuse(o, ev.ts, "entry_terms_changed_after_authorization")
                    continue
                dec = authorize_entry(o, base, qty, ev)
                if not dec.approved:
                    self._refuse(o, ev.ts, dec.reason)
                    continue
                if dec.quantity is not None and dec.quantity < qty:
                    if dec.quantity < 1:
                        self._refuse(o, ev.ts, dec.reason or "fill_quantity_zero")
                        continue
                    o.quantity = o.filled_qty + dec.quantity
                    qty = dec.quantity
            if o.role in ("stop_loss", "take_profit", "exit"):
                closing_buy = pos < 0
                if pos == 0 or o.is_buy != closing_buy:
                    self.cancel(o.order_id, ev.ts, "position_already_flat")
                    continue
                qty = min(qty, abs(pos))  # never over-close (no accidental reversal)
            ex = self._fill(o, qty, ref, base, reason, quote_based, ev)
            pos += ex.quantity if ex.is_buy else -ex.quantity
            execs.append(ex)
        return execs

    def _bar_ambiguous(self, tp: PaperOrder, ev: MarketEvent) -> bool:
        if not ev.synthetic_from_bars or ev.bar_low is None or ev.bar_high is None:
            return False
        for s in self.working(tp.root, ("stop_loss",)):
            if s.is_buy == tp.is_buy and s.stop_price is not None:  # any stop protecting the same position
                if (not s.is_buy and ev.bar_low <= s.stop_price) or (s.is_buy and ev.bar_high >= s.stop_price):
                    return True
        return False

    def _fresh_quote(self, b: Book, ev: MarketEvent, buy: bool) -> float | None:
        px = b.ask if buy else b.bid
        if px is None or b.ts is None or ev.ts - b.ts > self.quote_max_age:
            return None
        return px

    def _match(self, o: PaperOrder, ev: MarketEvent, b: Book):
        """(qty, requested_price, base_fill_price, reason, quote_based) or None."""
        buy = o.is_buy
        mkt = self._fresh_quote(b, ev, buy)  # None when no quote or the quote is stale
        trade = ev.last if ev.kind == "trade" else None
        if o.order_type == "market":
            if mkt is not None and ev.kind == "quote":
                return o.remaining, None, mkt, "market_at_quote", True
            if mkt is not None:
                return o.remaining, None, mkt, "market_at_last_quote", True
            if trade is not None:
                return o.remaining, None, trade, "market_at_trade_no_quote", False
            return None
        if o.order_type == "stop":
            s = o.stop_price
            if o.state is not St.TRIGGERED:
                hit = (trade is not None and (trade >= s if buy else trade <= s)) or \
                      (ev.kind == "quote" and mkt is not None and (mkt >= s if buy else mkt <= s))
                if not hit:
                    return None
                o.transition(St.TRIGGERED, ev.ts, "stop_triggered")
                self._log("triggered", o, ev.ts)
            px = mkt if mkt is not None and ev.kind == "quote" else (trade if trade is not None else mkt)
            if px is None:
                return None
            base = max(s, px) if buy else min(s, px)
            return o.remaining, s, base, "stop_gap_through" if base != s else "stop_triggered", ev.kind == "quote"
        # limit
        lim = o.limit_price
        if not o.posted:
            o.posted = True
            if mkt is not None and (mkt <= lim if buy else mkt >= lim):  # marketable on arrival: taker
                size = b.ask_size if buy else b.bid_size
                qty = o.remaining if size is None else min(o.remaining, int(size))
                if qty >= 1:
                    return qty, lim, mkt, "limit_marketable_at_quote", True
        through_trade = trade is not None and (trade < lim if buy else trade > lim)
        through_quote = ev.kind == "quote" and mkt is not None and (mkt < lim if buy else mkt > lim)
        if not (through_trade or through_quote):
            return None  # a touch is not a fill
        if through_trade:
            avail = int(math.floor(ev.size * self.participation + 1e-9))
        else:
            size = b.ask_size if buy else b.bid_size
            avail = o.remaining if size is None else int(math.floor(size * self.participation + 1e-9))
        qty = min(o.remaining, avail)
        if qty < 1:
            return None
        return qty, lim, lim, "limit_trade_through" if through_trade else "limit_quote_through", not through_trade

    def _fill(self, o: PaperOrder, qty: int, requested: float | None, base: float, reason: str,
              quote_based: bool, ev: MarketEvent) -> Execution:
        spec = self.specs[o.root]
        ticks = {"market": self.bt.slippage.market_ticks, "stop": self.bt.slippage.stop_ticks,
                 "limit": self.bt.slippage.limit_ticks}[o.order_type]
        price = round_to_tick(apply_slippage(base, o.is_buy, ticks, spec.tick_size), spec.tick_size)
        commission = self.bt.cost_for(o.root).commission_per_contract_per_side * qty
        slip_cost = abs(price - base) * qty * spec.point_value
        prev = o.filled_qty
        o.avg_fill_price = price if prev == 0 else (o.avg_fill_price * prev + price * qty) / (prev + qty)
        o.filled_qty += qty
        o.transition(St.FILLED if o.remaining == 0 else St.PARTIALLY_FILLED, ev.ts, reason)
        ex = Execution(f"X{self._exec_next:08d}", o.order_id, o.root, o.role, o.order_type, o.is_buy, qty, requested,
                       base, price, ticks, slip_cost, commission, ev.ts, ev.event_id, reason, quote_based,
                       ev.synthetic_from_bars)
        self._exec_next += 1
        self._log("filled", o, ev.ts, execution=ex.to_dict())
        return ex

    # ------------------------------------------------------------------ brackets
    def on_execution(self, ex: Execution, position_qty: int, when: pd.Timestamp) -> None:
        """Maintain bracket (OCO) orders after an execution has been booked."""
        o = self.orders[ex.order_id]
        if o.role == "entry":
            group = f"OCO-{o.order_id}"
            kids = [k for k in self.working(o.root) if k.parent_id == o.order_id]
            if kids:
                for k in kids:  # partial entry fill: protect the extra contracts
                    k.quantity += ex.quantity
                    self._log("resized", k, when, quantity=k.quantity)
                return
            for role, otype, px in (("stop_loss", "stop", o.stop_loss), ("take_profit", "limit", o.take_profit)):
                if px is None:
                    continue
                kid = PaperOrder(self.new_id("B"), o.root, role, otype, not o.is_buy, ex.quantity, when, self.seq,
                                 limit_price=px if otype == "limit" else None, stop_price=px if otype == "stop" else None,
                                 parent_id=o.order_id, oco_group=group, client_id=None, tag=role,
                                 session_date=o.session_date)
                kid.posted = True  # resting from the start: a take-profit never "takes" on arrival
                self.submit(kid, when)
            return
        # exit-side execution
        if position_qty == 0:
            for k in self.working(o.root, ("stop_loss", "take_profit", "exit")):
                self.cancel(k.order_id, when, "position_closed")
        elif o.oco_group:
            for k in self.working(o.root):
                if k.oco_group == o.oco_group and k.order_id != o.order_id:
                    k.quantity = k.filled_qty + abs(position_qty)  # sibling protects only what is left
                    self._log("resized", k, when, quantity=k.quantity)

    def state(self) -> dict[str, Any]:
        return {"orders": {k: v.to_dict() for k, v in self.orders.items()}, "next": self._next,
                "exec_next": self._exec_next,
                "books": {r: {"bid": b.bid, "ask": b.ask, "last": b.last,
                              "ts": b.ts.isoformat() if b.ts is not None else None} for r, b in self.books.items()}}

    def restore(self, st: dict[str, Any]) -> None:
        self.orders = {k: PaperOrder.from_dict(v) for k, v in st["orders"].items()}
        self._next, self._exec_next = int(st["next"]), int(st["exec_next"])
        # books are NOT restored as live prices: a restart waits for fresh market data
