"""Sprint 5: clock-driven bar completion in real-time mode, and the run_paper.py CLI."""

from dataclasses import replace

import pandas as pd

import run_paper
from tests.conftest import SAMPLES
from tests.paper_helpers import PCFG, LiveLikeFeed, T, allow_news, long, path, session


class QuietAfterData(LiveLikeFeed):
    """Delivers its events, then stays connected but silent (as a live feed between trades)."""

    def __init__(self, events):
        super().__init__(events)
        self.quiet_polls = 0
        self.stop = False

    @property
    def exhausted(self):
        return self.stop


def test_clock_completes_bars_when_no_more_trades_arrive(tmp_path):
    clock = {"t": T("10:09:50")}
    evs = path(T("10:00"), 10, lambda i: 5000.0)  # last trade 10:09:46: only the clock can complete minute 10:09
    seen = {}

    def sleeper(_):
        clock["t"] += pd.Timedelta(seconds=30)
        if clock["t"] > T("10:12"):
            s.feed.stop = True

    s = session(tmp_path, evs, {T("10:10"): [long("clocked")]}, pcfg=replace(PCFG, mode="paper"),
                news=allow_news(("MES",), operational=True), feed_cls=QuietAfterData, strategies=seen,
                clock=lambda: clock["t"])
    s.sleeper = sleeper
    s.run()
    assert [c.now for c in seen["MES"].contexts] == [T("10:05"), T("10:10")]  # 10:10 decided via the clock
    d = [x for x in s.decisions if x["signal_id"] == "clocked"]
    assert len(d) == 1 and d[0]["allowed"]
    o = next(o for o in s.broker.orders.values() if o.role == "entry")
    # no market event arrived after the decision: nothing was filled at a fabricated price
    assert o.filled_qty == 0 and not list(s.journal.entries("execution"))
    assert o.state.value == "cancelled" and o.reason.startswith("shutdown")


def _regime_off(tmp_path):
    from src.ai.market_regime.config import DEFAULT_REGIME_CONFIG_PATH

    p = tmp_path / "regime_off.toml"
    p.write_text(DEFAULT_REGIME_CONFIG_PATH.read_text().replace("enabled = true", "enabled = false", 1))
    return str(p)


def test_cli_infrastructure_validation_verify_and_refusals(tmp_path, capsys):
    base = ["--state-dir", str(tmp_path / "st"), "--regime-config", _regime_off(tmp_path),
            "--bars", f"MNQ={SAMPLES / 'SYNTHETIC_MNQ_1m.csv'}", "--replay-start", "2025-03-18"]
    rc = run_paper.main(["--new-account", "--mode", "infrastructure_validation", "--report", str(tmp_path / "r.json")]
                        + base)
    out = capsys.readouterr().out
    assert rc == 0 and "SIMULATION ONLY" in out and "no trading statistics" in out
    import json

    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["run_kind"] == "infrastructure_validation" and rep["trading"] is None
    assert run_paper.main(["--verify", "--state-dir", str(tmp_path / "st")]) == 0
    assert run_paper.main(["--new-account", "--mode", "infrastructure_validation"] + base) == 9   # no overwrite
    assert run_paper.main(["--mode", "replay"] + base) == 9  # stored run kind differs: refused, not reset
    assert run_paper.main(["--verify", "--state-dir", str(tmp_path / "none")]) == 9


def test_cli_paper_mode_has_no_live_provider(tmp_path, capsys):
    rc = run_paper.main(["--new-account", "--mode", "paper", "--state-dir", str(tmp_path / "p")])
    out = capsys.readouterr().out
    assert rc == 0 and "stopped: feed_unavailable" in out
