"""Validate the market regime classifiers on history (chronological walk-forward, purged).

Examples:
    python run_regime_validation.py --csv C:\\data\\MES.csv C:\\data\\MNQ.csv --oos-sessions 20
    python run_regime_validation.py --synthetic-sessions 45           (SYNTHETIC method check)
    python run_regime_validation.py --csv ... --reveal-oos             (final OOS, recorded in the ledger)
    python run_regime_validation.py --csv ... --save-model models/regime

Each instrument is evaluated separately. The final out-of-sample sessions stay sealed
until --reveal-oos (trusted committed code + validation ledger required). Exit codes
follow run_backtest.py (1 config/data, 4 OOS conflict, 5 ledger, 6 untrusted code).
No result on synthetic data is evidence about real markets; no result is evidence of
profitability.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src.ai.market_regime import ml
from src.ai.market_regime.config import DEFAULT_REGIME_CONFIG_PATH, load_regime_config
from src.ai.market_regime.identity import instrument_key, model_identity
from src.ai.market_regime.synthetic import generate, truth_on_bars, write_csv
from src.ai.market_regime.validation import build_dataset, evaluate_instrument, purged_train_mask
from src.backtest.manifest import code_identity, environment_info, file_sha256
from src.backtest.validation import (
    LedgerError,
    LedgerMissingError,
    Period,
    ValidationLedger,
    resolve_ledger_path,
    revealed_conflicts,
)
from src.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from src.data.errors import DataError
from src.pipeline import build_market_data
from src.strategy.vwap_pullback_config import DEFAULT_STRATEGY_CONFIG_PATH, load_strategy_config


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Chronological validation of the market regime classifiers.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, nargs="+", help="1-minute CSV per instrument (each evaluated separately)")
    src.add_argument("--synthetic-sessions", type=int, help="generate SYNTHETIC MES and MNQ regime data")
    p.add_argument("--settings", type=Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument("--strategy-config", type=Path, default=DEFAULT_STRATEGY_CONFIG_PATH)
    p.add_argument("--regime-config", type=Path, default=DEFAULT_REGIME_CONFIG_PATH)
    p.add_argument("--oos-sessions", type=int, default=10, help="final out-of-sample sessions (sealed)")
    p.add_argument("--reveal-oos", action="store_true")
    p.add_argument("--allow-oos-reuse", action="store_true")
    p.add_argument("--init-ledger", action="store_true")
    p.add_argument("--ledger", type=Path, default=None)
    p.add_argument("--save-model", type=Path, default=None,
                   help="train on the development sessions only and save calibration + ML artifacts here")
    p.add_argument("--output-dir", type=Path, default=Path("output/regime_validation"))
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    ledger_path, canonical = resolve_ledger_path(args.ledger)
    ledger = ValidationLedger(ledger_path)
    try:
        try:
            records = ledger.records()
        except LedgerMissingError:
            records = []
        app = load_config(args.settings)
        rc = load_regime_config(args.regime_config)
        sc = load_strategy_config(args.strategy_config)
        code = code_identity()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        inputs: list[tuple[Path, pd.DataFrame | None]] = []
        if args.synthetic_sessions:
            data_dir = args.output_dir / "synthetic_data"
            data_dir.mkdir(parents=True, exist_ok=True)
            for i, root in enumerate(("MES", "MNQ")):
                bars, truth = generate(root, args.synthetic_sessions, seed=20250106 + i)
                path = data_dir / f"SYNTHETIC_REGIME_{root}_1m.csv"
                write_csv(bars, path)
                inputs.append((path, truth))
        else:
            inputs = [(p, None) for p in args.csv]
        if args.reveal_oos:
            if not code["trusted"]:
                print(f"Error: refusing to reveal OOS from untrusted code: {', '.join(code['untrusted_reasons'])}",
                      file=sys.stderr)
                return 6
            if not ledger.exists():
                if not args.init_ledger:
                    print(f"Error: no validation ledger at {ledger_path}; use --init-ledger (first time only)",
                          file=sys.stderr)
                    return 5
                ledger.initialise("created by run_regime_validation.py --init-ledger")
    except LedgerError as exc:
        print(f"Error: validation ledger problem: {exc}", file=sys.stderr)
        return 5
    except (ConfigError, DataError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    kw = dict(ema_fast=sc.ema_fast_col, ema_slow=sc.ema_slow_col, atr_col=sc.atr_col, rsi_col=sc.rsi_col)
    summary = {}
    for path, truth in inputs:
        md = build_market_data(path, app)
        sha = file_sha256(path)
        key = instrument_key(md.contract.root, md.is_synthetic)
        ident = model_identity(rc, {key: sha}, code["source_sha256"])
        frame = md.frames[rc.signal_timeframe]
        ds = build_dataset(frame, rc, app.session, **kw)
        sessions = sorted(ds["session"].dropna().unique())
        if len(sessions) <= args.oos_sessions + rc.validation.train_sessions:
            print(f"Error: {key}: {len(sessions)} sessions are not enough", file=sys.stderr)
            return 1
        dev = Period("development", "in_sample", sessions[0], sessions[-args.oos_sessions - 1])
        oos = Period("final_oos", "out_of_sample", sessions[-args.oos_sessions], sessions[-1])
        try:
            records = ledger.records() if ledger.exists() else []
        except LedgerError as exc:
            print(f"Error: validation ledger problem: {exc}", file=sys.stderr)
            return 5
        conflicts = revealed_conflicts(records, key, dev, ident["fingerprint"])
        if args.reveal_oos:
            conflicts += revealed_conflicts(records, key, oos, ident["fingerprint"])
        if conflicts and not args.allow_oos_reuse:
            print(f"Error: {key}: dates overlap OOS revealed under another fingerprint "
                  f"{sorted({(c['start'], c['end']) for c in conflicts})}; use new data or --allow-oos-reuse",
                  file=sys.stderr)
            return 4
        truth_bars = truth_on_bars(truth, ds.index, 5) if truth is not None else None
        report = evaluate_instrument(ds, rc, args.oos_sessions, args.reveal_oos, truth_bars)
        if args.reveal_oos:
            rec = ledger.append({"type": "oos_reveal", "kind": "regime_model", "instrument": key, **oos.to_dict(),
                                 "data_sha256": sha, "fingerprint": ident["fingerprint"],
                                 "source_sha256": code["source_sha256"], "commit": code["commit"],
                                 "reuse_acknowledged": bool(conflicts)})
            report["ledger_seq"] = rec["seq"]
        if args.save_model:
            report["saved_artifacts"] = _save(ds, rc, dev, oos, args.save_model / key.replace(":", "_"), ident)
        folder = args.output_dir / key.replace(":", "_")
        folder.mkdir(parents=True, exist_ok=True)
        manifest = {"instrument": key, "contract": md.contract.label, "synthetic": md.is_synthetic,
                    "data": {"path": str(Path(path).resolve()), "sha256": sha}, "model_identity": ident,
                    "code": code, "environment": environment_info(), "regime_config_text":
                    Path(args.regime_config).read_text(encoding="utf-8"), "ledger": str(ledger_path),
                    "ledger_canonical": canonical, "contaminated": bool(conflicts),
                    "command_line": sys.argv if argv is None else ["run_regime_validation.py", *argv]}
        (folder / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        summary[key] = {k: report.get(f"pooled_walk_forward_{k}", {}) for k in ("rules", "ml")}
        _print(key, report, md.is_synthetic)
    print("\nNo result on synthetic data is evidence about real markets; no result here is evidence of profitability.")
    return 0


def _save(ds, rc, dev: Period, oos: Period, folder: Path, ident: dict) -> dict:
    """Fit the calibration and the ML model on development sessions only (purged before the OOS)."""
    from src.ai.market_regime.confidence import BinnedCalibrator

    folder.mkdir(parents=True, exist_ok=True)
    step = pd.Timedelta(ds["available_at"].iloc[0] - ds.index[0])
    dev_sessions = [s for s in sorted(ds["session"].dropna().unique()) if s <= dev.end]
    test_start = ds.index[ds["session"] >= oos.start][0]
    mask = purged_train_mask(ds, dev_sessions, test_start, step, rc.validation.embargo_bars)
    cal = BinnedCalibrator(meta={"model_identity": ident["fingerprint"], "trained_until": str(dev.end.date())})
    cal.fit(ds.loc[mask, "rule_pred"].to_numpy(float), ds.loc[mask, "rule_trend_score"].to_numpy(float),
            ds.loc[mask, "label"].to_numpy(float))
    cal_sha = cal.save(folder / "rules_calibration.json")
    model = ml.fit(rc.ml, ml.feature_matrix(ds[mask]), ds.loc[mask, "label"].to_numpy(float))
    mid = ml.save_model(model, folder / "regime_model.pkl", ident | {"trained_until": str(dev.end.date())})
    return {"calibration": str(folder / "rules_calibration.json"), "calibration_sha256": cal_sha,
            "model": str(folder / "regime_model.pkl"), "model_sha256": mid["artifact_sha256"]}


def _print(key: str, report: dict, synthetic: bool) -> None:
    print(f"\n=== {key}{' (SYNTHETIC)' if synthetic else ''}: {report['sessions']} sessions, "
          f"{report['labelled_valid_bars']} labelled bars, labels {report['label_distribution']}")
    for name in ("rules", "ml"):
        r = report.get(f"pooled_walk_forward_{name}")
        if r and r.get("n"):
            pc = {k: (round(v["precision"], 2), round(v["recall"], 2)) for k, v in r["per_class"].items()}
            print(f"  walk-forward {name}: accuracy {r['accuracy']:.3f} vs majority baseline "
                  f"{r['majority_baseline_accuracy']:.3f}; precision/recall {pc}")
    fo = report["final_oos"]
    print(f"  final OOS: {fo if isinstance(fo, str) else 'revealed (see report.json)'}")


if __name__ == "__main__":
    sys.exit(main())
