"""Loopback bridge server and the NinjaTrader 8 market-data feed (READ-ONLY).

Components (the smallest set that covers the job):

* :class:`BridgeServer` -- MarketDataConnection: listens on a LOOPBACK address only, authenticates
  the bridge (mutual HMAC challenge-response), verifies every line (MAC, sequence, schema),
  normalizes market data into :class:`~src.paper.events.MarketEvent` and hands it over through a
  BOUNDED queue. It also keeps ConnectionHealth / subscription state (SubscriptionManager role).
* :class:`NinjaTraderBridgeFeed` -- MarketDataProvider: the :class:`~src.paper.feeds.MarketDataFeed`
  the paper runtime already consumes. Disconnects surface as ``FeedError``; the runtime's existing
  reconnect/back-off logic is the ReconnectionManager, and DataHealth then requires fresh data
  before entries are authorized again.

Fail-closed rules: any protocol violation, MAC failure, sequence break, oversized line or queue
overflow drops the connection (the bridge reconnects); events already validated are kept, nothing
is fabricated. Nothing is ever sent to the bridge after the handshake.
"""

from __future__ import annotations

import collections
import ipaddress
import os
import queue
import secrets
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.marketdata.config import MarketDataConfig
from src.marketdata.instruments import InstrumentError, InstrumentResolver
from src.marketdata.protocol import (PROTOCOL, ProtocolError, hello_mac, parse_data_line, parse_hello, session_key,
                                     welcome_mac)
from src.marketdata.timestamps import TimestampConversionError, iana_zone, parse_iso, provider_time_to_utc, utc_now
from src.paper.events import MarketEvent
from src.paper.feeds import FeedError, LiveFeed

PROVIDER_NAME = "NinjaTrader 8"
SECRET_BYTES = 32


class BridgeSecretError(RuntimeError):
    """The shared bridge secret is missing, malformed or readable by other users."""


def load_secret(path: Path) -> bytes:
    """32-byte secret stored as 64 hex characters in an owner-only file (never logged)."""
    try:
        st = path.stat()
    except OSError:
        raise BridgeSecretError(f"bridge secret file {path} not found: run 'python run_marketdata.py init-secret'") \
            from None
    if os.name == "posix" and stat.S_IMODE(st.st_mode) & 0o077:
        raise BridgeSecretError(f"bridge secret file {path} is readable by other users: chmod 600 it")
    text = path.read_text(encoding="ascii", errors="replace").strip()
    if len(text) != 2 * SECRET_BYTES or any(c not in "0123456789abcdef" for c in text):
        raise BridgeSecretError(f"bridge secret file {path} must hold {2 * SECRET_BYTES} lowercase hex characters")
    return bytes.fromhex(text)


def create_secret(path: Path, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise BridgeSecretError(f"{path} already exists (use --force to replace it; the add-on must then be updated)")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(secrets.token_hex(SECRET_BYTES) + "\n")
    os.replace(tmp, path)


@dataclass
class HealthUpdate:
    """A data-health change the runtime applies to DataHealth (root None = every instrument)."""
    root: str | None
    action: str          # "block" | "unblock" | "flag" | "info"
    reason: str
    time: pd.Timestamp


@dataclass
class _Conn:
    conn_id: int
    sock: socket.socket
    key: bytes = b""
    tz: str = ""
    zone: Any = None
    client: str = ""
    lost_reason: str | None = None
    last_eseq: int | None = None


@dataclass
class BridgeStats:
    connections: int = 0                 # authenticated connections accepted
    reconnections: int = 0               # authenticated connections after the first one
    refused_handshakes: int = 0
    protocol_violations: int = 0
    rejected_messages: dict[str, int] = field(default_factory=dict)
    events_received: int = 0
    queue_overflows: int = 0
    dropped_by_bridge: int = 0           # eseq gaps reported by the bridge's counter
    last_message_at: pd.Timestamp | None = None
    last_heartbeat_at: pd.Timestamp | None = None
    provider_connection: str = "unknown"
    provider_connection_name: str = ""
    subscriptions: dict[str, str] = field(default_factory=dict)
    provider_connections: dict[str, str] = field(default_factory=dict)
    last_error: str = ""


class BridgeServer:
    """Loopback TCP server for ONE authenticated bridge connection at a time."""

    def __init__(self, cfg: MarketDataConfig, secret: bytes, resolver: InstrumentResolver, *,
                 clock: Callable[[], pd.Timestamp] = utc_now) -> None:
        if len(secret) != SECRET_BYTES:
            raise BridgeSecretError("bridge secret must be 32 bytes")
        self.cfg = cfg
        self._secret = secret
        self.resolver = resolver
        self.clock = clock
        # events AND health updates share one ordered, bounded queue: an update is applied exactly
        # between the events that arrived before and after it
        self.events: queue.Queue[MarketEvent | HealthUpdate] = queue.Queue(maxsize=cfg.queue_max_events)
        self._side_updates: collections.deque[HealthUpdate] = collections.deque(maxlen=1_000)  # when the queue is full
        self.run_id = secrets.token_hex(4)  # event ids stay unique across backend restarts
        self._lock = threading.Lock()
        self.stats = BridgeStats(subscriptions={r: "unsubscribed" for r in resolver.by_root})
        self.active: _Conn | None = None
        self.lost: collections.OrderedDict[int, str] = collections.OrderedDict()  # bounded (last 100)
        self.authenticated = threading.Event()
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._pending = threading.BoundedSemaphore(cfg.max_pending_handshakes)
        self._next_id = 0
        self._stop = threading.Event()
        self.port: int | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._listener is not None:
            return
        fam = socket.AF_INET6 if ":" in self.cfg.host else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM)
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # no port hijacking on Windows
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.cfg.host, self.cfg.port))
        if not ipaddress.ip_address(s.getsockname()[0]).is_loopback:  # defence in depth (config checks too)
            s.close()
            raise OSError("refusing a non-loopback listener")
        s.listen(8)
        s.settimeout(0.2)
        self._listener = s
        self.port = s.getsockname()[1]
        self._stop.clear()
        t = threading.Thread(target=self._accept_loop, name="bridge-accept", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with self._lock:
            conn = self.active
        if conn is not None:
            self._close(conn, "backend_stopped")
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = [t for t in self._threads if t.is_alive()]

    # ------------------------------------------------------------------ connections
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, addr = self._listener.accept()  # type: ignore[union-attr]
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            if not self._pending.acquire(blocking=False):  # too many unauthenticated connections
                sock.close()
                continue
            t = threading.Thread(target=self._serve, args=(sock,), name="bridge-conn", daemon=True)
            t.start()
            self._threads = [x for x in self._threads if x.is_alive()] + [t]

    def _serve(self, sock: socket.socket) -> None:
        conn = None
        try:
            conn = self._handshake(sock)
        except (ProtocolError, OSError, UnicodeDecodeError, TimestampConversionError) as exc:
            with self._lock:
                self.stats.refused_handshakes += 1
            try:
                sock.sendall(b'{"type":"refused","reason":"' + type(exc).__name__.encode() + b'"}\n')
            except OSError:
                pass
            sock.close()
            return
        finally:
            self._pending.release()
        self._read_loop(conn)

    def _readline(self, sock: socket.socket, buf: bytearray) -> bytes:
        limit = self.cfg.max_message_bytes
        while True:
            i = buf.find(b"\n")
            if i >= 0:
                line = bytes(buf[:i])
                del buf[: i + 1]
                if len(line) > limit:
                    raise ProtocolError("message too large")
                return line.rstrip(b"\r")
            if len(buf) > limit:
                raise ProtocolError("message too large")
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("bridge closed the connection")
            buf += chunk

    def _handshake(self, sock: socket.socket) -> _Conn:
        sock.settimeout(self.cfg.handshake_timeout)
        server_nonce = secrets.token_hex(32)
        sock.sendall(('{"type":"challenge","protocol":%d,"server_nonce":"%s"}\n' % (PROTOCOL, server_nonce)).encode())
        buf = bytearray()
        hello = parse_hello(self._readline(sock, buf))
        if not secrets.compare_digest(hello.mac, hello_mac(self._secret, server_nonce, hello.client_nonce, hello.tz)):
            raise ProtocolError("bridge authentication failed")
        zone = iana_zone(hello.tz)  # unsupported time zone: refused (timestamps could not be verified)
        if buf:
            raise ProtocolError("data before the handshake completed")
        sock.sendall(('{"type":"welcome","mac":"%s"}\n'
                      % welcome_mac(self._secret, server_nonce, hello.client_nonce)).encode())
        with self._lock:
            self._next_id += 1
            conn = _Conn(self._next_id, sock, session_key(self._secret, server_nonce, hello.client_nonce),
                         hello.tz, zone, hello.client)
            old, self.active = self.active, conn
            self.stats.connections += 1
            if self.stats.connections > 1:
                self.stats.reconnections += 1
            self.stats.subscriptions = {r: "unsubscribed" for r in self.resolver.by_root}
            self.stats.provider_connection = "unknown"
        if old is not None:
            self._close(old, "replaced_by_new_connection")
        now = self.clock()
        for r in self.resolver.by_root:  # nothing is trusted until the bridge reports the subscription
            self._update(HealthUpdate(r, "block", "not_subscribed", now))
        self._update(HealthUpdate(None, "info", f"bridge_connected:{hello.client}:{hello.tz}", now))
        self.authenticated.set()
        return conn

    def _read_loop(self, conn: _Conn) -> None:
        conn.sock.settimeout(self.cfg.heartbeat_timeout)  # silence longer than this = dead bridge
        buf = bytearray()
        seq = 1
        try:
            while not self._stop.is_set() and conn.lost_reason is None:
                line = self._readline(conn.sock, buf)
                received = self.clock()
                msg = parse_data_line(line, conn.key, seq)
                seq += 1
                with self._lock:
                    self.stats.last_message_at = received
                self._handle(conn, msg, received)
        except ProtocolError as exc:
            with self._lock:
                self.stats.protocol_violations += 1
            self._close(conn, f"protocol_violation:{str(exc)[:80]}")
        except (socket.timeout, TimeoutError):
            self._close(conn, "heartbeat_timeout")
        except (ConnectionError, OSError):
            self._close(conn, "connection_closed")
        except Exception as exc:  # noqa: BLE001 -- a backend bug must never leave a silent, half-dead feed
            with self._lock:
                self.stats.last_error = f"internal: {type(exc).__name__}"
            self._close(conn, f"internal_error:{type(exc).__name__}")

    def _close(self, conn: _Conn, reason: str) -> None:
        with self._lock:
            if conn.lost_reason is None:
                conn.lost_reason = reason
                self.lost[conn.conn_id] = reason
                while len(self.lost) > 100:
                    self.lost.popitem(last=False)
            if self.active is conn:
                self.active = None
                self.authenticated.clear()
        try:
            conn.sock.close()
        except OSError:
            pass

    def _update(self, u: HealthUpdate) -> None:
        try:
            self.events.put_nowait(u)
        except queue.Full:
            with self._lock:
                self._side_updates.append(u)

    def _reject(self, reason: str, root: str | None, now: pd.Timestamp, flag: bool) -> None:
        with self._lock:
            self.stats.rejected_messages[reason] = self.stats.rejected_messages.get(reason, 0) + 1
        self._update(HealthUpdate(root, "flag" if flag else "info", reason, now))

    # ------------------------------------------------------------------ messages
    def _handle(self, conn: _Conn, msg: dict[str, Any], received: pd.Timestamp) -> None:
        t = msg["type"]
        try:
            bridge_ts = pd.Timestamp(parse_iso(msg["t_bridge"], utc_required=True))
        except TimestampConversionError:
            self._reject("bad_bridge_timestamp", None, received, True)
            return
        if t == "heartbeat":
            with self._lock:
                self.stats.last_heartbeat_at = received
            return
        if t == "status":
            # per NinjaTrader connection: Connecting/ConnectionLost/Disconnecting block every
            # instrument (we cannot know which connection serves which instrument); Connected clears
            # it, and so does Disconnected (a connection the user switched off; if it was the data
            # connection, no data arrives and the stale-data check blocks entries)
            state, name = msg["provider_connection"], msg["connection_name"]
            with self._lock:
                self.stats.provider_connection, self.stats.provider_connection_name = state, name
                self.stats.provider_connections[name] = state
            reason = f"provider_not_connected:{name}"[:60]
            self._update(HealthUpdate(None, "unblock" if state in ("Connected", "Disconnected") else "block", reason,
                                      received))
            return
        if t == "error":
            with self._lock:
                self.stats.last_error = msg["detail"][:200]
            self._update(HealthUpdate(None, "flag", "provider_error", received))
            return
        try:
            rc = self.resolver.resolve(msg["instrument"])
        except InstrumentError:
            self._reject("instrument_mismatch", None, received, False)
            return
        if t == "subscription":
            with self._lock:
                self.stats.subscriptions[rc.root] = msg["state"]
            self._update(HealthUpdate(rc.root, "unblock" if msg["state"] == "subscribed" else "block",
                                      "not_subscribed", received))
            return
        if t == "book_reset":
            self._update(HealthUpdate(rc.root, "flag", "book_reset", received))
            return
        # t == "md"
        eseq = int(msg["eseq"])
        if conn.last_eseq is not None and eseq != conn.last_eseq + 1:
            if eseq <= conn.last_eseq:
                raise ProtocolError("event counter went backwards")
            with self._lock:
                self.stats.dropped_by_bridge += eseq - conn.last_eseq - 1
            self._update(HealthUpdate(None, "flag", "bridge_dropped_events", received))  # missing data
        conn.last_eseq = eseq
        try:
            ts = provider_time_to_utc(msg["t_local"], msg["t_utc"], conn.zone)
        except TimestampConversionError as exc:
            self._reject("timestamp_" + str(exc).split(" (")[0].replace(" ", "_")[:40], rc.root, received, True)
            return
        kind = msg["kind"]
        ev = MarketEvent(
            f"nt8-{self.run_id}-{conn.conn_id}-{eseq}", rc.root, kind, ts,
            msg.get("price") if kind == "trade" else None,
            float(msg["size"]) if kind == "trade" else 0.0,
            msg.get("bid") if kind == "quote" else None, msg.get("ask") if kind == "quote" else None,
            msg.get("bid_size") if kind == "quote" else None, msg.get("ask_size") if kind == "quote" else None,
            source="ninjatrader8", received_at=received, ts_source="provider", exchange_ts=None, provider_ts=ts,
            bridge_ts=bridge_ts, provider_seq=eseq, instrument=rc.name)
        try:
            self.events.put_nowait(ev)
        except queue.Full:
            # the consumer cannot keep up: never drop silently -- the connection is dropped, the gap
            # makes DataHealth require fresh data, and the bridge reconnects
            with self._lock:
                self.stats.queue_overflows += 1
            self._update(HealthUpdate(None, "flag", "backpressure_overflow", received))
            raise ProtocolError("event queue overflow (consumer too slow)") from None
        with self._lock:
            self.stats.events_received += 1

    # ------------------------------------------------------------------ consumer side
    def drain_side_updates(self) -> list[HealthUpdate]:
        with self._lock:
            out = list(self._side_updates)
            self._side_updates.clear()
        return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = self.stats
            return {"listening": self._listener is not None, "address": f"{self.cfg.host}:{self.port}",
                    "authenticated": self.active is not None, "connection_id": self.active.conn_id if self.active else None,
                    "bridge_client": self.active.client if self.active else None,
                    "bridge_time_zone": self.active.tz if self.active else None,
                    "connections": s.connections, "reconnections": s.reconnections,
                    "refused_handshakes": s.refused_handshakes, "protocol_violations": s.protocol_violations,
                    "rejected_messages": dict(s.rejected_messages), "events_received": s.events_received,
                    "queue_depth": self.events.qsize(), "queue_capacity": self.cfg.queue_max_events,
                    "queue_overflows": s.queue_overflows, "dropped_by_bridge": s.dropped_by_bridge,
                    "last_message_at": _iso(s.last_message_at), "last_heartbeat_at": _iso(s.last_heartbeat_at),
                    "provider_connection": s.provider_connection,
                    "provider_connection_name": s.provider_connection_name,
                    "provider_connections": dict(list(s.provider_connections.items())[-10:]),
                    "subscriptions": dict(s.subscriptions), "last_error": s.last_error,
                    "lost_connections": dict(list(self.lost.items())[-5:])}


def _iso(ts: pd.Timestamp | None) -> str | None:
    return ts.isoformat() if ts is not None else None


class NinjaTraderBridgeFeed(LiveFeed):
    """The live MarketDataFeed for the paper runtime (market data ONLY; it has no order methods)."""

    name = "ninjatrader8_bridge"
    TRANSMITS_ORDERS = False

    def __init__(self, cfg: MarketDataConfig, secret: bytes, resolver: InstrumentResolver, *,
                 clock: Callable[[], pd.Timestamp] = utc_now) -> None:
        super().__init__()
        self.server = BridgeServer(cfg, secret, resolver, clock=clock)
        self.cfg = cfg
        self.provider_name = PROVIDER_NAME   # the terminal's MOCK mode labels its synthetic bridge differently
        self._conn_id: int | None = None
        self._pending_updates: list[HealthUpdate] = []
        self._held: MarketEvent | HealthUpdate | None = None
        self.connect_attempts = 0
        self.today = lambda: clock().date()

    def connect(self) -> None:
        self.connect_attempts += 1
        expired = [rc.name for rc in self.server.resolver.by_root.values() if rc.last_trade_date < self.today()]
        if expired:
            raise FeedError(f"configured contract(s) {expired} have expired: update config/marketdata.toml")
        try:
            self.server.start()
        except OSError as exc:
            raise FeedError(f"cannot listen on {self.cfg.host}:{self.cfg.port}: {exc}") from None
        if not self.server.authenticated.wait(self.cfg.connect_timeout):
            raise FeedError("no authenticated NinjaTrader bridge connected (is NinjaTrader running with the "
                            "FtsMarketDataBridge add-on enabled?)")
        with self.server._lock:
            active = self.server.active
        if active is None:
            raise FeedError("bridge connection was lost during connect")
        self._conn_id = active.conn_id
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.server.stop()

    def poll(self, max_events: int = 1000) -> list[MarketEvent]:
        if not self.connected:
            raise FeedError("feed not connected")
        # Strict ordering: a batch never spans a health update. Updates that precede the next event are
        # handed to the runtime (health_updates) BEFORE that event is returned.
        out: list[MarketEvent] = []
        while len(out) < max_events:
            if self._held is not None:
                item, self._held = self._held, None
            else:
                try:
                    item = self.server.events.get_nowait()
                except queue.Empty:
                    break
            if isinstance(item, HealthUpdate):
                if out:
                    self._held = item  # apply after the events already in this batch
                    break
                self._pending_updates.append(item)
                continue
            if self._pending_updates:
                self._held = item      # pending updates first
                break
            out.append(item)
        if not out and not self._pending_updates and self._held is None:
            reason = self.server.lost.get(self._conn_id) if self._conn_id is not None else None
            if reason is not None:  # every event that arrived before the loss has been delivered
                self.connected = False
                raise FeedError(f"bridge disconnected: {reason}")
        return out

    def health_updates(self) -> list[HealthUpdate]:
        out = self.server.drain_side_updates() + self._pending_updates
        self._pending_updates = []
        return out

    def status(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "feed": self.name, **self.server.snapshot()}

    @property
    def exhausted(self) -> bool:
        return False


def wait_until(pred: Callable[[], bool], timeout: float, step: float = 0.005) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()
