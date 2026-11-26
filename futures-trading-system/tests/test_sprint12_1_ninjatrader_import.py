"""Sprint 12.1: first real NinjaTrader import (MES 12-26, MNQ 12-26) and the import hardening it needed.

Two kinds of test, kept apart on purpose:

* UNIT tests use small controlled fixtures (SYNTHETIC or hand-written lines). They exercise the code and say
  nothing about markets.
* REAL-DATA integration tests (``test_real_*``) run on the actual 6-day NinjaTrader exports validated in
  Sprint 12.1, identified by CONTENT (SHA-256 and size), never by path alone. The inbox is mutable (newer
  exports reuse the same file names), so the bytes are looked up, in order, in:

  1. ``FTS_NT_INBOX`` when set: an explicit request, so a file there with other content FAILS the test;
  2. the default inbox ``<repo>/../market-data-inbox``;
  3. the immutable raw byte copies kept by the local dataset store (``data/datasets/*/v*/raw/<sha16>_*``).

  The first candidate whose SHA-256 and size match is copied under its original file name (the adapter reads
  the contract from it) and checked again before use. When no candidate matches (e.g. a cloud checkout) the
  tests are SKIPPED with the reason, never silently run on other bytes or replaced by fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pandas as pd
import pytest

import run_data
from src.config import load_config
from src.data.errors import TimestampError
from src.histdata.providers import make_adapter
from src.histdata.quality import clock_alignment
from src.histdata.readiness import assess
from src.histdata.store import DatasetError, DatasetStore
from tests.histdata_fixtures import CAL, bars, write_nt

ROOT = Path(__file__).resolve().parents[1]
APP = load_config()
PROVENANCE = ROOT / "config" / "datasets" / "ninjatrader_2026-11.provenance.json"
EXPLICIT_INBOX = Path(os.environ["FTS_NT_INBOX"]) if os.environ.get("FTS_NT_INBOX") else None
DEFAULT_INBOX = ROOT.parent / "market-data-inbox"
STORE_ROOT = Path(os.environ.get("FTS_DATASETS_ROOT") or ROOT / "data" / "datasets")
REAL = {
    "MES": ("MES 12-26.Last.txt", "2a9db3ca7b7bb443147d09e03fccfd7017263a34cbcb5459c0d7d47a4c121897", 408741),
    "MNQ": ("MNQ 12-26.Last.txt", "d744b0fdd0f5a47ad126592aa79198badbd4cfeffe7ccd2e885645bffd97b5e2", 444216),
}
UTC_NT = make_adapter("ninjatrader_export", timezone="UTC")


@pytest.fixture()
def store(tmp_path):
    return DatasetStore(tmp_path / "store")


def ingest(store, did, files, adapter=UTC_NT, **kw):
    return store.ingest(did, adapter, [Path(f) for f in files], specs=APP.contracts, calendar=CAL, **kw)


def codes(store, ref):
    return {f["code"]: f for f in store.load(ref).validation()["findings"]}


def week(contract="MESZ26"):
    """SYNTHETIC: every scheduled-open minute of one contract, Sunday 2026-11-15 17:00 CT -> Fri 2026-11-20."""
    return bars(start="2026-11-15 23:00", end="2026-11-20 22:00", contracts=(contract,), roll_day="2026-01-01")


def nt_file(tmp_path, b, name="MES 12-26.Last.txt", tz="UTC", synthetic=False):
    p = write_nt(b, tmp_path / name, tz=tz)
    if synthetic:
        p.write_text("# SYNTHETIC fixture: not market prices\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    return p


def lines_file(tmp_path, lines, name="MES 12-26.Last.txt", newline="\r\n"):
    p = tmp_path / name
    p.write_bytes(newline.join(lines).encode("ascii") + newline.encode("ascii"))
    return p


# ============================================================================ parsing (unit, fixtures)
def test_semicolon_parsing_timestamps_and_original_text_are_kept(tmp_path):
    p = lines_file(tmp_path, ["20261123 143100;7276.75;7282;7254.5;7256.75;10893",
                              "20261123 143200;7257;7267.5;7243.25;7245.75;7616"])
    out = UTC_NT.parse(p)
    r = out.records
    assert list(r["ts_utc"]) == [pd.Timestamp("2026-11-23 14:30", tz="UTC"), pd.Timestamp("2026-11-23 14:31", tz="UTC")]
    assert list(r["source_ts"]) == ["20261123 143100", "20261123 143200"]       # original text, unchanged
    assert r.loc[0, ["open", "high", "low", "close", "volume"]].tolist() == [7276.75, 7282.0, 7254.5, 7256.75, 10893.0]
    assert r["symbol"].unique().tolist() == ["MES 12-26"]
    s = out.stats
    assert (s["encoding"], s["bom"], s["line_endings"], s["delimiter"]) == ("ascii", False, "CRLF", ";")
    assert (s["lines"], s["data_rows"], s["accepted_rows"], s["rejected_rows"]) == (2, 2, 2, 0)
    assert out.source_timezone == "UTC" and "shifted back one bar" in out.timestamp_semantics


def test_europe_sofia_is_utc_plus_2_in_november_and_conversion_happens_once(tmp_path):
    p = lines_file(tmp_path, ["20261123 163100;7276.75;7282;7254.5;7256.75;10893"])
    sofia = make_adapter("ninjatrader_export", timezone="Europe/Sofia").parse(p).records
    assert sofia.loc[0, "ts_utc"] == pd.Timestamp("2026-11-23 14:30", tz="UTC")   # 16:31 EET end -> 14:30 UTC open
    assert sofia.loc[0, "source_ts"] == "20261123 163100"
    # the stored bar re-read from a dataset is the same instant (no second conversion anywhere)
    st = DatasetStore(tmp_path / "s")
    r = st.ingest("t.sofia", make_adapter("ninjatrader_export", timezone="Europe/Sofia"), [p], specs=APP.contracts,
                  calendar=CAL)
    b = st.load(r["ref"]).bars()
    assert b.loc[0, "ts_utc"] == pd.Timestamp("2026-11-23 14:30", tz="UTC") and b.loc[0, "source_ts"] == "20261123 163100"


def test_europe_sofia_dst_transitions_are_never_guessed(tmp_path):
    summer = lines_file(tmp_path, ["20260715 120100;7000;7000.25;7000;7000;5"], name="MES 09-26.Last.txt")
    sofia = make_adapter("ninjatrader_export", timezone="Europe/Sofia")
    assert sofia.parse(summer).records.loc[0, "ts_utc"] == pd.Timestamp("2026-07-15 09:00", tz="UTC")   # EEST +3
    gap = lines_file(tmp_path, ["20260329 033100;7000;7000.25;7000;7000;5"], name="MES 06-26.Last.txt")    # skipped hour
    fold = lines_file(tmp_path, ["20261025 033100;7000;7000.25;7000;7000;5"], name="MES 12-26.Last.txt")  # repeated hour
    with pytest.raises(TimestampError):
        sofia.parse(gap)
    with pytest.raises(TimestampError):
        sofia.parse(fold)
    lenient = make_adapter("ninjatrader_export", timezone="Europe/Sofia", ambiguous="NaT", nonexistent="NaT")
    for f in (gap, fold):
        out = lenient.parse(f)
        assert out.records.empty and out.rejected[0]["reason"] == "unparseable or ambiguous timestamp"


def test_malformed_rows_are_rejected_with_line_numbers_and_quarantined(tmp_path, store):
    b = week().head(400)
    good = write_nt(b, tmp_path / "x.txt", tz="UTC").read_text(encoding="utf-8").splitlines()
    lines = (["Date;Open;High;Low;Close;Volume"] + good[:100] + ["", "20261116 0101;1;1;1;1;1",          # bad stamp
             "20261116 010200;7000;7000;7000;7000", "20261116 010300;7000;abc;7000;7000;5",           # 5 fields / text
             "20261332 250000;7000;7000;7000;7000;5", "   "] + good[100:])                              # impossible date
    p = lines_file(tmp_path, lines)
    out = UTC_NT.parse(p)
    s = out.stats
    assert (s["header_lines"], s["blank_lines"]) == (1, 2)
    reasons = {x["row"]: x["reason"] for x in out.rejected}
    assert reasons == {103: "malformed timestamp (expected yyyyMMdd HHmmss)",
                       104: "expected 6 ';'-separated fields, got 5", 105: "non-numeric high",
                       106: "unparseable or ambiguous timestamp"}
    r = ingest(store, "nt.bad", [p])
    assert r["status"] == "VALIDATION_FAILED" and r["rejected_rows"] == 4
    ds = store.load(r["ref"])
    q = pd.read_csv(ds.path / "rejected.csv")
    assert q["row"].tolist() == [103, 104, 105, 106] and set(q["name"]) == {"MES 12-26.Last.txt"}
    assert "rejected.csv" in ds.manifest["files"]                    # hash-protected like every other file
    f = codes(store, r["ref"])
    assert f["BAD_SOURCE_ROWS"]["count"] == 4 and f["MISSING_VALUES"]["count"] == 1   # the 'abc' row never filled
    assert len(ds.bars()) == 400                                     # every valid row kept, nothing invented


def test_an_undecodable_file_is_refused(tmp_path):
    p = tmp_path / "MES 12-26.Last.txt"
    p.write_bytes(b"20261116 230100;7441.5;7449.25;7439.75;7449;16\xff\xfe\n")
    with pytest.raises(Exception, match="not ASCII / UTF-8"):
        UTC_NT.parse(p)


# ============================================================================ validation (unit, fixtures)
def test_valid_mes_and_mnq_imports(tmp_path, store):
    for root, c in (("MES", "MESZ26"), ("MNQ", "MNQZ26")):
        r = ingest(store, f"nt.{root.lower()}", [nt_file(tmp_path, week(c), name=f"{root} 12-26.Last.txt")])
        assert r["status"] == "VALIDATED", codes(store, r["ref"])
        ds = store.load(r["ref"])
        assert ds.manifest["contracts"].keys() == {c} and ds.manifest["roots"] == [root]
        assert ds.validation()["clock_alignment"]["zero_closed_shifts_hours"] == [0.0]
        assert not [f for f in ds.validation()["findings"] if f["severity"] == "error"]


def test_duplicates_and_non_monotonic_timestamps(tmp_path, store):
    lines = write_nt(week().head(300), tmp_path / "x.txt", tz="UTC").read_text(encoding="utf-8").splitlines()
    exact = lines[:150] + [lines[149]] + lines[150:]
    r = ingest(store, "nt.exact", [lines_file(tmp_path, exact, name="MES 12-26.Last.txt")])
    assert codes(store, r["ref"])["EXACT_DUPLICATES"]["count"] == 1 and r["status"] != "VALIDATION_FAILED"
    t, o, h, lo, c, v = lines[149].split(";")
    conflicting = lines[:150] + [f"{t};{o};{h};{lo};{c};{int(v) + 1}"] + lines[150:]
    r = ingest(store, "nt.conflict", [lines_file(tmp_path, conflicting, name="MES 12-26.Last.txt")])
    assert r["status"] == "VALIDATION_FAILED" and codes(store, r["ref"])["CONFLICTING_DUPLICATES"]["count"] == 2
    swapped = lines[:10] + [lines[11], lines[10]] + lines[12:]
    r = ingest(store, "nt.order", [lines_file(tmp_path, swapped, name="MES 12-26.Last.txt")])
    assert "OUT_OF_ORDER" in codes(store, r["ref"])
    assert pd.DatetimeIndex(store.load(r["ref"]).bars()["ts_utc"]).is_monotonic_increasing


@pytest.mark.parametrize("edit,code", [
    (lambda o, h, lo, c, v: (o, lo - 1, lo, c, v), "OHLC_INCONSISTENT"),       # high below low
    (lambda o, h, lo, c, v: (o, h, lo, c, -5), "NEGATIVE_VOLUME"),
    (lambda o, h, lo, c, v: (o + 0.1, h + 0.1, lo, c, v), "OFF_TICK_PRICE"),
    (lambda o, h, lo, c, v: (0, h, 0, c, v), "NON_POSITIVE_PRICE"),
])
def test_invalid_ohlcv_is_an_error_and_never_repaired(tmp_path, store, edit, code):
    lines = write_nt(week().head(200), tmp_path / "x.txt", tz="UTC").read_text(encoding="utf-8").splitlines()
    t, *vals = lines[50].split(";")
    o, h, lo, c, v = (float(x) for x in vals)
    lines[50] = t + ";" + ";".join(f"{x:g}" for x in edit(o, h, lo, c, v))
    r = ingest(store, "nt.bad", [lines_file(tmp_path, lines)])
    assert r["status"] == "VALIDATION_FAILED" and code in codes(store, r["ref"])
    kept = store.load(r["ref"]).bars()
    row = kept[kept["source_ts"] == t]
    assert row.empty or row.iloc[0][["open", "high", "low", "close"]].tolist() == list(edit(o, h, lo, c, v))[:4]


def test_zero_volume_is_reported_separately_as_a_warning(tmp_path, store):
    lines = write_nt(week().head(200), tmp_path / "x.txt", tz="UTC").read_text(encoding="utf-8").splitlines()
    lines[20] = ";".join(lines[20].split(";")[:5] + ["0"])
    r = ingest(store, "nt.zero", [lines_file(tmp_path, lines)])
    f = codes(store, r["ref"])["ZERO_VOLUME"]
    assert f["severity"] == "warning" and f["count"] == 1 and r["status"] != "VALIDATION_FAILED"


def test_expected_closures_versus_unexpected_gaps(tmp_path, store):
    b = week()
    r = ingest(store, "nt.full", [nt_file(tmp_path, b)])
    f = codes(store, r["ref"])
    assert f["SCHEDULED_BREAK"]["severity"] == "info" and f["SCHEDULED_BREAK"]["count"] == 4    # Mon-Thu halts
    assert "MISSING_DATA_RTH" not in f and "MISSING_DATA_ETH" not in f and r["status"] == "VALIDATED"
    # remove 45 RTH minutes on Wednesday (09:00-09:45 CT): an unexplained gap, reported, never filled
    loc = pd.DatetimeIndex(b["ts_utc"]).tz_convert("America/Chicago")
    hole = (loc.date == pd.Timestamp("2026-11-18").date()) & (loc.hour == 9) & (loc.minute < 45)
    r = ingest(store, "nt.hole", [nt_file(tmp_path, b[~hole], name="MES 12-26.Last.txt")])
    f = codes(store, r["ref"])
    assert f["MISSING_DATA_RTH"]["count"] == 1 and r["status"] == "INGESTED"          # > 30-minute RTH gap
    assert len(store.load(r["ref"]).bars()) == int((~hole).sum())


def test_a_wrong_timezone_is_caught_and_the_likely_shift_is_named(tmp_path, store):
    p = nt_file(tmp_path, week(), tz="UTC")                             # written in UTC, declared as Sofia
    r = ingest(store, "nt.wrong", [p], adapter=make_adapter("ninjatrader_export", timezone="Europe/Sofia"))
    f = codes(store, r["ref"])
    assert r["status"] == "VALIDATION_FAILED" and f["BAR_WHILE_MARKET_CLOSED"]["count"] > 0
    assert "+2 h" in f["CLOCK_MISALIGNMENT_SUSPECTED"]["message"]
    clock = store.load(r["ref"]).validation()["clock_alignment"]
    assert clock["suspected_shift_hours"] == 2.0 and clock["zero_closed_shifts_hours"] == [2.0]


def test_clock_alignment_is_a_diagnostic_and_handles_dst(tmp_path):
    # a week spanning the US fall-back (2026-11-01): the schedule still pins the clock to shift 0
    b = bars(start="2026-10-28 22:00", end="2026-11-04 22:00", contracts=("MESZ26",), roll_day="2026-01-01")
    out = clock_alignment(b.assign(root="MES"), CAL)
    assert out["declared_closed_bars"] == 0 and out["unique"] and out["suspected_shift_hours"] is None
    # a single RTH hour cannot pin the clock: several shifts fit, and the report says so
    short = b[pd.DatetimeIndex(b["ts_utc"]).tz_convert("America/Chicago").hour == 10].head(60)
    assert not clock_alignment(short, CAL)["unique"]


# ============================================================================ store: idempotency, hashes, provenance
def test_idempotent_reimport_and_changed_file_detection(tmp_path, store):
    p = nt_file(tmp_path, week())
    first = ingest(store, "nt.mes", [p])
    n_records = len(store.records())
    again = ingest(store, "nt.mes", [p])
    assert again == {"created": False, "ref": first["ref"], "status": first["status"], "reason": "identical source files"}
    assert len(store.records()) == n_records and len(store.versions("nt.mes")) == 1
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")         # the export changed (one bar fewer)
    second = ingest(store, "nt.mes", [p])
    assert second["created"] and second["ref"] == "nt.mes@v2"
    ch = second["changed_sources"][0]
    assert (ch["name"], ch["previous_ref"]) == ("MES 12-26.Last.txt", "nt.mes@v1")
    assert codes(store, "nt.mes@v2")["SOURCE_FILE_CHANGED"]["severity"] == "warning"
    v1 = store.load("nt.mes@v1")                                         # the earlier version is untouched
    assert v1.manifest["rows"] == len(lines) and v1.manifest["source_files"][0]["sha256"] == ch["previous_sha256"]


def test_raw_files_are_preserved_byte_for_byte(tmp_path, store):
    p = nt_file(tmp_path, week())
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    r = ingest(store, "nt.mes", [p])
    assert p.read_bytes() == before and p.stat().st_mtime_ns == mtime
    ds = store.load(r["ref"])
    src = ds.manifest["source_files"][0]
    assert (ds.path / src["stored_as"]).read_bytes() == before and src["bytes"] == len(before)
    assert (src["first_source_ts"], src["last_source_ts"]) == tuple(p.read_text().splitlines()[i].split(";")[0]
                                                                     for i in (0, -1))


def test_provenance_is_recorded_hash_protected_and_free_of_secrets(tmp_path, store):
    prov = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    r = ingest(store, "nt.mes", [nt_file(tmp_path, week())], provenance=prov)
    ds = store.load(r["ref"])
    assert ds.manifest["provenance"] == prov and ds.validation()["provenance"] == prov
    assert ds.manifest["provenance"]["timezone"]["basis"] == "empirical_chart_export_comparison"
    assert ds.manifest["provenance"]["timezone"]["explicit_export_setting"] is False
    m = json.loads((ds.path / "manifest.json").read_text(encoding="utf-8"))
    m["provenance"]["timezone"]["value"] = "Europe/Sofia"                # tamper with the recorded evidence
    os.chmod(ds.path / "manifest.json", 0o644)
    (ds.path / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(DatasetError, match="self-hash"):
        store.load(r["ref"])
    for bad in ({"api_key": "x"}, {"source": {"password": "x"}}, {"notes": [{"account_id": 1}]}, ["not", "a", "dict"]):
        with pytest.raises(DatasetError):
            ingest(store, "nt.other", [nt_file(tmp_path, week().head(50), name="MES 12-26.Last.txt")], provenance=bad)


def test_readiness_has_eight_separate_dimensions(tmp_path, store):
    prov = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    r = ingest(store, "nt.mes", [nt_file(tmp_path, week())], provenance=prov)
    d = assess(store.load(r["ref"]))["dimensions"]
    assert list(d) == ["file_integrity", "structural_validity", "ohlcv_validity", "timestamp_timezone",
                       "session_calendar", "completeness", "cross_source_validation", "overall_research_readiness"]
    assert d["file_integrity"]["status"] == "PASS" and d["ohlcv_validity"]["status"] == "PASS"
    assert d["timestamp_timezone"]["status"] == "PASS_WITH_LIMITATIONS"       # empirical, not an export setting
    assert d["session_calendar"]["status"] == "PASS_WITH_LIMITATIONS"         # no verified holidays
    assert d["cross_source_validation"]["status"] == "NOT_PERFORMED"
    o = d["overall_research_readiness"]
    assert o["status"] == "NOT_READY_FOR_STRATEGY_RESEARCH" and "walk_forward" in o["unsupported_uses"]
    bad = ingest(store, "nt.bad", [nt_file(tmp_path, week(), name="MES 12-26.Last.txt")],
                 adapter=make_adapter("ninjatrader_export", timezone="Europe/Sofia"))
    d = assess(store.load(bad["ref"]))["dimensions"]
    assert d["timestamp_timezone"]["status"] == "FAIL" and d["overall_research_readiness"]["status"] == "BLOCKED"
    syn = ingest(store, "nt.syn", [nt_file(tmp_path, week(), name="MES 12-26.Last.txt", synthetic=True)])
    assert assess(store.load(syn["ref"]))["dimensions"]["overall_research_readiness"]["status"] == "BLOCKED"


def test_cli_ingest_with_provenance(tmp_path, capsys):
    p = nt_file(tmp_path, week())
    argv = ["--root", str(tmp_path / "cli"), "ingest", "--dataset-id", "nt.mes.cli", "--adapter",
            "ninjatrader_export", "--param", "timezone=UTC", "--file", str(p), "--provenance", str(PROVENANCE)]
    assert run_data.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["created"] and out["status"] == "VALIDATED" and out["rejected_rows"] == 0
    assert run_data.main(argv) == 0 and json.loads(capsys.readouterr().out)["created"] is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert run_data.main(argv[:-1] + [str(bad)]) == 1


# ============================================================================ REAL DATA (integration)
def find_original_export(name: str, sha: str, size: int, *, explicit: Path | None,
                         candidates: list[Path]) -> tuple[Path | None, list[str]]:
    """Content-addressed lookup of one validated export. Returns (matching file or None, what was checked).
    An explicit directory is strict: its file must match, or the lookup fails (AssertionError)."""
    from src.backtest.manifest import file_sha256

    if explicit is not None:
        p = explicit / name
        if not p.is_file():
            return None, [f"{p}: absent (FTS_NT_INBOX)"]
        assert p.stat().st_size == size and file_sha256(p) == sha, \
            f"{p} (FTS_NT_INBOX) is not the export validated in Sprint 12.1 (SHA-256 / size differ)"
        return p, [f"{p}: matches"]
    seen = []
    for p in candidates:
        if not p.is_file():
            seen.append(f"{p}: absent")
        elif p.stat().st_size == size and file_sha256(p) == sha:
            return p, seen + [f"{p}: matches"]
        else:
            seen.append(f"{p}: present, different content ({p.stat().st_size} bytes)")
    return None, seen


def _real(root, tmp_path):
    """(original file, verified working copy named as exported) of the validated 6-day export of ``root``."""
    from src.backtest.manifest import file_sha256

    name, sha, size = REAL[root]
    cands = [DEFAULT_INBOX / name] + sorted(STORE_ROOT.glob(f"*/v*/raw/{sha[:16]}_*"))
    src, seen = find_original_export(name, sha, size, explicit=EXPLICIT_INBOX, candidates=cands)
    if src is None:
        pytest.skip(f"validated export {name} ({sha[:16]}) not available: " + "; ".join(seen))
    work = tmp_path / "export"
    work.mkdir()
    p = work / name
    shutil.copyfile(src, p)
    assert p.stat().st_size == size and file_sha256(p) == sha, f"copy of {src} does not match"
    return src, p


def test_original_export_lookup_is_content_addressed(tmp_path):
    good, other = b"20261123 143100;1;1;1;1;1\r\n", b"20261123 143100;2;2;2;2;2\r\n"
    from src.backtest.manifest import file_sha256

    (tmp_path / "inbox").mkdir()
    (tmp_path / "store").mkdir()
    (tmp_path / "inbox" / "X 12-26.Last.txt").write_bytes(other)           # same name, newer export
    (tmp_path / "store" / "copy.txt").write_bytes(good)
    (tmp_path / "ref.txt").write_bytes(good)
    sha, size = file_sha256(tmp_path / "ref.txt"), len(good)
    cands = [tmp_path / "inbox" / "X 12-26.Last.txt", tmp_path / "store" / "copy.txt"]
    found, seen = find_original_export("X 12-26.Last.txt", sha, size, explicit=None, candidates=cands)
    assert found == tmp_path / "store" / "copy.txt" and "different content" in seen[0]
    found, seen = find_original_export("X 12-26.Last.txt", sha, size, explicit=None, candidates=cands[:1])
    assert found is None                                                      # never another file's bytes
    with pytest.raises(AssertionError, match="not the export validated"):
        find_original_export("X 12-26.Last.txt", sha, size, explicit=tmp_path / "inbox", candidates=cands)
    assert find_original_export("Y.txt", sha, size, explicit=tmp_path / "inbox", candidates=cands)[0] is None


@pytest.mark.parametrize("root", ["MES", "MNQ"])
def test_real_export_imports_and_validates(tmp_path, root):
    from src.backtest.manifest import file_sha256

    original, p = _real(root, tmp_path)
    before = p.read_bytes()
    st = DatasetStore(tmp_path / "real")
    prov = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    r = st.ingest(f"ninjatrader.{root.lower()}.1m", UTC_NT, [p], specs=APP.contracts, calendar=CAL, provenance=prov)
    assert r["status"] == "VALIDATED" and r["errors"] == 0 and r["rejected_rows"] == 0
    ds = st.load(r["ref"])
    m, v = ds.manifest, ds.validation()
    src = m["source_files"][0]
    assert (src["sha256"], src["bytes"], src["rows"], src["rejected"]) == (REAL[root][1], REAL[root][2], 8280, 0)
    assert src["parse_stats"]["line_endings"] == "CRLF" and src["parse_stats"]["blank_lines"] == 0
    assert (m["first_bar_utc"], m["last_bar_utc"]) == ("2026-11-16 23:00:00+00:00", "2026-11-24 21:59:00+00:00")
    assert m["contracts"] == {f"{root}Z26": {"first_bar": "2026-11-16 23:00:00+00:00",
                                            "last_bar": "2026-11-24 21:59:00+00:00", "bars": 8280}}
    assert v["clock_alignment"]["zero_closed_shifts_hours"] == [0.0]          # the schedule pins the clock
    f = {x["code"]: x for x in v["findings"]}
    assert f["SCHEDULED_BREAK"]["count"] == 4 and f["EXPECTED_CLOSURE_WEEKEND"]["count"] == 1
    assert not {"MISSING_DATA_RTH", "MISSING_DATA_ETH", "ZERO_VOLUME", "OHLC_INCONSISTENT", "OFF_TICK_PRICE",
                "EXACT_DUPLICATES", "OUT_OF_ORDER", "BAR_WHILE_MARKET_CLOSED"} & set(f)
    b = ds.bars()
    spike = b[b["source_ts"] == "20261123 143100"].iloc[0]                   # 09:30 ET cash open, line 6451
    assert spike["ts_utc"] == pd.Timestamp("2026-11-23 14:30", tz="UTC")
    assert spike["volume"] == {"MES": 10893, "MNQ": 12055}[root]
    again = st.ingest(f"ninjatrader.{root.lower()}.1m", UTC_NT, [p], specs=APP.contracts, calendar=CAL, provenance=prov)
    assert again["created"] is False and again["ref"] == r["ref"]
    assert p.read_bytes() == before
    assert file_sha256(original) == REAL[root][1]                           # the original is never touched


@pytest.mark.parametrize("root", ["MES", "MNQ"])
def test_real_export_read_as_europe_sofia_is_rejected(tmp_path, root):
    _, p = _real(root, tmp_path)
    st = DatasetStore(tmp_path / "real")
    r = st.ingest("probe.sofia", make_adapter("ninjatrader_export", timezone="Europe/Sofia"), [p],
                  specs=APP.contracts, calendar=CAL)
    v = st.load(r["ref"]).validation()
    f = {x["code"]: x for x in v["findings"]}
    assert r["status"] == "VALIDATION_FAILED" and f["BAR_WHILE_MARKET_CLOSED"]["count"] == 420
    assert v["clock_alignment"]["suspected_shift_hours"] == 2.0
