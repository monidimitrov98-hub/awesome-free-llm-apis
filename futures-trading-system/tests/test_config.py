from pathlib import Path

import pytest

from src.config import ConfigError, load_config


def test_default_config_loads(config):
    assert config.session.name == "globex"
    assert set(config.contracts) == {"MES", "MNQ"}
    assert config.contracts["MES"].point_value == 5.0
    assert config.contracts["MNQ"].point_value == 2.0
    assert config.timeframes == ["1min", "5min", "15min"]
    assert config.indicators.rsi_period == 14


def test_session_properties(config):
    g = config.sessions["globex"]
    assert g.is_overnight and g.duration.total_seconds() == 23 * 3600
    r = config.sessions["rth"]
    assert not r.is_overnight and r.duration.total_seconds() == 6.5 * 3600


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def _variant(tmp_path: Path, old: str, new: str) -> Path:
    from src.config import DEFAULT_CONFIG_PATH

    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    assert old in text
    p = tmp_path / "cfg.toml"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "old,new",
    [
        ('source_timezone = "America/New_York"', 'source_timezone = "Mars/Olympus"'),
        ('on_error = "raise"', 'on_error = "ignore"'),
        ('timestamp_label = "open"', 'timestamp_label = "middle"'),
        ('active = "globex"', 'active = "nope"'),
        ('timeframes = ["1min", "5min", "15min"]', 'timeframes = ["90s"]'),
        ("rsi_period = 14", "rsi_period = 0"),
        ("min_periods = 5", "min_periods = 50"),
    ],
)
def test_invalid_values_rejected(tmp_path, old, new):
    with pytest.raises(ConfigError):
        load_config(_variant(tmp_path, old, new))
