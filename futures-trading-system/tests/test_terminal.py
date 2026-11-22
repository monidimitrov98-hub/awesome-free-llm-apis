"""Sprint 7 terminal backend: read-only API security, contracts, WebSocket, modes, views, lifecycle.
Everything runs locally (loopback only, mock bridge / synthetic replay); no NinjaTrader, no network."""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.config import ConfigError
from src.terminal.api import create_app
from src.terminal.modes import ModeOptions, PacedReplayFeed, prerequisites
from src.terminal.service import NotificationCenter, TerminalService

TOKEN = "t" * 43
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = {"origin": BASE}


def wait(pred, timeout=20.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


@pytest.fixture(scope="module")
def mock_client():
    svc = TerminalService(ModeOptions(mode="mock", mock_rate_hz=10))
    app = create_app(svc, token=TOKEN, port=PORT)
    with TestClient(app, base_url=BASE) as c:
        assert wait(lambda: (c.get("/api/market", headers=AUTH).json() or {}).get("validation", {}).get("events_processed", 0) > 10)
        yield c, svc


# ----------------------------------------------------------------- security
def test_health_is_public_everything_else_needs_the_token(mock_client):
    c, _ = mock_client
    assert c.get("/api/health").json()["status"] == "ok"
    for path in ("/api/snapshot", "/api/market", "/api/risk", "/api/journal", "/api/system", "/api/logs",
                 "/api/bars?root=MES", "/api/journal.csv"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get(path, headers={"Authorization": TOKEN}).status_code == 401


def test_dns_rebinding_foreign_origin_and_writes_are_refused(mock_client):
    c, _ = mock_client
    assert c.get("/api/health", headers={"host": "evil.example:8765"}).status_code == 400
    assert c.get("/api/health", headers={"host": "testserver"}).status_code == 400
    assert c.get("/api/snapshot", headers={**AUTH, "origin": "http://evil.example"}).status_code == 403
    for method in ("post", "put", "delete", "patch"):
        assert getattr(c, method)("/api/snapshot", headers=AUTH).status_code == 405


def test_docs_are_disabled_and_security_headers_set(mock_client):
    c, _ = mock_client
    for p in ("/api/docs", "/api/openapi.json", "/docs", "/redoc", "/openapi.json"):
        assert c.get(p, headers=AUTH).status_code in (404, 405)
    r = c.get("/api/health")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/api/status", headers=AUTH).headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/api/bars?root=ES", "/api/bars?root=MES&tf=2min", "/api/bars?root=MES&limit=0",
                                  "/api/journal?page_size=100000", "/api/journal?sort=__class__",
                                  "/api/journal?start=2025-13-99x", "/api/journal?regime=a;drop", "/api/strategy?root=MES%00",
                                  "/api/logs?level=TRACE", "/api/performance?source=live", "/api/activity?since=-1"])
def test_invalid_requests_are_rejected(mock_client, path):
    c, _ = mock_client
    assert c.get(path, headers=AUTH).status_code == 422


def test_static_files_cannot_escape_the_bundle(mock_client):
    c, _ = mock_client
    for p in ("/assets/..%2f..%2fREADME.md", "/assets/%2e%2e/index.html", "/assets/.hidden", "/assets/../../run_terminal.py",
              "/assets/" + "a" * 200):
        assert c.get(p).status_code == 404


def test_rate_limit(mock_client):
    c, _ = mock_client
    codes = [c.get("/api/bars?root=MES&tf=5min&limit=10", headers=AUTH).status_code for _ in range(30)]
    assert 429 in codes and codes[0] == 200
    time.sleep(2.5)  # buckets refill (other tests keep working)


def test_api_is_read_only_by_construction(mock_client):
    c, svc = mock_client
    for route in c.app.routes:
        methods = getattr(route, "methods", None) or set()
        assert methods <= {"GET", "HEAD"}, route.path
        assert not any(w in route.path for w in ("order", "submit", "cancel", "flatten", "config", "settings", "exec"))
    assert not any(hasattr(svc, m) for m in ("submit", "place_order", "cancel_order", "flatten", "set_mode", "set_config"))


# ----------------------------------------------------------------- contracts (mock mode, real bridge protocol)
def test_snapshot_and_views_carry_real_backend_values(mock_client):
    c, svc = mock_client
    snap = c.get("/api/snapshot", headers=AUTH).json()
    meta = snap["meta"]
    assert meta["mode"] == "MOCK" and meta["data_label"] == "SYNTHETIC" and meta["read_only"] and not meta["order_entry"]
    assert meta["modes"]["PAPER"]["available"] is False
    m = c.get("/api/market", headers=AUTH).json()
    assert m["feed"]["provider"] == "MOCK bridge (synthetic data)"   # never presented as NinjaTrader
    mes = m["instruments"]["MES"]
    assert mes["subscription"] == "subscribed" and mes["last"] is not None and mes["spread"] is not None
    st = c.get("/api/status", headers=AUTH).json()
    assert st["mode_authorizes_entries"] is False and st["account"]["initial_balance"] == 10_000
    risk = c.get("/api/risk", headers=AUTH).json()
    assert risk["entries"]["mode"] == "infrastructure_validation"
    assert set(risk["news_guard"]["instruments"]) == {"MES", "MNQ"}
    assert risk["account_guard"]["state"] in ("OK", "BLOCKING", "LOCKED", "BREACHED", "DISABLED")
    bars = c.get("/api/bars?root=MES&tf=5min&limit=50", headers=AUTH).json()
    b = bars["bars"]
    assert len(b["time"]) == 50 and len(b["vwap"]) == 50 and bars["indicator_names"]["ema_fast"].startswith("ema_")
    assert all(x < y for x, y in zip(b["time"], b["time"][1:]))
    assert c.get("/api/positions", headers=AUTH).json()["label"].startswith("PAPER / SIMULATED")
    perf = c.get("/api/performance", headers=AUTH).json()
    assert perf["sample_status"] == "NO TRADES" and perf["source"] == "infrastructure_validation"
    bt = c.get("/api/performance?source=backtest", headers=AUTH).json()
    assert bt["available"] is False
    sysv = c.get("/api/system", headers=AUTH).json()
    assert sysv["session"]["journal"]["persistence_ok"] is True
    blob = json.dumps(sysv) + json.dumps(snap)
    secret = svc.built.feed.server._secret.hex()
    assert TOKEN not in blob and secret not in blob and secret[:16] not in blob and "/tmp/" not in blob


def test_websocket_requires_origin_and_token_then_streams(mock_client):
    c, svc = mock_client
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"ws://127.0.0.1:{PORT}/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    with c.websocket_connect(f"ws://127.0.0.1:{PORT}/ws", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "wrong"}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with c.websocket_connect(f"ws://127.0.0.1:{PORT}/ws", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": TOKEN}))
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["boot_id"] == svc.boot_id
        seen, seqs = set(), []
        while not {"market", "status"} <= seen:
            m = ws.receive_json()
            seen.add(m["type"])
            seqs.append(m["seq"])
            assert m["boot_id"] == svc.boot_id
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        ws.send_text('{"type":"order","side":"buy"}')   # not a command channel: ignored
        ws.send_text('{"type":"ping"}')
        while ws.receive_json()["type"] != "pong":
            pass


def test_slow_websocket_client_gets_resync_not_unbounded_memory():
    from src.terminal.api import Hub
    import asyncio

    hub, q = Hub(), asyncio.Queue(maxsize=3)
    hub.clients.add(q)
    for i in range(10):
        hub._fanout({"type": "market", "seq": i, "boot_id": "b", "data": None})
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert q.maxsize == 3 and len(items) <= 3 and any(m["type"] == "resync" for m in items)


def test_feed_failure_is_visible_and_never_replaced(mock_client):
    c, svc = mock_client
    streamer = svc.built.extras["streamer"]
    streamer.stop_event.set()                                   # the (mock) provider goes away
    assert wait(lambda: all(v["health"] != "ok" for v in c.get("/api/market", headers=AUTH).json()["instruments"].values()), 15)
    m = c.get("/api/market", headers=AUTH).json()
    assert m["feed"]["provider"] == "MOCK bridge (synthetic data)"   # no silent switch to another provider
    assert wait(lambda: any(n["title"] == "Market data disconnected"
                            for n in c.get("/api/notifications", headers=AUTH).json()), 10)


# ----------------------------------------------------------------- notifications
def test_notifications_are_deduplicated_and_bounded():
    nc = NotificationCenter(window=60, maxlen=5)
    assert nc.add("k", "warning", "c", "t", "d", when=0.0) is not None
    assert nc.add("k", "warning", "c", "t", "d", when=10.0) is None
    assert nc.add("k", "warning", "c", "t", "d", when=70.0) is not None
    for i in range(20):
        nc.add(f"x{i}", "info", "c", "t", "d", when=100.0)
    assert len(nc.items) == 5 and nc.suppressed == 1


# ----------------------------------------------------------------- modes
def test_shadow_without_bridge_setup_fails_visibly_without_fallback(tmp_path):
    cfg = tmp_path / "md.toml"
    cfg.write_text("""[bridge]
host = "127.0.0.1"
port = 50999
secret_file = "%s"
max_message_bytes = 4096
queue_max_events = 100
connect_timeout_seconds = 1
heartbeat_timeout_seconds = 5
handshake_timeout_seconds = 2
max_pending_handshakes = 2
[instruments]
contracts = { MES = "MES 12-30" }
""" % (tmp_path / "missing_secret"))
    opts = ModeOptions(mode="shadow", marketdata_config=cfg)
    assert prerequisites(opts) and "secret" in prerequisites(opts)[0]
    svc = TerminalService(opts)
    svc.start()
    try:
        assert svc.state == "failed" and "secret" in svc.error and svc.built is None
        meta = svc.meta()
        assert meta["state"] == "failed" and meta["mode"] == "SHADOW"   # not MOCK, not REPLAY
    finally:
        svc.stop()


def test_regime_gate_can_only_be_disabled_for_test_strategies():
    from src.terminal.modes import build

    with pytest.raises(ConfigError):
        build(ModeOptions(mode="replay", tests_disable_regime_gate=True))


def test_unknown_mode_and_bad_replay_speed():
    assert prerequisites(ModeOptions(mode="live")) == ["unknown mode 'live'"]
    with pytest.raises(ConfigError):
        PacedReplayFeed({}, speed=0)


# ----------------------------------------------------------------- full replay run with a deterministic TEST strategy
@pytest.fixture(scope="module")
def replay_service():
    from tests.e2e.e2e_server import EveryNthBarTestStrategy

    svc = TerminalService(ModeOptions(mode="replay", speed=1e7, replay_start="2025-03-12",
                                      strategy_factory=lambda r: EveryNthBarTestStrategy(), tests_disable_regime_gate=True))
    svc.start()
    assert wait(lambda: svc.state in ("stopped", "failed"), 240, 0.2), svc.state
    yield svc
    svc.stop()


def test_replay_run_produces_journal_positions_and_performance(replay_service):
    svc = replay_service
    assert svc.outcome == "end_of_data" and svc.state == "stopped"
    st = svc.status()
    n = st["account"]["trades"]
    assert n >= 3
    j = svc.journal(1, 2)
    assert j["total"] == n and len(j["rows"]) == 2 and j["rows"][0]["trade_id"].startswith("T")
    p2 = svc.journal(2, 2)
    assert {r["trade_id"] for r in j["rows"]}.isdisjoint({r["trade_id"] for r in p2["rows"]})
    wins = svc.journal(1, 500, outcome="win")["rows"]
    assert all(r["net_pnl"] > 0 for r in wins)
    asc = svc.journal_rows(sort="net_pnl", order="asc")
    assert [r["net_pnl"] for r in asc] == sorted(r["net_pnl"] for r in asc)
    perf = svc.performance("session")
    trades = svc.built.session.account.closed_trades
    assert perf["overall"]["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in trades))
    assert perf["curves"]["equity"][-1]["equity"] == pytest.approx(10_000 + sum(t["net_pnl"] for t in trades))
    assert min(p["drawdown"] for p in perf["curves"]["drawdown"]) <= 0
    assert perf["sample_status"] == "INSUFFICIENT SAMPLE SIZE"
    risk = svc.risk()
    assert risk["entries"]["mode"] == "replay"


def test_signals_rejections_and_simulated_fills_are_distinct_in_the_chart_view(replay_service):
    svc = replay_service
    bars = svc.bars("MES", "5min", 3000)
    kinds = {m["kind"] for m in bars["markers"]}
    assert {"signal", "authorized", "fill_entry", "fill_exit"} <= kinds
    rejected = [m for m in bars["markers"] if m["kind"] == "rejected"]
    real_reasons = {d["reason"] for d in svc.built.session.decisions if not d["allowed"]}
    assert rejected and {m["reason"] for m in rejected} <= real_reasons   # exact backend reasons
    assert all(m.get("simulated") for m in bars["markers"] if m["kind"].startswith("fill"))
    strat = svc.strategy("MES")
    assert strat["signals"] >= 1 and strat["decisions"] and all("stages" in d for d in strat["decisions"])


def test_csv_export_neutralises_spreadsheet_formulas(replay_service):
    svc = replay_service
    app = create_app(svc, token=TOKEN, port=PORT, manage_service=False)
    with svc.built.session.state_lock:
        svc.built.session.account.closed_trades[0]["exit_reason"] = "=HYPERLINK(\"http://x\")"
    with TestClient(app, base_url=BASE) as c:
        r = c.get("/api/journal.csv", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK" in r.text and ",=HYPERLINK" not in r.text


# ----------------------------------------------------------------- lifecycle
def test_service_stop_releases_threads_and_ephemeral_state():
    before = threading.active_count()
    svc = TerminalService(ModeOptions(mode="mock", mock_rate_hz=10))
    svc.start()
    assert wait(lambda: (svc.status() or {}).get("runtime", {}).get("events", 0) > 5, 20)
    state_dir = svc.built.session.journal.path.parent
    svc.stop()
    assert svc.state == "stopped" and not state_dir.exists()
    assert wait(lambda: threading.active_count() <= before + 1, 10), threading.active_count()


def test_launcher_refuses_busy_port_and_missing_prerequisites(tmp_path, capsys):
    import socket

    import run_terminal

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        assert run_terminal.main(["--port", str(port), "--no-browser"]) == 3
    assert run_terminal.main(["--mode", "replay", "--bars", f"MES={tmp_path / 'none.csv'}", "--no-browser"]) == 1
    assert run_terminal.main(["--port", "80", "--no-browser"]) == 1
    err = capsys.readouterr().err
    assert "in use" in err and "cannot start" in err


# ----------------------------------------------------------------- audit regressions
def test_chart_indicator_computation_does_not_hold_the_session_lock(replay_service, monkeypatch):
    import src.terminal.views as views

    svc = replay_service
    real = views.add_indicators
    held = []

    def probe(*a, **k):
        got = []

        def try_lock():  # acquire AND release in the same thread (RLock ownership)
            lock = svc.built.session.state_lock
            ok = lock.acquire(timeout=1.0)
            got.append(ok)
            if ok:
                lock.release()
        t = threading.Thread(target=try_lock)
        t.start()
        t.join()
        held.append(not got[0])
        return real(*a, **k)
    monkeypatch.setattr(views, "add_indicators", probe)
    svc._bars_cache.clear()
    assert svc.bars("MNQ", "15min", 100)["bars"]["time"]
    assert held == [False]  # another thread could take the lock while indicators were computed


def test_cpu_usage_is_measured_between_calls_not_always_zero():
    svc = TerminalService(ModeOptions(mode="mock"))
    first = svc.system()["process"]["cpu_percent"]
    p = svc._proc
    sum(i * i for i in range(2_000_000))  # some CPU work
    second = svc.system()["process"]["cpu_percent"]
    assert svc._proc is p and first is not None and second > 0
