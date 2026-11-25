"""Sprint 10 shadow verification and end-to-end integration: block categorisation by protection layer,
recovery integrity, reproducible session reports, interrupted-session recovery, full decision traces
(accepted and rejected), operational readiness and the terminal API."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.analytics.shadow.library import AnalyticsLibrary, Filters, load_analytics_config
from src.analytics.shadow.reports import BLOCK_CATEGORIES, categorize
from tests.paper_helpers import T, bt, long, path, session
from tests.test_prop_firms import _active, _engine_for
from tests.test_shadow_analytics import JB, ident, simple_run

CFG = {**load_analytics_config(), "statistics": {**load_analytics_config()["statistics"], "bootstrap_iterations": 100}}


def lib_of(*paths):
    return AnalyticsLibrary(list(paths), CFG)


# ============================================================================ Workstream C
def test_prop_firm_blocks_are_their_own_layer():  # DEFECT B5 (was "Other")
    assert categorize("prop_contract_limit", BLOCK_CATEGORIES) == "Prop firm compliance"
    assert categorize("prop_profile_not_operational", BLOCK_CATEGORIES) == "Prop firm compliance"
    assert categorize("account_safety_buffer", BLOCK_CATEGORIES) == "AccountGuard"
    assert categorize("news_calendar_incomplete", BLOCK_CATEGORIES) == "NewsGuard"


@pytest.mark.parametrize("rows,status", [
    ([("recovered", {"clean_shutdown_before": True, "reconciliation_problems": []})], "PASS"),
    ([("recovered", {"clean_shutdown_before": False, "reconciliation_problems": []})], "WARN"),
    ([("recovered", {"clean_shutdown_before": True, "reconciliation_problems": ["MES: position 1 != executions 0"]})], "FAIL"),
    ([("persistence_failure", {"error": "disk full"})], "FAIL"),
    ([("persistence_failure", {"error": "disk full"}), ("persistence_recovered", {})], "PASS"),
    ([("system_error", {"error": "x"})], "WARN"),
])
def test_recovery_integrity_check(tmp_path, rows, status):  # DEFECT B6 (no such check before)
    b = JB(tmp_path / "r" / "paper_state.db")
    b.trade_ev("MES", "2026-11-18T15:00:00Z", 5000.0)
    for i, (kind, payload) in enumerate(rows):
        b.raw(kind, pd.Timestamp("2026-11-18T15:01:00Z") + pd.Timedelta(seconds=i), payload)
    v = {c["check"]: c for c in lib_of(b.done()).validation()["checks"]}
    assert v["recovery integrity"]["status"] == status


def test_session_report_contents_and_completeness(tmp_path):
    p = simple_run(tmp_path, "s", "SHADOW", nets=(25.0, -20.0, 15.0))
    rep = lib_of(p).session_report(Filters(session_type="SHADOW"))
    m = rep["metrics"]
    assert m["simulated_trades"] == 3 and m["net_pnl"] == 20.0 and m["accepted"] == 3 and m["rejected"] == 0
    assert m["sharpe"] is None and "not meaningful" in m["note"]                 # 1 trading day < 20
    assert m["exposure"]["max_concurrent_contracts"] == 1
    assert rep["runs"][0]["strategy_config_sha256"] == ["a" * 64] and rep["runs"][0]["provider"] == "NinjaTrader 8 bridge"
    assert "do not establish or guarantee" in rep["disclaimer"]
    assert rep["completeness"]["status"] == "COMPLETE", rep["completeness"]
    # evidence missing -> INCOMPLETE, never silently complete
    b = JB(tmp_path / "noid" / "paper_state.db", with_identity=False)
    b.trade_ev("MES", "2026-11-18T15:00:00Z", 5000.0)
    rep = lib_of(b.done()).session_report(Filters())
    assert rep["completeness"]["status"] == "INCOMPLETE"


def test_session_report_refuses_mixed_types_and_flags_open_positions(tmp_path):
    a = simple_run(tmp_path, "r", "REPLAY", day="2026-11-10")
    s = simple_run(tmp_path, "s", "SHADOW")
    assert "never combined" in lib_of(a, s).session_report(Filters())["refused"]
    b = JB(tmp_path / "open" / "paper_state.db")
    b.trade_ev("MES", "2026-11-18T15:00:00Z", 5000.0)
    b.raw("account_update", "2026-11-18T15:00:01Z", {"equity": 9990.0, "unrealized": -10.0, "positions": {"MES": 1}})
    rep = lib_of(b.done()).session_report(Filters())
    assert rep["completeness"]["status"] == "INCOMPLETE"
    assert any("positions still open" in r for r in rep["completeness"]["reasons"])
    assert rep["metrics"]["results_basis"]["unrealized_at_end"]["open"]["unrealized"] == -10.0


def test_sharpe_and_sortino_only_with_enough_days(tmp_path):
    b = JB(tmp_path / "long" / "paper_state.db", ident("SHADOW"))
    nets = [30.0, -10.0, 20.0, -15.0, 25.0] * 5   # 25 trading days
    day = pd.Timestamp("2026-10-01")
    i = 0
    while i < len(nets):
        if day.weekday() < 5:
            t = pd.Timestamp(f"{day.date()}T15:00:00Z")
            sid = b.signal("MES", t)
            b.trade("MES", sid, t, t + pd.Timedelta(minutes=5), 5000.0, 5001.0, nets[i], stop=4996.0)
            i += 1
        day += pd.Timedelta(days=1)
    m = lib_of(b.done()).session_report(Filters(session_type="SHADOW"))["metrics"]
    assert m["trading_days_with_trades"] == 25 and m["sharpe"] is not None and m["sortino"] is not None
    assert "sqrt(252)" in m["note"]


def test_interrupted_session_recovers_and_is_reported_honestly(tmp_path):
    from tests.paper_helpers import session as mk

    reg, acts = _active(tmp_path)
    btc = bt()
    evs = path(T("10:00"), 40, lambda i: 5000.0)
    first, rest = evs[: len(evs) // 2], evs[len(evs) // 2:]
    s = mk(tmp_path / "s", first, {T("10:05"): [long(stop=4990.0, target=5050.0)]}, btc=btc,
           prop_engine=_engine_for(tmp_path, reg, acts, btc))
    assert s.connect()
    for ev in first:                                           # process half of the day ...
        s.process_event(ev)
    s._commit()
    s.journal.close()                                          # ... then "crash": no clean shutdown
    s2 = mk(tmp_path / "s", rest, btc=btc, create=False, prop_engine=_engine_for(tmp_path, reg, acts, btc))
    assert s2.prop.recovery_ok and s2.reconciliation_ok
    s2.run()
    s2.journal.close()
    lib = lib_of(tmp_path / "s" / "state.db")
    v = {c["check"]: c for c in lib.validation()["checks"]}
    assert v["recovery integrity"]["status"] == "WARN"                     # unclean interruption is visible
    rep = lib.session_report(Filters())
    assert rep["completeness"]["status"] == "INCOMPLETE"
    sess = lib.sessions(Filters())["sessions"]
    assert len({x["session_id"] for x in sess}) == len(sess)               # no duplicated session after recovery


# ============================================================================ Workstream D: traceability
def test_every_decision_traces_to_identity_market_data_and_outcome(tmp_path):
    from tests.paper_helpers import session as mk

    reg, acts = _active(tmp_path, contracts=1)
    btc = bt()
    s = mk(tmp_path / "a", path(T("10:00"), 20, lambda i: 5000.0 if i < 30 else 5011.0),
           {T("10:05"): [long(cid="ok1", stop=4990.0, target=5010.0)]}, btc=btc,
           prop_engine=_engine_for(tmp_path, reg, acts, btc), session_meta={"session_type": "REPLAY"})
    s.run()
    s.journal.close()
    r = mk(tmp_path / "b", path(T("10:00"), 20, lambda i: 5000.0), {T("10:05"): [long(cid="big", stop=4990.0, quantity=2)]},
           btc=btc, prop_engine=_engine_for(tmp_path, reg, acts, btc), session_meta={"session_type": "REPLAY"})
    r.run()
    r.journal.close()
    lib = lib_of(tmp_path / "a" / "state.db", tmp_path / "b" / "state.db")
    lib.refresh()
    for src, allowed in zip(lib.sources.values(), (True, False)):
        d = src.data.decisions[0]
        t = lib.trace(src.data.run_id, d.seq)
        assert t["allowed"] is allowed
        idn = t["identity"]
        assert idn["strategy_version"] and len(idn["strategy_config_sha256"]) == 64 and len(idn["risk_sha256"]) == 64
        assert idn["prop_firm"]["profile"] == "test_firm@1" and len(idn["prop_firm"]["ruleset_sha256"]) == 64
        assert t["market_events_before"] and all(e["root"] == "MES" and e["seq"] < d.seq for e in t["market_events_before"])
        stages = [x["stage"] for x in t["stages"]]
        if allowed:
            assert stages[-3:] == ["account_guard", "prop_firm", "risk_manager"]
            assert t["order"] and t["fill_authorizations"][0]["allowed"] and t["executions"] and t["trade"]
        else:
            assert t["reason"] == "prop_contract_limit" and stages[-1] == "prop_firm"
            assert t["order"] is None and not t["executions"]
    assert lib.trace("0" * 16, 5) is None


def test_cli_session_report_and_trace(tmp_path, monkeypatch, capsys):
    import run_shadow_analytics as cli
    from src.analytics.shadow import library
    from src.config import PROJECT_ROOT

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(library, "PROJECT_ROOT", tmp_path)
    cfg = tmp_path / "an.toml"
    cfg.write_text((PROJECT_ROOT / "config" / "analytics.toml").read_text().replace(
        'journals = ["data/terminal/shadow/paper_state.db", "data/paper/paper_state.db", "data/analytics/journals/*/paper_state.db"]',
        'journals = ["runs/*/paper_state.db"]'))
    simple_run(tmp_path / "runs", "a", "SHADOW")
    assert cli.main(["--config", str(cfg), "session-report", "--session-type", "SHADOW", "--out", str(tmp_path / "o")]) == 0
    out = capsys.readouterr().out
    assert '"COMPLETE"' in out and list((tmp_path / "o").glob("session_report_shadow_*.json"))
    lib = AnalyticsLibrary([tmp_path / "runs" / "a" / "paper_state.db"], CFG)
    lib.refresh()
    src = next(iter(lib.sources.values()))
    assert cli.main(["--config", str(cfg), "trace", "--run-id", src.data.run_id, "--seq", str(src.data.decisions[0].seq)]) == 0
    assert '"allowed": true' in capsys.readouterr().out
    assert cli.main(["--config", str(cfg), "trace", "--run-id", src.data.run_id, "--seq", "1"]) == 1


# ============================================================================ terminal: readiness and new endpoints
TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def fn_terminal(tmp_path_factory):
    import time

    from src.terminal.modes import ModeOptions
    from src.terminal.service import TerminalService
    from tests.e2e.e2e_server import EveryNthBarTestStrategy

    d = tmp_path_factory.mktemp("fn")
    cfg = d / "an.toml"
    from src.config import PROJECT_ROOT

    cfg.write_text((PROJECT_ROOT / "config" / "analytics.toml").read_text().replace(
        'journals = ["data/terminal/shadow/paper_state.db", "data/paper/paper_state.db", "data/analytics/journals/*/paper_state.db"]',
        "journals = []"))
    svc = TerminalService(ModeOptions(mode="replay", speed=1e7, replay_start="2025-03-12",
                                      strategy_factory=lambda r: EveryNthBarTestStrategy(), tests_disable_regime_gate=True,
                                      prop_profile="fundednext_futures_flex_50k_challenge@2026-11-25.1",
                                      prop_initial_balance_from_profile=True))
    svc.analytics_config = cfg
    svc.start()
    end = time.monotonic() + 300
    while svc.state not in ("stopped", "failed") and time.monotonic() < end:
        time.sleep(0.2)
    assert svc.state == "stopped", svc.error
    yield svc
    svc.stop()


def test_readiness_never_says_safe_with_an_unverified_profile(fn_terminal):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app

    with TestClient(create_app(fn_terminal, token=TOKEN, port=8765), base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/readiness").status_code == 401
        r = c.get("/api/readiness", headers=AUTH).json()
        assert r["verdict"] in ("INCOMPLETE", "BLOCKED") and "SAFE" not in json.dumps(r).replace("UNSAFE", "")
        checks = {x["check"]: x for x in r["checks"]}
        assert checks["prop firm profile"]["state"] in ("UNKNOWN", "BLOCK")
        assert "not operational" in checks["prop firm profile"]["detail"]
        assert checks["shadow results"]["state"] == "WARN"                 # test strategy, synthetic data
        rep = c.get("/api/shadow/session_report", headers=AUTH).json()["report"]
        assert rep["completeness"]["status"] == "INCOMPLETE"
        blocks = rep["blocks_by_layer"]["at_signal"]
        assert blocks.get("Prop firm compliance", 0) > 0                   # every entry blocked by the profile
        assert rep["metrics"]["simulated_trades"] == 0
        assert rep["runs"][0]["prop_firm"]["profile"] == "fundednext_futures_flex_50k_challenge@2026-11-25.1"
        for bad in ("/api/shadow/trace?run_id=xyz&seq=1", "/api/shadow/trace?run_id=" + "a" * 16 + "&seq=0",
                    "/api/shadow/trace?run_id=" + "a" * 16):
            assert c.get(bad, headers=AUTH).status_code == 422, bad
        assert c.get("/api/shadow/trace?run_id=" + "a" * 16 + "&seq=5", headers=AUTH).status_code == 409
        for method in ("post", "put", "delete"):
            assert getattr(c, method)("/api/readiness", headers=AUTH).status_code == 405


def test_service_never_starts_a_second_session():  # DEFECT B8
    import time

    from src.terminal.modes import ModeOptions
    from src.terminal.service import TerminalService

    svc = TerminalService(ModeOptions(mode="mock", mock_rate_hz=10, prop_profile=""))
    svc.start()
    first = svc.built
    end = time.monotonic() + 20
    while svc.state != "running" and time.monotonic() < end:
        time.sleep(0.05)
    svc.start()                                    # e.g. an API lifespan on an already running service
    assert svc.built is first and svc.state == "running"
    svc.stop()
    svc.start()                                    # a stopped service is never restarted either
    assert svc.built is first and svc.state != "running"
    svc.stop()                                     # idempotent
