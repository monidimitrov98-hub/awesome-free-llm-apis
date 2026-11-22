"""Terminal run modes, built only from EXISTING backend capabilities:

* ``mock``   -- the Sprint 6 bridge path (loopback server + authenticated mock bridge) fed with
  SYNTHETIC random-walk prices and synthetic warm-up history. Runs the paper runtime in
  infrastructure_validation mode: entries are never authorized. Always available.
* ``replay`` -- historical 1-minute bars (default: the repository's SYNTHETIC samples) replayed at
  a chosen speed through the replay feed; the research NewsGuard with a point-in-time calendar.
  Simulated (paper) fills. Never live.
* ``shadow`` -- live market data from the read-only NinjaTrader 8 bridge, paper mode, operational
  NewsGuard, simulated fills only. Needs config/marketdata.toml, the bridge secret and a running
  NinjaTrader add-on. If the feed fails it stays failed: there is NO fallback to another provider.

"PAPER" (a separate paper account on a broker's simulation) does not exist in the backend; the GUI
shows it as unavailable.
"""

from __future__ import annotations

import math
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from src.config import PROJECT_ROOT, ConfigError, load_config
from src.paper.feeds import BarReplayFeed

MODES = ("mock", "replay", "shadow")
SAMPLES = {"MES": PROJECT_ROOT / "data" / "samples" / "SYNTHETIC_MES_1m.csv",
           "MNQ": PROJECT_ROOT / "data" / "samples" / "SYNTHETIC_MNQ_1m.csv"}
SAMPLE_CALENDAR = PROJECT_ROOT / "data" / "samples" / "SYNTHETIC_news_calendar.json"


@dataclass
class ModeOptions:
    mode: str = "mock"
    bars: dict[str, Path] = field(default_factory=dict)   # replay / shadow warm-up history
    replay_start: str | None = None
    speed: float = 60.0                                     # replay: market seconds per wall second
    news_calendar: Path | None = None
    state_dir: Path | None = None
    marketdata_config: Path | None = None
    strategy_factory: Callable[[str], Any] | None = None    # TESTS ONLY (deterministic requests)
    tests_disable_regime_gate: bool = False                 # TESTS ONLY (exercise fills end to end)
    mock_seed: int = 7
    mock_rate_hz: float = 4.0


@dataclass
class ModeInfo:
    mode: str
    label: str            # MOCK / REPLAY / SHADOW
    data_label: str       # SYNTHETIC / HISTORICAL / LIVE
    description: str
    feed_kind: str


def prerequisites(opts: ModeOptions) -> list[str]:
    """Problems that prevent a mode from starting (empty = ready). Nothing is substituted."""
    problems = []
    if opts.mode not in MODES:
        return [f"unknown mode {opts.mode!r}"]
    if opts.mode == "replay":
        for root, p in (opts.bars or SAMPLES).items():
            if not Path(p).is_file():
                problems.append(f"replay data for {root} not found: {Path(p).name}")
    if opts.mode == "shadow":
        from src.marketdata.bridge import BridgeSecretError, load_secret
        from src.marketdata.config import load_marketdata_config

        try:
            cfg = load_marketdata_config(opts.marketdata_config)
            load_secret(cfg.secret_file)
        except (ConfigError, BridgeSecretError) as exc:
            problems.append(str(exc))
    return problems


# ---------------------------------------------------------------------------------------------- mock
def synthetic_history(root: str, end: pd.Timestamp, minutes: int, start_price: float, tick: float,
                      seed: int) -> pd.DataFrame:
    """SYNTHETIC 1-minute bars ending just before ``end`` (random walk on the tick grid)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end.floor("min") - pd.Timedelta(minutes=minutes), periods=minutes, freq="1min", tz="UTC")
    steps = rng.integers(-3, 4, size=minutes) * tick
    close = start_price + np.cumsum(steps)
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = rng.integers(0, 3, size=(2, minutes)) * tick
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    vol = rng.integers(20, 400, size=minutes).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


class MockStreamer(threading.Thread):
    """Streams SYNTHETIC level-1 data through the REAL bridge protocol (authenticated, MAC'd)."""

    def __init__(self, port: int, secret: bytes, contracts: dict[str, str], start: dict[str, float],
                 ticks: dict[str, float], seed: int, rate_hz: float) -> None:
        super().__init__(name="terminal-mock-bridge", daemon=True)
        self.port, self.secret, self.contracts = port, secret, contracts
        self.price = dict(start)
        self.ticks = ticks
        self.rng = np.random.default_rng(seed + 1)
        self.rate = rate_hz
        self.stop_event = threading.Event()
        self.sent = 0
        self.error: str | None = None

    def run(self) -> None:
        from src.marketdata.mock_bridge import MockBridge

        b = MockBridge("127.0.0.1", self.port, self.secret, client="fts-terminal-mock")
        for _ in range(100):
            if self.stop_event.is_set():
                return
            try:
                b.connect()
                break
            except OSError:
                time.sleep(0.05)
        else:
            self.error = "mock bridge could not connect"
            return
        try:
            b.status("Connected", "MOCK (synthetic data)")
            for name in self.contracts.values():
                b.subscribe(name)
            next_hb = time.monotonic()
            while not self.stop_event.is_set():
                now = datetime.now(timezone.utc)
                for root, name in self.contracts.items():
                    t = self.ticks[root]
                    self.price[root] = round((self.price[root] + int(self.rng.integers(-2, 3)) * t) / t) * t
                    px = self.price[root]
                    b.quote(name, now, px - t, px, float(self.rng.integers(5, 40)), float(self.rng.integers(5, 40)))
                    b.trade(name, now, px, float(self.rng.integers(1, 12)))
                    self.sent += 2
                if time.monotonic() >= next_hb:
                    b.heartbeat()
                    next_hb = time.monotonic() + 1.0
                self.stop_event.wait(1.0 / self.rate)
        except OSError as exc:
            self.error = f"mock bridge connection ended: {type(exc).__name__}"
        finally:
            b.close()


# ---------------------------------------------------------------------------------------------- replay
class PacedReplayFeed(BarReplayFeed):
    """The replay feed released at ``speed`` x market time (still kind 'replay': event time is the
    runtime clock, so results do not depend on the pace)."""

    name = "paced_bar_replay"

    def __init__(self, bars_by_root: dict[str, pd.DataFrame], speed: float) -> None:
        super().__init__(bars_by_root)
        if not (math.isfinite(speed) and speed > 0):
            raise ConfigError("replay speed must be positive")
        self.speed = speed
        self._t0: float | None = None
        self._first: pd.Timestamp | None = None

    def poll(self, max_events: int = 1000):
        if not self.connected:
            return super().poll(max_events)  # raises FeedError
        if self._t0 is None:
            self._t0, self._first = time.monotonic(), self.peek_time()
        if self._first is None:
            return super().poll(max_events)
        due = self._first + pd.Timedelta(seconds=(time.monotonic() - self._t0) * self.speed)
        n = 0
        while n < max_events and self._pos + n < len(self._events) and self._events[self._pos + n].ts <= due:
            n += 1
        return super().poll(n) if n else []


# ---------------------------------------------------------------------------------------------- build
@dataclass
class Built:
    session: Any
    feed: Any
    info: ModeInfo
    extras: dict[str, Any]
    contracts: dict[str, str]
    stop_when_exhausted: bool
    idle_sleep: float


def build(opts: ModeOptions, *, clock: Callable[[], pd.Timestamp] | None = None) -> Built:
    from src.ai.market_regime.config import load_regime_config
    from src.backtest.config import load_backtest_config
    from src.marketdata.config import load_marketdata_config
    from src.paper.config import load_paper_config
    from src.paper.journal import Journal
    from src.paper.runtime import PaperSession
    from src.risk.news.config import load_news_config
    from src.strategy.vwap_pullback_config import load_strategy_config

    problems = prerequisites(opts)
    if problems:
        raise ConfigError("; ".join(problems))
    app, bt, sc, rc, ncfg = (load_config(), load_backtest_config(), load_strategy_config(), load_regime_config(),
                             load_news_config())
    if opts.tests_disable_regime_gate:
        if opts.strategy_factory is None:
            raise ConfigError("the regime gate can only be disabled for test strategies")
        rc = replace(rc, enabled=False)
    base_pcfg = load_paper_config()
    clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
    ephemeral = opts.state_dir is None and opts.mode in ("mock", "replay")
    state_dir = Path(tempfile.mkdtemp(prefix=f"fts-terminal-{opts.mode}-")) if ephemeral else \
        Path(opts.state_dir or PROJECT_ROOT / "data" / "terminal" / opts.mode)
    db = state_dir / "paper_state.db"
    from run_paper import news_guards  # the same NewsGuard construction as the paper CLI

    if opts.mode == "mock":
        from src.marketdata.bridge import NinjaTraderBridgeFeed
        from src.marketdata.config import MarketDataConfig
        from src.marketdata.instruments import InstrumentResolver

        mcfg_file = load_marketdata_config(opts.marketdata_config)
        contracts = dict(mcfg_file.contracts)
        roots = tuple(contracts)
        secret = secrets.token_bytes(32)  # in-process only: the mock never touches the real secret file
        mcfg = MarketDataConfig("127.0.0.1", 0, Path("unused"), mcfg_file.max_message_bytes, mcfg_file.queue_max_events,
                                5.0, mcfg_file.heartbeat_timeout, mcfg_file.handshake_timeout, 2, contracts)
        feed = NinjaTraderBridgeFeed(mcfg, secret, InstrumentResolver(contracts, app.contracts), clock=clock)
        feed.provider_name = "MOCK bridge (synthetic data)"  # never presented as NinjaTrader
        feed.server.start()
        start = {"MES": 6000.0, "MNQ": 21000.0}
        ticks = {r: app.contracts[r].tick_size for r in roots}
        now = clock()
        warm = {r: synthetic_history(r, now, 3 * 1380, start[r], ticks[r], opts.mock_seed + i)
                for i, r in enumerate(roots)}
        last = {r: float(warm[r]["close"].iloc[-1]) for r in roots}
        streamer = MockStreamer(feed.server.port, secret, contracts, last, ticks, opts.mock_seed, opts.mock_rate_hz)
        pcfg = replace(base_pcfg, mode="infrastructure_validation", instruments=roots)
        guards, readiness = news_guards(pcfg.mode, roots, ncfg, None)
        info = ModeInfo("mock", "MOCK", "SYNTHETIC", "Synthetic prices through the real bridge protocol; "
                        "entries are never authorized (infrastructure validation).", "live")
        extras = {"streamer": streamer}
        stop_when_exhausted, idle = False, 0.01
    elif opts.mode == "replay":
        from src.pipeline import build_market_data

        bars = opts.bars or SAMPLES
        warm, live = {}, {}
        for root, path in bars.items():
            md = build_market_data(path, app)
            b = md.base
            sd = pd.to_datetime(md.frames["1min"]["session_date"])
            sessions = sorted(sd.dropna().unique())
            start = pd.Timestamp(opts.replay_start) if opts.replay_start else (
                sessions[min(2, len(sessions) - 1)] if sessions else None)
            mask = (sd >= start).to_numpy() if start is not None else np.ones(len(b), bool)
            warm[root.upper()], live[root.upper()] = b[~mask], b[mask]
        roots = tuple(live)
        contracts = {r: f"{r} (replay data)" for r in roots}
        feed = PacedReplayFeed(live, opts.speed)
        pcfg = replace(base_pcfg, mode="replay", instruments=roots)
        cal = opts.news_calendar or (SAMPLE_CALENDAR if not opts.bars else None)  # samples: synthetic calendar
        guards, readiness = news_guards("replay", roots, ncfg, cal)
        synthetic = not opts.bars
        info = ModeInfo("replay", "REPLAY", "SYNTHETIC" if synthetic else "HISTORICAL",
                        ("Replay of the repository's SYNTHETIC sample bars" if synthetic else "Replay of historical bars")
                        + f" at {opts.speed:g}x; simulated (paper) fills; not live.", "replay")
        extras = {}
        stop_when_exhausted, idle = True, 0.02
    else:  # shadow
        from src.marketdata.factory import bridge_feed

        mcfg_file = load_marketdata_config(opts.marketdata_config)
        contracts = dict(mcfg_file.contracts)
        roots = tuple(contracts)
        feed = bridge_feed(roots, app.contracts, opts.marketdata_config)
        warm = {}
        if opts.bars:
            from src.pipeline import build_market_data

            warm = {r.upper(): build_market_data(p, app).base for r, p in opts.bars.items()}
        pcfg = replace(base_pcfg, mode="paper", instruments=roots)
        guards, readiness = news_guards("paper", roots, ncfg, None)
        info = ModeInfo("shadow", "SHADOW", "LIVE", "Live NinjaTrader 8 market data; strategy and protections run; "
                        "fills are SIMULATED, nothing is sent to a broker.", "live")
        extras = {}
        stop_when_exhausted, idle = False, 0.005
    journal = Journal(db, create=not db.exists())
    session = PaperSession(pcfg, app, bt, sc, rc, guards, feed, journal, warmup=warm,
                           clock=clock if feed.kind != "replay" else None, news_readiness=readiness,
                           strategy_factory=opts.strategy_factory)
    extras["ephemeral_state"] = ephemeral
    return Built(session, feed, info, extras, contracts, stop_when_exhausted, idle)
