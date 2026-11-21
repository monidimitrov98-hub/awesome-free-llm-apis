"""Deterministic mock of the NinjaTrader 8 bridge add-on (same wire protocol), for tests and for
running the whole pipeline WITHOUT NinjaTrader. It only ever sends market data -- like the real
add-on, it has no order functionality. Prices it generates are SYNTHETIC and labelled as such by
the instrument/connection name "MOCK".
"""

from __future__ import annotations

import json
import math
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from src.marketdata.protocol import PROTOCOL, encode_data_line, hello_mac, session_key, welcome_mac
from src.marketdata.timestamps import WINDOWS_TO_IANA


def _fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0"  # 7 fractional digits, like .NET "o" without offset


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


class MockBridge:
    def __init__(self, host: str, port: int, secret: bytes, *, tz: str = "Eastern Standard Time",
                 client: str = "fts-nt8-bridge-mock") -> None:
        self.host, self.port, self.secret, self.tz, self.client = host, port, secret, tz, client
        self.zone = ZoneInfo(WINDOWS_TO_IANA[tz]) if tz in WINDOWS_TO_IANA else None
        self.sock: socket.socket | None = None
        self.key = b""
        self.seq = 0
        self.eseq = 0

    # ------------------------------------------------------------------ connection
    def connect(self, *, wrong_secret: bool = False, timeout: float = 5.0) -> None:
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        f = s.makefile("rb")
        ch = json.loads(f.readline())
        cn = secrets.token_hex(32)
        secret = secrets.token_bytes(32) if wrong_secret else self.secret
        hello = {"type": "hello", "protocol": PROTOCOL, "client": self.client, "client_version": "1.0.0",
                 "provider": "NinjaTrader 8", "tz": self.tz, "client_nonce": cn,
                 "mac": hello_mac(secret, ch["server_nonce"], cn, self.tz)}
        s.sendall(json.dumps(hello).encode() + b"\n")
        reply = json.loads(f.readline() or b'{"type":"closed"}')
        if reply.get("type") != "welcome" or reply.get("mac") != welcome_mac(self.secret, ch["server_nonce"], cn):
            s.close()
            raise ConnectionRefusedError(f"bridge handshake refused: {reply}")
        self.sock, self.key, self.seq = s, session_key(self.secret, ch["server_nonce"], cn), 0

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # ------------------------------------------------------------------ messages
    def _now_bridge(self) -> str:
        return _fmt_utc(datetime.now(timezone.utc))

    def message(self, obj: dict[str, Any]) -> bytes:
        self.seq += 1
        return encode_data_line(self.key, {"seq": self.seq, **obj})

    def send(self, obj: dict[str, Any]) -> None:
        assert self.sock is not None
        self.sock.sendall(self.message(obj))

    def send_raw(self, data: bytes) -> None:
        assert self.sock is not None
        self.sock.sendall(data)

    def times(self, t_utc: datetime) -> dict[str, str]:
        local = t_utc.astimezone(self.zone).replace(tzinfo=None)
        return {"t_local": _fmt_local(local), "t_utc": _fmt_utc(t_utc), "t_bridge": self._now_bridge()}

    def md(self, instrument: str, kind: str, t_utc: datetime, **px: float) -> dict[str, Any]:
        self.eseq += 1
        return {"type": "md", "eseq": self.eseq, "instrument": instrument, "kind": kind, **self.times(t_utc), **px}

    def trade(self, instrument: str, t_utc: datetime, price: float, size: float = 1.0) -> None:
        self.send(self.md(instrument, "trade", t_utc, price=price, size=size))

    def quote(self, instrument: str, t_utc: datetime, bid: float, ask: float, bid_size: float = 10.0,
              ask_size: float = 10.0) -> None:
        self.send(self.md(instrument, "quote", t_utc, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size))

    def subscribe(self, instrument: str, state: str = "subscribed") -> None:
        self.send({"type": "subscription", "instrument": instrument, "state": state, "t_bridge": self._now_bridge()})

    def status(self, state: str = "Connected", name: str = "MOCK") -> None:
        self.send({"type": "status", "provider_connection": state, "connection_name": name,
                   "t_bridge": self._now_bridge()})

    def heartbeat(self) -> None:
        self.send({"type": "heartbeat", "t_bridge": self._now_bridge()})


def synthetic_stream(contracts: dict[str, str], start: datetime, n: int, step: timedelta,
                     base: dict[str, float] | None = None) -> Iterable[tuple[str, str, datetime, dict[str, float]]]:
    """Deterministic SYNTHETIC level-1 stream: (instrument, kind, t_utc, prices), quote then trade."""
    base = base or {"MES": 6000.0, "MNQ": 21000.0}
    for i in range(n):
        t = start + step * i
        for root, name in contracts.items():
            mid = base[root] + 0.25 * ((i % 8) - 4)
            yield name, "quote", t, {"bid": mid - 0.25, "ask": mid, "bid_size": 10.0, "ask_size": 12.0}
            yield name, "trade", t + timedelta(milliseconds=1), {"price": mid, "size": 1.0}


def run_mock(bridge: MockBridge, contracts: dict[str, str], *, duration: float, rate_hz: float = 20.0,
             stop: threading.Event | None = None) -> int:
    """Stream SYNTHETIC real-time data (timestamped now) for ``duration`` seconds. Returns events sent."""
    bridge.status("Connected", "MOCK (synthetic data)")
    for name in contracts.values():
        bridge.subscribe(name)
    sent, i = 0, 0
    end = time.monotonic() + duration
    next_hb = time.monotonic()
    while time.monotonic() < end and not (stop is not None and stop.is_set()):
        now = datetime.now(timezone.utc)
        for root, name in contracts.items():
            base = 6000.0 if root == "MES" else 21000.0
            mid = round((base + 2.0 * math.sin(i / 40.0)) * 4) / 4
            bridge.quote(name, now, mid - 0.25, mid)
            bridge.trade(name, now, mid, 1.0)
            sent += 2
        i += 1
        if time.monotonic() >= next_hb:
            bridge.heartbeat()
            next_hb = time.monotonic() + 1.0
        time.sleep(1.0 / rate_hz)
    return sent
