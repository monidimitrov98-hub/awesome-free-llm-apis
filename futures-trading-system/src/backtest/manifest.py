"""Run manifests: everything needed to reproduce a backtest and verify the result.

A manifest records the exact configuration texts (settings, backtest, strategy,
account profile), parsed snapshots, the data file's SHA-256, the period, the git
commit, library versions and a fingerprint of the results. :func:`replay` rebuilds
the configuration from the manifest, checks the data hash, re-runs the backtest and
compares the result fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError

SCHEMA_VERSION = 1


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_info(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10,
                                  check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--", ".")
    return {"commit": commit or "unknown", "dirty": bool(status) if status is not None else None}


def environment_info() -> dict[str, Any]:
    def version(pkg: str) -> str | None:
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return None

    return {"python": sys.version.split()[0], "platform": platform.platform(),
            "pandas": pd.__version__, "numpy": version("numpy"), "tzdata": version("tzdata")}


def snapshot(obj: Any) -> Any:
    """JSON-friendly snapshot of dataclasses, timestamps, etc."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: snapshot(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): snapshot(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [snapshot(v) for v in obj]
    if isinstance(obj, float) and obj != obj:
        return "nan"
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def results_fingerprint(trades: pd.DataFrame, equity: pd.DataFrame) -> str:
    """Deterministic hash of the trade log and the equity curve."""
    return text_sha256(trades.to_csv(index=False) + "\n#equity\n" + equity.to_csv())


def config_hash(config_texts: dict[str, str]) -> str:
    """Identity of a configuration: hash of the exact configuration file texts."""
    return text_sha256(json.dumps(config_texts, sort_keys=True))


def read_config_texts(settings_path: Path, backtest_path: Path, strategy_path: Path) -> dict[str, str]:
    texts = {"settings": settings_path.read_text(encoding="utf-8"),
             "backtest": backtest_path.read_text(encoding="utf-8"),
             "strategy": strategy_path.read_text(encoding="utf-8")}
    m = re.search(r'^\s*protection_profile\s*=\s*"([^"]*)"', texts["backtest"], flags=re.M)
    if m and m.group(1).strip():
        prof = Path(m.group(1))
        prof = prof if prof.is_absolute() else PROJECT_ROOT / prof
        texts["account_profile"] = prof.read_text(encoding="utf-8")
    return texts


def build_manifest(**kw: Any) -> dict[str, Any]:
    """Assemble the manifest (see runner.write_outputs for the fields passed)."""
    md, app, bt, strat, result = kw["md"], kw["app"], kw["bt"], kw["strategy_config"], kw["result"]
    base = md.base
    equity = result.equity
    return {
        "schema_version": SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": kw["label"],
        "period": kw["period"],
        "data": {
            "path": str(Path(kw["csv_path"]).resolve()),
            "sha256": kw["data_sha256"],
            "size_bytes": Path(kw["csv_path"]).stat().st_size,
            "rows_read": md.report.rows_read,
            "synthetic": md.is_synthetic,
            "file_first_bar": str(base.index[0]),
            "file_last_bar": str(base.index[-1]),
            "period_first_bar": str(equity.index[0]) if len(equity) else None,
            "period_last_bar": str(equity.index[-1]) if len(equity) else None,
            "sessions_in_period": kw.get("sessions_in_period"),
        },
        "instrument": {"contract": md.contract.label, "contract_source": md.contract_source,
                       "specification": snapshot(md.contract.spec), "expiry": str(md.contract.expiry)},
        "data_settings": {
            "source_timezone": app.data.source_timezone,
            "timestamp_label": app.data.timestamp_label,
            "column_mapping": dict(app.data.columns),
            "session": snapshot(app.session),
            "indicators": snapshot(app.indicators),
            "timeframes": list(app.timeframes),
            "full": snapshot(app.data),
        },
        "strategy": snapshot(strat),
        "backtest": snapshot(bt),
        "execution_assumptions": {
            "slippage_ticks": snapshot(bt.slippage), "commission": snapshot(bt.costs),
            "execution": snapshot(bt.execution), "fill_model": "OHLC bars; market at next bar open; "
            "stops/limits per src/backtest/execution.py; same-bar stop/target ambiguity resolved worst case",
        },
        "account_rules": snapshot(bt.account_rules),
        "config_texts": kw["config_texts"],
        "config_hash": config_hash(kw["config_texts"]),
        "git": git_info(),
        "environment": environment_info(),
        "command_line": kw.get("command_line"),
        "oos": kw.get("oos"),
        "results": {
            "fingerprint": results_fingerprint(result.trades, result.equity),
            "total_trades": int(result.metrics["total_trades"]),
            "net_profit": result.metrics["net_profit"],
        },
    }


def write_manifest(folder: Path, manifest: dict[str, Any]) -> Path:
    path = folder / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return path


def replay(manifest_path: str | Path, csv_override: str | Path | None = None) -> dict[str, Any]:
    """Re-run a backtest from its manifest and compare the result fingerprint.

    Returns a dict with ``reproduced`` (bool), the expected and actual fingerprints and
    any data-hash mismatch. Raises ConfigError if the data file is missing or altered.
    """
    from src.backtest.config import load_backtest_config
    from src.backtest.runner import run_strategy
    from src.backtest.validation import Period, parse_date
    from src.config import load_config
    from src.pipeline import build_market_data
    from src.strategy.vwap_pullback_config import load_strategy_config

    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    csv = Path(csv_override or m["data"]["path"])
    if not csv.is_file():
        raise ConfigError(f"Data file for replay not found: {csv}")
    actual_hash = file_sha256(csv)
    if actual_hash != m["data"]["sha256"]:
        raise ConfigError(f"Data file changed: sha256 {actual_hash} != manifest {m['data']['sha256']}")
    texts = m["config_texts"]
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "settings.toml").write_text(texts["settings"], encoding="utf-8")
        (d / "strategy.toml").write_text(texts["strategy"], encoding="utf-8")
        backtest_text = texts["backtest"]
        if "account_profile" in texts:
            (d / "account.toml").write_text(texts["account_profile"], encoding="utf-8")
            backtest_text = re.sub(r'^(\s*protection_profile\s*=\s*)"[^"]*"',
                                   lambda mm: f'{mm.group(1)}"{(d / "account.toml").as_posix()}"',
                                   backtest_text, flags=re.M)
        (d / "backtest.toml").write_text(backtest_text, encoding="utf-8")
        app = load_config(d / "settings.toml")
        bt = load_backtest_config(d / "backtest.toml")
        strat = load_strategy_config(d / "strategy.toml").with_r_multiple(m["strategy"]["target"]["r_multiple"])
    md = build_market_data(csv, app)
    p = m["period"]
    period = Period(p["label"], p["kind"], parse_date(p["start"]), parse_date(p["end"]), p["fold"])
    run = run_strategy(md, app, bt, strat, label=m["label"], period=period)
    actual = results_fingerprint(run.result.trades, run.result.equity)
    return {"reproduced": actual == m["results"]["fingerprint"], "expected": m["results"]["fingerprint"],
            "actual": actual, "data_sha256": actual_hash, "git_commit_then": m["git"]["commit"],
            "git_commit_now": git_info()["commit"]}
