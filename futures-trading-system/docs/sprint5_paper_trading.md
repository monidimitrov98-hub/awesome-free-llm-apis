# Sprint 5 — Paper trading infrastructure

**Simulation only.** No module in `src/paper/` can open a network connection or send an order.
A static test (`test_no_real_order_transmission_possible`) enforces this for the package and
the CLI.

## Reuse (nothing rebuilt)

| Existing component | Used for |
| --- | --- |
| `VwapTrendPullbackStrategy`, `RegimeGatedStrategy`, `RegimeEngine` | Decisions, unchanged; the same `StrategyContext` as the backtest |
| `resample_ohlcv`, `add_indicators`, runner `_regime_frames` | Frames at every signal boundary from completed 1-minute bars (bitwise identical to the backtest for the same history) |
| `NewsGuard` (operational in paper mode, research in replay), `AccountGuard`, `RiskManager` | Every authorization and protection decision |
| `position_size`, `risk_per_contract`, `Position`, slippage and tick-rounding helpers, `BacktestConfig` | Sizing, accounting, costs (same configuration as backtests) |

The only change to existing code is `relative_volume_time_of_day`, which is now vectorized. It
is bitwise identical on MES/MNQ at 1, 5 and 15 minutes, and about 7× faster.

## Modules (`src/paper/`)

| Module | Role |
| --- | --- |
| `events.py` | Trade and quote events (UTC), strict parsing |
| `feeds.py` | `BarReplayFeed` (1-minute bars to synthetic trade paths, labelled), `TickReplayFeed` (recorded trades/quotes), `SimulatedRealtimeFeed` (clock-paced, fault injection), `LiveFeed` interface plus `UnconfiguredLiveFeed` (no live provider exists) |
| `validation.py` | `EventValidator` (duplicates, out-of-order, tick grid, crossed quotes, jumps, unknown instrument), `DataHealth` (connected / stale / gap / recovery pending), `SessionCalendar` (DST-aware session dates) |
| `bars.py` | `BarBuilder` (a minute is complete only once time has passed it), `build_frames` |
| `orders.py` | `PaperOrder` with an enforced lifecycle, and `Execution` records |
| `broker.py` | Paper execution engine (fill rules below) |
| `account.py` | Paper account with the existing AccountGuard and RiskManager inside |
| `authorization.py` | The only path that creates entry orders (signal-time and fill-time gates) |
| `journal.py` | SQLite journal: hash-chained (optional HMAC), atomic batches, snapshots |
| `runtime.py` | `PaperSession`: the event loop, protections, recovery, safe shutdown |
| `metrics.py` | Reports separated by run kind |
| `config.py` | `config/paper.toml` (it cannot change risk or account rules) |
| `run_paper.py` | The CLI |

## Data flow

```
feed.poll() -> EventValidator --rejected--> journal "data_rejected"
                    | ok
               DataHealth -> journal "market_event"
                    v
   PaperBroker.on_event (working orders vs this event; entry fills re-authorized) -> executions
                    v
   PaperAccount (positions, PnL, costs) -> AccountGuard / RiskManager / session-end checks
                    |  (flatten = cancel entries + market exits; exits are never blocked)
                    v
   BarBuilder (completed 1m bars; the clock completes them in real time) -> signal boundary?
                    v
   build_frames -> StrategyContext (history and context cut at available_at <= now)
                    v
   strategy.on_bar (wrapped by RegimeGatedStrategy) -> OrderRequests
                    v
   EntryAuthorizer -> PaperBroker.submit, or a rejection record with a machine-readable reason
```

Each batch of events is one SQLite transaction: its journal rows, completed bars and a state
snapshot are committed together.

## Entry authorization sequence

**At the signal:**

1. run mode
2. system state (reconciliation, persistence, shutdown, restart block)
3. market-data health
4. request validity (a stop-loss is required when AccountGuard is on)
5. position / pending entry (reversals only if `allow_reversal`)
6. NewsGuard (operational in paper mode)
7. sizing and bracket validity
8. session cutoff
9. AccountGuard
10. RiskManager

The regime gate runs before all of this, inside the strategy wrapper.

**At the fill** (before anything is executed): system state, NewsGuard at the fill time,
bracket and fill-quality limits at the actual fill price, risk budget (reduce or reject),
AccountGuard with the actual trade risk plus the gap reserve, RiskManager. Any failure rejects
the order; a rejection is never converted into a fill.

The backtest asks RiskManager before AccountGuard; paper asks AccountGuard first, as specified.
Both are independent vetoes, so the outcome is the same and only the first recorded reason can
differ.

## Order lifecycle and fill rules

**States:** `accepted → (triggered) → partially_filled → filled`; `cancelled`, `rejected` and
`expired` are terminal. Transitions are enforced, and an impossible transition raises. Every
order has a unique id and a timestamped history.

**Fill rules:**
- **No same-event fills.** An order fills only on an event with a higher sequence number than
  the one it was created on.
- **Market.** At the ask (buy) or bid (sell), or at the trade price when no quote is known
  (flagged `quote_based = False`), plus `slippage.market_ticks`. A quote older than
  `quote_max_age_seconds` is never used to price any fill; the event's trade price is used instead.
- **Stop / stop-loss.** Triggered by a trade at or through the stop, or by the quote reaching
  it. Fills at the worse of the stop and the market (gaps fill at the market), plus
  `slippage.stop_ticks`.
- **Limit / take-profit.** A marketable limit takes the quote. A resting limit fills only when
  the market trades or quotes *through* it; a touch is not enough. It fills at the limit, for
  at most `limit_participation` × the trade (or quote) size, so partial fills happen.
- **Brackets (OCO).** An entry fill creates or extends a stop-loss and take-profit. A partial
  exit shrinks the sibling; a complete exit cancels it. Exits never over-close: the broker
  tracks the position across all fills of one event.
- **Entries need an authorization token.** The broker accepts an entry order only if the
  EntryAuthorizer issued it, with unchanged terms, once. A direct `broker.submit` of an entry is
  refused. Fill-time authorization also re-checks the run mode and the session-end cutoff.
- **Synthetic bar paths.** When a stop and a target both lie inside one synthetic bar, the stop
  is assumed to fill first (worst case).

**Expiry:** market entries expire after `execution.max_fill_delay`; limit and stop entries
expire at the session-end cutoff (an order is expired at, not after, its expiry time).

Every execution records the requested price, reference (pre-slippage) price, fill price,
quantity, timestamp, slippage ticks and cost, commission, order type, reason and source event.

## Protections

- **AccountGuard** (existing class) is evaluated on every event. Flatten or breach triggers
  market exits and locks entries; flattening is never blocked by NewsGuard. While an open
  position has no mark (full equity unknown), the known losses alone still trigger the flatten
  (`account_protection_partial_equity`); no peak and no breach are recorded from partial data.
- **RiskManager** (existing class) enforces daily limits, size, trading windows, unrealized
  daily loss and the emergency stop.
- **Session end.** Pending entries are cancelled and positions closed at `flatten_before_session_end`.
- **No guarantee.** Simulated exits fill on the next real event; a real market exit can fill
  worse. Simulated flattening does not guarantee any maximum loss.

## Persistence and recovery

**Storage** is `paper_state.db` (SQLite, WAL, `synchronous=FULL`, owner-only permissions):
- an append-only journal with `hash = H(prev, seq, ts, kind, payload)`. H is HMAC-SHA256 with
  `FTS_JOURNAL_KEY`, otherwise SHA-256 (which detects accidents, not a determined editor);
- snapshots whose hash binds them to the journal hash at the snapshot point (HMAC when keyed).
  With `FTS_JOURNAL_KEY` set, an unkeyed journal is refused, which blocks a downgrade;
- completed bars.

**A failed commit** keeps the pending batch for retry (nothing is lost) and blocks new entries
until a later commit succeeds.

**On restart:**
1. Verify the chain.
2. Require that the latest snapshot sits at the end of the journal.
3. Restore account and AccountGuard (peak, locks, breach — never reset), RiskManager state,
   orders and positions.
4. Reconcile positions, realized PnL, commissions and order fills against the journal's
   executions. Also check that every position has a working stop and that the AccountGuard peak
   is not below the initial balance. Also check that no drawdown breach, permanent lock or
   high-water mark recorded in the journal was lost. Any mismatch blocks new entries.
5. Cancel working entry orders. Protective orders stay working. A position without a working
   stop gets its stop-loss re-created from the trade record, or is flattened if no stop is known.
6. Block new entries for the rest of the session in which the restart happened, because the
   strategy's in-memory state cannot be verified.
7. Never restore a price as live.

**Refusals:**
- A missing account is never recreated silently.
- `--new-account` never overwrites an existing one.
- A stored account of another run kind is refused.

## Failure handling

| Failure | Behaviour |
| --- | --- |
| Disconnect | Journaled. Reconnect with back-off; after the attempts run out, safe shutdown. After reconnect, entries resume only after `resume_after_events` fresh events |
| Stale data / data gap | Entries blocked (`market_data_stale`, `data_recovery_pending:*`) |
| Invalid, duplicate or out-of-order events | Rejected and journaled, never used. Duplicates of the last timestamp stay duplicates after a restart |
| Price jump beyond `max_price_jump_ticks` | Rejected; three consecutive consistent events confirm a new level (journaled `price_level_reset`), after which entries wait for fresh data |
| Internal error | Journaled `system_error`; entries stop; safe shutdown; CLI exit code 9 |
| Unknown marks | Equity unknown: entries blocked (`account_equity_unknown`) |
| NewsGuard not ready | Entries blocked with `news_<state>` |
| Persistence failure | Entries blocked until a commit succeeds |
| Reconciliation failure | Entries blocked |

**Safe shutdown:** no new entries, pending entries cancelled. Positions are either kept with
their protective orders (default) or flattened on the next real events. State is persisted.

## Run kinds and reports

- `infrastructure_validation` never authorizes entries and reports no trading statistics.
- `replay` and `simulated_realtime` are labelled as such.
- `paper` requires a live feed, and none exists yet.

Rejected opportunities are counted separately from trades.

**Reports cover:**
- signals, accepted/rejected entries and rejection reasons;
- orders and fill rate;
- win rate, average win/loss, profit factor, expectancy, average R;
- maximum drawdown, gross/net PnL, commissions, slippage;
- results by instrument, regime, RTH/ETH segment and session date.

## Current NewsGuard situation

The manual calendar is intentionally unresolved, so readiness is not OPERATIONAL READY.
`run_paper.py --mode paper` prints the readiness verdict, and the operational NewsGuard blocks
every new entry (`news_calendar_incomplete` / `news_calendar_stale`). Market data, indicators
and signals are still processed and recorded.

## Known limitations

- **No live provider.** There is no live market-data provider (interface only). Paper mode can
  only run on the simulated real-time replay.
- **Bar replay is approximate.** It turns bars into an approximate 5-trade path per minute
  (open, open again one second later with no volume, one extreme, the other, close) without
  quotes. Fills are therefore trade-based and flagged as synthetic, and the report counts them.
- **Fills and the backtest.** A decision on a bar's open print fills on the repeated open, which
  is the backtest's next-bar-open assumption. The sample's MNQ 2025-03-17 trade now matches the
  backtest's entry, exit and net PnL. The earlier difference came from the synthetic path, not
  from the market. Paper "gross" PnL is after slippage, while the backtest's is before it; net
  PnL is comparable.
- **No depth model.** Market and stop orders fill the full micro quantity; there is no queue
  position model for limits beyond the trade-through and participation rules.
- **Restart cost.** The strategy's in-memory state is not persisted, hence the restart-session
  entry block.
- **Speed.** Indicators are recomputed on the full history at every 5-minute boundary: about
  0.1 s per boundary with the regime engine. That is fine in real time, but replays are slow
  (about 40 s per session per instrument with the regime engine).
- **Tamper evidence.** Without `FTS_JOURNAL_KEY`, the chain detects accidental corruption, not
  deliberate rewriting.
