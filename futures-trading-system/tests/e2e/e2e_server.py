"""END-TO-END TEST SERVER (not for normal use).

Starts the real terminal backend in REPLAY mode on the repository's SYNTHETIC sample bars, with a
deterministic TEST strategy that requests a long entry (with stop and target) on every 6th signal
bar. Every request still passes the real EntryAuthorizer (NewsGuard, AccountGuard, RiskManager);
fills are simulated by the PaperBroker. Used by the Playwright GUI test and the visual audit.

    python tests/e2e/e2e_server.py PORT TOKEN [SPEED]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402

from src.backtest.orders import OrderRequest  # noqa: E402
from src.strategy.base import Strategy  # noqa: E402
from src.terminal.api import create_app  # noqa: E402
from src.terminal.modes import ModeOptions  # noqa: E402
from src.terminal.service import TerminalService  # noqa: E402


class EveryNthBarTestStrategy(Strategy):
    """TEST ONLY: deterministic entry requests so the GUI can be verified end to end."""

    name = "e2e_test_strategy"

    def __init__(self) -> None:
        self.n = 0
        self.diagnostics = []

    def on_bar(self, ctx):
        self.n += 1
        close = float(ctx.bar["close"])
        row = {"time": ctx.now, "bar_time": ctx.bar.name, "direction": "long", "close": close,
               "decision": "skip", "rejection_reason": "e2e_waiting", "reasons": "e2e_waiting"}
        if ctx.position.side is None and not ctx.has_pending_entry and self.n % 6 == 0:
            tick = ctx.contract.tick_size
            sid = f"e2e_test_strategy:long:{ctx.now.isoformat()}"
            row.update(decision="signal", rejection_reason="", reasons="", stop_price=close - 16 * tick,
                       target_price=close + 24 * tick, stop_distance=16 * tick, target_distance=24 * tick, signal_id=sid)
            self.diagnostics.append(row)
            return [OrderRequest.enter("long", stop_loss=close - 16 * tick, take_profit=close + 24 * tick, client_id=sid)]
        self.diagnostics.append(row)
        return []

    def on_fill(self, fill):  # noqa: D401
        return None

    def on_order_event(self, ev):
        return None


def main() -> None:
    """argv: PORT TOKEN SCENARIO [SPEED]; SCENARIO = replay | replay-nogate | mock | mock-fail | shadow-analytics | prop-firm."""
    port, token, scenario = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    speed = float(sys.argv[4]) if len(sys.argv) > 4 else 900.0
    if scenario.startswith("replay"):
        opts = ModeOptions(mode="replay", speed=speed, strategy_factory=lambda root: EveryNthBarTestStrategy(),
                           tests_disable_regime_gate=scenario == "replay-nogate")
    elif scenario == "prop-firm":  # the FundedNext research profile (UNVERIFIED): every entry must be blocked
        opts = ModeOptions(mode="replay", speed=speed, replay_start="2025-03-12",
                           strategy_factory=lambda root: EveryNthBarTestStrategy(), tests_disable_regime_gate=True,
                           prop_profile="fundednext_futures_flex_50k_challenge@2026-11-25.1",
                           prop_initial_balance_from_profile=True)
    elif scenario == "shadow-analytics":  # a fast replay run with simulated trades, analysed in Shadow Analytics
        opts = ModeOptions(mode="replay", speed=speed, replay_start="2025-03-12",
                           strategy_factory=lambda root: EveryNthBarTestStrategy(), tests_disable_regime_gate=True)
    elif scenario == "research":  # Sprint 11: one research run (temporary ledger / registry / promotion store)
        opts = ModeOptions(mode="mock", mock_rate_hz=10, **_research_fixture())
    else:
        opts = ModeOptions(mode="mock", mock_rate_hz=10)
    svc = TerminalService(opts)
    # analyse only this run's own journal (never the developer's data/ journals)
    import tempfile
    from pathlib import Path

    cfg = Path(tempfile.mkdtemp(prefix="fts-e2e-")) / "analytics.toml"
    src = (Path(__file__).resolve().parents[2] / "config" / "analytics.toml").read_text(encoding="utf-8")
    cfg.write_text("\n".join("journals = []" if line.startswith("journals =") else line for line in src.splitlines()),
                   encoding="utf-8")
    svc.analytics_config = cfg
    if scenario == "mock-fail":  # the (mock) provider disappears after 6 s: the GUI must show it
        import threading

        def kill():
            import time

            time.sleep(6)
            svc.built.extras["streamer"].stop_event.set()
        threading.Thread(target=kill, daemon=True).start()
    app = create_app(svc, token=token, port=port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


def _research_fixture() -> dict:
    """A research run of V1-C2-SWING on the synthetic MES sample with the UNVERIFIED FundedNext profile (every
    entry blocked, coverage INCOMPLETE). Everything lives in a temporary folder; the project is never touched."""
    import os
    import tempfile
    from pathlib import Path

    from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
    from src.backtest.validation import ValidationLedger
    from src.config import DEFAULT_CONFIG_PATH, load_config
    from src.research.experiments import ExperimentRegistry
    from src.research.runs import ResearchEnv, run_research

    d = Path(tempfile.mkdtemp(prefix="fts-e2e-research-"))
    os.environ["FTS_LEDGER_PATH"] = str(d / "ledger.jsonl")
    os.environ["FTS_EXPERIMENTS_PATH"] = str(d / "experiments.jsonl")
    ValidationLedger(d / "ledger.jsonl").initialise("e2e")
    env = ResearchEnv(load_config(), load_backtest_config(), d / "out", DEFAULT_CONFIG_PATH, DEFAULT_BACKTEST_CONFIG_PATH,
                      registry=ExperimentRegistry())
    root = Path(__file__).resolve().parents[2]
    run_research(env, "vwap_mean_reversion", "V1-C2-SWING", root / "data/samples/SYNTHETIC_MES_1m.csv",
                 prop_profile="fundednext_futures_flex_50k_challenge@2026-11-25.1")
    from src.histdata.calendar import load_calendar
    from src.histdata.providers import make_adapter
    from src.histdata.store import DatasetStore

    DatasetStore(d / "datasets").ingest("synthetic.mes.1m", make_adapter("generic_csv", timezone="America/New_York"),
                                        [root / "data/samples/SYNTHETIC_MES_1m.csv"], specs=env.app.contracts,
                                        calendar=load_calendar())
    return {"research_root": d / "out", "governance_path": d / "promotions.jsonl", "datasets_root": d / "datasets"}


if __name__ == "__main__":
    main()
