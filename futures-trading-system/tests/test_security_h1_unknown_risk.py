"""H1: unknown, missing or invalid risk can never pass the AccountGuard."""

import math
from dataclasses import replace

import pytest

from src.backtest.config import SizingConfig
from src.backtest.engine import BacktestEngine, _Simulation
from src.backtest.orders import OrderRequest
from src.config import ConfigError, ContractSpec
from src.risk.account import AccountGuard, AccountRules
from tests.backtest_helpers import GLOBEX, MES, frame, make_config, run

ROWS = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000), ("10:02", 5000, 5001, 4999, 5000)]
SHORT_ROWS = [(t, 10000 - o, 10000 - l, 10000 - h, 10000 - c) for t, o, h, l, c in ROWS]
RULES = AccountRules(enabled=True, profile="t", drawdown_type="static", max_drawdown=1000.0, safety_buffer=100.0,
                     flatten_buffer=50.0)
SIZINGS = [SizingConfig("fixed", contracts=1), SizingConfig("risk", risk_per_trade=10_000.0)]


def cfg(rules=RULES, **kw):
    return replace(make_config(**kw), account_rules=rules)


def assert_nothing_opened(res, reason, stage="signal"):
    assert res.trades.empty and res.fills.empty
    assert (res.orders.empty or not (res.orders.status == "filled").any())
    rej = res.rejections.iloc[0]
    assert (rej.reason, rej.stage) == (reason, stage), res.rejections


@pytest.mark.parametrize("sizing", SIZINGS, ids=["fixed", "risk"])
@pytest.mark.parametrize("side,rows", [("long", ROWS), ("short", SHORT_ROWS)])
@pytest.mark.parametrize("qty", [None, 1, 50])
def test_missing_stop_rejected_for_every_sizing_side_and_quantity(sizing, side, rows, qty):
    res, _ = run(rows, {"10:00": [OrderRequest.enter(side, quantity=qty)]}, config=cfg(sizing=sizing))
    assert_nothing_opened(res, "account_requires_stop_loss")


@pytest.mark.parametrize("field,value", [("stop_loss", math.nan), ("stop_loss", math.inf), ("stop_loss", -math.inf),
                                         ("stop_loss", 0.0), ("stop_loss", -4990.0), ("take_profit", math.nan),
                                         ("take_profit", math.inf), ("price", math.nan)])
def test_non_finite_or_invalid_prices_rejected(field, value):
    kw = {"stop_loss": 4990.0, field: value}
    order_type = "stop" if field == "price" else "market"
    res, _ = run(ROWS, {"10:00": [OrderRequest.enter("long", order_type, **kw)]}, config=cfg())
    assert_nothing_opened(res, "invalid_order_price")
    # Same validation without the guard (no NaN can reach the rounding/fill code).
    res, _ = run(ROWS, {"10:00": [OrderRequest.enter("long", order_type, **kw)]})
    assert_nothing_opened(res, "invalid_order_price")


@pytest.mark.parametrize("qty", [0, -1, 2.5, True, math.nan])
def test_invalid_quantity_rejected(qty):
    res, _ = run(ROWS, {"10:00": [OrderRequest.enter("long", stop_loss=4990, quantity=qty)]}, config=cfg())
    assert_nothing_opened(res, "invalid_quantity")


@pytest.mark.parametrize("field,value", [("point_value", math.nan), ("point_value", 0.0), ("point_value", -5.0),
                                         ("point_value", math.inf), ("tick_size", 0.0), ("tick_size", math.nan)])
def test_invalid_contract_multiplier_rejected_at_startup(field, value):
    spec = replace(MES, **{field: value})
    with pytest.raises(ConfigError, match=field):
        BacktestEngine(cfg(), spec, GLOBEX)


@pytest.mark.parametrize("risk", [None, math.nan, math.inf, -math.inf, 0.0, -5.0, "100"])
def test_guard_rejects_unknown_risk_values(risk):
    g = AccountGuard(RULES, 10_000)
    assert g.fill_block_reason(10_000, None, risk) == "account_risk_unknown"
    if risk is not None:
        assert g.entry_block_reason(10_000, None, trade_risk=risk) == "account_risk_unknown"
    assert g.entry_block_reason(math.nan, None) == "account_equity_unknown"


def test_no_engine_path_bypasses_the_risk_check(monkeypatch):
    # Even if the risk calculation itself failed, the fill-time guard refuses the entry.
    monkeypatch.setattr(_Simulation, "_actual_risk_dollars", lambda self, order, price: math.nan)
    res, _ = run(ROWS, {"10:00": [OrderRequest.enter("long", stop_loss=4990)]}, config=cfg())
    assert_nothing_opened(res, "account_risk_unknown", stage="fill")
    chk = res.entry_checks.iloc[0]
    assert chk.outcome == "rejected" and math.isnan(chk.account_risk_dollars)


@pytest.mark.parametrize("side,rows,stop", [("long", ROWS, 4990.0), ("short", SHORT_ROWS, 10000 - 4990.0)])
def test_multi_contract_risk_with_gap_reserve(side, rows, stop):
    # 3 contracts, 10.25-pt stop: 3 x (10.25 x $5 + $2 + 1.25) = 163.50; gap reserve 8 ticks x $1.25 x 3 = 30.
    res, _ = run(rows, {"10:00": [OrderRequest.enter(side, stop_loss=stop, quantity=3)]},
                 config=cfg(replace(RULES, gap_reserve_ticks=8.0)))
    chk = res.entry_checks.iloc[0]
    assert chk.outcome == "accepted"
    assert chk.actual_risk_dollars == pytest.approx(163.5) and chk.account_risk_dollars == pytest.approx(193.5)


def test_gap_reserve_can_block_an_entry():
    # Buffer 1000, safety 900: 54.50 risk passes, + 8-tick reserve (10.00) still passes; 100-tick reserve does not.
    tight = replace(RULES, safety_buffer=900.0)
    ok, _ = run(ROWS, {"10:00": [OrderRequest.enter("long", stop_loss=4990)]}, config=cfg(replace(tight, gap_reserve_ticks=8)))
    assert len(ok.trades) == 1
    res, _ = run(ROWS, {"10:00": [OrderRequest.enter("long", stop_loss=4990)]},
                 config=cfg(replace(tight, gap_reserve_ticks=100)))
    assert_nothing_opened(res, "account_trade_risk_exceeds_buffer", stage="fill")


def test_missing_risk_near_safety_buffer_is_refused():
    near = replace(RULES, max_drawdown=1000.0, safety_buffer=990.0, flatten_buffer=50.0)
    res, _ = run(ROWS, {"10:00": [OrderRequest.enter("long", quantity=1)]}, config=cfg(near))
    assert_nothing_opened(res, "account_requires_stop_loss")


def test_guard_disabled_keeps_sprint2_behaviour():
    res, _ = run(ROWS, {"10:00": [OrderRequest.enter("long")]})  # no stop, guard disabled
    assert len(res.trades) == 1
