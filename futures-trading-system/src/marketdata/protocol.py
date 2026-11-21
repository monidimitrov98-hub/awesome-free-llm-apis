"""Bridge wire protocol (version 1). Every incoming byte is UNTRUSTED.

Transport: one TCP connection to a LOOPBACK listener; UTF-8 lines terminated by '\\n', each at
most ``max_message_bytes``. The backend listens, the NinjaTrader add-on connects.

Handshake (mutual authentication with a shared 32-byte secret, never sent on the wire):

    backend -> bridge  {"type":"challenge","protocol":1,"server_nonce":<64 hex>}
    bridge  -> backend {"type":"hello","protocol":1,"client":"fts-nt8-bridge","client_version":"1.0.0",
                        "provider":"NinjaTrader 8","tz":<Windows time-zone id>,"client_nonce":<64 hex>,
                        "mac":HMAC(secret, "hello|1|<server_nonce>|<client_nonce>|<tz>")}
    backend -> bridge  {"type":"welcome","mac":HMAC(secret, "welcome|1|<server_nonce>|<client_nonce>")}
                       (or {"type":"refused","reason":...} and the connection is closed)

    session_key = HMAC(secret, "session|1|<server_nonce>|<client_nonce>")   (fresh per connection)

After the handshake data flows ONLY bridge -> backend; the backend never sends anything (there is
no control channel). Each data line is ``<64 hex MAC> <json>`` with MAC = HMAC(session_key, json
bytes). Every message carries ``seq`` = 1, 2, 3, ... per connection: a replayed, reordered or
injected line fails the MAC or the sequence check and the connection is dropped (fail closed).
Market-data messages also carry ``eseq``, the bridge's event counter, whose gaps reveal events the
bridge had to drop.

Message types (exact key sets, anything else is a protocol violation):
    md            trade or quote (level 1)
    status        provider connection status (NinjaTrader ConnectionStatusEventArgs.PriceStatus)
    subscription  per-instrument subscription state
    book_reset    the provider reset the instrument's book (MarketDataEventArgs.IsReset)
    heartbeat     liveness while the market is quiet
    error         provider/bridge error text (informational, short)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from typing import Any

PROTOCOL = 1
HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TEXT = re.compile(r"^[\w .:/()\-+#,]{0,120}$")
_FREE_TEXT = re.compile(r"^[\x20-\x7e]{0,200}$")  # printable ASCII only (no control characters)
CONNECTION_STATES = {"Connected", "Connecting", "ConnectionLost", "Disconnecting", "Disconnected"}
SUBSCRIPTION_STATES = {"subscribed", "unsubscribed", "failed"}

_COMMON = {"type", "seq"}
SCHEMAS: dict[str, dict[str, set[str]]] = {
    # type: {"required": ..., "optional": ...}
    "md": {"required": _COMMON | {"eseq", "instrument", "kind", "t_local", "t_utc", "t_bridge"},
           "optional": {"price", "size", "bid", "ask", "bid_size", "ask_size"}},
    "status": {"required": _COMMON | {"provider_connection", "connection_name", "t_bridge"}, "optional": set()},
    "subscription": {"required": _COMMON | {"instrument", "state", "t_bridge"}, "optional": {"detail"}},
    "book_reset": {"required": _COMMON | {"instrument", "t_bridge"}, "optional": set()},
    "heartbeat": {"required": _COMMON | {"t_bridge"}, "optional": set()},
    "error": {"required": _COMMON | {"detail", "t_bridge"}, "optional": set()},
}


class ProtocolError(ValueError):
    """A message violates the protocol. The connection is dropped; nothing in it is used."""


def mac(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def hello_mac(secret: bytes, server_nonce: str, client_nonce: str, tz: str) -> str:
    return mac(secret, f"hello|{PROTOCOL}|{server_nonce}|{client_nonce}|{tz}".encode())


def welcome_mac(secret: bytes, server_nonce: str, client_nonce: str) -> str:
    return mac(secret, f"welcome|{PROTOCOL}|{server_nonce}|{client_nonce}".encode())


def session_key(secret: bytes, server_nonce: str, client_nonce: str) -> bytes:
    return hmac.new(secret, f"session|{PROTOCOL}|{server_nonce}|{client_nonce}".encode(), hashlib.sha256).digest()


def _reject_constant(name: str) -> Any:
    raise ProtocolError(f"non-finite JSON constant {name}")


def loads_strict(text: str) -> dict[str, Any]:
    """JSON object with flat, bounded values only (no NaN/Infinity, no nesting)."""
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(f"invalid JSON: {str(exc)[:80]}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("message must be a JSON object")
    for k, v in obj.items():
        if not isinstance(k, str) or len(k) > 32:
            raise ProtocolError("invalid key")
        if isinstance(v, (dict, list)):
            raise ProtocolError(f"{k}: nested values are not allowed")
        if isinstance(v, str) and len(v) > 200:
            raise ProtocolError(f"{k}: string too long")
        if isinstance(v, float) and not math.isfinite(v):
            raise ProtocolError(f"{k}: non-finite number")
        if isinstance(v, int) and not isinstance(v, bool) and abs(v) > 2**53:
            raise ProtocolError(f"{k}: integer out of range")
    return obj


def _num(obj: dict[str, Any], key: str, *, integer: bool = False) -> float | int | None:
    v = obj.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ProtocolError(f"{key} must be a number")
    if integer and (not isinstance(v, int) and not float(v).is_integer()):
        raise ProtocolError(f"{key} must be an integer")
    return int(v) if integer else float(v)


def _free_text(obj: dict[str, Any], key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not _FREE_TEXT.match(v):
        raise ProtocolError(f"{key} must be printable text")
    return v


def _text(obj: dict[str, Any], key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not _SAFE_TEXT.match(v):
        raise ProtocolError(f"{key} must be short plain text")
    return v


@dataclass(frozen=True)
class Hello:
    client: str
    client_version: str
    provider: str
    tz: str
    client_nonce: str
    mac: str


def parse_hello(line: bytes) -> Hello:
    obj = loads_strict(line.decode("utf-8", errors="strict"))
    keys = {"type", "protocol", "client", "client_version", "provider", "tz", "client_nonce", "mac"}
    if set(obj) != keys or obj["type"] != "hello":
        raise ProtocolError("expected a hello message")
    if obj["protocol"] != PROTOCOL:
        raise ProtocolError(f"unsupported protocol {obj['protocol']!r}")
    for k in ("client_nonce", "mac"):
        if not isinstance(obj[k], str) or not HEX64.match(obj[k]):
            raise ProtocolError(f"{k} must be 64 lowercase hex characters")
    return Hello(_text(obj, "client"), _text(obj, "client_version"), _text(obj, "provider"), _text(obj, "tz"),
                 obj["client_nonce"], obj["mac"])


def parse_data_line(line: bytes, key: bytes, expected_seq: int) -> dict[str, Any]:
    """Authenticate, sequence-check and schema-check one data line."""
    if len(line) < 66 or line[64:65] != b" ":
        raise ProtocolError("data line must be '<mac> <json>'")
    tag, body = line[:64].decode("ascii", errors="replace"), line[65:]
    if not HEX64.match(tag) or not hmac.compare_digest(tag, mac(key, body)):
        raise ProtocolError("message authentication failed")
    try:
        obj = loads_strict(body.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        raise ProtocolError("message is not UTF-8") from None
    t = obj.get("type")
    schema = SCHEMAS.get(t) if isinstance(t, str) else None
    if schema is None:
        raise ProtocolError(f"unknown message type {str(t)[:20]!r}")
    keys = set(obj)
    if not schema["required"] <= keys or keys - schema["required"] - schema["optional"]:
        raise ProtocolError(f"{t}: wrong fields")
    seq = _num(obj, "seq", integer=True)
    if seq != expected_seq:
        raise ProtocolError(f"sequence {seq} where {expected_seq} was expected (replay, loss or reordering)")
    if t == "md":
        if obj["kind"] not in ("trade", "quote"):
            raise ProtocolError("md.kind must be 'trade' or 'quote'")
        for k in ("price", "size", "bid", "ask", "bid_size", "ask_size"):
            _num(obj, k)
        eseq = _num(obj, "eseq", integer=True)
        if eseq is None or eseq < 1:
            raise ProtocolError("eseq must be a positive integer")
        _text(obj, "instrument")
        if obj["kind"] == "trade" and (obj.get("price") is None or obj.get("size") is None):
            raise ProtocolError("trade needs price and size")
        if obj["kind"] == "quote" and (obj.get("bid") is None or obj.get("ask") is None):
            raise ProtocolError("quote needs bid and ask")
    elif t == "status":
        if obj["provider_connection"] not in CONNECTION_STATES:
            raise ProtocolError("unknown provider connection state")
        _free_text(obj, "connection_name")
    elif t == "subscription":
        if obj["state"] not in SUBSCRIPTION_STATES:
            raise ProtocolError("unknown subscription state")
        _text(obj, "instrument")
        if "detail" in obj:
            _free_text(obj, "detail")
    elif t == "book_reset":
        _text(obj, "instrument")
    elif t == "error":
        _free_text(obj, "detail")
    for k in ("t_local", "t_utc", "t_bridge"):
        if k in obj and not isinstance(obj[k], str):
            raise ProtocolError(f"{k} must be a string")
    return obj


def encode_data_line(key: bytes, obj: dict[str, Any]) -> bytes:
    """What the bridge sends (used by the mock bridge and the tests)."""
    body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
    return mac(key, body).encode() + b" " + body + b"\n"
