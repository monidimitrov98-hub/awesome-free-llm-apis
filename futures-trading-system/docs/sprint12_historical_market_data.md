# Sprint 12: Historical market-data infrastructure and quant validation

Data quality and research infrastructure only. No real historical market data was obtained in this sprint:
no provider was contacted, no account was created and nothing was purchased. Every test uses deterministic
SYNTHETIC fixtures, which exercise the code and say nothing about markets. No dataset in this repository is
VALIDATED real data, and no strategy result from real data exists.

## 1. Provider research

### 1.1 How the research was done, and its limits

Accessed on 2026-11-25. The build environment's network policy blocked every provider website:

* direct HTTPS fetches got a `CONNECT tunnel failed, response 403` from the environment proxy;
* the page-fetch helper got DNS failures for databento.com, rithmic.com, barchart.com, firstratedata.com,
  cqg.com and kibot.com.

Only web **search** worked. The findings below therefore come from search-engine excerpts of the providers'
own pages, where the excerpt names the provider's page, and from secondary sources. Each figure is labelled:

* **[P]** – a search excerpt of the provider's own page or documentation. It was not opened, so the excerpt
  may be outdated.
* **[S]** – a secondary source (reviews, forums, third-party guides).
* **UNKNOWN** – not independently verifiable here.

Nothing below is a verified current price or a licence interpretation. Confirm everything on the providers'
pages before any decision. Allowing these hosts in the environment's network settings would let a later
session read the pages directly.

### 1.2 Comparison matrix

| | Databento (GLBX.MDP3) | NinjaTrader historical data / Kinetick | Rithmic | CQG Data Factory | Barchart OnDemand | FirstRate Data | Kibot |
|---|---|---|---|---|---|---|---|
| MES / MNQ | Yes: all CME/CBOT/NYMEX/COMEX futures [P] | Yes (CME futures) [S] | Yes (CME) [P] | Yes (futures by commodity) [P-brochure] | Futures supported [P] | MES and MNQ pages exist [P] | MES and MNQ from 2019-05-05 [P] |
| History | From 2010 for the dataset [P]. The Micros only exist since May 2019 [S] | Minute data from 2006, tick data about 1 year (NT brokerage servers) [S]. Kinetick: at least 2 years of minute and 180 days of tick (2023 support reply) [S] | Tick data since Dec 2011 [P] | Futures from 1998 [P-brochure] | UNKNOWN (old free-tier notes ~1 month of minute data [S]) | "15 years" page title for MNQ [P]; the Micro contracts start 2019 | From 2019-05 for MES/MNQ [P] |
| Granularity | Trades, MBP/MBO, OHLCV 1s/1m/1h/1d, definitions [P/S] | Tick, minute, daily [S] | Tick and MBO history [P] | Tick (time & sales), 1-minute, daily [P-brochure] | Tick, minute, daily [P] | 1m, 5m, 30m, 1h, 1d [P] | 1-minute [P] |
| Individual contracts | Yes, plus continuous symbology (calendar / OI / volume rules, unadjusted prices) [P] | Yes (per contract, e.g. `MES 03-25`) [S] | Yes | Yes, all tradeable contracts in the ordered window [P] | UNKNOWN | Individual and continuous [P] | **Continuous only** [P] |
| Bar construction | OHLCV built from trades; `ts_event` = interval START; **no bar when nothing traded** [P] | Vendor bars; export stamped at bar END in PC time [S + platform behaviour] | Tick-level | Vendor bars | Vendor bars | Vendor bars; method UNKNOWN | Vendor bars; method UNKNOWN |
| Timestamps | Exchange `ts_event` (ns, UTC) + receive time [P] | Platform timezone on export | Exchange time | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Pricing | Usage-based "from $0.50/GB"; subscription tiers quoted at $199 / $1,750 / $4,500 per month ($179 grandfathered); $125 free credit for new accounts [P/S, conflicting dates] | Free EOD tier is daily data only. Minute/tick history needs a subscription or brokerage account; prices UNKNOWN [S] | Through an FCM/broker; fees vary [S] | Per month per commodity: brochures list $10 (1-minute) and $12 (tick) per month, plus a 10 % surcharge for continuation data [P-brochure, undated] | Usage-based, sales-led; a $500/month figure is unverified [S] | One-time bundles; updates $59.95/month; bundle price UNKNOWN [P] | $520 one-time (83 continuous symbols) [P] |
| API / Python | REST + official Python client; cost estimate before download [P/S] | Export from the platform (no data API) | R\|API / Protocol API (C++, protobuf; community Python) [P/S] | File delivery [P] | REST API [P] | File download | File download |
| Licensing (personal research) | The vendor says historical data (older than 24 h) needs no licence for users who do not redistribute; CME charges for historical redistribution [P/S, interpretation **UNKNOWN**] | Platform terms; UNKNOWN | Broker terms; UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Rate limits / retention | UNKNOWN (billed per byte) | Download limits during RTH (~8 days of tick) [S] | 40 GB per user per week [P] | n/a (orders) | Free tier historically ~150 queries/day [S, old] | n/a | n/a |

Sources (all accessed 2026-11-25; "excerpt" = read through a search result, not opened):

* Databento:
  - https://databento.com/datasets/GLBX.MDP3 (excerpt)
  - https://databento.com/docs/schemas-and-data-formats/ohlcv (excerpt: interval-start `ts_event`, no record when no trade)
  - https://databento.com/docs/standards-and-conventions/symbology (excerpt: `[ROOT].[c|n|v].[rank]`)
  - https://databento.com/docs/examples/futures/futures-introduction (excerpt: continuous prices unadjusted)
  - https://databento.com/docs/quickstart (excerpt: $125 credit, usage-based or flat-rate)
  - https://databento.com/blog/updates-to-subscription-pricing (excerpt)
  - https://databento.com/blog/introduction-market-data-licensing (excerpt)
  - https://api.databento.com/static/licensing/cme/cme-market-data-fee-list.pdf (excerpt)
  - https://www.quantvps.com/blog/databento-review [S]
* NinjaTrader / Kinetick:
  - https://support.ninjatrader.com/s/article/Connecting-To-Your-Kinetick-Data-Connection [P excerpt]
  - https://forum.ninjatrader.com/forum/ninjatrader-8/platform-technical-support-aa/1235434-how-far-back-does-kinetick-data-go [S]
  - https://forum.ninjatrader.com/forum/ninjatrader-8/platform-technical-support-aa/1240658-historical-data [S]
* Rithmic:
  - https://www.rithmic.com/apis (excerpt)
  - https://www.rithmic.com/platforms/exchanges (excerpt)
* CQG:
  - https://www.cqgdatafactory.com/docs/CQGDataFactory_FAQ.pdf (excerpt)
  - https://www.ice.com/publicdocs/data/Data_Factory.pdf (excerpt, undated brochure)
* Barchart:
  - https://www.barchart.com/ondemand/api/getHistory (excerpt)
  - https://www.barchart.com/solutions/blog/market-data-apis-for-price-data (excerpt)
  - https://fxmacrodata.com/articles/fxmacrodata-vs-barchart-ondemand [S]
* FirstRate Data:
  - https://firstratedata.com/i/futures/MNQ (excerpt)
  - https://firstratedata.com/b/29/futures-25 (excerpt)
  - https://firstratedata.com/free-intraday-data (excerpt)
* Kibot: https://www.kibot.com/historical-data/continuous-futures-contracts-1-minute-intraday-data.html (excerpt)
* Other vendors seen but not assessed: FinzData, MarketParquet, historicalfuturesdata.com [S].

### 1.3 Recommendations (decisions remain yours)

1. **Best overall: Databento GLBX.MDP3.**
   - It delivers raw CME MDP 3.0 data, so the chain of custody is direct from the exchange feed.
   - Every individual contract is included, with definitions (expirations) and exchange `ts_event` timestamps.
   - Trades are available, so bars can be rebuilt and checked.
   - Pricing is usage-based with a pre-download cost estimate, and there is an official Python client.
   - The `databento_ohlcv_csv` adapter already parses its OHLCV CSV.
   - Caveats: an account is required (an external commitment you decide). Prices and licence terms must be
     read on the live pages. MES/MNQ only exist since May 2019.
2. **Lower-cost alternative: data you may already be entitled to via NinjaTrader.**
   - The NinjaTrader historical servers reportedly hold minute data back to 2006 per contract [S]. They are
     exported per contract with the existing NT8 installation and ingested with `ninjatrader_export`.
   - Its cost depends on your NinjaTrader account (UNKNOWN).
   - **FirstRate Data** (one-time purchase, individual contracts [P]) is the paid low-cost alternative, if a
     sample confirms its bar construction and timestamps.
   - Kibot is not recommended as a primary source: it sells continuous series only, and contracts must stay
     the primary source of truth.
3. **Independent cross-validation: yes, justified.** Pairing (1) with a NinjaTrader export of the same
   contracts and sample months gives two independent vendor chains at no extra cost. CQG Data Factory's
   per-month pricing is suited to buying a few specific months for checks. `run_data.py cross-validate`
   implements the comparison.

## 2. Architecture

```
provider files (downloaded with the provider's own tools)
   │  src/histdata/providers.py   adapters: generic_csv, databento_ohlcv_csv, ninjatrader_export
   ▼                              (parse only: no network, no credentials)
raw records ──► src/histdata/quality.py   normalize (contract identity, duplicates, order, grid) + validate
   ▼
src/histdata/store.py   immutable versioned datasets: raw copies + bars.csv.gz + validation + manifest,
   │                    hash-chained registry, staging / reservation / atomic rename, recovery
   ├─► src/histdata/rollover.py    continuous series (derived dataset, roll table, adjusted = flagged)
   ├─► src/histdata/crossval.py    cross-source discrepancy report → CROSS_SOURCE_VALIDATED
   ├─► src/histdata/readiness.py   readiness per use
   └─► src/histdata/market.py      dataset → existing pipeline → BacktestEngine / research engine
src/histdata/calendar.py           CME equity calendar (America/Chicago, versioned TOML)
src/research/plans.py              frozen train / validation / OOS plans; OOS reservation
run_data.py                        CLI; run_research.py --dataset / --plan
```

* **Provider neutrality:** strategies and the backtester see only the existing `MarketData`. A provider
  change is a new adapter and new datasets, nothing else. An adapter cannot write a dataset; the store
  validates everything an adapter returns.
* **Safety boundary:** the data layer imports nothing from the paper runtime, the backtest engine, risk,
  NewsGuard or the NinjaTrader bridge, and has no network modules (tested). The NinjaTrader bridge stays
  read-only and untouched.

## 3. Data schema

Normalized bars, one row per bar of one individual contract:

| Column | Meaning |
|---|---|
| `ts_utc` | Bar open, UTC, on the resolution grid |
| `root` | `MES` / `MNQ` |
| `contract` | Individual contract, e.g. `MESH25`. Never a continuous symbol in raw data. |
| `open`, `high`, `low`, `close` | As delivered |
| `volume` | Contracts traded in the bar |
| `source_ts` | Original timestamp text |
| `source_file` | Index into the manifest's raw-file list |

* Continuous datasets add `segment`, and `adjustment` only when explicitly adjusted.
* Files are a deterministic gzip CSV (mtime 0, fixed format), so the same input gives byte-identical files.
* The content hash (`bars_content_sha256`) is independent of encoding and of pandas' internal timestamp
  resolution.

## 4. Timestamps and sessions

* **Storage:** UTC throughout. Each raw file's original timestamps, source timezone and labelling are kept
  in the manifest. Adapters require an explicit source timezone; none is ever assumed.
* **Close-stamped sources** (NinjaTrader exports) are shifted back one bar to the bar open.
* **Integer Databento timestamps** must be 19-digit nanosecond epochs. A wrong unit is refused, not
  converted.
* **Exchange calendar** (`config/calendars/cme_equity_index.toml`, versioned): America/Chicago.
  - A Globex trade date `D` runs `D-1 17:00` to `D 16:00` CT.
  - Closed Friday 16:00 to Sunday 17:00, and during the daily halt 16:00–17:00 CT (Mon–Thu).
  - RTH is 08:30–15:00 CT, a research convention. A test shows it matches the existing RTH session
    (09:30–16:00 New York) minute for minute across both 2025 DST changes.
  - All conversions use the tz database; no fixed offsets.
* **Detection of clock errors:** a bar during a scheduled closure is an ERROR (`BAR_WHILE_MARKET_CLOSED`).
  A wrongly declared source timezone shifts bars into the daily halt or the weekend, so it is caught.
* **Holidays:** the calendar contains **no verified holidays**. CME publishes holiday hours shortly before
  each holiday, and the pages were unreachable.
  - Rule-based US holiday dates are candidates only: missing minutes on them are reported as
    `HOLIDAY_CANDIDATE_UNVERIFIED`.
  - VERIFIED entries (with a source URL) turn the closures into expected gaps. Bars after a verified early
    close are errors (tested).
* **Expiry:** quarterly contracts terminate at 08:30 CT on the third Friday. Bars at or after termination
  are errors. Holidays on expiry days are not modelled.

## 5. Contracts and rollover

* Individual contracts are the source of truth. Datasets holding several contracts are never merged for
  research: one contract must be selected, or a continuous dataset built.
* Continuous series are DERIVED datasets with lineage (source refs and manifest hashes) and a roll table
  (`rolls.csv`):
  - Rolls happen only at a trade-date boundary, so one contract is used per trade date and every price and
    timestamp is unchanged.
  - **`calendar` rule:** roll N trade dates before the front contract's expiry. Only the published expiry
    schedule is used.
  - **`volume` rule:** roll at the start of trade date `d` once the next contract out-traded the front on
    the previous `confirm_days` completed trade dates. The calendar rule remains a hard limit.
  - **No look-ahead:** a test checks that cutting the data after a roll changes no earlier decision.
* **Roll gaps** (the price difference at the last common minute before each roll) are reported.
* **Adjustment:** `difference` and `ratio` back-adjustment exist only as flagged ADJUSTED datasets. The
  research engine and the market-data bridge **refuse** them, because fills, ticks and P&L must use real
  contract prices.
* **Contract specifications:** tick size and point value come from `config/settings.toml`
  (MES 0.25 / $5, MNQ 0.25 / $2), checked per bar for the tick grid.

## 6. Validation methodology and statuses

* **Errors** (any error → `VALIDATION_FAILED`; the rows are excluded and counted, never repaired):
  - missing values, off-grid timestamps
  - unknown or continuous symbols
  - conflicting duplicates (all versions excluded)
  - prices ≤ 0, negative volume, broken OHLC, off-tick prices
  - bars while the market is closed, bars after expiry
  - unparseable source rows
* **Warnings:** exact duplicates (one kept), out-of-order input (sorted), zero volume, abnormal volume
  (> 50× the contract's median bar volume), price jumps > 3 % within 5 bars, and missing data.
* **Gap classification:**

  | Class | Meaning |
  |---|---|
  | `EXPECTED_CLOSURE_WEEKEND` | weekend closure |
  | `SCHEDULED_BREAK` | daily halt |
  | `EXPECTED_HOLIDAY` | verified calendar entries only |
  | `HOLIDAY_CANDIDATE_UNVERIFIED` | missing minutes on a rule-based holiday candidate date |
  | `NO_TRADE_INTERVAL` | trade-built bars only: ≤ 15 ETH bars or ≤ 2 RTH bars |
  | `MISSING_DATA_RTH` / `MISSING_DATA_ETH` | missing vendor data |
* **Statuses and their exact evidence:**

  | Status | Evidence required |
  |---|---|
  | `UNVERIFIED` | Defined for completeness; versions are only registered after validation. |
  | `INGESTED` | Normalized and validated with no ERROR, but the coverage gate fails: exploration only, refused by research runs. |
  | `VALIDATION_FAILED` | At least one ERROR, or a human downgrade (`run_data.py downgrade`, recorded with a reason). |
  | `VALIDATED` | No ERROR. On active trade dates (contract volume ≥ 10 % of its peak daily volume), `MISSING_DATA_RTH` ≤ 0.5 % of open RTH minutes, and no single missing RTH gap > 30 minutes. Computed, never set by hand. |
  | `CROSS_SOURCE_VALIDATED` | Both datasets VALIDATED, unadjusted, per-contract and not synthetic. Different adapters and no shared source file. At least 5 common trade dates. Best timestamp alignment = 0 within ±120 minutes. RTH bars present in both ≥ 99 %. OHLC within 1 tick on ≥ 99.5 % of matched bars. Volume is reported, not decisive. The report hash is recorded in the registry. |

  Ingestion success alone never makes a dataset more than its gate allows.
* **Synthetic data:** a dataset is marked SYNTHETIC by its source file name or a comment marker. It can
  never be cross-source validated, revealed out of sample or used for parameter selection, and it is never
  "ready" for any evidential use.

## 7. Dataset versioning and immutability

* **Layout and identity:** `data/datasets/<id>/<vN>/` holds raw byte copies, `bars.csv.gz`,
  `validation.json`, `gaps.csv` and `manifest.json`. The manifest records:
  - id, version, provider adapter and parameters
  - source files (name, SHA-256, size, rows, rejected rows, timestamp semantics, source timezone)
  - roots, contracts, date range, resolution
  - calendar identity, normalization and schema versions, ingestion time
  - validation summary, file hashes, content hash, lineage
  - its own self-hash
* **Registry:** `registry.jsonl` is append-only and hash-chained (HMAC with `FTS_JOURNAL_KEY`).
* **Write path:** a version is written to staging, reserved in the registry, then atomically renamed, and
  its files are made read-only.
* **Never overwritten:** the identical source returns the existing version. New or corrected data creates a
  new version.
  - Incremental updates carry the base files forward and can never drop a bar.
  - Changed bars are refused unless `--allow-corrections` is passed; the corrections are then recorded.
* **Every load verifies** the registry record, the manifest self-hash, the manifest against the registry,
  every file hash, and path safety. Tests cover:
  - corruption, missing files and tampered manifests
  - a substituted manifest, path traversal and registry tampering
  - double reservation, idempotent re-ingestion and an interrupted ingestion (`recover`)

## 8. Research integration and OOS protection

* **Dataset sources:** `run_research.py ... --dataset <id>@v<N>[#CONTRACT]` (VALIDATED or better).
  - Every manifest records the dataset identity, and the experiment registry records its manifest hash.
  - Sweeps and fold selection on SYNTHETIC datasets are refused.
  - The legacy `--csv` path is unchanged (Sprint 11 behaviour) and does not satisfy readiness or plans.
* **Frozen plans** (`config/research/plans`, `run_research.py plan-check | plan-register | plans`) pin:
  - the datasets, by manifest hash
  - chronological train / validation / OOS segments with purge and embargo gaps
  - the strategy versions under test
  - the backtest configuration (costs, slippage), by hash
* **Once a plan is registered:**
  - its OOS segment is reserved for research of any kind (the reservation lives in the append-only index,
    so deleting the plan file does not lift it);
  - the segment can be revealed only through `reveal-oos --plan`, once, with the frozen dataset, dates and
    strategy versions;
  - the reveal is recorded in the validation ledger with the plan hash.
* **Unchanged safeguards:** the Sprint 11 one-time reveal, replay, revealed-date and multiple-testing rules
  are not weakened.
* **Walk-forward and purged CV** accept `--start/--end` so that folds can stay inside a plan's train and
  validation dates.

## 9. Research readiness

`run_data.py readiness <ref>` (and the terminal's Quant Research → Datasets view) gives each use one of
READY, READY_WITH_LIMITATIONS or NOT_READY, with reasons. The uses are: indicators, backtesting,
walk-forward (≥ 250 active trade dates), OOS validation (≥ 370 trade dates; cross-source validation
recommended) and Monte Carlo.

The report also lists: instruments, contracts, dates, resolution, session coverage (RTH/ETH), missing
periods, validation status and history, the gate values and known biases. The fixed numbers are planning
minimums for this project, not proofs of adequacy.

Known biases:

* bar data only, so no quotes are observed;
* intrabar order is unknown;
* provider bar construction shapes the gaps;
* roll effects on indicators that carry across sessions;
* holidays are not verified.

## 10. First real-data research plan (proposal; nothing has been run)

Draft: `config/research/plans/real-data-v1.toml.draft`. It cannot be registered before real data exists.

1. **Instruments:** MES and MNQ, evaluated separately. They are never pooled for inference.
2. **Period:** all available history since the Micros launched (May 2019). Proposed segments:

   | Segment | Dates |
   |---|---|
   | train | 2021-01-04 – 2023-06-30 |
   | validation | 2023-07-17 – 2024-06-28 |
   | OOS | 2024-07-15 – 2025-06-27 |

   Purge and embargo are 10 trade dates each. 2019–2020 is optional extra training data (a distinct COVID
   regime). The split must be decided and registered before any real-data result is seen.
3. **Resolution:** 1-minute bars from the provider. Signal bars (5 minutes) are resampled by the existing
   pipeline.
4. **Sessions:**
   - VWAP Mean Reversion V1 uses its versioned `rth` scope.
   - VWAP Trend Pullback uses its existing configuration.
   - The calendar version is recorded.
5. **Contracts:** an unadjusted continuous series per root.
   - `volume` rule with 1 confirmation day; the calendar hard limit is 5 trade dates.
   - Strategies are flat every day, so no position crosses a roll.
   - Roll sessions are listed in the report.
6. **Costs:** `config/backtest.toml` currently holds **placeholder** commissions ($1.00 per side). You must
   set your real all-in costs before the plan is frozen; the plan pins the file's hash.
   - Slippage: 1 tick for market and stop orders.
   - Cost sensitivity by re-runs: commission ×1.5 and ×2, and +1 / +2 ticks.
7. **Strategy versions:** the existing registered versions, unchanged. They are VWAP Trend Pullback V0 and
   the six Mean Reversion V1 variants: C1, C2, C3 and C4 with the swing stop, and C2 with the ATR and band
   stops.
8. **Methodology:**
   - Train: exploration only, inside the declared bounds; every attempt is recorded.
   - Validation: the frozen versions are evaluated, plus walk-forward over train and validation.
   - Exactly ONE configuration per instrument goes forward to the one-time OOS reveal. It is chosen on
     validation evidence, before the reveal. The unchanged Sprint 11 ledger rule refuses a second
     configuration on revealed dates.
   - Monte Carlo is run on the validation and OOS runs.
   - Human promotion decisions follow.
9. **Sample size:** below 30 trades the label is INSUFFICIENT EVIDENCE. Sprint 11's power calculation
   reports the trade dates needed at the observed effect. No result is interpreted without it.
10. **Statistics:**
    - daily P&L as the unit, one-sided t-test, day-bootstrap intervals
    - Holm (and Benjamini-Hochberg) across every attempt on the instrument
    - the OOS family is the set of reveals
    - evidence labels as in Sprint 11
    - no ranking by net profit
11. **Data prerequisites, in order:**
    1. obtain the data (your decision);
    2. ingest per contract;
    3. reach VALIDATED;
    4. cross-validate sample months (e.g. one month per quarter) against an independent source;
    5. build the continuous series;
    6. check readiness;
    7. register the plan.

## 11. Test results

Full suite on the final code, run on a snapshot identical to the committed source, configuration and
tests:

| Suite | Result |
|---|---|
| Python 3.13.18 / pandas 3.0.5 / numpy 2.5.3 | 1519 passed, 0 failed, 0 skipped (24 min 24 s) |
| Python 3.12.3 / pandas 2.2.3 / numpy 2.1.3 | 1519 passed, 0 failed, 0 skipped (28 min 43 s) |
| Frontend unit tests (vitest) | 48 passed |
| TypeScript typecheck | clean |
| Production build | passed |
| Playwright | 7 scenarios passed (inside the Python suite), including Quant Research → Datasets |

The 30 new Sprint 12 tests are in `tests/test_sprint12_histdata.py`. Two defects were found only because
the suite runs in both pandas versions, and both are fixed: a content hash that depended on pandas'
timestamp resolution, and a coverage gate that never matched trade dates under pandas 2.

## 12. Known limitations

* **No real data has been obtained, ingested or validated.** Readiness of real data is therefore unknown.
* **Provider findings are unverified:** the pages themselves could not be opened (section 1.1).
* **Calendar:** holidays are unverified. The daily halt hours come from secondary sources. Pre-2021
  session rules (e.g. any former intraday maintenance halt) are not modelled.
* **Gap classification thresholds** (`NO_TRADE_INTERVAL` limits, the coverage gate) are project choices,
  stated in every report. Back months trade thinly, so the gate is applied to active trade dates only.
* **Integrity:** the registry and stores are tamper-evident, not tamper-proof (as in Sprint 11). Keep
  backups.
* **Synthetic detection** relies on the file-name or comment marker.

## 13. Decisions requiring your approval

1. Whether to obtain data, and from which provider. This needs an account or a purchase; nothing has been
   done.
2. The licence interpretation for personal research use (UNKNOWN; read the provider and CME terms).
3. Your real all-in commission and fee costs, to replace the placeholders.
4. The train / validation / OOS dates. They must be registered before any real-data result is seen.
5. Whether and how to verify CME holiday schedules and add them as VERIFIED calendar entries.
6. Whether to allow the provider documentation hosts in the environment's network settings, so a later
   session can verify the figures above directly.
7. Whether a frozen plan should be allowed to reveal several pre-frozen configurations on the same OOS
   segment (counted in the OOS multiple-testing family). It is NOT implemented: the existing one-reveal rule
   was kept so that no OOS safeguard is weakened.
