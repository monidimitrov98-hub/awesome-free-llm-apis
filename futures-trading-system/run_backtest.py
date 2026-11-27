"""Run the VWAP Trend Pullback strategy on a historical CSV (simulation only).

Examples (from the project root):
    python run_backtest.py --csv data/samples/SYNTHETIC_MES_1m.csv
    python run_backtest.py --csv C:\\data\\MNQ.csv --start 2025-01-06 --end 2025-03-28
    python run_backtest.py --csv C:\\data\\MNQ.csv --in-sample 2025-01-02 2025-02-28 \\
        --out-of-sample 2025-03-03 2025-03-28              (OOS stays sealed)
    python run_backtest.py --init-ledger                   (once, creates the validation ledger)
    python run_backtest.py ... --out-of-sample 2025-03-03 2025-03-28 --reveal-oos
    python run_backtest.py --csv C:\\data\\MNQ.csv --walk-forward 20 5 --reveal-oos
    python run_backtest.py --replay output/r_2/manifest.json
    python run_backtest.py --verify-ledger

Every result folder contains a manifest.json (configuration texts, data SHA-256, code
identity, versions, result fingerprint) that --replay verifies.

Exit codes: 0 ok, 1 configuration/data error, 2 data validation failed, 3 replay
mismatch, 4 refused: overlaps previously revealed OOS dates under another
configuration/code, 5 validation ledger missing/damaged, 6 untrusted code (OOS reveal or
replay), 7 replay only approximate.

No broker connection, no real orders. In-sample results and results on synthetic data
are not evidence of profitability.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
from src.backtest.manifest import (
    code_identity,
    config_hash,
    file_sha256,
    read_config_texts,
    replay,
    validation_fingerprint,
)
from src.backtest.runner import run_strategy, summary_table, write_outputs
from src.backtest.validation import (
    LedgerError,
    LedgerMissingError,
    Period,
    ValidationLedger,
    check_no_overlap,
    make_period,
    resolve_ledger_path,
    revealed_conflicts,
    session_dates,
    walk_forward_periods,
)
from src.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from src.data.errors import DataError, DataValidationError
from src.pipeline import build_market_data
from src.strategy.vwap_pullback_config import DEFAULT_STRATEGY_CONFIG_PATH, load_strategy_config
from src.ai.market_regime.config import DEFAULT_REGIME_CONFIG_PATH, load_regime_config
from src.ai.market_regime.identity import instrument_key
from src.risk.news.backtest import load_setup, paired_comparison
from src.risk.news.config import DEFAULT_NEWS_CONFIG_PATH, load_news_config

DISCLAIMER = ("No result here is evidence of profitability. In-sample results are descriptive only; "
              "judge the strategy on untouched out-of-sample data.")
REPLAY_EXIT = {"EXACT": 0, "EXACT_DIFFERENT_DEPENDENCIES": 7, "APPROXIMATE": 7, "MISMATCH": 3,
               "UNTRUSTED_CODE": 6, "MANIFEST_INCONSISTENT": 3}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest the VWAP Trend Pullback strategy on an OHLCV CSV.")
    p.add_argument("--csv", type=Path, help="1-minute OHLCV CSV (Sprint 1 format)")
    p.add_argument("--settings", type=Path, default=DEFAULT_CONFIG_PATH, help="data config")
    p.add_argument("--backtest-config", type=Path, default=DEFAULT_BACKTEST_CONFIG_PATH)
    p.add_argument("--strategy-config", type=Path, default=DEFAULT_STRATEGY_CONFIG_PATH)
    p.add_argument("--regime-config", type=Path, default=DEFAULT_REGIME_CONFIG_PATH,
                   help="AI market regime config (advisory gate; its [regime] enabled flag switches it)")
    p.add_argument("--news-config", type=Path, default=DEFAULT_NEWS_CONFIG_PATH,
                   help="economic news & event risk filter config ([news] enabled/mode)")
    p.add_argument("--news-calendar", type=Path, default=None,
                   help="point-in-time historical calendar JSON (without one, the enabled filter blocks every entry)")
    p.add_argument("--news-compare", action="store_true",
                   help="in-sample only: also run without the filter and in observe mode, write news_comparison.json")
    p.add_argument("--r-multiples", type=float, nargs="+", default=None,
                   help="in-sample only: one run per take-profit R (side by side, never ranked)")
    p.add_argument("--output-dir", type=Path, default=Path("output"))
    p.add_argument("--start", help="first session date (YYYY-MM-DD) of a single evaluation range")
    p.add_argument("--end", help="last session date (inclusive)")
    p.add_argument("--in-sample", nargs=2, metavar=("START", "END"))
    p.add_argument("--out-of-sample", nargs=2, metavar=("START", "END"))
    p.add_argument("--walk-forward", nargs=2, type=int, metavar=("TRAIN_SESSIONS", "TEST_SESSIONS"))
    p.add_argument("--wf-step", type=int, default=None, help="sessions to advance per fold (default and minimum: TEST)")
    p.add_argument("--reveal-oos", action="store_true",
                   help="compute out-of-sample results (needs committed code and the validation ledger)")
    p.add_argument("--allow-oos-reuse", action="store_true",
                   help="run although the dates overlap OOS revealed under another configuration/code "
                        "(results are marked contaminated)")
    p.add_argument("--ledger", type=Path, default=None,
                   help="validation ledger path (default: validation_history/oos_ledger.jsonl in the project, "
                        "or $FTS_LEDGER_PATH)")
    p.add_argument("--init-ledger", action="store_true", help="create the validation ledger if it does not exist")
    p.add_argument("--verify-ledger", action="store_true", help="verify the validation ledger and exit")
    p.add_argument("--replay", type=Path, help="re-run a manifest.json and verify code, data and results")
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


def _ledger_records(ledger: ValidationLedger) -> list[dict]:
    try:
        return ledger.records()
    except LedgerMissingError:
        return []


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ledger_path, canonical = resolve_ledger_path(args.ledger)
    ledger = ValidationLedger(ledger_path)
    try:
        if args.verify_ledger or (args.init_ledger and args.csv is None and args.replay is None):
            if args.init_ledger and not ledger.exists():
                ledger.initialise("created with --init-ledger")
                print(f"Initialised validation ledger {ledger_path}")
            records = ledger.records()
            reveals = [r for r in records if r.get("type") == "oos_reveal"]
            print(f"Ledger {ledger_path} OK: {len(records)} records, {len(reveals)} OOS reveals")
            return 0
        if args.replay:
            rep = replay(args.replay, args.csv)
            print(f"Replay of {args.replay}: {rep['status']}")
            for k in ("expected", "actual", "data_sha256", "git_commit_then", "git_commit_now"):
                print(f"  {k}: {rep[k]}")
            for reason in rep["reasons"]:
                print(f"  ! {reason}")
            return REPLAY_EXIT[rep["status"]]
        if args.csv is None:
            raise ConfigError("--csv is required (or use --replay / --verify-ledger / --init-ledger)")
        records = _ledger_records(ledger)  # verifies an existing ledger; raises LedgerError when damaged
        app = load_config(args.settings)
        bt = load_backtest_config(args.backtest_config)
        strat = load_strategy_config(args.strategy_config)
        regime = load_regime_config(args.regime_config)
        news_cfg = load_news_config(args.news_config)
        news = load_setup(news_cfg, args.news_calendar) if news_cfg.enabled else None
        texts = read_config_texts(Path(args.settings), Path(args.backtest_config), Path(args.strategy_config),
                                  Path(args.regime_config), news)
        md = build_market_data(args.csv, app, holdout_guard=True)   # refused if the file holds reserved bars
        data_hash = file_sha256(args.csv)
        code = code_identity()
        r_values = args.r_multiples or [strat.target.r_multiple]
        periods = _plan(args, md.frames[strat.execution_timeframe])
        # Holdout (Sprint 13 P1.1): refuse BEFORE anything runs or is written to the ledger. run_backtest.py never
        # evaluates holdout data, not even as its legacy out-of-sample period: the one-time final evaluation of
        # reserved data is `run_research.py reveal-oos --plan` only. (run_spec re-checks the simulated bars.)
        from src.research.holdout import check_research, simulated_span

        span = simulated_span(md.frames[strat.execution_timeframe])
        if span is not None:
            for p in periods:
                d = p.to_dict()
                check_research(md.contract.root, d["start"] or span[0], d["end"] or span[1],
                               f"run_backtest.py period {p.label}")
        oos_periods = [p for p in periods if p.is_out_of_sample]
        if oos_periods and args.news_compare:
            raise ConfigError("--news-compare evaluates several configurations; use it in-sample only")
        if oos_periods and len(r_values) > 1:
            raise ConfigError("Out-of-sample evaluation needs ONE pre-registered configuration; "
                              "compare R-multiples in-sample only")
        if oos_periods and args.reveal_oos:
            if not code["trusted"]:
                print(f"Error: refusing to reveal out-of-sample results from untrusted code: "
                      f"{', '.join(code['untrusted_reasons'])}. Commit your changes first.", file=sys.stderr)
                return 6
            if not ledger.exists():
                if not args.init_ledger:
                    print(f"Error: no validation ledger at {ledger_path}. If this is genuinely the first "
                          "validation of this project, create it with --init-ledger; if it existed before, "
                          "restore it from backup instead.", file=sys.stderr)
                    return 5
                ledger.initialise("created with --init-ledger before the first OOS reveal")
    except DataValidationError as exc:
        print(exc.report.summary(), file=sys.stderr)
        return 2
    except LedgerError as exc:
        print(f"Error: validation ledger problem: {exc}\nRestore the ledger and its .head file from backup; "
              "the ledger is never recreated automatically.", file=sys.stderr)
        return 5
    except (ConfigError, DataError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if md.is_synthetic:
        print("*** SYNTHETIC DATA: results are meaningless for evaluating the strategy ***\n")
    if not canonical:
        print(f"NOTE: using non-canonical validation ledger {ledger_path}")
    print(f"Contract {md.contract.label}; signal {strat.signal_timeframe}, context "
          f"{strat.context_timeframe or 'off'}, execution {strat.execution_timeframe}; "
          f"account protection: {bt.account_rules.profile if bt.account_rules.enabled else 'disabled'}; "
          f"code {'trusted' if code['trusted'] else 'UNTRUSTED (' + ', '.join(code['untrusted_reasons']) + ')'}")
    command_line = sys.argv if argv is None else ["run_backtest.py", *argv]
    root = instrument_key(md.contract.root, md.is_synthetic)
    if news is None:
        print("News filter: DISABLED in config (no event-risk protection in this run)")
    else:
        print(f"News filter: {news.cfg.mode}; calendar {news.calendar_path or 'NONE'}")
        if news.store_text is None:
            print("  *** no historical calendar: the filter cannot see events, so every new entry is blocked "
                  "(fail closed). Pass --news-calendar or set [calendar] backtest_calendar_path. ***")
    print(f"Regime engine: {'enabled (' + regime.enforcement + ', ' + regime.model_version + ')' if regime.enabled else 'disabled'}")
    for period in periods:
        if period.is_out_of_sample and not args.reveal_oos:
            print(f"\n=== {period.label} {period.to_dict()['start']}..{period.to_dict()['end']}: SEALED "
                  "(pass --reveal-oos once the configuration is final and committed)")
            continue
        runs = []
        for r in r_values:
            cfg = strat.with_r_multiple(r)
            fingerprint = validation_fingerprint(texts, r, code["source_sha256"])
            try:
                records = _ledger_records(ledger)
            except LedgerError as exc:
                print(f"Error: validation ledger problem: {exc}", file=sys.stderr)
                return 5
            conflicts = revealed_conflicts(records, root, period, fingerprint)
            if conflicts and not args.allow_oos_reuse:
                spans = sorted({f"{c['start']}..{c['end']}" for c in conflicts})
                print(f"Error: {period.label} overlaps out-of-sample dates {spans} of {root} that were already "
                      "revealed under a different configuration or code. Using them now would turn them into "
                      "in-sample data. Choose dates after the revealed periods, or pass --allow-oos-reuse "
                      "(results are then marked contaminated).", file=sys.stderr)
                return 4
            oos = None
            if period.is_out_of_sample:
                rec = ledger.append({"type": "oos_reveal", "instrument": root, **period.to_dict(),
                                     "data_sha256": data_hash, "fingerprint": fingerprint,
                                     "config_hash": config_hash(texts), "source_sha256": code["source_sha256"],
                                     "commit": code["commit"], "r_multiple": r,
                                     "reuse_acknowledged": bool(conflicts)})
                oos = {"revealed": True, "ledger_seq": rec["seq"], "reuse_acknowledged": bool(conflicts)}
            try:
                run = run_strategy(md, app, bt, cfg, period=None if period.kind == "full" else period,
                                   regime=regime, news=news)
            except ConfigError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            validation = {"fingerprint": fingerprint, "ledger_path": str(ledger_path),
                          "ledger_canonical": canonical, "contaminated": bool(conflicts),
                          "conflicting_reveals": [{k: c[k] for k in ("seq", "start", "end", "fingerprint")}
                                                  for c in conflicts]}
            out = args.output_dir if period.kind == "full" else args.output_dir / period.label
            folder = write_outputs(run, md, app, bt, out, manifest={
                "csv_path": args.csv, "data_sha256": data_hash, "config_texts": texts, "code": code,
                "validation": validation, "command_line": command_line, "oos": oos,
                "regime_info": None if run.regime is None else {
                    "model_version": regime.model_version, "enforcement": regime.enforcement,
                    "artifacts": run.regime["artifacts"], "errors": run.regime["errors"],
                    "gate": run.regime["performance"].get("gate")},
                "news_info": None if run.news is None else {
                    "mode": run.news["mode"], "calendar_sha256": run.news["calendar_sha256"],
                    "calendar_path": news.calendar_path,
                    "summary": {k: run.news["report"].get(k) for k in (
                        "entry_signals_evaluated", "entry_signals_blocked", "entry_signals_would_block",
                        "trades_allowed", "blocked_by_state")}}})
            if args.news_compare and news is not None:
                import json as _json

                from src.backtest.runner import _jsonable
                pp = None if period.kind == "full" else period
                off = run_strategy(md, app, bt, cfg, period=pp, regime=regime, news=None)
                obs = run if news.cfg.mode == "observe" else run_strategy(
                    md, app, bt, cfg, period=pp, regime=regime, news=news.with_mode("observe"))
                enf = run if news.cfg.mode == "enforce" else run_strategy(
                    md, app, bt, cfg, period=pp, regime=regime, news=news.with_mode("enforce"))
                comp = paired_comparison(off, obs, enf)
                (folder / "news_comparison.json").write_text(_json.dumps(_jsonable(comp), indent=2, default=str), encoding="utf-8")
                print("  news comparison (same data/config):", {k: comp[k]["total_trades"]
                                                                for k in ("without_filter", "observe", "enforce")})
            runs.append(run)
            d = run.diagnostics
            print(f"\n=== {period.label} [{period.to_dict()['start'] or 'start'}..{period.to_dict()['end'] or 'end'}]"
                  f" {run.label}: {len(run.result.trades)} trades -> {folder}"
                  + ("  [CONTAMINATED: overlaps revealed OOS]" if conflicts else ""))
            print("  signal decisions:", d["decision"].value_counts().to_dict())
            if run.signals is not None and not run.signals.empty:
                print("  submitted signals by engine outcome:", run.signals["status"].value_counts().to_dict())
            if not run.result.rejections.empty:
                print("  engine/risk rejections:", run.result.rejections["reason"].value_counts().to_dict())
            if run.news is not None:
                nr = run.news["report"]
                print(f"  news filter ({nr['mode']}): {nr.get('entry_signals_would_block', 0)} of "
                      f"{nr.get('entry_signals_evaluated', 0)} entry signals {'blocked' if nr['mode'] == 'enforce' else 'would be blocked'}"
                      f" {nr.get('blocked_by_state', {})}; calendar gaps {len(nr['calendar']['gaps'])}")
            if run.regime is not None and not run.regime["decisions"].empty:
                dec = run.regime["decisions"]
                print("  regime gate:", dec["suitability"].value_counts().to_dict(),
                      f"blocked {int((~dec['allowed'].astype(bool)).sum())}/{len(dec)}")
            acct = run.result.account
            if acct.get("enabled"):
                print(f"  account: floor {acct['final_floor']:.2f}, min buffer {acct['min_remaining_buffer']:.2f}, "
                      f"breached {acct['breached']} (fresh account per period)")
        with pd.option_context("display.width", 160, "display.precision", 3):
            print(f"  key figures for {period.label} (net = after costs; inspection only, not a ranking):")
            print(summary_table(runs).to_string().replace("\n", "\n  "))
    print("\n" + DISCLAIMER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
