"""Regression tests for the Sprint 5 paper-trading audit (commit 61a2a7a).

Each test reproduces a vulnerability found by the audit and asserts the fixed, fail-closed behaviour.
"""

import hashlib
import json
import sqlite3
from dataclasses import replace

import pandas as pd
import pytest

from src.paper.broker import FillDecision, PaperBroker
from src.paper.events import MarketEvent
from src.paper.feeds import bars_to_events
from src.paper.journal import KEY_ENV, Journal, JournalError
from src.paper.metrics import paper_report
from src.paper.orders import LifecycleError, PaperOrder, PaperOrderState as St
from src.paper.validation import EventValidator
from tests.paper_helpers import APP, BT, PCFG, T, allow_news, bt, guard_rules, long, path, quote, session, trade

FLAT = lambda i: 5000.0  # noqa: E731
SPECS = {"MES": APP.contracts["MES"]}


def feed(s, events):
    for e in events:
        s.process_event(e)
    s._commit()  # journal reads see committed rows only


def started(tmp_path, script=None, **kw):
    s = session(tmp_path, [], script or {}, **kw)
    s.connect()
    return s


# ----------------------------------------------------------------- H2: genuine level change blinded the system
def test_single_price_outlier_is_still_rejected():
    v = EventValidator(SPECS, APP.session, max_jump_ticks=40)
    assert v.check(trade(T("10:00"), 5000.0)).ok
    assert v.check(trade(T("10:00:01"), 5100.0)).reason == "implausible_price_jump"
    assert v.check(trade(T("10:00:02"), 5000.25)).ok  # back to normal: the outlier never became a reference


def test_confirmed_new_price_level_is_accepted_and_protective_stop_fires(tmp_path):
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0)]}, pcfg=replace(PCFG, max_price_jump_ticks=40))
    feed(s, path(T("10:00"), 6, FLAT))
    assert s.account.position_qty("MES") == 1
    feed(s, path(T("10:06"), 2, lambda i: 4900.0, quotes=False))  # real gap down of 400 ticks
    assert s.validator.rejected.get("implausible_price_jump") == 2      # two events to confirm the level
    assert s.account.position_qty("MES") == 0                           # the stop-loss fired at the new level
    stop = [e["payload"] for e in s.journal.entries("execution")][-1]
    assert stop["role"] == "stop_loss" and stop["fill_price"] <= 4900.0  # gap: filled at the market, not the stop
    assert list(s.journal.entries("price_level_reset"))


def test_price_level_reset_requires_recovery_before_entries(tmp_path):
    s = started(tmp_path, pcfg=replace(PCFG, max_price_jump_ticks=40))
    feed(s, path(T("10:00"), 2, FLAT, quotes=False))
    feed(s, [trade(T("10:02") + pd.Timedelta(seconds=i), 5100.0) for i in range(3)])
    assert s.health.healthy("MES", T("10:02:03"))[0] is False  # entries wait for fresh confirmation


# ----------------------------------------------------------------- H3: fills on stale quotes
def test_market_entry_never_fills_on_a_stale_quote(tmp_path):
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0)]})
    feed(s, [quote(T("10:00"), 4999.75, 5000.0)] +
         path(T("10:00:01"), 6, lambda i: 5000.0 if i < 20 else 5020.0, quotes=False))
    ex = [e["payload"] for e in s.journal.entries("execution") if e["payload"]["role"] == "entry"]
    assert ex and ex[0]["reference_price"] == 5020.0 and not ex[0]["quote_based"]  # the actual trade, not 5000


def test_fresh_quote_is_still_used():
    b = PaperBroker(BT, SPECS, entry_token_check=lambda o: True)
    b.submit(PaperOrder(b.new_id(), "MES", "entry", "market", True, 1, T("10:00"), 0), T("10:00"))
    b.on_event(quote(T("10:00:01"), 4999.75, 5000.0), 1, lambda r: 0, lambda *a: FillDecision(True))
    (o,) = b.orders.values()
    assert o.state is St.FILLED and o.avg_fill_price == 5000.0 + BT.slippage.market_ticks * 0.25


def test_stop_does_not_fill_at_a_stale_bid():
    b = PaperBroker(BT, SPECS, quote_max_age=pd.Timedelta(seconds=5))
    b.on_event(quote(T("10:00"), 5000.0, 5000.25), 1, lambda r: 1, lambda *a: FillDecision(True))
    b.submit(PaperOrder(b.new_id(), "MES", "stop_loss", "stop", False, 1, T("10:00"), 1, stop_price=4990.0),
             T("10:00"))
    ex = b.on_event(trade(T("10:01"), 4980.0), 2, lambda r: 1, lambda *a: FillDecision(True))
    assert ex[0].reference_price == 4980.0  # the gap price, not the minute-old 5000 bid


# ----------------------------------------------------------------- M1: synthetic bar paths
def _bars(o, h, l, c, n=10, start="10:00"):
    idx = pd.date_range(T(start), periods=n, freq="1min")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 40.0}, index=idx)


def test_bar_replay_market_entry_fills_at_the_next_bar_open_not_its_extreme(tmp_path):
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0)]})
    feed(s, list(bars_to_events(_bars(5000.0, 5003.0, 4999.0, 4999.5), "MES")))
    o = next(o for o in s.broker.orders.values() if o.role == "entry")
    assert o.avg_fill_price == 5000.0 + BT.slippage.market_ticks * 0.25  # was 5003.25 (bar high)


def test_stop_and_target_inside_one_synthetic_bar_resolve_stop_first(tmp_path):
    bars = pd.concat([_bars(5000.0, 5001.0, 4999.0, 5000.5, n=6),
                      _bars(5000.0, 5012.0, 4985.0, 5011.0, n=2, start="10:06")])  # up bar: path hits the low first
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0, target=5010.0)]})
    feed(s, list(bars_to_events(bars, "MES")))
    (t,) = s.account.closed_trades
    assert t["exit_role"] == "stop_loss" and t["net_pnl"] < 0
    # a DOWN bar visits the high first: the old path would have taken the target before the stop
    bars2 = pd.concat([_bars(5000.0, 5001.0, 4999.0, 5000.5, n=6),
                       _bars(5000.0, 5012.0, 4985.0, 4986.0, n=2, start="10:06")])
    s2 = started(tmp_path / "b", {T("10:05"): [long(stop=4990.0, target=5010.0)]})
    feed(s2, list(bars_to_events(bars2, "MES")))
    (t2,) = s2.account.closed_trades
    assert t2["exit_role"] == "stop_loss"


def test_report_labels_synthetic_executions(tmp_path):
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0)]})
    feed(s, list(bars_to_events(_bars(5000.0, 5001.0, 4999.0, 5000.5), "MES")))
    rep = paper_report(s)
    assert rep["execution_basis"]["from_synthetic_bar_paths"] == rep["execution_basis"]["executions"] >= 1
    assert "stop-first" in rep["execution_basis"]["note"]
    assert rep["open_positions_excluded_from_trade_stats"] == {"MES": 1}


# ----------------------------------------------------------------- M4: unmarked instrument disabled AccountGuard
def test_unmarked_instrument_does_not_disable_account_protection(tmp_path):
    rules = guard_rules(max_drawdown=300.0, safety_buffer=100.0, flatten_buffer=100.0)
    evs = sorted(path(T("10:00"), 6, FLAT, "MES") + path(T("10:00"), 6, lambda i: 20000.0, "MNQ"),
                 key=lambda e: (e.ts, e.root))
    s = started(tmp_path, {"MES": {T("10:05"): [long("m", stop=4900.0)]},
                           "MNQ": {T("10:05"): [long("n", stop=19900.0)]}},
                pcfg=replace(PCFG, instruments=("MES", "MNQ")), news=allow_news(("MES", "MNQ")),
                btc=bt(account_rules=rules))
    feed(s, evs)
    assert s.account.position_qty("MES") == 1 and s.account.position_qty("MNQ") == 1
    s.account.marks.pop("MNQ")  # e.g. after a restart: no MNQ price yet
    feed(s, path(T("10:06"), 2, lambda i: 4940.0, "MES"))  # -300 USD on MES alone
    assert s.account.position_qty("MES") == 0
    assert any(e["payload"].get("reason") == "account_protection_partial_equity"
               for e in s.journal.entries("flatten_requested"))
    assert not s.account.guard.breached  # no breach recorded from partial information


# ----------------------------------------------------------------- M5: direct API bypass of the EntryAuthorizer
def test_broker_refuses_entries_without_an_authorization_token():
    b = PaperBroker(BT, SPECS)
    with pytest.raises(LifecycleError, match="not issued by the EntryAuthorizer"):
        b.submit(PaperOrder("Z1", "MES", "entry", "market", True, 1, T("10:00"), 0), T("10:00"))
    assert not b.orders


def test_direct_entry_submission_in_a_session_is_refused(tmp_path):
    s = started(tmp_path, pcfg=replace(PCFG, mode="infrastructure_validation"))
    feed(s, path(T("10:00"), 1, FLAT))
    with pytest.raises(LifecycleError):
        s.broker.submit(PaperOrder("Z1", "MES", "entry", "market", True, 1, T("10:01"), s.seq,
                                   session_date=pd.Timestamp("2025-03-10")), T("10:01"))
    feed(s, path(T("10:01"), 1, FLAT))
    assert s.account.position_qty("MES") == 0


def _approved_entry(s, oid, **over):
    """An entry built from a REAL approval of authorize_signal (the only way to obtain a token)."""
    now, sd = s.last_event_ts, pd.Timestamp("2025-03-10")
    d, info = s.authorizer.authorize_signal(long(stop=4990.0), "MES", now, sd, 5000.0)
    assert d.allowed, d.reason
    kw = dict(stop_loss=info["stop"], take_profit=info["target"], risk_budget=info["risk_budget"], session_date=sd,
              expire_at=now + pd.Timedelta(minutes=1), auth_token=info["token"])
    return PaperOrder(oid, "MES", "entry", "market", True, info["quantity"], now, s.seq, **(kw | over))


def test_authorized_entry_with_altered_terms_or_reused_token_is_refused(tmp_path):
    s = started(tmp_path)
    feed(s, path(T("10:00"), 6, FLAT))
    o = _approved_entry(s, "N9")
    o.quantity += 5  # tampered after authorization
    with pytest.raises(LifecycleError):
        s.broker.submit(o, s.last_event_ts)
    o2 = _approved_entry(s, "N10")
    s.broker.submit(o2, s.last_event_ts)
    o3 = replace(o2, order_id="N11", history=[])  # replay of the consumed token with identical terms
    with pytest.raises(LifecycleError):
        s.broker.submit(o3, s.last_event_ts)


def test_fill_authorization_rechecks_mode_and_session_cutoff(tmp_path):
    s = started(tmp_path, pcfg=replace(PCFG, mode="infrastructure_validation"))
    feed(s, path(T("10:00"), 1, FLAT))
    o = PaperOrder("N1", "MES", "entry", "market", True, 1, T("10:00"), 0, stop_loss=4990.0,
                   session_date=pd.Timestamp("2025-03-10"))
    d, _ = s.authorizer.authorize_fill(o, 5000.0, 1, trade(T("10:01"), 5000.0))
    assert not d.allowed and d.reason == "infrastructure_validation_mode"
    s2 = started(tmp_path / "b")
    sd = pd.Timestamp("2025-03-10")
    cutoff = s2.flatten_time(sd)
    feed(s2, path(cutoff - pd.Timedelta(minutes=1), 1, FLAT))
    d2, _ = s2.authorizer.authorize_fill(o, 5000.0, 1, trade(cutoff, 5000.0))
    assert not d2.allowed and d2.reason == "entry_cutoff_reached_at_fill"


def test_order_expires_exactly_at_its_expiry_time():
    b = PaperBroker(BT, SPECS, entry_token_check=lambda o: True)
    b.submit(PaperOrder("N1", "MES", "entry", "limit", True, 1, T("10:00"), 0, limit_price=4990.0,
                        expire_at=T("10:05")), T("10:00"))
    b.on_event(trade(T("10:05"), 4980.0), 1, lambda r: 0, lambda *a: FillDecision(True))
    assert b.orders["N1"].state is St.EXPIRED and b.orders["N1"].filled_qty == 0


# ----------------------------------------------------------------- H1: never over-close within one event
def test_two_exit_orders_on_one_event_never_reverse_the_position():
    b = PaperBroker(BT, SPECS)
    b.submit(PaperOrder("S1", "MES", "stop_loss", "stop", False, 1, T("10:00"), 0, stop_price=4990.0), T("10:00"))
    b.submit(PaperOrder("E1", "MES", "exit", "market", False, 1, T("10:00"), 0), T("10:00"))
    ex = b.on_event(trade(T("10:01"), 4985.0), 1, lambda r: 1, lambda *a: FillDecision(True))  # stale callback
    assert sum(e.quantity for e in ex) == 1  # one closes, the other is cancelled (position already flat)


# ----------------------------------------------------------------- M3: duplicate events after a restart
def test_duplicate_event_after_restart_is_rejected(tmp_path):
    s = started(tmp_path)
    last = trade(T("10:00"), 5000.0, eid="dup-1")
    feed(s, [trade(T("09:59:59"), 5000.0), last])
    s._commit()
    s.journal.close()
    s2 = session(tmp_path, [], {}, create=False)
    s2.connect()
    seq = s2.seq
    s2.process_event(MarketEvent("dup-1", "MES", "trade", T("10:00"), 5000.0, 5.0, source="test"))
    assert s2.seq == seq and s2.validator.rejected.get("duplicate_event") == 1


# ----------------------------------------------------------------- M6: internal failure must fail closed
def test_internal_error_stops_entries_journals_and_shuts_down(tmp_path):
    s = session(tmp_path, path(T("10:00"), 10, FLAT), {T("10:05"): [long()]})

    def boom(*a, **k):
        raise RuntimeError("simulated internal failure")
    s._risk_checks = boom
    assert s.run() == "system_error"
    k = [e["kind"] for e in s.journal.entries()]
    assert "system_error" in k and "shutdown" in k
    assert not s.accepting_entries and s.account.position_qty("MES") == 0


# ----------------------------------------------------------------- M7: restored position without a stop
def test_restored_position_without_stop_gets_its_stop_back(tmp_path):
    s = started(tmp_path, {T("10:05"): [long(stop=4990.0)]})
    feed(s, path(T("10:00"), 7, FLAT))
    assert s.account.position_qty("MES") == 1
    for o in s.broker.working("MES", ("stop_loss",)):
        o.state = St.CANCELLED  # simulate a lost protective order in the stored state
    s._commit()
    s.journal.close()
    s2 = session(tmp_path, [], {}, create=False)
    assert not s2.reconciliation_ok  # still reported
    (stop,) = s2.broker.working("MES", ("stop_loss",))
    assert stop.stop_price == 4990.0 and stop.quantity == 1 and not stop.is_buy
    assert list(s2.journal.entries("protective_stop_restored"))


# ----------------------------------------------------------------- M2: snapshot integrity / protection state
def _rewrite_snapshot(db_path, mutate):
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


def test_reset_drawdown_breach_in_snapshot_is_detected(tmp_path):
    rules = guard_rules(max_drawdown=300.0, safety_buffer=100.0, flatten_buffer=50.0)
    s = started(tmp_path, {T("10:05"): [long(stop=4900.0, quantity=1)]}, btc=bt(account_rules=rules))
    feed(s, path(T("10:00"), 6, FLAT) + path(T("10:06"), 2, lambda i: 4930.0))
    feed(s, path(T("10:08"), 1, lambda i: 4930.0))
    assert s.account.guard.breached
    s._commit()
    s.journal.close()

    def unbreach(st):
        st["account"]["guard"]["breached"] = False
        st["account"]["guard"]["locked_permanently"] = False
    _rewrite_snapshot(tmp_path / "state.db", unbreach)
    s2 = session(tmp_path, [], {}, create=False, btc=bt(account_rules=rules))
    assert not s2.reconciliation_ok
    rec = [e["payload"] for e in s2.journal.entries("recovered")][0]
    assert any("breach" in p for p in rec["reconciliation_problems"])


def test_snapshot_cannot_be_moved_to_another_journal(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        s = started(d)
        feed(s, path(T("10:00"), 1, FLAT))
        s._commit()
        s.journal.close()
    da, db_ = sqlite3.connect(a / "state.db"), sqlite3.connect(b / "state.db")
    row = da.execute("SELECT journal_seq, ts, state, hash FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
    jseq_b = db_.execute("SELECT MAX(seq) FROM journal").fetchone()[0]
    db_.execute("INSERT INTO snapshot (journal_seq, ts, state, hash) VALUES (?,?,?,?)", (jseq_b, *row[1:]))
    db_.commit()
    for c in (da, db_):
        c.close()
    with pytest.raises(JournalError, match="not bound to this journal"):
        session(b, [], {}, create=False)


def test_keyed_snapshot_cannot_be_recomputed_without_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "audit-test-key-0123456789")
    s = started(tmp_path)
    feed(s, path(T("10:00"), 1, FLAT))
    s._commit()
    s.journal.close()
    _rewrite_snapshot(tmp_path / "state.db", lambda st: st["account"].update(realized_gross=500.0))
    with pytest.raises(JournalError, match="snapshot is corrupted"):
        session(tmp_path, [], {}, create=False)


def test_unkeyed_journal_is_refused_when_a_key_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    Journal(tmp_path / "j.db", create=True).close()
    monkeypatch.setenv(KEY_ENV, "audit-test-key-0123456789")
    with pytest.raises(JournalError, match="NOT HMAC-protected"):
        Journal(tmp_path / "j.db")

