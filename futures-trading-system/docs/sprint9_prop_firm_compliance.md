# Sprint 9: Prop firm account protection and compliance engine

This is compliance and simulation tooling. It decides whether a **new simulated entry** complies
with a configured prop firm profile, and it watches the account against that profile. It sends no
orders, has no broker methods, never changes an account at a firm, never submits payouts, and
never activates a profile by itself.

**A passing test suite does not establish compliance with a firm's current rules.** Software
guards cannot guarantee a maximum real-world loss. Gaps, outages and fills worse than modelled can
exceed any reserve.

## 1. What existed before (Sprints 1–8)

| Protection | Where | Kept as is |
|---|---|---|
| AccountGuard: static / trailing EOD / trailing intraday floor, safety and flatten buffers, locks, trade risk within buffer, gap reserve | `src/risk/account.py` | yes, still the **survival authority** |
| RiskManager: daily loss (realized and unrealized), daily trades, size, windows, consecutive losses, emergency stop | `src/risk/manager.py` | yes |
| NewsGuard: event calendar, blackout windows, fail-closed readiness | `src/risk/news/` | yes; prop firm news rules use its state and never duplicate its calendar |
| Entry authorization pipeline (signal and fill time), one-time approval tokens | `src/paper/authorization.py` | extended with one stage |
| Hash-chained journal and snapshots, recovery and reconciliation | `src/paper/journal.py`, `runtime.py` | extended (compliance state and records) |

Before this sprint, no firm-specific rules existed. The repository shipped only the generic
AccountGuard profile `generic_example`.

## 2. Official FundedNext Futures Flex $50K research

### Retrieval

Retrieved **2026-11-22** from FundedNext's **official knowledge base**: the help-center FAQ,
queried through FundedNext's own knowledge-base search. Each result returned the article title, a
text **excerpt** and the article URL.

The help-center pages themselves (`helpfutures.fundednext.com`) could **not** be opened from the
build environment because the network policy blocked the host. Only excerpts were read, and some
excerpts are cut off.

No effective dates were shown in the excerpts. The excerpts and sources are recorded in
`config/prop_firms/sources/fundednext_futures_2026-11-22.toml`.

### Sources (all `https://helpfutures.fundednext.com/en/articles/...`)

| Id | Title |
|---|---|
| S1 | FundedNext Futures Flex Challenge All Rules (`17229829`) |
| S2 | FundedNext Futures Flex FundedNext Account All Rules (`17229848`) |
| S3 | What is FundedNext Futures Flex Challenge? (`17229807`) |
| S4 | Daily Loss Limit and Maximum Loss Limit at FundedNext Futures (`17230292`) |
| S5 | Contract Limits and Contract Types at FundedNext Futures (`17230194`) |
| S6 | Does FundedNext Futures allow overnight and weekend trade holding? (`14268506`) |
| S7 | Are there any news trading rules for FundedNext Futures? (`14298245`) |
| S8 | Is there any consistency rule … Challenge & FundedNext Account? (`17230245`) |
| S9 | Which symbols/instruments can I trade on FundedNext Futures? (`17225740`) |
| S10 | What Are the Trading Hours in FundedNext Futures? (`17225754`) |
| S11 | Performance Reward eligibility criteria for Flex FundedNext Account (`14878865`) |
| S12 | What are Benchmark Days at FundedNext Futures? (`17230209`) |
| S13 | Profit target in the FundedNext Futures Flex Challenge (`14878840`) |
| S14 | Road To Live Trading - Flex Challenge (`15430139`), live-account stage, **not applied** |
| S15 | Prohibited trading strategies of FundedNext Futures (`14298337`) |

### Rules

Account type: Flex Challenge (single phase) and Flex FundedNext Account, $50,000.

| Rule id | Rule (as stated in the excerpts) | Stage | Sources | Status |
|---|---|---|---|---|
| FNF-FLEX50-MLL | Maximum Loss Limit **$1,500**. Trailing end-of-day on the balance; never moves down; **locks at $50,100**. Floating losses breach it intraday (permanent). | both | S1 S2 S3 S4 | VERIFIED |
| FNF-FLEX50-MLL-EOD | Exact EOD update time, and whether equity exactly at the MLL is a breach | both | S1 S4 | **UNVERIFIED** (excerpt cut off) |
| FNF-FLEX50-NO-DLL | No daily loss limit | both | S1 S2 | VERIFIED, not applicable |
| FNF-FLEX50-CONTRACTS | Max **3 e-mini / 30 micro** held at once, mixing 1:10. The firm deducts profit from excess contracts. | both | S1 S5 | VERIFIED (engine blocks instead: stricter) |
| FNF-FLEX50-INSTRUMENTS | CME equity futures incl. **MES, MNQ** (ES, NQ, RTY, M2K) | both | S9 | VERIFIED |
| FNF-FLEX50-FLAT-BY | No overnight or weekend holding; flat by **3:10 PM CT** (firm auto-closes) | both | S6 | VERIFIED |
| FNF-FLEX50-HOURS | Trading resumes **5:00 PM CT** (Sunday 5:00 PM CT) | both | S6 | VERIFIED |
| FNF-FLEX50-HOLIDAYS | Holiday and early-close hours are "communicated in advance"; auto-close may not apply at early closes | both | S10 | **NOT_FOUND** (no schedule retrieved) |
| FNF-FLEX50-NEWS | **No** news trading rule for FundedNext Futures | both | S7 | VERIFIED, not applicable (NewsGuard stays on) |
| FNF-FLEX50-CONSISTENCY | Profit on a single day ≤ **40%** of the profit target; otherwise the target is recalculated (highest day / 0.40). Not a breach. | challenge | S1 S3 S8 | **UNVERIFIED** (numerator definition) |
| FNF-FLEX50-NO-CONSISTENCY | No consistency rule | funded | S2 S8 | VERIFIED, not applicable |
| FNF-FLEX50-TARGET | Profit target **$2,500**; no time limit | challenge | S13 | VERIFIED (info) |
| FNF-FLEX50-MIN-DAYS | Minimum trading days | challenge | none | **NOT_FOUND**: none assumed |
| FNF-FLEX50-BENCHMARK-DAYS | **5 Benchmark Days of $200** before each withdrawal | funded | S2 S11 S12 | **UNVERIFIED** (Benchmark Day definition cut off) |
| FNF-FLEX50-MIN-PROFIT | Minimum profit **$500** in the current cycle | funded | S2 S11 | VERIFIED |
| FNF-FLEX50-MIN-WITHDRAWAL | Minimum withdrawal **$250** | funded | S2 S11 | VERIFIED |
| FNF-FLEX50-MAX-WITHDRAWAL | Up to **50%** of profit, capped at **$1,500** | funded | S11 | VERIFIED |
| FNF-FLEX50-PROHIBITED | Prohibited practices (spoofing, layering, multi-order spam, …) | both | S15 | VERIFIED (info; not machine-checkable) |

### Unresolved points and conflicts

These block activation:

1. **MLL end-of-day time and boundary.** The excerpts stop at "updates once per day at the end
   of…". The engine trails at its trading-day start (5:00 PM CT, after the mandatory 3:10 PM CT
   flat time, so the balance is final). It treats equity *at* the MLL as a breach, which is the
   conservative choice.
2. **Holiday / early-close schedule.** None was retrieved. Without
   `config/prop_firms/holidays/fundednext_futures.toml` covering the date, new entries fail
   closed.
3. **Consistency numerator.** Whether "daily profit" is net of fees, realized only, and per
   trading day is not stated.
4. **Benchmark Day definition and cycle reset after a withdrawal.** Not confirmed.
5. **Live-account stage (S14) differs.** It gives other contract limits after the MLL locks, and
   says the MLL locks when a payout is requested. It is not applied to the Challenge or Funded
   profiles. If the funded MLL locked at a payout request, the real floor would be *lower* than
   modelled here, so this model is the more conservative one.

Both profiles are therefore **UNVERIFIED and not operational**. Selecting one **blocks every new
entry** (`prop_profile_not_operational`) while still showing the full evaluation. Nothing here
approves any live or funded account.

## 3. Universal profile architecture (`src/risk/prop_firms/`)

| Module | Role |
|---|---|
| `profile.py` | Profile data model and strict TOML loader. Unknown keys are refused, VERIFIED rules must cite sources, https-only sources, and types and parameters are validated. Computes status (DRAFT / INCOMPLETE / UNVERIFIED / VERIFIED / EXPIRED / DISABLED), the profile SHA-256 and the rule-set SHA-256. |
| `versions.py` | `Registry` (every version kept at `config/prop_firms/profiles/<id>/<version>.toml`), `Activations` (append-only human approval records in `data/prop_firms/activations.jsonl`, bound to the exact SHA-256), `operational_state`, `load_selection` (exact id + version, never "latest"), `diff` (unsafe-change flags), `affected_accounts`. |
| `engine.py` | `ComplianceEngine`: generic rule evaluators, drawdown models, trading-day calendar, monitoring actions, state persistence and integrity checks, display classification. |
| `payout.py` | Read-only payout eligibility tracker. |
| `authority.py` | The explicit hierarchy and the unified (reporting) decision. |
| `reports.py` | Auditable reports from the chain-verified journal. |
| `runtime.py` | Builds an engine from an explicit selection. |

Profiles are data. The engine contains no firm-specific code. Rule types:

- `max_loss`
- `daily_loss`
- `max_contracts`
- `instrument_allowlist`
- `flat_by`
- `trading_hours`
- `news_restriction`
- `holiday_schedule`
- `consistency`
- `profit_target`
- `min_trading_days`
- `payout_*`
- `other` (an unresolved safety-critical `other` rule fails closed)

Every rule carries:

- id, type and description;
- applicability (`applicable`, `stages`);
- `mandatory`, `safety_critical` and `severity`;
- `verification`, `sources`, `effective` and `ambiguity`;
- `params`.

**Operational** means the computed status is VERIFIED, which requires human `reviewed_by` and
`reviewed_on` fields. It also requires a human activation record for that exact content. The
activation is re-checked before every recorded decision, so a revocation takes effect
immediately.

## 4. Rule engine

Results are **PASS, WARNING, BLOCK, UNVERIFIED** or **UNAVAILABLE**. A decision
(`ComplianceDecision`) contains:

- allowed;
- exact reason codes;
- every rule result with its threshold, current value, explanation, sources and verification;
- evaluation time, profile key, profile SHA-256, rule-set SHA-256 and account-state SHA-256;
- the official floor, official buffer and estimated buffer;
- the allowed quantity.

These cases **fail closed** for new entries:

- a non-operational profile;
- an untrusted restored state;
- unhealthy market data;
- unknown equity (unmarked position);
- unknown trade risk at the fill;
- an unknown tick value for the gap allowance;
- an account size different from the profile;
- an unknown instrument weight;
- a missing holiday schedule (when the profile has one);
- NewsGuard state unavailable (when the profile restricts news);
- any unverified safety-critical rule.

Evaluation scopes:

- **pre-trade** (signal time, with the stop-based risk);
- **fill** (actual risk required, plus the gap allowance);
- **status** (terminal, not recorded);
- **monitoring** (`observe`: breach, flatten reserve, flat-by time, end of day);
- **account / session / end of day** (journaled records);
- **payout** (`payout.py`).

## 5. Drawdown models

| Term | Meaning |
|---|---|
| balance | initial balance + realized net P&L (commissions included) |
| equity | balance + unrealized P&L (unknown if any position is unmarked) |
| high-water mark | highest EOD reference (trailing EOD) or intraday reference (trailing intraday) |
| official floor (threshold) | `static`: initial − amount. `trailing_*`: high-water − amount, capped at `lock_at`. It never moves down. |
| official buffer | equity (or balance if the rule excludes open P&L) − floor. A buffer of 0 is shown as **0** (a breach), never as unavailable. |
| uncertainty reserve | internal $ allowance for fee / fill / data differences from the firm's figures |
| estimated buffer | official buffer − uncertainty reserve |
| safety reserve | internal: entries need estimated buffer > safety reserve, and a full stop-out (risk + gap allowance) must leave more than it |
| flatten reserve | internal: open positions are flattened at or below it |
| available risk budget | estimated buffer − safety reserve |

**End-of-day trailing** is applied when a new trading date begins. The profile's day start is
17:00 America/Chicago, and weekend times belong to Monday. Wall-clock arithmetic is DST-safe.
The trail is applied before any entry of the new day is evaluated.

**Breaches** compare equity (open losses included when the rule says so) with the floor at every
event. A breach is permanent for the engine. Out-of-order events never move the day or the trail
backwards, but the loss check still runs.

Partial exits and commissions reach the engine through the account's balance. Slippage is already
inside the fill prices. Price gaps are covered by the gap allowance, and the doc does not claim
any guarantee. Missing marks leave equity unknown and block entries.

## 6. Daily loss and consistency

**Daily loss** (only when a profile has a `daily_loss` rule; FundedNext Flex has none):

- day P&L = balance change since the trading-day start, plus open P&L if `includes_unrealized`;
- fees are included through the balance;
- the reset is at the profile's day start (timezone-aware);
- entries need remaining − uncertainty − trade risk > 0;
- a WARNING is shown at 25% remaining.

**Consistency** (profile-defined; FundedNext Flex Challenge only):

- numerator: the best trading day's net balance change (realized, fees included, open P&L
  excluded);
- denominator: `max_day_share` × profit target;
- period: all recorded trading days;
- evaluated continuously.

The consequence follows the profile: for Flex, the target is recalculated to best day / 0.40, as
in the firm's $1,500 → $3,750 example. It warns at 90% of the threshold and never blocks (the
firm does not breach for it). It is marked UNVERIFIED because the numerator is not defined in the
excerpts.

## 7. Position and contract limits

Exposure is the sum of |position| × weight plus working entries × weight, where weight is the
micro-equivalent (MES = MNQ = 1, ES = NQ = 10). A new entry must fit the limit.

- An unknown instrument weight blocks.
- The quantity is never increased.
- `internal.quantity_policy = "reduce"` (explicit opt-in) may return a smaller allowed quantity.
  The authorizer then uses it, and RiskManager checks the reduced size.
- Otherwise the entry is rejected (`prop_contract_limit`). Strategy-requested quantities go
  through the same check and cannot bypass it.

## 8. News, session and holding restrictions

- **flat_by / trading_hours**:
  - new entries stop `entry_cutoff_minutes` before the flat time (2:55 PM CT for the FundedNext
    profiles);
  - open positions are flattened from `flatten_lead_minutes` before it (3:05 PM CT);
  - entries resume at 17:00 CT; Friday after the cutoff until Sunday 17:00 is closed;
  - all times are local wall-clock (DST-safe).
- **holiday_schedule**: a firm schedule file is required. A missing, stale or out-of-coverage
  schedule fails closed. Full-day closures and early closes are enforced.
- **news_restriction**: uses NewsGuard's decision (no second calendar). Unavailable means
  blocked. Not applicable to FundedNext Futures, where NewsGuard still applies independently.
- Protective exits (`flatten`, stops, targets) are never evaluated by the engine. Its flatten
  requests (`prop_firm_max_loss_protection`, `prop_firm_flat_by`) go through the runtime's
  ungated flatten path.

## 9. Pre-trade authorization and hierarchy

Pipeline in `src/paper/authorization.py`:

1. mode
2. system
3. request
4. position
5. NewsGuard
6. sizing
7. session cut-off
8. **AccountGuard**
9. **prop firm** (only when a profile is selected)
10. RiskManager

Fill time follows the same order with actual risk. Each stage is an independent veto, recorded in
the journal with profile, rule set, reasons, buffers and the account-state hash.

| # | Layer | Authority |
|---|---|---|
| 1 | Emergency protections | RiskManager emergency stop, system state, shutdown |
| 2 | AccountGuard | account survival (floor, flatten, locks) |
| 3 | Prop firm compliance engine | firm rules of the selected, verified, activated profile |
| 4 | RiskManager | daily limits, size, windows |
| 5 | Strategy and AI analysis | signals, regime gate |
| 6 | Simulated execution | PaperBroker; exits never gated |

The engine also warns when AccountGuard is disabled or looser than the firm rule (larger amount,
other model, or a lower lock level). It still enforces the firm threshold for entries itself.
`authority.unified_decision` reports all layers together. It reports only; it never replaces the
pipeline.

## 10. Persistence and recovery

- The engine state is part of the paper snapshot (`prop_firm`). It contains:
  - profile key and rule-set SHA-256;
  - high-water marks and the floor maximum;
  - breach state;
  - the current trading date and day start balance;
  - per-day statistics (bounded to 400 days);
  - processed execution ids (bounded to 20,000);
  - violations (bounded).
- The journal records `prop_firm_eod` (each trading day close) and `prop_firm_breach`. It also
  records every decision stage and the run identity, including the profile.
- On restart:
  - a state written under another profile or rule set is **never adopted or reset**: entries
    are blocked;
  - an account with execution history but no compliance state is untrusted;
  - a high-water mark below a journaled one, a lost breach, or a corrupt state is untrusted;
  - a profile removed from an account with compliance history is a reconciliation problem, so
    entries stay blocked.
- Duplicate executions are counted once. Concurrent access is serialized; the session lock is
  held on top of the engine's own lock.

## 11. Payout eligibility tracker (read-only)

It covers benchmark days, minimum trading days, minimum cycle profit, minimum withdrawal and
maximum withdrawal. Each requirement shows current value, required value, status, source and the
last verification date.

| Overall state | Meaning |
|---|---|
| RULES SATISFIED | every applicable verified rule is met by recorded data |
| RULES NOT SATISFIED | at least one rule is not met |
| DATA INCOMPLETE | a rule cannot be computed from recorded data |
| MANUAL FIRM CONFIRMATION REQUIRED | a rule is unverified, or the profile is not operational |

The tracker never promises, requests or submits a payout. Withdrawal cycles (resets after a
payout) are not known to the simulation; the whole recorded history is one cycle unless
`cycle_start` is given.

## 12. Rule change management

- A new rule version is a new file. Old versions stay.
- `run_prop_firm.py diff ID OLD NEW` lists every change. It flags looser or unknown-direction
  safety-critical changes, removed protective rules, lowered severities, reduced internal
  reserves and changed account parameters. It also lists accounts bound to the profile.
- Activation needs a human, a VERIFIED status and the first 16 characters of the exact SHA-256.
  Editing an active file makes it non-operational until approved again.
- Every journaled evaluation carries the profile key and rule-set hash in force. History is never
  re-evaluated under new rules.

## 13. Terminal: Prop Firm Center

Views:

- **Account compliance**:
  - firm, type, size, verification and activation, unresolved rules;
  - balance, equity, high-water mark, official floor, official buffer, uncertainty, estimated
    buffer, safety reserve, risk budget;
  - daily loss, contracts and trading date;
  - the entry check now with exact reasons;
  - the hierarchy, rule results and payout.
- **Rules & sources**.
- **Profiles** (library with statuses and verification dates).
- **Compare versions** (diff with unsafe flags).
- **History**: evaluations, block reasons, EOD drawdown history.

Status colours: SAFE (green), WARNING, BLOCKED, UNVERIFIED and DATA UNAVAILABLE. SAFE appears only
when the backend classifies it so: every rule passed and the profile is operational.

API (GET only, token, Host/Origin checks, validated parameters, rate limits):

```
/api/propfirm/status   /api/propfirm/profiles   /api/propfirm/profile?key=ID@VERSION
/api/propfirm/compare?a=ID@VERSION&b=ID@VERSION  /api/propfirm/history
/api/propfirm/export/{evaluations|drawdown|breaches|flattens|account|rules}.{csv|json}
```

No endpoint accepts balances, risk values or rule results from the client. There are no controls
for orders, activation, overrides or payouts.

## 14. Reports and exports

`run_prop_firm.py report --journal …` (text / JSON / CSV) and the terminal exports read the
chain-verified journal. A broken chain is refused.

Exports:

- carry provenance (report, generation time, UTC timezone, profile, rule-set hash, profile status,
  SIMULATED label);
- are bounded to 20,000 rows;
- are formula-injection safe;
- never contain order authorization tokens.

## 15. Commands

```
python run_prop_firm.py list
python run_prop_firm.py show fundednext_futures_flex_50k_challenge 2026-11-22.1
python run_prop_firm.py evaluate fundednext_futures_flex_50k_challenge 2026-11-22.1 \
    --balance 50000 --equity 49900 --position MES=2 --root MNQ --qty 1 --risk 60 --time 2026-11-20T15:00:00Z
python run_prop_firm.py diff ID OLD_VERSION NEW_VERSION
python run_prop_firm.py report --journal data/paper/paper_state.db --format csv
python run_terminal.py --mode replay        # Prop Firm Center (no profile selected by default)
python run_paper.py ... --prop-profile fundednext_futures_flex_50k_challenge@2026-11-22.1 --prop-initial-balance-from-profile
# HUMAN ONLY, after verifying every rule on the firm's full pages and recording the review in a NEW version file:
python run_prop_firm.py activate ID VERSION --approved-by "Name" --confirm <sha16> --statement "..."
```

## 16. Limitations

- FundedNext rules come from official knowledge-base **excerpts**. The full pages were not
  reachable from the build environment, and no effective dates were published in the excerpts.
  Rules change, and the profiles are dated research data.
- The firm's own figures (its commissions, fills, data and end-of-day time) can differ from this
  simulation. The uncertainty reserve is an allowance, not a bound.
- There is no firm holiday schedule, so the FundedNext profiles fail closed on that rule by
  design.
- Withdrawal cycles and a firm-side MLL lock on a payout request are not modelled.
- A whole-database rollback (an older copy of the state file replacing the current one) cannot be
  detected without an external anchor. This is the same limitation as for AccountGuard.
- The engine never sees the firm's real account. It protects a simulated account under the
  configured rules only.
