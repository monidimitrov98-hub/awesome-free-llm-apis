# Futures Trading System: Sprint 1 Market Data Engine

A personal Python **research and paper-trading** project for intraday **MES** and **MNQ** futures.

> **No live trading.** This code has no order-execution component. It does not claim that any
> strategy is profitable. The sample data is **synthetic** and is not real market prices.

Sprint 1 covers the market data engine: loading CSV files, validating them, converting
timestamps to UTC, identifying contracts, resampling and computing core indicators.

## Project layout

```
futures-trading-system/
├── config/settings.toml        # every setting: columns, timezones, sessions, contracts, indicator periods
├── data/samples/               # SYNTHETIC sample CSVs (clearly marked) + README
├── run_pipeline.py             # command-line entry point
├── scripts/generate_synthetic_data.py
├── src/
│   ├── config.py               # TOML -> validated dataclasses
│   ├── pipeline.py             # load -> validate -> resample -> indicators
│   ├── data/
│   │   ├── loader.py           # CSV loader with column mapping
│   │   ├── timestamps.py       # normalization to UTC (DST-aware)
│   │   ├── contracts.py        # contract symbol identification (MESH25, MNQ 06-25, ...)
│   │   ├── sessions.py         # session / trade-date assignment
│   │   ├── validation.py       # OHLCV checks, gap detection
│   │   ├── resample.py         # 1m / 5m / 15m bars
│   │   ├── report.py           # data quality report
│   │   └── errors.py
│   ├── indicators/             # ema, rsi (Wilder), atr (Wilder), session vwap, relative volume
│   ├── strategy/ ai/ risk/ backtest/ dashboard/   # placeholders for later sprints
└── tests/                      # pytest suite (one file per module/indicator)
```

## Installation on Windows

1. Install **Python 3.12** from <https://www.python.org/downloads/windows/>. During setup, tick
   *"Add python.exe to PATH"*.
2. Open **PowerShell** and go to the project folder:
   ```powershell
   cd C:\path\to\awesome-free-llm-apis\futures-trading-system
   ```
3. Create and activate a virtual environment:
   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   If PowerShell refuses to run the script, run this once:
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
   (in `cmd.exe`, use `.venv\Scripts\activate.bat` instead).
4. Install the dependencies:
   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
   `tzdata` is included because Windows has no built-in IANA timezone database.

## Running

Run the tests:
```powershell
python -m pytest
```

Run the pipeline on the synthetic sample:
```powershell
python run_pipeline.py --csv data\samples\SYNTHETIC_MES_1m.csv
python run_pipeline.py --csv data\samples\SYNTHETIC_MNQ_1m.csv --timeframe 5min --tail 10
python run_pipeline.py --csv data\samples\SYNTHETIC_MES_1m.csv --output-dir output
```

The command prints the data quality report, the identified contract and the last bars of
each timeframe with these indicator columns:
`ema_9, ema_21, rsi_14, atr_14, vwap, rvol`, plus `session_date`, `n_source_bars` and `is_complete`.

Exit codes: `0` ok, `1` configuration or data error, `2` validation errors with `on_error = "raise"`.

From Python:
```python
from src.config import load_config
from src.pipeline import build_market_data

md = build_market_data("data/samples/SYNTHETIC_MES_1m.csv", load_config())
print(md.report.summary())
bars_5m = md.frames["5min"]
```

## Loading a real MES/MNQ CSV

1. **Export 1-minute bars** from your platform or vendor. You need a timestamp column and
   open/high/low/close/volume columns. A symbol column is optional.
2. **Copy the config** so the defaults stay untouched:
   `copy config\settings.toml config\settings.local.toml` (`*.local.toml` is git-ignored).
3. **Map your columns** under `[data.columns]`. Matching ignores case. For example, for a
   file with the header `Date Time,Open,High,Low,Last,Volume`:
   ```toml
   [data.columns]
   timestamp = "Date Time"
   open = "Open"
   high = "High"
   low = "Low"
   close = "Last"
   volume = "Volume"
   ```
4. **Set the timezone of the timestamps.** This is the most common source of errors.
   - Timestamps without an offset: set `source_timezone` to the zone your platform exported
     in (e.g. `"America/Chicago"` for CME exchange time, `"America/New_York"`, or `"UTC"`).
   - Timestamps with an offset (`...Z`, `...-05:00`) are converted automatically.
   - Epoch numbers: set `timestamp_unit = "s"` (or `"ms"`).
   - A custom text format: set `timestamp_format`, e.g. `"%Y%m%d %H%M%S"`.
   - Local exports that cross a DST change can contain an hour that occurs twice. With the
     default `ambiguous_time = "raise"` the loader stops and explains why. Use `"infer"` for
     ordered data, or export in UTC.
5. **Check the bar-time convention.** Internally every bar is stamped at its **open**. Some
   platforms (for example NinjaTrader exports) stamp the bar's **close**. For those, set
   `timestamp_label = "close"`. Otherwise every bar is shifted by one period, and
   session/VWAP boundaries will be off by one bar.
6. **Contract identification**, in order of priority: the `symbol` column (`symbol_column`),
   then the file name (e.g. `MESM25_1m.csv`, `MES 06-25.Last.txt`), then `default_symbol`.
   A file containing several contracts (around a roll) is rejected unless you set
   `symbol_filter = "MESM25"`.
7. **Choose the error policy.** `on_error = "raise"` (the default) stops when there are
   invalid rows. `"drop"` removes them and reports the count. Gaps are always reported and
   never filled.
8. Run:
   ```powershell
   python run_pipeline.py --csv C:\data\MESM25_1m.csv --config config\settings.local.toml
   ```
   Read the report before using the numbers.

## Methodology (what exactly is computed)

| Item | Definition |
|---|---|
| Timestamps | Converted to tz-aware UTC. Naive values are localized with DST rules first. Unparseable or ambiguous values are reported, never guessed. |
| Sessions | Globex runs 18:00 to 17:00 New York time (end exclusive). Each bar's `session_date` is the trade date, so Sunday 18:00 belongs to Monday. RTH 09:30–16:00 is also configured. Each session's start and end are resolved to exact UTC instants, so DST is handled per session. A boundary that falls in the skipped spring hour moves forward. One that falls in the repeated autumn hour uses its first occurrence. |
| Resampling | Bars are left-labelled: the 09:30 5m bar covers 09:30:00–09:34:59 and is **known only at 09:35** (`available_at`). Empty bins are dropped, never filled, and no valid bar is removed. `is_complete` is `True` only if the bin's source timestamps are exactly the expected base grid (no missing and no off-grid rows). |
| Session completeness | Expected bars per session come from that session's real elapsed length: 22h, 23h or 24h, not a fixed 23h. Sessions with fewer on-grid bars are reported as `INCOMPLETE_SESSION` (e.g. `500/540 bars`). |
| EMA | `alpha = 2/(n+1)`, seeded with the SMA of the first n closes. NaN before that. |
| RSI | Wilder: the first averages are simple means of n changes, then `(prev*(n-1)+x)/n`. If the average loss is 0 the RSI is 100, or 50 when the price is flat. |
| ATR | `TR = max(H-L, |H-Cprev|, |L-Cprev|)`, with `TR0 = H0-L0`. Wilder smoothing is seeded with the mean of the first n TRs. |
| VWAP | Cumulative `Σ(price·vol)/Σvol` within each session, where price is the typical price `(H+L+C)/3`. It resets at every session open. It is NaN while session volume is 0 and outside the session. |
| RVOL | `volume / mean(volume at the same NY time of day over the previous 20 occurrences)` (min 5). The current bar is excluded. A `rolling` method (previous N bars) is also available. |

EMA, RSI and ATR run continuously across sessions. Only VWAP resets.

## Rules for downstream code (Sprint 2 onwards)

Every resampled frame has these columns:

| Column | Meaning |
|---|---|
| `n_source_bars` | Number of source rows aggregated into the bar. Off-grid rows are included, so no data is discarded |
| `n_grid_bars` | Source rows exactly on the base grid (09:30:00, 09:31:00, ...) |
| `n_off_grid_bars` | Source rows between grid points (e.g. 09:30:30) |
| `is_complete` | `True` only when the source timestamps are **exactly** the expected grid of the bin: every minute present (5 for 5m, 15 for 15m) **and** no off-grid rows. An off-grid row never stands in for a missing minute. |
| `available_at` | The earliest instant the bar's values are known (`timestamp + timeframe`) |

Strategy, backtest and dashboard code **must**:

1. **Never generate a signal from a bar with `is_complete == False`.** Such a bar is missing
   source data, so its high, low, close and volume understate the real bar. Filter with
   `frame[frame["is_complete"]]`, or check the flag on the signal bar.
2. **Never act on a bar before `available_at`.** A bar's timestamp is its open, so acting at
   the timestamp would be look-ahead.
3. Be aware that indicators are still computed *through* incomplete bars, because those bars
   are real observations, not fabricated data. A later complete bar can therefore carry
   influence from an earlier incomplete one. `report.gaps` and the `INCOMPLETE_BARS` report
   entry show where this applies.

The engine never removes valid data or fabricates bars to make a frame look complete.

**Look-ahead protection:** every indicator is causal. `tests/test_no_lookahead.py` checks that
truncating the data, or changing future bars, never changes earlier values.

## Known limitations

- No exchange holiday or early-close calendar. Missing whole sessions are not reported.
  Shortened sessions are reported only as `INCOMPLETE_SESSION` (info). Expected bar counts
  are DST-aware but do not know about holidays or early closes.
- US DST changes at 02:00 on a Sunday, while CME Globex is closed, so with the default
  config every real Globex session is 23h. The DST-aware logic matters for sessions that
  contain a transition: custom sessions, other timezones, or stray weekend data.
- Gaps are detected only *between* bars of the same session. Bars missing at the very
  start or end of a session show up only in the incomplete-session count.
- Minutes with no trades are often simply absent from vendor files. They are reported as
  gaps (not filled), so thinly traded overnight data will produce many gap entries.
- VWAP is computed from bars (typical price), so it approximates a true tick-level VWAP.
  On 5m and 15m bars it is computed from those bars, so it differs slightly from the 1m VWAP.
- No contract roll handling or back-adjustment. Continuous series show roll gaps.
  The expiry shown is the nominal third Friday and ignores holidays.
- Bins are aligned to UTC midnight. This is correct for whole-hour timezone offsets
  (New York, Chicago) and timeframes up to 1h. For other offsets (e.g. +05:30) the
  pipeline refuses to resample when a session boundary would fall inside a bin.
- `is_complete` is relative to the source data. If a vendor file ends with a still-forming
  1m bar, the engine cannot tell. Historical exports normally contain only finished bars.
- Only MES and MNQ are configured. Other roots can be added under `[contracts]`.
- Indicators use simple Python loops. They are fine for months of 1m data but not optimised
  for many years of tick data.
- Single-digit years (`MESH5`) are resolved to the year nearest the data start.
