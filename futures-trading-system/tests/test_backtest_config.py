from pathlib import Path

import pandas as pd
import pytest

from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
from src.config import ConfigError


def test_default_backtest_config():
    c = load_backtest_config()
    assert c.initial_capital == 10_000
    assert c.cost_for("mes").commission_per_contract_per_side == 1.0
    assert c.slippage.market_ticks == 1 and c.slippage.limit_ticks == 0
    assert c.execution.incomplete_signal_bar_policy == "skip"
    assert not c.execution.entries_on_incomplete_bars
    assert c.execution.max_fill_delay == pd.Timedelta(minutes=2)
    assert c.risk.max_daily_loss == 300 and c.risk.max_position_size == 2
    assert len(c.risk.trading_windows) == 1 and not c.risk.emergency_stop
    with pytest.raises(ConfigError):
        c.cost_for("ES")


def _variant(tmp_path: Path, old: str, new: str) -> Path:
    text = DEFAULT_BACKTEST_CONFIG_PATH.read_text(encoding="utf-8")
    assert old in text
    p = tmp_path / "bt.toml"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return p


@pytest.mark.parametrize("old,new", [
    ("initial_capital = 10000.0", "initial_capital = 0"),
    ("market_ticks = 1", "market_ticks = -1"),
    ('incomplete_signal_bar_policy = "skip"', 'incomplete_signal_bar_policy = "use"'),
    ('method = "fixed"', 'method = "kelly"'),
    ("contracts = 1", "contracts = 0"),
    ("max_daily_loss = 300.0", "max_daily_loss = -5"),
    ('trading_windows = ["09:35-15:45"]', 'trading_windows = ["morning"]'),
])
def test_invalid_backtest_config(tmp_path, old, new):
    with pytest.raises(ConfigError):
        load_backtest_config(_variant(tmp_path, old, new))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_backtest_config(tmp_path / "none.toml")
