"""Sprint 5: paper execution engine -- fill rules, realism, brackets/OCO, partial fills,
lifecycle, rejection (never converted into a fill)."""

import pandas as pd
import pytest

from src.paper.broker import FillDecision, PaperBroker
from src.paper.orders import LifecycleError, PaperOrder, PaperOrderState as St
from tests.paper_helpers import APP, BT, T, quote, trade

SPECS = {"MES": APP.contracts["MES"]}
OK = lambda o, base, qty, ev: FillDecision(True)  # noqa: E731
# Unit tests of fill mechanics stand in for the EntryAuthorizer's token (the default broker refuses
# every entry without one -- see tests/test_paper_audit.py).
UNIT_TOKEN = lambda o: True  # noqa: E731
t0 = T("10:00")


class Pos:
    def __init__(self):
        self.q = 0

    def __call__(self, root):
        return self.q


def mk(broker, role="entry", otype="market", buy=True, qty=1, seq=0, **kw):
    o = PaperOrder(broker.new_id(), "MES", role, otype, buy, qty, t0, seq, **kw)
    return broker.submit(o, t0)


def run(broker, events, pos=None, auth=OK, start_seq=1):
    pos = pos or Pos()
    out = []
    for i, ev in enumerate(events):
        for ex in broker.on_event(ev, start_seq + i, pos, auth):
            out.append(ex)
            pos.q += ex.quantity if ex.is_buy else -ex.quantity
            broker.on_execution(ex, pos.q, ev.ts)
    return out, pos


def at(s):
    return t0 + pd.Timedelta(seconds=s)


def test_market_fills_at_ask_bid_plus_slippage_never_on_the_decision_event():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    o = mk(b, seq=1)  # created while processing event seq 1
    ex, _ = run(b, [quote(at(0), 4999.75, 5000.0)], start_seq=1)
    assert ex == []                                   # same event: no fill
    ex, _ = run(b, [quote(at(1), 4999.75, 5000.0)], start_seq=2)
    e = ex[0]
    tick = SPECS["MES"].tick_size
    assert e.reference_price == 5000.0 and e.fill_price == 5000.0 + BT.slippage.market_ticks * tick
    assert e.slippage_cost == pytest.approx(BT.slippage.market_ticks * tick * SPECS["MES"].point_value)
    assert e.commission == BT.cost_for("MES").commission_per_contract_per_side and e.quote_based
    assert o.state is St.FILLED


def test_market_without_quote_uses_trade_and_is_flagged():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    mk(b, buy=False)
    ex, _ = run(b, [trade(at(1), 5000.0)], pos=Pos())
    assert ex[0].reference_price == 5000.0 and not ex[0].quote_based and ex[0].reason == "market_at_trade_no_quote"


def test_limit_touch_does_not_fill_trade_through_does():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    o = mk(b, otype="limit", limit_price=4998.0)
    ex, _ = run(b, [quote(at(1), 4999.75, 5000.0), trade(at(2), 4998.0), quote(at(3), 4997.75, 4998.0)])
    assert ex == [] and o.state is St.ACCEPTED       # touches only
    ex, _ = run(b, [trade(at(4), 4997.75, size=3)], start_seq=10)
    assert ex[0].fill_price == 4998.0 and ex[0].reason == "limit_trade_through" and o.state is St.FILLED


def test_marketable_limit_takes_the_ask():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    mk(b, otype="limit", limit_price=5001.0)
    ex, _ = run(b, [quote(at(1), 4999.75, 5000.0)])
    assert ex[0].fill_price == 5000.0 and ex[0].reason == "limit_marketable_at_quote"


def test_partial_fills_limited_by_trade_size_and_participation():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN, participation=0.5)
    o = mk(b, otype="limit", qty=5, limit_price=4998.0)
    ex, _ = run(b, [quote(at(1), 4999.75, 5000.0), trade(at(2), 4997.75, size=4), trade(at(3), 4997.75, size=1)])
    assert [e.quantity for e in ex] == [2] and o.state is St.PARTIALLY_FILLED and o.filled_qty == 2
    ex2, _ = run(b, [trade(at(4), 4997.5, size=10)], start_seq=20)
    assert [e.quantity for e in ex2] == [3] and o.state is St.FILLED
    assert o.avg_fill_price == 4998.0


def test_stop_triggers_and_gaps_fill_worse():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    o = mk(b, otype="stop", limit_price=None, stop_price=5002.0)
    ex, _ = run(b, [trade(at(1), 5001.75)])
    assert ex == [] and o.state is St.ACCEPTED
    ex, _ = run(b, [trade(at(2), 5004.0)], start_seq=5)       # gapped through
    tick = SPECS["MES"].tick_size
    assert ex[0].reference_price == 5004.0 and ex[0].fill_price == 5004.0 + BT.slippage.stop_ticks * tick
    assert ex[0].reason == "stop_gap_through"
    b2 = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    mk(b2, otype="stop", buy=False, stop_price=4995.0)
    ex, _ = run(b2, [quote(at(1), 4995.0, 4995.25)])         # bid reaches the sell stop
    assert ex[0].reference_price == 4995.0 and ex[0].fill_price == 4995.0 - BT.slippage.stop_ticks * tick


def test_bracket_oco_and_partial_exit_resizing():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN, participation=1.0)
    entry = mk(b, qty=2, stop_loss=4990.0, take_profit=5010.0)
    ex, pos = run(b, [quote(at(1), 4999.75, 5000.0)])
    kids = {k.role: k for k in b.working("MES")}
    assert set(kids) == {"stop_loss", "take_profit"} and all(k.quantity == 2 for k in kids.values())
    assert kids["stop_loss"].oco_group == kids["take_profit"].oco_group
    # take-profit partially filled (1 of 2) -> stop resized to the remaining 1
    ex, pos = run(b, [trade(at(2), 5010.25, size=1)], pos=pos, start_seq=10)
    assert ex[0].quantity == 1 and kids["stop_loss"].quantity == 1 and pos.q == 1
    ex, pos = run(b, [trade(at(3), 4989.75, size=5)], pos=pos, start_seq=20)  # stop hit -> flat, TP cancelled
    assert pos.q == 0 and kids["stop_loss"].state is St.FILLED and kids["take_profit"].state is St.CANCELLED
    assert entry.state is St.FILLED


def test_entry_partial_fill_extends_protection():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    mk(b, otype="limit", qty=3, limit_price=4998.0, stop_loss=4990.0)
    ex, pos = run(b, [quote(at(1), 4999.75, 5000.0), trade(at(2), 4997.75, size=1)])
    sl = b.working("MES", ("stop_loss",))[0]
    assert sl.quantity == 1
    ex, pos = run(b, [trade(at(3), 4997.75, size=5)], pos=pos, start_seq=10)
    assert sl.quantity == 3 and pos.q == 3


def test_rejected_entry_is_never_filled():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    o = mk(b)
    ex, pos = run(b, [quote(at(1), 4999.75, 5000.0)], auth=lambda *a: FillDecision(False, "news_blackout_active"))
    assert ex == [] and o.state is St.REJECTED and o.reason == "news_blackout_active" and pos.q == 0
    ex, _ = run(b, [quote(at(2), 4999.75, 5000.0)], start_seq=5)
    assert ex == []


def test_fill_authorizer_can_reduce_quantity():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    o = mk(b, qty=4)
    ex, _ = run(b, [quote(at(1), 4999.75, 5000.0)], auth=lambda *a: FillDecision(True, "", 2))
    assert ex[0].quantity == 2 and o.quantity == 2 and o.state is St.FILLED


def test_cancel_expire_and_lifecycle_rules():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    o = mk(b, otype="limit", limit_price=4990.0, expire_at=at(5))
    run(b, [trade(at(6), 5000.0)])
    assert o.state is St.EXPIRED and not b.cancel(o.order_id, at(7), "late")
    o2 = mk(b, otype="limit", limit_price=4990.0)
    assert b.cancel(o2.order_id, at(8), "user") and o2.state is St.CANCELLED
    with pytest.raises(LifecycleError):
        o2.transition(St.FILLED, at(9))
    with pytest.raises(ValueError, match="tick grid"):
        mk(b, otype="limit", limit_price=4990.1)
    with pytest.raises(LifecycleError, match="duplicate"):
        b.submit(o2, at(10))


def test_exit_never_over_closes():
    b = PaperBroker(BT, SPECS, entry_token_check=UNIT_TOKEN)
    mk(b, role="exit", qty=5, buy=False)
    pos = Pos()
    pos.q = 2
    ex, _ = run(b, [quote(at(1), 4999.75, 5000.0)], pos=pos)
    assert ex[0].quantity == 2 and pos.q == 0


def test_broker_cannot_transmit():
    import inspect

    import src.paper.broker as m

    assert PaperBroker.TRANSMITS_REAL_ORDERS is False
    src = inspect.getsource(m)
    for word in ("socket", "requests", "urllib", "http.client", "ninja", "websocket"):
        assert word not in src.lower()
