"""Sprint 5: end-to-end paper sessions -- authorization chain, protections, exits, sessions,
multiple instruments, safe shutdown and emergency stop. Deterministic events, no network."""

from dataclasses import replace

import pandas as pd
import pytest

from src.backtest.orders import OrderRequest
from src.paper.metrics import paper_report
from src.risk.manager import RiskConfig
from tests.paper_helpers import (PCFG, LiveLikeFeed, T, allow_news, bt, guard_rules, long, path, quote, rec, session,
                                 trade)

FLAT = lambda i: 5000.0  # noqa: E731


def kinds(s):
    return [e["kind"] for e in s.journal.entries()]


def decisions(s):
    return [(d["reason"], d["allowed"]) for d in s.decisions]


def drop_after(start, minutes, at_minute, to, root="MES"):
    """Flat at 5000, then from ``at_minute`` on at ``to``."""
    return path(start, minutes, lambda i: 5000.0 if i * 15 < at_minute * 60 else to, root)


# ----------------------------------------------------------------- normal operation
def test_complete_trade_stop_loss(tmp_path):
    evs = drop_after(T("10:00"), 20, 12, 4989.0)
    s = session(tmp_path, evs, {T("10:05"): [long(stop=4990.0, target=5010.0)]})
    assert s.run() == "end_of_data"
    assert decisions(s) == [("", True)]
    t = s.account.closed_trades[0]
    entry_exec = [e["payload"] for e in s.journal.entries("execution")][0]
    assert pd.Timestamp(entry_exec["ts"]) > T("10:05")              # filled AFTER the decision event
    assert entry_exec["reference_price"] == 5000.0 and entry_exec["quote_based"]  # at the ask
    assert t["exit_role"] == "stop_loss" and t["net_pnl"] < 0 and t["quantity"] == 1
    st = s.test_strategies["MES"]
    assert [e.status for e in st.events] == ["accepted", "filled"] and len(st.fills) == 2
    for k in ("session_created", "market_event", "entry_decision", "order_submitted", "fill_authorization",
              "execution", "account_update", "trade_closed", "order_cancelled", "shutdown"):
        assert k in kinds(s), k
    rep = paper_report(s)
    assert rep["run_kind"] == "replay" and rep["trading"]["overall"]["trades"] == 1
    assert rep["orders"]["fill_rate"] == 1.0 and rep["signals"]["accepted"] == 1


def test_take_profit_and_strategy_exit(tmp_path):
    up = path(T("10:00"), 20, lambda i: 5000.0 if i * 15 < 600 else 5012.0)
    s = session(tmp_path / "a", up, {T("10:05"): [long(stop=4990.0, target=5010.0)]})
    s.run()
    assert s.account.closed_trades[0]["exit_role"] == "take_profit"
    assert s.account.closed_trades[0]["exit_price"] == 5010.0  # limit price, trade-through only
    s2 = session(tmp_path / "b", path(T("10:00"), 20, lambda i: 5000.0),
                 {T("10:05"): [long()], T("10:10"): [OrderRequest.exit("time_exit")]})
    s2.run()
    assert s2.account.closed_trades[0]["exit_role"] == "exit"


# ----------------------------------------------------------------- authorization chain
def test_news_blackout_blocks_entry_but_records_the_signal(tmp_path):
    news = allow_news(("MES",), rec("Consumer Price Index", "2025-03-10T10:15:00"))  # window 09:45..10:45
    s = session(tmp_path, path(T("10:00"), 10, lambda i: 5000.0), {T("10:05"): [long()]}, news=news)
    s.run()
    assert decisions(s) == [("news_blackout_active", False)] and not s.broker.orders
    assert [e.status for e in s.test_strategies["MES"].events] == ["rejected"]
    stage = [st for st in s.decisions[0]["stages"] if st["stage"] == "news"][0]
    assert "Consumer Price Index" in stage["explanation"]


def test_paper_mode_with_incomplete_calendar_blocks_every_entry(tmp_path):
    from src.risk.news.guard import NewsGuard
    from tests.news_helpers import CFG, store

    incomplete = store(coverage={"fixture": ["cpi"]})  # most required categories uncovered
    incomplete.provider_class = {"fixture": "manual"}
    news = {"MES": NewsGuard(CFG, incomplete, "MES", operational=True)}
    pcfg = replace(PCFG, mode="paper")
    s = session(tmp_path, path(T("10:00"), 30, lambda i: 5000.0),
                {T("10:05"): [long("a")], T("10:15"): [long("b")], T("10:25"): [long("c")]},
                pcfg=pcfg, news=news, feed_cls=LiveLikeFeed)
    s.run()
    assert s.run_kind == "simulated_realtime"
    assert decisions(s) == [("news_calendar_incomplete", False)] * 3 and not s.broker.orders
    assert "market_event" in kinds(s)  # data and signals still processed and recorded
    rep = paper_report(s)
    assert rep["signals"]["rejection_reasons"] == {"news_calendar_incomplete": 3}
    assert rep["trading"]["overall"]["trades"] == 0


def test_paper_mode_refuses_replay_feed(tmp_path):
    from src.config import ConfigError

    with pytest.raises(ConfigError, match="paper mode"):
        session(tmp_path, [], pcfg=replace(PCFG, mode="paper"))


def test_infrastructure_validation_never_authorizes_and_has_no_trade_stats(tmp_path):
    s = session(tmp_path, path(T("10:00"), 10, lambda i: 5000.0), {T("10:05"): [long()]},
                pcfg=replace(PCFG, mode="infrastructure_validation"))
    s.run()
    assert decisions(s) == [("infrastructure_validation_mode", False)]
    rep = paper_report(s)
    assert rep["trading"] is None and rep["run_kind"] == "infrastructure_validation"


def test_account_guard_blocks_and_flattens_even_during_news_blackout(tmp_path):
    rules = guard_rules(max_drawdown=300.0, safety_buffer=100.0, flatten_buffer=100.0)
    news = allow_news(("MES",), rec("Consumer Price Index", "2025-03-10T10:45:00"))  # window 10:15..11:15
    evs = drop_after(T("10:00"), 40, 16, 4979.0)  # -21 pts x 2 contracts = -210 USD at 10:16 (inside blackout)
    req = OrderRequest.enter("long", stop_loss=4950.0, quantity=2, client_id="a")
    s = session(tmp_path, evs, {T("10:05"): [req], T("10:30"): [long("b", stop=4970.0)]},
                btc=bt(account_rules=rules), news=news)
    s.run()
    t = s.account.closed_trades[0]
    assert t["exit_role"] == "exit" and t["exit_reason"] == "account_protection"
    assert any(e["payload"].get("reason") == "account_protection" for e in s.journal.entries("flatten_requested"))
    assert s.decisions[-1]["reason"] in ("account_locked_after_flatten", "news_blackout_active")
    assert s.account.guard.is_locked(pd.Timestamp("2025-03-10"))
    # a loss that leaves the buffer at/below the safety buffer blocks the next entry
    rules2 = guard_rules(max_drawdown=300.0, safety_buffer=250.0, flatten_buffer=100.0)
    evs2 = drop_after(T("10:00"), 20, 8, 4989.0)  # stop at 4990 -> about -55 USD -> buffer ~245 <= 250
    s2 = session(tmp_path / "b", evs2, {T("10:05"): [long("a", stop=4990.0)], T("10:15"): [long("b", stop=4980.0)]},
                 btc=bt(account_rules=rules2))
    s2.run()
    assert decisions(s2) == [("", True), ("account_safety_buffer", False)]


def test_account_guard_requires_a_stop_loss(tmp_path):
    s = session(tmp_path, path(T("10:00"), 10, FLAT), {T("10:05"): [OrderRequest.enter("long", client_id="x")]},
                btc=bt(account_rules=guard_rules()))
    s.run()
    assert decisions(s) == [("account_requires_stop_loss", False)]


def test_risk_manager_limits_and_emergency_stop(tmp_path):
    evs = path(T("10:00"), 30, FLAT)
    script = {T("10:05"): [long("a")], T("10:10"): [OrderRequest.exit()], T("10:15"): [long("b")]}
    s = session(tmp_path, evs, script, btc=bt(risk=RiskConfig(max_daily_trades=1)))
    s.run()
    assert decisions(s) == [("", True), ("max_daily_trades", False)]
    s2 = session(tmp_path / "e", path(T("10:00"), 20, FLAT), {T("10:05"): [long("a")], T("10:15"): [long("b")]})
    s2.connect()
    for ev in s2.feed.poll(10_000):
        s2.process_event(ev)
        if ev.ts >= T("10:08") and not s2.account.risk.emergency_active:
            s2.emergency_stop("operator test")
    assert s2.account.closed_trades and s2.account.closed_trades[0]["exit_role"] == "exit"
    assert s2.decisions[-1]["reason"] == "emergency_stop"


def test_session_end_flatten_and_cutoff(tmp_path):
    evs = path(T("16:30"), 29, FLAT)
    s = session(tmp_path, evs, {T("16:35"): [long("a")]}, btc=bt(risk=RiskConfig()))
    s.run()
    assert s.account.closed_trades[0]["exit_role"] == "exit"
    assert pd.Timestamp(s.account.closed_trades[0]["exit_time"]) >= T("16:55")
    assert any(e["payload"].get("reason") == "session_end" for e in s.journal.entries("flatten_requested"))
    s2 = session(tmp_path / "b", evs, {T("16:35"): [long("a")], T("16:45"): [OrderRequest.exit()],
                                       T("16:55"): [long("b")]}, btc=bt(risk=RiskConfig()))
    s2.run()
    assert decisions(s2)[-1] == ("session_end_cutoff", False)


def test_multiple_instruments_share_one_account(tmp_path):
    evs = sorted(path(T("10:00"), 15, FLAT, "MES") + path(T("10:00"), 15, lambda i: 20000.0, "MNQ"),
                 key=lambda e: (e.ts, e.root))
    script = {"MES": {T("10:05"): [long("m1")]}, "MNQ": {T("10:05"): [long("n1", stop=19990.0)]}}
    s = session(tmp_path, evs, script, pcfg=replace(PCFG, instruments=("MES", "MNQ")),
                news=allow_news(("MES", "MNQ")))
    s.connect()
    for ev in s.feed.poll(10_000):
        s.process_event(ev)
    assert s.account.position_qty("MES") == 1 and s.account.position_qty("MNQ") == 1
    eq = s.account.equity()
    assert eq == pytest.approx(10_000 + s.account.unrealized() - s.account.commissions)
    assert {o.root for o in s.broker.working(roles=("stop_loss",))} == {"MES", "MNQ"}


# ----------------------------------------------------------------- data failures
class FlakyFeed(LiveLikeFeed):
    """Delivers 20 events per poll and fails (disconnect) on the given poll numbers."""

    def __init__(self, events, fail_on=(3,)):
        super().__init__(events)
        self.fail_on, self.polls = set(fail_on), 0

    def poll(self, max_events=1000):
        self.polls += 1
        if self.polls in self.fail_on:
            self.connected = False
            from src.paper.feeds import FeedError
            raise FeedError("connection reset")
        return super().poll(20)


def test_disconnect_reconnect_and_recovery_pending(tmp_path):
    from tests.news_helpers import CFG, store
    from src.risk.news.guard import NewsGuard

    st = store()
    st.provider_class = {"fixture": "manual"}
    news = {"MES": NewsGuard(CFG, st, "MES", operational=True)}
    evs = path(T("10:00"), 12, FLAT)
    s = session(tmp_path, evs, {T("10:05"): [long()]}, pcfg=replace(PCFG, mode="paper", resume_after_events=500),
                news=news, feed_cls=lambda e: FlakyFeed(e, fail_on=(3,)))
    s.run()
    k = kinds(s)
    assert "feed_disconnected" in k and k.count("feed_connected") == 2
    assert decisions(s) == [("data_recovery_pending:disconnected", False)]  # not enough fresh data after reconnect


def test_reconnect_gives_up_and_shuts_down_safely(tmp_path):
    class Dead(LiveLikeFeed):
        def connect(self):
            from src.paper.feeds import FeedError
            raise FeedError("refused")

    s = session(tmp_path, [], pcfg=replace(PCFG, mode="paper"), feed_cls=Dead)
    assert s.run() == "feed_unavailable"
    assert kinds(s).count("feed_connect_failed") == s.pcfg.reconnect_max_attempts + 1
    assert not s.accepting_entries


def test_stale_data_gap_blocks_entries(tmp_path):
    evs = path(T("10:00"), 4, FLAT) + path(T("10:15"), 6, FLAT)  # 11-minute hole
    s = session(tmp_path, evs, {T("10:20"): [long()]}, pcfg=replace(PCFG, resume_after_events=60))
    s.run()
    assert decisions(s) == [("data_recovery_pending:data_gap", False)]


def test_invalid_events_are_rejected_and_never_used(tmp_path):
    good = path(T("10:00"), 10, FLAT)
    bad = [trade(T("10:02"), 5000.1, eid="offgrid"), trade(T("10:01"), 5100.0, eid="jump"),
           quote(T("10:03"), 5001.0, 5000.0, eid="crossed")]
    evs = sorted(good + bad, key=lambda e: e.ts)
    s = session(tmp_path, evs, {})
    s.run()
    rej = [e["payload"]["reason"] for e in s.journal.entries("data_rejected")]
    assert set(rej) >= {"price_off_tick_grid", "implausible_price_jump", "crossed_quote"}
    assert all(m == 5000.0 for m in [s.account.marks.get("MES", 5000.0)])


# ----------------------------------------------------------------- shutdown
def test_safe_shutdown_policies(tmp_path):
    evs = path(T("10:00"), 10, FLAT)
    s = session(tmp_path / "keep", evs, {T("10:05"): [long()]})
    s.run()
    sd = [e["payload"] for e in s.journal.entries("shutdown")][0]
    assert sd["open_positions_at_shutdown"] == {"MES": 1} and sd["working_protective_orders"]
    assert s.clean_shutdown
    s2 = session(tmp_path / "flat", path(T("10:00"), 20, FLAT), {T("10:05"): [long()]},
                 pcfg=replace(PCFG, shutdown_policy="flatten"))
    s2.connect()
    for ev in s2.feed.poll(150):  # stop early: data remains for the flatten
        s2.process_event(ev)
    assert s2.account.position_qty("MES") == 1
    s2.shutdown("operator")
    assert s2.account.position_qty("MES") == 0
    assert [e["payload"]["open_positions_at_shutdown"] for e in s2.journal.entries("shutdown")] == [{}]


def test_no_real_order_transmission_possible():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    for f in list((root / "src" / "paper").glob("*.py")) + [root / "run_paper.py"]:
        text = f.read_text().lower()
        for word in ("import socket", "import requests", "urllib", "http.client", "websocket", "ninjatrader",
                     "ninja_trader", "fix44", "broker_api"):
            assert word not in text, (f.name, word)
    from src.paper.broker import PaperBroker

    assert PaperBroker.TRANSMITS_REAL_ORDERS is False


def test_unrealized_daily_loss_limit_flattens(tmp_path):
    risk = RiskConfig(max_daily_loss=40.0, daily_loss_basis="realized_and_unrealized", daily_loss_action="flatten")
    evs = drop_after(T("10:00"), 20, 10, 4992.0)  # -8 pts x 5 USD = -40 USD (plus paid commission)
    s = session(tmp_path, evs, {T("10:05"): [long(stop=4950.0)], T("10:15"): [long("b", stop=4950.0)]},
                btc=bt(risk=risk))
    s.run()
    assert any(e["payload"].get("reason") == "daily_loss_limit" for e in s.journal.entries("flatten_requested"))
    assert s.decisions[-1]["reason"] == "max_daily_loss"
