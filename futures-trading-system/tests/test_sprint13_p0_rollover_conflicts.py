"""Sprint 13 P0: rollover safeguards and the data-conflict register (SYNTHETIC fixtures only).

Rollover: missing / non-consecutive contracts, bars outside the schedule, silent date loss, duplicate sessions,
holiday-aware roll dates, full bar accounting. Conflicts: deterministic minute-by-minute comparison, a recorded
conflict refuses both datasets for research / plans / continuous series, a human supersede resolves it, the
evidence is tamper-evident, and the dataset registry keeps its record kinds.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.config import ConfigError, load_config
from src.histdata.calendar import Holiday
from src.histdata.conflicts import CONFLICTS_FILE, REPORTS_DIR, compare_datasets
from src.histdata.providers import make_adapter
from src.histdata.readiness import assess
from src.histdata.rollover import build_continuous, check_contract_sequence
from src.histdata.store import DatasetError, DatasetStore
from src.research.chain import ChainStore
from tests.histdata_fixtures import CAL, bars, write_databento

APP = load_config()
DB = make_adapter("databento_ohlcv_csv")


@pytest.fixture(scope="module")
def fx():
    return bars(roll_day="2025-03-11").assign(root="MES", source_ts="", source_file=0)


@pytest.fixture()
def store(tmp_path):
    return DatasetStore(tmp_path / "store")


def ingest(store, did, frame, path):
    return store.ingest(did, DB, [write_databento(frame, path)], specs=APP.contracts, calendar=CAL)


def tdates(frame):
    return pd.Series(CAL.trade_dates(frame["ts_utc"]).to_numpy(), index=frame.index)


# ============================================================================ rollover safeguards
def test_missing_or_unlisted_contracts_are_refused():
    assert check_contract_sequence(["MESM25", "MESH25"], CAL) == ["MESH25", "MESM25"]
    with pytest.raises(ConfigError, match="not consecutive: missing MESM25"):
        check_contract_sequence(["MESH25", "MESU25"], CAL)
    with pytest.raises(ConfigError, match="MESZ24, MESH25, MESM25, MESU25, MESZ25, MESH26, MESM26, MESU26"):
        check_contract_sequence(["MESH24", "MESM24", "MESU24", "MESZ26"], CAL)
    with pytest.raises(ConfigError, match="not a listed month"):
        check_contract_sequence(["MESF25"], CAL)
    with pytest.raises(ConfigError, match="not an individual contract code"):
        check_contract_sequence(["MES.c.0"], CAL)
    gap = bars(contracts=("MESH25", "MESU25"), roll_day="2025-03-11").assign(root="MES", source_ts="", source_file=0)
    with pytest.raises(ConfigError, match="missing MESM25"):
        build_continuous(gap, CAL)


def test_bars_outside_the_schedule_are_refused_not_dropped(fx):
    sat = fx.iloc[[0]].copy()
    sat["ts_utc"] = pd.Timestamp("2025-03-08 18:00", tz="UTC")             # Saturday
    with pytest.raises(ConfigError, match="1 bar\\(s\\) fall outside the exchange schedule"):
        build_continuous(pd.concat([fx, sat], ignore_index=True), CAL)


def test_a_trade_date_is_never_lost_silently(fx):
    td = tdates(fx)
    hole = fx[~((fx["contract"] == "MESH25") & (td == pd.Timestamp("2025-03-10")))]   # front missing that day
    with pytest.raises(ConfigError, match=r"no bars on 1 trade date\(s\) \['2025-03-10'\]"):
        build_continuous(hole, CAL, rule="calendar")


def test_every_bar_is_accounted_for_and_one_contract_per_trade_date(fx):
    rep: dict = {}
    out, rolls = build_continuous(fx, CAL, rule="calendar", days_before_expiry=5, report=rep)
    assert rolls.iloc[0]["trade_date"] == "2025-03-14"                         # unchanged behaviour
    assert rep["bars_in"] == len(fx) and rep["bars_selected"] == len(out)
    assert rep["bars_selected"] + rep["bars_not_selected"] == rep["bars_in"]
    assert sum(rep["bars_not_selected_by_contract"].values()) == rep["bars_not_selected"]
    assert sum(rep["trade_dates_by_contract"].values()) == rep["trade_dates"]
    assert rep["contracts"] == ["MESH25", "MESM25"] and rep["verified_closed_days_used"] == []
    td = CAL.trade_dates(out["ts_utc"]).to_numpy()
    assert out.groupby(td)["contract"].nunique().max() == 1 and not out["ts_utc"].duplicated().any()
    for c in ("open", "high", "low", "close", "volume"):                     # prices never change
        m = out.merge(fx, on=["contract", "ts_utc"], suffixes=("", "_src"))
        assert (m[c] == m[f"{c}_src"]).all()


def test_a_verified_closure_moves_the_calendar_roll_to_the_right_session(fx):
    holiday = Holiday(date(2025, 3, 17), "closed", None, "https://example.invalid/test-only", "VERIFIED")
    cal = dataclasses.replace(CAL, holidays=(holiday,))
    td = tdates(fx)
    closed = fx[td != pd.Timestamp("2025-03-17")]                             # no session that trade date
    _, plain = build_continuous(closed, CAL, rule="calendar", days_before_expiry=5)
    rep: dict = {}
    _, aware = build_continuous(closed, cal, rule="calendar", days_before_expiry=5, report=rep)
    assert plain.iloc[0]["trade_date"] == "2025-03-14"                        # weekday count ignores the closure
    assert aware.iloc[0]["trade_date"] == "2025-03-13"                        # 5 real sessions before expiry
    assert rep["verified_closed_days_used"] == ["2025-03-17"]


def test_the_last_contract_inside_its_roll_window_is_reported(fx):
    rep: dict = {}
    build_continuous(fx[fx["contract"] == "MESH25"], CAL, rule="calendar", days_before_expiry=5, report=rep)
    assert "2025-03-20" in rep["last_contract_inside_roll_window"]


def test_continuous_datasets_need_validated_sources_and_record_the_accounting(tmp_path, store, fx):
    good = ingest(store, "per.contract", fx, tmp_path / "all.csv")
    assert good["status"] == "VALIDATED"
    c = store.create_continuous("cont", [good["ref"]], specs=APP.contracts, calendar=CAL, rule="volume")
    acc = store.load(c["ref"]).manifest["lineage"]["roll_accounting"]
    assert acc["bars_in"] == acc["bars_selected"] + acc["bars_not_selected"] and acc["contracts"] == ["MESH25",
                                                                                                        "MESM25"]
    sat = fx[fx["contract"] == "MESH25"].iloc[[0]].copy()
    sat["ts_utc"] = pd.Timestamp("2025-03-08 18:00", tz="UTC")
    bad = ingest(store, "per.contract.bad", pd.concat([fx[fx["contract"] == "MESH25"], sat]), tmp_path / "bad.csv")
    assert bad["status"] == "VALIDATION_FAILED"
    with pytest.raises(DatasetError, match="needs VALIDATED sources"):
        store.create_continuous("cont.bad", [bad["ref"]], specs=APP.contracts, calendar=CAL)


# ============================================================================ conflict register
def _pair(tmp_path, store, fx):
    one = fx[fx["contract"] == "MESH25"].reset_index(drop=True)
    two = one.copy()
    two.loc[100, "volume"] += 1000                                            # 2 differing minutes
    two.loc[200, ["high", "close"]] = two.loc[200, ["high", "close"]] + 0.25
    two = two.drop(index=300).reset_index(drop=True)                          # 1 minute only in the first
    a = ingest(store, "nt.mes.old", one, tmp_path / "old.csv")["ref"]
    b = ingest(store, "nt.mes.new", two, tmp_path / "new.csv")["ref"]
    return a, b, one


def test_comparison_is_exact_and_deterministic(tmp_path, store, fx):
    a, b, one = _pair(tmp_path, store, fx)
    rep = store.compare(a, b)
    rev = compare_datasets(store.load(b), store.load(a))
    assert rep["report_sha256"] == rev["report_sha256"]                       # pair order does not matter
    assert rep["a"]["ref"] == "nt.mes.new@v1" and rep["b"]["ref"] == "nt.mes.old@v1"
    assert rep["totals"] == {"differing": 2, "only_in_a": 0, "only_in_b": 1,
                             "common_minutes": len(one) - 1}
    by_ts = {c["ts_utc"]: c for c in rep["conflicts"]}
    t100 = pd.Timestamp(one.loc[100, "ts_utc"]).isoformat()
    assert by_ts[t100]["fields"] == ["volume"]
    assert by_ts[t100]["b"]["volume"] == one.loc[100, "volume"] and by_ts[t100]["a"]["volume"] == one.loc[100, "volume"] + 1000
    assert rep["only_in_b"][0]["ts_utc"] == pd.Timestamp(one.loc[300, "ts_utc"]).isoformat()
    for ref in (a, b):                                                        # comparing records nothing
        assert store.load(ref).usage_refusal() is None
    assert not (store.root / CONFLICTS_FILE).exists()


def test_a_recorded_conflict_refuses_both_until_a_human_supersedes(tmp_path, store, fx):
    from src.research import runs as R

    a, b, _ = _pair(tmp_path, store, fx)
    rep = store.compare(a, b)
    with pytest.raises(ConfigError, match="--by"):
        store.record_conflict(rep, by=" ")
    rec = store.record_conflict(rep, by="tester", note="two exports of the same contract")
    assert rec["conflict_id"] == rep["report_sha256"][:16]
    with pytest.raises(ConfigError, match="already recorded"):
        store.record_conflict(rep, by="tester")
    env = SimpleNamespace(store=store)
    for ref in (a, b):
        ds = store.load(ref)
        assert "unresolved data conflict" in ds.usage_refusal()
        with pytest.raises(ConfigError, match="unresolved data conflict"):
            R.ResearchEnv.dataset(env, f"dataset:{ref}")
        r = assess(ds)
        assert r["uses"]["backtesting"]["verdict"] == "NOT_READY"
        assert r["dimensions"]["overall_research_readiness"]["status"] == "BLOCKED"
        with pytest.raises(DatasetError, match="unresolved data conflict"):
            store.create_continuous(f"cont.{ref[:9]}", [ref], specs=APP.contracts, calendar=CAL)
    with pytest.raises(ConfigError, match="no recorded conflict"):
        store.conflicts.supersede(a, "other.ds@v1", by="tester", reason="x")
    with pytest.raises(ConfigError, match="--by and --reason"):
        store.supersede(a, b, by="tester", reason="")
    sup = store.supersede(a, b, by="tester", reason="newer export rebuilt from server history")
    assert sup["dataset"] == a and sup["superseded_by"] == b
    assert store.load(a).usage_refusal() == f"{a} is refused: superseded by {b}"
    assert store.load(b).usage_refusal() is None                              # resolved: usable again
    assert R.ResearchEnv.dataset(env, f"dataset:{b}")[0].ref == b
    state = store.load(b).conflict_state
    assert state["open_conflicts"] == [] and state["resolved_conflicts"][0]["conflict_id"] == rec["conflict_id"]
    with pytest.raises(ConfigError, match="already superseded"):
        store.supersede(a, b, by="tester", reason="again")
    with pytest.raises(ConfigError, match="itself superseded"):
        store.supersede(b, a, by="tester", reason="reverse")
    # the dataset registry keeps its record kinds: older readers still verify it
    assert {r["kind"] for r in ChainStore(store.root / "registry.jsonl", kinds=("reserved", "status")).records()} \
        == {"reserved"}


def test_agreeing_datasets_and_modified_reports_are_refused(tmp_path, store, fx):
    one = fx[fx["contract"] == "MESH25"].reset_index(drop=True)
    a = ingest(store, "same.a", one, tmp_path / "a.csv")["ref"]
    b = ingest(store, "same.b", one, tmp_path / "b.csv")["ref"]
    rep = store.compare(a, b)
    assert rep["totals"]["differing"] == 0
    with pytest.raises(ConfigError, match="no conflict to record"):
        store.record_conflict(rep, by="tester")
    c, d, _ = _pair(tmp_path, store, fx)
    rep = store.compare(c, d)
    forged = json.loads(json.dumps(rep))
    forged["totals"]["differing"] = 0
    with pytest.raises(ConfigError, match="not an unmodified conflict report"):
        store.record_conflict(forged, by="tester")
    with pytest.raises(ConfigError, match="cannot conflict with itself"):
        store.compare(c, c)


def test_tampered_or_missing_conflict_evidence_fails_closed(tmp_path, store, fx):
    a, b, _ = _pair(tmp_path, store, fx)
    rep = store.compare(a, b)
    store.record_conflict(rep, by="tester")
    p = store.root / REPORTS_DIR / f"{rep['report_sha256']}.json"
    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    p.write_text(p.read_text(encoding="utf-8").replace('"tester"', '"x"') + " ", encoding="utf-8")
    with pytest.raises(DatasetError, match="conflict register integrity failure"):
        store.load(a)
    p.unlink()
    with pytest.raises(DatasetError, match="is missing"):
        store.load(b)


def test_cli_compare_record_supersede_and_list(tmp_path, store, fx, capsys):
    import run_data

    a, b, _ = _pair(tmp_path, store, fx)
    root = str(store.root)
    assert run_data.main(["--root", root, "conflicts", a, b]) == 0
    out = capsys.readouterr().out
    assert "read-only comparison: nothing recorded" in out and '"differing": 2' in out
    assert not (Path(root) / CONFLICTS_FILE).exists()
    assert run_data.main(["--root", root, "conflicts", a, b, "--record"]) == 1     # --by is required
    assert run_data.main(["--root", root, "conflicts", a, b, "--record", "--by", "tester"]) == 0
    assert "recorded" in capsys.readouterr().out
    assert run_data.main(["--root", root, "list"]) == 0
    listed = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert all("unresolved data conflict" in x["data_conflict"] for x in listed)
    assert run_data.main(["--root", root, "supersede", a, "--with", b, "--by", "tester", "--reason", "newer"]) == 0
    capsys.readouterr()
    assert run_data.main(["--root", root, "list"]) == 0
    listed = {f"{x['dataset_id']}@{x['version']}": x["data_conflict"]
              for x in (json.loads(y) for y in capsys.readouterr().out.splitlines() if y.startswith("{"))}
    assert listed[a] == f"superseded by {b}" and listed[b] is None
