"""Strict loader for ``config/marketdata.toml`` (live market-data bridge)."""

from __future__ import annotations

import ipaddress
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.config import PROJECT_ROOT, ConfigError

DEFAULT_MARKETDATA_CONFIG_PATH = PROJECT_ROOT / "config" / "marketdata.toml"
SECTIONS = {
    "bridge": {"host", "port", "secret_file", "max_message_bytes", "queue_max_events", "connect_timeout_seconds",
               "heartbeat_timeout_seconds", "handshake_timeout_seconds", "max_pending_handshakes"},
    "instruments": {"contracts"},
}


@dataclass(frozen=True)
class MarketDataConfig:
    host: str
    port: int
    secret_file: Path
    max_message_bytes: int
    queue_max_events: int
    connect_timeout: float
    heartbeat_timeout: float
    handshake_timeout: float
    max_pending_handshakes: int
    contracts: dict[str, str]          # root -> exact NinjaTrader contract name, e.g. {"MES": "MES 12-26"}


def _pos(v, name, integer=False, maximum=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ConfigError(f"{name} must be a positive number")
    if integer and not isinstance(v, int):
        raise ConfigError(f"{name} must be an integer")
    if maximum is not None and v > maximum:
        raise ConfigError(f"{name} must be <= {maximum}")
    return v


def load_marketdata_config(path: str | Path | None = None) -> MarketDataConfig:
    p = Path(path) if path else DEFAULT_MARKETDATA_CONFIG_PATH
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read market-data config {p}: {exc}") from exc
    for name, sec in raw.items():
        if name not in SECTIONS:
            raise ConfigError(f"Unknown section [{name}] in {p}")
        if set(sec) - SECTIONS[name]:
            raise ConfigError(f"Unknown key(s) {sorted(set(sec) - SECTIONS[name])} in [{name}]")
        if SECTIONS[name] - set(sec):
            raise ConfigError(f"Missing key(s) {sorted(SECTIONS[name] - set(sec))} in [{name}]")
    for name in SECTIONS:
        if name not in raw:
            raise ConfigError(f"Missing section [{name}] in {p}")
    b = raw["bridge"]
    host = b["host"]
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:  # never a public or LAN listener: only a literal loopback address is accepted
        raise ConfigError("[bridge] host must be a loopback address (127.0.0.1 or ::1)")
    port = _pos(b["port"], "[bridge] port", True, 65535)
    if port < 1024:
        raise ConfigError("[bridge] port must be >= 1024")
    contracts = raw["instruments"]["contracts"]
    if not isinstance(contracts, dict) or not contracts or not set(contracts) <= {"MES", "MNQ"}:
        raise ConfigError("[instruments] contracts must map MES and/or MNQ to NinjaTrader contract names")
    sf = Path(str(b["secret_file"]))
    return MarketDataConfig(
        host=host, port=port, secret_file=sf if sf.is_absolute() else PROJECT_ROOT / sf,
        max_message_bytes=_pos(b["max_message_bytes"], "[bridge] max_message_bytes", True, 65536),
        queue_max_events=_pos(b["queue_max_events"], "[bridge] queue_max_events", True, 1_000_000),
        connect_timeout=float(_pos(b["connect_timeout_seconds"], "[bridge] connect_timeout_seconds")),
        heartbeat_timeout=float(_pos(b["heartbeat_timeout_seconds"], "[bridge] heartbeat_timeout_seconds")),
        handshake_timeout=float(_pos(b["handshake_timeout_seconds"], "[bridge] handshake_timeout_seconds")),
        max_pending_handshakes=_pos(b["max_pending_handshakes"], "[bridge] max_pending_handshakes", True, 64),
        contracts={str(k): str(v) for k, v in contracts.items()})
