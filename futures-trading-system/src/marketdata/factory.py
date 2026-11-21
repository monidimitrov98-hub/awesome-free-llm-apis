"""Construction of the live market-data feed from configuration (keeps provider details out of the
paper package and its CLI)."""

from __future__ import annotations

from pathlib import Path

from src.config import ConfigError
from src.marketdata.bridge import BridgeSecretError, NinjaTraderBridgeFeed, load_secret
from src.marketdata.config import load_marketdata_config
from src.marketdata.instruments import InstrumentError, InstrumentResolver


def bridge_feed(instruments: tuple[str, ...], contracts_specs: dict, config_path: str | Path | None = None):
    """The read-only NinjaTrader 8 bridge feed for ``instruments`` (raises ConfigError on any problem)."""
    cfg = load_marketdata_config(config_path)
    if not set(instruments) <= set(cfg.contracts):
        raise ConfigError(f"instruments {instruments} need contracts in config/marketdata.toml")
    try:
        resolver = InstrumentResolver({r: cfg.contracts[r] for r in instruments}, contracts_specs)
        return NinjaTraderBridgeFeed(cfg, load_secret(cfg.secret_file), resolver)
    except (BridgeSecretError, InstrumentError) as exc:
        raise ConfigError(str(exc)) from None
