"""Sprint 12: historical market-data infrastructure.

Provider adapters, normalization, duplicates, missing data, OHLC / volume validation, timestamps and DST,
RTH / ETH, holidays, expiry, rollover and continuous series, dataset hashing, manifest integrity,
immutability, incremental ingestion, corrupted files, cross-source discrepancies, OOS protection with frozen
plans, research reproducibility, API security and recovery after an interrupted ingestion.

All data here is SYNTHETIC (deterministic fixtures): it exercises the code and proves nothing about markets.
"""

from __future__ import annotations

import ast
import json
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import ConfigError, load_config
from src.histdata.calendar import load_calendar, us_holiday_candidates
from src.histdata.crossval import compare
from src.histdata.providers import make_adapter
from src.histdata.readiness import assess
from src.histdata.rollover import build_continuous
from src.histdata.store import DatasetError, DatasetStore
from src.research.chain import ChainError
from tests.histdata_fixtures import CAL, bars, write_databento, write_generic, write_nt

ROOT = Path(__file__).resolve().parents[1]
APP = load_config()
DB = make_adapter("databento_ohlcv_csv")


@pytest.fixture()
def store(tmp_path):
    return DatasetStore(tmp_path / "store")


def ingest(store, did, files, adapter=DB, **kw):
    return store.ingest(did, adapter, [Path(f) for f in files], specs=APP.contracts, calendar=CAL, **kw)


def findings(store, ref):
    return {f["code"]: f for f in store.load(ref).validation()["findings"]}


@pytest.fixture(scope="module")
def fx():
    return bars(roll_day="2025-03-11")


def one(fx, c="MESH25"):
    return fx[fx["contract"] == c].reset_index(drop=True)


# ============================================================================ adapters & timestamps
def test_three_provider_formats_normalize_to_identical_bars(tmp_path, store, fx):
    b = one(fx)
    a = ingest(store, "db.fixed", [write_databento(b, tmp_path / "f.csv")])
    i = ingest(store, "db.iso", [write_databento(b, tmp_path / "i.csv", fixed_point=False, iso=True)])
    g = ingest(store, "gen.ny", [write_generic(b, tmp_path / "SYNTHETIC_g.csv")],
               adapter=make_adapter("generic_csv", timezone="America/New_York"))
    n = ingest(store, "nt.ct", [write_nt(b, tmp_path / "MES 03-25.Last.txt")],
               adapter=make_adapter("ninjatrader_export", timezone="America/Chicago"))
    shas = {store.load(r["ref"]).manifest["bars_content_sha256"] for r in (a, i, g, n)}
    assert len(shas) == 1                                    # same UTC bar opens, contracts and values
    assert {r["status"] for r in (a, i, g, n)} == {"VALIDATED"}
    m = store.load(n["ref"]).manifest
    assert m["source_files"][0]["timestamp_semantics"].startswith("bar_open (source stamped at bar close")
    assert m["source_files"][0]["source_timezone"] == "America/Chicago"
    assert store.load(g["ref"]).manifest["synthetic"] is True


def test_adapters_refuse_to_guess(tmp_path, fx):
    b = one(fx).head(50)
    with pytest.raises(ConfigError, match="timezone"):
        make_adapter("generic_csv").parse(write_generic(b, tmp_path / "g.csv"))
    with pytest.raises(ConfigError, match="timezone"):
        make_adapter("ninjatrader_export").parse(write_nt(b, tmp_path / "MES 03-25.Last.txt"))
    f = write_nt(b, tmp_path / "export.txt")
    with pytest.raises(ConfigError, match="contract not in the file name"):
        make_adapter("ninjatrader_export", timezone="America/Chicago").parse(f)
    us = write_databento(b, tmp_path / "us.csv")
    t = pd.read_csv(us, dtype=str)
    t["ts_event"] = t["ts_event"].str[:-3]                   # microsecond epochs: wrong unit
    t.to_csv(us, index=False)
    with pytest.raises(ConfigError, match="not nanosecond"):
        DB.parse(us)
    with pytest.raises(ConfigError, match="unknown parameter"):
        make_adapter("databento_ohlcv_csv", api_key="x")
    with pytest.raises(ConfigError, match="unknown provider adapter"):
        make_adapter("magic_feed")
    p = DB.parse(write_databento(b, tmp_path / "rt.csv"))
    assert len(p.records) == 50 and not p.rejected
    wrong = pd.read_csv(tmp_path / "rt.csv", dtype=str)
    wrong.loc[3, "rtype"] = "32"                             # a 1-second bar in a 1-minute file
    wrong.to_csv(tmp_path / "rt.csv", index=False)
    assert DB.parse(tmp_path / "rt.csv").rejected[0]["reason"].startswith("rtype 32")


def test_dst_transitions_spring_and_fall(tmp_path, store):
    # 2025-03-09 (spring forward) and 2025-11-02 (fall back), New York naive vs UTC epochs
    for start, end in (("2025-03-06 22:00", "2025-03-12 20:00"), ("2025-10-30 21:00", "2025-11-05 20:00")):
        b = bars(start=start, end=end, contracts=("MESZ25",), roll_day="2025-01-01")
        a = ingest(store, f"db.{start[:10]}", [write_databento(b, tmp_path / f"d{start[:10]}.csv")])
        g = ingest(store, f"gen.{start[:10]}", [write_generic(b, tmp_path / f"SYNTHETIC_g{start[:10]}.csv")],
                   adapter=make_adapter("generic_csv", timezone="America/New_York", ambiguous="raise"))
        assert store.load(a["ref"]).manifest["bars_content_sha256"] == store.load(g["ref"]).manifest["bars_content_sha256"]
        assert "BAR_WHILE_MARKET_CLOSED" not in findings(store, a["ref"])
    # the CME open stays 17:00 Chicago in UTC terms across DST: 22:00 UTC (CDT) vs 23:00 UTC (CST)
    assert CAL.is_open(pd.DatetimeIndex([pd.Timestamp("2025-03-09 22:00", tz="UTC")])).iloc[0]
    assert not CAL.is_open(pd.DatetimeIndex([pd.Timestamp("2025-11-02 22:00", tz="UTC")])).iloc[0]
    assert CAL.is_open(pd.DatetimeIndex([pd.Timestamp("2025-11-02 23:00", tz="UTC")])).iloc[0]


def test_a_wrong_source_timezone_is_detected(tmp_path, store, fx):
    b = one(fx)
    r = ingest(store, "nt.wrongtz", [write_nt(b, tmp_path / "MES 03-25.Last.txt", tz="America/Chicago")],
               adapter=make_adapter("ninjatrader_export", timezone="America/New_York"))   # declared wrongly
    assert r["status"] == "VALIDATION_FAILED"
    assert "BAR_WHILE_MARKET_CLOSED" in findings(store, r["ref"])     # bars inside the daily halt / weekend


def test_rth_and_eth_definitions_are_explicit_and_dst_safe():
    idx = pd.date_range("2025-03-03", "2025-11-14", freq="1min", tz="UTC")
    mine = CAL.is_rth(idx).to_numpy()
    from src.data.sessions import assign_session_dates

    rth_ny = assign_session_dates(idx, APP.sessions["rth"]).notna().to_numpy()   # the research RTH 09:30-16:00 NY
    assert (mine == (rth_ny & CAL.is_open(idx).to_numpy())).all()
    td = CAL.trade_dates(pd.DatetimeIndex([pd.Timestamp("2025-03-09 22:00", tz="UTC"),       # Sunday 17:00 CT
                                           pd.Timestamp("2025-03-14 21:00", tz="UTC"),       # Friday 16:00 CT: closed
                                           pd.Timestamp("2025-03-11 21:30", tz="UTC")]))     # daily halt
    assert str(td.iloc[0].date()) == "2025-03-10" and pd.isna(td.iloc[1]) and pd.isna(td.iloc[2])


# ============================================================================ normalization & validation
def _mutate(tmp_path, b, name, fn):
    f = write_databento(b, tmp_path / f"{name}.csv")
    t = pd.read_csv(f, dtype=str)
    t = fn(t)
    t.to_csv(f, index=False)
    return f


def test_duplicates_order_and_bad_values(tmp_path, store, fx):
    b = one(fx)
    exact = _mutate(tmp_path, b, "dup", lambda t: pd.concat([t, t.iloc[[10, 11]]]).sample(frac=1, random_state=1))
    r = ingest(store, "dup.exact", [exact])
    f = findings(store, r["ref"])
    assert f["EXACT_DUPLICATES"]["count"] == 2 and "OUT_OF_ORDER" in f and r["status"] == "VALIDATED"
    assert store.load(r["ref"]).manifest["rows"] == len(b)

    def conflict(t):
        d = t.iloc[[20]].copy()
        d["close"] = str(int(d["close"].iloc[0]) + 250_000_000)
        return pd.concat([t, d])
    r = ingest(store, "dup.conflict", [_mutate(tmp_path, b, "conf", conflict)])
    assert r["status"] == "VALIDATION_FAILED" and findings(store, r["ref"])["CONFLICTING_DUPLICATES"]["count"] == 2
    assert store.load(r["ref"]).manifest["rows"] == len(b) - 1          # never "pick one"

    def bad(t):
        t.loc[5, "high"] = str(int(t.loc[5, "low"]) - 250_000_000)         # high below low
        t.loc[6, "open"] = "0"                                              # non-positive
        t.loc[7, "volume"] = "-3"
        t.loc[8, "close"] = str(int(t.loc[8, "close"]) + 100_000_000)      # 0.10 off the 0.25 tick grid
        t.loc[9, "close"] = "abc"
        t.loc[11, "symbol"] = "MES.c.0"                                     # continuous symbol in raw data
        t.loc[12, "symbol"] = "ESH5"                                        # unconfigured root
        return t
    r = ingest(store, "bad.values", [_mutate(tmp_path, b, "bad", bad)])
    f = findings(store, r["ref"])
    assert r["status"] == "VALIDATION_FAILED"
    for code in ("OHLC_INCONSISTENT", "NON_POSITIVE_PRICE", "NEGATIVE_VOLUME", "OFF_TICK_PRICE",
                 "UNKNOWN_OR_CONTINUOUS_SYMBOL", "BAD_SOURCE_ROWS"):
        assert code in f and f[code]["severity"] == "error", code
    assert f["UNKNOWN_OR_CONTINUOUS_SYMBOL"]["count"] == 2

    def vol(t):
        t.loc[30, "volume"] = str(int(t["volume"].astype(int).median()) * 500)
        t.loc[31, "volume"] = "0"
        return t
    r = ingest(store, "vol", [_mutate(tmp_path, b, "vol", vol)])
    f = findings(store, r["ref"])
    assert f["ABNORMAL_VOLUME"]["severity"] == "warning" and f["ZERO_VOLUME"]["count"] == 1 and r["status"] == "VALIDATED"


def test_missing_data_is_classified_never_filled(tmp_path, store, fx):
    b = one(fx)
    rth = CAL.is_rth(b["ts_utc"]).to_numpy()
    day = pd.DatetimeIndex(b["ts_utc"]).tz_convert("America/Chicago").normalize() == pd.Timestamp("2025-03-12", tz="America/Chicago")
    cut = np.flatnonzero(rth & day)[60:105]                                  # 45 RTH minutes missing
    r = ingest(store, "miss.rth", [write_databento(b.drop(index=cut), tmp_path / "m.csv")])
    f = findings(store, r["ref"])
    assert f["MISSING_DATA_RTH"]["count"] == 1 and r["status"] == "INGESTED"   # the coverage gate failed
    assert store.load(r["ref"]).manifest["rows"] == len(b) - 45                # nothing fabricated
    gate = store.load(r["ref"]).validation()["gate"]
    assert gate["longest_missing_rth_gap_minutes"] == 45
    two = np.flatnonzero(rth & day)[200:202]
    eth = np.flatnonzero(~rth & day)[100:110]
    r = ingest(store, "miss.short", [write_databento(b.drop(index=np.r_[two, eth]), tmp_path / "s.csv")])
    f = findings(store, r["ref"])
    assert f["NO_TRADE_INTERVAL"]["count"] == 2 and "MISSING_DATA_RTH" not in f and r["status"] == "VALIDATED"
    g = ingest(store, "miss.generic", [write_generic(b.drop(index=eth), tmp_path / "SYNTHETIC_s.csv")],
               adapter=make_adapter("generic_csv", timezone="America/New_York"))
    assert "MISSING_DATA_ETH" in findings(store, g["ref"])                 # no trade-built assumption here
    f = findings(store, r["ref"])
    assert f["SCHEDULED_BREAK"]["severity"] == "info" and f["EXPECTED_CLOSURE_WEEKEND"]["severity"] == "info"


def test_holidays_candidates_and_verified_calendar(tmp_path, store):
    c = us_holiday_candidates(2025)
    names = {v: k for k, v in c.items()}
    assert str(names["Thanksgiving Day"]) == "2025-11-27" and str(names["Good Friday"]) == "2025-04-18"
    assert str(names["Juneteenth"]) == "2025-06-19" and "Juneteenth" not in us_holiday_candidates(2021).values()
    assert str({v: k for k, v in us_holiday_candidates(2026).items()}["Independence Day"]) == "2026-07-03"
    b = bars(start="2025-11-25 23:00", end="2025-11-28 19:00", contracts=("MESZ25",), roll_day="2025-01-01")
    loc = pd.DatetimeIndex(b["ts_utc"]).tz_convert("America/Chicago")
    keep = ~((loc.date == pd.Timestamp("2025-11-27").date()) & (loc.hour >= 12))   # an early close on Thanksgiving
    r = ingest(store, "hol", [write_databento(b[keep], tmp_path / "h.csv")])
    assert findings(store, r["ref"])["HOLIDAY_CANDIDATE_UNVERIFIED"]["severity"] == "warning"
    text = (ROOT / "config/calendars/cme_equity_index.toml").read_text()
    bad = tmp_path / "bad.toml"
    bad.write_text(text + '\n[[holidays]]\ndate = "2025-11-27"\nkind = "early_close"\nclose = "12:00"\n'
                          'source = ""\nverification = "VERIFIED"\n')
    with pytest.raises(ConfigError, match="source URL"):
        load_calendar(bad)
    good = tmp_path / "good.toml"
    good.write_text(text.replace('version = "2026-11-25.1"', 'version = "test.1"') + '\n[[holidays]]\ndate = '
                    '"2025-11-27"\nkind = "early_close"\nclose = "12:00"\nsource = "https://example.invalid/hol"\n'
                    'verification = "VERIFIED"\n')
    cal2 = load_calendar(good)
    r2 = store.ingest("hol.verified", DB, [tmp_path / "h.csv"], specs=APP.contracts, calendar=cal2)
    f2 = findings(store, r2["ref"])
    assert "HOLIDAY_CANDIDATE_UNVERIFIED" not in f2 and r2["status"] == "VALIDATED"
    assert store.load(r2["ref"]).manifest["calendar"]["verified_holidays"] == 1
    r3 = store.ingest("hol.after", DB, [write_databento(b, tmp_path / "all.csv")], specs=APP.contracts, calendar=cal2)
    assert findings(store, r3["ref"])["BAR_WHILE_MARKET_CLOSED"]["severity"] == "error"   # bars after the early close


def test_bars_after_expiry_are_errors(tmp_path, store):
    b = bars(start="2025-03-18 22:00", end="2025-03-21 18:00", contracts=("MESH25",), roll_day="2026-01-01")
    r = ingest(store, "exp", [write_databento(b, tmp_path / "e.csv")])
    f = findings(store, r["ref"])
    assert f["BAR_AFTER_EXPIRY"]["severity"] == "error" and r["status"] == "VALIDATION_FAILED"
    assert CAL.expiry(3, 2025) == pd.Timestamp("2025-03-21 13:30", tz="UTC")       # 08:30 CDT


# ============================================================================ rollover
def test_rollover_rules_no_lookahead_and_unadjusted_prices(fx):
    b = fx.assign(root="MES", source_ts="", source_file=0)
    cal_c, cal_r = build_continuous(b, CAL, rule="calendar", days_before_expiry=5)
    vol_c, vol_r = build_continuous(b, CAL, rule="volume", confirm_days=1)
    assert cal_r.iloc[0]["trade_date"] == "2025-03-14" and cal_r.iloc[0]["rule"] == "calendar"
    assert vol_r.iloc[0]["trade_date"] == "2025-03-12" and vol_r.iloc[0]["decided_on"] == "2025-03-11"
    assert vol_r.iloc[0]["gap_points"] == pytest.approx(30.0)
    merged = vol_c.merge(b, on=["contract", "ts_utc"], suffixes=("", "_src"))
    assert len(merged) == len(vol_c)
    for c in ("open", "high", "low", "close", "volume"):
        assert (merged[c] == merged[f"{c}_src"]).all()                       # real contract prices, untouched
    td = CAL.trade_dates(vol_c["ts_utc"])
    assert vol_c.groupby(td.to_numpy())["contract"].nunique().max() == 1  # one contract per trade date
    # no look-ahead: cutting the data after the roll changes no earlier decision
    cut = b[pd.DatetimeIndex(b["ts_utc"]) < pd.Timestamp("2025-03-12 22:00", tz="UTC")]
    short, _ = build_continuous(cut, CAL, rule="volume", confirm_days=1)
    full_part = vol_c[pd.DatetimeIndex(vol_c["ts_utc"]) < pd.Timestamp("2025-03-12 22:00", tz="UTC")]
    assert list(short["contract"]) == list(full_part["contract"])
    adj, _ = build_continuous(b, CAL, rule="volume", adjustment="difference")
    early = adj["segment"] == 0
    raw = vol_c.loc[early.to_numpy(), "close"].to_numpy()
    assert np.allclose(adj.loc[early, "close"].to_numpy(), raw + 30.0) and (adj.loc[early, "adjustment"] == 30.0).all()
    with pytest.raises(ConfigError, match="ONE root"):
        build_continuous(pd.concat([b, b.assign(root="MNQ")]), CAL)


def test_continuous_datasets_record_lineage_and_adjusted_series_are_refused_for_simulation(tmp_path, store, fx):
    src = ingest(store, "per.contract", [write_databento(fx, tmp_path / "all.csv")])
    c = store.create_continuous("cont.vol", [src["ref"]], specs=APP.contracts, calendar=CAL, rule="volume")
    ds = store.load(c["ref"])
    assert ds.manifest["kind"] == "continuous" and ds.manifest["adjustment"] == "none"
    assert ds.manifest["lineage"]["sources"][0]["manifest_sha256"] == store.load(src["ref"]).manifest["manifest_sha256"]
    assert (ds.path / "rolls.csv").is_file() and "rolls.csv" in ds.manifest["files"]
    a = store.create_continuous("cont.adj", [src["ref"]], specs=APP.contracts, calendar=CAL, rule="volume",
                                adjustment="ratio")
    from src.histdata.market import market_data_from_dataset

    with pytest.raises(ConfigError, match="adjusted: refused"):
        market_data_from_dataset(store.load(a["ref"]), APP)
    with pytest.raises(ConfigError, match="select one"):
        market_data_from_dataset(store.load(src["ref"]), APP)
    md = market_data_from_dataset(store.load(c["ref"]), APP)
    assert md.contract.root == "MES" and md.contract.spec.point_value == 5.0 and md.is_synthetic is False \
        or md.contract.spec.tick_size == 0.25
    with pytest.raises(DatasetError, match="unadjusted per-contract"):
        store.create_continuous("cont.of.cont", [c["ref"]], specs=APP.contracts, calendar=CAL)


# ============================================================================ integrity / immutability / recovery
def test_hashing_is_deterministic_and_reingest_is_idempotent(tmp_path, fx):
    f = write_databento(one(fx), tmp_path / "x.csv")
    a, b = DatasetStore(tmp_path / "s1"), DatasetStore(tmp_path / "s2")
    ra, rb = ingest(a, "xxx", [f]), ingest(b, "xxx", [f])
    ma, mb = a.load(ra["ref"]).manifest, b.load(rb["ref"]).manifest
    assert ma["files"]["bars.csv.gz"] == mb["files"]["bars.csv.gz"]                # byte-identical normalized file
    assert ma["bars_content_sha256"] == mb["bars_content_sha256"]
    again = ingest(a, "xxx", [f])
    assert again == {"created": False, "ref": ra["ref"], "status": ra["status"], "reason": "identical source files"}


def test_tampering_substitution_and_missing_files_are_refused(tmp_path, store, fx):
    b = one(fx)
    r1 = ingest(store, "ttt", [write_databento(b, tmp_path / "1.csv")])
    r2 = ingest(store, "ttt", [write_databento(b.head(5000), tmp_path / "2.csv")])
    d1, d2 = store.root / "ttt" / "v1", store.root / "ttt" / "v2"
    for p in (d1, d2):
        for x in [p, *p.rglob("*")]:
            os.chmod(x, 0o755 if x.is_dir() else 0o644)
    # substituted manifest (a valid manifest of another version)
    m1, m2 = (d1 / "manifest.json").read_text(), (d2 / "manifest.json").read_text()
    (d1 / "manifest.json").write_text(m2)
    with pytest.raises(DatasetError, match="does not match the registry"):
        store.load(r1["ref"])
    (d1 / "manifest.json").write_text(m1)
    store.load(r1["ref"])
    # edited data file
    data = (d1 / "bars.csv.gz").read_bytes()
    (d1 / "bars.csv.gz").write_bytes(data[:-10] + b"0" * 10)
    with pytest.raises(DatasetError, match="bars.csv.gz was modified"):
        store.load(r1["ref"])
    (d1 / "bars.csv.gz").write_bytes(data)
    # edited manifest (status upgrade attempt)
    m = json.loads(m1)
    m["validation"]["status"] = "CROSS_SOURCE_VALIDATED"
    (d1 / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(DatasetError, match="self-hash"):
        store.load(r1["ref"])
    (d1 / "manifest.json").write_text(m1)
    # missing raw file
    raw = next((d1 / "raw").iterdir())
    raw.rename(raw.with_suffix(".bak"))
    with pytest.raises(DatasetError, match="missing"):
        store.load(r1["ref"])
    raw.with_suffix(".bak").rename(raw)
    store.load(r1["ref"])
    # path traversal in a manifest is refused even with consistent hashes
    from src.backtest.manifest import text_sha256
    from src.histdata.store import _canonical

    evil = json.loads(m1)
    evil["files"]["../../../etc/passwd"] = "0" * 64
    evil.pop("manifest_sha256")
    sha = text_sha256(_canonical(evil))
    evil["manifest_sha256"] = sha
    (d1 / "manifest.json").write_text(json.dumps(evil))
    with pytest.raises(DatasetError, match="unsafe file name"):
        store._verify(d1, sha, "ttt", "v1")
    (d1 / "manifest.json").write_text(m1)
    # registry tampering breaks the chain
    reg = store.root / "registry.jsonl"
    lines = reg.read_text().splitlines()
    reg.write_text("\n".join(lines[1:]) + "\n")
    with pytest.raises(ChainError):
        store.load(r2["ref"])


def test_versions_are_never_overwritten_and_files_are_read_only(tmp_path, store, fx):
    r = ingest(store, "ro0", [write_databento(one(fx), tmp_path / "a.csv")])
    p = store.root / "ro0" / "v1" / "bars.csv.gz"
    assert not (p.stat().st_mode & 0o222)                                  # no write bits
    st = store._stage()
    m = dict(store.load(r["ref"]).manifest)
    with pytest.raises(DatasetError, match="never overwritten"):
        store._finalize(st, {**m, "manifest_sha256": None}, "VALIDATED")


def test_incremental_ingestion_extends_without_changing_history(tmp_path, store, fx):
    b = one(fx)
    first, second = b.iloc[:8000], b.iloc[8000:]
    r1 = ingest(store, "inc", [write_databento(first, tmp_path / "a.csv")])
    r2 = ingest(store, "inc", [write_databento(second, tmp_path / "b.csv")], base=r1["ref"])
    d2 = store.load(r2["ref"])
    assert r2["ref"] == "inc@v2" and d2.manifest["rows"] == len(b)
    assert d2.manifest["lineage"]["base"] == "inc@v1" and d2.manifest["source_files"][0]["from_base"]
    assert store.load(r1["ref"]).manifest["rows"] == 8000                  # the old version is untouched
    changed = second.copy()
    changed = pd.concat([first.iloc[[100]].assign(close=first.iloc[100]["close"] + 1.0,
                                                  high=max(first.iloc[100]["high"], first.iloc[100]["close"] + 1.0)),
                         changed])
    f = write_databento(changed, tmp_path / "c.csv")
    with pytest.raises(DatasetError, match="would change"):
        ingest(store, "inc", [f], base=r1["ref"])
    r3 = ingest(store, "inc", [f], base=r1["ref"], allow_corrections=True)
    f3 = findings(store, r3["ref"])
    assert f3["CORRECTIONS"]["count"] == 1 and "CONFLICTING_DUPLICATES" not in f3
    v3 = store.load(r3["ref"]).bars()
    row = v3[(v3["ts_utc"] == first.iloc[100]["ts_utc"])]
    assert float(row["close"].iloc[0]) == first.iloc[100]["close"] + 1.0     # the correction, recorded
    assert store.load(r1["ref"]).bars()["close"].iloc[100] == first.iloc[100]["close"]   # v1 never changes
    with pytest.raises(DatasetError, match="same dataset id"):
        ingest(store, "other", [f], base=r1["ref"])


def test_corrupted_source_files(tmp_path, store, fx):
    junk = tmp_path / "junk.csv"
    junk.write_text("this is not,a databento file\n1,2\n")
    with pytest.raises(ConfigError, match="not a Databento OHLCV CSV"):
        ingest(store, "junk", [junk])
    assert not any((store.root / ".staging").iterdir())                    # nothing half-written is left
    trunc = write_databento(one(fx).head(100), tmp_path / "t.csv")
    trunc.write_text(trunc.read_text()[:-40])                               # truncated last line
    r = ingest(store, "trunc", [trunc])
    assert r["status"] == "VALIDATION_FAILED"
    with pytest.raises(DatasetError, match="not found"):
        ingest(store, "nofile", [tmp_path / "missing.csv"])
    with pytest.raises(DatasetError, match="twice"):
        ingest(store, "twice", [trunc, trunc])
    with pytest.raises(DatasetError, match="invalid dataset id"):
        ingest(store, "../evil", [trunc])


def test_recovery_after_an_interrupted_ingestion(tmp_path, store, fx, monkeypatch):
    f = write_databento(one(fx).head(3000), tmp_path / "r.csv")
    real = os.replace

    def crash(src, dst):
        raise OSError("simulated crash during the final rename")
    monkeypatch.setattr("src.histdata.store.os.replace", crash)
    with pytest.raises(OSError):
        ingest(store, "crash", [f])
    monkeypatch.setattr("src.histdata.store.os.replace", real)
    assert store.versions("crash")                                          # reserved in the registry ...
    with pytest.raises(DatasetError):
        store.load("crash@v1")                                              # ... but never readable half-done
    # the failed call's cleanup removed the staging folder: recover reports the reservation as lost
    rep = store.recover()
    assert any("crash@v1" in p for p in rep["problems"])
    # a crash AFTER the staging folder is complete but before the rename is completed by recover()
    monkeypatch.setattr("src.histdata.store.shutil.rmtree", lambda *a, **k: None)
    monkeypatch.setattr("src.histdata.store.os.replace", crash)
    with pytest.raises(OSError):
        ingest(store, "crash2", [f])
    monkeypatch.setattr("src.histdata.store.os.replace", real)
    rep = store.recover()
    assert "crash2@v1" in rep["completed"]
    assert store.load("crash2@v1").manifest["rows"] == 3000
    orphan = store.root / ".staging" / "deadbeef"
    orphan.mkdir(parents=True)
    assert "deadbeef" in store.recover()["removed"]


# ============================================================================ cross-source validation
def test_cross_source_validation_pass_and_discrepancies(tmp_path, store, fx):
    b = one(fx)
    a = ingest(store, "vendor.a", [write_databento(b, tmp_path / "a.csv")])
    n = ingest(store, "vendor.b", [write_nt(b, tmp_path / "MES 03-25.Last.txt")],
               adapter=make_adapter("ninjatrader_export", timezone="America/Chicago"))
    rep = compare(store.load(a["ref"]), store.load(n["ref"]), CAL, APP.contracts)
    assert rep["result"] == "PASS" and rep["rth"]["match_share"] == 1.0 and rep["best_timestamp_shift_minutes"] == 0
    store.set_status(a["ref"], "CROSS_SOURCE_VALIDATED", by="test", evidence={"report_sha256": rep["report_sha256"]})
    assert store.load(a["ref"]).status == "CROSS_SOURCE_VALIDATED"
    with pytest.raises(DatasetError, match="computed by the validation gate"):
        store.set_status(n["ref"], "VALIDATED", by="me", evidence={})
    # price discrepancies
    bad = b.copy()
    idx = bad.index[::40]
    bad.loc[idx, ["close", "high"]] = bad.loc[idx, ["close", "high"]] + 0.75
    n2 = ingest(store, "vendor.c", [write_nt(bad, tmp_path / "MES 03-25.Last.txt")],
                adapter=make_adapter("ninjatrader_export", timezone="America/Chicago"))
    rep2 = compare(store.load(a["ref"]), store.load(n2["ref"]), CAL, APP.contracts)
    assert rep2["result"] == "FAIL" and rep2["discrepancy_examples"] and rep2["ohlc"]["within_1_tick_share"] < 0.99
    # a one-hour clock error in the second source is detected as a shift
    sh = b.assign(ts_utc=b["ts_utc"] + pd.Timedelta(minutes=60))
    sh = sh[CAL.is_open(sh["ts_utc"]).to_numpy()]
    shifted = ingest(store, "vendor.d", [write_nt(sh, tmp_path / "MES 03-25.Last.txt")],
                     adapter=make_adapter("ninjatrader_export", timezone="America/Chicago"))
    rep3 = compare(store.load(a["ref"]), store.load(shifted["ref"]), CAL, APP.contracts)
    assert rep3["best_timestamp_shift_minutes"] == -60 and rep3["result"] == "FAIL"
    # not independent: same adapter / same source file
    same = ingest(store, "vendor.e", [write_databento(b, tmp_path / "e.csv")])
    rep4 = compare(store.load(a["ref"]), store.load(same["ref"]), CAL, APP.contracts)
    assert any("not independent" in p for p in rep4["problems"])
    # synthetic data never passes
    g = ingest(store, "vendor.syn", [write_generic(b, tmp_path / "SYNTHETIC_x.csv")],
               adapter=make_adapter("generic_csv", timezone="America/New_York"))
    assert any("SYNTHETIC" in p for p in compare(store.load(a["ref"]), store.load(g["ref"]), CAL, APP.contracts)["problems"])


# ============================================================================ readiness
def test_readiness_is_conservative(tmp_path, store, fx):
    g = ingest(store, "syn", [write_generic(one(fx), tmp_path / "SYNTHETIC_r.csv")],
               adapter=make_adapter("generic_csv", timezone="America/New_York"))
    r = assess(store.load(g["ref"]))
    assert {u["verdict"] for u in r["uses"].values()} == {"NOT_READY"}
    a = ingest(store, "real.like", [write_databento(one(fx), tmp_path / "a.csv")])
    r = assess(store.load(a["ref"]))
    assert r["uses"]["backtesting"]["verdict"] in ("READY", "READY_WITH_LIMITATIONS")
    assert r["uses"]["walk_forward"]["verdict"] == "NOT_READY"              # 11 trade dates
    assert any("cross-source" in x for x in r["uses"]["oos_validation"]["limitations"])
    assert r["known_biases"] and "not evidence" in r["note"]


# ============================================================================ research integration & OOS protection
@pytest.fixture()
def research(tmp_path, monkeypatch, fx):
    from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
    from src.backtest.validation import ValidationLedger
    from src.config import DEFAULT_CONFIG_PATH
    from src.research import governance as G
    from src.research.experiments import ExperimentRegistry
    from src.research.runs import ResearchEnv

    monkeypatch.setenv("FTS_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("FTS_EXPERIMENTS_PATH", str(tmp_path / "exp.jsonl"))
    monkeypatch.setenv("FTS_PLANS_DIR", str(tmp_path / "plans"))
    monkeypatch.setattr(G, "GOVERNANCE_FILE", tmp_path / "prom.jsonl")
    (tmp_path / "plans").mkdir()
    ValidationLedger(tmp_path / "ledger.jsonl").initialise("t")
    store = DatasetStore(tmp_path / "ds")
    monkeypatch.setenv("FTS_DATASETS_ROOT", str(tmp_path / "ds"))
    src = ingest(store, "syn.mes", [write_databento(fx, tmp_path / "SYNTHETIC_all.csv")])
    cont = store.create_continuous("syn.mes.cont", [src["ref"]], specs=APP.contracts, calendar=CAL, rule="volume")
    env = ResearchEnv(APP, load_backtest_config(), tmp_path / "out", DEFAULT_CONFIG_PATH, DEFAULT_BACKTEST_CONFIG_PATH,
                      registry=ExperimentRegistry(), store=store)
    return env, store, cont["ref"], tmp_path


def test_research_runs_record_dataset_identity_and_are_reproducible(research):
    from src.research import runs as R

    env, store, ref, tmp = research
    a = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}")
    b = R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}")
    ma = json.loads((a["folder"] / "manifest.json").read_text())
    mb = json.loads((b["folder"] / "manifest.json").read_text())
    assert ma["results_fingerprint"] == mb["results_fingerprint"]
    assert ma["data"]["dataset"]["ref"] == ref and ma["data"]["sha256"] == store.load(ref).manifest["manifest_sha256"]
    assert ma["data"]["dataset"]["synthetic"] is True
    att = env.registry.attempts()
    assert att[0]["data_sha256"] == store.load(ref).manifest["manifest_sha256"]
    with pytest.raises(ConfigError, match="SYNTHETIC"):
        R.sweep(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", {"indicators.band_k": [1.5, 2.0]})


def test_research_refuses_datasets_below_validated(research, fx):
    from src.research import runs as R

    env, store, ref, tmp = research
    b = one(fx)
    rth = np.flatnonzero(CAL.is_rth(b["ts_utc"]).to_numpy())[100:160]
    r = ingest(store, "gappy", [write_databento(b.drop(index=rth), tmp / "gappy.csv")])
    assert r["status"] == "INGESTED"
    with pytest.raises(ConfigError, match="needs a VALIDATED dataset"):
        R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{r['ref']}")


def _plan(tmp, ref, store, *, oos=("2025-03-17", "2025-03-19"), strategies=('"vwap_mean_reversion@V1-C2-SWING"',)):
    from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH
    from src.backtest.manifest import file_sha256

    text = f'''[plan]
id = "plan1"
description = "test plan on SYNTHETIC data"
[[datasets]]
instrument = "MES"
ref = "{ref}"
manifest_sha256 = "{store.load(ref).manifest['manifest_sha256']}"
[segments]
train = ["2025-03-06", "2025-03-10"]
validation = ["2025-03-12", "2025-03-13"]
oos = ["{oos[0]}", "{oos[1]}"]
purge_trade_dates = 1
embargo_trade_dates = 1
[frozen]
strategies = [{", ".join(strategies)}]
backtest_config_sha256 = "{file_sha256(DEFAULT_BACKTEST_CONFIG_PATH)}"
'''
    (tmp / "plans" / "plan1.toml").write_text(text)
    return tmp / "plans" / "plan1.toml"


def test_frozen_plans_reserve_the_oos_segment(research, monkeypatch):
    from src.research import plans as P
    from src.research import runs as R

    env, store, ref, tmp = research
    path = _plan(tmp, ref, store)
    p = P.register("plan1", APP, store)
    assert p["oos"] == ["2025-03-17", "2025-03-19"]
    with pytest.raises(ConfigError, match="already registered"):
        P.register("plan1", APP, store)
    # research on the reserved OOS dates is refused, before and after the reveal
    with pytest.raises(ConfigError, match="reserved"):
        R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", purpose="full")
    with pytest.raises(ConfigError, match="reserved"):
        R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", purpose="validation",
                       start="2025-03-18", end="2025-03-18")
    R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", purpose="train",
                   start="2025-03-06", end="2025-03-10")                       # the train segment is fine
    # reveals of reserved dates go through the plan, exactly as frozen; synthetic data is never revealed
    monkeypatch.setattr(R, "code_identity", lambda: {"commit": "a" * 40, "dirty": False, "source_sha256": "s" * 64,
                                                     "source_files": 1, "trusted": True, "untrusted_reasons": []})
    with pytest.raises(ConfigError, match="SYNTHETIC"):
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", "2025-03-17", "2025-03-19",
                     plan="plan1")
    # plan immutability: an edited plan is refused; a deleted plan file keeps its reservation
    path.write_text(path.read_text().replace("test plan", "edited plan"))
    with pytest.raises(ConfigError, match="modified or removed"):
        P.load_plan("plan1", APP, store)
    path.unlink()
    assert P.reserved_oos()[0]["instrument"] == "MES:synthetic"
    with pytest.raises(ConfigError, match="reserved"):
        R.run_research(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", purpose="full")


def test_plan_reveal_checks_without_synthetic_shortcut(research, monkeypatch, fx):
    """The plan checks themselves (dates, strategy list, dataset). The fixture file here carries no SYNTHETIC
    marker so the dataset is treated like market data (test-only: the values are still synthetic)."""
    from src.research import plans as P
    from src.research import runs as R

    env, store, _, tmp = research
    src = ingest(store, "unmarked.mes", [write_databento(fx, tmp / "unmarked.csv")])
    ref = store.create_continuous("unmarked.cont", [src["ref"]], specs=APP.contracts, calendar=CAL,
                                  rule="volume")["ref"]
    assert store.load(ref).manifest["synthetic"] is False
    _plan(tmp, ref, store)
    P.register("plan1", APP, store)
    assert P.reserved_oos()[0]["instrument"] == "MES"
    monkeypatch.setattr(R, "code_identity", lambda: {"commit": "a" * 40, "dirty": False, "source_sha256": "s" * 64,
                                                     "source_files": 1, "trusted": True, "untrusted_reasons": []})
    with pytest.raises(ConfigError, match="reveal it with --plan"):
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", "2025-03-17", "2025-03-19")
    with pytest.raises(ConfigError, match="freezes the OOS segment"):
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", "2025-03-17", "2025-03-18",
                     plan="plan1")
    with pytest.raises(ConfigError, match="not one of the strategy versions"):
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C4-SWING", f"dataset:{ref}", "2025-03-17", "2025-03-19",
                     plan="plan1")
    with pytest.raises(ConfigError, match="freezes dataset"):
        R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{src['ref']}#MESM25", "2025-03-17",
                     "2025-03-19", plan="plan1")
    res = R.reveal_oos(env, "vwap_mean_reversion", "V1-C2-SWING", f"dataset:{ref}", "2025-03-17", "2025-03-19",
                       plan="plan1")
    m = json.loads((res["folder"] / "manifest.json").read_text())
    assert m["oos"]["plan"]["plan"] == "plan1" and m["data"]["dataset"]["ref"] == ref
    rec = [r for r in env.ledger()[1] if r.get("type") == "oos_reveal"][0]
    assert rec["plan"]["plan_sha256"] == P.load_index()["plan1"]["sha256"]
    with pytest.raises(ConfigError, match="revealed|reserved"):                # never research data again
        R.run_research(env, "vwap_mean_reversion", "V1-C4-SWING", f"dataset:{ref}", purpose="validation",
                       start="2025-03-18", end="2025-03-18")


def test_plan_validation_rules(research):
    from src.research import plans as P

    env, store, ref, tmp = research
    path = _plan(tmp, ref, store, oos=("2025-03-14", "2025-03-19"))          # no embargo gap after validation
    with pytest.raises(ConfigError, match="embargo"):
        P.parse_plan(path, APP, store)
    path = _plan(tmp, ref, store, oos=("2025-03-17", "2025-03-31"))          # beyond the dataset
    with pytest.raises(ConfigError, match="does not cover"):
        P.parse_plan(path, APP, store)
    path = _plan(tmp, ref, store, strategies=('"vwap_mean_reversion@V9"',))
    with pytest.raises(ConfigError):
        P.parse_plan(path, APP, store)
    path = _plan(tmp, ref, store)
    path.write_text(path.read_text().replace(store.load(ref).manifest["manifest_sha256"], "0" * 64))
    with pytest.raises(ConfigError, match="substituted"):
        P.parse_plan(path, APP, store)


def test_terminal_dataset_view_is_read_only(research):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app
    from src.terminal.modes import ModeOptions
    from src.terminal.service import TerminalService

    env, store, ref, tmp = research
    svc = TerminalService(ModeOptions(mode="mock", research_root=env.out_root, datasets_root=store.root))
    app = create_app(svc, token="t" * 32, port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        h = {"Authorization": "Bearer " + "t" * 32}
        body = c.get("/api/research/overview", headers=h).json()
        refs = {d["ref"]: d for d in body["data"]["datasets"]}
        assert ref in refs and refs[ref]["synthetic"] is True
        assert set(refs[ref]["readiness"].values()) == {"NOT_READY"}
        assert c.get("/api/research/overview").status_code in (401, 403)
        for m in ("post", "put", "delete"):
            assert getattr(c, m)("/api/research/overview", headers=h).status_code == 405
        # a corrupted dataset is shown as an integrity failure, never hidden
        d = store.root / "syn.mes" / "v1"
        for x in [d, *d.rglob("*")]:
            os.chmod(x, 0o755 if x.is_dir() else 0o644)
        (d / "gaps.csv").write_text("tampered\n")
        refs = {x["ref"]: x for x in c.get("/api/research/overview", headers=h).json()["data"]["datasets"]}
        assert refs["syn.mes@v1"]["integrity_ok"] is False and "gaps.csv was modified" in refs["syn.mes@v1"]["error"]


def test_data_cli_end_to_end(tmp_path, fx, capsys):
    import run_data

    f = write_databento(one(fx), tmp_path / "a.csv")
    root = ["--root", str(tmp_path / "cli")]
    assert run_data.main(root + ["ingest", "--dataset-id", "cli.mes", "--adapter", "databento_ohlcv_csv",
                                 "--file", str(f)]) == 0
    assert '"status": "VALIDATED"' in capsys.readouterr().out
    assert run_data.main(root + ["verify", "cli.mes@v1"]) == 0
    assert run_data.main(root + ["readiness", "cli.mes@v1"]) == 0
    assert '"oos_validation"' in capsys.readouterr().out
    assert run_data.main(root + ["show", "cli.mes@v9"]) == 1
    assert "not registered" in capsys.readouterr().err
    assert run_data.main(root + ["ingest", "--dataset-id", "cli.bad", "--adapter", "nope", "--file", str(f)]) == 1
    assert run_data.main(root + ["recover"]) == 0


# ============================================================================ safety boundaries
def test_the_data_layer_has_no_network_credential_or_order_path():
    banned = {"requests", "urllib", "http", "socket", "httpx", "websockets", "src.paper", "src.backtest.engine",
              "src.risk", "src.marketdata"}
    for p in (ROOT / "src" / "histdata").glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for m in mods:
                assert not any(m == b or m.startswith(b + ".") for b in banned), f"{p.name} imports {m}"
        text = p.read_text(encoding="utf-8").lower()
        assert "api_key" not in text and "os.environ[" not in text


# ============================================================================ audit regressions (Sprint 12)
def test_dataset_references_cannot_traverse_paths():
    from src.histdata.store import parse_ref

    for bad in ("../x@v1", "x/../../y@v1", "abc@1", "abc@v0", "ABC@v1", "abc", "abc@v1/../v2"):
        with pytest.raises(DatasetError):
            parse_ref(bad)
    assert parse_ref("databento.mes.1m@v12") == ("databento.mes.1m", "v12")


def test_a_double_reservation_is_refused(tmp_path, store, fx):
    r = ingest(store, "race", [write_databento(one(fx).head(2000), tmp_path / "a.csv")])
    st = store._stage()
    m = {**store.load(r["ref"]).manifest, "manifest_sha256": None, "version": "v1"}
    with pytest.raises(DatasetError, match="already|never overwritten"):
        store._finalize(st, m, "VALIDATED")
    # an ambiguous registry (two reservations of one version) is refused at load
    store.registry.append({"kind": "reserved", "dataset_id": "race", "version": "v1", "manifest_sha256": "0" * 64,
                           "status": "VALIDATED", "synthetic": False, "dataset_kind": "per_contract", "staging": "x"})
    with pytest.raises(DatasetError, match="ambiguous"):
        store.load(r["ref"])


def test_content_hash_does_not_depend_on_timestamp_resolution(fx):
    from src.histdata.schema import frame_sha256

    b = one(fx).head(100)
    ns = b.assign(ts_utc=pd.DatetimeIndex(b["ts_utc"]).as_unit("ns"))
    us = b.assign(ts_utc=pd.DatetimeIndex(b["ts_utc"]).as_unit("us"))
    assert frame_sha256(ns) == frame_sha256(us)
    assert frame_sha256(ns) != frame_sha256(ns.assign(close=ns["close"] + 0.25))
