"""M5: append-only, hash-chained validation ledger; M3: overlap detection (unit level)."""

import json
import shutil

import pandas as pd
import pytest

from src.backtest.validation import (
    LedgerError,
    LedgerMissingError,
    Period,
    ValidationLedger,
    resolve_ledger_path,
    revealed_conflicts,
)

P = Period("out_of_sample", "out_of_sample", pd.Timestamp("2025-03-17"), pd.Timestamp("2025-03-18"))


def reveal(fp="fp1", start="2025-03-17", end="2025-03-18", instrument="MNQ"):
    return {"type": "oos_reveal", "instrument": instrument, "start": start, "end": end, "fingerprint": fp}


@pytest.fixture
def ledger(tmp_path):
    led = ValidationLedger(tmp_path / "oos_ledger.jsonl")
    led.initialise("test")
    led.append(reveal())
    led.append(reveal("fp2", "2025-03-19", "2025-03-20"))
    return led


def lines(led):
    return led.path.read_text(encoding="utf-8").splitlines(keepends=True)


def test_valid_ledger(ledger):
    recs = ledger.records()
    assert [r["seq"] for r in recs] == [0, 1, 2] and recs[0]["type"] == "genesis"
    assert recs[2]["prev_hash"] == recs[1]["record_hash"]


def test_missing_ledger_and_head(tmp_path, ledger):
    with pytest.raises(LedgerMissingError):
        ValidationLedger(tmp_path / "none.jsonl").records()
    ledger.head_path.unlink()
    with pytest.raises(LedgerError, match="head"):
        ledger.records()


def test_modified_record_detected(ledger):
    ls = lines(ledger)
    rec = json.loads(ls[1])
    rec["fingerprint"] = "tampered"
    ls[1] = json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
    ledger.path.write_text("".join(ls), encoding="utf-8")
    with pytest.raises(LedgerError, match="verification"):
        ledger.records()


def test_deleted_middle_record_detected(ledger):
    ls = lines(ledger)
    ledger.path.write_text(ls[0] + ls[2], encoding="utf-8")
    with pytest.raises(LedgerError):
        ledger.records()


def test_deleted_last_record_detected_by_head(ledger):
    ls = lines(ledger)
    ledger.path.write_text("".join(ls[:-1]), encoding="utf-8")
    with pytest.raises(LedgerError, match="head"):
        ledger.records()


def test_truncated_file_detected(ledger):
    text = ledger.path.read_text(encoding="utf-8")
    ledger.path.write_text(text[:-25], encoding="utf-8")
    with pytest.raises(LedgerError, match="truncated"):
        ledger.records()


def test_invalid_json_detected(ledger):
    ledger.path.write_text("{corrupt\n", encoding="utf-8")
    with pytest.raises(LedgerError):
        ledger.records()


def test_damaged_ledger_is_never_recreated_or_appended(ledger):
    ledger.path.write_text("{corrupt\n", encoding="utf-8")
    with pytest.raises(LedgerError):
        ledger.initialise()
    with pytest.raises(LedgerError):
        ledger.append(reveal())
    assert ledger.path.read_text(encoding="utf-8") == "{corrupt\n"  # untouched


def test_duplicate_reveal_is_recorded_not_overwritten(ledger):
    ledger.append(reveal())
    recs = ledger.records()
    assert sum(r.get("fingerprint") == "fp1" for r in recs) == 2 and len(recs) == 4


def test_recovery_from_backup(tmp_path, ledger):
    backup = tmp_path / "backup"
    backup.mkdir()
    shutil.copy(ledger.path, backup / ledger.path.name)
    shutil.copy(ledger.head_path, backup / ledger.head_path.name)
    ledger.path.write_text("garbage\n", encoding="utf-8")
    with pytest.raises(LedgerError):
        ledger.records()
    shutil.copy(backup / ledger.path.name, ledger.path)
    shutil.copy(backup / ledger.head_path.name, ledger.head_path)
    assert len(ledger.records()) == 3


def test_overlap_rules():
    recs = [{"seq": 1, **reveal("A")}]
    same = revealed_conflicts(recs, "MNQ", P, "A")
    exact = revealed_conflicts(recs, "MNQ", P, "B")
    shifted = revealed_conflicts(recs, "MNQ", Period("o", "out_of_sample", pd.Timestamp("2025-03-14"),
                                                     pd.Timestamp("2025-03-17")), "B")
    partial = revealed_conflicts(recs, "MNQ", Period("o", "out_of_sample", pd.Timestamp("2025-03-18"),
                                                     pd.Timestamp("2025-03-25")), "B")
    in_sample = revealed_conflicts(recs, "MNQ", Period("is", "in_sample", pd.Timestamp("2025-03-01"),
                                                       pd.Timestamp("2025-03-18")), "B")
    full = revealed_conflicts(recs, "MNQ", Period("full", "full"), "B")
    disjoint = revealed_conflicts(recs, "MNQ", Period("o", "out_of_sample", pd.Timestamp("2025-03-19"), None), "B")
    other = revealed_conflicts(recs, "MES", P, "B")
    assert not same and exact and shifted and partial and in_sample and full
    assert not disjoint and not other


def test_ledger_location_independent_of_output_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FTS_LEDGER_PATH", raising=False)
    path, canonical = resolve_ledger_path(None)
    assert canonical and path.parts[-2:] == ("validation_history", "oos_ledger.jsonl")
    custom, canonical = resolve_ledger_path(tmp_path / "x.jsonl")
    assert not canonical and custom == (tmp_path / "x.jsonl").resolve()
