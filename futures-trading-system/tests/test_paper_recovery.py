"""Sprint 5: persistence and recovery -- atomic batches, crash/restart, corruption, tampering,
reconciliation, persistence failures, never resetting drawdown state."""

import hashlib
import json
import sqlite3
from dataclasses import replace

import pandas as pd
import pytest

from src.paper.journal import Journal, JournalError
from src.paper.runtime import RecoveryError
from tests.paper_helpers import PCFG, T, bt, guard_rules, long, path, session

FLAT = lambda i: 5000.0  # noqa: E731
EVS = path(T("10:00"), 40, FLAT)


def first_part(tmp_path, upto, script=None, **kw):
    """Run until ``upto`` (exclusive), commit, then 'crash' (no shutdown)."""
    s = session(tmp_path, EVS, script if script is not None else {T("10:05"): [long()]}, **kw)
    s.connect()
    for ev in s.feed.poll(100_000):
        if ev.ts >= upto:
            break
        s.process_event(ev)
    s._commit()
    s.journal.close()
    return s


def resume(tmp_path, script=None, start=T("10:20"), **kw):
    later = [e for e in EVS if e.ts >= start]
    return session(tmp_path, later, script or {}, create=False, **kw)


def test_crash_restart_restores_positions_protection_and_blocks_session_entries(tmp_path):
    rules = guard_rules(drawdown_type="trailing_intraday", max_drawdown=1000.0, safety_buffer=200.0)
    s1 = first_part(tmp_path, T("10:12"), {T("10:05"): [long("a")], T("10:10"): [long("p", order_type="limit",
                                                                                         price=4990.0)]},
                    btc=bt(account_rules=rules))
    assert s1.account.position_qty("MES") == 1
    peak = s1.account.guard.peak
    s2 = resume(tmp_path, {T("10:25"): [long("late")]}, btc=bt(account_rules=rules))
    rec = [e["payload"] for e in s2.journal.entries("recovered")][0]
    assert rec["clean_shutdown_before"] is False and rec["reconciliation_problems"] == []
    assert s2.account.position_qty("MES") == 1 and s2.broker.working("MES", ("stop_loss",))
    assert s2.account.guard.peak == peak  # never reset
    s2.run()
    assert s2.decisions[-1]["reason"] == "restart_strategy_state_unverified"


def test_uncommitted_batch_is_lost_atomically(tmp_path):
    s = session(tmp_path, EVS, {T("10:05"): [long()]})
    s.connect()
    for ev in s.feed.poll(100_000):
        if ev.ts >= T("10:08"):
            break
        s.process_event(ev)
    assert s.account.position_qty("MES") == 1  # in memory only
    s.journal.close()  # crash before commit
    s2 = resume(tmp_path)
    assert s2.account.position_qty("MES") == 0 and not list(s2.journal.entries("execution"))
    assert s2.reconciliation_ok


def test_corrupted_or_tampered_journal_is_refused(tmp_path):
    first_part(tmp_path, T("10:12"))
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("UPDATE journal SET payload = replace(payload, '5000', '4000') WHERE kind = 'execution'")
    db.commit()
    db.close()
    with pytest.raises(JournalError, match="hash chain broken"):
        resume(tmp_path)


def test_tampered_snapshot_is_refused(tmp_path):
    first_part(tmp_path, T("10:12"))
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("UPDATE snapshot SET state = replace(state, '\"realized_gross\":0.0', '\"realized_gross\":1.0')")
    db.commit()
    db.close()
    with pytest.raises(JournalError, match="snapshot is corrupted"):
        resume(tmp_path)


def test_consistent_looking_but_wrong_snapshot_fails_reconciliation(tmp_path):
    first_part(tmp_path, T("10:12"))
    db = sqlite3.connect(tmp_path / "state.db")
    sid, body = db.execute("SELECT id, state FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
    st = json.loads(body)
    st["account"]["positions"]["MES"]["quantity"] = 3  # bug or tampering with a recomputed hash
    body2 = json.dumps(st, sort_keys=True, separators=(",", ":"), default=str)
    db.execute("UPDATE snapshot SET state=?, hash=? WHERE id=?", (body2, hashlib.sha256(body2.encode()).hexdigest(), sid))
    db.commit()
    db.close()
    s2 = resume(tmp_path, {T("10:25"): [long()]}, start=T("10:20"))
    assert not s2.reconciliation_ok
    s2.restart_block_session = None  # isolate the reconciliation block
    s2.run()
    assert s2.decisions[-1]["reason"] == "reconciliation_failed"
    rec = [e["payload"] for e in s2.journal.entries("recovered")][0]
    assert any("MES: position 3" in p for p in rec["reconciliation_problems"])


def test_snapshot_behind_the_journal_is_refused(tmp_path):
    first_part(tmp_path, T("10:12"))
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("DELETE FROM snapshot WHERE id = (SELECT MAX(id) FROM snapshot)")
    db.commit()
    db.close()
    with pytest.raises(RecoveryError, match="does not match"):
        resume(tmp_path)


def test_missing_account_is_never_recreated_silently(tmp_path):
    with pytest.raises(JournalError, match="no paper account"):
        Journal(tmp_path / "none.db")
    first_part(tmp_path, T("10:02"))
    with pytest.raises(JournalError, match="already exists"):
        Journal(tmp_path / "state.db", create=True)


def test_breach_survives_restart(tmp_path):
    rules = guard_rules(max_drawdown=300.0, safety_buffer=200.0, flatten_buffer=100.0)
    s1 = first_part(tmp_path, T("10:03"), {}, btc=bt(account_rules=rules))
    del s1
    j = Journal(tmp_path / "state.db")
    seq, st = j.latest_snapshot()
    j.close()
    s2 = resume(tmp_path, btc=bt(account_rules=rules))
    s2.account.guard.breached = True
    s2._commit()
    s2.journal.close()
    s3 = resume(tmp_path, {T("10:30"): [long()]}, start=T("10:21"), btc=bt(account_rules=rules))
    assert s3.account.guard.breached
    s3.restart_block_session = None
    s3.run()
    assert s3.decisions[-1]["reason"] == "account_drawdown_breached"


def test_run_kind_mismatch_is_refused(tmp_path):
    first_part(tmp_path, T("10:02"))
    with pytest.raises(RecoveryError, match="belongs to a replay run"):
        resume(tmp_path, pcfg=replace(PCFG, mode="infrastructure_validation"))


def test_persistence_failure_blocks_entries_and_recovers_without_losing_records(tmp_path):
    s = session(tmp_path, EVS, {T("10:05"): [long("a")], T("10:15"): [long("b")]})
    s.connect()
    real = s.journal.commit
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise JournalError("persistence failed: disk I/O error")
        return real()

    s.journal.commit = flaky
    evs = s.feed.poll(100_000)
    for ev in evs[:40]:
        s.process_event(ev)
    s._commit()
    assert not s.persistence_ok
    for ev in [e for e in evs if T("10:04") <= e.ts < T("10:06")]:
        s.process_event(ev)
    assert s.decisions[-1]["reason"] == "persistence_unhealthy"
    s._commit()
    assert s.persistence_ok
    k = [e["kind"] for e in s.journal.entries()]
    assert "persistence_failure" in k and "persistence_recovered" in k and "entry_decision" in k
    assert s.journal.verify() == len(k)


def test_hmac_protected_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("FTS_JOURNAL_KEY", "k" * 40)
    first_part(tmp_path, T("10:02"))
    monkeypatch.delenv("FTS_JOURNAL_KEY")
    with pytest.raises(JournalError, match="HMAC-protected"):
        Journal(tmp_path / "state.db")
    monkeypatch.setenv("FTS_JOURNAL_KEY", "x" * 40)
    with pytest.raises(JournalError, match="hash chain broken"):
        Journal(tmp_path / "state.db").verify()
    monkeypatch.setenv("FTS_JOURNAL_KEY", "k" * 40)
    assert Journal(tmp_path / "state.db").verify() > 0


def test_state_file_permissions(tmp_path):
    import os
    import stat

    first_part(tmp_path, T("10:02"))
    mode = stat.S_IMODE(os.stat(tmp_path / "state.db").st_mode)
    assert mode & 0o077 == 0
