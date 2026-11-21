"""Sprint 6 security boundaries: read-only add-on, no order path, loopback only, no secrets in output."""

import ast
import re
import socket
from pathlib import Path

import pytest

from src.marketdata.bridge import BridgeServer, NinjaTraderBridgeFeed
from src.paper.broker import PaperBroker
from tests.marketdata_helpers import bridge_for, make_feed, md_cfg

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "ninjatrader" / "FtsMarketDataBridge.cs"

# NinjaScript order/account APIs and generic order vocabulary that must never appear in the add-on
FORBIDDEN_CS = [r"\bAccount\b", r"\bAccounts\b", r"SubmitOrder", r"CreateOrder", r"\bOrder\b", r"OrderAction",
                r"OrderType", r"\bChange\s*\(", r"\bCancel\s*\(", r"CancelAllOrders", r"Flatten", r"EnterLong",
                r"EnterShort", r"ExitLong", r"ExitShort", r"\bAtm", r"Execution", r"Position\b", r"TcpListener",
                r"HttpListener", r"Process\.Start", r"Assembly\.Load", r"BinaryFormatter", r"IPAddress\.Any",
                r"IPAddress\.Parse", r"\.Read\(\s*buffer"]


def _code_only(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', text)


def test_addon_contains_no_order_or_listener_api():
    code = _code_only(ADDON.read_text())
    hits = [p for p in FORBIDDEN_CS if re.search(p, code)]
    assert hits == []
    assert "IPAddress.Loopback" in code and "client.Connect(IPAddress.Loopback, port)" in ADDON.read_text()


def test_addon_reads_nothing_after_the_handshake():
    code = _code_only(ADDON.read_text())
    send_loop = code[code.index("private void SendLoop"):code.index("private byte[] Handshake")]
    assert "ReadLine(" not in send_loop and "ReadByte(" not in send_loop and ".Read(" not in send_loop


def test_python_market_data_package_has_no_order_capability():
    pkg = ROOT / "src" / "marketdata"
    for f in pkg.glob("*.py"):
        tree = ast.parse(f.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not names & {"submit", "PaperBroker", "PaperOrder", "flatten", "authorize_signal"}, f.name
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
            n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not {m for m in mods if m and m.split(".")[0] in ("requests", "urllib", "http", "subprocess", "pickle")}
    assert NinjaTraderBridgeFeed.TRANSMITS_ORDERS is False and PaperBroker.TRANSMITS_REAL_ORDERS is False
    assert not any(hasattr(NinjaTraderBridgeFeed, m) for m in ("submit", "send_order", "cancel", "flatten"))


def test_listener_is_loopback_only():
    f, secret, _ = make_feed()
    try:
        host, port = f.server._listener.getsockname()[:2]
        assert host == "127.0.0.1"
        external = [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                    if not a[4][0].startswith("127.")]
        for ip in external[:2]:
            with pytest.raises(OSError):
                socket.create_connection((ip, port), timeout=0.5)
    finally:
        f.disconnect()


def test_server_refuses_a_non_loopback_bind_even_if_the_config_check_is_bypassed():
    from src.marketdata.instruments import InstrumentResolver
    from tests.paper_helpers import APP

    cfg = md_cfg(host="0.0.0.0")
    s = BridgeServer(cfg, b"k" * 32, InstrumentResolver(cfg.contracts, APP.contracts))
    with pytest.raises(OSError, match="non-loopback"):
        s.start()


def test_secret_never_appears_in_status_or_journal(tmp_path):
    from tests.marketdata_helpers import live_session, pump

    f, secret, clock = make_feed()
    try:
        s = live_session(tmp_path, f, clock)
        b = bridge_for(f, secret)
        f.connect()
        b.status("Connected")
        b.subscribe("MES 12-30")
        b.trade("MES 12-30", clock.dt(), 6000.0)
        pump(s, lambda s: s.seq >= 1)
        s._commit()
        blob = repr(s.status()) + "".join(repr(e) for e in s.journal.entries())
        assert secret.hex() not in blob and secret.hex()[:16] not in blob
        assert b.key.hex() not in blob
        b.close()
    finally:
        f.disconnect()
