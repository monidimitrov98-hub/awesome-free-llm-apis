# Futures Trading System

A personal Python **research and paper-trading** project for intraday **MES** and **MNQ** futures.

> **No live trading.** This code has no order-execution component. It does not claim that any
> strategy is profitable. The sample data is **synthetic** and is not real market prices.

- **Sprint 1, market data engine:** loading CSV files, validating them, converting timestamps
  to UTC, identifying contracts, resampling and computing core indicators.
- **Sprint 2, backtesting engine:** an event-driven historical simulation with orders, fills,
  positions, costs, a risk engine and performance metrics.
- **Sprint 3, VWAP Trend Pullback strategy:** the first rule-based strategy, with
  multi-timeframe context, structured signal diagnostics and a reproducible backtest runner.
  It is **not** validated and makes **no** profitability claim.

## Project layout

```
futures-trading-system/
├── config/settings.toml        # data settings: columns, timezones, sessions, contracts, indicator periods
├── config/backtest.toml        # backtest settings: capital, costs, slippage, execution policies, sizing, risk
├── config/strategy_vwap_pullback.toml  # strategy parameters (unvalidated research hypotheses)
├── config/account_profiles/    # generic account-protection (drawdown) rule profiles
├── run_backtest.py             # run the strategy on a CSV and write all result files
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
│   ├── strategy/base.py        # Strategy interface + StrategyContext (incl. higher-timeframe context)
│   ├── strategy/vwap_pullback.py, vwap_pullback_config.py   # VWAP Trend Pullback strategy
│   ├── indicators/swings.py    # confirmed (non-repainting) swing pivots and market structure
│   ├── backtest/runner.py      # CSV -> pipeline -> engine -> result files
│   ├── backtest/manifest.py    # run manifests (config snapshot, data hash, git commit) + replay
│   ├── backtest/validation.py  # date ranges, in-sample / out-of-sample, walk-forward, OOS ledger
│   ├── risk/account.py         # account-level drawdown protection (AccountGuard)
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

## Sprint 3: VWAP Trend Pullback strategy

> A research implementation. All parameters are **unvalidated hypotheses**. Results on the
> synthetic sample data are meaningless, and nothing here is evidence of profitability
> without out-of-sample testing on real data.

### Architecture

```
CSV ─► Sprint 1 pipeline ─► 1m, 5m, 15m frames (indicators, is_complete, available_at)
                                  │        │       │
         execution bars (1m) ◄────┘        │       └──► context_bars (15m): the engine cuts each
         signal bars (5m) ◄────────────────┘             frame to available_at <= now (ctx.context)
                                  │
            BacktestEngine (Sprint 2) ── SignalEvent ──► VwapTrendPullbackStrategy.on_bar(ctx)
                                  │                          │ rules + diagnostics record
                                  │ ◄── OrderRequest.enter(market, stop_loss, take_profit)
            RiskManager ─► execution model ─► bracket ─► trade log, equity, metrics
```

The strategy contains only decision logic. Orders, fills, brackets, risk checks, the trade log
and metrics all come from Sprint 2, unchanged.

Two small, additive engine changes were needed:
1. `BacktestEngine.run(..., context_bars=...)` with a new `ctx.context` field. Without
   context frames, behaviour is unchanged.
2. Lazy exports in `src/backtest/__init__.py`. This fixes a latent circular import that
   appeared when `src.strategy` was imported first.

### LONG rules (SHORT is the exact mirror)

All checks run on **completed** signal bars. Every check is evaluated and recorded.

| # | Check | Default |
|---|---|---|
| Gates | Flat, no pending entry, current bar complete, inside the session and the strategy window (decision time), indicator values present on every bar used, and those bars contiguous, complete and in the current session | window 09:45–15:30 NY |
| Trend | Close > VWAP; EMA9 > EMA21; (EMA9 − EMA21) / ATR ≥ minimum strength | 0.10 ATR |
| Structure | Confirmed swing pivots in the current session: last swing high > previous **and** last swing low > previous (HH + HL) | pivots 2 left / 2 right, 60 bars |
| Context | Last **completed** 15m candle of this session: close > VWAP, EMA9 > EMA21; not stale, not incomplete | on |
| Pullback | In the last 3 signal bars, some bar's low lies in `[level − 0.5 ATR, level + 0.25 ATR]` of EMA9, EMA21 or VWAP. No bar of that window may reach the last confirmed swing low | 3 bars, 0.25 / 0.50 ATR |
| Not chasing | (close − VWAP) ≤ 1.5 ATR and \|close − EMA9\| ≤ 1.0 ATR | |
| Confirmation | Close > open, close > EMA9 (close > previous high is optional) | |
| RSI | Min RSI in the window ≤ 50, RSI now ≥ 50 and ≤ 75, rising | |
| RVOL | RVOL ≥ 1.0. NaN (not enough history) never passes; the filter can be disabled | |
| Stop / target | See below | |
| Filters | 15-minute cooldown after an exit fill; max 3 filled entries per trade date; one entry per pullback | |

**SHORT** mirrors every rule:
- **Trend:** close < VWAP, EMA9 < EMA21, LH + LL structure.
- **Pullback:** a *high* reaches into `[level − 0.25 ATR, level + 0.5 ATR]`, and the last
  confirmed swing *high* must not be reached.
- **Confirmation:** close < open and below EMA9.
- **RSI:** max RSI in the window ≥ 50, RSI now ≤ 50 and ≥ 25, falling.

A pullback alone never generates an entry: the confirmation candle, RSI and RVOL checks must
pass on the current completed bar. At most one entry is emitted per bar.

### Stop-loss and take-profit

The reference price is the confirmation bar's close.

- **Swing (default):** the stop is the last confirmed swing low − 2 ticks (long), or the last
  confirmed swing high + 2 ticks (short).
- **ATR:** the stop is close − 1.5 × ATR (long), or close + 1.5 × ATR (short).
- **Rounding:** prices are rounded with the engine's own `conservative_order_price`. A sell
  stop rounds down and a buy stop rounds up, so the stop is never closer than requested.
- **Distance limits:** the stop must lie on the correct side, at least
  `max(0.5 ATR, 4 ticks)` away and at most `3 ATR` away. An optional absolute tick cap is
  also available. Otherwise the setup is rejected (`stop_wrong_side`, `stop_too_tight`,
  `stop_too_wide`, `stop_unavailable`).
- **Take-profit:** close ± R × stop distance, rounded conservatively (sell limit up, buy limit
  down). The default is R = 2.
- **Several R values:** `--r-multiples 1.5 2 3` runs each value separately, side by side. No
  value is selected.
- **Re-checks at the fill:** the engine re-checks both prices at the actual fill. If a gap
  puts the fill outside the bracket, the entry is rejected (`invalid_bracket_at_fill`).
  Risk-based size is fitted to the budget. The realised R therefore differs slightly from
  the planned R.

### How the timeframes interact

| Timeframe | Role | Visible to the strategy |
|---|---|---|
| 1m | Execution only (fills, brackets, session flatten) | Never |
| 5m | Signal: one decision per completed bar at `now = available_at` (bar open + 5 min) | Rows with `available_at <= now`, i.e. up to the current bar |
| 15m | Context: trend filter | Rows with `available_at <= now`, cut by the engine. The 10:30 candle is visible from 10:45, never earlier |

- **Example:** at 10:55 (5m bar 10:50 just completed) the last visible 15m candle is the
  10:30 one. The 10:45 candle becomes visible at 11:00.
- **Context rejections:**
  - `context_stale`: the latest visible 15m candle is older than one 15m period (a data gap).
  - `context_unavailable`: it belongs to another session, for example the first 15 minutes
    after the open.
  - `context_incomplete`: it is incomplete.
  - `context_missing_indicator`: its indicator values are missing.
- **Order execution:** the strategy's order fills on the next eligible 1m bar (open ≥ `now`).
  If no 1m bar arrives within 2 minutes, the order is cancelled and reported.
- **Incomplete signal bars:** they are skipped (engine policy, logged as
  `incomplete_signal_bar_skipped`). If the engine is configured to allow them, the strategy
  itself rejects them (`incomplete_signal_bar`).
- **No fabricated data:** missing indicator values (warm-up) are rejected, never filled in.

### Signal diagnostics

Every evaluated setup produces one row per direction in `strategy.diagnostics_frame()`
(written to `signal_diagnostics.csv`). The columns are:

`time, bar_time, session_date, instrument, direction, decision (signal / reject / skip),
rejection_reason (first failing check), reasons (all failing checks), close, vwap_relation,
ema_relation, trend_strength_atr, structure, context_status, pullback_status, pullback_level,
extension_status, candle_status, rsi, rsi_status, rvol, rvol_status, stop_price,
stop_distance, target_price, target_distance, pullback_id`.

The codes are defined in `src/strategy/vwap_pullback.py` (`Reason`). Rejections by the risk
manager and the execution engine (for example `outside_trading_window`,
`invalid_bracket_at_fill` or `max_daily_loss`) are written to `engine_rejections.csv`.

### Running the strategy

```powershell
python run_backtest.py --csv data\samples\SYNTHETIC_MES_1m.csv
python run_backtest.py --csv C:\data\MNQM25_1m.csv --output-dir output\mnq
python run_backtest.py --csv C:\data\MESM25_1m.csv --r-multiples 1.5 2 3
python run_backtest.py --csv my.csv --strategy-config config\strategy_vwap_pullback.local.toml
```

Each run writes the following files to `output/r_<R>/`:
- `trades.csv`, `equity.csv`, `daily_pnl.csv`, `metrics.json`
- `config_used.json` (strategy, backtest, contract and session settings)
- `signal_diagnostics.csv`, `engine_rejections.csv`, `orders.csv`, `adjustments.csv`
- `warnings.txt`, `data_quality.txt`

The CSV needs at least 6 sessions of 1m data, because RVOL needs 5 earlier sessions at the
same time of day. Earlier bars are rejected with `rvol_unavailable` unless `[rvol]` is
disabled. Real data is loaded as in Sprint 1, with the same column mapping and timezone
settings.

## Sprint 3 audit corrections

Each correction below has its own regression test file:

| Finding | Test file |
|---|---|
| H1, fill-price risk | `test_fix_h1_fill_quality.py` |
| H2 and M1, data integrity and pullback key | `test_fix_h2_m1_structure.py` |
| M2, independent filters | `test_fix_m2_filters.py` |
| M3, order lifecycle | `test_fix_m3_lifecycle.py` |
| Drawdown protection | `test_account_guard.py` |
| M5 and M6, manifests and validation | `test_manifest_validation.py` |

### Fill-price risk and reward/risk (H1)

The strategy attaches three limits to every entry:
- `max_stop_distance`: the smaller of `max_distance_atr × ATR` and the tick cap.
- `min_stop_distance`: `max(min_distance_atr × ATR, min_distance_ticks)`.
- `min_reward_risk`: `r_multiple × min_actual_r_fraction`, which is 2.0 × 0.9 = 1.8 by default.

At the **actual simulated fill** the engine recomputes the stop distance, the reward/risk and
the dollar risk. If a limit is violated it **rejects** the entry (`stop_too_wide_at_fill`,
`stop_too_tight_at_fill` or `reward_risk_below_min_at_fill`). These checks run for **every
sizing method, including fixed**, before the risk-budget and risk-engine checks. Stops and
targets are never moved.

Every fill attempt is logged in `entry_checks.csv`: fill price, planned and actual stop
distance, planned and actual R, dollar risk, outcome and reason. Accepted trades carry the
same columns in `trades.csv`.

### Data integrity in market structure (H2)

Swing pivots are accepted only if every bar of their `left + 1 + right` window is complete and
exactly one period apart in **time**. A missing bar is never treated as an adjacent candle.

Rejected pivot shapes are counted, and the diagnostics column `structure_data` shows
`rejected_pivots=N`. When this leaves no valid structure, the setup is rejected with
`structure_data_invalid`. Stops come only from valid pivots. The pullback window must also be
complete, contiguous and in the current session. This is data integrity, not a filter, so it
cannot be switched off.

### Stable pullback identity (M1)

A pullback's id is the last confirmed swing high (long) or low (short). If none is confirmed,
it is the bar holding the session's highest high (lowest low) up to the latest touch of the
zone. That is the same timestamp format a later pivot confirmation of that bar produces.

The id does not move with the lookback window, so one pullback can only be entered once.

### Independent filters (M2)

- Trend strength is now **direction-free**: `|EMA9 − EMA21| / ATR`.
- Direction is checked only by `require_ema_alignment`.
- A value of 0 disables each numeric filter: trend strength, both extension limits, the
  maximum stop distance and `min_actual_r_fraction`.
- `pullback.require_structure_intact` switches the "pullback must not break the last swing"
  rule on its own.
- `test_fix_m2_filters.py` proves, filter by filter, that each one fails when on and has no
  effect when off.

### Signal ↔ engine reconciliation (M3)

Every submitted signal has a `signal_id`, sent to the engine as `client_id`. The engine reports
`accepted`, `rejected` (stage `signal` or `fill`), `filled` or `cancelled` through
`Strategy.on_order_event`.

These outcomes are recorded in three places:
- **`signal_diagnostics.csv`**: the `order_status`, `order_reason` and `order_id` columns.
- **`signals.csv`**: one lifecycle row per signal.
- **`trades.csv`, `entry_checks.csv` and `engine_rejections.csv`**: these carry the same id.

A pullback is *reserved* when its order is submitted, *released* if the order is rejected or
cancelled, and *consumed* only when it fills. A second pending entry is impossible.

### Security and validation fixes (after the adversarial audit of `0b6bb79`)

| Finding | Fix | Tests |
|---|---|---|
| H1: unknown risk passed the guard | See *Entry risk validation* below | `test_security_h1_unknown_risk.py` |
| H2: flatten ignored exit costs | See the emergency-flatten notes below | `test_security_guard_flatten.py` |
| H3: OOS ledger blind to code | See *Code identity* below | `test_validation_integrity.py` |
| M1: negative `floor_lock_offset` | `floor_lock_offset` must be a finite number ≥ 0; checked at start-up | `test_security_guard_flatten.py` |
| M2: end-of-data breach not recorded | The final forced exit updates the guard; a final invariant marks any equity row at or below its floor as a breach | `test_security_guard_flatten.py` |
| M3: ledger bypasses | Overlap checks; a canonical ledger location independent of `--output-dir` | `test_validation_integrity.py`, `test_security_ledger.py` |
| M4: replay ignored code identity | Replay checks code, data, manifest consistency and versions | `test_validation_integrity.py` |
| M5: fragile ledger | Append-only, hash-chained ledger with a head file | `test_security_ledger.py` |
| L1: no gap reserve | `gap_reserve_ticks` | `test_security_h1_unknown_risk.py` |
| L2: intraday timing | A floor raised by a bar's high is never acted on at that bar's open | `test_security_guard_flatten.py` |
| L3: lifecycle idempotency | Explicit lifecycle state machine; entries counted once per order ID | `test_security_lifecycle.py` |
| L4: overlapping walk-forward folds | `--wf-step` smaller than the test size is rejected | `test_manifest_validation.py` |
| L5: R on gross prices | Documented below; `estimated_net_reward_risk` added | |
| L6: account state per period | Documented below | |

#### Entry risk validation (H1)

- With account protection enabled, every entry needs a valid stop-loss (`account_requires_stop_loss`).
- All order prices must be finite and positive (`invalid_order_price`), and quantities must be
  integers ≥ 1 (`invalid_quantity`).
- The contract tick size and point value must be finite and positive; this is checked when the
  engine starts.
- At the fill, risk that is NaN, infinite, zero or negative is rejected (`account_risk_unknown`).
  This applies to every sizing method and order type.

#### Code identity (H3)

The code identity is the git commit, a clean/dirty flag for the working tree, and a SHA-256 over
all `src/**/*.py` files plus `run_backtest.py` and `run_pipeline.py` (line endings normalised).

The validation fingerprint combines the configuration texts, the target R and that source hash.

An out-of-sample reveal is refused (exit 6) if:
- the commit is unknown, or
- the working tree is dirty (modified or untracked files).

It is also refused (exit 4) when it overlaps dates already revealed under another fingerprint.

#### Validation ledger (M3, M5)

**Location:** `validation_history/oos_ledger.jsonl` in the project, plus a `.head` file. The
folder is git-ignored. `--ledger PATH` or the `FTS_LEDGER_PATH` environment variable can override
the location; the manifest records which ledger was used and whether it was the canonical one.

**Creation:** the ledger is created only by `--init-ledger`. A missing ledger is never created
implicitly.

**Overlap rule:** any run is refused (exit 4) if it overlaps out-of-sample dates already revealed
**for the same instrument under a different fingerprint**. This covers exact, shifted and partial
overlaps, in-sample ranges and full-file runs. Runs with the same fingerprint are allowed, because
they add no new selection. `--allow-oos-reuse` overrides the rule but marks the result as
CONTAMINATED in the manifest and the ledger.

**Damage detection:** each record stores its sequence number, the previous record's hash and its
own SHA-256. The head file stores the record count and the last hash. Edited, deleted, reordered or
truncated records, invalid JSON and a missing head file are all detected. When that happens every
run stops (exit 5), and nothing is ever rewritten or recreated. `--verify-ledger` checks it on
demand.

**Backup and recovery:** copy both files (`oos_ledger.jsonl` and `oos_ledger.jsonl.head`)
somewhere safe after each reveal, for example into a separate private repository. Restore both
together.

**Limitation:** the ledger is tamper-*evident* against accidents and casual edits, not
tamper-proof. Deleting both files, or consistently rewriting every record and the head, cannot be
detected without your backup.

#### Replay statuses (M4)

`--replay` checks the data hash, that the manifest's embedded configuration matches its recorded
hash, the code identity, and the dependency versions. Only `EXACT` counts as a successful exact
reproduction.

| Status | Meaning | Exit code |
|---|---|---|
| `EXACT` | Same trusted code, same data, identical results | 0 |
| `EXACT_DIFFERENT_DEPENDENCIES` | Identical results, but other Python/pandas/numpy versions | 7 |
| `APPROXIMATE` | Other library versions; results differ only numerically (same trade count, net profit within 1e-6) | 7 |
| `MISMATCH` | Same code, different results | 3 |
| `MANIFEST_INCONSISTENT` | The embedded configuration does not match its recorded hash | 3 |
| `UNTRUSTED_CODE` | Different source, a dirty tree, or an unknown commit (either side) | 6 |

Changed data stops the replay with exit 1.

#### Reward/risk (L5) and account state (L6)

**Reward/risk:** the minimum R checked at the fill is the **gross, price-based** ratio
`|target − fill| / |fill − stop|`. `entry_checks.csv` also reports `estimated_net_reward_risk`
(target reward minus round-trip commission, divided by the stop-out risk including costs). Neither
number is a statement about profitability.

**Account state:** each period or fold starts a **fresh** simulated account, with a fresh risk
manager and account guard. Fold results are **not** a continuous funded-account survival
simulation. A continuous mode is not implemented.

### Reproducible runs (M5)

Every result folder contains `manifest.json` with:
- **Configuration:** the exact text of `settings.toml`, `backtest.toml`, the strategy file
  and the account profile, plus parsed snapshots.
- **Data:** the CSV's SHA-256, size and first/last bars, and the period's first/last bars and
  sessions.
- **Market setup:** the contract specification, timezone, column mapping, session and
  indicator settings.
- **Assumptions:** slippage, commission and the fill model.
- **Environment:** the git commit (and whether the tree was dirty), and the Python, pandas,
  numpy and tzdata versions.
- **Run:** the command line and a result fingerprint (a hash of the trade log and equity
  curve).

`python run_backtest.py --replay <manifest.json>` rebuilds the configuration from the
manifest, refuses an altered data file, re-runs the backtest and compares fingerprints.
`REPRODUCED` exits 0; `MISMATCH` exits 3.

### Historical validation controls (M6)

```powershell
python run_backtest.py --csv MNQ.csv --start 2025-01-06 --end 2025-03-28
python run_backtest.py --csv MNQ.csv --in-sample 2025-01-02 2025-02-28 --out-of-sample 2025-03-03 2025-03-28
python run_backtest.py --csv MNQ.csv --in-sample ... --out-of-sample ... --reveal-oos
python run_backtest.py --csv MNQ.csv --walk-forward 40 10 --reveal-oos
```

- **Selection:** periods are selected by trade date (inclusive), and each period starts from
  the initial capital. Indicators are computed on the whole file; they are causal, so earlier
  data is only warm-up.
- **Out-of-sample rules:**
  - The out-of-sample period must start after the in-sample period ends.
  - It stays **sealed** until `--reveal-oos` is given.
  - It accepts **one** pre-registered configuration only. `--r-multiples` is refused there.
- **Out-of-sample ledger:** every reveal is written to `oos_ledger.json`. Re-testing the same
  data and out-of-sample period with a *different* configuration is refused (exit 4). It is
  only possible with `--allow-oos-reuse`, which the manifest records.
- **Walk-forward:** fixed-configuration rolling folds. There is no optimizer, so nothing is
  fitted per fold.
- **Reporting:** each period is reported separately and nothing is ranked.

## Account-level drawdown protection

`src/risk/account.py` (`AccountGuard`) is independent of the strategy and of any AI module. Its
rules come from a profile file referenced by `config/backtest.toml`
(`[account] protection_profile`). The strategy configuration cannot contain or override them:
an unknown `[account_rules]` section is rejected.

The shipped `config/account_profiles/generic_example.toml` holds **illustrative numbers, not
any firm's rules**. A verified prop-firm profile must be added as its own file.

**What the guard supports:**
- **Drawdown models:**
  - `static`: the floor is the initial balance − max drawdown.
  - `trailing_eod`: the floor trails the highest end-of-session equity.
  - `trailing_intraday`: the floor trails intraday equity, conservatively using each bar's
    favourable extreme before its adverse move, or bar closes.
- **Floor lock:** optional `floor_lock_offset` stops trailing at the initial balance plus an
  offset.
- **Entry blocking:**
  - when the remaining buffer is at or below `safety_buffer` (checked at the signal and at
    the fill);
  - when a full stop-out of the new trade, using its actual risk at the fill, would leave less
    than `safety_buffer` (`require_trade_risk_within_buffer`).
- **Emergency flatten:** `floor + flatten_buffer` acts as a protective stop for open
  positions, alongside the bracket stop and the daily-loss level (the closest triggers first).
  Entries are then locked for the session or permanently.
- **Breach:** equity at or below the floor locks trading permanently.
- **Reporting:** the floor and buffer are added to the equity curve, and a summary goes to
  `account_protection.json` and `metrics.json`.

**What still needs a broker or prop-firm adapter:**
- tick-level real-time equity and the firm's own equity calculation (fees, platform charges)
- consistency rules, minimum trading days, payout rules and scaling plans
- news-event and holiday restrictions, and weekend rules
- per-account contract limits beyond `max_position_size`
- any rule that depends on data this bar-based backtest does not have

**Emergency flatten and exit costs:**
- The flatten trigger includes the **estimated exit commission and stop slippage of the whole
  position**. If the exit fills as modelled, equity afterwards is `floor + flatten_buffer`.
- `flatten_buffer` must be > 0 and at least one contract's estimated exit cost; this is checked at
  start-up.
- Every emergency flatten locks entries, whatever price it filled at.
- **Gap reserve:** `gap_reserve_ticks` adds a per-contract allowance to each trade's risk before
  the safety-buffer check.

These are estimates. A bar-based backtest cannot guarantee real-world fill prices, and no finite
reserve covers every gap. Whenever simulated equity reaches or falls below the floor, including
through gaps or a final forced exit, it is recorded as a breach.

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
- **Strategy:**
  - Parameters are untested hypotheses. There has been no optimisation and no
    out-of-sample validation.
  - The synthetic sample is a random walk and produces almost no trends, so it only
    exercises the code.
- **Strategy, stops and targets:** they are planned from the confirmation close. The fill
  happens at the next 1m open, so the realised risk and R differ slightly. The engine
  re-checks the bracket and the risk budget.
- **Strategy, market structure:** structure is evaluated within the current session only,
  and pivots need 2 confirmation bars, so structure lags by design. Ties (equal highs or
  lows) are not pivots.
- **Strategy, entry counting:** `max_entries_per_session` counts filled entries. A pullback
  is consumed only by a filled entry.
- **Strategy, no in-trade management:** there is no trailing stop, break-even or partial exit.
  Exits come from the bracket, the session flatten or the daily-loss limit.
- **Backtest, daily loss:**
  - The unrealized check uses the bar's extreme. Inside one bar the engine cannot tell whether
    the extreme came before or after a take-profit, so it assumes the worst case.
  - The exit commission and exit slippage are not part of the trigger, so the realized loss
    can slightly exceed the limit.
