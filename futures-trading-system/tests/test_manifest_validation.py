"""M5 (reproducible run manifests) and M6 (date ranges, IS/OOS, walk-forward, OOS ledger)."""

import json
import shutil

import pandas as pd
import pytest

import run_backtest
from src.backtest.manifest import file_sha256, replay
from src.backtest.validation import (
    OosLedger,
    OosReuseError,
    Period,
    check_no_overlap,
    make_period,
    walk_forward_periods,
)
from src.config import ConfigError
from tests.conftest import SAMPLES

CSV = SAMPLES / "SYNTHETIC_MNQ_1m.csv"
DATES = [pd.Timestamp(d) for d in ("2025-03-10", "2025-03-11", "2025-03-12", "2025-03-13", "2025-03-14",
                                   "2025-03-17", "2025-03-18")]


@pytest.fixture(scope="module")
def full_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("full")
    assert run_backtest.main(["--csv", str(CSV), "--output-dir", str(out)]) == 0
    return out / "r_2"


def test_manifest_contents(full_run):
    m = json.loads((full_run / "manifest.json").read_text())
    assert m["data"]["sha256"] == file_sha256(CSV) and m["data"]["synthetic"] is True
    assert set(m["config_texts"]) == {"settings", "backtest", "strategy", "account_profile"}
    assert m["instrument"]["specification"]["point_value"] == 2.0 and m["instrument"]["contract"] == "MNQH25"
    ds = m["data_settings"]
    assert ds["source_timezone"] == "America/New_York" and ds["column_mapping"]["close"] == "close"
    assert ds["indicators"]["ema_fast"] == 9 and ds["session"]["timezone"] == "America/New_York"
    assert m["execution_assumptions"]["slippage_ticks"]["market_ticks"] == 1
    assert m["account_rules"]["profile"] == "generic_example"
    assert m["strategy"]["target"]["r_multiple"] == 2.0
    assert len(m["git"]["commit"]) in (40, 7) or m["git"]["commit"] == "unknown"
    assert m["environment"]["pandas"] == pd.__version__
    assert m["data"]["file_first_bar"].startswith("2025-03-09") and m["data"]["period_last_bar"]
    assert len(m["results"]["fingerprint"]) == 64


def test_replay_reproduces(full_run):
    rep = replay(full_run / "manifest.json")
    assert rep["reproduced"] and rep["actual"] == rep["expected"]


def test_replay_detects_altered_data_and_results(full_run, tmp_path):
    altered = tmp_path / "SYNTHETIC_MNQ_1m.csv"
    shutil.copy(CSV, altered)
    with altered.open("a", encoding="utf-8") as fh:
        fh.write("2025-03-18 17:30:00,1,1,1,1,1,MNQH25\n")
    with pytest.raises(ConfigError, match="Data file changed"):
        replay(full_run / "manifest.json", altered)
    m = json.loads((full_run / "manifest.json").read_text())
    m["results"]["fingerprint"] = "0" * 64
    forged = tmp_path / "manifest.json"
    forged.write_text(json.dumps(m))
    assert replay(forged)["reproduced"] is False
    assert run_backtest.main(["--replay", str(forged)]) == 3


def test_date_range_restricts_trading(tmp_path):
    assert run_backtest.main(["--csv", str(CSV), "--start", "2025-03-17", "--end", "2025-03-18",
                              "--output-dir", str(tmp_path)]) == 0
    folder = tmp_path / "range" / "r_2"
    eq = pd.read_csv(folder / "equity.csv")
    assert set(eq["session_date"].str[:10]) == {"2025-03-17", "2025-03-18"}
    m = json.loads((folder / "manifest.json").read_text())
    assert m["period"] == {"label": "range", "kind": "range", "fold": None, "start": "2025-03-17", "end": "2025-03-18"}
    assert replay(folder / "manifest.json")["reproduced"]


def test_oos_sealed_reveal_and_ledger(tmp_path):
    base = ["--csv", str(CSV), "--in-sample", "2025-03-10", "2025-03-14",
            "--out-of-sample", "2025-03-17", "2025-03-18", "--output-dir", str(tmp_path)]
    assert run_backtest.main(base) == 0
    assert (tmp_path / "in_sample" / "r_2" / "manifest.json").is_file()
    assert not (tmp_path / "out_of_sample").exists()  # sealed
    assert run_backtest.main(base + ["--reveal-oos"]) == 0
    m = json.loads((tmp_path / "out_of_sample" / "r_2" / "manifest.json").read_text())
    assert m["period"]["kind"] == "out_of_sample" and m["oos"]["other_configurations"] == 0
    # Same configuration again: allowed (no new information used).
    assert run_backtest.main(base + ["--reveal-oos"]) == 0
    # Changed configuration on the same OOS period: refused.
    changed = tmp_path / "strategy.toml"
    changed.write_text((run_backtest.DEFAULT_STRATEGY_CONFIG_PATH).read_text().replace(
        "min_rvol = 1.0", "min_rvol = 1.2"), encoding="utf-8")
    assert run_backtest.main(base + ["--reveal-oos", "--strategy-config", str(changed)]) == 4
    assert run_backtest.main(base + ["--reveal-oos", "--strategy-config", str(changed), "--allow-oos-reuse"]) == 0
    m = json.loads((tmp_path / "out_of_sample" / "r_2" / "manifest.json").read_text())
    assert m["oos"]["reuse_acknowledged"] is True


@pytest.mark.parametrize("extra", [
    ["--in-sample", "2025-03-10", "2025-03-14", "--out-of-sample", "2025-03-14", "2025-03-18"],  # overlap
    ["--out-of-sample", "2025-03-17", "2025-03-18"],                                             # no IS
    ["--in-sample", "2025-03-10", "2025-03-14", "--out-of-sample", "2025-03-17", "2025-03-18",
     "--r-multiples", "1.5", "2"],                                                               # variants on OOS
    ["--start", "2025-03-10", "--walk-forward", "3", "1"],                                       # two modes
    ["--start", "2025-03-18", "--end", "2025-03-10"],                                            # reversed
])
def test_invalid_validation_requests(tmp_path, extra):
    assert run_backtest.main(["--csv", str(CSV), "--output-dir", str(tmp_path), *extra]) == 1


def test_walk_forward_periods_and_cli(tmp_path):
    folds = walk_forward_periods(DATES, train=3, test=1, step=2)
    assert [(f[0].start.day, f[0].end.day, f[1].start.day) for f in folds] == [(10, 12, 13), (12, 14, 17)]
    with pytest.raises(ConfigError):
        walk_forward_periods(DATES, train=7, test=1)
    assert run_backtest.main(["--csv", str(CSV), "--walk-forward", "5", "1", "--wf-step", "1", "--reveal-oos",
                              "--output-dir", str(tmp_path)]) == 0
    for name in ("wf01_in_sample", "wf01_out_of_sample", "wf02_in_sample", "wf02_out_of_sample"):
        assert (tmp_path / name / "r_2" / "manifest.json").is_file()
    m = json.loads((tmp_path / "wf02_out_of_sample" / "r_2" / "manifest.json").read_text())
    assert m["period"] == {"label": "wf02_out_of_sample", "kind": "wf_out_of_sample", "fold": 2,
                           "start": "2025-03-18", "end": "2025-03-18"}


def test_period_helpers(tmp_path):
    is_, oos = make_period("is", "in_sample", "2025-03-10", "2025-03-14"), make_period("o", "out_of_sample",
                                                                                       "2025-03-17", None)
    check_no_overlap(is_, oos)
    with pytest.raises(ConfigError):
        check_no_overlap(is_, make_period("o", "out_of_sample", "2025-03-14", None))
    ledger = OosLedger(tmp_path / "l.json")
    p = Period("o", "out_of_sample", pd.Timestamp("2025-03-17"), pd.Timestamp("2025-03-18"))
    ledger.check_and_record("abc", p, "cfg1", False)
    ledger.check_and_record("abc", p, "cfg1", False)
    with pytest.raises(OosReuseError):
        ledger.check_and_record("abc", p, "cfg2", False)
    ledger.check_and_record("other-data", p, "cfg2", False)  # different data: unaffected
