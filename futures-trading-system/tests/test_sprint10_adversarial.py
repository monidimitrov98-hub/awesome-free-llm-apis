"""Sprint 10 adversarial compliance audit: every scenario is a reproduction of an attack or failure mode
against the prop firm / AccountGuard / NewsGuard pipeline. Defects found in this audit carry a
``# DEFECT Bn`` marker and their fix lives in the same commit."""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from src.config import PROJECT_ROOT, ConfigError
from src.paper.journal import KEY_ENV
from src.risk.prop_firms.engine import UNVERIFIED, ComplianceEngine
from src.risk.prop_firms.profile import load_profile
from src.risk.prop_firms.reports import ComplianceJournalReader
from src.risk.prop_firms.versions import Activations, Registry, diff, operational_state
from tests.paper_helpers import APP, PCFG, T, bt, guard_rules, long, path, session
from tests.test_prop_firms import (FN_C, FN_F, MON, OP, _active, _decisions, _engine_for, ct, engine, snap,
                                   write_profile)

FN_V2 = "2026-11-25.1"


# ============================================================================ Workstream A: re-verified profiles
def test_new_fundednext_versions_keep_history_and_stay_unverified():
    reg = Registry()
    assert not reg.errors
    for pid in (FN_C, FN_F):
        old, new = reg.get(pid, "2026-11-22.1"), reg.get(pid, FN_V2)
        assert old.rule("max_loss").params.get("lock_verification") is None          # history untouched
        ml = new.rule("max_loss")
        assert ml.params["lock_verification"] == "CONFLICTING" and ml.params["lock_at"] == 50100.0
        lock = next(r for r in new.rules if r.id == "FNF-FLEX50-MLL-LOCK")
        assert lock.verification == "CONFLICTING" and lock.safety_critical
        st = operational_state(new, Activations(Path("/nonexistent/a.jsonl")))
        assert st["status"] == "UNVERIFIED" and not st["operational"]
        assert "FNF-FLEX50-MLL-LOCK" in st["safety_critical_unresolved"]
        d = diff(old, new)
        assert d["potentially_unsafe"] == []                                         # only stricter changes
        assert any(c["change"] == "params.lock_verification" and c["direction"] == "STRICTER" for c in d["changes"])
        assert new.verification["retrieved"] == "2026-11-25" and "refused" in new.verification["method"]
    c = reg.get(FN_C, FN_V2)
    assert not any("Benchmark" in r.description for r in c.rules)                   # funded-only rules stay funded
    f = reg.get(FN_F, FN_V2)
    assert {"FNF-FLEX50-PAYOUT-MLL-RESET", "FNF-FLEX50-CYCLE"} <= {r.id for r in f.rules}


def test_unverified_lock_is_never_applied_so_the_floor_keeps_trailing():
    from src.config import load_config

    app = load_config()
    reg = Registry()
    for ver, expect_floor, lock_applied in (("2026-11-22.1", 50100.0, True), (FN_V2, 50500.0, False)):
        e = ComplianceEngine(reg.get(FN_C, ver), operational={"operational": False},
                             tick_values={r: s.tick_value for r, s in app.contracts.items()})
        e.observe(ct(f"{MON} 09:00"), snap(50000.0, initial=50000.0))
        e.observe(ct(f"{MON} 15:30"), snap(52000.0, initial=50000.0))
        e.observe(ct(f"{MON} 17:05"), snap(52000.0, initial=50000.0))               # EOD high 52,000
        assert e.official_floor() == expect_floor and (e.lock_level() is not None) == lock_applied
        s = e.summary(snap(52000.0, initial=50000.0))
        assert s["official_threshold"]["lock_applied"] is lock_applied


def test_sources_record_the_access_attempts_and_conflict():
    import tomllib

    raw = tomllib.loads((PROJECT_ROOT / "config/prop_firms/sources/fundednext_futures_2026-11-25.toml").read_text())
    st = {f["point"][:2].strip(" ."): f["status"] for f in raw["findings"]}
    assert st["7"] == "CONFLICTING" and st["3"] == "NOT_FOUND"
    assert all(st[k] == "UNVERIFIED" for k in ("1", "2", "4", "5", "6"))


# ============================================================================ drawdown and equity
def test_sudden_collapse_breaches_and_requests_flatten_in_one_event(tmp_path):
    e = engine(tmp_path)
    e.observe(ct(f"{MON} 09:00"), snap(10000.0, 10000.0, {"MES": 1}))
    acts = e.observe(ct(f"{MON} 09:00:01"), snap(10000.0, 6000.0, {"MES": 1}))
    assert e.breached and {"prop_firm_breach"} <= {a.get("kind") for a in acts}
    assert any(a.get("reason") == "prop_firm_max_loss_protection" for a in acts)


def test_partial_equity_protection_without_accountguard(tmp_path):  # DEFECT B2
    e = engine(tmp_path, ag=None)  # AccountGuard disabled: the prop engine alone protects
    # equity unknown (a position is unmarked); the KNOWN losses already reach the flatten reserve
    acts = e.observe(ct(f"{MON} 09:00"), AccountSnapshotLike(10000.0, math.nan, {"MES": 1, "MNQ": 1}, 8100.0))
    assert any(a.get("reason") == "prop_firm_max_loss_protection_partial_equity" for a in acts)
    assert not e.breached                                                     # no breach from partial information
    acts = e.observe(ct(f"{MON} 09:01"), AccountSnapshotLike(10000.0, math.nan, {"MES": 1}, 9900.0))
    assert not any(a["action"] == "flatten" for a in acts)


def AccountSnapshotLike(balance, equity, positions, known):
    from src.risk.prop_firms.engine import AccountSnapshot

    return AccountSnapshot(balance=balance, equity=equity, positions=positions, initial_balance=10000.0,
                           known_equity=known)


def test_incorrect_initial_balance_and_day_boundary(tmp_path):
    e = engine(tmp_path)
    assert "prop_account_size_mismatch" in e.evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap(initial=10000.01)).reasons
    assert e.trading_date(ct(f"{MON} 16:59:59")) == MON and e.trading_date(ct(f"{MON} 17:00:00")) == "2026-11-17"


def test_missing_events_before_the_new_day_cannot_reuse_yesterdays_floor(tmp_path):
    e = engine(tmp_path)
    e.observe(ct(f"{MON} 15:30"), snap(10700.0))
    # no market event at all on Tuesday until 10:00; an entry evaluation happens first
    d = e.evaluate_entry(ct("2026-11-17 10:00"), "MES", 1, snap(10700.0))
    assert d.official_floor == 8700.0


# ============================================================================ execution and positions
def test_partial_fill_pending_and_fill_time_window(tmp_path):
    e = engine(tmp_path)  # 3 micro-equivalent
    # order for 3 MES: 2 already filled (position), 1 remaining is THIS fill: pending excludes it
    d = e.evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap(positions={"MES": 2}), trade_risk=30.0, scope="fill")
    assert d.allowed
    assert "prop_contract_limit" in e.evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap(positions={"MES": 2},
                                                     pending={"MNQ": 1}), trade_risk=30.0, scope="fill").reasons
    # signal at 14:59 CT was fine; the fill arrives after the entry cut-off: blocked at the fill
    late = e.evaluate_entry(ct(f"{MON} 15:00:30"), "MES", 1, snap(), trade_risk=30.0, scope="fill")
    assert "prop_session_outside_permitted_entry_hours" in late.reasons


def test_duplicate_and_out_of_order_executions(tmp_path):
    e = engine(tmp_path)
    assert e.on_execution("X9", ct(f"{MON} 10:00"))
    assert e.on_execution("X8", ct(f"{MON} 09:00"))           # out of order: still counted once
    assert not e.on_execution("X9", ct(f"{MON} 10:00")) and not e.on_execution("X8", ct(f"{MON} 11:00"))
    assert e.days[MON]["executions"] == 2


def test_duplicate_market_events_never_double_fill(tmp_path):
    evs = path(T("10:00"), 20, lambda i: 5000.0)
    evs = evs + evs[:40]                                        # the same events replayed (same event ids)
    s = session(tmp_path, sorted(evs, key=lambda e: e.ts), {T("10:05"): [long(stop=4990.0, target=5010.0)]})
    s.run()
    execs = [e["payload"]["exec_id"] for e in s.journal.entries("execution")]
    assert len(execs) == len(set(execs)) and any(True for _ in s.journal.entries("data_rejected"))


# ============================================================================ persistence and recovery
def test_restart_mid_day_keeps_daily_counters_and_high_water(tmp_path):
    from tests.paper_helpers import session as mk

    reg, acts = _active(tmp_path)
    btc = bt()
    evs = path(T("10:00"), 20, lambda i: 5000.0 if i < 40 else 5008.0)
    s = mk(tmp_path / "s", evs, {T("10:05"): [long(stop=4990.0, target=5005.0)]}, btc=btc,
           prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    before = s.prop.state()
    s.journal.close()
    s2 = mk(tmp_path / "s", [], btc=btc, create=False, prop_engine=_engine_for(tmp_path, reg, acts, btc))
    after = s2.prop.state()
    for k in ("current_day", "day_start_balance", "eod_high", "intraday_high", "breached", "days", "exec_ids"):
        assert after[k] == before[k], k
    s2.journal.close()


def test_corrupted_journal_refuses_to_start_with_a_profile(tmp_path):
    import sqlite3

    from src.paper.runtime import RecoveryError
    from src.paper.journal import JournalError
    from tests.paper_helpers import session as mk

    reg, acts = _active(tmp_path)
    btc = bt()
    s = mk(tmp_path / "s", path(T("10:00"), 10, lambda i: 5000.0), btc=btc, prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    s.journal.close()
    db = sqlite3.connect(tmp_path / "s" / "state.db")
    db.execute("UPDATE journal SET payload = replace(payload, 'MES', 'MNQ') WHERE seq = 5")
    db.commit()
    db.close()
    with pytest.raises((RecoveryError, JournalError)):
        mk(tmp_path / "s", [], btc=btc, create=False, prop_engine=_engine_for(tmp_path, reg, acts, btc))


def test_snapshot_without_compliance_state_but_journal_records_is_untrusted(tmp_path):
    e = engine(tmp_path)
    e.check_journal_consistency([10500.0], False)                       # journal saw a high this state never had
    assert not e.recovery_ok and "prop_state_untrusted" in e.evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap()).reasons


# ============================================================================ profile security
def test_rule_and_source_ids_cannot_inject_control_characters(tmp_path):  # DEFECT B1
    f = write_profile(tmp_path)
    for old, new in (('id = "T-MLL"', 'id = "T-MLL\\nINJECTED"'), ('id = "S1"\ntitle', 'id = "S 1"\ntitle'),
                     ('url = "https://example.invalid/rules"', 'url = "https://example.invalid/rules\\n=cmd"')):
        g = tmp_path / "x" / "test_firm"
        g.mkdir(parents=True, exist_ok=True)
        (g / "1.toml").write_text(f.read_text().replace(old, new))
        with pytest.raises(ConfigError):
            load_profile(g / "1.toml")


def test_malformed_toml_unknown_types_and_missing_descriptions(tmp_path):
    f = write_profile(tmp_path)
    cases = [("[profile]", "[profile"), ('type = "max_contracts"', 'type = "teleport"'),
             ('description = "test loss limit"', 'description = ""')]
    for old, new in cases:
        g = tmp_path / "y" / "test_firm"
        g.mkdir(parents=True, exist_ok=True)
        (g / "1.toml").write_text(f.read_text().replace(old, new, 1))
        with pytest.raises(ConfigError):
            load_profile(g / "1.toml")
    reg = Registry(tmp_path / "y")
    assert reg.errors and not reg.profiles                                # invalid files are listed, never loaded


def test_activation_store_detects_replay_edit_reorder_and_wrong_key(tmp_path, monkeypatch):  # DEFECT B7
    monkeypatch.delenv(KEY_ENV, raising=False)
    reg_dir = tmp_path / "p"
    write_profile(reg_dir)
    p = Registry(reg_dir).get("test_firm", "1")
    store = tmp_path / "acts.jsonl"
    acts = Activations(store)
    acts.activate(p, approved_by="Test Reviewer", confirm_sha=p.sha256[:16], statement="ok")
    first = store.read_text().splitlines()[0]
    acts.revoke(p, by="Test Reviewer", reason="rules changed")
    assert not operational_state(p, acts)["operational"]
    # replay the old activation line after the revocation
    store.write_text(store.read_text() + first + "\n")
    assert not operational_state(p, acts)["operational"]
    with pytest.raises(ConfigError, match="sequence"):
        acts.records()
    # edit a field of a valid record
    store.unlink()
    acts.activate(p, approved_by="Test Reviewer", confirm_sha=p.sha256[:16], statement="ok")
    store.write_text(store.read_text().replace("Test Reviewer", "Someone Else"))
    assert not operational_state(p, acts)["operational"]
    with pytest.raises(ConfigError, match="integrity"):
        acts.records()
    # a store written with a key fails without it (and vice versa)
    store.unlink()
    monkeypatch.setenv(KEY_ENV, "k" * 32)
    acts.activate(p, approved_by="Test Reviewer", confirm_sha=p.sha256[:16], statement="ok")
    assert operational_state(p, acts)["operational"] and json.loads(store.read_text())["hash"].startswith("hmac:")
    monkeypatch.setenv(KEY_ENV, "z" * 32)
    assert not operational_state(p, acts)["operational"]
    monkeypatch.delenv(KEY_ENV)
    assert not operational_state(p, acts)["operational"]
    # a broken store is never extended
    with pytest.raises(ConfigError):
        acts.revoke(p, by="Test Reviewer", reason="x")


def test_concurrent_activation_changes_keep_the_chain_intact(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    reg_dir = tmp_path / "p"
    write_profile(reg_dir)
    p = Registry(reg_dir).get("test_firm", "1")
    acts = Activations(tmp_path / "acts.jsonl")
    errors = []

    def worker(i):
        try:
            for _ in range(10):
                if i % 2:
                    acts.activate(p, approved_by="Test Reviewer", confirm_sha=p.sha256[:16], statement="x")
                else:
                    acts.revoke(p, by="Test Reviewer", reason="y")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    recs = acts.records()
    assert [r["seq"] for r in recs] == list(range(1, 41))


def test_stale_version_and_exact_selection(tmp_path):
    reg_dir = tmp_path / "p"
    write_profile(reg_dir, ver="1")
    write_profile(reg_dir, ver="2", amount=1500.0)
    reg = Registry(reg_dir)
    acts = Activations(tmp_path / "acts.jsonl")
    p1 = reg.get("test_firm", "1")
    acts.activate(p1, approved_by="Test Reviewer", confirm_sha=p1.sha256[:16], statement="v1")
    assert operational_state(p1, acts)["operational"]
    assert not operational_state(reg.get("test_firm", "2"), acts)["operational"]   # v1 approval never covers v2
    from src.risk.prop_firms.runtime import build_engine

    with pytest.raises(ConfigError, match="exact version"):
        build_engine(bt=bt(), specs={}, profile_id="test_firm", version="", registry=reg, activations=acts)


def test_status_view_reflects_a_revocation_immediately(tmp_path):  # DEFECT B3
    from src.risk.prop_firms.runtime import build_engine

    reg, acts = _active(tmp_path)
    e = build_engine(bt=bt(), specs={"MES": APP.contracts["MES"]}, profile_id="test_firm", version="1", registry=reg,
                     activations=acts)
    assert e.operational["operational"]
    acts.revoke(reg.get("test_firm", "1"), by="Test Reviewer", reason="x")
    assert not e.refresh_operational()["operational"]                    # used by the terminal status view
    d = e.evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap(), record=False)
    assert ComplianceEngine.classify(d, False) != "SAFE"


# ============================================================================ concurrency, API, memory
def test_simultaneous_entry_decisions_are_consistent(tmp_path):
    e = engine(tmp_path)
    results, errors = [], []

    def worker():
        try:
            for i in range(200):
                results.append(e.evaluate_entry(ct(f"{MON} 09:00"), "MES", 1 + i % 5, snap()).allowed)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(results) == 1200
    assert results.count(True) == 1200 * 3 // 5                         # quantities 1-3 allowed, 4-5 blocked


def test_history_reader_is_incremental_and_bounded(tmp_path):  # DEFECT B4
    from src.risk.prop_firms import reports
    from tests.paper_helpers import session as mk

    reg, acts = _active(tmp_path, contracts=1)
    btc = bt()
    s = mk(tmp_path / "s", path(T("10:00"), 20, lambda i: 5000.0), {T("10:05"): [long(stop=4990.0, quantity=2)]},
           btc=btc, prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    rd = ComplianceJournalReader(tmp_path / "s" / "state.db")
    n = rd.refresh().rows_verified
    assert n == s.journal.seq and rd.report()["counts"]["blocks"] == 1
    assert rd.refresh().rows_verified == n                                # nothing new: nothing re-read
    assert reports.MAX_ROWS == rd.evaluations.maxlen == rd.account.maxlen
    s.journal.close()


def test_api_ignores_injected_state_and_discloses_no_paths(tmp_path):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app
    from src.terminal.modes import ModeOptions
    from src.terminal.service import TerminalService

    reg, acts = _active(tmp_path)
    svc = TerminalService(ModeOptions(mode="mock", mock_rate_hz=10, prop_profile="test_firm@1", prop_registry=reg,
                                      prop_activations=acts))
    tok = "t" * 43
    h = {"Authorization": f"Bearer {tok}"}
    with TestClient(create_app(svc, token=tok, port=8765), base_url="http://127.0.0.1:8765") as c:
        import time

        end = time.monotonic() + 20
        while (svc.built is None or not c.get("/api/propfirm/status", headers=h).json().get("selected")) \
                and time.monotonic() < end:
            time.sleep(0.1)
        base = c.get("/api/propfirm/status", headers=h).json()
        inj = c.get("/api/propfirm/status?balance=999999&equity=999999&operational=true&status=SAFE", headers=h).json()
        assert inj["summary"]["balance"] == base["summary"]["balance"] and inj["status"] == base["status"]
        assert c.get("/api/propfirm/status", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
        assert c.get("/api/propfirm/status").status_code == 401
        acts.revoke(reg.get("test_firm", "1"), by="Test Reviewer", reason="audit")
        after = c.get("/api/propfirm/status", headers=h).json()
        assert after["summary"]["operational"]["operational"] is False and after["status"] != "SAFE"   # DEFECT B3
        for p in ("/api/propfirm/status", "/api/propfirm/profiles", "/api/propfirm/profile?key=test_firm@1"):
            txt = c.get(p, headers=h).text
            assert str(PROJECT_ROOT) not in txt and str(tmp_path) not in txt, p


# ============================================================================ AccountGuard and NewsGuard
def test_accountguard_blocks_even_when_the_profile_is_operational(tmp_path):
    reg, acts = _active(tmp_path)
    # AccountGuard: 1000 limit, the trade risks 200 points x $5 = $1,000 -> rejected at the fill. The prop
    # profile (2000 limit) would allow it: AccountGuard decides first and the prop stage is never reached.
    btc = bt(account_rules=guard_rules(max_drawdown=1000.0, safety_buffer=200.0, flatten_buffer=100.0,
                                       require_trade_risk_within_buffer=True))
    s = session(tmp_path / "s", path(T("10:00"), 20, lambda i: 5000.0), {T("10:05"): [long(stop=4800.0)]}, btc=btc,
                prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    assert _decisions(s)[0]["allowed"]
    fa = [e["payload"] for e in s.journal.entries("fill_authorization")][0]
    assert not fa["allowed"] and fa["reason"] == "account_trade_risk_exceeds_buffer"
    assert not any(x["stage"] == "prop_firm" for x in fa["stages"]) and not list(s.journal.entries("execution"))


def test_newsguard_unavailable_or_failing_fails_closed_before_the_prop_stage(tmp_path):
    from src.risk.news.guard import NewsGuard
    from tests.news_helpers import CFG, store
    from tests.paper_helpers import LiveLikeFeed

    reg, acts = _active(tmp_path)
    btc = bt()
    incomplete = store(coverage={"fixture": ["cpi"]})
    incomplete.provider_class = {"fixture": "manual"}
    news = {"MES": NewsGuard(CFG, incomplete, "MES", operational=True)}
    s = session(tmp_path / "a", path(T("10:00"), 20, lambda i: 5000.0), {T("10:05"): [long(stop=4990.0)]}, btc=btc,
                pcfg=replace(PCFG, mode="paper"), news=news, feed_cls=LiveLikeFeed,
                prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    d = _decisions(s)[0]
    assert d["reason"].startswith("news_") and not any(x["stage"] == "prop_firm" for x in d["stages"])
    # a NewsGuard that raises: the session stops (system_error), nothing is authorized
    s2 = session(tmp_path / "b", path(T("10:00"), 20, lambda i: 5000.0), {T("10:05"): [long(stop=4990.0)]}, btc=btc,
                 prop_engine=_engine_for(tmp_path, reg, acts, btc))

    def boom(*a, **k):
        raise RuntimeError("calendar backend down")
    s2.news["MES"].check_entry = boom
    assert s2.run() == "system_error"
    assert not list(s2.journal.entries("execution"))
