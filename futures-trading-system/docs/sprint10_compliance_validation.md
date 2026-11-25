# Sprint 10: Compliance validation, adversarial testing and shadow verification

Sprint 10 is simulation and compliance tooling only. No order can reach a broker. The NinjaTrader
bridge stays read-only. No prop firm profile was activated, and none can activate itself.

**A passing test suite does not establish real-world prop firm compliance.**

## Workstream A — FundedNext Futures Flex $50K re-verification

### How the evidence was retrieved

| Attempt | Result |
|---|---|
| Direct HTTPS from the build container | refused by the environment proxy (`CONNECT` 403) |
| Hosts `helpfutures.fundednext.com` and `help.fundednext.com` added to the environment's allowed hosts | the direct download was then refused by the session's permission policy. This was **not** bypassed. |
| Page-fetch service | could not resolve `helpfutures.fundednext.com` (`ENOTFOUND`) |
| FundedNext official knowledge-base search (excerpts with article URLs), 2026-11-25 | answered parts of the questions; see below |

The full help-center pages remain **unread**. No effective dates appeared in any excerpt. The
evidence, with exact excerpts and URLs, is in
`config/prop_firms/sources/fundednext_futures_2026-11-25.toml`.

### The eight open points

| # | Point | Evidence | Status |
|---|---|---|---|
| 1 | Exact EOD trailing time | excerpt still stops at "updates once per day at the end of…" | UNVERIFIED |
| 2 | Equity exactly at the limit | only the daily-loss-limit text says "equity reaching $50,500 or lower"; nothing MLL-specific | UNVERIFIED (engine: conservative *at or below*) |
| 3 | Holiday / early-close schedule | "communicated to traders in advance" (S10); announced by email (S16) | NOT_FOUND |
| 4 | Daily profit for consistency | examples only ("keep daily profits below $1,000"); no net/gross or realized/open definition | UNVERIFIED |
| 5 | Benchmark Day definition | the Futures excerpt is cut off. A **CFD** article defines it on closed-trade net profit; that is not applied to Futures. | UNVERIFIED |
| 6 | Cycle reset after withdrawal | "after every 5 benchmark days" (S11); "in the cycle" (Legacy) | UNVERIFIED |
| 7 | MLL and payouts | **new conflict**: "Locks at $50,100 after first payout" / "The first withdrawal resets the MLL back to $50,100" (S11) vs "the MLL locks at your initial balance (initial + $100 for Flex…" (S4) | **CONFLICTING** |
| 8 | Challenge / Funded / Road to Live | the live stage has its own MLL ("Starts from $0"), locks on a payout request and doubles the contract limits after the lock; Flex FundedNext accounts move to live after withdrawals | documented; live stage not applied |

**Consequence of point 7.** If the funded MLL keeps trailing above $50,100 until the first payout,
the Sprint 9 model, which capped the floor at $50,100, would be **looser than the firm**. This was
reproduced on the Sprint 9 code: after an end-of-day balance of $52,000, the model floor was
$50,100, while the firm's could be $50,500.

New profile versions **`2026-11-25.1`** were created for both the Challenge and the Funded stage.
The `2026-11-22.1` files are unchanged. The new versions:

- mark the lock `lock_verification = "CONFLICTING"` and add the safety-critical rule
  `FNF-FLEX50-MLL-LOCK`;
- in the Funded version, record the payout reset (`FNF-FLEX50-PAYOUT-MLL-RESET`) and the cycle
  ambiguity (`FNF-FLEX50-CYCLE`).

The engine now applies a lock level **only when the loss rule and its lock are both VERIFIED**.
Otherwise the floor keeps trailing, which is stricter under every interpretation.
`run_prop_firm.py diff` classifies the change as STRICTER, with no potentially unsafe change.
Both new versions remain **UNVERIFIED and non-operational**. No activation was performed.

## Workstream B — adversarial audit

Each defect was reproduced against the unmodified Sprint 9 code (commit `3f51132`, checked out in
a separate worktree), fixed, and covered by a regression test. Tests are in
`tests/test_sprint10_adversarial.py` and `tests/test_sprint10_shadow.py`.

| Id | Severity | Defect (reproduced) | Fix |
|---|---|---|---|
| A-lock | MEDIUM | An unverified $50,100 lock could leave the modelled floor below the firm's | the lock is applied only when VERIFIED; new profile versions |
| B1 | LOW | Rule and source ids with newlines or control characters were accepted (reason codes, journals, exports) | ids must match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`; source URLs may not contain whitespace, quotes or angle brackets; mandatory rules need a description |
| B2 | MEDIUM | With AccountGuard disabled and an unmarked position (equity unknown), the prop engine requested no protection at all | flatten (`prop_firm_max_loss_protection_partial_equity`) when the *known* losses reach the flatten reserve; no breach is recorded from partial data |
| B3 | MEDIUM | After a human revocation, the status view kept showing "VERIFIED + ACTIVATED" until the next real entry evaluation | `refresh_operational()` on every status, readiness and decision path |
| B4 | MEDIUM | `/api/propfirm/history` re-verified and parsed the whole journal on every request (DoS on long shadow journals) | `ComplianceJournalReader`: incremental chain verification and bounded buffers, one reader per terminal |
| B5 | LOW | Prop firm blocks were counted as "Other" in Shadow Analytics | new category "Prop firm compliance" |
| B6 | LOW | Validation had no recovery-integrity check, so an interrupted run looked clean | "recovery integrity" check: reconciliation problems or an unrecovered persistence failure are FAIL; unclean interruptions and system errors are WARN |
| B7 | MEDIUM | Activation store without integrity: re-appending an old "activate" line after a revocation re-activated the profile, and edits or reordering went unnoticed | records chained (`seq`, `prev`, `hash`), HMAC with `FTS_JOURNAL_KEY` when set; a broken chain approves nothing and is never extended; cross-process `flock` |
| B8 | MEDIUM | `TerminalService.start()` was not idempotent: a second start (e.g. an API lifespan on a running service) built a second PaperSession on the same state directory (two journal writers) | `start()` runs once per service under a lock; later calls are refused and logged |

### Scenarios covered, all passing after the fixes

**Drawdown and equity:**
- sudden collapse (breach plus flatten in one event);
- floating losses crossing the floor;
- equity exactly at the limit and just below (`at_or_below` vs `below`);
- EOD trailing and lock;
- high-water rollback (state below the journal, below the initial balance, a lost breach);
- a wrong initial balance;
- the trading-day boundary at 17:00 CT;
- DST (March and November);
- missing events before the new trading day.

**Execution and positions:**
- duplicate and out-of-order executions;
- duplicate market events (validator rejection, no double fill);
- partial fills at fill time (the order's own remaining quantity is not double-counted);
- pending entries;
- oversized quantities;
- unknown instruments;
- mixed mini/micro exposure;
- strategy-requested quantities;
- a fill arriving after the entry cut-off.

**Persistence and recovery:**
- a corrupted journal (start refused);
- a missing compliance state;
- a hash-chain mismatch;
- rollback;
- a lost breach;
- restart mid-day (daily counters, high-water mark and execution ids preserved);
- recovery after duplicates;
- profile removal with history;
- an interrupted shadow session (recovery integrity WARN, results INCOMPLETE, no duplicated
  sessions).

**Profile security:**
- substitution;
- stale versions (a v1 approval never covers v2);
- SHA-256 mismatch;
- revocation;
- 4 concurrent writers × 10 activation changes (chain intact);
- path traversal (profile keys, holiday calendars);
- malformed TOML;
- unknown rule types;
- missing metadata;
- injected ids and URLs;
- unverified safety-critical rules.

**Concurrency and API:**
- 6 threads × 200 simultaneous entry decisions (consistent results);
- concurrent observe and evaluate;
- unauthorized requests;
- client-injected balance, equity, status or operational flags (ignored);
- no absolute paths in responses;
- bounded readers and buffers.

**AccountGuard and NewsGuard:**
- AccountGuard disabled or looser (warning, plus the B2 protection);
- AccountGuard rejecting at the fill while the operational profile would allow the trade (the prop
  stage is never reached);
- NewsGuard with an incomplete calendar (fails closed before the prop stage);
- NewsGuard raising (session stops with `system_error`, nothing authorized);
- a restricted news window and NewsGuard state unavailable (prop news rule fails closed).

## Workstream C — shadow verification

**Data integrity (verified, existing):** the EventValidator rejects missing, future-stamped,
duplicate, out-of-order, off-tick-grid, crossed, too-wide or non-finite events, and implausible
jumps. It flags out-of-session events and fabricates nothing. Journal bars have the primary key
(root, ts), so duplicate bars are impossible. Gaps longer than 180 s inside a session are reported.

**Execution realism (verified, existing):**
- commissions per contract per side;
- configurable slippage (market, stop, limit);
- quotes used when fresh, with the bid/ask spread at signal, order and fill;
- limit participation (partial fills);
- fill timing;
- rejected simulated executions (fill-time authorization).

Nothing is invented: without quotes, spreads are UNAVAILABLE.

**New: reproducible session report.** It is produced by `run_shadow_analytics.py session-report`,
`GET /api/shadow/session_report` and `src/analytics/shadow/session_report.py`. Contents:

- **identity and provenance**: provider, data label, strategy version, configuration hash, code,
  regime, risk hash, account profile, prop firm profile and rule set;
- **execution assumptions**: costs, slippage, contracts;
- **signal counts**: total, accepted, rejected;
- **performance**: simulated trades, win rate with CI, gross and net P&L, fees, slippage, profit
  factor, average win and loss, expectancy with CI, maximum drawdown;
- **Sharpe and Sortino**: only with at least 20 trading days with P&L, otherwise "not meaningful";
- **exposure**: time in market, share of observed time, maximum concurrent contracts;
- **blocks by protection layer**, at the signal and at the fill;
- **realized vs unrealized vs simulated**, including open positions at the end;
- **data-quality warnings**;
- **a completeness verdict**: COMPLETE only when no validation, recovery, identity or open-position
  problem exists. Otherwise INCOMPLETE, with reasons. The CLI exits with 3 when INCOMPLETE.

**New: decision trace.** It is produced by `run_shadow_analytics.py trace`,
`GET /api/shadow/trace?run_id=&seq=` and `ShadowAnalytics/trace`. For any entry decision it shows:

- the identity in force (strategy version and configuration, risk, prop profile and rule set, code,
  session rules);
- the three preceding market events of the instrument;
- the strategy signal and regime record;
- every authorization stage with its exact reason;
- the order, fill authorizations, executions and closed trade.

**Recovery:** interrupted sessions recover with the compliance state intact, and analytics never
report them as clean.

## Workstream D — integration and operational readiness

The complete path is tested on both an accepted and a rejected signal:

> market data → strategy → system / NewsGuard → AccountGuard → prop firm → RiskManager →
> simulated execution → journal → analytics → terminal

See `test_every_decision_traces_to_identity_market_data_and_outcome` and the `prop-firm`
Playwright scenario.

**New: `GET /api/readiness` and the Dashboard "Operational readiness" panel.** The checklist covers:

- session, persistence, reconciliation;
- market data per instrument;
- AccountGuard, NewsGuard per instrument;
- run mode;
- prop firm profile;
- data provenance;
- shadow results completeness.

The verdict is one of:

- **BLOCKED**: any blocking check;
- **INCOMPLETE**: any warning or unknown evidence;
- **OPERATIONAL (SIMULATION)**: every check passed.

It is **never "SAFE"**. A separate `continue_simulation` flag says whether protections and
persistence are intact.

## Security review (final)

Areas covered: authentication, HMAC, activation, SHA-256 binding, path traversal, replay,
concurrency, journal tampering, rollback, API and frontend trust boundaries, secrets, DoS and
fail-closed behaviour.

| Severity | Count | Items |
|---|---|---|
| CRITICAL | 0 | none found |
| HIGH | 0 | none found |
| MEDIUM | 6 | A-lock, B2, B3, B4, B7, B8 (all fixed) |
| LOW | 3 | B1, B5, B6 (all fixed) |
| INFORMATIONAL | 6 | see below (not fixable in scope or accepted design) |

Informational:

1. Without `FTS_JOURNAL_KEY`, both the journal and the activation chain are tamper-*evident* but
   can be recomputed by someone with write access to the files. Set the key for real integrity.
2. A whole-file rollback (an older copy of `paper_state.db` replacing the current one) cannot be
   detected without an external anchor.
3. Analytics and readiness run in the terminal process: they share the Python GIL with the paper
   session. They are rate-limited and incremental, and heavy work belongs in the CLI.
4. FastAPI 422 responses echo the rejected parameter value. These are local, authenticated
   responses and contain no secrets.
5. The session token is printed to the local console at start (Sprint 7 design). It is never
   logged or written to disk.
6. Flat-by and session-end flattening are event-driven. If the feed stops delivering events, they
   happen at the next event (the firm also auto-closes at 3:10 PM CT).

No secrets, tokens or credentials were added. No new network listener was added. Every new
endpoint is GET-only behind the bearer token and the Host/Origin checks.

## Remaining limitations

- The FundedNext rules come from knowledge-base excerpts. Five points are UNVERIFIED, one is
  NOT_FOUND and one is CONFLICTING. A human must read the full official pages before any
  activation.
- Withdrawals, cycle resets and the MLL reset on a payout are not simulated.
- Sharpe and Sortino need at least 20 trading days with P&L. Bootstrap intervals assume the
  recorded days are representative.
- The backtest engine does not yet call the prop firm compliance engine; see
  `docs/sprint10_quant_integration_assessment.md`.
