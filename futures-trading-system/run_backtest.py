"""Run the VWAP Trend Pullback strategy on a historical CSV (simulation only).

Examples (from the project root):
    python run_backtest.py --csv data/samples/SYNTHETIC_MES_1m.csv
    python run_backtest.py --csv C:\\data\\MNQ.csv --start 2025-01-06 --end 2025-03-28
    python run_backtest.py --csv C:\\data\\MNQ.csv --in-sample 2025-01-02 2025-02-28 \\
        --out-of-sample 2025-03-03 2025-03-28              (OOS stays sealed)
    python run_backtest.py ... --out-of-sample 2025-03-03 2025-03-28 --reveal-oos
    python run_backtest.py --csv C:\\data\\MNQ.csv --walk-forward 20 5 --reveal-oos
    python run_backtest.py --replay output/r_2/manifest.json

Every result folder contains a manifest.json (configuration texts, data SHA-256, git
commit, versions, result fingerprint) that --replay can verify.

No broker connection, no real orders. In-sample results and results on synthetic data
are not evidence of profitability.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
from src.backtest.manifest import config_hash, file_sha256, read_config_texts, replay
from src.backtest.runner import run_strategy, summary_table, write_outputs
from src.backtest.validation import (
    OosLedger,
    Period,
    check_no_overlap,
    make_period,
    session_dates,
    walk_forward_periods,
)
from src.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from src.data.errors import DataError, DataValidationError
from src.pipeline import build_market_data
from src.strategy.vwap_pullback_config import DEFAULT_STRATEGY_CONFIG_PATH, load_strategy_config

DISCLAIMER = ("No result here is evidence of profitability. In-sample results are descriptive only; "
              "judge the strategy on untouched out-of-sample data.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest the VWAP Trend Pullback strategy on an OHLCV CSV.")
    p.add_argument("--csv", type=Path, help="1-minute OHLCV CSV (Sprint 1 format)")
    p.add_argument("--settings", type=Path, default=DEFAULT_CONFIG_PATH, help="data config")
    p.add_argument("--backtest-config", type=Path, default=DEFAULT_BACKTEST_CONFIG_PATH)
    p.add_argument("--strategy-config", type=Path, default=DEFAULT_STRATEGY_CONFIG_PATH)
    p.add_argument("--r-multiples", type=float, nargs="+", default=None,
                   help="in-sample only: one run per take-profit R (side by side, never ranked)")
    p.add_argument("--output-dir", type=Path, default=Path("output"))
    p.add_argument("--start", help="first session date (YYYY-MM-DD) of a single evaluation range")
    p.add_argument("--end", help="last session date (inclusive)")
    p.add_argument("--in-sample", nargs=2, metavar=("START", "END"))
    p.add_argument("--out-of-sample", nargs=2, metavar=("START", "END"))
    p.add_argument("--walk-forward", nargs=2, type=int, metavar=("TRAIN_SESSIONS", "TEST_SESSIONS"))
    p.add_argument("--wf-step", type=int, default=None, help="sessions to advance per fold (default TEST)")
    p.add_argument("--reveal-oos", action="store_true",
                   help="actually compute out-of-sample results (recorded in the OOS ledger)")
    p.add_argument("--allow-oos-reuse", action="store_true",
                   help="allow re-testing an OOS period already evaluated with another configuration")
    p.add_argument("--replay", type=Path, help="re-run a manifest.json and verify its result fingerprint")
    return p.parse_args(argv)


def _plan(args: argparse.Namespace, exec_frame: pd.DataFrame) -> list[Period]:
    modes = sum(bool(x) for x in (args.start or args.end, args.in_sample or args.out_of_sample, args.walk_forward))
    if modes > 1:
        raise ConfigError("Use only one of --start/--end, --in-sample/--out-of-sample or --walk-forward")
    if args.walk_forward:
        folds = walk_forward_periods(session_dates(exec_frame), *args.walk_forward, args.wf_step)
        return [p for fold in folds for p in fold]
    if args.in_sample or args.out_of_sample:
        if not args.in_sample:
            raise ConfigError("--out-of-sample requires --in-sample")
        periods = [make_period("in_sample", "in_sample", *args.in_sample)]
        if args.out_of_sample:
            periods.append(make_period("out_of_sample", "out_of_sample", *args.out_of_sample))
            check_no_overlap(periods[0], periods[1])
        return periods
    if args.start or args.end:
        return [make_period("range", "range", args.start, args.end)]
    return [Period("full", "full")]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.replay:
            rep = replay(args.replay, args.csv)
            print(f"Replay of {args.replay}: {'REPRODUCED' if rep['reproduced'] else 'MISMATCH'}")
            for k in ("expected", "actual", "data_sha256", "git_commit_then", "git_commit_now"):
                print(f"  {k}: {rep[k]}")
            return 0 if rep["reproduced"] else 3
        if args.csv is None:
            raise ConfigError("--csv is required (or use --replay)")
        app = load_config(args.settings)
        bt = load_backtest_config(args.backtest_config)
        strat = load_strategy_config(args.strategy_config)
        texts = read_config_texts(Path(args.settings), Path(args.backtest_config), Path(args.strategy_config))
        md = build_market_data(args.csv, app)
        data_hash = file_sha256(args.csv)
        r_values = args.r_multiples or [strat.target.r_multiple]
        periods = _plan(args, md.frames[strat.execution_timeframe])
        oos_periods = [p for p in periods if p.is_out_of_sample]
        if oos_periods and len(r_values) > 1:
            raise ConfigError("Out-of-sample evaluation needs ONE pre-registered configuration; "
                              "compare R-multiples in-sample only")
        ledger = OosLedger(args.output_dir / "oos_ledger.json")
    except DataValidationError as exc:
        print(exc.report.summary(), file=sys.stderr)
        return 2
    except (ConfigError, DataError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if md.is_synthetic:
        print("*** SYNTHETIC DATA: results are meaningless for evaluating the strategy ***\n")
    print(f"Contract {md.contract.label}; signal {strat.signal_timeframe}, context "
          f"{strat.context_timeframe or 'off'}, execution {strat.execution_timeframe}; "
          f"account protection: {bt.account_rules.profile if bt.account_rules.enabled else 'disabled'}")
    command_line = sys.argv if argv is None else ["run_backtest.py", *argv]
    for period in periods:
        if period.is_out_of_sample and not args.reveal_oos:
            print(f"\n=== {period.label} {period.to_dict()['start']}..{period.to_dict()['end']}: SEALED "
                  "(pass --reveal-oos once the configuration is final)")
            continue
        runs = []
        for r in r_values:
            cfg = strat.with_r_multiple(r)
            oos = None
            if period.is_out_of_sample:
                try:
                    oos = ledger.check_and_record(data_hash, period, config_hash(texts | {"r_multiple": str(r)}),
                                                  args.allow_oos_reuse)
                except ConfigError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return 4
            try:
                run = run_strategy(md, app, bt, cfg, period=None if period.kind == "full" else period)
            except ConfigError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            out = args.output_dir if period.kind == "full" else args.output_dir / period.label
            folder = write_outputs(run, md, app, bt, out, manifest={
                "csv_path": args.csv, "data_sha256": data_hash, "config_texts": texts,
                "command_line": command_line, "oos": oos})
            runs.append(run)
            d = run.diagnostics
            print(f"\n=== {period.label} [{period.to_dict()['start'] or 'start'}..{period.to_dict()['end'] or 'end'}]"
                  f" {run.label}: {len(run.result.trades)} trades -> {folder}")
            print("  signal decisions:", d["decision"].value_counts().to_dict())
            if run.signals is not None and not run.signals.empty:
                print("  submitted signals by engine outcome:", run.signals["status"].value_counts().to_dict())
            if not run.result.rejections.empty:
                print("  engine/risk rejections:", run.result.rejections["reason"].value_counts().to_dict())
            acct = run.result.account
            if acct.get("enabled"):
                print(f"  account: floor {acct['final_floor']:.2f}, min buffer {acct['min_remaining_buffer']:.2f}, "
                      f"breached {acct['breached']}")
        with pd.option_context("display.width", 160, "display.precision", 3):
            print(f"  key figures for {period.label} (net = after costs; inspection only, not a ranking):")
            print(summary_table(runs).to_string().replace("\n", "\n  "))
    print("\n" + DISCLAIMER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
