"""Regression tests for the Sprint 2 review corrections.

Defaults (make_config): $1.00 commission per contract per side; slippage 1 tick on
market and stop fills, 0 on limits. MES: tick 0.25 = $1.25, $5 per point.
"""

import math

import pandas as pd
import pytest

from src.backtest.config import SizingConfig
from src.backtest.metrics import compute_metrics
from src.backtest.orders import OrderRequest
from src.config import ConfigError
from src.risk.manager import RiskConfig, RiskManager
from tests.backtest_helpers import GLOBEX, frame, make_config, run, ts

QUIET = ("10:00", 5000, 5000.5, 4999.5, 5000)


def bars(*rows):
    return [QUIET, *rows, ("10:05", 5000, 5000.5, 4999.5, 5000)]


# =============================================================== 1. sizing at the fill price

RISK = SizingConfig("risk", risk_per_trade=200.0, include_costs_in_risk=True)
LONG_4990 = OrderRequest.enter("long", stop_loss=4990)


def _risk_run(rows, req=LONG_4990, policy="reduce"):
    from dataclasses import replace

    cfg = make_config(sizing=replace(RISK, fill_risk_policy=policy))
    return run(rows, {"10:00": [req]}, config=cfg)[0]


def test_signal_time_size_unchanged_without_gap():
    # Signal estimate: 10 pts * $5 + $2 commission + (1 + 1) ticks * $1.25 = 54.5 -> 3 contracts.
    # Fill 5000.25: 10.25 * 5 + 2 + 1 * 1.25 (stop exit) = 54.5 -> 3 * 54.5 = 163.5 <= 200.
    res = _risk_run(bars(("10:01", 5000, 5000.5, 4999.5, 5000)))
    assert res.trades.iloc[0].quantity == 3 and res.adjustments.empty


def test_adverse_gap_reduces_quantity_to_budget():
    # Open 5010 -> fill 5010.25: 20.25 * 5 + 2 + 1.25 = 104.5 per contract; 3 would risk 313.5.
    res = _risk_run(bars(("10:01", 5010, 5010.5, 5009.5, 5010)))
    t = res.trades.iloc[0]
    assert t.quantity == 1 and t.entry_price == 5010.25
    adj = res.adjustments.iloc[0]
    assert (adj.from_quantity, adj.to_quantity) == (3, 1)
    assert adj.risk_per_contract == pytest.approx(104.5)
    assert t.quantity * adj.risk_per_contract <= 200


def test_adverse_gap_rejected_with_reject_policy():
    res = _risk_run(bars(("10:01", 5010, 5010.5, 5009.5, 5010)), policy="reject")
    assert res.trades.empty and res.fills.empty
    rej = res.rejections.iloc[0]
    assert (rej.reason, rej.stage) == ("risk_budget_exceeded_at_fill", "fill")
    assert "313.50 > budget 200.00" in rej.detail


def test_adverse_gap_too_large_for_one_contract_rejected():
    # Open 5050 -> 60.25 * 5 + 3.25 = 304.5 > 200 even for one contract.
    res = _risk_run(bars(("10:01", 5050, 5050.5, 5049.5, 5050)))
    assert res.trades.empty and list(res.rejections.reason) == ["risk_budget_exceeded_at_fill"]


def test_favorable_gap_keeps_quantity_never_increases():
    # Open 4995 -> fill 4995.25: 5.25 * 5 + 3.25 = 29.5 per contract (budget would allow 6).
    res = _risk_run(bars(("10:01", 4995, 4995.5, 4994.5, 4995)))
    assert res.trades.iloc[0].quantity == 3 and res.adjustments.empty


def test_stop_entry_gap_resized():
    req = OrderRequest.enter("long", "stop", price=5005, stop_loss=4990)
    # Signal: 15 * 5 + 2 + (1 stop-entry + 1 stop-exit) * 1.25 = 79.5 -> 2 contracts.
    res = _risk_run(bars(("10:01", 5000, 5006, 4999.5, 5005.5)), req)
    assert res.trades.iloc[0].quantity == 2  # intrabar fill 5005.25 -> 79.5 * 2 = 159
    # Gap open 5020 -> fill 5020.25: 30.25 * 5 + 3.25 = 154.5; 2 would risk 309 -> 1.
    res = _risk_run(bars(("10:01", 5020, 5021, 5019.5, 5020)), req)
    assert res.trades.iloc[0].quantity == 1 and res.adjustments.iloc[0].to_quantity == 1


def test_limit_entry_better_fill_keeps_quantity():
    req = OrderRequest.enter("long", "limit", price=4995, stop_loss=4985)
    # Signal: 10 * 5 + 2 + (0 + 1) * 1.25 = 53.25 -> 3. Opens through at 4992 -> 7 pts: 38.25.
    res = _risk_run(bars(("10:01", 4992, 4993, 4991.5, 4992.5)), req)
    t = res.trades.iloc[0]
    assert t.quantity == 3 and t.entry_price == 4992 and res.adjustments.empty


def test_actual_risk_never_exceeds_budget():
    for open_ in (4995, 5000, 5003, 5010, 5020, 5030):
        res = _risk_run(bars(("10:01", open_, open_ + 0.5, open_ - 0.5, open_)))
        for t in res.trades.itertuples():
            actual = (t.entry_price - 4990) * 5 * t.quantity + t.quantity * (2 + 1.25)
            assert actual <= 200 + 1e-9


def test_explicit_quantity_is_not_resized():
    req = OrderRequest.enter("long", stop_loss=4990, quantity=2)
    res = _risk_run(bars(("10:01", 5010, 5010.5, 5009.5, 5010)), req)
    assert res.trades.iloc[0].quantity == 2 and res.adjustments.empty


# =============================================================== 3. brackets at the actual fill

@pytest.mark.parametrize(
    "req,bar,why",
    [
        # long, adverse gap below the stop: fill 4985.25 <= stop 4990
        (OrderRequest.enter("long", stop_loss=4990, take_profit=5010), ("10:01", 4985, 4986, 4984, 4985), "stop_loss"),
        # long, favorable gap above the target: fill 5015.25 >= target 5010
        (OrderRequest.enter("long", stop_loss=4990, take_profit=5010), ("10:01", 5015, 5016, 5014, 5015), "take_profit"),
        # long, fill exactly on the stop: open 4989.75 + 1 tick = 4990
        (OrderRequest.enter("long", stop_loss=4990), ("10:01", 4989.75, 4990, 4989.5, 4990), "stop_loss"),
        # short, adverse gap above the stop: fill 5014.75 >= stop 5010
        (OrderRequest.enter("short", stop_loss=5010, take_profit=4990), ("10:01", 5015, 5016, 5014, 5015), "stop_loss"),
        # short, favorable gap below the target: fill 4984.75 <= target 4990
        (OrderRequest.enter("short", stop_loss=5010, take_profit=4990), ("10:01", 4985, 4986, 4984, 4985), "take_profit"),
        # long stop entry gapping above the target
        (OrderRequest.enter("long", "stop", price=5005, stop_loss=4998, take_profit=5010),
         ("10:01", 5012, 5013, 5011, 5012), "take_profit"),
        # short stop entry gapping below the target
        (OrderRequest.enter("short", "stop", price=4995, stop_loss=5002, take_profit=4990),
         ("10:01", 4988, 4989, 4987, 4988), "take_profit"),
    ],
)
def test_invalid_bracket_at_fill_rejects_entry(req, bar, why):
    res, _ = run(bars(bar), {"10:00": [req]})
    assert res.trades.empty and res.fills.empty
    rej = res.rejections.iloc[0]
    assert (rej.reason, rej.stage, rej.time) == ("invalid_bracket_at_fill", "fill", ts("10:01"))
    assert rej.detail.startswith(why)
    assert set(res.orders.role) == {"entry"} and res.orders.iloc[0].status == "rejected"  # no bracket created


@pytest.mark.parametrize(
    "req,bar",
    [
        (OrderRequest.enter("long", stop_loss=4990, take_profit=5010), ("10:01", 4995, 4996, 4994, 4995)),  # gap down
        (OrderRequest.enter("long", stop_loss=4990, take_profit=5010), ("10:01", 5005, 5006, 5004, 5005)),  # gap up
        (OrderRequest.enter("short", stop_loss=5010, take_profit=4990), ("10:01", 5005, 5006, 5004, 5005)),
        (OrderRequest.enter("short", stop_loss=5010, take_profit=4990), ("10:01", 4995, 4996, 4994, 4995)),
    ],
)
def test_gaps_that_keep_bracket_valid_still_open(req, bar):
    res, _ = run(bars(bar), {"10:00": [req]})
    assert len(res.trades) == 1 and res.rejections.empty
    assert {"stop_loss", "take_profit"} <= set(res.orders.role)


# =============================================================== 2. daily loss incl. unrealized PnL

def dl_config(action="flatten", basis="realized_and_unrealized", limit=30.0):
    return make_config(risk=RiskConfig(max_daily_loss=limit, daily_loss_basis=basis, daily_loss_action=action))


def test_trigger_price_hand_calculated():
    r = RiskManager(RiskConfig(max_daily_loss=30, daily_loss_basis="realized_and_unrealized"), GLOBEX)
    d = pd.Timestamp("2025-03-10")
    kw = dict(session_date=d, paid_costs=1.0, point_value=5.0, tick=0.25)
    # long 1 @ 5000.25: 5000.25 + (-30 + 1) / 5 = 4994.45 -> rounded up to 4994.5
    assert r.daily_loss_trigger_price(is_long=True, quantity=1, avg_price=5000.25, **kw) == 4994.5
    # long 2: 5000.25 - 29 / 10 = 4997.35 -> 4997.5
    assert r.daily_loss_trigger_price(is_long=True, quantity=2, avg_price=5000.25, **kw) == 4997.5
    # short 1 @ 5000: 5000 + 29 / 5 = 5005.8 -> rounded down to 5005.75
    assert r.daily_loss_trigger_price(is_long=False, quantity=1, avg_price=5000, **kw) == 5005.75
    r.on_trade_closed(-20.0, d)  # an earlier realized loss tightens the level
    assert r.daily_loss_trigger_price(is_long=True, quantity=1, avg_price=5000.25, **kw) == 4998.5
    realized_only = RiskManager(RiskConfig(max_daily_loss=30), GLOBEX)
    assert realized_only.daily_loss_trigger_price(is_long=True, quantity=1, avg_price=5000, **kw) is None


def test_daily_pnl_basis_and_sticky_breach():
    d = pd.Timestamp("2025-03-10")
    r = RiskManager(RiskConfig(max_daily_loss=30, daily_loss_basis="realized_and_unrealized"), GLOBEX)
    r.on_trade_closed(-10.0, d)
    assert r.daily_pnl(d, open_pnl=-15.0) == -25.0 and not r.check_daily_loss(d, -15.0)
    assert r.check_daily_loss(d, -20.0)  # -30 reached
    assert r.check_daily_loss(d, +50.0)  # stays breached after a recovery
    assert r.check_entry(when=ts("12:00"), session_date=d, quantity=1).reason == "max_daily_loss"
    assert r.check_entry(when=ts("12:00"), session_date=pd.Timestamp("2025-03-11"), quantity=1).approved
    realized = RiskManager(RiskConfig(max_daily_loss=30), GLOBEX)
    assert realized.daily_pnl(d, open_pnl=-100.0) == 0.0  # unrealized ignored on the realized basis


def test_open_position_flattened_at_daily_loss_level():
    rows = bars(("10:01", 5000, 5000.5, 4999.5, 5000), ("10:02", 4999, 4999.5, 4990, 4992),
                ("10:03", 4992, 4993, 4991, 4992), ("10:04", 4992, 4993, 4991, 4992))
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long")], "10:02": [OrderRequest.enter("long")]},
                 config=dl_config())
    t = res.trades.iloc[0]
    # Level 4994.5 (see hand calculation above); sell stop + 1 tick slippage = 4994.25.
    assert t.exit_reason == "daily_loss_limit" and t.exit_time == ts("10:02") and t.exit_price == 4994.25
    assert t.net_pnl == pytest.approx((4994.25 - 5000.25) * 5 - 2)  # -32: exit costs come on top of the trigger
    assert len(res.trades) == 1
    rej = res.rejections.iloc[0]
    assert (rej.reason, rej.time) == ("max_daily_loss", ts("10:03"))


def test_daily_loss_gap_fills_at_open():
    rows = bars(("10:01", 5000, 5000.5, 4999.5, 5000), ("10:02", 4980, 4981, 4979, 4980))
    t = run(rows, {"10:00": [OrderRequest.enter("long")]}, config=dl_config())[0].trades.iloc[0]
    assert t.exit_reason == "daily_loss_limit" and t.exit_price == 4979.75 and t.exit_at_open


def test_closer_bracket_stop_governs_and_limit_not_breached():
    rows = bars(("10:01", 5000, 5000.5, 4999.5, 5000), ("10:02", 4999, 4999.5, 4990, 4992),
                ("10:03", 4992, 4993, 4991, 4992), ("10:04", 4992, 4993, 4991, 4992))
    script = {"10:00": [OrderRequest.enter("long", stop_loss=4996)], "10:02": [OrderRequest.enter("long")]}
    res, _ = run(rows, script, config=dl_config())
    t = res.trades.iloc[0]
    assert t.exit_reason == "stop_loss" and t.exit_price == 4995.75
    assert t.net_pnl == pytest.approx(-24.5)  # > -30: no breach
    assert len(res.trades) == 2 and res.rejections.empty
    # A bracket stop beyond the daily-loss level: the daily-loss level triggers first.
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long", stop_loss=4990)]}, config=dl_config())
    assert res.trades.iloc[0].exit_reason == "daily_loss_limit"


def test_realized_basis_ignores_open_loss():
    rows = bars(("10:01", 5000, 5000.5, 4999.5, 5000), ("10:02", 4999, 4999.5, 4990, 4992))
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long")]}, config=dl_config(basis="realized"))
    t = res.trades.iloc[0]
    assert t.exit_reason == "end_of_data" and res.equity.equity.min() < 10_000 - 30


def test_block_entries_keeps_position_and_allows_protective_exit():
    # Low 4990 crosses the limit, close recovers to 4999: the day stays breached.
    rows = bars(("10:01", 5000, 5000.5, 4999.5, 5000), ("10:02", 4999, 4999.5, 4990, 4999),
                ("10:03", 4999, 5000, 4998, 4999), ("10:04", 4999, 5000, 4998, 4999))
    script = {"10:00": [OrderRequest.enter("long")], "10:02": [OrderRequest.exit()],
              "10:04": [OrderRequest.enter("long")]}
    res, _ = run(rows, script, config=dl_config(action="block_entries"))
    t = res.trades.iloc[0]
    assert t.exit_reason == "strategy_exit" and t.exit_time == ts("10:03")  # exit not blocked
    assert list(res.rejections.reason) == ["max_daily_loss"]
    assert any("daily loss limit reached" in w for w in res.warnings)


def test_daily_loss_resets_next_session():
    day1 = frame(bars(("10:01", 5000, 5000.5, 4999.5, 5000), ("10:02", 4980, 4981, 4979, 4980)))
    day2 = frame([("18:00", 5000, 5000.5, 4999.5, 5000), ("18:01", 5000, 5000.5, 4999.5, 5000),
                  ("18:02", 5000, 5000.5, 4999.5, 5000)])  # trade date 2025-03-11
    script = {"10:00": [OrderRequest.enter("long")], "10:02": [OrderRequest.enter("long")],
              "18:00": [OrderRequest.enter("long")]}
    res, _ = run(pd.concat([day1, day2]), script, config=dl_config())
    assert list(res.trades.exit_reason) == ["daily_loss_limit", "end_of_data"]
    assert list(res.rejections.reason) == ["max_daily_loss"]


def test_no_double_counting_of_costs():
    rows = bars(("10:01", 5000, 5000.5, 4999.5, 5000), ("10:02", 4999, 4999.5, 4990, 4992))
    cfg = dl_config()
    risk = RiskManager(cfg.risk, GLOBEX)
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long")]}, config=cfg, risk_manager=risk)
    t = res.trades.iloc[0]
    d = pd.Timestamp("2025-03-10")
    # Realized daily PnL == the trade's net PnL == the equity change (costs counted once).
    assert risk.daily_realized_pnl(d) == pytest.approx(t.net_pnl)
    assert res.equity.equity.iloc[-1] - 10_000 == pytest.approx(t.net_pnl)
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.commission - t.slippage_cost)


@pytest.mark.parametrize("raw", [{"daily_loss_basis": "equity"}, {"daily_loss_action": "pause"}])
def test_daily_loss_config_validation(raw):
    with pytest.raises(ConfigError):
        RiskConfig.from_dict(raw)


def test_shipped_config_uses_unrealized_basis():
    from src.backtest.config import load_backtest_config

    c = load_backtest_config()
    assert c.risk.daily_loss_basis == "realized_and_unrealized" and c.risk.daily_loss_action == "flatten"
    assert c.sizing.fill_risk_policy == "reduce"


# =============================================================== 4. metric definitions

def test_gross_vs_net_hand_calculated():
    trades = pd.DataFrame({
        "side": ["long", "long", "short"],
        "gross_pnl": [50.0, 2.0, -20.0],     # before costs
        "commission": [2.0, 2.0, 2.0],
        "slippage_cost": [1.25, 1.25, 1.0],
        "net_pnl": [46.75, -1.25, -23.0],     # gross - commission - slippage
        "r_multiple": [math.nan] * 3,
        "ambiguous": [False] * 3,
        "exit_session_date": [pd.Timestamp("2025-03-10")] * 3,
    })
    m, _ = compute_metrics(trades, pd.DataFrame(), 10_000.0)
    # The second trade is profitable before costs but a net loss after costs.
    assert m["gross_profit"] == pytest.approx(52.0) and m["gross_loss"] == pytest.approx(-20.0)
    assert m["gross_pnl_before_costs"] == pytest.approx(32.0)
    assert m["total_commission"] == 6.0 and m["total_slippage_cost"] == 3.5 and m["total_costs"] == 9.5
    assert m["net_profit"] == pytest.approx(22.5) == pytest.approx(m["gross_pnl_before_costs"] - m["total_costs"])
    assert m["net_profit_of_winners"] == pytest.approx(46.75)
    assert m["net_loss_of_losers"] == pytest.approx(-24.25)
    assert m["profit_factor"] == pytest.approx(46.75 / 24.25)
    assert m["gross_profit_factor"] == pytest.approx(52 / 20)
    assert m["win_rate"] == pytest.approx(1 / 3)
    assert m["expectancy"] == pytest.approx(22.5 / 3)
    assert m["average_win"] == 46.75 and m["average_loss"] == pytest.approx(-12.125)


def test_engine_trade_gross_is_before_costs():
    rows = [QUIET, ("10:01", 5000, 5003, 4998, 5002), ("10:02", 5002, 5011, 4999, 5010)]
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long", stop_loss=4990, take_profit=5010)]})
    t = res.trades.iloc[0]
    assert t.gross_pnl == pytest.approx(50.0)  # (5010 - 5000) * 5 at un-slipped prices
    assert t.slippage_cost == pytest.approx(1.25) and t.commission == 2.0 and t.total_costs == pytest.approx(3.25)
    assert t.net_pnl == pytest.approx(46.75)
    assert res.metrics["gross_profit"] == pytest.approx(50.0) and res.metrics["net_profit"] == pytest.approx(46.75)
    fill = res.fills.iloc[0]
    assert (fill.base_price, fill.price) == (5000.0, 5000.25)
