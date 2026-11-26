# Sprint 12.1: First real NinjaTrader data import and validation

This sprint imports and validates the first real market data in the project: two NinjaTrader 8 exports of
1-minute `Last` bars for MES 12-26 and MNQ 12-26, 2026-11-16 to 2026-11-24.

It is a data-ingestion and validation sprint only:
* no strategy was run, tuned or compared;
* no OOS segment was touched;
* no order path, broker connection or safety control was changed.

## 1. Scope and what was really validated

| Validated with the REAL exports | Tested with SYNTHETIC fixtures only | Not verified |
|---|---|---|
| File hashes, sizes, encoding, line structure, row counts | Malformed rows, bad timestamps, header and blank lines | The export timezone as a NinjaTrader *setting* (see §3) |
| Every OHLCV, tick-grid and volume check | Duplicates (exact and conflicting), out-of-order rows | The data feed NinjaTrader downloaded from |
| Timezone interpretation (UTC vs Europe/Sofia) | Invalid OHLC, negative volume, off-tick and non-positive prices | Prices against any independent source (no cross-source validation) |
| Session and closure alignment, gaps, completeness | Unexpected RTH gaps, zero volume | CME holiday schedules (none in the data range) |
| Idempotent re-import, raw-file preservation | Changed-file detection, quarantine file, provenance tampering | |
| | Europe/Sofia DST (spring gap, fall fold), clock check across a US DST change | |

## 2. Source files

Both files are in `C:\tradingbot\market-data-inbox\`, which is git-ignored. Byte copies are kept inside each
dataset version.

| | MES 12-26.Last.txt | MNQ 12-26.Last.txt |
|---|---|---|
| SHA-256 | `2a9db3ca7b7bb443147d09e03fccfd7017263a34cbcb5459c0d7d47a4c121897` | `d744b0fdd0f5a47ad126592aa79198badbd4cfeffe7ccd2e885645bffd97b5e2` |
| Size | 408,741 bytes | 444,216 bytes |
| Rows | 8,280 (no header, no blank lines) | 8,280 (no header, no blank lines) |
| Format | ASCII, no BOM, CRLF, `;`, 6 fields: `yyyyMMdd HHmmss;O;H;L;C;V` | same |
| First / last export stamp | `20261116 230100` / `20261124 220000` | same |

## 3. Timezone: what was found and what was decided

**Expected:** the export was believed to be in Europe/Sofia (UTC+02:00 in November). Windows reports
`FLE Standard Time`. NinjaTrader's `Config.xml` has an empty `TimeZoneInfoSerializable`, which means the PC
timezone.

**Found:** read as Europe/Sofia, the bars contradict the CME schedule.

| | Read as Europe/Sofia | Read as UTC | CME schedule |
|---|---|---|---|
| Daily halt | 14:00–15:00 CT | 16:00–17:00 CT | 16:00–17:00 CT |
| Sunday open / Friday close | 15:00 / 14:00 CT | 17:00 / 16:00 CT | 17:00 / 16:00 CT |
| Bars in scheduled closures | 420 per file (`VALIDATION_FAILED`) | 0 | |
| Export 14:31 volume spike | 06:30 CT | 08:30 CT = 09:30 ET cash open | |

**Confirmed by the user:** the NinjaTrader 1-minute MES 12-26 chart (Europe/Sofia display) shows the
2026-11-23 cash-open spike at about 16:29–16:32. The export has it at `20261123 143100`, on line 6451 of
both files (MES volume 10,893, MNQ 12,055).

**Decision (the user's):** the export timestamps are ingested as **UTC**, bar END.
* It is recorded as **empirically inferred**, not as an explicitly configured export timezone. See
  `config/datasets/ninjatrader_2026-11.provenance.json`, stored verbatim in each manifest and validation
  report.
* The original export text is kept unchanged in `source_ts`. Only `ts_utc` (the bar open) is derived, with
  one conversion and one 1-minute shift from bar end to bar open.

**Supporting automated evidence:** a new clock check (`clock_alignment` in `validation.json`) shifts every
bar by −14 h to +14 h in 30-minute steps. For both datasets the only shift with no bar in a closure is 0 h,
so the exchange schedule pins the clock uniquely. Read as Sofia, the same check names +2 h.

**Limitations:**
* UTC, Europe/London, Europe/Lisbon and Atlantic/Reykjavik are identical in November, so this sample cannot
  separate them. An export that spans a European DST change must be re-checked.
* Why NinjaTrader exported UTC while displaying Sofia time is not established. Future exports will catch a
  wrong declaration automatically: `BAR_WHILE_MARKET_CLOSED` plus `CLOCK_MISALIGNMENT_SUSPECTED`.

## 4. Results

The commands used to create the datasets (reproducible; a re-run returns the same versions):

```
python run_data.py ingest --dataset-id ninjatrader.mes.1m --adapter ninjatrader_export --param timezone=UTC \
    --file "../market-data-inbox/MES 12-26.Last.txt" --provenance config/datasets/ninjatrader_2026-11.provenance.json
python run_data.py ingest --dataset-id ninjatrader.mnq.1m --adapter ninjatrader_export --param timezone=UTC \
    --file "../market-data-inbox/MNQ 12-26.Last.txt" --provenance config/datasets/ninjatrader_2026-11.provenance.json
python run_data.py readiness ninjatrader.mes.1m@v1     (and ninjatrader.mnq.1m@v1)
```

The datasets live in `data/datasets/` (git-ignored, local only).

| | ninjatrader.mes.1m@v1 | ninjatrader.mnq.1m@v1 |
|---|---|---|
| Contract | MESZ26 | MNQZ26 |
| Status | VALIDATED | VALIDATED |
| Bars imported / rejected | 8,280 / 0 | 8,280 / 0 |
| First / last bar open (UTC) | 2026-11-16 23:00 / 2026-11-24 21:59 | same |
| Globex trade dates | 6 (Nov 17, 18, 19, 20, 23, 24), all complete | same |
| Open minutes present / expected | 8,280 / 8,280 | 8,280 / 8,280 |
| RTH / ETH bars | 2,340 / 5,940 | 2,340 / 5,940 |
| Errors | 0 | 0 |
| Warnings | 1 `ABNORMAL_VOLUME` (see below) | 0 |
| Info | 4 `SCHEDULED_BREAK`, 1 `EXPECTED_CLOSURE_WEEKEND` | same |
| Price range (low / high) | 6,898.00 / 7,457.00 | 24,976.00 / 27,838.00 |
| Total volume | 12,044,058 | 26,949,957 |
| Manifest SHA-256 | `5f31317d…b90c20ac9` | `4e48516c…601cba718` |

**Data-quality findings:**
* No duplicates, out-of-order rows, missing values, OHLC inconsistencies, off-tick prices (0.25), zero or
  negative volume, or bars in closures.
* The export starts on the evening of Monday 2026-11-16 at 17:00 CT. That is the opening of Globex trade date
  2026-11-17, kept as delivered.
* **MES `ABNORMAL_VOLUME`:** one bar, 2026-11-20 20:59 UTC (14:59 CT), volume 34,412, more than 50× the
  median. It is the last minute before the 15:00 CT settlement on the third Friday of November (monthly
  options expiry). It is kept, not altered.
* **MNQ, observation only:** 2026-11-23 14:27–14:30 UTC has four identical bars at 26,878.50 with volume,
  ending exactly at the 08:30 CT opening of RTH. This looks like trading locked at a price limit, but it is
  not verified. No check flags it, and the bars are kept.

**Readiness:** each use is assessed separately; there is no single green status.

| Dimension | MES | MNQ |
|---|---|---|
| 1. File integrity | PASS | PASS |
| 2. Structural validity | PASS | PASS |
| 3. OHLCV validity | PASS_WITH_LIMITATIONS (1 abnormal-volume bar) | PASS |
| 4. Timestamp and timezone | PASS_WITH_LIMITATIONS (timezone inferred empirically) | PASS_WITH_LIMITATIONS |
| 5. Session and calendar | PASS_WITH_LIMITATIONS (calendar has no verified holidays; none fall in the range) | PASS_WITH_LIMITATIONS |
| 6. Completeness | PASS | PASS |
| 7. Cross-source validation | NOT_PERFORMED (no independent source) | NOT_PERFORMED |
| 8. Overall research readiness | **NOT_READY_FOR_STRATEGY_RESEARCH** | **NOT_READY_FOR_STRATEGY_RESEARCH** |

* **Overall:** six trade dates are far below the planning minimums (250 for walk-forward, 370 with a held-out
  OOS segment). The data supports indicator checks and mechanical backtest or Monte Carlo runs only. Nothing
  computed on it is statistical evidence.
* **Cross-source validation was not performed** and nothing was substituted for it.

## 5. Code changes (no new ingestion path)

* `providers.py`, NinjaTrader adapter: strict line-based reading instead of `read_csv`.
  - Every line is a data row, a blank line, a `#` comment or a first-line header.
  - A wrong field count or a malformed timestamp is REJECTED with its line number. Before, a 7-field line
    stopped the whole import with a parser exception.
  - Encoding, BOM, line endings and counts are recorded as `parse_stats`.
  - An undecodable file is refused.
* `store.py`:
  - `provenance` is recorded in the manifest and validation report. It is bounded and JSON-only, and keys that
    look like credentials are refused. It is not part of the idempotency identity.
  - `rejected.csv` lists every rejected source row (the quarantine). It is hash-protected like every other file.
  - `SOURCE_FILE_CHANGED`: a file with a known name but a new SHA-256 creates a new version and is reported.
  - Each source records its first and last original timestamp.
* `quality.py`: `clock_alignment` diagnostic and the `CLOCK_MISALIGNMENT_SUSPECTED` warning. It never shifts
  data.
* `readiness.py`: the eight separate dimensions above.
* `run_data.py`: `ingest --provenance FILE.json`.
* `schema.py`: normalization version `2026-11-25.2`.
* Tests: `tests/test_sprint12_1_ninjatrader_import.py`, 24 tests.
  - 20 are unit tests on fixtures.
  - 4 are real-data integration tests. They check the exact SHA-256 first and SKIP when the inbox is absent.

The existing Sprint 12 checks, status gates and thresholds are unchanged.

## 6. Tests

**The full suite is NOT green on this Windows machine.** Every failure below also fails, identically, on the
unchanged Sprint 12 commit `c0b44ec`. That was checked by re-running each failing test on an exact copy of
`c0b44ec`, with the same Windows line endings and the same Python environments. None of these failures
involves the data layer. They are pre-existing and are NOT fixed in this sprint; they need their own change.

| | Python 3.12.10 | Python 3.15.0 |
|---|---|---|
| Libraries | pandas 3.1.1 / numpy 2.5.5 | pandas 3.1.1 / numpy 2.5.5 |
| Full suite | 1524 passed, 12 failed, 7 skipped (19 min 00 s) | 1518 passed, 14 failed, 4 errors, 7 skipped (20 min 18 s) |
| Sprint 12.1 tests (24, including 4 on the real files) | 24 passed | 24 passed |
| Sprint 12 data tests (30) | 30 passed | 30 passed |
| Failures caused by Sprint 12.1 | 0 | 0 |

* The 7 skips are the Playwright end-to-end scenarios (node, Playwright and a built frontend are not set
  up on this machine).
* The frontend was not changed, so the vitest suite was not run.

**Baseline failures (pre-existing at `c0b44ec`):**

| Test | 3.12 | 3.15 | Probable cause |
|---|---|---|---|
| `test_artifact_security.py`: `test_random_forest_round_trip` (failure); `test_signed_model_round_trip`, `test_attacker_replacing_both_files_cannot_execute_code`, `test_allow_list_blocks_gadgets_even_in_a_correctly_signed_file`, `test_wrong_or_missing_key` (setup errors) | pass | FAIL / ERROR | The model-artifact unpickler allow-list lacks `numpy._core.numeric._frombuffer`, which this numpy uses under Python 3.15. |
| `test_regime_validation.py::test_ml_fit_is_reproducible_and_tamper_evident` | pass | FAIL | Same allow-list cause. |
| `test_marketdata_units.py::test_secret_file_rules` | FAIL | FAIL | Expects POSIX mode 0600; Windows reports 0666. |
| `test_paper_audit2.py::test_n11_state_directory_and_files_are_owner_only` | FAIL | FAIL | Expects POSIX mode 0700; Windows reports 0777. |
| `test_paper_recovery.py::test_state_file_permissions` | FAIL | FAIL | Same POSIX-permission cause. |
| `test_marketdata_cli.py::test_init_secret_and_check`, `test_monitor_with_mock_bridge_runs_read_only_and_writes_status` | FAIL | FAIL | A Windows path written into a TOML basic string: `\U...` is parsed as an escape ("Invalid hex value"). |
| `test_terminal.py::test_shadow_without_bridge_setup_fails_visibly_without_fallback` | FAIL | FAIL | Same TOML-path cause: a different error message than the one expected. |
| `test_shadow_analytics.py::test_journal_patterns_stay_inside_the_project` | FAIL | FAIL | `/etc/passwd` is not refused on Windows (path semantics differ). |
| `test_sprint11_registry.py::test_a_modified_registered_version_is_refused` | FAIL | FAIL | The checkout is CRLF (`core.autocrlf=true`), so the test's `\n` → `\r\n` replacement produces `\r\r\n` and changes the content hash. |
| `test_sprint11_registry.py::test_trend_pullback_is_byte_identical_to_sprint10` (MES/MNQ × default/relaxed) | FAIL | FAIL | **UNRESOLVED.** The trade/equity fingerprints differ from the Sprint 10 golden values. Probably the Windows checkout or newer library versions, but this is NOT established. The VWAP Trend Pullback results on this machine are not proven identical to Sprint 10 until it is investigated. |

Totals: 12 failures on 3.12; 12 + 6 = 18 (14 failed + 4 errors) on 3.15.

## 7. Known limitations and next steps

1. The timezone is inferred, not configured. Find out why NinjaTrader exports UTC (or set its export
   timezone explicitly), and check every new export the same way.
2. Six trade dates are a pipeline test, not a research sample. Export at least 18 months per contract, and
   preferably all history since 2019, rolling through the quarterly contracts.
3. There is no cross-source validation. Obtain a second, independent source (e.g. Databento) for sample
   months.
4. CME holidays are unverified in the calendar. Thanksgiving (2026-11-26) and the following Friday early
   close fall just after this range and will matter for the next export.
5. The raw inbox is outside the repository. Keep a backup: the dataset folders hold the only other copy.
