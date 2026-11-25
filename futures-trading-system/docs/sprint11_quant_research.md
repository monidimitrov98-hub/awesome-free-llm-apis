# Sprint 11: Quant research engine and the VWAP Mean Reversion research candidate

Research and simulation only. Nothing in this sprint creates a real order path, issues an
authorization token, changes AccountGuard limits or compliance rules, activates a prop firm
profile, promotes a strategy automatically or changes live trading parameters. The FundedNext
profiles `2026-11-25.1` remain **UNVERIFIED** and non-operational. No result in this sprint is
evidence that any strategy is profitable. The sample data is synthetic.

## 1. Architecture

```
config/strategies/<id>/<version>.toml      immutable parameter versions (+ VERSIONS.toml index, BOUNDS.toml)
        |
src/research/params.py     load_version / register_version (SHA-256 identity, append-only index)
src/research/registry.py   stable strategy ids -> load / prepare (causal features) / build / derive
        |
src/backtest/runner.py     run_spec(): the ONE event-driven BacktestEngine for every strategy
src/backtest/engine.py     + optional prop firm ComplianceEngine (same class as the paper runtime)
        |
src/research/runs.py       research runs, sweeps, walk-forward, purged CV, one-time OOS reveal, compare
src/research/experiments.py   every attempted configuration (hash-chained, failures included)
src/research/evaluation.py    evaluation report + evidence label
src/research/stats.py         one-sided tests, Holm / Benjamini-Hochberg, power, Sharpe / Sortino
src/research/monte_carlo.py   trade / day-block / moving-block / shuffle resampling, cost scenarios, prop checks
src/research/governance.py    human promotion gates (hash-chained store, no live state)
src/research/paper_binding.py governance-gated use of a version in the PAPER runtime (PaperBroker only)
src/research/views.py         read-only terminal views (/api/research/*, page "Quant Research")
run_research.py               CLI (the only place where versions are registered and promotions recorded)
```

The safety hierarchy is unchanged: AccountGuard -> prop firm compliance -> NewsGuard -> RiskManager
-> paper authorization and execution controls. Every stage is an independent veto. The paper chain
checks them in the order mode -> system -> request -> position -> NewsGuard -> sizing -> session ->
AccountGuard -> prop firm -> RiskManager. The backtest checks NewsGuard -> sizing -> cutoff ->
RiskManager -> AccountGuard -> prop firm at the signal, and NewsGuard -> RiskManager -> AccountGuard
-> prop firm at the fill. Because each stage can only refuse, the order changes only which reason is
recorded first, never whether an entry is allowed.

## 2. Strategy registry and parameter versions

* Stable ids: `vwap_trend_pullback` (the Sprint 2-4 strategy) and `vwap_mean_reversion` (V1
  research candidate). An unknown id is refused.
* `run_strategy()` now delegates to the strategy-agnostic `run_spec()`. A **golden regression
  test** compares the VWAP Trend Pullback outputs with values captured on the unmodified Sprint 10
  code: trades/equity fingerprint, trade count, net P&L, diagnostics hash and rejections hash, for
  MES and MNQ, with the default and a relaxed configuration. They are byte-identical in pandas 3.0.5
  and in pandas 2.2.3.
* A version is `config/strategies/<id>/<version>.toml`. Its identity is the SHA-256 of the file
  (LF-normalised) registered in `VERSIONS.toml`. The rules:
  - A registered file whose content no longer matches is refused ("modified after registration:
    create a new version").
  - Registration only appends to the index, and re-registering a version id is refused.
  - An unregistered file is used only with an explicit `--allow-unregistered`, as exploration. It
    can never be revealed out of sample or promoted.
* Schemas are strict: unknown sections or keys, invalid numbers, contradictions (oversold >=
  overbought, a band stop inside the entry band), combined confirmation methods and pyramiding are
  all refused.
* Every manifest records the strategy id, version, version SHA-256, registration, the parameter
  hash, any overrides and the full parameters.

Registered versions: `vwap_trend_pullback@V0` is a copy of `config/strategy_vwap_pullback.toml`.
`vwap_mean_reversion@V1-C1-SWING`, `V1-C2-SWING`, `V1-C3-SWING`, `V1-C4-SWING`, `V1-C2-ATR` and
`V1-C2-BAND` are the mean-reversion candidates.

## 3. VWAP indicator formulas (`src/indicators/vwap_bands.py`)

For one session, over bars `j = 1..i` up to and including bar `i`:

```
VWAP_i   = sum(v_j * p_j) / sum(v_j)                    p = typical price (H+L+C)/3 (or close)
Var_i    = sum(v_j * (p_j - VWAP_i)^2) / sum(v_j)       volume-weighted population variance about VWAP
sigma_i  = sqrt(Var_i);   band_i(k) = VWAP_i +/- k * sigma_i;   z_i = (close_i - VWAP_i) / sigma_i
```

* The session is explicit (`SessionConfig`). Bars outside it are NaN, and nothing accumulates
  across sessions or between RTH and ETH.
* The bands are causal: each value is a cumulative sum over past bars. A test cuts the data short
  and checks that earlier values do not change. A bar-by-bar incremental version (West's weighted
  algorithm) matches the batch version to about 1e-8. The VWAP equals the existing Sprint 1
  `session_vwap`.
* Zero volume contributes nothing. With no volume yet in the session, every output is NaN.
  Negative volume and NaN or infinite input are refused.
* When sigma is 0 (or at most `min_sigma`) the z-score is NaN, never infinite.
* For numerical stability, the batch version works on sums of `p - p0` (the session's first
  price). Tests check it against a direct two-pass computation at a price level of 20,000 (relative
  error at most 1e-9) and against hand-computed examples.
* RSI and ATR reuse the existing Wilder implementations, computed on the selected session's bars
  only (`indicator_scope = "session_bars"`).

## 4. VWAP Mean Reversion V1 (`src/strategy/vwap_mean_reversion.py`)

The concept is the user's. The exact rules below are **explicit research defaults**, not the
user's historical settings. The user's original confirmation rule and stop rule are **not known**.
C1-C4 and the three stop methods are experimental variants, and none is presented as the original.

For a LONG trade (SHORT is the mirror image), on the session `rth` (09:30-16:00 New York), with
5-minute signal bars and 1-minute execution bars:

1. **Setup** on a completed bar. All of these must hold:
   - `close <= VWAP - band_k * sigma` (band_k 2.0)
   - `RSI(14) <= 30`
   - `sigma >= 2 ticks`
   - at least 6 session bars have completed
2. **Confirmation** on a later bar, at most 3 bars after the setup, by exactly one configured
   method:
   - C1: bullish engulfing
   - C2: close back inside the band
   - C3: close above the previous bar's high
   - C4: RSI crosses back above 30

   The setup bar itself never confirms. Methods are never combined.
3. **Entry:** a market order at the confirmation bar's `available_at`. It fills at the open of
   the next execution bar, never on the signal bar. The close must still be below VWAP.
4. **Target:** `close + fraction * (VWAP - close)`, with fraction 1.0 (full reversion).
5. **Stop** (versioned):
   - `swing`: lowest low of the last 5 session bars
   - `atr`: 1.5 x ATR(14)
   - `band`: the 3-sigma band

   Each minus 2 ticks.
6. **Limits:**
   - one position at a time, no pyramiding (refused by the schema)
   - at most 3 entries per session
   - a 2-bar cooldown after each exit
   - no entry in the last 30 minutes of RTH
   - the engine flattens before 15:55

Costs and slippage come from the existing engine and are counted once. Sizing, NewsGuard,
RiskManager, AccountGuard and prop compliance cannot be bypassed from the strategy. The regime
engine only **labels** V1 trades (observe mode).

## 5. Prop firm compliance in backtests and parity with paper

* `BacktestEngine.run(prop_engine=...)` uses the same `ComplianceEngine` class as the paper
  runtime:
  - Every entry is checked at the signal (`pre_trade`) and again at the fill (`fill`), with the
    same risk computation and a quantity that can only be reduced.
  - `observe()` runs on every bar for end-of-day trailing and the flat-by time.
  - There is a protective stop at the floor plus the uncertainty and flatten reserves, with ties
    resolved to the worst case.
  - A flatten requested by the profile closes the position at market.
* Every backtest with a profile reports **compliance coverage** for each rule:
  - `APPLIED` / `UNAVAILABLE` / `NOT_APPLICABLE` / `OTHER_STAGE` / `REPORTED_NOT_ENFORCED` /
    `NOT_SIMULATABLE`
  - the lists of applied, verified, unavailable and not-simulatable rules
  - a verdict: `COMPLETE FOR SUPPORTED RULES` or `INCOMPLETE -- RESEARCH ONLY`

  A news restriction without a NewsGuard, or a missing holiday schedule, fails closed.
* Parity tests (`tests/test_sprint11_compliance_parity.py`) run the same profile file and the same
  requests through the backtest engine and the paper runtime. They check:
  - an unverified profile blocks with `prop_profile_not_operational` in both
  - the contract limit blocks with `prop_contract_limit` in both
  - an allowed entry passes `pre_trade` then `fill` in both
  - the flat-by cutoff blocks with the same reason
  - a gap to the loss threshold triggers `prop_firm_max_loss_protection` in both, and the official
    floor is never reached
  - a news rule without a NewsGuard fails closed

  Restart and recovery of the compliance state remain covered by the Sprint 9/10 paper tests. The
  backtest has no restart.
* A research backtest with the FundedNext `2026-11-25.1` profile blocks every entry, because the
  profile is not operational, and its coverage is `INCOMPLETE -- RESEARCH ONLY`. Passing these
  tests is not proof of compliance with FundedNext's real rules.

## 6. Statistical methodology

* **Inference unit:** the trading day (daily net P&L). Trades on the same day are correlated.
* **Test:** one-sided Student t of H0 "mean daily net P&L <= 0". A sample without variance gives
  no p-value.
* **Multiple testing:** every configuration ever attempted for a strategy on an instrument is in
  the family, failures and unfinished attempts included. They come from the hash-chained
  experiment registry, which records an attempt BEFORE it runs. Holm-Bonferroni gives the evidence
  label; Benjamini-Hochberg is reported alongside. For the one-time holdout, the family is the
  strategy's OOS reveals on that instrument.
* **Intervals:**
  - mean daily P&L: day bootstrap, 5,000 resamples, fixed seed
  - expectancy per trade: the wider of a t interval and a day-cluster bootstrap
  - win rate: Wilson
  - profit factor: day-cluster bootstrap
* **Sharpe / Sortino:** only with at least 20 trading days (annualised with sqrt(252), zero
  risk-free rate).
* **Power:** the number of trading days needed to detect the observed effect at 80 % power, at the
  adjusted level. It is information for the reader, not a pass mark.
* **Evidence labels.** The only fixed numbers are the project's existing sample-size convention
  (fewer than 30 trades is INSUFFICIENT) and alpha = 0.05.
  - `INSUFFICIENT EVIDENCE`: fewer than 30 trades or fewer than 2 days
  - `POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE`: Holm p < alpha AND the day-bootstrap interval
    is entirely above 0
  - `NEGATIVE EXPECTANCY IN THIS SAMPLE`: the interval is entirely below 0
  - `INCONCLUSIVE`: everything else (the default)
* **Scope** is always stated: full / train / validation / walk-forward test folds / purged CV /
  one-time OOS holdout.
* **Safeguards:**
  - Sweeps only vary parameters declared in `BOUNDS.toml`, inside their ranges. Every value is
    checked before anything runs. Rows are in parameter order and are never ranked.
  - Each sweep point gets a stability check: the share of its grid neighbours with the same sign of
    mean daily P&L, and their range.
  - Cost sensitivity is measured by full re-runs: commission x1.5 and x2, and +1 / +2 ticks of
    slippage per side.
  - Walk-forward has a purge gap between training and test windows, and test windows never
    overlap. Purged k-fold cross-validation removes `purge` sessions before and `embargo` sessions
    after each test block.
  - Fold selection uses a declared objective: the highest lower bound of the day-bootstrap interval
    on the training sessions. Net profit is never used to select.
* **One-time OOS reveal** (`run_research.py reveal-oos`). Requirements:
  - a registered, unmodified version with no overrides
  - committed, clean code
  - a verified validation ledger
  - dates that no research attempt of the strategy on that instrument has touched
  - no overlap with dates already revealed under a different fingerprint

  The fingerprint is the version SHA, the configuration files, the source code, the prop profile,
  the regime and the news calendar. The reveal is recorded in the ledger BEFORE anything is written
  or shown. The identical configuration can only replay it (identical results, no new record).
  Revealed dates can never be used for research again: no override exists. The ledger and the
  experiment registry used are recorded in every manifest and reveal.
* **Comparisons** are made only on equivalent data: the same data hash, instrument, period,
  configuration files and prop profile. They are not ranked, and pairwise differences come with a
  paired day-bootstrap interval.

## 7. Monte Carlo methodology (`run_research.py monte-carlo`)

* `HISTORICAL`: the actual backtest path.
* `MONTE_CARLO`: paths built by resampling the run's own trades. They show the spread one trade
  sample could produce and add no market information. Methods:
  - `trade_iid`: independent trades; its tails are usually too thin
  - `day_block`: whole days
  - `moving_block`: blocks of `ceil(n_days^(1/3))` consecutive days
  - `shuffle`: the same trades in a random order, for sequence risk only
* **Outputs:**
  - final P&L, drawdown and loss-streak quantiles
  - P(final < 0)
  - P(drawdown >= X)
  - risk of ruin to a stated loss (using MAE for intra-trade lows)
  - an equity fan
* **Cost scenarios** re-price every trade: +1 / +2 ticks per side, commission x2, and both
  combined.
* **Prop rules:** only rules individually marked `VERIFIED` (max loss, daily loss) are simulated.
  Unverified, conflicting or incomplete rules are listed as NOT EVALUATED. A conflicting lock is
  not applied, which keeps the floor trailing and is stricter. An unverified or non-activated
  profile is labelled `RESEARCH ESTIMATE`.
* **Determinism:** fixed seeds (`numpy.random.default_rng([seed, method, scenario])`). The manifest
  hashes the exact trades and parameters, and identical inputs give byte-identical reports.

## 8. Human promotion gates (`run_research.py promote`)

`RESEARCH -> BACKTESTED -> OOS_VALIDATED -> SHADOW_APPROVED -> PAPER_APPROVED`. Any state except
REJECTED can go to `SUSPENDED`, which can be reinstated only to the state it was suspended from.
Any state can go to `REJECTED`, which is final for that version.

* **No live state:** "LIVE" in any form is refused, and no step can be skipped.
* **Every transition is a human CLI action.** It needs:
  - `--approved-by`
  - `--confirm` (the first 16 characters of the version's SHA-256)
  - a statement
  - the evidence required by the target state

  An AST test checks that no module calls `transition()` or `register_version()`. Only
  `run_research.py` does.
* **Evidence is verified and hashed.** Run folders must pass manifest verification.
  - BACKTESTED: a research run of exactly this version, with no overrides.
  - OOS_VALIDATED: a one-time OOS run whose ledger record matches the version and its results. The
    evidence label is read from the hash-chained ledger record, never from an editable file, and
    only `POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE` qualifies.
  - SHADOW_APPROVED: a Monte Carlo report produced from that verified run, plus its compliance
    coverage.
  - PAPER_APPROVED: a `run_paper.py --report` of a SHADOW session of this version.
* **The store** (`data/research/promotions.jsonl`) is append-only and hash-chained, and uses HMAC
  with `FTS_JOURNAL_KEY`. A broken store approves nothing.
* **Use in the paper runtime:** `run_paper.py --strategy ID@VERSION` needs SHADOW_APPROVED (replay
  and live-data shadow sessions) or PAPER_APPROVED (`--mode paper` without the bridge). The rules:
  - Only PaperBroker is involved, and every authorization stage still applies.
  - A RTH strategy is flattened before the earlier of the two session ends.
  - The promotion state is re-read at most every 30 s while the session runs. A suspension,
    rejection or integrity failure blocks new entries (`strategy_not_approved`).

  The default path (`config/strategy_vwap_pullback.toml` with no `--strategy`) is unchanged.

## 9. Terminal

The read-only page **Quant Research** (`/api/research/overview`, `/api/research/run?attempt=<16 hex>`)
shows:

* the registry, versions with their file state and promotion state
* experiments, including failures and unfinished attempts
* OOS reveals
* research runs with evidence and compliance coverage
* run details: manifest integrity, evaluation, coverage per rule, cost sensitivity and Monte Carlo
* the limitations

There is no control to run research, reveal, register, promote or trade. Runs are addressed by
attempt id, never by a path. Evidence and promotion states are never shown in green.

## 10. Adversarial audit (Sprint 11)

| Area | Result |
|---|---|
| Look-ahead | Features are causal: truncation-invariance tests cover the bands, the MR features and the signals. The setup bar never confirms. Fills happen only at or after `available_at`. |
| Session leakage | RTH/ETH separation test. The first RTH bar's VWAP equals its own typical price. Features are NaN outside the session. |
| OOS reuse | Revealed dates are refused for every research purpose. Sweeps and folds check the whole period first (**fixed**: a sweep used to return per-row refusals instead of refusing up front). OOS dates used by earlier research are refused. A second configuration is refused. Identical replays are verified. |
| Variance errors | Hand examples, two-pass reference at index price levels, batch/incremental parity, negative-variance clipping. |
| Same-bar fills | The engine fills at the next execution bar's open. Tested `entry_time >= signal_time` for every version and instrument. |
| Double costs | `net = gross - costs`, `costs = commission + slippage`, `commission = 2 x qty x per side`, tested for every run. Monte Carlo cost scenarios add only the explicit extra. |
| Compliance bypass | Prop is checked at signal and fill in backtests, with parity tests against paper. The strategy cannot alter it. An unverified profile always blocks. |
| Unverified activation | No research path touches activations. Monte Carlo simulates only VERIFIED rules and labels non-operational profiles. |
| Version mutation | Modified versions are refused, the index is append-only, and governance, the ledger fingerprint and the registry are all keyed by the version SHA. |
| Nondeterministic Monte Carlo | Fixed seeds; byte-identical reports are tested. |
| Promotion without a human | No automatic path (AST test). Name, SHA confirmation, statement and evidence are required. |
| Corrupted manifests | Self-hash plus file hashes are verified. **Fixed:** promotion now verifies run folders and reads the OOS label from the ledger, so a forged label with a recomputed manifest hash is still refused. |
| Recovery / running sessions | **Fixed:** approval used to be checked only at start. The paper runtime now re-reads the promotion state and blocks new entries after a suspension or an integrity failure. |

Remaining limitations of the audit:

* The experiment registry and the ledger are tamper-evident, not tamper-proof. Someone who points
  `FTS_EXPERIMENTS_PATH` at a fresh file understates the multiple-testing family. Every manifest
  and reveal records the registry path, whether it is canonical, and its attempt count.
* `VERSIONS.toml` is not chained. A hand edit that re-registers a changed file creates a new SHA
  identity, so governance states, ledger fingerprints and experiment records do not carry over.
* The paper runtime's binding was exercised in replay with the synthetic sample. It was not
  exercised on live data.

## 11. Manual decisions that remain the user's

1. The original historical confirmation rule and stop rule of the user's VWAP mean-reversion
   method. V1 offers C1-C4 and the swing / ATR / band stops as experiments only.
2. Whether any FundedNext profile is ever verified and activated. That requires independent,
   approved evidence and human review (`run_prop_firm.py activate`). Nothing in Sprint 11 changes
   its UNVERIFIED status.
3. Real historical data, and the train / validation / OOS split, chosen before looking at results.
4. Every promotion step.
