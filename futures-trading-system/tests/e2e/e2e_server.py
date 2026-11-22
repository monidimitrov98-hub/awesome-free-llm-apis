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
    """argv: PORT TOKEN SCENARIO [SPEED]; SCENARIO = replay | replay-nogate | mock | mock-fail."""
    port, token, scenario = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    speed = float(sys.argv[4]) if len(sys.argv) > 4 else 900.0
    if scenario.startswith("replay"):
        opts = ModeOptions(mode="replay", speed=speed, strategy_factory=lambda root: EveryNthBarTestStrategy(),
                           tests_disable_regime_gate=scenario == "replay-nogate")
    else:
        opts = ModeOptions(mode="mock", mock_rate_hz=10)
    svc = TerminalService(opts)
    if scenario == "mock-fail":  # the (mock) provider disappears after 6 s: the GUI must show it
        import threading

        def kill():
            import time

            time.sleep(6)
            svc.built.extras["streamer"].stop_event.set()
        threading.Thread(target=kill, daemon=True).start()
    app = create_app(svc, token=token, port=port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
