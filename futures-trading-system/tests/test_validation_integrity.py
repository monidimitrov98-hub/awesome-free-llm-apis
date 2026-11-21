"""H3 / M3 / M4: OOS reveals and manifest replays must detect code and configuration changes.

Every test runs the real CLI in an isolated, committed COPY of the project (pytest temp
folder); the real repository and its validation ledger are never touched.
"""

import json
import shutil

import pytest

from tests.git_project import CSV, IS_OOS, cli, commit_all, copy_project, needs_git

pytestmark = needs_git


def edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


STRATEGY_PY = ("src/strategy/vwap_pullback.py", "        if r.require_turn and not rsi_now > rsi_prev:",
               "        if r.require_turn and not rsi_now >= rsi_prev:")
ENGINE_PY = ("src/backtest/engine.py", '"""Event-driven backtesting engine', '"""Event-driven  backtesting engine')
CONFIG = ("config/strategy_vwap_pullback.toml", "min_rvol = 1.0", "min_rvol = 1.2")


@pytest.fixture
def project(tmp_path):
    proj = copy_project(tmp_path)
    first = cli(proj, *IS_OOS, "--reveal-oos", "--init-ledger", "--output-dir", "output/run")
    assert first.returncode == 0, first.stderr
    return proj


def reveal(proj, *extra, out="output/run"):
    return cli(proj, *IS_OOS, "--reveal-oos", "--output-dir", out, *extra)


def change(proj, spec, commit=True):
    edit(proj / spec[0], spec[1], spec[2])
    if commit:
        commit_all(proj, f"change {spec[0]}")


# ----------------------------------------------------------------- H3

def test_same_committed_code_can_reveal_again_and_replay_exactly(project):
    assert reveal(project).returncode == 0  # same fingerprint: allowed, recorded twice
    rep = cli(project, "--replay", "output/run/out_of_sample/r_2/manifest.json")
    assert rep.returncode == 0 and "EXACT" in rep.stdout
    m = json.loads((project / "output/run/out_of_sample/r_2/manifest.json").read_text())
    assert m["code"]["trusted"] and len(m["code"]["source_sha256"]) == 64 and m["oos"]["ledger_seq"] >= 1


@pytest.mark.parametrize("spec", [STRATEGY_PY, ENGINE_PY], ids=["strategy_code", "engine_code"])
def test_code_change_blocks_new_reveal(project, spec):
    change(project, spec)
    res = reveal(project)
    assert res.returncode == 4 and "different configuration or code" in res.stderr


def test_config_change_blocks_new_reveal(project):
    change(project, CONFIG)
    assert reveal(project).returncode == 4


def test_dirty_tree_cannot_reveal(project):
    change(project, STRATEGY_PY, commit=False)
    res = reveal(project)
    assert res.returncode == 6 and "uncommitted" in res.stderr
    (project / "scratch.txt").write_text("x", encoding="utf-8")  # untracked file also counts
    edit(project / STRATEGY_PY[0], STRATEGY_PY[2], STRATEGY_PY[1])
    assert reveal(project).returncode == 6


def test_unknown_commit_cannot_reveal(tmp_path):
    proj = copy_project(tmp_path, with_git=False)
    res = cli(proj, *IS_OOS, "--reveal-oos", "--init-ledger", "--output-dir", "output/run")
    assert res.returncode == 6 and "commit unknown" in res.stderr


# ----------------------------------------------------------------- M3

def test_overlap_shift_in_sample_and_output_dir_bypasses_blocked(project):
    change(project, CONFIG)
    shifted = cli(project, "--csv", CSV, "--in-sample", "2025-03-10", "2025-03-13",
                  "--out-of-sample", "2025-03-14", "2025-03-18", "--reveal-oos", "--output-dir", "output/run")
    partial = cli(project, "--csv", CSV, "--in-sample", "2025-03-10", "2025-03-12",
                  "--out-of-sample", "2025-03-13", "2025-03-17", "--reveal-oos", "--output-dir", "output/run")
    in_sample = cli(project, "--csv", CSV, "--in-sample", "2025-03-10", "2025-03-18", "--output-dir", "output/run")
    other_dir = reveal(project, out="output/elsewhere")
    full = cli(project, "--csv", CSV, "--output-dir", "output/run")
    assert [r.returncode for r in (shifted, partial, in_sample, other_dir, full)] == [4, 4, 4, 4, 4]


def test_code_change_and_new_output_dir_together_blocked(project):
    change(project, STRATEGY_PY)
    assert reveal(project, out="output/brand_new_dir").returncode == 4


def test_explicit_reuse_is_marked_contaminated(project):
    change(project, CONFIG)
    res = reveal(project, "--allow-oos-reuse")
    assert res.returncode == 0 and "CONTAMINATED" in res.stdout
    m = json.loads((project / "output/run/out_of_sample/r_2/manifest.json").read_text())
    assert m["validation"]["contaminated"] and m["oos"]["reuse_acknowledged"]


def test_in_sample_with_same_fingerprint_allowed(project):
    res = cli(project, "--csv", CSV, "--in-sample", "2025-03-10", "2025-03-18", "--output-dir", "output/run")
    assert res.returncode == 0


def test_missing_and_corrupt_ledger_fail_safely(tmp_path):
    proj = copy_project(tmp_path)
    assert reveal(proj).returncode == 5  # no ledger and no --init-ledger
    assert cli(proj, "--init-ledger").returncode == 0
    assert reveal(proj).returncode == 0
    ledger = proj / "validation_history" / "oos_ledger.jsonl"
    ledger.write_text(ledger.read_text(encoding="utf-8")[:-10], encoding="utf-8")  # truncate
    for args in ((*IS_OOS, "--reveal-oos", "--output-dir", "output/run"), ("--csv", CSV, "--output-dir", "output/run"),
                 ("--verify-ledger",)):
        res = cli(proj, *args)
        assert res.returncode == 5 and "truncated" in res.stderr, args
    assert ledger.exists()  # not replaced


def test_walk_forward_reveals_share_one_fingerprint(tmp_path):
    proj = copy_project(tmp_path)
    res = cli(proj, "--csv", CSV, "--walk-forward", "5", "1", "--reveal-oos", "--init-ledger", "--output-dir", "output/wf")
    assert res.returncode == 0, res.stderr
    records = [json.loads(x) for x in (proj / "validation_history/oos_ledger.jsonl").read_text().splitlines()]
    reveals = [r for r in records if r["type"] == "oos_reveal"]
    assert [(r["start"], r["end"]) for r in reveals] == [("2025-03-17", "2025-03-17"), ("2025-03-18", "2025-03-18")]
    assert len({r["fingerprint"] for r in reveals}) == 1


# ----------------------------------------------------------------- M4

@pytest.fixture
def manifest(project):
    return project / "output/run/out_of_sample/r_2/manifest.json"


def replay(project, manifest_path, *extra):
    return cli(project, "--replay", str(manifest_path), *extra)


@pytest.mark.parametrize("spec", [STRATEGY_PY, ENGINE_PY], ids=["strategy_code", "engine_code"])
def test_replay_with_different_code_is_untrusted(project, manifest, spec):
    change(project, spec)
    res = replay(project, manifest)
    assert res.returncode == 6 and "UNTRUSTED_CODE" in res.stdout and "Python source differs" in res.stdout


def test_replay_on_dirty_tree_is_untrusted(project, manifest):
    change(project, ENGINE_PY, commit=False)
    res = replay(project, manifest)
    assert res.returncode == 6 and "uncommitted" in res.stdout


def test_replay_of_unknown_commit_manifest_is_untrusted(tmp_path, project):
    nogit = copy_project(tmp_path / "nogit", with_git=False)
    run = cli(nogit, "--csv", CSV, "--start", "2025-03-17", "--end", "2025-03-18", "--output-dir", "output/run")
    assert run.returncode == 0
    res = replay(project, nogit / "output/run/range/r_2/manifest.json")
    assert res.returncode == 6 and "UNTRUSTED_CODE" in res.stdout


def test_replay_detects_configuration_tampering(project, manifest, tmp_path):
    m = json.loads(manifest.read_text())
    m["config_texts"]["strategy"] = m["config_texts"]["strategy"].replace("min_rvol = 1.0", "min_rvol = 0.5")
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(m))
    res = replay(project, forged)
    assert res.returncode == 3 and "MANIFEST_INCONSISTENT" in res.stdout


def test_replay_detects_different_data(project, manifest, tmp_path):
    altered = tmp_path / "data.csv"
    shutil.copy(project / CSV, altered)
    with altered.open("a", encoding="utf-8") as fh:
        fh.write("2025-03-18 17:30:00,1,1,1,1,1,MNQH25\n")
    res = replay(project, manifest, "--csv", str(altered))
    assert res.returncode == 1 and "Data file changed" in res.stderr


def test_replay_reports_different_dependency_versions(project, manifest, tmp_path):
    m = json.loads(manifest.read_text())
    m["environment"]["pandas"] = "0.0.0-other"
    other = tmp_path / "other_env.json"
    other.write_text(json.dumps(m))
    res = replay(project, other)
    assert res.returncode == 7 and "EXACT_DIFFERENT_DEPENDENCIES" in res.stdout


def test_replay_detects_result_mismatch(project, manifest, tmp_path):
    m = json.loads(manifest.read_text())
    m["results"]["fingerprint"] = "0" * 64
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(m))
    res = replay(project, forged)
    assert res.returncode == 3 and "MISMATCH" in res.stdout
