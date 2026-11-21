"""Sprint 5: paper account accounting (existing AccountGuard/RiskManager inside)."""

import math

import pandas as pd
import pytest

from src.paper.account import AccountStateError, PaperAccount
from src.paper.orders import Execution
from tests.paper_helpers import APP, T, bt, guard_rules

SPECS = {r: APP.contracts[r] for r in ("MES", "MNQ")}
SD = pd.Timestamp("2025-03-10")


def ex(i, role, buy, qty, px, root="MES", comm=1.0, slip=1.25):
    return Execution(f"X{i}", f"O{i}", root, role, "market", buy, qty, None, px, px, 1, slip, comm,
                     T("10:00") + pd.Timedelta(minutes=i), f"e{i}", "test", True, False)


def test_round_trip_pnl_costs_and_r_multiple():
    a = PaperAccount(bt(), SPECS, APP.session)
    a.apply(ex(1, "entry", True, 2, 5000.0), SD, {"stop_loss": 4990.0, "signal_id": "s"})
    a.mark("MES", 5004.0)
    assert a.unrealized() == pytest.approx(4 * 2 * 5.0) and a.equity() == pytest.approx(10_000 + 40 - 1)
    rec = a.apply(ex(2, "take_profit", False, 2, 5010.0), SD)
    assert rec["gross_pnl"] == pytest.approx(10 * 2 * 5.0) and rec["commission"] == 2.0
    assert rec["net_pnl"] == pytest.approx(98.0) and rec["r_multiple"] == pytest.approx(98.0 / 100.0)
    assert rec["slippage_cost"] == pytest.approx(2.5) and a.positions["MES"].is_flat
    assert a.net_realized == pytest.approx(98.0) and a.daily_net["2025-03-10"] == pytest.approx(98.0)
    assert a.risk.daily_trades(SD) == 1 and a.risk.daily_realized_pnl(SD) == pytest.approx(98.0)


def test_drawdown_high_water_and_unknown_marks():
    a = PaperAccount(bt(), SPECS, APP.session)
    a.apply(ex(1, "entry", False, 1, 20000.0, root="MNQ"), SD, {"stop_loss": 20010.0})
    assert math.isnan(a.equity()) or "MNQ" in a.marks  # first fill marks at the fill price
    a.mark("MNQ", 19990.0)
    a.update_drawdown()
    hw = a.high_water
    a.mark("MNQ", 20030.0)
    a.update_drawdown()
    assert a.high_water == hw and a.max_drawdown == pytest.approx(hw - a.equity())
    b = PaperAccount(bt(), SPECS, APP.session)
    b.positions["MES"].apply_fill(True, 1, 5000.0)
    assert math.isnan(b.unrealized())  # never assume a price that was not seen


def test_double_booking_and_orphan_exit_are_refused():
    a = PaperAccount(bt(), SPECS, APP.session)
    e = ex(1, "entry", True, 1, 5000.0)
    a.apply(e, SD, {})
    with pytest.raises(AccountStateError, match="twice"):
        a.apply(e, SD, {})
    with pytest.raises(AccountStateError, match="without an open trade"):
        a.apply(ex(5, "exit", True, 1, 20000.0, root="MNQ"), SD)
    with pytest.raises(AccountStateError):
        a.mark("MES", float("nan"))


def test_state_round_trip_keeps_guard_and_risk_state():
    a = PaperAccount(bt(account_rules=guard_rules(drawdown_type="trailing_eod")), SPECS, APP.session)
    a.apply(ex(1, "entry", True, 1, 5000.0), SD, {"stop_loss": 4990.0})
    a.apply(ex(2, "exit", False, 1, 4980.0), SD)
    a.guard.peak = 10_500.0
    a.guard.locked_session = SD
    st = a.state()
    b = PaperAccount(bt(account_rules=guard_rules(drawdown_type="trailing_eod")), SPECS, APP.session)
    b.restore(st)
    assert b.guard.peak == 10_500.0 and b.guard.is_locked(SD) and b.guard.floor == a.guard.floor
    assert b.realized_gross == a.realized_gross and b.risk.consecutive_losses(SD) == 1
    assert b.marks == {}  # stored prices are never treated as live
    st["initial_balance"] = 5.0
    with pytest.raises(AccountStateError):
        PaperAccount(bt(), SPECS, APP.session).restore(st)
