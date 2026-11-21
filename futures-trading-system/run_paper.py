"""Paper trading infrastructure (Sprint 5). SIMULATION ONLY -- no order can reach a broker.

Examples:
    python run_paper.py --new-account --mode infrastructure_validation \\
        --bars MNQ=data/samples/SYNTHETIC_MNQ_1m.csv --replay-start 2025-03-17
    python run_paper.py --new-account --mode replay --bars MES=... MNQ=... --replay-start 2025-03-17 \\
        --news-calendar data/samples/SYNTHETIC_news_calendar.json --report output/paper/report.json
    python run_paper.py --mode paper --realtime-speed 60 --bars MNQ=... --replay-start ...   (simulated real time)
    python run_paper.py --verify                       (verify the journal chain and snapshot binding; the full
                                                        reconciliation runs at every start of a session)

Modes: infrastructure_validation (never authorizes entries), replay (historical data; research
NewsGuard with a point-in-time calendar), paper (operational NewsGuard; needs a live or simulated
real-time feed -- no live provider exists in Sprint 5, so only the simulated one can run).
An existing account is resumed; --new-account refuses to overwrite one.
Exit codes: 0 ok, 1 configuration/data error, 9 state cannot be recovered.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.ai.market_regime.config import DEFAULT_REGIME_CONFIG_PATH, load_regime_config
from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
from src.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, ConfigError, load_config
from src.data.errors import DataError
from src.paper.config import DEFAULT_PAPER_CONFIG_PATH, MODES, load_paper_config
from src.paper.feeds import BarReplayFeed, SimulatedRealtimeFeed, TickReplayFeed, UnconfiguredLiveFeed
from src.paper.journal import Journal, JournalError
from src.paper.metrics import paper_report
from src.paper.runtime import PaperSession, RecoveryError
from src.pipeline import build_market_data
from src.risk.news.config import DEFAULT_NEWS_CONFIG_PATH, load_news_config
from src.strategy.vwap_pullback_config import DEFAULT_STRATEGY_CONFIG_PATH, load_strategy_config


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Paper trading (simulation only).")
    p.add_argument("--config", type=Path, default=DEFAULT_PAPER_CONFIG_PATH)
    p.add_argument("--settings", type=Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument("--backtest-config", type=Path, default=DEFAULT_BACKTEST_CONFIG_PATH)
    p.add_argument("--strategy-config", type=Path, default=DEFAULT_STRATEGY_CONFIG_PATH)
    p.add_argument("--regime-config", type=Path, default=DEFAULT_REGIME_CONFIG_PATH)
    p.add_argument("--news-config", type=Path, default=DEFAULT_NEWS_CONFIG_PATH)
    p.add_argument("--mode", choices=MODES, default=None)
    p.add_argument("--state-dir", type=Path, default=None)
    p.add_argument("--new-account", action="store_true")
    p.add_argument("--bars", nargs="*", default=[], metavar="ROOT=CSV", help="1-minute OHLCV per instrument")
    p.add_argument("--ticks", type=Path, default=None, help="recorded trade/quote CSV (UTC)")
    p.add_argument("--replay-start", default=None, help="first replayed session date; earlier bars = warm-up")
    p.add_argument("--realtime-speed", type=float, default=None, help="simulated real time (x speed)")
    p.add_argument("--news-calendar", type=Path, default=None, help="historical calendar (replay mode)")
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--verify", action="store_true")
    return p.parse_args(argv)


def news_guards(mode, roots, ncfg, calendar_path):
    from src.risk.news.calendar import CalendarStore
    from src.risk.news.classifier import make_classifier
    from src.risk.news.guard import NewsGuard
    from src.risk.news.readiness import readiness_report

    if mode == "replay":
        from src.risk.news.backtest import load_setup

        setup = load_setup(ncfg, calendar_path)  # no calendar -> every entry blocked (fail closed)
        return {r: setup.guard(r) for r in roots}, {"mode": "research", "calendar": setup.calendar_path or None}
    classify = make_classifier(ncfg)
    store_path = PROJECT_ROOT / ncfg.store_path
    store = CalendarStore.load(store_path, classify) if store_path.is_file() else CalendarStore()
    rep = readiness_report(ncfg, store, pd.Timestamp.now(tz="UTC"), classify)
    return ({r: NewsGuard(ncfg, store, r, operational=True) for r in roots},
            {"mode": "operational", "verdict": rep["verdict"], "findings": rep["findings"][:20]})


def _json_safe(v):
    """Standard JSON for reports: NaN / infinity (unknown values) become null."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        pcfg = load_paper_config(a.config)
        if a.mode:
            pcfg = replace(pcfg, mode=a.mode)
        bars = dict(x.split("=", 1) for x in a.bars)
        if bars:
            pcfg = replace(pcfg, instruments=tuple(r.upper() for r in bars))
        state_dir = a.state_dir or (PROJECT_ROOT / pcfg.state_dir)
        db = Path(state_dir) / "paper_state.db"
        if a.verify:
            j = Journal(db)
            n = j.verify()
            snap = j.latest_snapshot()
            print(f"journal OK: {n} entries; snapshot at seq {snap[0] if snap else None}; keyed={j.keyed}")
            return 0
        app, bt = load_config(a.settings), load_backtest_config(a.backtest_config)
        sc, rc, ncfg = load_strategy_config(a.strategy_config), load_regime_config(a.regime_config), \
            load_news_config(a.news_config)
        warm, live = {}, {}
        for root, path in bars.items():
            md = build_market_data(path, app)
            if md.contract.root != root.upper():
                raise ConfigError(f"{path} is {md.contract.root}, not {root}")
            b = md.base
            if a.replay_start:
                sd = md.frames["1min"]["session_date"]
                start = pd.Timestamp(a.replay_start)
                warm[root.upper()], live[root.upper()] = b[(sd < start).to_numpy()], b[(sd >= start).to_numpy()]
            else:
                live[root.upper()] = b
        if pcfg.mode == "paper" and not a.realtime_speed:
            feed = UnconfiguredLiveFeed()
        elif a.ticks:
            feed = TickReplayFeed(a.ticks)
        else:
            if not live:
                raise ConfigError("no market data: pass --bars ROOT=CSV (or --ticks)")
            feed = BarReplayFeed(live)
        if a.realtime_speed:
            feed = SimulatedRealtimeFeed(feed, lambda: pd.Timestamp.now(tz="UTC"), speed=a.realtime_speed)
        guards, readiness = news_guards(pcfg.mode, pcfg.instruments, ncfg, a.news_calendar)
        journal = Journal(db, create=a.new_account)
        session = PaperSession(pcfg, app, bt, sc, rc, guards, feed, journal, warmup=warm,
                               clock=(lambda: pd.Timestamp.now(tz="UTC")), news_readiness=readiness)
    except RecoveryError as exc:
        print(f"Error: paper state cannot be recovered: {exc}. Nothing was traded; fix or archive the state.",
              file=sys.stderr)
        return 9
    except JournalError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 9
    except (ConfigError, DataError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"*** PAPER TRADING -- SIMULATION ONLY ({session.run_kind}); no order can reach a broker ***")
    if readiness.get("mode") == "operational":
        print(f"NewsGuard readiness: {readiness['verdict']}" +
              ("" if readiness["verdict"] == "OPERATIONAL READY" else " -> new entries will be BLOCKED"))
    outcome = session.run()
    rep = paper_report(session)
    print(f"stopped: {outcome}; events {session.seq}; signals {rep['signals']}")
    if rep["trading"]:
        print("trading:", json.dumps(rep["trading"]["overall"], default=str))
    print(rep["disclaimer"])
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(_json_safe(rep), indent=2, default=str, allow_nan=False), encoding="utf-8")
    return 9 if outcome == "system_error" else 0


if __name__ == "__main__":
    sys.exit(main())
