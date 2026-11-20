"""Command-line entry point for the Sprint 1 market data engine.

Examples (from the project root):
    python run_pipeline.py --csv data/samples/SYNTHETIC_MES_1m.csv
    python run_pipeline.py --csv C:\\data\\MESM25.csv --timeframe 5min --output-dir output
    python run_pipeline.py --csv my.csv --config config/settings.local.toml

Research use only. This tool does not place orders.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.config import ConfigError, load_config
from src.data.errors import DataError, DataValidationError
from src.pipeline import build_market_data


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load, validate, resample and compute indicators for an OHLCV CSV.")
    parser.add_argument("--csv", required=True, type=Path, help="Path to the OHLCV CSV file")
    parser.add_argument("--config", type=Path, default=None, help="TOML config (default: config/settings.toml)")
    parser.add_argument("--timeframe", default=None, help="Only show this timeframe (e.g. 5min)")
    parser.add_argument("--tail", type=int, default=5, help="Rows to print per timeframe")
    parser.add_argument("--output-dir", type=Path, default=None, help="Write one CSV per timeframe here")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        md = build_market_data(args.csv, config)
    except DataValidationError as exc:
        print(exc.report.summary(), file=sys.stderr)
        print("\nStopped: fix the data or set data.on_error = \"drop\" in the config.", file=sys.stderr)
        return 2
    except (ConfigError, DataError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if md.is_synthetic:
        print("*** SYNTHETIC DATA: values are randomly generated, not real market prices ***\n")
    print(md.report.summary())
    c = md.contract
    print(
        f"\nContract: {c.label} ({c.spec.description if c.spec else '?'}) via {md.contract_source}; "
        f"tick={c.spec.tick_size if c.spec else '?'}, point value={c.spec.point_value if c.spec else '?'}, "
        f"nominal expiry={c.expiry or 'n/a'}"
    )
    print(f"Session: {config.session.name} {config.session.start:%H:%M}-{config.session.end:%H:%M} {config.session.timezone}")

    frames = md.frames
    if args.timeframe:
        if args.timeframe not in frames:
            print(f"Error: timeframe {args.timeframe} not configured; choose from {list(frames)}", file=sys.stderr)
            return 1
        frames = {args.timeframe: frames[args.timeframe]}

    with pd.option_context("display.width", 200, "display.max_columns", 30, "display.precision", 4):
        for tf, frame in frames.items():
            print(f"\n=== {tf}: {len(frame)} bars, {frame.index[0]} -> {frame.index[-1]} (UTC, bar open) ===")
            print(frame.tail(args.tail))

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for tf, frame in frames.items():
            out = args.output_dir / f"{args.csv.stem}_{tf}.csv"
            frame.to_csv(out)
            print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
