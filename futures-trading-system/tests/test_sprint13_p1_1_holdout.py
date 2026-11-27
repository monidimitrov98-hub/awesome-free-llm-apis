"""Sprint 13 P1.1: final out-of-sample HOLDOUT protection - claim lifecycle, first-access ordering, Git-rollback
anchor, reserved-bar isolation for research, and adversarial cases.

Generated (SYNTHETIC-valued) prices only. Files carry no SYNTHETIC marker so that the real-data paths (plans,
reveals) are exercised; the holdout applies whatever the marker says. The claim ledger, its anchor and their locks are
redirected to a temporary directory for EVERY test (tests/conftest.py); tests that need ledgers create them there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.backtest.validation import Period
from src.config import ConfigError, load_config
from src.histdata.providers import make_adapter
from src.histdata.store import DatasetStore
from src.research import holdout as H
from src.research import holdout_ledger as HL
from tests.histdata_fixtures import CAL, bars, write_databento, write_generic

ROOT = Path(__file__).resolve().parents[1]
APP = load_config()
DB = make_adapter("databento_ohlcv_csv")
TRUSTED = {"commit": "a" * 40, "dirty": False, "source_sha256": "s" * 64, "source_files": 1, "trusted": True,
           "untrusted_reasons": []}


@pytest.fixture()
def ledgers(monkeypatch):
    """Fresh claim ledger + anchor (in the conftest's temporary directory); the Git-commit check is relaxed for
    temporary files (it is tested on a real repository in test_git_rollback_*)."""
    monkeypatch.delenv(H.EXTRA_ENV, raising=False)
    monkeypatch.setattr(HL, "uncommitted_reason", lambda: None)
    HL._create_new("test ledger")
    return HL.LEDGER_FILE.parent


@pytest.fixture(scope="module")
def fx_cross():
    return bars(start="2025-12-08 00:00", end="2026-01-24 00:00", contracts=("MESH26",), roll_day="2027-01-01")


def _binding(start="2026-01-05", end="2026-01-23", root="MES", **kw):
    b = {k: "x" for k in HL.BINDING_KEYS}
    b.update(root=root, start=start, end=end, reservation="mes-mnq-2026", policy_sha256=H.load_policy().sha256)
    b.update(kw)
    return b


def _token(**kw):
    b = _binding(**kw)
    cid, owner = H._claim_segment(b, None)
    return H._issue_token(cid, b, owner)


def _state(cid):
    return HL.claims()[cid]["state"]


class Spy:
    """Records every read of dataset data files and every indicator / feature computation, with the claim states
    at that moment."""

    def __init__(self, monkeypatch):
        import src.histdata.schema as schema
        import src.histdata.store as store
        import src.pipeline as pipeline

        self.reads, self.indicators = [], []
        real_sha, real_read, real_ind = store.file_sha256, schema.read_bars, pipeline.add_indicators

        def states():
            try:
                return sorted(c["state"] for c in HL.claims().values())
            except Exception:                         # noqa: BLE001 - recorded, not raised
                return ["<unreadable>"]

        def sha(p):
            self.reads.append(("hash", Path(p).name, states()))
            return real_sha(p)

        def read(p):
            self.reads.append(("bars", Path(p).name, states()))
            return real_read(p)

        def ind(frame, *a, **k):
            self.indicators.append(pd.DatetimeIndex(frame.index).max())
            return real_ind(frame, *a, **k)

        monkeypatch.setattr(store, "file_sha256", sha)
        monkeypatch.setattr(schema, "read_bars", read)
        monkeypatch.setattr(pipeline, "add_indicators", ind)


# ============================================================================ policy and embargo
def test_the_committed_policy_and_the_trading_date_embargo():
    pol = H.load_policy()
    assert pol.path == H.DEFAULT_FILE and pol.version == 2
    r = pol.reservation("MES")
    assert r is pol.reservation("MNQ") and r.id == "mes-mnq-2026"
    assert (str(r.start.date()), r.end, r.embargo_trade_dates, r.access) == ("2026-01-01", None, 10, "final_oos_only")
    assert str(r.blocked_start.date()) == "2025-12-17"
    assert r.embargo_dates() == ["2025-12-31", "2025-12-30", "2025-12-29", "2025-12-26", "2025-12-24", "2025-12-23",
                                 "2025-12-22", "2025-12-19", "2025-12-18", "2025-12-17"]
    assert set(APP.contracts) <= set(r.instruments) and pol.reservation("MES:synthetic") is r
    assert not r.touches("2025-01-02", "2025-12-16") and r.touches("2025-01-02", "2025-12-17")


@pytest.mark.parametrize("text,match", [
    ("[policy]\nid='x'\nversion=1\n", "no reservation"),
    ("reservations=[]\n[policy]\nid='x'\nversion=1\n", "no reservation"),
    ("not toml ===", "unreadable"),
    ("[policy]\nid='x'\nversion=0\n[[reservations]]\nid='r'\ninstruments=['MES']\nstart='2026-01-01'\n", "version"),
    ("[policy]\nid='x'\nversion=1\n[[reservations]]\nid='r'\ninstruments=['MES']\nstart='2026-01-01'\naccess='open'\n",
     "access"),
])
def test_a_broken_canonical_or_extra_policy_fails_closed(tmp_path, monkeypatch, text, match):
    p = tmp_path / "bad.toml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(H, "DEFAULT_FILE", p)
    with pytest.raises(H.HoldoutError, match=match):
        H.check_research("MES", "2024-01-02", "2024-01-31", "research")
    monkeypatch.undo()
    monkeypatch.setenv(H.EXTRA_ENV, str(p))
    with pytest.raises(H.HoldoutError, match=match):
        H.check_research("MES", "2024-01-02", "2024-01-31", "research")


def test_environment_overrides_can_only_add_restrictions(tmp_path, monkeypatch):
    permissive = tmp_path / "permissive.toml"
    permissive.write_text("[policy]\nid='p'\nversion=1\n[[reservations]]\nid='late'\ninstruments=['MES']\n"
                          "start='2099-01-01'\n", encoding="utf-8")
    monkeypatch.setenv("FTS_HOLDOUT_FILE", str(permissive))                    # no replacement variable exists
    monkeypatch.setenv(H.EXTRA_ENV, str(permissive))                           # an extra policy only ADDS
    with pytest.raises(H.HoldoutError):
        H.check_research("MES", "2026-02-02", "2026-02-27", "research")
    clash = tmp_path / "clash.toml"
    clash.write_text("[policy]\nid='c'\nversion=1\n[[reservations]]\nid='mes-mnq-2026'\ninstruments=['MES']\n"
                     "start='2099-01-01'\n", encoding="utf-8")
    monkeypatch.setenv(H.EXTRA_ENV, str(clash))
    with pytest.raises(H.HoldoutError, match="can only ADD"):
        H.load_policy()


# ============================================================================ (8) ledgers fail closed
def test_damaged_missing_or_inconsistent_ledgers_fail_closed(ledgers):
    _token()
    lf = HL.LEDGER_FILE
    lines = lf.read_text(encoding="utf-8").splitlines()
    lf.write_text("\n".join([lines[0], lines[1].replace('"MES"', '"MNQ"')] + lines[2:]) + "\n", encoding="utf-8")
    with pytest.raises(HL.HoldoutLedgerError, match="fails verification"):
        HL.claims()
    lf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    HL.claims()
    with lf.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 99, "type": "claim_consumed"')                       # interrupted write
    with pytest.raises(HL.HoldoutLedgerError, match="truncated"):
        HL.claims()
    lf.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")              # a removed last record
    with pytest.raises(HL.HoldoutLedgerError, match="does not match"):
        HL.claims()
    lf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    HL.ANCHOR_HEAD.unlink()                                                     # anchor incomplete
    with pytest.raises(HL.HoldoutLedgerError, match="anchor incomplete"):
        HL.claims()
    HL.ANCHOR_FILE.unlink()
    HL._anchor().create({"type": "genesis", "note": "a NEW anchor"})          # a recreated anchor is not THE anchor
    with pytest.raises(HL.HoldoutLedgerError, match="do not belong together"):
        H._claim_segment(_binding(start="2026-03-02", end="2026-03-31"), None)
    with pytest.raises(HL.HoldoutLedgerError, match="never overwritten"):
        HL._create_new("reset attempt")


def test_impossible_transitions_and_unlocked_appends_are_refused(ledgers):
    t = _token()
    with HL.locked():
        with pytest.raises(HL.HoldoutLedgerError, match="cannot go CLAIMED -> COMPLETED"):
            HL.append({"type": "claim_completed", "claim_id": t.claim_id, "results": {}})
    with pytest.raises(HL.HoldoutLedgerError, match="without the ledger lock"):
        HL.append({"type": "claim_aborted", "claim_id": t.claim_id})
    assert _state(t.claim_id) == HL.CLAIMED


def test_interrupted_writes_are_repaired_extend_only(ledgers):
    t = _token()
    hf = HL.HEAD_FILE
    good_head = hf.read_text(encoding="utf-8")
    hf.write_text(json.dumps({"count": 2, "last_hash": HL._ledger().scan()[0][1]["record_hash"]}), encoding="utf-8")
    with pytest.raises(HL.HoldoutLedgerError, match="does not match"):     # head lags: fail closed
        HL.claims()
    with HL.LEDGER_FILE.open("a", encoding="utf-8") as fh:
        fh.write('{"partial": ')
    with pytest.raises(H.HoldoutError, match="--by and --reason"):
        HL.repair(by="", reason="")
    done = HL.repair(by="tester", reason="interrupted append")
    assert any("preserved" in d for d in done) and any("head moved forward" in d for d in done)
    assert list(HL.LEDGER_FILE.parent.glob("holdout_ledger.jsonl.partial-*"))   # never discarded
    assert hf.read_text(encoding="utf-8") == good_head and _state(t.claim_id) == HL.CLAIMED
    hf.write_text(json.dumps({"count": 99, "last_hash": "x"}), encoding="utf-8")   # head AHEAD of the records
    with pytest.raises(HL.HoldoutLedgerError, match="not repairable without discarding history"):
        HL.repair(by="tester", reason="head ahead")


# ============================================================================ (4) Git rollback
def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


@pytest.mark.parametrize("rollback", [("checkout", "--", "."), ("restore", "."), ("stash",), ("reset", "--hard")])
def test_git_rollback_cannot_erase_claim_history(tmp_path, monkeypatch, rollback):
    repo, outside = tmp_path / "repo", tmp_path / "ignored"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    monkeypatch.setattr(HL, "LEDGER_FILE", repo / "holdout_ledger.jsonl")
    monkeypatch.setattr(HL, "HEAD_FILE", repo / "holdout_ledger.head.json")
    monkeypatch.setattr(HL, "ANCHOR_FILE", outside / "holdout_anchor.jsonl")     # outside Git (like git-ignored)
    monkeypatch.setattr(HL, "ANCHOR_HEAD", outside / "holdout_anchor.head.json")
    HL._create_new("git test")
    assert HL.uncommitted_reason() == "the claim ledger files are not tracked by Git"
    with pytest.raises(H.HoldoutError, match="not tracked"):
        H._claim_segment(_binding(), None)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "genesis")
    t = _token()
    H.consume(t)                                                               # CONSUMED: the dates are used up
    assert HL.uncommitted_reason().startswith("the claim ledger has uncommitted changes")
    with pytest.raises(H.HoldoutError, match="uncommitted"):                   # no new claim before a commit
        H._claim_segment(_binding(start="2026-03-02", end="2026-03-31"), None)
    _git(repo, *rollback)                                                       # an ordinary Git rollback
    assert HL.LEDGER_FILE.read_text(encoding="utf-8").count("\n") == 1          # the tracked file lost the claim
    with pytest.raises(HL.HoldoutLedgerError, match="BEHIND its anchor"):
        HL.claims()                                                            # ... but every operation fails closed
    with pytest.raises(HL.HoldoutLedgerError, match="BEHIND its anchor"):
        H._claim_segment(_binding(), None)
    done = HL.repair(by="tester", reason="git rollback")                       # replays the anchor
    assert any("replayed from the anchor" in d for d in done)
    assert _state(t.claim_id) == HL.CONSUMED                                   # the consumed claim is back
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "claim records")
    with pytest.raises(H.HoldoutError, match="overlaps holdout claim"):
        H._claim_segment(_binding(), None)                                    # never a second evaluation


# ============================================================================ (7) concurrency, crashes
CHILD = r'''
import json, os, sys, time, traceback
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from src.research import holdout_ledger as HL, holdout as H
d = Path(sys.argv[2])
HL.LEDGER_FILE, HL.HEAD_FILE = d / "holdout_ledger.jsonl", d / "holdout_ledger.head.json"
HL.ANCHOR_FILE, HL.ANCHOR_HEAD = d / "holdout_anchor.jsonl", d / "holdout_anchor.head.json"
HL.LOCK_DIR = d / "locks"
HL.uncommitted_reason = lambda: None
mode, binding = sys.argv[3], json.loads(sys.argv[4])
if mode == "prepared":
    with HL.locked():
        HL.append({"type": "claim_prepared", "claim_id": "p" + str(os.getpid()), "binding": binding,
                   "monte_carlo": None, "owner": HL.owner_info()})
    print("prepared p" + str(os.getpid()), flush=True)
    os._exit(0)
while not (d / "go").exists():
    time.sleep(0.005)
try:
    cid, owner = H._claim_segment(binding, None)
except Exception as exc:
    print("refused", type(exc).__name__, repr(exc)[:160], flush=True)
    os._exit(0)
print("claimed", cid, flush=True)
if mode == "consume":
    with HL.locked():
        HL.append({"type": "claim_consumed", "claim_id": cid, "owner": HL.owner_info()})
    print("consumed", flush=True)
if mode == "sleep":
    time.sleep(120)
os._exit(0)                      # a crash: nothing is aborted or completed
'''


def _spawn(d, mode, binding):
    return subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT), str(d), mode, json.dumps(binding)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_concurrent_processes_cannot_both_claim_overlapping_dates(ledgers):
    d = ledgers
    for n, month in enumerate(("02", "03", "04", "05", "06"), 1):
        go = d / "go"
        go.unlink(missing_ok=True)
        procs = [_spawn(d, "exit", _binding(start=f"2026-{month}-02", end=f"2026-{month}-20")) for _ in range(4)]
        time.sleep(1.0)
        go.write_text("go")
        outs = [p.communicate(timeout=120)[0].strip() for p in procs]
        assert sum(o.startswith("claimed") for o in outs) == 1, outs
        assert sum(o.startswith("refused HoldoutError") and "overlaps holdout claim" in o for o in outs) == 3, outs
        assert sorted(c["state"] for c in HL.claims().values()) == sorted([HL.ABORTED] * 3 * n + [HL.CLAIMED] * n)


def test_crash_recovery_releases_only_provably_unaccessed_claims(ledgers):
    d = ledgers
    (d / "go").write_text("go")
    out = _spawn(d, "prepared", _binding(start="2026-02-02", end="2026-02-13")).communicate(timeout=120)[0]
    assert H.recover_claim(out.split()[1], by="t", reason="owner crashed after PREPARED") == HL.ABORTED
    p = _spawn(d, "sleep", _binding(start="2026-03-02", end="2026-03-13"))
    cid = p.stdout.readline().split()[1]
    with pytest.raises(H.HoldoutError, match="still holds the claim lock"):
        H.recover_claim(cid, by="t", reason="too early")
    p.kill()
    p.communicate(timeout=60)
    assert H.recover_claim(cid, by="t", reason="owner killed before CONSUMED") == HL.ABORTED
    again, owner = H._claim_segment(_binding(start="2026-03-02", end="2026-03-13"), None)
    owner.release()
    assert _state(again) == HL.CLAIMED                                         # free again
    p = _spawn(d, "consume", _binding(start="2026-04-01", end="2026-04-30"))
    cid = p.communicate(timeout=120)[0].split()[1]
    assert _state(cid) == HL.CONSUMED
    with pytest.raises(H.HoldoutError, match="stays consumed for good"):
        H.recover_claim(cid, by="t", reason="try to release")
    with pytest.raises(H.HoldoutError, match="overlaps holdout claim"):
        H._claim_segment(_binding(start="2026-04-15", end="2026-05-15"), None)  # partial overlap


# ============================================================================ (9) tokens
def test_tokens_are_bound_single_use_and_cannot_be_forged(ledgers, monkeypatch, tmp_path):
    t = _token(data_sha256="d1", version_sha256="v1", plan="p1", eval_config_sha256="e1", dataset_ref="ds@v1",
               dataset_manifest_sha256="m1", bars_content_sha256="c1")
    with pytest.raises(H.HoldoutError, match="claimed, not consumed"):
        H.verify_binding(t, data_sha256="d1")                                  # not opened yet
    forged = H.FinalOOSAccess(t.claim_id, "0" * 32, t.binding)
    with pytest.raises(H.HoldoutError, match="unknown final-OOS access"):
        H.consume(forged)
    H.consume(t)
    assert _state(t.claim_id) == HL.CONSUMED
    for k, v in (("data_sha256", "d2"), ("version_sha256", "v2"), ("plan", "p2"), ("eval_config_sha256", "e2"),
                 ("start", "2026-01-06"), ("end", "2026-01-22"), ("root", "MNQ"), ("dataset_ref", "other@v1")):
        with pytest.raises(H.HoldoutError, match=f"claimed for {k}"):
            H.verify_binding(t, **{k: v})
    ident = {"ref": "ds@v1", "manifest_sha256": "m1", "bars_content_sha256": "c1"}
    H.check_access(t, root="MES", start="2026-01-05", end="2026-01-23", dataset_identity=ident)
    for bad in ({**ident, "bars_content_sha256": "c2"}, {**ident, "ref": "x@v1"}, None):     # (L2) runtime data
        with pytest.raises(H.HoldoutError):
            H.check_access(t, root="MES", start="2026-01-05", end="2026-01-23", dataset_identity=bad)
    with pytest.raises(H.HoldoutError, match="consumed"):
        H.consume(t)                                                           # single use
    H.mark_monte_carlo(t)
    with pytest.raises(H.HoldoutError, match="already produced"):              # (L5)
        H.mark_monte_carlo(t)
    assert H.abort_unaccessed(t, "late failure") == HL.CONSUMED                # never released after access
    with pytest.raises(H.HoldoutError, match="unknown"):
        H.complete_claim(t, {})
    # (L3) a policy change between the claim and CONSUMED is caught under the lock
    t2 = _token(start="2026-02-02", end="2026-02-27")
    extra = tmp_path / "x.toml"
    extra.write_text("[policy]\nid='x'\nversion=1\n[[reservations]]\nid='nq'\ninstruments=['MNQ']\n"
                     "start='2024-01-02'\nend='2024-01-31'\n", encoding="utf-8")
    monkeypatch.setenv(H.EXTRA_ENV, str(extra))
    with pytest.raises(H.HoldoutError, match="policy changed"):
        H.consume(t2)
    assert H.abort_unaccessed(t2, "policy changed") == HL.ABORTED


# ============================================================================ (5, 6) research never sees reserved bars
@pytest.fixture()
def research(tmp_path, monkeypatch, ledgers):
    import src.backtest.validation as V
    from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
    from src.backtest.validation import ValidationLedger
    from src.config import DEFAULT_CONFIG_PATH
    from src.research import governance as G
    from src.research import plans as P
    from src.research.experiments import ExperimentRegistry
    from src.research.runs import ResearchEnv

    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(V, "CANONICAL_LEDGER", ledger.resolve())          # the canonical ledger, test copy
    monkeypatch.delenv("FTS_LEDGER_PATH", raising=False)
    monkeypatch.setenv("FTS_EXPERIMENTS_PATH", str(tmp_path / "exp.jsonl"))
    (tmp_path / "plans").mkdir()
    monkeypatch.setattr(P, "DEFAULT_DIR", tmp_path / "plans")              # the canonical plans directory, test copy
    monkeypatch.setenv("FTS_PLANS_DIR", str(tmp_path / "plans"))
    monkeypatch.setattr(G, "GOVERNANCE_FILE", tmp_path / "prom.jsonl")
    ValidationLedger(ledger).initialise("t")                              # recorded in the anchor (canonical)
    store = DatasetStore(tmp_path / "ds")
    monkeypatch.setenv("FTS_DATASETS_ROOT", str(tmp_path / "ds"))
    pre = bars(start="2025-12-01 00:00", end="2025-12-16 22:00", contracts=("MESH26",), roll_day="2027-01-01")
    oos = bars(start="2026-01-01 23:00", end="2026-01-24 00:00", contracts=("MESH26",), roll_day="2027-01-01")
    cross = bars(start="2025-12-01 00:00", end="2026-01-24 00:00", contracts=("MESH26",), roll_day="2027-01-01")
    refs = {}
    for name, frame in (("research", pre), ("oos", oos), ("cross", cross)):
        r = store.ingest(f"mes.h26.{name}", DB, [write_databento(frame, tmp_path / f"{name}.csv")], specs=APP.contracts,
                         calendar=CAL)
        assert r["status"] == "VALIDATED", (name, r)
        refs[name] = r["ref"]
    env = ResearchEnv(APP, load_backtest_config(), tmp_path / "out", DEFAULT_CONFIG_PATH, DEFAULT_BACKTEST_CONFIG_PATH,
                      registry=ExperimentRegistry(), store=store)
    return SimpleNamespace(env=env, store=store, refs=refs, tmp=tmp_path)


def test_research_on_a_dataset_reaching_the_window_reads_no_byte(research, monkeypatch):
    from src.research import runs as R

    spy = Spy(monkeypatch)
    src = f"dataset:{research.refs['cross']}"
    for kw in ({"purpose": "full"}, {"purpose": "train", "start": "2025-12-08", "end": "2025-12-12"}):
        with pytest.raises(H.HoldoutError, match="refused from the manifest; no data byte was read"):
            R.run_research(research.env, "vwap_mean_reversion", "V1-C2-SWING", src, **kw)
    with pytest.raises(H.HoldoutError, match="no data byte"):
        R.sweep(research.env, "vwap_mean_reversion", "V1-C2-SWING", src, {"indicators.band_k": [2.0, 2.5]},
                purpose="train", start="2025-12-08", end="2025-12-12")
    assert spy.reads == [] and spy.indicators == []                            # nothing read, nothing computed
    assert research.env.registry.attempts() == []                              # refusals are never attempts


def test_allowed_research_never_computes_on_reserved_bars(research, monkeypatch):
    from src.research import runs as R

    spy = Spy(monkeypatch)
    ok = R.run_research(research.env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{research.refs['research']}",
                        purpose="train", start="2025-12-02", end="2025-12-16")
    assert ok["period"]["end"] == "2025-12-16"
    assert spy.indicators and max(spy.indicators) < pd.Timestamp("2025-12-16 23:00", tz="UTC")
    blocked = H.load_policy().reservation("MES").blocked_start
    assert not H.reserved_mask("MES", pd.DatetimeIndex(spy.indicators)).any() and str(blocked.date()) == "2025-12-17"


def test_csv_inputs_with_reserved_bars_are_refused_before_any_computation(research, fx_cross, monkeypatch, tmp_path):
    import run_backtest
    import run_regime_validation
    from src.pipeline import build_market_data
    from src.research import runs as R

    spy = Spy(monkeypatch)
    csv = write_generic(fx_cross, tmp_path / "MESH26.csv")
    csv.write_text("\n".join(x for x in csv.read_text().splitlines() if not x.startswith("#")) + "\n")
    for marker in (False, True):                                               # the SYNTHETIC marker changes nothing
        path = csv if not marker else write_generic(fx_cross, tmp_path / "SYNTHETIC_MESH26.csv")
        with pytest.raises(H.HoldoutError, match="refused before any indicator"):
            build_market_data(path, APP, holdout_guard=True)
    with pytest.raises(H.HoldoutError, match="refused before any indicator"):
        R.run_research(research.env, "vwap_mean_reversion", "V1-C2-SWING", str(csv), purpose="train",
                       start="2025-12-08", end="2025-12-12")
    ledger = tmp_path / "bt_ledger.jsonl"
    assert run_backtest.main(["--csv", str(csv), "--ledger", str(ledger), "--output-dir", str(tmp_path / "o"),
                              "--out-of-sample", "2026-01-05", "2026-01-23", "--reveal-oos", "--init-ledger"]) == 1
    assert not ledger.exists()                                                 # nothing written, no date burned
    assert run_regime_validation.main(["--csv", str(csv), "--output-dir", str(tmp_path / "rv"),
                                       "--ledger", str(tmp_path / "l.jsonl")]) == 1
    assert spy.indicators == []                                                # no indicator ever ran


def test_the_engine_refuses_reserved_bars_before_prepare(fx_cross, tmp_path):
    from src.backtest.config import load_backtest_config
    from src.backtest.runner import run_spec, run_strategy
    from src.pipeline import build_market_data
    from src.strategy.vwap_pullback_config import load_strategy_config

    md = build_market_data(write_generic(fx_cross, tmp_path / "MESH26.csv"), APP)    # a direct caller, no guard

    def boom(*a, **k):
        raise AssertionError("spec.prepare saw the data")

    spec = SimpleNamespace(prepare=boom)
    for period in (None, Period("x", "range", pd.Timestamp("2025-12-08"), pd.Timestamp("2025-12-12"))):
        with pytest.raises(H.HoldoutError, match="backtest engine input"):
            run_spec(md, APP, load_backtest_config(), spec, None, label="x", period=period)
    pre = build_market_data(write_generic(fx_cross[fx_cross["ts_utc"] < pd.Timestamp("2025-12-16 22:00", tz="UTC")],
                                          tmp_path / "MESH26_pre.csv"), APP, holdout_guard=True)
    run = run_strategy(pre, APP, load_backtest_config(), load_strategy_config())
    assert run.result is not None                                              # input before the window: allowed


# ============================================================================ (1, 2, 3, 10) the final evaluation
MC = "\n[final_evaluation.monte_carlo]\nmethods = [\"trade_iid\", \"day_block\"]\npaths = 50\nseed = 4242\n"


def _plan(rs, *, pid="hold1", oos=("2026-01-05", "2026-01-23"), extra=MC, research_ref=None, oos_ref=None):
    from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH
    from src.backtest.manifest import file_sha256

    def entry(ref, use):
        return (f'[[datasets]]\ninstrument = "MES"\nuse = "{use}"\nref = "{ref}"\n'
                f'manifest_sha256 = "{rs.store.load_manifest(ref).manifest["manifest_sha256"]}"\n')

    (rs.tmp / "plans" / f"{pid}.toml").write_text(
        f'[plan]\nid = "{pid}"\ndescription = "holdout test plan (generated prices)"\n'
        + entry(research_ref or rs.refs["research"], "research") + entry(oos_ref or rs.refs["oos"], "oos")
        + f'[segments]\ntrain = ["2025-12-02", "2025-12-08"]\nvalidation = ["2025-12-10", "2025-12-15"]\n'
        f'oos = ["{oos[0]}", "{oos[1]}"]\npurge_trade_dates = 1\nembargo_trade_dates = 1\n[frozen]\n'
        f'strategies = ["vwap_mean_reversion@V1-C2-SWING"]\n'
        f'backtest_config_sha256 = "{file_sha256(DEFAULT_BACKTEST_CONFIG_PATH)}"\n{extra}')
    return pid


def test_plan_rules_keep_research_datasets_before_the_window(research):
    from src.research import plans as P

    rs = research
    p = P.parse_plan(rs.tmp / "plans" / f"{_plan(rs)}.toml", APP, rs.store)
    assert p["holdout"]["MES"]["reservation"] == "mes-mnq-2026"
    assert {d["use"] for d in p["datasets"]} == {"research", "oos"}
    with pytest.raises(H.HoldoutError, match="reaches the holdout"):           # research data inside the window
        P.parse_plan(rs.tmp / "plans" / f"{_plan(rs, pid='bad1', research_ref=rs.refs['cross'])}.toml", APP, rs.store)
    with pytest.raises(H.HoldoutError, match="without lying entirely inside"):
        P.parse_plan(rs.tmp / "plans" / f"{_plan(rs, pid='bad2', oos=('2025-12-29', '2026-01-23'), oos_ref=rs.refs['cross'])}.toml",
                     APP, rs.store)


def test_the_final_evaluation_reads_no_byte_before_consumed_and_runs_once(research, monkeypatch, capsys):
    import run_research
    from src.backtest.manifest import file_sha256
    from src.backtest.validation import ValidationLedger
    from src.research import governance as G
    from src.research import plans as P
    from src.research import runs as R
    from src.research.monte_carlo import run_report

    rs = research
    src = f"dataset:{rs.refs['oos']}"
    monkeypatch.setattr(R, "code_identity", lambda: TRUSTED)
    P.register(_plan(rs), APP, rs.store)
    spy = Spy(monkeypatch)
    res = R.reveal_oos(rs.env, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-05", "2026-01-23", plan="hold1")
    oos_files = [r for r in spy.reads if r[0] in ("hash", "bars")]
    assert oos_files, "the dataset must have been read (after CONSUMED)"
    assert all(states == [HL.CONSUMED] for _, _, states in oos_files), oos_files   # (1) FIRST read after CONSUMED
    (claim,) = HL.claims().values()
    assert [h["type"] for h in claim["history"]] == ["claim_prepared", "claim_claimed", "claim_consumed",
                                                     "claim_completed"]
    mirrors = [a for a in HL.anchor_records() if a.get("type") == "mirror"]
    assert [m["body"]["type"] for m in mirrors] == [h["type"] for h in claim["history"]]   # every step anchored
    done = claim["history"][-1]["results"]
    assert done["monte_carlo_sha256"] == file_sha256(res["monte_carlo"])
    m = json.loads((res["folder"] / "manifest.json").read_text())
    rec = [r for r in ValidationLedger(rs.tmp / "ledger.jsonl").records() if r.get("type") == "oos_reveal"]
    assert len(rec) == 1 and rec[0]["holdout"]["claim_id"] == claim["claim_id"]
    # (10) never again, and nothing derived from it outside the frozen evaluation
    with pytest.raises(R.ResearchError, match="evaluated once and never re-run"):
        R.reveal_oos(rs.env, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-05", "2026-01-23", plan="hold1")
    P.register(_plan(rs, pid="hold2", oos=("2026-01-12", "2026-01-23")), APP, rs.store)
    with pytest.raises(R.ResearchError, match="evaluated once and never re-run"):
        R.reveal_oos(rs.env, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-12", "2026-01-23", plan="hold2")
    assert run_research.main(["monte-carlo", "--run", str(res["folder"]), "--paths", "10"]) == 1
    assert "Monte Carlo of a run" in capsys.readouterr().err
    with pytest.raises(H.HoldoutError):
        run_report(res["folder"], APP, rs.env.bt, paths=50, seed=4242)
    with pytest.raises(H.HoldoutError, match="comparing a run"):
        R.compare([res["folder"], res["folder"]])
    G._evidence("SHADOW_APPROVED", "vwap_mean_reversion", "V1-C2-SWING", m["strategy"]["sha256"],
                {"monte_carlo": res["monte_carlo"], "run": res["folder"]}, None)
    fake = res["folder"] / "monte_carlo_other.json"
    other = json.loads(res["monte_carlo"].read_text())
    other["note"] = "another Monte Carlo"
    fake.write_text(json.dumps(other))
    with pytest.raises(G.GovernanceError, match="frozen in the plan"):
        G._evidence("SHADOW_APPROVED", "vwap_mean_reversion", "V1-C2-SWING", m["strategy"]["sha256"],
                    {"monte_carlo": fake, "run": res["folder"]}, None)


def test_failures_before_consumed_abort_and_after_consumed_never_release(research, monkeypatch):
    import src.histdata.schema as schema
    from src.research import plans as P
    from src.research import runs as R

    rs = research
    src = f"dataset:{rs.refs['oos']}"
    monkeypatch.setattr(R, "code_identity", lambda: TRUSTED)
    P.register(_plan(rs), APP, rs.store)
    spy = Spy(monkeypatch)
    real_consume = H.consume
    monkeypatch.setattr(H, "consume", lambda t: (_ for _ in ()).throw(RuntimeError("crash before CONSUMED")))
    with pytest.raises(RuntimeError, match="before CONSUMED"):
        R.reveal_oos(rs.env, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-05", "2026-01-23", plan="hold1")
    (first,) = HL.claims().values()
    assert first["state"] == HL.ABORTED and first["history"][-1]["accessed"] is False   # (2)
    assert spy.reads == []                                                      # provably nothing was read
    monkeypatch.setattr(H, "consume", real_consume)
    real_read = schema.read_bars

    def crash_on_read(p):
        raise RuntimeError("crash while reading the data")

    monkeypatch.setattr(schema, "read_bars", crash_on_read)
    with pytest.raises(RuntimeError, match="while reading"):
        R.reveal_oos(rs.env, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-05", "2026-01-23", plan="hold1")
    second = [c for c in HL.claims().values() if c["claim_id"] != first["claim_id"]][0]
    assert second["state"] == HL.CONSUMED                                      # (3) possible access: consumed
    monkeypatch.setattr(schema, "read_bars", real_read)
    with pytest.raises(H.HoldoutError, match="overlaps holdout claim"):
        R.reveal_oos(rs.env, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-05", "2026-01-23", plan="hold1")
    with pytest.raises(H.HoldoutError, match="stays consumed for good"):
        H.recover_claim(second["claim_id"], by="t", reason="release attempt")


def test_a_consumed_claim_completes_only_from_its_recorded_run(research, monkeypatch):
    from src.backtest.manifest import file_sha256
    from src.backtest.validation import ValidationLedger
    from src.research import plans as P
    from src.research import runs as R

    rs = research
    src = f"dataset:{rs.refs['oos']}"
    monkeypatch.setattr(R, "code_identity", lambda: TRUSTED)
    P.register(_plan(rs), APP, rs.store)
    import src.research.monte_carlo as MCm

    real_report = MCm.run_report
    monkeypatch.setattr(MCm, "run_report", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("crash after the run folder was written")))
    with pytest.raises(RuntimeError, match="after the run folder"):
        R.reveal_oos(rs.env, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-05", "2026-01-23", plan="hold1")
    monkeypatch.setattr(MCm, "run_report", real_report)
    (claim,) = HL.claims().values()
    assert claim["state"] == HL.CONSUMED
    folder = next((rs.tmp / "out").rglob("manifest.json")).parent
    records = ValidationLedger(rs.tmp / "ledger.jsonl").records()
    with pytest.raises(H.HoldoutError, match="--by and --reason"):
        H.complete_consumed_claim(claim["claim_id"], folder, app=APP, bt=rs.env.bt, validation_records=records, by="",
                                  reason="")
    done = H.complete_consumed_claim(claim["claim_id"], folder, app=APP, bt=rs.env.bt, validation_records=records,
                                     by="tester", reason="owner crashed during the frozen Monte Carlo")
    assert HL.claims()[claim["claim_id"]]["state"] == HL.COMPLETED
    assert done["results"]["monte_carlo_sha256"] == file_sha256(folder / "monte_carlo_final_oos.json")
    with pytest.raises(H.HoldoutError, match="only a CONSUMED claim"):
        H.complete_consumed_claim(claim["claim_id"], folder, app=APP, bt=rs.env.bt, validation_records=records,
                                  by="tester", reason="again")


def test_alternate_ledgers_and_plans_cannot_reach_the_holdout(research, monkeypatch, tmp_path):
    from src.backtest.validation import LedgerError, ValidationLedger
    from src.research import plans as P
    from src.research import runs as R

    rs = research
    src = f"dataset:{rs.refs['oos']}"
    monkeypatch.setattr(R, "code_identity", lambda: TRUSTED)
    P.register(_plan(rs), APP, rs.store)
    other = tmp_path / "other.jsonl"
    ValidationLedger(other).initialise("alt")
    alt = R.ResearchEnv(rs.env.app, rs.env.bt, rs.env.out_root, rs.env.settings_path, rs.env.backtest_path,
                        ledger_path=other, registry=rs.env.registry, store=rs.store)
    with pytest.raises(R.ResearchError, match="canonical validation ledger"):
        R.reveal_oos(alt, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-05", "2026-01-23", plan="hold1")
    alt_plans = tmp_path / "alt_plans"                                         # (L4) another plans directory,
    alt_plans.mkdir()                                                          # holding the same registered plan
    for f in (rs.tmp / "plans").iterdir():
        (alt_plans / f.name).write_bytes(f.read_bytes())
    monkeypatch.setenv("FTS_PLANS_DIR", str(alt_plans))
    with pytest.raises(H.HoldoutError, match="canonical plans directory"):
        R.reveal_oos(rs.env, "vwap_mean_reversion", "V1-C2-SWING", src, "2026-01-05", "2026-01-23", plan="hold1")
    assert HL.claims() == {}
    # (L6) the canonical validation ledger is never re-initialised after a deletion
    canonical = rs.tmp / "ledger.jsonl"
    canonical.unlink()
    canonical.with_name(canonical.name + ".head").unlink()
    with pytest.raises(LedgerError, match="never re-initialised"):
        ValidationLedger(canonical).initialise("again")


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl locking: not available on this Windows machine")
def test_posix_locks_exclude_other_processes(tmp_path):
    from src.backtest import filelock

    lock = tmp_path / "x.lock"
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from src.backtest import filelock; "
            "h = filelock.acquire(sys.argv[2]); print('held', flush=True); time.sleep(30)")
    p = subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(lock)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    assert filelock.try_exclusive(lock) is None
    p.kill()
    p.wait()
    assert filelock.try_exclusive(lock) is not None
