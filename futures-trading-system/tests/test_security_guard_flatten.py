"""H2 (cost-aware flatten), M1 (floor_lock_offset), M2 (end-of-data exit), L2 (intraday timing)
and their interactions."""

import math
from dataclasses import replace

import pandas as pd
import pytest

from src.backtest.engine import BacktestEngine
from src.backtest.orders import OrderRequest
from src.config import ConfigError
from src.risk.account import AccountGuard, AccountRules, load_account_rules
from tests.backtest_helpers import GLOBEX, MES, MNQ, frame, make_config, run, ts

DROP = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000),
        ("10:02", 4999, 4999.5, 4900, 4905), ("10:03", 4905, 4906, 4904, 4905)]  # deep enough for 1 lot


def mirror(rows, c=10000.0):
    return [(t, c - o, c - l, c - h, c - cl) for t, o, h, l, cl in rows]


def rules(**kw):
    base = dict(enabled=True, profile="t", drawdown_type="static", max_drawdown=300.0, safety_buffer=40.0,
                flatten_buffer=20.0, require_trade_risk_within_buffer=False)
    return AccountRules(**(base | kw))


def cfg(r, **kw):
    return replace(make_config(**kw), account_rules=r)


def assert_no_hidden_breach(res):
    """Invariant: any equity at/below the floor is recorded as a breach."""
    below = (res.equity["drawdown_buffer"] <= 1e-9).any()
    assert res.account["breached"] == bool(below) or res.account["breached"]


# ----------------------------------------------------------------- H2

def test_zero_flatten_buffer_rejected():
    with pytest.raises(ConfigError, match="flatten_buffer"):
        rules(flatten_buffer=0.0)


@pytest.mark.parametrize("commission,stop_ticks,contract,buffer,ok", [
    (1.0, 1, MES, 2.0, False),   # MES exit cost 1 + 1.25 = 2.25 > 2.00
    (1.0, 1, MES, 2.25, True),   # exactly equal
    (1.0, 1, MNQ, 2.0, True),    # MNQ exit cost 1 + 0.50 = 1.50
    (3.0, 2, MES, 5.0, False),   # 3 + 2 x 1.25 = 5.50
    (3.0, 2, MES, 5.5, True),
])
def test_flatten_buffer_must_cover_one_contract_exit_cost(commission, stop_ticks, contract, buffer, ok):
    config = cfg(rules(flatten_buffer=buffer, safety_buffer=max(buffer, 40.0)), commission=commission,
                 stop_ticks=stop_ticks)
    if ok:
        BacktestEngine(config, contract, GLOBEX).run.__self__  # constructs
        res, _ = run(DROP, {"10:00": [OrderRequest.enter("long", stop_loss=4800)]}, contract=contract, config=config) \
            if contract is MES else (None, None)
        if res is not None:
            assert not res.account["breached"]
    else:
        with pytest.raises(ConfigError, match="exit cost"):
            run(DROP, {"10:00": [OrderRequest.enter("long", stop_loss=4800)]}, contract=contract, config=config)


@pytest.mark.parametrize("side,qty", [("long", 1), ("long", 3), ("short", 1), ("short", 3)])
@pytest.mark.parametrize("commission,stop_ticks", [(1.0, 1), (2.5, 2), (0.0, 0), (4.0, 4)])
def test_flatten_keeps_equity_at_or_above_floor_plus_buffer(side, qty, commission, stop_ticks):
    fb = max(20.0, commission + stop_ticks * 1.25)
    rows = DROP if side == "long" else mirror(DROP)
    stop = 4700 if side == "long" else 10000 - 4700
    res, _ = run(rows, {"10:00": [OrderRequest.enter(side, stop_loss=stop, quantity=qty)]},
                 config=cfg(rules(flatten_buffer=fb), commission=commission, stop_ticks=stop_ticks))
    t = res.trades.iloc[0]
    assert t.exit_reason == "account_protection" and not t.exit_at_open
    equity_after = 10_000 + res.trades.net_pnl.sum()
    assert equity_after >= 9_700 + fb - 1e-9  # floor 9,700 + flatten buffer, after ALL exit costs
    assert not res.account["breached"]


def test_hand_calculated_three_contracts():
    # Entry 5000.25 x 3 (realized -3); target equity 9,720 + exit costs 3 x 2.25 = 6.75:
    # level = 5000.25 + (9720 + 6.75 - 10000 + 3) / 15 = 4982.233 -> up 4982.25; fill 4982.00.
    res, _ = run(DROP, {"10:00": [OrderRequest.enter("long", stop_loss=4700, quantity=3)]}, config=cfg(rules()))
    t = res.trades.iloc[0]
    assert t.exit_price == 4982.0
    assert 10_000 + t.net_pnl == pytest.approx(9720.25)


def test_gap_through_flatten_level_is_a_recorded_breach():
    gap = DROP[:2] + [("10:02", 4930, 4931, 4925, 4928)]
    res, _ = run(gap, {"10:00": [OrderRequest.enter("long", stop_loss=4700)]}, config=cfg(rules()))
    t = res.trades.iloc[0]
    assert t.exit_at_open and t.exit_price == 4929.75
    assert res.account["breached"] and res.equity["drawdown_buffer"].iloc[-1] < 0


# ----------------------------------------------------------------- M1

@pytest.mark.parametrize("offset", [-0.01, -500.0, math.nan, math.inf, -math.inf])
def test_floor_lock_offset_cannot_weaken_the_floor(offset):
    with pytest.raises(ConfigError, match="floor_lock_offset"):
        rules(floor_lock_offset=offset)


@pytest.mark.parametrize("offset,peak,floor", [(0.0, 10_500, 10_000), (100.0, 10_500, 10_100),
                                               (1e9, 10_500, 10_200)])
def test_valid_floor_lock_offsets(offset, peak, floor):
    g = AccountGuard(rules(drawdown_type="trailing_eod", floor_lock_offset=offset), 10_000)
    g.on_session_end(peak, pd.Timestamp("2025-03-11"))
    assert g.floor == floor
    static = AccountGuard(rules(floor_lock_offset=offset), 10_000)
    assert static.floor == 9_700  # never below the static rule


def test_invalid_offset_rejected_at_startup(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('[account_rules]\nenabled = true\nmax_drawdown = 100.0\nsafety_buffer = 40.0\n'
                 'flatten_buffer = 20.0\nfloor_lock_offset = -50.0\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="floor_lock_offset"):
        load_account_rules(p)


# ----------------------------------------------------------------- M2

def end_rows(last_close):
    return [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000),
            ("10:02", 4999, 4999.5, last_close, last_close)]


@pytest.mark.parametrize("side", ["long", "short"])
def test_end_of_data_exit_above_floor(side):
    rows = end_rows(4995.0) if side == "long" else mirror(end_rows(4995.0))
    stop = 4700 if side == "long" else 10000 - 4700
    res, _ = run(rows, {"10:00": [OrderRequest.enter(side, stop_loss=stop)]}, config=cfg(rules()))
    assert res.trades.exit_reason.iloc[0] == "end_of_data" and not res.account["breached"]
    assert res.equity["drawdown_buffer"].iloc[-1] == pytest.approx(res.account["min_remaining_buffer"])


@pytest.mark.parametrize("side", ["long", "short"])
def test_end_of_data_exit_costs_causing_breach(side):
    # End-of-data is a MARKET exit: 12 ticks of market slippage exceed the stop-slippage reserve,
    # pushing equity below the floor although the close was above the flatten level.
    r = rules(max_drawdown=100.0, flatten_buffer=2.25)
    # Market entry with 12 ticks slippage fills at 5003.00; cost-aware flatten level = 4984.25.
    # The last close 4985.00 stays above it, then the end-of-data market exit (12 ticks = 3 pts)
    # fills at 4982.00 -> equity 10,000 - 2 + (4982 - 5003) x 5 = 9,893.00 < floor 9,900.
    rows = end_rows(4985.0) if side == "long" else mirror(end_rows(4985.0))
    stop = 4700 if side == "long" else 10000 - 4700
    res, _ = run(rows, {"10:00": [OrderRequest.enter(side, stop_loss=stop)]}, config=cfg(r, market_ticks=12))
    final = res.equity["equity"].iloc[-1]
    assert res.trades.exit_reason.iloc[0] == "end_of_data" and final < 9_900
    assert res.account["breached"] and res.account["min_remaining_buffer"] < 0
    assert any(e["event"] == "drawdown_breach" for e in res.account["events"])


def test_end_of_data_exactly_at_floor_is_a_breach():
    g = AccountGuard(rules(max_drawdown=100.0, flatten_buffer=2.25), 10_000)
    assert g.evaluate(9_900.0, "t", None, position_open=True) == "breach"


# ----------------------------------------------------------------- L2 and interactions

def test_intraday_trailing_floor_not_acted_on_at_the_open():
    # Long from 5000.25. Bar 10:02 opens 5005 (pre-bar level not reached), spikes to 5080 (raises the
    # trailing peak) and falls to 5040: the raised floor can only be acted on AFTER the spike.
    r = rules(drawdown_type="trailing_intraday", max_drawdown=300.0, flatten_buffer=20.0, safety_buffer=40.0)
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000),
            ("10:02", 5005, 5080, 5020, 5040), ("10:03", 5040, 5041, 5039, 5040)]
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long", stop_loss=4700)]}, config=cfg(r))
    t = res.trades.iloc[0]
    assert t.exit_reason == "account_protection" and t.exit_time == ts("10:02")
    assert not t.exit_at_open and t.exit_price > 5005  # intrabar at the raised level, not the open
    assert not res.account["breached"]


def test_gap_during_trailing_floor_update_is_recorded():
    # Peak from closes: the 10:01 close 5025 raises the floor to 10,022.75 for the next bar,
    # which gaps open at 4990 -> exit at the open, equity 9,945.50 below the raised floor.
    r = rules(drawdown_type="trailing_intraday", max_drawdown=100.0, flatten_buffer=20.0, safety_buffer=40.0,
              intraday_peak_source="close")
    rows = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5026, 4999, 5025),  # peak raised at the close
            ("10:02", 4990, 4991, 4985, 4988)]                                       # gap below the new floor
    res, _ = run(rows, {"10:00": [OrderRequest.enter("long", stop_loss=4700)]}, config=cfg(r))
    t = res.trades.iloc[0]
    assert t.exit_at_open and res.account["breached"]
    assert_no_hidden_breach(res)


def test_flatten_with_costs_multiple_contracts_and_lock():
    res, _ = run(DROP, {"10:00": [OrderRequest.enter("long", stop_loss=4700, quantity=3)],
                        "10:03": [OrderRequest.enter("long", stop_loss=4700)]}, config=cfg(rules()))
    assert res.trades.exit_reason.iloc[0] == "account_protection"
    assert list(res.rejections.reason) == ["account_locked_after_flatten"]
