# Sprint 5 paper trading — adversarial audit (2026-11-21)

Commit under review: `61a2a7a`. Scope: order execution, execution realism, authorization,
accounting, persistence and recovery, market data and reporting. Simulation only: no broker
connection, no live data and no real order path was added.

## Findings and fixes

| ID | Severity | Finding | Fix |
| --- | --- | --- | --- |
| H2 | HIGH | After a genuine move beyond `max_price_jump_ticks`, every later event was rejected for good. Marks froze and protective stops could not fire. | A new level is accepted after 3 consecutive consistent events. A single outlier is still rejected. The reset is journaled, and entries wait for fresh data. |
| H3 | HIGH | Market, stop and limit fills used the last quote however old it was. A minute-old ask could fill an entry 20 points away from the market. | `quote_max_age_seconds` (default 5). An older quote is never used; the trade price is used instead. |
| M1 | MEDIUM | Bar replay filled a next-bar entry at the bar's adverse extreme, and a down bar took a long's target before its stop. This produced the MNQ 2025-03-17 difference from the backtest. | The synthetic path repeats the open with zero volume. A stop and a target inside one bar resolve stop-first. The report counts synthetic executions. |
| M4 | MEDIUM | One unmarked instrument made equity NaN, which disabled AccountGuard for the whole account. | Known losses alone trigger the flatten. No peak and no breach are recorded from partial equity. |
| M5 | MEDIUM | `broker.submit` accepted entries that never went through `authorize_signal` (including in infrastructure-validation mode). Fill authorization had no mode or cutoff check. | One-time authorization token bound to the order terms. Fill-time checks for mode and session cutoff. |
| M2 | MEDIUM | The snapshot hash was plain SHA-256 even with a key, and not bound to the journal. A recomputed snapshot could reset a drawdown breach or a lock. Setting the key did not reject an unkeyed journal. | The snapshot hash binds the journal hash (HMAC when keyed). Breach, lock and high-water mark are reconciled against the journal. An unkeyed journal is refused when a key is set. |
| M3 | MEDIUM | Duplicate-event memory was not persisted, so an event at the last timestamp was processed twice after a restart. | IDs at the last timestamp are persisted per instrument. |
| M6 | MEDIUM | An exception in event processing ended the run without a safe shutdown or a journal record. | `system_error` is journaled, entries stop, then safe shutdown; CLI exit code 9. |
| M7 | MEDIUM | A restored position without a working stop was only reported. | The stop is re-created from the trade record, or the position is flattened. |
| H1 | LOW (latent) | Two exit orders on one event could both fill, because the position callback was stale within the event (a reversal). The runtime's flatten cancelled the stops first, so it was not reachable from the runtime. | The broker tracks the position across fills within an event. |
| L1 | LOW | An order was still working at its exact expiry time. | Expires at `>=`. |
| L2 | LOW | A closed trade's exit reason was the fill rule (`market_at_trade_no_quote`), not the cause (`session_end`). Open positions and synthetic fills were not stated in the report. | Fixed and reported. |

## What the unkeyed journal can and cannot detect (`FTS_JOURNAL_KEY` absent)

- **Detected:** accidental corruption, truncated, missing or reordered rows, and an edited row
  whose chain was not recomputed. A snapshot not bound to this journal (moved, stale or edited
  without recomputing) is also detected.
- **Not detected:** a deliberate rewrite that recomputes every hash, of the journal or of a
  snapshot. Anyone with write access can do that. Reconciliation then compares the snapshot
  with the journal's executions, guard events and account updates. A consistent rewrite of both
  cannot be detected without the key.
- **Not chained:** the `bars` table. It only seeds indicators and never touches balances or
  positions.

## Verified unchanged

- NewsGuard: operational in paper mode; an incomplete or stale calendar blocks every entry.
- AccountGuard, RiskManager and OOS protections: no rule was relaxed.
- `PaperBroker.TRANSMITS_REAL_ORDERS = False`. There is no network code in `src/paper`, and
  `UnconfiguredLiveFeed` refuses to connect.
