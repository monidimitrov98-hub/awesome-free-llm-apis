# Sprint 13 P0: Rollover safeguards and the data-conflict register

Data-layer work only. Nothing in this step runs a strategy, a backtest or an optimization, touches any OOS
data, or changes AccountGuard, NewsGuard, RiskManager, prop firm compliance, the NinjaTrader bridge or any
strategy logic. No dataset was modified and no conflict was recorded in the real store.

## 1. Why

The Sprint 13 data-quality assessment of the eight imported NinjaTrader contracts found two gaps:

- **Rollover:** `build_continuous` sorted contracts by expiry without checking the sequence. Given MESH24,
  MESM24, MESU24 and MESZ26, it would have rolled from U24 straight to Z26 five trade dates before U24's
  expiry. The U24 trade dates for which Z26 has no bars would have been dropped silently, and nothing would
  have flagged the missing contracts Z24 to U26. Bars outside the exchange schedule were also dropped without
  a record, and roll dates were counted on weekdays, ignoring holidays.
- **Conflicts:** `ninjatrader.mes.1m@v1` and `ninjatrader.mes.z26.1m@v1` are both VALIDATED, but they disagree
  on 7 minutes. The two MNQ datasets disagree on the same minutes. Nothing recorded the disagreement, and
  research could have used either dataset.

## 2. Rollover safeguards (`src/histdata/rollover.py`, `DatasetStore.create_continuous`)

| Risk | Safeguard |
|---|---|
| Missing quarterly contract | `check_contract_sequence` refuses any gap in the calendar's listed months (`HMUZ`) and names the missing contracts. Build one continuous series per contiguous block instead. |
| Unlisted month / not a contract code | Refused. |
| Bars outside the schedule | Refused with examples. Before, they were dropped silently. Such bars are validation errors, so a VALIDATED source has none. |
| Silent date loss | Refused if the contract selected for a trade date has no bars on that date. |
| Duplicate sessions | Internal check: one contract per trade date and one bar per minute. |
| Roll dates and holidays | The days before expiry are counted without VERIFIED full-day closures, so a holiday inside the window moves the calendar roll to the correct session. The calendar has no verified holidays yet, so current results are unchanged. |
| Failed or conflicted sources | `create_continuous` refuses sources below VALIDATED, superseded sources and sources with an unresolved conflict. |
| Accounting | `lineage.roll_accounting` in the manifest records, per contract: bars in, selected and not selected; the trade dates per contract; the dates on which the last contract sits inside its roll window; and the closures used. The counts must add up, or the build fails. |

Unchanged: rolls happen only at trade-date boundaries, prices and timestamps are never modified, roll gaps are
reported, and adjusted series stay refused for simulation.

## 3. Data-conflict register (`src/histdata/conflicts.py`)

- `run_data.py conflicts A B` is a **read-only** comparison. It covers the contracts both datasets hold, inside
  the period both cover, minute by minute:
  - every minute with differing OHLCV, with both values;
  - every minute present in only one dataset.

  The report is deterministic: the pair is ordered by reference, and the report is identified by its SHA-256.
- `--record --by NAME [--note TEXT]` is a **human** decision. It writes the full report read-only to
  `data/datasets/_conflicts/<sha>.json` and appends a hash-chained record to `data/datasets/conflicts.jsonl`.
  Recording is refused when:
  - the two datasets agree;
  - the report was modified;
  - either manifest changed since the comparison;
  - the conflict is already recorded.
- **Effect:** both datasets are refused, with the conflict ID and the other dataset named, by:
  - research runs (`ResearchEnv.dataset`);
  - plan registration and checks;
  - continuous series;
  - cross-validation.

  Readiness reports show the conflict and give the verdict BLOCKED.
- `run_data.py supersede OLD --with NEW --by NAME --reason TEXT` is a **human** decision. It needs a recorded
  conflict between the two datasets with intact evidence. The superseded dataset stays refused for good, and
  the other one becomes usable again. It is refused when a dataset is already superseded, when the
  "canonical" dataset is itself superseded, and when a dataset would supersede itself.
- Nothing is merged, deleted, downgraded or edited. Both versions stay byte-identical and loadable.
- **Integrity:**
  - every load re-verifies each report a dataset is named in (file hash, content hash, chain);
  - a modified or missing report makes the affected datasets refuse to load, so it fails closed;
  - the register is a separate file, so `registry.jsonl` keeps its record kinds and older code still reads it;
  - like every store here, it is tamper-evident, not tamper-proof: deleting the whole register is visible
    only from backups.

## 4. Real data: comparison only (nothing recorded)

| Pair | Common minutes | Differing | Only in one | Volume (old → new) | Report SHA-256 |
|---|---|---|---|---|---|
| `ninjatrader.mes.1m@v1` / `ninjatrader.mes.z26.1m@v1` | 8,280 | 7 | 0 | 12,044,058 → 12,048,488 | `d8c3886dc22cc2d846f9b0b0ae3700a9d805b52ae4cf239942f04acb843524db` |
| `ninjatrader.mnq.1m@v1` / `ninjatrader.mnq.z26.1m@v1` | 8,280 | 7 | 0 | 26,949,957 → 26,958,040 | `dc2fd597fe746e3fab2cd98206859a1183885bb1765f57a25e29ff33bfa3c6d2` |

The differing minutes fall at the same times in both roots:

- 2026-11-17 10:24 CT;
- 2026-11-18 00:35 CT (MNQ also 00:36);
- 2026-11-20 02:06–02:07 CT;
- 2026-11-23 10:21–10:22 CT;
- 2026-11-24 09:37 CT (MES only).

Recording these conflicts, and choosing which version is canonical, waits for your decision. Until a conflict
is recorded, `ninjatrader.mes.z26.1m@v1` stays VALIDATED and research-eligible.

## 5. Tests

`tests/test_sprint13_p0_rollover_conflicts.py`, 12 tests, SYNTHETIC fixtures:

- **Rollover:**
  - a missing contract, the whole 2024 → 2026 gap, unlisted months and non-contract codes are refused;
  - Saturday bars are refused, not dropped;
  - a front contract missing one trade date is refused;
  - every bar is accounted for, with one contract per trade date and prices unchanged;
  - a VERIFIED closure moves the calendar roll from 2025-03-14 to 2025-03-13;
  - the last contract inside its roll window is reported;
  - a VALIDATION_FAILED source is refused, and the accounting is stored in the manifest.
- **Conflicts:**
  - the comparison is exact and independent of pair order, and comparing records nothing;
  - a recorded conflict refuses both datasets in research runs, readiness, continuous series and the
    superseding rules;
  - agreeing datasets, forged reports and comparing a dataset with itself are refused;
  - tampered or missing evidence fails closed;
  - CLI coverage for compare, record (`--by` required), list and supersede.

## 6. Sprint 12.1 real-export tests: content-addressed fixture lookup

**Problem.** The four real-export tests found their files by path in the inbox (`<repo>/../market-data-inbox`).
The inbox now holds the full `12-26` exports under the same file names, so the tests' SHA-256 check failed.
The importer was not at fault.

**Fix** (`tests/test_sprint12_1_ninjatrader_import.py`, test code only). The tests identify the validated
6-day exports by content: exact SHA-256 and size. They look for those bytes, in order, in:

1. `FTS_NT_INBOX`, if set. This is an explicit request, so a file there with other content **fails** the test.
2. The default inbox.
3. The immutable raw byte copies kept by the local dataset store (`data/datasets/*/v*/raw/<sha16>_*`).

The first match is copied under its original file name, because the adapter reads the contract from the
name, and checked again before use. No assertion was removed. One was added: the original file is unchanged
after the test. When no candidate matches, for example in a cloud checkout, the tests are skipped with the
list of files checked. The tests never run on other bytes.

`test_original_export_lookup_is_content_addressed` checks the lookup itself:

- a same-named file with other content is never used;
- the first content match is returned;
- an explicit mismatch fails.
