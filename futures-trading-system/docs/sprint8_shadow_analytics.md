# Sprint 8: Shadow trading validation and analytics

Shadow analytics is **read-only research tooling**. It reads the journals that paper sessions already
write (MOCK, REPLAY and SHADOW) and reports what happened: sessions, execution quality, performance with
honest uncertainty, regimes, blocked signals, data reliability, account survival and validation status.

It cannot trade. It has no broker methods. It never changes trading, risk, NewsGuard, AccountGuard,
strategy, regime or execution configuration. Scenario and counterfactual results are kept apart from
recorded results and are never written back anywhere.

Simulated results are not evidence of profitability. Most reports carry a sample-size warning, and
nothing here can establish a future edge.

## Architecture

```
PaperSession (Sprint 5)                         src/analytics/shadow/
  journal: paper_state.db  ──(read-only)──►  events.py      ReadOnlyJournal + JournalSource (verified, incremental)
  run_identity (new)                          model.py       trades / signals / decisions tables, CME trading dates
  market_event prices (new)                   reports.py     sessions, execution, performance, costs, regimes,
                                                             blocked, reliability, account survival, compare
                                              stats.py       Wilson, Student-t, day-cluster bootstrap, PF, drawdown
                                              validation.py  integrity / chronology / identity / OOS-ledger checks
                                              library.py     journals, filters, session-type separation, exports
                                                 │
              ┌──────────────────────────────────┼─────────────────────────────┐
     run_shadow_analytics.py (CLI)     TerminalService.shadow()       /api/shadow/* (GET, token)
                                                                       Shadow Analytics screen (React)
```

The **journal is the only source**. It is the hash-chained event log of a paper session. Analytics
open it with a read-only SQLite connection (`mode=ro`). They verify every row against the same chain
hash (HMAC when the journal is keyed) and read incrementally, so each refresh reads only the new rows.
A journal whose chain does not verify is **excluded and listed**, never analysed.

### What Sprint 8 added to the paper runtime (compatibility-preserving)

- `run_identity` journal entry, at creation and after every recovery. It records the session type
  (MOCK / REPLAY / SHADOW / MONITOR) and data label, the provider and the strategy names. It also
  records a SHA-256 hash of the strategy configuration and the strategy version (`name@sha12`), the
  code identity (commit, whether the tree is clean, source hash) and the regime engine identity
  (enabled, config hash, model version, classifier, enforcement). Finally it records contract
  specifications, the session-rules hash, the cost model, the account profile hash and the risk hash.
  This is what keeps results traceable to the exact configuration that produced them.
- `market_event` entries now include the prices (last / size / bid / ask / sizes / instrument). The
  analytics can then measure spreads at signal, order and fill time from the event stream the system
  actually saw.
- `run_paper.py`, `run_marketdata.py` and the terminal record the session type. `run_terminal.py
  --keep-state` keeps a MOCK or REPLAY run's journal in `data/analytics/journals/<mode>-<UTC time>/`.
  By default, those runs stay ephemeral.

No risk, NewsGuard, AccountGuard, RiskManager, EventValidator, strategy or execution logic was changed.

## Data flow

1. **Sources**: `config/analytics.toml` `[sources] journals` (globs relative to the project, `..` and
   absolute paths refused), `--journal` (CLI, inside the project), and in the terminal also the
   terminal's own session journal.
2. **Verify and ingest** (`JournalSource.refresh`). Market events are aggregated into counts, gaps,
   the last quote and per-trading-day spans; they are not stored. Strategy evaluations are kept only
   for signals, and rejections are counted by reason. Decisions, regime records, orders, executions,
   trades, account updates, protection and feed events are kept with their journal `seq`.
3. **Tables** (`model.py`): trades (joined with their entry order, signal and regime record), signals and
   decisions. Each row carries the identity in force at its `seq`: session type, strategy version,
   regime identity and provider.
4. **Reports** (`reports.py`, `library.py`): filtered by instrument, dates, session type, strategy
   version, regime, provider or session. Performance, costs, execution, regimes and blocked signals
   **refuse** to combine several session types.
5. **Outputs**: terminal screen, CLI text/JSON, CSV/JSON exports with provenance metadata.

## Session tracking

A session is (run, CME trading date, instrument). Its ID is `run_id:YYYY-MM-DD:ROOT`, where `run_id`
is the hash of the journal's first row. CME trading dates are DST-aware: 18:00 New York time starts
the next trading date, and the 17:00–18:00 maintenance break has none. For each session:

- **Identity**: session type, data label, provider, contract, strategy version and config hash, code
  hash, regime identity. The contract is the one reported by the feed (`MES 12-26`). "Not reported"
  means the feed did not report one, for example bar replay.
- **Activity**: signals, authorized entries, blocked signals, blocks at fill time, simulated fills,
  completed trades, and cancelled / expired / rejected orders.
- **Results**: net and gross P&L and costs, all SIMULATED. Session drawdown is computed on the
  closed-trade sequence.
- **Context**: first and last market event, market events, in-session data gaps, invalid events,
  NewsGuard readiness at creation, AccountGuard events, and status (complete / interrupted / last
  recorded).

A field that cannot be known is `null` and displayed as Unavailable. It is never estimated.

## Metric definitions

| Metric | Definition |
|---|---|
| Net P&L | Sum of `trade_closed.net_pnl` (gross minus commission minus modelled slippage), simulated |
| Win rate | Trades with net P&L > 0 ÷ trades (a breakeven trade counts as a loss) |
| Expectancy | Mean net P&L per trade |
| Profit factor | Gross wins ÷ gross losses. **Undefined (null)** without losses, never "infinite" |
| Average R | Mean `r_multiple` as journaled (net P&L ÷ initial risk) |
| Max drawdown | Largest peak-to-trough decline of the cumulative closed-trade P&L sequence |
| MFE / MAE (R) | Best / worst excursion from entry to exit in 1-minute journal bars, divided by the stop distance. **Ex-post** |
| Signal → order | Order submission time − authorization decision time (0 by design: same event) |
| Order → fill | First execution time − submission time |
| Fill vs expected | (fill − order reference price) in ticks, positive = adverse |
| Stop slippage | (stop price − fill) in ticks, positive = adverse. "Gap through" fills are counted separately |
| Spread at signal / order / fill | Ask − bid of the last journaled quote at that moment. Unavailable without quotes (bar replay) |
| Ambiguous OHLC exit bar | The exit minute bar touched both stop and target, so the intrabar order is unknown |
| Data availability | (connected time − health blocks − in-session gaps) ÷ observed run time. The observed time includes closed-market hours |
| In-session gap | > 180 s without a valid event inside a trading session |

## Statistical methodology

- **Sample-size levels**: `< 30` trades is INSUFFICIENT SAMPLE SIZE, `< 100` is LIMITED SAMPLE, and
  ≥ 100 is "ADEQUATE SAMPLE (not proof of a future edge)". The level is shown next to every figure.
- **Win-rate interval**: Wilson score interval (95%). It assumes independent trades, so it is
  optimistic when trades are correlated.
- **Expectancy interval**: the **wider** of a Student-t interval and a **trading-day cluster
  bootstrap** (whole days resampled, seeded, `bootstrap_iterations` in `config/analytics.toml`).
  Day clustering partly accounts for same-day correlation, including between MES and MNQ.
- **Profit-factor interval**: day-cluster bootstrap percentile interval. It is null when it cannot be
  computed, for example with no losses in most resamples.
- **Caveats** are shown with every performance report:
  - trades overlap in time and MES/MNQ move together, so they are not independent;
  - browsing many breakdowns is a multiple-comparisons exercise;
  - fills are simulated;
  - results describe the recorded period only.
- **Breakdowns** (long/short, instrument, segment, weekday, regime) are exploratory. Subgroup
  intervals are not corrected for multiple comparisons.
- Results are **reproducible**: bootstrap seeds are fixed, and every row is traceable to its journal
  `seq` and identity.

## Live vs replay comparison

`compare(root, REPLAY, SHADOW)` covers **one instrument at a time**. It compares only observations
from runs that contributed data in scope, and it **refuses** when any of these differ or are
unknown:

- strategy configuration hash;
- regime identity;
- contract specifications;
- session-rules hash;
- signal timeframe.

When the sets are compatible, it compares distributions: signals per session, direction share, signal
hour, stop and target distances, trend strength, RSI, RVOL, VWAP and EMA relations, regime trend,
rejection reasons, trade duration, MFE, MAE and R. It never claims that a replay trade "should have
happened" in shadow. The periods and the inputs differ: replay fills come from OHLC paths, and shadow
fills come from live level-1 events.

## Execution quality

Every execution figure is labelled **SIMULATED (PaperBroker fills; not exchange executions)**. There is
no order-book or queue model. Spreads are UNAVAILABLE without quotes, as in bar replay, and the
report says so. Partial fills and rejected, expired and cancelled orders are counted. So are stop
fills, gap-through stop fills, target fills and ambiguous OHLC exit bars.

## Cost and execution scenarios

These are research only. They are kept separate from recorded results and never change any
configuration. Each scenario re-prices the **recorded** trades. Each trade's net P&L is reduced by
the extra ticks (slippage per side × 2, spread, extra stop slippage on stop exits) × tick value ×
quantity, plus any commission increase.

The default scenarios are:

- normal;
- wider spreads;
- higher slippage;
- volatility expansion;
- news volatility;
- delayed data;
- poor execution.

Each scenario reports net P&L, expectancy, PF, max drawdown, average R and an account-limit check
(minimum drawdown buffer, floor touches and daily-loss-limit days on closed-trade equity).

Trade selection is unchanged, so effects that would change **which** trades happen are not modelled,
for example AccountGuard blocking earlier.

## Market regimes

Regime analytics use **only** the regime engine's journaled records (`regime_decision`, Sprint 4). The
buckets are:

- strong trend, moderate trend, weak trend;
- range;
- high volatility, volatility expansion, volatility contraction;
- unfavorable, no-trade;
- unavailable / invalid.

Buckets overlap. A trade is bucketed by the regime record of its own signal. A trade without a
record, for example with the regime gate disabled, or with an invalid regime is **unavailable**. It
is never inferred from the strategy's own context fields, which are kept separately as
`strategy_context_trend`.

## Blocked signals and missed opportunities

- Every authorization block keeps its **exact reason code** and a category: NewsGuard, AccountGuard,
  RiskManager, Data health, Market regime, Session cutoff, Position limits, Duplicate prevention,
  Invalid stop / sizing, Run mode, System state or Other. Fill-time blocks are listed separately as
  "(at fill)".
- Strategy conditions not met (cooldown, invalid stop/target, session restrictions, missing data, other
  conditions) are counted by exact reason.
- **Counterfactual** (labelled **HYPOTHETICAL — never traded; not part of any P&L**):
  - entry at the open of the first 1-minute bar after the decision;
  - stop and target from the strategy's own signal;
  - the first level touched wins;
  - a bar touching both levels is AMBIGUOUS and is not counted as a win;
  - no costs and no fill check;
  - the horizon is `counterfactual_horizon_minutes`.

  Only the most recent 2,000 blocked signals are evaluated. Decided outcomes are cached because they
  depend only on past bars. These results never enter P&L, performance or sessions.

## Data reliability

The connection states are reported **separately**:

- provider connected;
- bridge connected;
- valid events received;
- data fresh;
- system operational.

The report also counts disconnections, reconnections, average recovery time, heartbeat failures,
queue overloads, subscription failures, delayed-data incidents, in-session gaps and invalid events by
reason.

**Exchange-level packet loss is not detectable.** NinjaTrader exposes no exchange sequence numbers.
Only bridge event-counter gaps can be seen.

## Account survival

The **configured account profile is authoritative**. Rules are read from `config/backtest.toml` and
its profile file. Prop-firm rules are not hardcoded. For a specific prop firm, such as a FundedNext
Futures Flex $50K account, create a profile file with that firm's current published rules and select
it. The repository ships only `generic_example`.

Reported figures:

- equity, remaining drawdown buffer and floor;
- max observed drawdown, safety reserve and flatten buffer;
- daily loss limit;
- consecutive losses;
- AccountGuard blocks;
- protection, flatten and emergency events.

**Stress scenarios** (`[[stress]]`) apply N consecutive losses (average recorded loss, or the
configured risk per trade, plus gap ticks) to the current buffer. They report how many losses the
safety reserve and the floor can absorb. A scenario whose basis is missing is UNAVAILABLE with the
reason. Stress results are illustrative: a simulated drawdown never guarantees a maximum real loss.

## Validation and walk-forward safeguards

The Validation section and `run_shadow_analytics.py validation` check:

- **Journal integrity**: hash chain verified; broken journals are excluded and listed (FAIL).
- **Duplicate journals**: a copied journal is the same run (same first-row hash). It is analysed once
  and flagged.
- **Session types not mixed**: every report needs one session type, and journals with several are
  flagged.
- **Run identity recorded**: pre-Sprint-8 journals have none and cannot be attributed (WARN).
- **Strategy identity stable within runs**: a configuration change splits the evidence (WARN).
- **Code reproducibility**: runs from uncommitted or unknown code are flagged.
- **Production strategy only**: TEST-strategy runs, such as the terminal end-to-end hooks, are flagged.
- **Chronology**: SHADOW sessions must be dated after all REPLAY sessions. Otherwise the shadow
  evidence is not forward evidence, and the check WARNs.
- **Out-of-sample ledger**: the Sprint 3 ledger is read only. Replay sessions inside **revealed** OOS
  periods are flagged. Sealed periods are unknown to this module and are never inferred or revealed.
- **Look-ahead**: decision analytics use journaled decision-time values only. MFE/MAE and
  counterfactuals are ex-post and labelled.

Passing every check does not prove an edge. It only means the evidence is not obviously contaminated.

## Terminal integration

There is a new screen, **Shadow Analytics**, with 10 sections:

- Sessions
- Live vs Replay
- Execution
- Performance (with cost scenarios and caveats)
- By Instrument
- By Regime
- Blocked Signals
- Data Reliability
- Account Survival
- Validation

Filters: session type (MOCK / REPLAY / SHADOW / MONITOR), MES/MNQ, date range, strategy version, regime,
provider, session.

Every figure is computed by the backend. When several session types are in view, Performance shows
"Choose one session type" instead of a mixed headline figure. Session-type badges use distinct
colours. There are no trading controls.

API (GET only, bearer token, Host/Origin checks, `heavy` rate limit, 422 on invalid parameters):

```
/api/shadow/{sessions|performance|costs|execution|regimes|blocked|reliability|account|validation|journals}
    ?root=MES|MNQ&start=YYYY-MM-DD&end=YYYY-MM-DD&session_type=SHADOW&strategy_version=..&regime=..&provider=..&session_id=..
/api/shadow/compare?root=MES&a=REPLAY&b=SHADOW
/api/shadow/export/{sessions|trades|execution|reliability|regimes|account|blocked|costs}.{csv|json}
```

Responses are `{"meta": {...provenance}, "report": {...}}`. The status codes are:

- 409: an export that would mix session types;
- 503: a broken analytics configuration;
- 500: an internal error, with a generic message (details only in the local log).

## Exports

CSV and JSON exports are bounded to 20,000 rows. The first CSV lines are provenance, commented with
`#`: report, generation time, filters, session type, label and limitations.

Every text cell starting with `= + - @ TAB CR` is prefixed with `'`, which prevents spreadsheet
formula injection. JSON has no NaN or Infinity (they become null).

No secrets are in exports. The order authorization tokens in the journal are never copied into any
report.

## CLI

```
python run_shadow_analytics.py sessions
python run_shadow_analytics.py report --session-type SHADOW --root MES
python run_shadow_analytics.py report --session-type REPLAY --section performance --section costs
python run_shadow_analytics.py compare --root MES                 # REPLAY vs SHADOW, compatible runs only
python run_shadow_analytics.py validation
python run_shadow_analytics.py export trades --session-type SHADOW --format csv --out output/analytics
python run_shadow_analytics.py sessions --journal data/analytics/journals/replay-20261122T100000/paper_state.db
python run_terminal.py --mode replay --keep-state                 # keep a replay run for analysis
```

Exit codes: 0 ok, 1 configuration or input error, 2 refused (for example mixed session types).

## Interpreting results

- Look at the **sample level first**. Below 30 trades, nothing is evidence.
- Read the **expectancy interval**, not the point estimate. An interval spanning zero means no
  demonstrated edge.
- Prefer **SHADOW** over REPLAY for execution questions. REPLAY fills follow synthetic OHLC paths.
- A breakdown that looks good is expected by chance when many are inspected.
- Cost scenarios show **fragility**. If "higher slippage" turns the result negative, the edge (if any)
  is thin.
- Counterfactuals only describe what blocked signals *might* have done without costs or fill checks.
  They do not argue for removing a guard.

## Limitations

- Fills are simulated (no queue position or order book). Replay spreads are unavailable.
- MFE/MAE and counterfactuals use 1-minute OHLC, so intrabar order is unknown.
- Analytics run inside the terminal process when used from the GUI. A long analysis shares the
  Python interpreter (GIL) with the paper session, which is why requests are serialized and rate
  limited. Use the CLI for heavy historical work.
- With a growing live journal, tables are recomputed when new rows arrive, at most every ~10 s from
  the GUI.
- Data availability includes closed-market hours. No exchange holiday calendar is used.
- Pre-Sprint-8 journals have no run identity or prices. They are analysed as UNKNOWN type with
  spreads unavailable.
- No exchange-level packet-loss detection is possible.
