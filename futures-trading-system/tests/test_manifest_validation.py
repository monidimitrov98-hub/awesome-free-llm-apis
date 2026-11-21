"""M5 (reproducible run manifests) and M6 (date ranges, IS/OOS, walk-forward, OOS ledger)."""

import json
import shutil

import pandas as pd
import pytest

import run_backtest
from src.backtest.manifest import file_sha256, replay
from src.backtest.validation import (
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
    # Sprint 4: the regime configuration (and its artifact hashes) is part of the configuration identity.
    assert set(m["config_texts"]) == {"settings", "backtest", "strategy", "account_profile", "regime",
                                      "regime_artifacts"}
    assert m["instrument"]["specification"]["point_value"] == 2.0 and m["instrument"]["contract"] == "MNQH25"
    ds = m["data_settings"]
    assert ds["source_timezone"] == "America/New_York" and ds["column_mapping"]["close"] == "close"
    assert ds["indicators"]["ema_fast"] == 9 and ds["session"]["timezone"] == "America/New_York"
    assert m["execution_assumptions"]["slippage_ticks"]["market_ticks"] == 1
    assert m["account_rules"]["profile"] == "generic_example"
    assert m["strategy"]["target"]["r_multiple"] == 2.0
    assert len(m["git"]["commit"]) in (40, 7) or m["git"]["commit"] == "unknown"
    # Sprint 3 security fixes (H3/M4): the manifest records the code identity and validation fingerprint.
    assert len(m["code"]["source_sha256"]) == 64 and m["code"]["source_files"] > 0
    assert len(m["validation"]["fingerprint"]) == 64 and m["validation"]["contaminated"] is False
    assert m["environment"]["pandas"] == pd.__version__
    assert m["data"]["file_first_bar"].startswith("2025-03-09") and m["data"]["period_last_bar"]
    assert len(m["results"]["fingerprint"]) == 64


def test_replay_rejects_altered_data(full_run, tmp_path):
    # Exact/untrusted replay statuses depend on the code identity and are tested in isolated
    # git repositories (test_validation_integrity.py); altered data is rejected in any state.
    altered = tmp_path / "SYNTHETIC_MNQ_1m.csv"
    shutil.copy(CSV, altered)
    with altered.open("a", encoding="utf-8") as fh:
        fh.write("2025-03-18 17:30:00,1,1,1,1,1,MNQH25\n")
    with pytest.raises(ConfigError, match="Data file changed"):
        replay(full_run / "manifest.json", altered)


def test_date_range_restricts_trading(tmp_path):
    assert run_backtest.main(["--csv", str(CSV), "--start", "2025-03-17", "--end", "2025-03-18",
                              "--output-dir", str(tmp_path)]) == 0
    folder = tmp_path / "range" / "r_2"
    eq = pd.read_csv(folder / "equity.csv")
    assert set(eq["session_date"].str[:10]) == {"2025-03-17", "2025-03-18"}
    m = json.loads((folder / "manifest.json").read_text())
    assert m["period"] == {"label": "range", "kind": "range", "fold": None, "start": "2025-03-17", "end": "2025-03-18"}


def test_oos_sealed_without_reveal(tmp_path):
    # Reveal behaviour (trusted code, ledger) is tested in isolated git repositories.
    assert run_backtest.main(["--csv", str(CSV), "--in-sample", "2025-03-10", "2025-03-14",
                              "--out-of-sample", "2025-03-17", "2025-03-18", "--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / "in_sample" / "r_2" / "manifest.json").is_file()
    assert not (tmp_path / "out_of_sample").exists()  # sealed


@pytest.mark.parametrize("extra", [
    ["--in-sample", "2025-03-10", "2025-03-14", "--out-of-sample", "2025-03-14", "2025-03-18"],  # overlap
    ["--out-of-sample", "2025-03-17", "2025-03-18"],                                             # no IS
    ["--in-sample", "2025-03-10", "2025-03-14", "--out-of-sample", "2025-03-17", "2025-03-18",
     "--r-multiples", "1.5", "2"],                                                               # variants on OOS
    ["--start", "2025-03-10", "--walk-forward", "3", "1"],                                       # two modes
    ["--start", "2025-03-18", "--end", "2025-03-10"],                                            # reversed
    ["--walk-forward", "3", "2", "--wf-step", "1"],                                              # L4: OOS overlap
])
def test_invalid_validation_requests(tmp_path, extra):
    assert run_backtest.main(["--csv", str(CSV), "--output-dir", str(tmp_path), *extra]) == 1


def test_walk_forward_periods(tmp_path):
    folds = walk_forward_periods(DATES, train=3, test=1, step=2)
    assert [(f[0].start.day, f[0].end.day, f[1].start.day) for f in folds] == [(10, 12, 13), (12, 14, 17)]
    with pytest.raises(ConfigError):
        walk_forward_periods(DATES, train=7, test=1)
    with pytest.raises(ConfigError, match="overlap"):
        walk_forward_periods(DATES, train=3, test=2, step=1)  # L4
    oos = [f[1] for f in walk_forward_periods(DATES, train=2, test=2)]
    assert all(a.end < b.start for a, b in zip(oos, oos[1:]))  # disjoint OOS windows


def test_period_helpers():
    is_, oos = make_period("is", "in_sample", "2025-03-10", "2025-03-14"), make_period("o", "out_of_sample",
                                                                                       "2025-03-17", None)
    check_no_overlap(is_, oos)
    with pytest.raises(ConfigError):
        check_no_overlap(is_, make_period("o", "out_of_sample", "2025-03-14", None))
    assert Period("o", "out_of_sample", pd.Timestamp("2025-03-17"), pd.Timestamp("2025-03-18")).is_out_of_sample
