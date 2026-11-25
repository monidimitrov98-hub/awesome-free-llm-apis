# Quant Research Engine (Sprint 11): integration assessment

Scope agreed for Sprint 11:

- **Direction:** QUANT, FULL scope, BALANCED risk, on MES and MNQ.
- **First hypothesis:** VWAP Pullback / Mean Reversion:
  - Bollinger Bands computed around session VWAP;
  - RSI confirmation;
  - price deviation from VWAP;
  - entry only after confirmation;
  - target back toward VWAP.

**Sprint 10 does not implement or optimise any strategy.** This document maps what already exists
so that Sprint 11 extends the current architecture instead of building a second one.

**Non-negotiable rule:** a research model is *data plus a Strategy object*. It never holds a broker,
an order router or a risk object. Every simulated order goes through the same hierarchy:
emergency → AccountGuard → prop firm → RiskManager → execution.

## 1. Historical market-data interfaces — exists, reuse

- `src/data/loader.py` (`load_csv`, column mapping, symbol filtering).
- `src/data/timestamps.py`, `sessions.py` (CME session dates, DST-aware) and `validation.py`
  (gaps, incomplete sessions, duplicates).
- `src/data/resample.py` (1-minute → 5 and 15 minutes; `available_at` and `is_complete`, so there
  is no look-ahead).
- `src/pipeline.py` / `run_pipeline.py` (`build_market_data`), and `src/data/contracts.py` (roots,
  expiry, specifications).

**Gap:** there is no roll / back-adjustment and no holiday calendar (documented in the README).
Sprint 11 should add an explicit continuous-contract policy before multi-month studies.

## 2. Feature engineering — partly exists, extend

`src/indicators/` provides session VWAP (bar-based), EMA, RSI, ATR, relative volume and swings,
all causal. `add_indicators(bars, cfg, session)` is the single entry point.
`src/ai/market_regime/features.py` provides the regime features.

**Missing for the hypothesis:** VWAP-centred bands (rolling standard deviation of price around
session VWAP, reset per session) and the deviation in σ. Add them as **new causal indicator
functions in `src/indicators/`**, with the same no-look-ahead tests as the existing ones. They
should not be computed inside the strategy.

## 3. Strategy interface — exists, reuse

`src/strategy/base.py`:

- `Strategy.on_bar(ctx) -> [OrderRequest]`, `on_fill`, `on_order_event`;
- `StrategyContext` exposes only data available at `now` (signal bar, causal history, context frames
  cut at `available_at`, position snapshot, contract).

A mean-reversion strategy is a new `Strategy` subclass with a frozen config dataclass (like
`vwap_pullback_config.py`). The paper runtime already accepts any strategy through
`strategy_factory`.

**Gap:** `run_backtest.py` / `runner.run_strategy` are hard-wired to `VwapTrendPullbackStrategy`.
Add a small **strategy registry** (name → (config loader, factory)) used by the backtest runner,
the paper runtime and the identity record. Do not add a parallel runner.

## 4. Statistical evaluation — exists, reuse

- `src/backtest/metrics.py` (backtest metrics).
- `src/analytics/shadow/stats.py`: Wilson intervals, Student-t, trading-day cluster bootstrap,
  profit-factor bootstrap, sample-size levels.
- `session_report.py`: guarded Sharpe and Sortino, exposure.

Sprint 11 should call these, not copy them. Add a deflated-Sharpe or multiple-testing adjustment
when many parameter sets are compared.

## 5. Backtesting — exists, extend once

`BacktestEngine.run(strategy, signal_bars, execution_bars, risk_manager=, context_bars=,
news_guard=)` already provides:

- 1-minute execution;
- conservative ambiguous bars;
- slippage and costs;
- NewsGuard at signal and fill;
- RiskManager and AccountGuard.

**Gap (must fix before research results are trusted):** the backtest engine does **not** call the
prop firm compliance engine. `ComplianceEngine` is engine-agnostic (`AccountSnapshot`,
`evaluate_entry(scope="pre_trade"/"fill")`, `observe`). Sprint 11 should insert it in
`_Simulation` at signal and fill, after AccountGuard and before RiskManager. This mirrors the paper
`EntryAuthorizer`, so backtests, paper and shadow apply one hierarchy.

## 6. Walk-forward validation — exists

`src/backtest/validation.py`: `walk_forward_periods(train, test, step)`, in-sample /
out-of-sample periods with an overlap check, and `run_backtest.py --walk-forward`. Parameters must
be fitted only on the train window. Sprint 11 should add the *fitting* loop (grid or random search
within declared bounds) that runs per fold.

## 7. Out-of-sample testing — exists, reuse strictly

`ValidationLedger`: a hash-chained ledger in which an OOS period is revealed once
(`--reveal-oos`), with reuse refused (`OosReuseError`) and a `validation_fingerprint` that binds
the configuration, code and R multiple. Shadow Analytics flags replay sessions inside revealed OOS
periods. Sprint 11 must declare its final OOS period **before** any search and reveal it only
through the ledger.

## 8. Monte Carlo — missing, add in analytics

There is no Monte Carlo today; only bootstrap confidence intervals exist. Add
`src/analytics/research/montecarlo.py`, offering:

- trade-order reshuffling;
- trading-day block bootstrap of the equity path;
- cost and slippage stress (reusing `cost_scenarios`);
- prop-firm survival probability under the selected profile's drawdown model (reusing
  `ComplianceEngine` offline with `AccountSnapshot`s).

Results must be seeded and labelled SIMULATED or HYPOTHETICAL.

## 9. Parameter versioning — partly exists

`src/backtest/manifest.py` gives config hashes, code identity, `results_fingerprint` and replay.
`run_identity` (Sprint 8) gives the strategy version as `name@sha12`.

Add **versioned parameter sets** as TOML files `config/strategies/<name>/<version>.toml`, mirroring
prop firm profiles: an immutable version per file, a SHA-256 identity, and a status of DRAFT /
RESEARCH / CANDIDATE / APPROVED / RETIRED.

## 10. Strategy approval and promotion — pattern exists, reuse

Reuse the prop firm activation pattern (`versions.Activations`): a hash-chained, HMAC-capable,
human-only approval bound to the exact SHA-256, and revocable. Promotion gates, all recorded in
the ledger:

1. walk-forward evidence;
2. a single OOS reveal;
3. Monte Carlo survival under the selected prop profile;
4. a minimum shadow sample (session report COMPLETE, at least 20 days).

Nothing is ever promoted automatically.

## 11. Regime engine integration — exists

`RegimeGatedStrategy` wraps any `Strategy`. Regime records are journaled per signal and analysed
in Shadow Analytics (by-regime buckets, never inferred). A mean-reversion strategy should declare
its own regime suitability: mean reversion in range and contraction regimes, not in strong trends.
This should be configured, not hard-coded.

## 12. RiskManager and AccountGuard — exists, must stay outside the model

Sizing comes from `src/backtest/sizing.py` and is enforced by RiskManager and AccountGuard in the
engine. A research model must never size beyond `OrderRequest.quantity`, which is still clipped by
RiskManager, the prop contract limits and AccountGuard. A *balanced* risk approach is expressed as
configuration (`risk_per_trade`, daily limits, the prop profile's internal reserves), never as
strategy code.

## 13. Shadow Analytics integration — exists

The run identity carries the strategy version, config hash, regime, risk and prop firm identity.
The session report and decision trace work for any strategy. Live-vs-replay comparison requires
identical strategy hashes. Sprint 11 strategies therefore become comparable in shadow without new
code.

## Proposed Sprint 11 order

1. Strategy registry, plus the backtest runner made strategy-agnostic.
2. Insert the prop firm engine into the backtest `_Simulation`, with parity tests against paper.
3. VWAP-band and deviation indicators (causal, tested).
4. Mean-reversion strategy, config dataclass and versioned parameter files.
5. Research CLI: walk-forward fitting with declared bounds, a single OOS reveal through the
   ledger, and a multiple-testing adjustment.
6. Monte Carlo module (orders, blocks, costs, prop survival).
7. Promotion gates and human approval (reuse `Activations`).
8. A shadow run of the candidate with session reports; no live trading.
