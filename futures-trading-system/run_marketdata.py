"""Live market data (Sprint 6) -- READ-ONLY. No order can be sent by anything started here.

Commands:
    python run_marketdata.py init-secret                 create the shared bridge secret (owner-only file)
    python run_marketdata.py check                       validate the config, secret and contracts (no network)
    python run_marketdata.py monitor                     wait for the NinjaTrader add-on and monitor live data
    python run_marketdata.py monitor --mock --duration 60
                                                         the same pipeline fed by the built-in MOCK bridge
                                                         (SYNTHETIC prices; no NinjaTrader needed)
    python run_marketdata.py mock-bridge --duration 60   run only the mock bridge against a running monitor

The monitor runs the paper runtime in infrastructure_validation mode: every event goes through the
EventValidator, DataHealth, journal, bar builder, indicators, regime engine and strategy, and NO entry
is ever authorized. Its status (for the Sprint 7 GUI) is written atomically to --status-file.
Exit codes: 0 ok, 1 configuration error, 2 feed unavailable, 9 state cannot be recovered / system error.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError, load_config
from src.marketdata.bridge import BridgeSecretError, NinjaTraderBridgeFeed, create_secret, load_secret
from src.marketdata.config import DEFAULT_MARKETDATA_CONFIG_PATH, load_marketdata_config
from src.marketdata.instruments import InstrumentError, InstrumentResolver


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Live market data (read-only).")
    p.add_argument("--config", type=Path, default=DEFAULT_MARKETDATA_CONFIG_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init-secret")
    s.add_argument("--force", action="store_true")
    sub.add_parser("check")
    m = sub.add_parser("monitor")
    m.add_argument("--mock", action="store_true", help="feed the pipeline with the built-in SYNTHETIC mock bridge")
    m.add_argument("--duration", type=float, default=None, help="seconds (default: until Ctrl+C)")
    m.add_argument("--status-file", type=Path, default=PROJECT_ROOT / "output" / "marketdata" / "status.json")
    m.add_argument("--status-every", type=float, default=1.0)
    m.add_argument("--state-dir", type=Path, default=PROJECT_ROOT / "data" / "marketdata_monitor")
    mb = sub.add_parser("mock-bridge")
    mb.add_argument("--duration", type=float, default=60.0)
    mb.add_argument("--rate", type=float, default=20.0)
    return p.parse_args(argv)


def _setup(cfg_path: Path):
    from src.backtest.config import load_backtest_config

    cfg = load_marketdata_config(cfg_path)
    app = load_config()
    resolver = InstrumentResolver(cfg.contracts, {r: app.contracts[r] for r in cfg.contracts})
    for w in resolver.roll_warnings(date.today()):
        print(f"WARNING: {w}")
    expired = [rc.name for rc in resolver.by_root.values() if rc.last_trade_date < date.today()]
    if expired:
        raise ConfigError(f"configured contract(s) {expired} have expired: update config/marketdata.toml")
    return cfg, app, load_backtest_config(), resolver


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        cfg, app, bt, resolver = _setup(a.config)
        if a.cmd == "init-secret":
            create_secret(cfg.secret_file, force=a.force)
            print(f"bridge secret written to {cfg.secret_file} (owner-only). Copy this file to the NinjaTrader "
                  "add-on folder (see docs/sprint6_live_market_data.md). It is never printed or logged.")
            return 0
        secret = load_secret(cfg.secret_file)
        if a.cmd == "check":
            print(f"config OK: listener {cfg.host}:{cfg.port} (loopback only); contracts "
                  f"{', '.join(rc.name for rc in resolver.by_root.values())}; secret file OK")
            return 0
        if a.cmd == "mock-bridge":
            from src.marketdata.mock_bridge import MockBridge, run_mock

            b = MockBridge(cfg.host, cfg.port, secret)
            b.connect()
            n = run_mock(b, cfg.contracts, duration=a.duration, rate_hz=a.rate)
            b.close()
            print(f"mock bridge sent {n} SYNTHETIC events")
            return 0
        return _monitor(a, cfg, app, bt, resolver, secret)
    except (ConfigError, BridgeSecretError, InstrumentError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _monitor(a, cfg, app, bt, resolver, secret) -> int:
    from src.ai.market_regime.config import load_regime_config
    from src.marketdata.status import write_status_file
    from src.paper.config import load_paper_config
    from src.paper.journal import Journal, JournalError
    from src.paper.runtime import PaperSession, RecoveryError
    from src.risk.news.config import load_news_config
    from src.strategy.vwap_pullback_config import load_strategy_config
    from run_paper import news_guards

    pcfg = replace(load_paper_config(), mode="infrastructure_validation", instruments=tuple(cfg.contracts))
    clock = lambda: pd.Timestamp.now(tz="UTC")  # noqa: E731
    feed = NinjaTraderBridgeFeed(cfg, secret, resolver, clock=clock)
    stop = threading.Event()
    mock_thread = None
    if a.mock:
        from src.marketdata.mock_bridge import MockBridge, run_mock

        feed.server.start()  # listen first, then let the mock connect

        def _mock():
            b = MockBridge(cfg.host, feed.server.port, secret)
            for _ in range(50):
                try:
                    b.connect()
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                run_mock(b, cfg.contracts, duration=1e9, stop=stop)  # until the monitor stops
            except OSError:
                pass  # the monitor stopped and closed the connection
            b.close()
        mock_thread = threading.Thread(target=_mock, name="mock-bridge", daemon=True)
        mock_thread.start()
        print("*** MOCK BRIDGE: SYNTHETIC prices, not market data ***")
    db = Path(a.state_dir) / "paper_state.db"
    try:
        guards, _ = news_guards(pcfg.mode, pcfg.instruments, load_news_config(), None)
        journal = Journal(db, create=not db.exists())
        session = PaperSession(pcfg, app, bt, load_strategy_config(), load_regime_config(), guards, feed, journal,
                               clock=clock)
    except (RecoveryError, JournalError) as exc:
        print(f"Error: monitor state cannot be used: {exc}", file=sys.stderr)
        return 9
    print(f"*** MARKET DATA MONITOR -- READ-ONLY; entries are never authorized ({session.run_kind}) ***")
    print(f"listening on {cfg.host}:{cfg.port} (loopback only); waiting for the NinjaTrader bridge ...")
    end = time.monotonic() + a.duration if a.duration else None
    last = {"t": 0.0}

    def on_cycle(s):
        if time.monotonic() - last["t"] >= a.status_every:
            last["t"] = time.monotonic()
            try:
                write_status_file(s.status(), a.status_file)
            except (OSError, ValueError) as exc:
                print(f"status file not written: {exc}", file=sys.stderr)

    try:
        outcome = session.run(stop_when_exhausted=False, idle_sleep=0.005,
                              should_stop=lambda: end is not None and time.monotonic() >= end, on_cycle=on_cycle)
    except KeyboardInterrupt:
        session.shutdown("operator_interrupt")
        outcome = "operator_interrupt"
    finally:
        stop.set()
    try:
        write_status_file(session.status(), a.status_file)
    except (OSError, ValueError):
        pass
    st = session.status()
    print(f"stopped: {outcome}; events processed {st['validation']['events_processed']}; "
          f"rejected {st['validation']['rejected']}; status file {a.status_file}")
    if outcome == "feed_unavailable":
        return 2
    return 9 if outcome == "system_error" else 0


if __name__ == "__main__":
    sys.exit(main())
