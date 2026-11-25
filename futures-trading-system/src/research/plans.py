"""Frozen research split plans (Sprint 12): explicit train / validation / OOS segments on named datasets.

``config/research/plans/<plan_id>.toml`` + the append-only index ``PLANS.toml`` (SHA-256 of the plan file,
and a copy of every OOS segment so the protection survives even if a plan file is deleted)::

    [plan]
    id = "realdata-v1"
    description = "..."
    [[datasets]]
    instrument = "MES"
    ref = "databento.mes.1m.cont@v1"
    manifest_sha256 = "<64 hex>"
    [segments]
    train = ["2021-01-04", "2023-06-30"]
    validation = ["2023-07-10", "2024-06-28"]
    oos = ["2024-07-08", "2025-06-27"]
    purge_trade_dates = 5          # gap between train and validation
    embargo_trade_dates = 5        # gap between validation and OOS
    [frozen]
    strategies = ["vwap_trend_pullback@V0", "vwap_mean_reversion@V1-C2-SWING"]
    backtest_config_sha256 = "<64 hex>"

Rules: segments are chronological and separated by at least the purge / embargo trade dates; every dataset
is VALIDATED or better, unadjusted, and its manifest hash is pinned; every strategy version is registered
and unmodified; registration is append-only. Once registered, the OOS segment of each instrument is
RESERVED: no research attempt may touch it, and it can be revealed only through the plan, once.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.manifest import file_sha256
from src.config import PROJECT_ROOT, ConfigError
from src.research.params import content_sha256

PLANS_ENV = "FTS_PLANS_DIR"
DEFAULT_DIR = PROJECT_ROOT / "config" / "research" / "plans"
_ID = re.compile(r"^[a-z0-9][a-z0-9_.\-]{2,63}$")


def plans_dir() -> Path:
    raw = os.environ.get(PLANS_ENV)
    return Path(raw).resolve() if raw else DEFAULT_DIR


def load_index(d: Path | None = None) -> dict[str, dict[str, Any]]:
    p = (d or plans_dir()) / "PLANS.toml"
    if not p.is_file():
        return {}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"PLANS.toml is not valid TOML: {exc}") from exc
    if set(raw) - {"plans"}:
        raise ConfigError("PLANS.toml may only contain [plans]")
    out = {}
    for pid, e in (raw.get("plans") or {}).items():
        if not _ID.match(pid) or not re.fullmatch(r"[0-9a-f]{64}", str(e.get("sha256", ""))) \
                or not isinstance(e.get("oos"), dict):
            raise ConfigError(f"PLANS.toml entry {pid!r} is malformed")
        out[pid] = e
    return out


def reserved_oos(d: Path | None = None) -> list[dict[str, Any]]:
    """Every registered plan's OOS segment per instrument key (from the index: survives a deleted plan file)."""
    rows = []
    for pid, e in load_index(d).items():
        for inst, (a, b) in e["oos"].items():
            rows.append({"plan": pid, "instrument": inst, "start": a, "end": b})
    return rows


def _seg(v: Any, name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    if not (isinstance(v, list) and len(v) == 2):
        raise ConfigError(f"segments.{name} must be [start, end]")
    a, b = pd.Timestamp(v[0]), pd.Timestamp(v[1])
    if b < a:
        raise ConfigError(f"segments.{name} ends before it starts")
    return a, b


def parse_plan(path: Path, app, store) -> dict[str, Any]:
    """Load and fully validate a plan file (datasets, chronology, strategy versions, configuration)."""
    from src.ai.market_regime.identity import instrument_key
    from src.histdata.store import RANK
    from src.research.params import load_version

    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read plan {path}: {exc}") from exc
    if set(raw) - {"plan", "datasets", "segments", "frozen"}:
        raise ConfigError(f"unknown plan section(s) {sorted(set(raw) - {'plan', 'datasets', 'segments', 'frozen'})}")
    pid = (raw.get("plan") or {}).get("id", "")
    if not _ID.match(pid) or Path(path).stem != pid:
        raise ConfigError("[plan] id must be a valid id equal to the file name")
    seg = raw.get("segments") or {}
    tr, va, oo = _seg(seg.get("train"), "train"), _seg(seg.get("validation"), "validation"), _seg(seg.get("oos"), "oos")
    purge, embargo = int(seg.get("purge_trade_dates", -1)), int(seg.get("embargo_trade_dates", -1))
    if purge < 1 or embargo < 1:
        raise ConfigError("purge_trade_dates and embargo_trade_dates must be >= 1")
    gap1 = int(np.busday_count(tr[1].date(), va[0].date())) - 1
    gap2 = int(np.busday_count(va[1].date(), oo[0].date())) - 1
    if not (tr[1] < va[0] and va[1] < oo[0]):
        raise ConfigError("segments must be chronological: train < validation < oos, without overlap")
    if gap1 < purge or gap2 < embargo:
        raise ConfigError(f"segment gaps ({gap1}, {gap2} trade dates) are smaller than purge {purge} / embargo {embargo}")
    datasets = []
    for d in raw.get("datasets") or []:
        ds = store.load(d.get("ref", ""))
        if ds.manifest["manifest_sha256"] != d.get("manifest_sha256"):
            raise ConfigError(f"{ds.ref}: manifest hash differs from the plan (dataset substituted)")
        if RANK[ds.status] < RANK["VALIDATED"]:
            raise ConfigError(f"{ds.ref} is {ds.status}: a plan needs VALIDATED datasets")
        if ds.manifest["adjustment"] != "none":
            raise ConfigError(f"{ds.ref} is adjusted: refused")
        if d.get("instrument") not in ds.manifest["roots"]:
            raise ConfigError(f"{ds.ref} does not contain {d.get('instrument')}")
        first, last = pd.Timestamp(ds.manifest["first_bar_utc"]), pd.Timestamp(ds.manifest["last_bar_utc"])
        if first.tz_localize(None).normalize() > tr[0] or last.tz_localize(None).normalize() < oo[1]:
            raise ConfigError(f"{ds.ref} ({first.date()}..{last.date()}) does not cover the plan "
                              f"({tr[0].date()}..{oo[1].date()})")
        datasets.append({"instrument": d["instrument"], "ref": ds.ref, "manifest_sha256": d["manifest_sha256"],
                         "key": instrument_key(d["instrument"], ds.manifest["synthetic"]),
                         "synthetic": ds.manifest["synthetic"]})
    if not datasets:
        raise ConfigError("a plan needs at least one dataset")
    fr = raw.get("frozen") or {}
    strategies = list(fr.get("strategies") or [])
    if not strategies:
        raise ConfigError("[frozen] strategies must list the strategy versions under test")
    for s in strategies:
        sid, _, ver = s.partition("@")
        load_version(sid, ver, app)              # registered and unmodified, or raises
    from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH

    if fr.get("backtest_config_sha256") != file_sha256(DEFAULT_BACKTEST_CONFIG_PATH):
        raise ConfigError("[frozen] backtest_config_sha256 does not match config/backtest.toml (costs / slippage "
                          "changed since the plan was frozen)")
    f = lambda t: str(t.date())  # noqa: E731
    return {"id": pid, "datasets": datasets, "train": [f(tr[0]), f(tr[1])], "validation": [f(va[0]), f(va[1])],
            "oos": [f(oo[0]), f(oo[1])], "purge_trade_dates": purge, "embargo_trade_dates": embargo,
            "strategies": strategies, "backtest_config_sha256": fr["backtest_config_sha256"],
            "sha256": content_sha256(Path(path)), "description": (raw.get("plan") or {}).get("description", "")}


def register(plan_id: str, app, store, d: Path | None = None) -> dict[str, Any]:
    d = d or plans_dir()
    idx = load_index(d)
    if plan_id in idx:
        raise ConfigError(f"plan {plan_id} is already registered: plans are immutable")
    p = parse_plan(d / f"{plan_id}.toml", app, store)
    oos = {x["key"]: p["oos"] for x in p["datasets"]}
    text = (d / "PLANS.toml").read_text(encoding="utf-8") if (d / "PLANS.toml").is_file() else \
        "# Registered (frozen) research plans. Entries are only ever appended.\n"
    entries = ", ".join('"%s" = ["%s", "%s"]' % (k, a, b) for k, (a, b) in oos.items())
    text += ('\n[plans."%s"]\nsha256 = "%s"\nregistered = "%s"\noos = { %s }\n'
             % (plan_id, p["sha256"], pd.Timestamp.now(tz="UTC").date(), entries))
    (d / "PLANS.toml").write_text(text, encoding="utf-8")
    return p


def load_plan(plan_id: str, app, store, d: Path | None = None) -> dict[str, Any]:
    d = d or plans_dir()
    e = load_index(d).get(plan_id)
    if e is None:
        raise ConfigError(f"plan {plan_id} is not registered")
    path = d / f"{plan_id}.toml"
    if not path.is_file() or content_sha256(path) != e["sha256"]:
        raise ConfigError(f"plan {plan_id} was modified or removed after registration: plans are immutable")
    return parse_plan(path, app, store)


__all__ = ["register", "load_plan", "parse_plan", "reserved_oos", "load_index", "PLANS_ENV"]
