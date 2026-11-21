# Futures Trading System

A personal Python **research and paper-trading** project for intraday **MES** and **MNQ** futures.

> **No live trading.** This code has no order-execution component. It does not claim that any
> strategy is profitable. The sample data is **synthetic** and is not real market prices.

- **Sprint 1, market data engine:** loading CSV files, validating them, converting timestamps
  to UTC, identifying contracts, resampling and computing core indicators.
- **Sprint 2, backtesting engine:** an event-driven historical simulation with orders, fills,
  positions, costs, a risk engine and performance metrics. There is no trading strategy yet,
  only the interface for one.

## Project layout

```
futures-trading-system/
├── config/settings.toml        # data settings: columns, timezones, sessions, contracts, indicator periods
├── config/backtest.toml        # backtest settings: capital, costs, slippage, execution policies, sizing, risk
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
│   ├── backtest/               # engine, orders, position, execution (fill rules), sizing, metrics, config
│   ├── risk/manager.py         # configurable risk limits that can reject any entry
│   ├── strategy/base.py        # Strategy interface + StrategyContext (no strategy implemented yet)
│   ├── ai/ dashboard/          # placeholders for later sprints
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

## Sprint 2: Backtesting engine

> Historical simulation only. Nothing here connects to a broker or sends an order.
> Results on the synthetic sample data are meaningless and are not evidence of profitability.

### Architecture

```
signal bars (1m/5m/15m) ──► SignalEvent at available_at ──► Strategy.on_bar(ctx) ──► OrderRequest
                                                                                       │
                                     validate ─► size (sizing.py) ─► RiskManager.check_entry ─► Order
                                                                                       │
execution bars (1m) ──► ExecutionEvent at bar open ──► housekeeping (session end, gaps, emergency)
                                                   ──► entry fill (+ second risk check) ─► bracket orders
                                                   ──► exits (market / stop-loss / take-profit, OCO)
                                                   ──► mark-to-market at close ─► equity curve
                                                                                       │
                                          trades, orders, fills, rejections ─► metrics.py
```

| Module | Responsibility |
|---|---|
| `backtest/engine.py` | Event queue, order lifecycle, fills, trade log, equity curve |
| `backtest/execution.py` | Pure fill rules: tick rounding, slippage, stop/limit triggering, gaps |
| `backtest/orders.py` | `OrderRequest` (strategy → engine), `Order`, `Fill`, enums |
| `backtest/position.py` | Signed position, average entry price, realized/unrealized PnL |
| `backtest/sizing.py` | Fixed or risk-based contract count |
| `backtest/metrics.py` | Performance statistics |
| `risk/manager.py` | Daily loss, daily trades, position size, consecutive losses, profit target, trading windows, emergency stop |
| `strategy/base.py` | `Strategy` ABC and the read-only `StrategyContext` |

### Timing rules (anti-look-ahead)

1. A signal bar produces an event at its `available_at` (open + timeframe). Only then is the
   strategy called. `ctx.history` holds only bars with `available_at <= now`, and the engine
   asserts this on every call.
2. Execution bars produce an event at their **open**. At equal timestamps the signal is
   processed first. An order created at time T can therefore only fill on bars opening at or
   after T: a decision made at the close of bar k fills at the open of the next bar at the
   earliest.
3. Signals can come from 5m or 15m bars while fills use 1m bars (recommended, because it reduces
   ambiguity). The execution frame must not be coarser than the signal frame.
4. Each signal bar is decided exactly once. Only one entry per bar and one pending entry at a
   time are allowed, and there is no entry while a position is open. Rejections are logged.
5. Missing data is never filled. A market entry that cannot fill within
   `max_fill_delay_minutes` (data gap) is cancelled and reported. A position still open when the
   data jumps into a new session is closed at the first new bar and reported as `session_gap`.
   NaN prices are rejected.

### Incomplete-bar policy (configurable)

- **Signal bars:** with `incomplete_signal_bar_policy = "skip"` (the default), the strategy is
  not called on incomplete bars. With `"allow"`, it is called with `ctx.bar_is_complete = False`.
- **Execution bars:** `entries_on_incomplete_bars = false` (the default) makes entries wait for
  a complete bar. Exits are always evaluated on every real bar, because protective stops must
  not be skipped.
- Trades touching an incomplete bar are flagged `data_incomplete`.

### Fill rules

| Order | Fill |
|---|---|
| Market | Next eligible bar open ± slippage (adverse) |
| Stop | Stop price ± slippage when the bar trades through it, or the **open** when the bar gaps past it (worse) |
| Limit | Limit price when price trades **through** it (touch is not enough, configurable), or the open when the bar opens through it. No slippage by default |
| Stop-loss / take-profit | Attached to the entry as an OCO bracket and active from the entry bar |

- **Tick rounding:** trigger prices are rounded so the order is *harder* to fill (buy stop up,
  sell stop down, buy limit down, sell limit up).
- **Costs:** commission is charged per contract per side. Slippage is included in fill prices
  and also reported separately.

**Ambiguity (never the favorable outcome):**
- If the stop-loss and take-profit could both have been hit in the same bar, and neither at
  the open, the stop-loss fills and the trade is flagged `ambiguous`.
- On the bar where a stop or limit entry filled intrabar, a take-profit is never credited, and
  a stop-loss inside the bar range is assumed hit (also flagged `ambiguous`).
- Market entries fill at the open, so the rest of that bar can legitimately trigger the bracket.

### Checks at the actual entry fill

Signal-time checks can only use a reference price: the signal bar's close for market orders,
or the order price otherwise. The next bar can open somewhere else. So when an entry would
fill, the engine repeats three checks against the **simulated fill price** (slippage
included) **before** opening anything:

1. **Bracket validity.** A long needs `stop_loss < fill < take_profit`; a short needs
   `take_profit < fill < stop_loss`. If a gap breaks this, adverse or favorable, the entry is
   rejected with `invalid_bracket_at_fill` (stage `fill`). An invalid protective bracket is
   never created, and no position is opened.
2. **Risk budget**, for entries sized by `sizing.method = "risk"`. The actual risk per
   contract is `|fill - stop_loss| × point value + round-trip commission + stop-exit
   slippage`. Entry slippage is already inside the fill price, so it is not counted twice. If
   `quantity × actual risk > risk_per_trade`, then `fill_risk_policy` decides:
   - `"reduce"` (the default) lowers the quantity to `floor(risk_per_trade / actual risk)`
     and logs the change in `result.adjustments`. The entry is rejected
     (`risk_budget_exceeded_at_fill`) if that is 0.
   - `"reject"` rejects the entry.

   The quantity is never increased after a favorable gap. Quantities requested explicitly by
   the strategy, and `fixed` sizing, have no risk budget and are not resized.
3. **Risk engine:** trading windows, daily limits and position size are checked again.

**Intraday flattening:** positions are closed at market and pending entries cancelled
`flatten_minutes_before_session_end` before the session end. New entries are rejected from
that point.

### Contract specifications (verified against CME Group)

| | Multiplier | Tick | Tick value |
|---|---|---|---|
| MES | $5 × S&P 500 | 0.25 | $1.25 |
| MNQ | $2 × Nasdaq-100 | 0.25 | $0.50 |

### Risk engine

`RiskManager.check_entry()` runs when the signal is processed **and again at fill time**.
Exits are never blocked. Every limit is configurable in `[risk]`, and `0` disables a limit:

- `max_daily_loss`: see *Daily loss limit* below
- `daily_profit_target`: realized net PnL per trade date
- `max_daily_trades`: entries per trade date
- `max_position_size`
- `max_consecutive_losses`: a win resets the count; optionally it also resets daily
- `trading_windows`: local session time, `[start, end)`
- `emergency_stop`: config flag or `activate_emergency_stop()`. It rejects all entries and
  flattens at the next bar open.

#### Daily loss limit

`daily_loss_basis` controls what counts towards the limit:
- `"realized"`: closed trades only, net of all costs.
- `"realized_and_unrealized"` (the shipped config): closed trades plus the open position:

```
daily PnL = realized net PnL of the trade date
          + (price − average entry fill) × signed quantity × point value
          − entry commission already paid for the open position
```

Entry slippage is already inside the average entry fill. Exit commission and exit slippage
count only once they happen, and then they appear in the realized net PnL. Nothing is
counted twice.

**When it is evaluated:**
- **Each signal and each entry-fill attempt:** the day must not be breached.
- **Each execution bar while a position is open:** after that bar's fills, the daily PnL is
  evaluated at the bar's most **adverse** price (low for a long, high for a short). If it
  reaches `-max_daily_loss`, the trade date is marked breached. It **stays** breached even if
  prices recover, and all new entries that day are rejected (`max_daily_loss`).

**`daily_loss_action` decides what happens to an open position:**
- `"flatten"`: the price at which the daily PnL reaches the limit acts as a protective stop.
  It is rounded to the tick so it triggers no later than the limit. If the bar opens beyond
  it, the fill is at the open. The exit reason is `daily_loss_limit`, and the bracket stop and
  this level compete as two stops on the same side.
- `"block_entries"`: the position is kept, and only new entries are blocked.

Protective exits (stops, targets, strategy exits) are never blocked. The realized loss can
exceed the limit by the exit commission, the exit slippage and any gap. The limit is a
trigger, not a guaranteed maximum.

The basis must be set explicitly. `RiskConfig()` built in code defaults to `"realized"` for
backward compatibility; `config/backtest.toml` uses `"realized_and_unrealized"`.

### Metrics

"Gross" always means **before** commission and slippage. "Net" means **after** both.

| Per trade | Definition |
|---|---|
| `gross_pnl` | (exit price − entry price) × direction × quantity × point value, using un-slipped prices |
| `slippage_cost`, `commission`, `total_costs` | Entry + exit slippage, entry + exit commission, and their sum |
| `net_pnl` | `gross_pnl − commission − slippage_cost` |

| Metric | Definition |
|---|---|
| `gross_profit` / `gross_loss` | Sum of positive / negative `gross_pnl` (before costs) |
| `gross_pnl_before_costs` | Sum of all `gross_pnl` |
| `total_commission`, `total_slippage_cost`, `total_costs` | Cost totals |
| `net_profit` | Sum of `net_pnl` (equals `gross_pnl_before_costs − total_costs`) |
| win / loss | `net_pnl > 0` / `net_pnl < 0`, after costs. Breakeven trades count only in the totals |
| `net_profit_of_winners` / `net_loss_of_losers` | Sum of `net_pnl` of wins / of losses |
| `profit_factor` | `net_profit_of_winners / |net_loss_of_losers|`, after costs. inf with no losses, NaN with no trades |
| `gross_profit_factor` | `gross_profit / |gross_loss|`, before costs |
| `win_rate`, `average_win`, `average_loss` | Based on net results |
| `expectancy` | `net_profit / total_trades` |
| `average_r_multiple` | Mean of `net_pnl / initial risk`, over trades with a stop |
| `max_drawdown`, `max_drawdown_pct` | Largest peak-to-trough fall of the mark-to-market equity, including the starting capital |
| `daily_pnl` | `net_pnl` summed by exit trade date |

A trade can be a gross profit and still a net loss once costs are deducted. The metrics keep
both views.

### Using the engine

```python
from src.config import load_config
from src.backtest import BacktestEngine, OrderRequest, load_backtest_config
from src.pipeline import build_market_data
from src.strategy import Strategy

class MyStrategy(Strategy):
    def on_bar(self, ctx):
        ...  # use only ctx.bar / ctx.history; return [OrderRequest.enter("long", stop_loss=..., take_profit=...)]

cfg = load_config()
md = build_market_data("data/samples/SYNTHETIC_MES_1m.csv", cfg)
engine = BacktestEngine(load_backtest_config(), md.contract.spec, cfg.session)
result = engine.run(MyStrategy(), signal_bars=md.frames["5min"], execution_bars=md.frames["1min"])
print(result.metrics); result.trades; result.equity; result.rejections
```

### Sprint 2 assumptions

- **Commission:** `$1.00` per contract per side is a placeholder. Set your own all-in cost.
- **Slippage:** the default is 1 adverse tick on market and stop fills and 0 on limits. This
  is a modelling assumption, not a measurement.
- **Intrabar timing:** intrabar fill times are unknown inside a bar. `entry_time` and
  `exit_time` are the open of the execution bar (`*_at_open` says whether the fill was at the
  open). The fill-time risk check uses the bar's open time.
- **Daily figures:** daily figures use the trade date of the exit.
  - The profit target uses **realized** PnL.
  - The daily-loss limit uses the configured basis. The shipped config includes unrealized
    PnL, evaluated at each execution bar's most adverse price.
- **Risk budget:** risk-sized entries are re-checked at the actual fill and reduced or
  rejected, never increased. Exit gaps can still exceed the budget, because a stop is not a
  guaranteed price.
- **Positions:** one position at a time, no scaling in or out, and no reversal in one order.
  The `Position` class is general, but the engine limits it.
- **Win/loss:** a trade with `net_pnl < 0` is a loss, `> 0` a win, and breakeven resets the
  consecutive-loss count.

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
- **Backtest, bar data only:**
  - Intrabar order is unknown, so ambiguous bars are resolved pessimistically. Use 1m
    execution bars to reduce how often this happens.
  - There is no queue position, partial fill, liquidity or market-impact model. Limit orders
    either fill fully or not at all.
- **Backtest, slippage:** slippage is a fixed number of ticks. It does not vary with
  volatility, news or order size.
- **Backtest, positions:** one contract root and one open position at a time. Multi-asset
  and multi-strategy portfolios are not supported.
- **Backtest, accounting:** there is no margin, overnight financing or account liquidation
  model, and no prop-firm rules. The risk limits are generic.
- **Backtest, exits:** the end of data closes any open position at the last close, and the
  run reports a warning.
- **Backtest, daily loss:**
  - The unrealized check uses the bar's extreme. Inside one bar the engine cannot tell whether
    the extreme came before or after a take-profit, so it assumes the worst case.
  - The exit commission and exit slippage are not part of the trigger, so the realized loss
    can slightly exceed the limit.
