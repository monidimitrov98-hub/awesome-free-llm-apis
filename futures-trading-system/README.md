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
| Sessions | Globex runs 18:00 to 17:00 New York time (end exclusive). Each bar's `session_date` is the trade date, so Sunday 18:00 belongs to Monday. RTH 09:30–16:00 is also configured. |
| Resampling | Bars are left-labelled: the 09:30 5m bar covers 09:30:00–09:34:59 and is **known only at 09:35**. Empty bins are dropped. `n_source_bars` and `is_complete` flag partial bars. |
| EMA | `alpha = 2/(n+1)`, seeded with the SMA of the first n closes. NaN before that. |
| RSI | Wilder: the first averages are simple means of n changes, then `(prev*(n-1)+x)/n`. If the average loss is 0 the RSI is 100, or 50 when the price is flat. |
| ATR | `TR = max(H-L, |H-Cprev|, |L-Cprev|)`, with `TR0 = H0-L0`. Wilder smoothing is seeded with the mean of the first n TRs. |
| VWAP | Cumulative `Σ(price·vol)/Σvol` within each session, where price is the typical price `(H+L+C)/3`. It resets at every session open. It is NaN while session volume is 0 and outside the session. |
| RVOL | `volume / mean(volume at the same NY time of day over the previous 20 occurrences)` (min 5). The current bar is excluded. A `rolling` method (previous N bars) is also available. |

EMA, RSI and ATR run continuously across sessions. Only VWAP resets.

**Look-ahead protection:** every indicator is causal. `tests/test_no_lookahead.py` checks that
truncating the data, or changing future bars, never changes earlier values.

## Known limitations

- No exchange holiday or early-close calendar. Missing whole sessions are not reported.
  Shortened sessions are reported only as `INCOMPLETE_SESSION` (info).
- Gaps are detected only *between* bars of the same session. Bars missing at the very
  start or end of a session show up only in the incomplete-session count.
- Minutes with no trades are often simply absent from vendor files. They are reported as
  gaps (not filled), so thinly traded overnight data will produce many gap entries.
- VWAP is computed from bars (typical price), so it approximates a true tick-level VWAP.
  On 5m and 15m bars it is computed from those bars, so it differs slightly from the 1m VWAP.
- No contract roll handling or back-adjustment. Continuous series show roll gaps.
  The expiry shown is the nominal third Friday and ignores holidays.
- Bins are aligned to UTC midnight. This is correct for whole-hour timezone offsets
  (New York, Chicago) and timeframes up to 1h.
- Only MES and MNQ are configured. Other roots can be added under `[contracts]`.
- Indicators use simple Python loops. They are fine for months of 1m data but not optimised
  for many years of tick data.
- Single-digit years (`MESH5`) are resolved to the year nearest the data start.
