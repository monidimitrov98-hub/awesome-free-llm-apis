"""Account-level drawdown protection (generic rules), unit and engine level."""

from dataclasses import replace

import pandas as pd
import pytest

from src.backtest.config import load_backtest_config
from src.backtest.orders import OrderRequest
from src.config import ConfigError
from src.risk.account import AccountGuard, AccountRules, load_account_rules
from src.risk.manager import RiskConfig
from tests.backtest_helpers import frame, make_config, run, ts

D1, D2 = pd.Timestamp("2025-03-10"), pd.Timestamp("2025-03-11")


def rules(**kw):
    base = dict(enabled=True, profile="test", drawdown_type="static", max_drawdown=100.0, safety_buffer=40.0,
                flatten_buffer=20.0)
    return AccountRules(**(base | kw))


# ----------------------------------------------------------------- unit

def test_static_floor_and_buffers():
    g = AccountGuard(rules(), 10_000)
    assert g.floor == 9_900 and g.remaining_buffer(9_950) == 50
    assert g.entry_block_reason(9_950, D1) is None
    assert g.entry_block_reason(9_940, D1) == "account_safety_buffer"  # buffer 40 <= safety 40
    assert g.entry_block_reason(10_000, D1, trade_risk=61) == "account_trade_risk_exceeds_buffer"  # 100 - 61 < 40
    assert g.entry_block_reason(10_000, D1, trade_risk=60) is None


def test_trailing_eod_and_floor_lock():
    g = AccountGuard(rules(drawdown_type="trailing_eod", max_drawdown=500, safety_buffer=50, flatten_buffer=10), 10_000)
    g.on_session_end(10_300, D2)
    assert g.floor == 9_800
    g.on_session_end(10_100, D2)  # the peak never falls
    assert g.floor == 9_800
    locked = AccountGuard(rules(drawdown_type="trailing_eod", max_drawdown=500, safety_buffer=50, flatten_buffer=10,
                                floor_lock_offset=0.0), 10_000)
    locked.on_session_end(10_700, D2)
    assert locked.floor == 10_000  # stops trailing at the initial balance


def test_trailing_intraday_peak_sources():
    fav = AccountGuard(rules(drawdown_type="trailing_intraday"), 10_000)
    fav.observe_intraday(10_080, favorable=True)
    assert fav.floor == 9_980
    close_only = AccountGuard(rules(drawdown_type="trailing_intraday", intraday_peak_source="close"), 10_000)
    close_only.observe_intraday(10_080, favorable=True)
    assert close_only.floor == 9_900
    close_only.observe_intraday(10_050, favorable=False)
    assert close_only.floor == 9_950
    static = AccountGuard(rules(), 10_000)
    static.observe_intraday(20_000, favorable=False)
    assert static.floor == 9_900  # static never trails


def test_flatten_lock_session_and_permanent_and_breach():
    g = AccountGuard(rules(), 10_000)
    assert g.evaluate(9_915, "t1", D1) == "flatten" and g.is_locked(D1)
    assert g.entry_block_reason(9_990, D1) == "account_locked_after_flatten"
    g.on_session_end(9_990, D2)
    assert not g.is_locked(D2)  # session lock released next session
    p = AccountGuard(rules(lock_after_flatten="permanent"), 10_000)
    p.evaluate(9_915, "t1", D1)
    p.on_session_end(9_990, D2)
    assert p.is_locked(D2)
    b = AccountGuard(rules(), 10_000)
    assert b.evaluate(9_899, "t1", D1) == "breach" and b.breached
    flat = AccountGuard(rules(), 10_000)
    assert flat.evaluate(9_915, "t1", D1, position_open=False) is None  # nothing to flatten
    assert flat.entry_block_reason(9_915, D1) == "account_safety_buffer"  # but entries are still blocked
    assert b.entry_block_reason(20_000, D2) == "account_drawdown_breached"
    assert b.summary()["events"][0]["event"] == "drawdown_breach"


def test_disabled_guard_never_blocks():
    g = AccountGuard(AccountRules(), 10_000)
    assert g.entry_block_reason(-1e9, D1, trade_risk=1e9) is None and g.evaluate(-1e9, "t", D1) is None
    assert g.flatten_equity_level() is None


@pytest.mark.parametrize("kw", [dict(drawdown_type="weekly"), dict(max_drawdown=0.0),
                                dict(safety_buffer=200.0), dict(flatten_buffer=50.0),
                                dict(lock_after_flatten="day")])
def test_rule_validation(kw):
    with pytest.raises(ConfigError):
        rules(**kw)


def test_profile_loading(tmp_path):
    r = load_account_rules("config/account_profiles/generic_example.toml")
    assert r.enabled and r.drawdown_type == "trailing_eod" and r.profile == "generic_example"
    assert load_backtest_config().account_rules == r
    bad = tmp_path / "p.toml"
    bad.write_text("[account_rules]\nenabled = true\nmax_dd = 5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Unknown"):
        load_account_rules(bad)


def test_strategy_config_cannot_carry_account_rules(tmp_path):
    from src.strategy.vwap_pullback_config import DEFAULT_STRATEGY_CONFIG_PATH, load_strategy_config

    p = tmp_path / "s.toml"
    p.write_text(DEFAULT_STRATEGY_CONFIG_PATH.read_text(encoding="utf-8") +
                 "\n[account_rules]\nenabled = false\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Unknown section"):
        load_strategy_config(p)


# ----------------------------------------------------------------- engine

def cfg_with(r, **kw):
    return replace(make_config(**kw), account_rules=r)


LONG_ROWS = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5001, 4999, 5000),
             ("10:02", 4999, 4999.5, 4980, 4982), ("10:03", 4982, 4983, 4981, 4982), ("10:04", 4982, 4983, 4981, 4982)]


def test_open_position_flattened_at_account_level_hand_calculated():
    # Static floor 9,900; flatten at equity 9,920. Entry 5000.25 (realized -1 commission):
    # level = 5000.25 + (9920 - 10000 + 1) / 5 = 4984.45 -> rounded up 4984.50; sell stop - 1 tick.
    res, _ = run(LONG_ROWS, {"10:00": [OrderRequest.enter("long")], "10:03": [OrderRequest.enter("long")]},
                 config=cfg_with(rules()))
    t = res.trades.iloc[0]
    assert (t.exit_reason, t.exit_price, t.exit_time) == ("account_protection", 4984.25, ts("10:02"))
    assert t.net_pnl == pytest.approx((4984.25 - 5000.25) * 5 - 2)  # -82
    assert list(res.rejections.reason) == ["account_locked_after_flatten"]
    assert res.equity["drawdown_floor"].iloc[-1] == 9_900
    assert res.equity["drawdown_buffer"].iloc[-1] == pytest.approx(18.0)
    assert res.account["events"][0]["event"] == "flatten_buffer_reached"


def test_lock_resets_next_session_but_safety_buffer_still_applies():
    day1 = frame(LONG_ROWS)
    day2 = frame([("18:00", 4982, 4983, 4981, 4982), ("18:01", 4982, 4983, 4981, 4982)])
    res, _ = run(pd.concat([day1, day2]), {"10:00": [OrderRequest.enter("long")],
                                           "18:00": [OrderRequest.enter("long")]}, config=cfg_with(rules()))
    assert list(res.rejections.reason) == ["account_safety_buffer"]  # buffer 18 <= 40 in the new session


def test_gap_through_floor_is_a_breach_and_locks_for_good():
    rows = LONG_ROWS[:2] + [("10:02", 4970, 4971, 4960, 4965)]
    day2 = frame([("18:00", 5000, 5001, 4999, 5000), ("18:01", 5000, 5001, 4999, 5000)])
    res, _ = run(pd.concat([frame(rows), day2]), {"10:00": [OrderRequest.enter("long")],
                                                   "18:00": [OrderRequest.enter("long")]},
                 config=cfg_with(rules(lock_after_flatten="session")))
    t = res.trades.iloc[0]
    assert t.exit_reason == "account_protection" and t.exit_price == 4969.75 and t.exit_at_open
    assert res.account["breached"] and list(res.rejections.reason) == ["account_drawdown_breached"]


def test_trade_risk_must_fit_the_buffer():
    r = rules(max_drawdown=1000.0, safety_buffer=900.0, flatten_buffer=100.0)
    ok, _ = run(LONG_ROWS[:2], {"10:00": [OrderRequest.enter("long", stop_loss=4990)]}, config=cfg_with(r))
    assert len(ok.trades) == 1  # risk 10.25 x 5 + 3.25 = 54.50 leaves 945.50 >= 900
    res, _ = run(LONG_ROWS[:2], {"10:00": [OrderRequest.enter("long", stop_loss=4970)]}, config=cfg_with(r))
    assert res.trades.empty  # risk 154.50 leaves 845.50 < 900
    rej = res.rejections.iloc[0]
    assert (rej.stage, rej.reason) == ("fill", "account_trade_risk_exceeds_buffer")


def test_trailing_eod_floor_rises_after_profitable_session():
    r = rules(drawdown_type="trailing_eod")
    day1 = frame([("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5003, 4998, 5002),
                  ("10:02", 5002, 5011, 4999, 5010)])
    day2 = frame([("18:00", 5010, 5011, 5009, 5010)])
    res, _ = run(pd.concat([day1, day2]), {"10:00": [OrderRequest.enter("long", stop_loss=4990,
                                                                            take_profit=5010)]}, config=cfg_with(r))
    assert res.trades.iloc[0].net_pnl == pytest.approx(46.75)
    floors = res.equity["drawdown_floor"]
    assert floors.iloc[0] == 9_900 and floors.iloc[-1] == pytest.approx(9_946.75)


def test_closer_protective_level_wins():
    # Daily loss 30 -> level 4994.50 is closer than the account level 4984.50.
    res, _ = run(LONG_ROWS, {"10:00": [OrderRequest.enter("long")]},
                 config=cfg_with(rules(), risk=RiskConfig(max_daily_loss=30,
                                                          daily_loss_basis="realized_and_unrealized")))
    assert res.trades.iloc[0].exit_reason == "daily_loss_limit"
    res, _ = run(LONG_ROWS, {"10:00": [OrderRequest.enter("long")]},
                 config=cfg_with(rules(), risk=RiskConfig(max_daily_loss=300,
                                                          daily_loss_basis="realized_and_unrealized")))
    assert res.trades.iloc[0].exit_reason == "account_protection"


def test_short_symmetry():
    c = 10000.0
    rows = [(t, c - o, c - l, c - h, c - cl) for t, o, h, l, cl in LONG_ROWS]
    res, _ = run(rows, {"10:00": [OrderRequest.enter("short")]}, config=cfg_with(rules()))
    t = res.trades.iloc[0]
    # Short at 4999.75; level = 4999.75 + 15.8 = 5015.55 -> rounded down 5015.50; buy stop + 1 tick.
    assert (t.exit_reason, t.exit_price) == ("account_protection", 5015.75)
    assert t.net_pnl == pytest.approx(-82.0)
