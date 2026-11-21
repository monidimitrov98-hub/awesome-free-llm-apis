"""Run the VWAP Trend Pullback strategy on a historical CSV (simulation only).

Examples (from the project root):
    python run_backtest.py --csv data/samples/SYNTHETIC_MES_1m.csv
    python run_backtest.py --csv C:\\data\\MNQM25_1m.csv --output-dir output\\mnq
    python run_backtest.py --csv data/samples/SYNTHETIC_MNQ_1m.csv --r-multiples 1.5 2 3

No broker connection, no real orders. Results on synthetic data are meaningless and
no result of this tool is evidence of profitability without out-of-sample testing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.backtest.config import load_backtest_config
from src.backtest.runner import run_from_csv, summary_table, write_outputs
from src.config import ConfigError, load_config
from src.data.errors import DataError, DataValidationError
from src.strategy.vwap_pullback_config import load_strategy_config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest the VWAP Trend Pullback strategy on an OHLCV CSV.")
    p.add_argument("--csv", required=True, type=Path, help="1-minute OHLCV CSV (Sprint 1 format)")
    p.add_argument("--settings", type=Path, default=None, help="data config (default config/settings.toml)")
    p.add_argument("--backtest-config", type=Path, default=None, help="default config/backtest.toml")
    p.add_argument("--strategy-config", type=Path, default=None, help="default config/strategy_vwap_pullback.toml")
    p.add_argument("--r-multiples", type=float, nargs="+", default=None,
                   help="run once per take-profit R-multiple (side by side, nothing is selected)")
    p.add_argument("--output-dir", type=Path, default=Path("output"), help="where result files are written")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        app = load_config(args.settings)
        bt = load_backtest_config(args.backtest_config)
        strat = load_strategy_config(args.strategy_config)
        md, runs = run_from_csv(args.csv, app, bt, strat, args.r_multiples)
    except DataValidationError as exc:
        print(exc.report.summary(), file=sys.stderr)
        return 2
    except (ConfigError, DataError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if md.is_synthetic:
        print("*** SYNTHETIC DATA: results are meaningless for evaluating the strategy ***\n")
    print(f"Contract {md.contract.label}; signal {strat.signal_timeframe}, context "
          f"{strat.context_timeframe or 'off'}, execution {strat.execution_timeframe}")
    for run in runs:
        folder = write_outputs(run, md, app, bt, args.output_dir)
        d = run.diagnostics
        print(f"\n=== {run.label}: {len(run.result.trades)} trades -> {folder}")
        print("signal decisions:", d["decision"].value_counts().to_dict())
        print("top rejection reasons:", d.loc[d.decision == "reject", "rejection_reason"].value_counts().head(5).to_dict())
        if not run.result.rejections.empty:
            print("engine/risk rejections:", run.result.rejections["reason"].value_counts().to_dict())
    with pd.option_context("display.width", 160, "display.precision", 3):
        print("\nKey figures (net = after commission and slippage; for inspection only, not a ranking):")
        print(summary_table(runs))
    print("\nNo result here is evidence of profitability without out-of-sample validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
