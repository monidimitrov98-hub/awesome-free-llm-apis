"""Regression tests for the second Sprint 5 audit (starting commit f31fbc3).

Each test reproduces a defect found in f31fbc3 and asserts the fixed, fail-closed behaviour.
"""

import json
import os
import sqlite3
import stat
from dataclasses import replace

import pandas as pd
import pytest

from src.paper.broker import FillDecision, PaperBroker
from src.paper.events import MarketEvent
from src.paper.journal import KEY_ENV, Journal, JournalError
from src.paper.orders import LifecycleError, PaperOrder, PaperOrderState as St
from src.paper.runtime import RecoveryError
from src.paper.validation import EventValidator
from src.risk.manager import RiskConfig
from tests.paper_helpers import (APP, BT, PCFG, LiveLikeFeed, ListFeed, T, allow_news, bt, guard_rules, long, path,
                                 quote, session, trade)

FLAT = lambda i: 5000.0  # noqa: E731
SPECS = {"MES": APP.contracts["MES"]}
SD = pd.Timestamp("2025-03-10")


def feed(s, events):
    for e in events:
        s.process_event(e)
    s._commit()


def started(tmp_path, script=None, **kw):
    s = session(tmp_path, [], script or {}, **kw)
    s.connect()
    return s


def entry_after_signal(tmp_path, **kw):
    """A session whose strategy entry was accepted at 10:05 (the entry is still working)."""
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0)]}, **kw)
    feed(s, path(T("10:00"), 5, FLAT) + [trade(T("10:05:00.5"), 5000.0)])
    (o,) = s.broker.working("MES", ("entry",))
    return s, o


# ----------------------------------------------------------------- N1: authorization tokens
def test_n1_an_entry_without_an_approved_signal_cannot_be_submitted(tmp_path):
    s = started(tmp_path)
    feed(s, path(T("10:00"), 6, FLAT))
    assert not hasattr(s.authorizer, "issue")  # no public way to mint a token
    forged = PaperOrder("Z9", "MES", "entry", "market", True, 7, s.last_event_ts, s.seq, session_date=SD,
                        expire_at=s.last_event_ts + pd.Timedelta(minutes=1), auth_token="0" * 32)
    with pytest.raises(LifecycleError, match="not issued by the EntryAuthorizer"):
        s.broker.submit(forged, s.last_event_ts)


def test_n1_entry_terms_changed_after_submission_are_never_filled(tmp_path):
    s, o = entry_after_signal(tmp_path)
    o.stop_loss = 4000.0  # mutated after authorization (wider stop = larger risk)
    feed(s, path(T("10:05:01"), 1, FLAT))
    assert o.state is St.REJECTED and o.reason == "entry_terms_changed_after_authorization"
    assert s.account.position_qty("MES") == 0 and not s.broker.working("MES", ("stop_loss",))


def test_n1_entry_cannot_be_reactivated_for_the_current_event(tmp_path):
    s, o = entry_after_signal(tmp_path)
    o.active_after_seq = 0
    feed(s, path(T("10:05:01"), 1, FLAT))
    assert o.state is St.REJECTED and s.account.position_qty("MES") == 0


def test_n1_no_order_may_be_activated_for_an_already_processed_event():
    b = PaperBroker(BT, SPECS)
    b.on_event(trade(T("10:00"), 5000.0), 5, lambda r: 0, lambda *a: FillDecision(True))
    with pytest.raises(LifecycleError, match="already processed"):
        b.submit(PaperOrder("S1", "MES", "stop_loss", "stop", False, 1, T("10:00"), 4, stop_price=4990.0), T("10:00"))


def test_n1_quantity_tampering_after_submission_is_refused(tmp_path):
    s, o = entry_after_signal(tmp_path)
    o.quantity = 20
    feed(s, path(T("10:05:01"), 1, FLAT))
    assert o.state is St.REJECTED and s.account.position_qty("MES") == 0
    assert o.reason == "entry_terms_changed_after_authorization"  # refused before any other check


# ----------------------------------------------------------------- N10: refusal after a partial fill
def test_n10_fill_refusal_after_a_partial_fill_cancels_the_rest_without_crashing():
    b = PaperBroker(BT, SPECS, participation=0.5, entry_token_check=lambda o: True)
    o = b.submit(PaperOrder("N1", "MES", "entry", "limit", True, 4, T("10:00"), 0, limit_price=5000.0,
                            stop_loss=4990.0), T("10:00"))
    b.on_event(trade(T("10:00:01"), 5001.0), 1, lambda r: 0, lambda *a: FillDecision(True))  # posts
    ex = b.on_event(trade(T("10:00:02"), 4999.0, size=4.0), 2, lambda r: 0, lambda *a: FillDecision(True))
    assert o.state is St.PARTIALLY_FILLED and ex[0].quantity == 2
    ex2 = b.on_event(trade(T("10:00:03"), 4998.0, size=4.0), 3, lambda r: 2,
                     lambda *a: FillDecision(False, "news_blackout_active"))
    assert ex2 == [] and o.state is St.CANCELLED and o.filled_qty == 2
    assert o.reason == "remainder_refused:news_blackout_active"


# ----------------------------------------------------------------- N3: wide quotes
def test_n3_a_wide_quote_never_prices_a_fill(tmp_path):
    s, o = entry_after_signal(tmp_path)
    feed(s, [quote(T("10:05:01"), 4960.0, 5040.0)])          # 320-tick spread, mid unchanged
    assert s.validator.rejected.get("spread_too_wide") == 1 and o.filled_qty == 0
    feed(s, [trade(T("10:05:02"), 5000.0)])
    assert o.avg_fill_price == 5000.0 + BT.slippage.market_ticks * 0.25


def test_n3_every_price_of_an_event_is_checked_against_the_reference():
    v = EventValidator(SPECS, APP.session, max_jump_ticks=40, max_spread_ticks=400)
    assert v.check(trade(T("10:00"), 5000.0)).ok
    r = v.check(quote(T("10:00:01"), 4980.0, 5020.0))  # mid 5000, but both sides 80 ticks away
    assert not r.ok and r.reason == "implausible_price_jump"


# ----------------------------------------------------------------- N4: malformed events
@pytest.mark.parametrize("ev,reason", [
    (MarketEvent("i", "MES", "trade", T("10:01"), float("inf"), 1.0), "non_finite_number"),
    (MarketEvent("n", "MES", "trade", T("10:01"), float("nan"), 1.0), "non_finite_number"),
    (MarketEvent("s", "MES", "trade", T("10:01"), 5000.0, float("nan")), "non_finite_number"),
    (MarketEvent("z", "MES", "trade", T("10:01"), 5000.0, None), "non_finite_number"),
    (MarketEvent("q", "MES", "quote", T("10:01"), None, 0.0, 4999.75, 5000.0, float("nan"), 1.0),
     "non_finite_number"),
    (MarketEvent("k", "MES", "bogus", T("10:01"), None, 1.0), "unknown_event_kind"),
    (MarketEvent("t", "MES", "trade", None, 5000.0, 1.0), "missing_timestamp"),
])
def test_n4_malformed_events_are_rejected_not_fatal(tmp_path, ev, reason):
    s = started(tmp_path)
    feed(s, path(T("10:00"), 1, FLAT))
    seq = s.seq
    s.process_event(ev)
    assert s.seq == seq and s.validator.rejected.get(reason) == 1
    s._commit()
    assert [e["payload"]["reason"] for e in s.journal.entries("data_rejected")] == [reason]


# ----------------------------------------------------------------- N5: future timestamps (real-time feeds)
def test_n5_future_stamped_event_cannot_blind_a_realtime_session(tmp_path):
    clock = {"t": T("10:00")}
    s = started(tmp_path, pcfg=replace(PCFG, mode="paper"), feed_cls=LiveLikeFeed, clock=lambda: clock["t"])
    s.process_event(trade(T("10:00"), 5000.0))
    s.process_event(trade(T("10:00", "2026-01-01"), 5000.0))   # bad clock on the provider side
    clock["t"] = T("10:00:05")
    s.process_event(trade(T("10:00:05"), 4999.0))
    assert s.validator.rejected == {"timestamp_in_future": 1}
    assert s.account.marks["MES"] == 4999.0  # the genuine later event is used
    assert s.last_event_ts == T("10:00:05")


# ----------------------------------------------------------------- N6: emptied state file
def test_n6_emptied_journal_is_not_restarted_as_a_fresh_account(tmp_path):
    rules = guard_rules(max_drawdown=300.0, safety_buffer=100.0, flatten_buffer=50.0)
    s = started(tmp_path, {T("10:05"): [long(stop=4900.0)]}, btc=bt(account_rules=rules))
    feed(s, path(T("10:00"), 6, FLAT) + path(T("10:06"), 3, lambda i: 4930.0))
    assert s.account.guard.breached
    s.journal.close()
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("DELETE FROM journal")
    db.execute("DELETE FROM snapshot")
    db.commit()
    db.close()
    with pytest.raises(RecoveryError, match="empty journal"):
        session(tmp_path, [], {}, create=False, btc=bt(account_rules=rules))


# ----------------------------------------------------------------- N7: failures outside event processing
def test_n7_feed_exception_ends_in_a_safe_shutdown(tmp_path):
    class Broken(ListFeed):
        def poll(self, max_events=1000):
            raise ValueError("decoder bug")
    s = session(tmp_path, [], {}, feed_cls=Broken)
    assert s.run() == "system_error"
    kinds = [e["kind"] for e in s.journal.entries()]
    assert "system_error" in kinds and "shutdown" in kinds and not s.accepting_entries


def test_n7_failure_between_fill_and_bracket_leaves_the_position_protected(tmp_path):
    s = session(tmp_path, path(T("10:00"), 10, FLAT), {T("10:05"): [long(stop=4990.0)]})
    real = s.broker.on_execution

    def crash_on_entry(ex, *a, **k):
        if ex.role == "entry":
            raise RuntimeError("crash before the bracket was placed")
        return real(ex, *a, **k)
    s.broker.on_execution = crash_on_entry
    assert s.run() == "system_error"
    assert s.account.position_qty("MES") == 1
    (stop,) = s.broker.working("MES", ("stop_loss",))
    assert stop.stop_price == 4990.0 and stop.quantity == 1
    assert list(s.journal.entries("protective_stop_restored"))


def test_n7_partially_protected_position_gets_the_missing_cover(tmp_path):
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0)]})
    feed(s, path(T("10:00"), 7, FLAT))
    s.account.positions["MES"].quantity = 3  # e.g. interrupted resize of the bracket
    s.account.open_trades["MES"].quantity = 3
    s._protect_restored_positions(T("10:07"))
    assert sum(o.remaining for o in s.broker.working("MES", ("stop_loss",))) == 3


# ----------------------------------------------------------------- N2: daily loss with an unmarked instrument
def test_n2_daily_loss_limit_still_flattens_with_an_unmarked_instrument(tmp_path):
    risk = RiskConfig(max_daily_loss=100.0, daily_loss_basis="realized_and_unrealized", daily_loss_action="flatten")
    evs = sorted(path(T("10:00"), 6, FLAT, "MES") + path(T("10:00"), 6, lambda i: 20000.0, "MNQ"),
                 key=lambda e: (e.ts, e.root))
    s = started(tmp_path, {"MES": {T("10:05"): [long("m", stop=4900.0)]},
                           "MNQ": {T("10:05"): [long("n", stop=19900.0)]}},
                pcfg=replace(PCFG, instruments=("MES", "MNQ")), news=allow_news(("MES", "MNQ")), btc=bt(risk=risk))
    feed(s, evs)
    assert s.account.position_qty("MES") == 1 and s.account.position_qty("MNQ") == 1
    s.account.marks.pop("MNQ")
    feed(s, path(T("10:06"), 2, lambda i: 4970.0, "MES"))  # -150 USD on MES alone
    assert s.account.position_qty("MES") == 0
    assert any(e["payload"].get("reason") == "daily_loss_limit_partial_equity"
               for e in s.journal.entries("flatten_requested"))


# ----------------------------------------------------------------- N8: protection state reconciliation
def _rewrite_snapshot(db_path, mutate):
    import hashlib
    db = sqlite3.connect(db_path)
    sid, jseq, body = db.execute("SELECT id, journal_seq, state FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
    jhash = db.execute("SELECT hash FROM journal WHERE seq=?", (jseq,)).fetchone()[0]
    st = json.loads(body)
    mutate(st)
    b2 = json.dumps(st, sort_keys=True, separators=(",", ":"), default=str)
    db.execute("UPDATE snapshot SET state=?, hash=? WHERE id=?",
               (b2, hashlib.sha256(f"SNAPSHOT|{jhash}|{jseq}|{b2}".encode()).hexdigest(), sid))
    db.commit()
    db.close()


def _recovery_problems(tmp_path, **kw):
    s2 = session(tmp_path, [], {}, create=False, **kw)
    return s2, [e["payload"] for e in s2.journal.entries("recovered")][-1]["reconciliation_problems"]


def test_n8_erased_emergency_stop_is_detected(tmp_path):
    s = started(tmp_path)
    feed(s, path(T("10:00"), 2, FLAT))
    s.emergency_stop("operator")
    s.journal.close()
    _rewrite_snapshot(tmp_path / "state.db", lambda st: st["account"]["risk"].update(emergency_reason=None))
    s2, problems = _recovery_problems(tmp_path)
    assert not s2.reconciliation_ok and any("emergency stop" in p for p in problems)


def test_n8_reset_daily_trade_count_is_detected(tmp_path):
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0)]}, btc=bt(risk=RiskConfig(max_daily_trades=1)))
    feed(s, path(T("10:00"), 7, FLAT))
    s.journal.close()
    _rewrite_snapshot(tmp_path / "state.db", lambda st: st["account"]["risk"].update(days={}))
    s2, problems = _recovery_problems(tmp_path, btc=bt(risk=RiskConfig(max_daily_trades=1)))
    assert not s2.reconciliation_ok and any("RiskManager daily state" in p for p in problems)


def test_n8_erased_daily_loss_breach_is_detected(tmp_path):
    risk = RiskConfig(max_daily_loss=40.0, daily_loss_basis="realized_and_unrealized", daily_loss_action="block_entries")
    s = started(tmp_path, {T("10:05"): [long(stop=4900.0)]}, btc=bt(risk=risk))
    feed(s, path(T("10:00"), 6, FLAT) + path(T("10:06"), 2, lambda i: 4990.0))
    assert s.account.risk.daily_loss_breached(SD)
    s.journal.close()

    def unmark(st):
        for d in st["account"]["risk"]["days"].values():
            d["loss_limit_breached"] = False
    _rewrite_snapshot(tmp_path / "state.db", unmark)
    s2, problems = _recovery_problems(tmp_path, btc=bt(risk=risk))
    assert any("daily-loss breach" in p for p in problems)


def test_n8_lowered_trailing_peak_is_detected(tmp_path):
    rules = guard_rules(drawdown_type="trailing_intraday", max_drawdown=1000.0, safety_buffer=100.0,
                        flatten_buffer=50.0)
    s = started(tmp_path, {T("10:05"): [long(stop=4900.0)]}, btc=bt(account_rules=rules))
    feed(s, path(T("10:00"), 6, FLAT) + path(T("10:06"), 3, lambda i: 5040.0))
    peak = s.account.guard.peak
    assert peak > 10_000
    s.journal.close()
    _rewrite_snapshot(tmp_path / "state.db", lambda st: st["account"]["guard"].update(peak=10_000.0))
    s2, problems = _recovery_problems(tmp_path, btc=bt(account_rules=rules))
    assert any("peak" in p for p in problems)


def test_n8_clean_restart_still_reconciles_without_problems(tmp_path):
    rules = guard_rules(drawdown_type="trailing_intraday", max_drawdown=1000.0, safety_buffer=100.0)
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0, target=5010.0)]},
                btc=bt(account_rules=rules, risk=RiskConfig(max_daily_trades=3)))
    feed(s, path(T("10:00"), 6, FLAT) + path(T("10:06"), 3, lambda i: 5015.0))
    assert s.account.closed_trades
    s.journal.close()
    s2, problems = _recovery_problems(tmp_path, btc=bt(account_rules=rules, risk=RiskConfig(max_daily_trades=3)))
    assert problems == [] and s2.reconciliation_ok


# ----------------------------------------------------------------- N9: level confirmation needs time
def test_n9_a_same_timestamp_burst_cannot_confirm_a_new_level():
    v = EventValidator(SPECS, APP.session, max_jump_ticks=40)
    assert v.check(trade(T("10:00"), 5000.0)).ok
    burst = [v.check(trade(T("10:00:01"), 5100.0)) for _ in range(10)]
    assert not any(r.ok for r in burst)
    assert v.check(trade(T("10:00:02"), 5100.0)).ok is False
    r = v.check(trade(T("10:00:03"), 5100.0))
    assert r.ok and "price_level_reset" in r.warnings  # three distinct timestamps


# ----------------------------------------------------------------- N11: journal key handling
def test_n11_weak_or_wrong_key_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "short")
    with pytest.raises(JournalError, match="weak key"):
        Journal(tmp_path / "a.db", create=True)
    monkeypatch.setenv(KEY_ENV, "first-key-0123456789abcdef")
    Journal(tmp_path / "b.db", create=True).close()
    monkeypatch.setenv(KEY_ENV, "other-key-0123456789abcdef")
    with pytest.raises(JournalError, match="does not match"):
        Journal(tmp_path / "b.db")


def test_n11_state_directory_and_files_are_owner_only(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    j = Journal(tmp_path / "new" / "state.db", create=True)
    j.append("x", T("10:00"), {})
    j.commit()
    assert stat.S_IMODE(os.stat(tmp_path / "new").st_mode) == 0o700
    for f in os.listdir(tmp_path / "new"):
        assert stat.S_IMODE(os.stat(tmp_path / "new" / f).st_mode) == 0o600


# ----------------------------------------------------------------- N12: reporting
def test_n12_take_profit_exit_is_classified_as_take_profit(tmp_path):
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0, target=5010.0)]})
    feed(s, path(T("10:00"), 6, FLAT) + path(T("10:06"), 2, lambda i: 5012.0))
    (t,) = s.account.closed_trades
    assert t["exit_role"] == "take_profit" and t["exit_reason"] == "take_profit"


def test_n12_report_file_is_standard_json(tmp_path):
    import run_paper
    rep = {"equity": float("nan"), "pf": float("inf"), "nested": [{"r": float("-inf")}]}
    assert json.loads(json.dumps(run_paper._json_safe(rep), allow_nan=False)) == \
        {"equity": None, "pf": None, "nested": [{"r": None}]}


# ----------------------------------------------------------------- re-audit of the fixes
def test_market_entry_decided_just_before_the_cutoff_expires_at_the_cutoff(tmp_path):
    s = started(tmp_path, btc=bt(risk=RiskConfig()))  # no trading windows: isolate the cutoff
    cutoff = s.flatten_time(SD)
    feed(s, path(cutoff - pd.Timedelta(minutes=3), 2, FLAT))
    from src.backtest.orders import OrderType
    req = long(stop=4990.0)
    decided = cutoff - pd.Timedelta(seconds=1)
    assert decided + BT.execution.max_fill_delay > cutoff
    s._entry("MES", req, trade(decided, 5000.0), SD, 5000.0, None)
    (o,) = s.broker.working("MES", ("entry",))
    assert o.expire_at == cutoff and req.order_type is OrderType.MARKET


def test_restored_stop_without_oco_still_wins_an_ambiguous_synthetic_bar():
    b = PaperBroker(BT, SPECS)
    b.submit(PaperOrder("S1", "MES", "stop_loss", "stop", False, 1, T("10:00"), 0, stop_price=4990.0), T("10:00"))
    b.submit(PaperOrder("T1", "MES", "take_profit", "limit", False, 1, T("10:00"), 0, limit_price=5010.0,
                        oco_group="OCO-X", posted=True), T("10:00"))
    hi = MarketEvent("b-2", "MES", "trade", T("10:01:20"), 5012.0, 5.0, synthetic_from_bars=True, bar_high=5012.0,
                     bar_low=4985.0)
    lo = MarketEvent("b-3", "MES", "trade", T("10:01:40"), 4985.0, 5.0, synthetic_from_bars=True, bar_high=5012.0,
                     bar_low=4985.0)
    assert b.on_event(hi, 1, lambda r: 1, lambda *a: FillDecision(True)) == []
    (ex,) = b.on_event(lo, 2, lambda r: 1, lambda *a: FillDecision(True))
    assert ex.role == "stop_loss"
