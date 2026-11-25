# Frozen research plans (Sprint 12)

A plan freezes, BEFORE any real-data result is seen:

* the datasets (`<id>@v<N>` + manifest SHA-256, VALIDATED or better, unadjusted),
* the train / validation / OOS segments with purge and embargo gaps,
* the strategy versions under test and the backtest configuration (costs, slippage) by SHA-256.

`python run_research.py plan-check <id>` validates a plan file; `plan-register <id>` appends it to
`PLANS.toml` (append-only). From that moment the OOS segment is RESERVED: no research attempt may touch it,
and it can be revealed only with `run_research.py reveal-oos --plan <id>`, once, exactly as frozen.

`real-data-v1.toml.draft` is the proposed first real-data plan. It cannot be registered until real data has
been obtained, ingested and VALIDATED (its dataset references and hashes are placeholders) -- see
`docs/sprint12_historical_market_data.md`, section "First real-data research plan".
