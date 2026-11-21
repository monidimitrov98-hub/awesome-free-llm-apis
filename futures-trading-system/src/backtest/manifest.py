"""Run manifests: everything needed to reproduce a backtest and verify the result.

A manifest records the exact configuration texts (settings, backtest, strategy,
account profile), parsed snapshots, the data file's SHA-256, the period, the CODE
IDENTITY (git commit, clean/dirty tree, SHA-256 of every Python source file), library
versions and a fingerprint of the results. :func:`replay` verifies the data hash, the
manifest's own consistency and the code identity before re-running and comparing.

Replay statuses (only ``EXACT`` is a successful exact reproduction):

* ``EXACT``                      same trusted code, same data, identical result fingerprint
* ``EXACT_DIFFERENT_DEPENDENCIES`` identical fingerprint but other library versions
* ``APPROXIMATE``                other library versions, fingerprint differs, same trade
                                 count and net profit within 1e-6 (numerical, not exact)
* ``MISMATCH``                   same code but different results
* ``UNTRUSTED_CODE``             code differs, or either side is dirty / has an unknown commit
* ``MANIFEST_INCONSISTENT``      embedded configuration does not match its recorded hash
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

SCHEMA_VERSION = 2
SOURCE_PATTERNS = ("src/**/*.py", "run_backtest.py", "run_pipeline.py")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


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
    status = run("status", "--porcelain", "--untracked-files=all", "--", ".")
    return {"commit": commit or "unknown", "dirty": bool(status) if status is not None else None}


def source_fingerprint(root: Path = PROJECT_ROOT) -> tuple[str, int]:
    """Deterministic SHA-256 over the relevant Python source (paths + contents, CRLF normalised)."""
    files = sorted({p for pattern in SOURCE_PATTERNS for p in root.glob(pattern) if p.is_file()})
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(root).as_posix().encode("utf-8") + b"\0")
        h.update(p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest(), len(files)


def code_identity(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Which code is running. ``trusted`` requires a known commit and a clean working tree."""
    git = git_info(root)
    source, n = source_fingerprint(root)
    reasons = []
    if not _COMMIT.match(git["commit"] or ""):
        reasons.append("git commit unknown")
    if git["dirty"] is not False:
        reasons.append("working tree has uncommitted changes" if git["dirty"] else "git status unavailable")
    return {"commit": git["commit"], "dirty": git["dirty"], "source_sha256": source, "source_files": n,
            "trusted": not reasons, "untrusted_reasons": reasons}


def validation_fingerprint(config_texts: dict[str, str], r_multiple: float, source_sha256: str) -> str:
    """Identity of what is being validated: configuration texts, target R and source code."""
    return text_sha256(json.dumps({"config": config_texts, "r_multiple": repr(float(r_multiple)),
                                   "source_sha256": source_sha256}, sort_keys=True))


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
        "code": kw.get("code") or code_identity(),
        "validation": kw.get("validation"),
        "account_state": "each period starts a fresh simulated account (not a continuous survival simulation)",
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
    """Re-run a backtest from its manifest; see the module docstring for the statuses.

    Raises ConfigError if the data file is missing or altered.
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
    reasons: list[str] = []
    result: dict[str, Any] = {"data_sha256": actual_hash, "git_commit_then": m.get("git", {}).get("commit"),
                              "git_commit_now": git_info()["commit"], "reasons": reasons}
    if config_hash(texts) != m.get("config_hash"):
        reasons.append("embedded configuration does not match the recorded config hash")
        return result | {"status": "MANIFEST_INCONSISTENT", "reproduced": False, "expected": None, "actual": None}

    then = m.get("code") or {}
    now = code_identity()
    if not then.get("trusted"):
        reasons.append(f"original run used untrusted code: {then.get('untrusted_reasons') or 'no code identity'}")
    if not now["trusted"]:
        reasons.append(f"current code is untrusted: {now['untrusted_reasons']}")
    if then.get("source_sha256") != now["source_sha256"]:
        reasons.append("Python source differs from the code that produced the manifest")
    if then.get("commit") != now["commit"]:
        reasons.append(f"git commit differs ({then.get('commit')} vs {now['commit']})")
    code_ok = then.get("trusted") and now["trusted"] and then.get("source_sha256") == now["source_sha256"]

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
    expected = m["results"]["fingerprint"]
    env_then, env_now = m.get("environment", {}), environment_info()
    deps_same = all(env_then.get(k) == env_now.get(k) for k in ("python", "pandas", "numpy"))
    if not deps_same:
        reasons.append(f"dependency versions differ: then {[env_then.get(k) for k in ('python', 'pandas', 'numpy')]} "
                       f"now {[env_now.get(k) for k in ('python', 'pandas', 'numpy')]}")
    if not code_ok:
        status = "UNTRUSTED_CODE"
    elif actual == expected:
        status = "EXACT" if deps_same else "EXACT_DIFFERENT_DEPENDENCIES"
    elif not deps_same and int(run.result.metrics["total_trades"]) == m["results"]["total_trades"] and \
            abs(run.result.metrics["net_profit"] - m["results"]["net_profit"]) <= 1e-6 * max(1.0, abs(m["results"]["net_profit"])):
        status = "APPROXIMATE"
    else:
        status = "MISMATCH"
        reasons.append("result fingerprint differs")
    return result | {"status": status, "reproduced": status == "EXACT", "expected": expected, "actual": actual}
