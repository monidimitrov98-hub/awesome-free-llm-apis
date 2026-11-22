"""AnalyticsLibrary: the set of journals analysed, filters, report assembly, validation status and safe
exports. READ-ONLY: journals are opened for reading; nothing is written except explicit CLI exports.

Session types are never mixed into one headline figure: performance and cost reports REQUIRE a single
session type (MOCK / REPLAY / SHADOW / MONITOR); the live-vs-replay comparison is the only place two
types appear side by side, and only for compatible identities.
"""

from __future__ import annotations

import csv
import glob
import io
import json
import math
import re
import threading
import tomllib
from pathlib import Path
from typing import Any

import pandas as pd

from src.analytics.shadow import reports
from src.analytics.shadow.events import JournalSource
from src.analytics.shadow.model import decisions, signals, trades
from src.analytics.shadow.stats import sample_level
from src.config import PROJECT_ROOT, ConfigError

DEFAULT_ANALYTICS_CONFIG = PROJECT_ROOT / "config" / "analytics.toml"
SESSION_TYPES = ("MOCK", "REPLAY", "SHADOW", "MONITOR", "UNKNOWN")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE = re.compile(r"^[\w.:@\-]{1,80}$")
MAX_ROWS = 20_000


def load_analytics_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_ANALYTICS_CONFIG
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read analytics config {p}: {exc}") from exc
    unknown = set(raw) - {"sources", "statistics", "scenarios", "stress"}
    if unknown:
        raise ConfigError(f"Unknown section(s) {sorted(unknown)} in {p}")
    src = raw.get("sources", {})
    stats = raw.get("statistics", {})
    for k in ("bootstrap_iterations", "counterfactual_horizon_minutes", "bootstrap_seed"):
        v = stats.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 100_000):
            raise ConfigError(f"[statistics] {k} must be a non-negative integer")
    for sc in raw.get("scenarios", []):
        for k, v in sc.items():
            if k != "name" and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0):
                raise ConfigError(f"scenario {sc.get('name')!r}: {k} must be a non-negative number")
    return {"journals": list(src.get("journals", [])), "max_journals": int(src.get("max_journals", 50)),
            "statistics": stats, "scenarios": list(raw.get("scenarios", [])), "stress": list(raw.get("stress", []))}


def resolve_journals(patterns: list[str], limit: int) -> list[Path]:
    """Expand configured globs, staying inside the project (no path traversal)."""
    root = PROJECT_ROOT.resolve()
    out: list[Path] = []
    for pat in patterns:
        if Path(pat).is_absolute() or ".." in Path(pat).parts:
            raise ConfigError(f"journal pattern {pat!r} must be relative to the project and must not contain '..'")
        for m in sorted(glob.glob(str(root / pat))):
            p = Path(m).resolve()
            if root in p.parents and p.is_file() and p not in out:
                out.append(p)
    return out[:limit]


class Filters:
    """Validated report filters (everything optional)."""

    def __init__(self, root=None, start=None, end=None, session_type=None, strategy_version=None, regime=None,
                 provider=None, session_id=None) -> None:
        if root not in (None, "MES", "MNQ"):
            raise ValueError("root must be MES or MNQ")
        for d in (start, end):
            if d is not None and not _DATE.match(d):
                raise ValueError("dates must be YYYY-MM-DD")
        if session_type not in (None, *SESSION_TYPES):
            raise ValueError("unknown session type")
        for v in (strategy_version, regime, provider, session_id):
            if v is not None and not _SAFE.match(str(v).replace(" ", "_")):
                raise ValueError("invalid filter value")
        self.root, self.start, self.end, self.session_type = root, start, end, session_type
        self.strategy_version, self.regime, self.provider, self.session_id = strategy_version, regime, provider, session_id

    def match(self, row: dict[str, Any], *, day_key: str = "day", root_key: str = "root") -> bool:
        day = str(row.get(day_key) or "")[:10]
        if self.root and row.get(root_key) != self.root:
            return False
        if self.start and (not day or day < self.start):
            return False
        if self.end and (not day or day > self.end):
            return False
        if self.session_type and row.get("session_type") != self.session_type:
            return False
        if self.strategy_version and row.get("strategy_version") != self.strategy_version:
            return False
        if self.regime and str(row.get("regime_trend") or "unavailable") != self.regime:
            return False
        if self.provider and row.get("provider") != self.provider:
            return False
        if self.session_id and f"{row.get('run_id')}:{day}:{row.get(root_key)}" != self.session_id \
                and row.get("session_id") != self.session_id:
            return False
        return True

    def without_regime(self) -> "Filters":
        return Filters(self.root, self.start, self.end, self.session_type, self.strategy_version, None, self.provider,
                       self.session_id)

    def describe(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v is not None}


class AnalyticsLibrary:
    def __init__(self, paths: list[Path], cfg: dict[str, Any] | None = None, *, extra: dict[str, Path] | None = None) -> None:
        self.cfg = cfg or load_analytics_config()
        self.sources: dict[str, JournalSource] = {}
        self._lock = threading.RLock()
        for p in paths:
            self.sources[str(p)] = JournalSource(p)
        for label, p in (extra or {}).items():
            self.sources[str(p)] = JournalSource(p, label=label)
        self._cache: dict = {}
        self.errors: dict[str, str] = {}
        self.duplicates: dict[str, str] = {}

    @classmethod
    def from_config(cls, cfg_path=None, extra: dict[str, Path] | None = None) -> "AnalyticsLibrary":
        cfg = load_analytics_config(cfg_path)
        return cls(resolve_journals(cfg["journals"], cfg["max_journals"]), cfg, extra=extra)

    def add(self, path: Path, label: str | None = None) -> None:
        with self._lock:
            if str(path) not in self.sources:
                self.sources[str(path)] = JournalSource(path, label=label)

    def close(self) -> None:
        for s in self.sources.values():
            s.close()

    # ------------------------------------------------------------------ data
    def refresh(self) -> list[JournalSource]:
        ok = []
        with self._lock:
            for key, s in list(self.sources.items()):
                if not s.path.exists():
                    self.errors[s.label] = "journal file not found"
                    continue
                try:
                    s.refresh()
                    self.errors.pop(s.label, None)
                    ok.append(s)
                except Exception as exc:  # noqa: BLE001 -- a broken journal is reported, never analysed
                    self.errors[s.label] = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.duplicates = {}
            seen: dict[str, JournalSource] = {}
            unique = []
            for s in ok:  # a copied journal is the SAME run: analysing both would double-count it
                first = seen.get(s.data.run_id)
                if first is None:
                    seen[s.data.run_id] = s
                    unique.append(s)
                else:
                    self.duplicates[s.label] = f"duplicate of {first.label} (same run {s.data.run_id}); analysed once"
        return unique

    def _tables(self, srcs: list[JournalSource]) -> dict[str, list[dict]]:
        key = tuple((id(s), s.data.rows) for s in srcs)
        hit = self._cache.get(("tables", key))
        if hit is not None:
            return hit
        tr, sg, dc, ss = [], [], [], []
        bars_cache: dict = self._cache.setdefault("bars", {})
        for s in srcs:
            t = trades(s, bars_cache)
            tr += t
            sg += signals(s)
            dc += decisions(s)
            ss += reports.session_records(s, t, bars_cache)
        out = {"trades": tr, "signals": sg, "decisions": dc, "sessions": ss}
        self._cache = {k: v for k, v in self._cache.items() if k == "bars" or k[0] != "tables"}
        self._cache[("tables", key)] = out
        return out

    # ------------------------------------------------------------------ reports
    def sessions(self, f: Filters) -> dict[str, Any]:
        srcs = self.refresh()
        t = self._tables(srcs)
        fs = f.without_regime()  # a session has no single regime: the regime filter applies to signals / trades
        rows = [r for r in t["sessions"] if fs.match(r, day_key="trading_date", root_key="instrument")]
        return {"filters": f.describe(), "sessions": rows[-MAX_ROWS:], "count": len(rows),
                "session_types": sorted({r["session_type"] for r in rows}), "journals": self.journal_status()}

    def _need_single_type(self, rows: list[dict], f: Filters) -> str | None:
        types = sorted({r.get("session_type") for r in rows})
        if f.session_type is None and len(types) > 1:
            return f"several session types {types}: choose one (results of different session types are never combined)"
        return None

    def performance(self, f: Filters) -> dict[str, Any]:
        t = self._tables(self.refresh())
        tr = [x for x in t["trades"] if f.match(x)]
        err = self._need_single_type(tr, f)
        if err:
            return {"filters": f.describe(), "refused": err}
        out = reports.performance(tr, self.cfg["statistics"])
        out["filters"] = f.describe()
        out["session_type"] = f.session_type or (tr[0]["session_type"] if tr else None)
        out["trades"] = out["trades"][-MAX_ROWS:]
        return out

    def costs(self, f: Filters) -> dict[str, Any]:
        srcs = self.refresh()
        t = self._tables(srcs)
        tr = [x for x in t["trades"] if f.match(x)]
        err = self._need_single_type(tr, f)
        if err:
            return {"filters": f.describe(), "refused": err}
        ticks, account = {}, None
        for s in srcs:
            for r in s.data.identities:
                ticks.update(r.payload.get("contracts") or {})
        account = self.account_rules()
        return {**reports.cost_scenarios(tr, self.cfg["scenarios"], ticks, account), "filters": f.describe()}

    def execution(self, f: Filters) -> dict[str, Any]:
        rows, by_state, notes = [], {}, set()
        for s in self.refresh():
            e = reports.execution_quality(s)
            rows += [r for r in e["rows"] if f.match(r)]
            notes.update(e["notes"])
        err = self._need_single_type(rows, f)
        if err:
            return {"filters": f.describe(), "refused": err}
        merged = _merge_exec(rows)
        tr = [x for x in self._tables(self.refresh())["trades"] if f.match(x)]
        merged["ambiguous_ohlc_exit_bars"] = sum(1 for x in tr if x.get("ohlc_ambiguous_exit_bar"))
        merged["ambiguous_ohlc_basis"] = ("trades whose exit minute bar touched both the stop and the target: the "
                                          "intrabar order is unknown from OHLC (relevant to bar replay fills)")
        merged["notes"] = sorted(notes)
        merged["filters"] = f.describe()
        return merged

    def regimes(self, f: Filters) -> dict[str, Any]:
        srcs = self.refresh()
        t = self._tables(srcs)
        tr = [x for x in t["trades"] if f.match(x)]
        err = self._need_single_type(tr + [s for s in t["signals"] if f.match(s)], f)
        if err:
            return {"filters": f.describe(), "refused": err}
        keep = [s for s in srcs if self._src_matches(s, f)]
        out = reports.regimes(keep, tr, keep=f.match)
        out["filters"] = f.describe()
        return out

    def blocked(self, f: Filters) -> dict[str, Any]:
        srcs = [s for s in self.refresh() if self._src_matches(s, f)]
        types = sorted({self._type(s) for s in srcs if s.data.decisions or s.data.strategy_rejects})
        if f.session_type is None and len(types) > 1:
            return {"filters": f.describe(), "refused": f"several session types {types}: choose one (results of "
                                                         "different session types are never combined)"}
        out = reports.blocked(srcs, True, self._cache.setdefault("bars", {}),
                              int(self.cfg["statistics"].get("counterfactual_horizon_minutes", 240)),
                              keep=f.without_regime().match)
        out["filters"] = f.describe()
        return out

    def reliability(self, f: Filters) -> dict[str, Any]:
        srcs = [s for s in self.refresh() if self._src_matches(s, f)]
        return {"filters": f.describe(), "runs": [{"run": s.label, "session_type": self._type(s), **reports.reliability(s)}
                                                  for s in srcs]}

    def account(self, f: Filters) -> dict[str, Any]:
        srcs = [s for s in self.refresh() if self._src_matches(s, f)]
        t = self._tables(srcs)
        rules = self.account_rules() or {}
        out = []
        for s in srcs:
            tr = [x for x in t["trades"] if x["run_id"] == s.data.run_id and f.match(x)]
            out.append({"run": s.label, "session_type": self._type(s),
                        **reports.account_survival(s, tr, rules, self.cfg["stress"])})
        return {"filters": f.describe(), "runs": out}

    def compare(self, root: str, a_type: str = "REPLAY", b_type: str = "SHADOW", f: Filters | None = None) -> dict[str, Any]:
        f = f or Filters(root=root)
        srcs = self.refresh()
        t = self._tables(srcs)

        def pick(kind):
            sg = [s for s in t["signals"] if s["root"] == root and s["session_type"] == kind
                  and Filters(start=f.start, end=f.end).match(s)]
            tr = [x for x in t["trades"] if x["root"] == root and x["session_type"] == kind
                  and Filters(start=f.start, end=f.end).match(x)]
            ident = []
            contributing = {x["run_id"] for x in sg + tr}
            for s in srcs:
                if s.data.run_id not in contributing:
                    continue
                for r in s.data.identities:
                    if r.payload.get("session_type") == kind:
                        p = r.payload
                        reg = p.get("regime") or {}
                        ident.append({"strategy_config_sha256": (p.get("strategy") or {}).get("config_sha256"),
                                      "regime_identity": (f"{reg.get('model_version')}@{str(reg.get('config_sha256'))[:12]}"
                                                          if reg.get("enabled") else "disabled"),
                                      "contracts": (p.get("contracts") or {}).get(root),
                                      "session_rules_sha256": p.get("session_rules_sha256"),
                                      "signal_timeframe": (p.get("strategy") or {}).get("signal_timeframe")})
            return sg, tr, ident
        a, b = pick(a_type), pick(b_type)
        if not (a[0] or a[1]) or not (b[0] or b[1]):
            return {"root": root, "comparable": False,
                    "reasons": [f"no {k} observations for {root}" for k, side in ((a_type, a), (b_type, b)) if not (side[0] or side[1])]}
        out = reports.compare(a[0], a[1], b[0], b[1], a_type, b_type, a[2], b[2])
        out["root"] = root
        return out

    def validation(self) -> dict[str, Any]:
        from src.analytics.shadow.validation import validation_status

        srcs = self.refresh()
        return validation_status(srcs, self._tables(srcs), self.errors, self.duplicates)

    # ------------------------------------------------------------------ helpers
    def _type(self, s: JournalSource) -> str:
        ids = s.data.identities
        types = sorted({i.payload.get("session_type", "UNKNOWN") for i in ids}) or ["UNKNOWN"]
        return types[0] if len(types) == 1 else "MIXED:" + "+".join(types)

    def _src_matches(self, s: JournalSource, f: Filters) -> bool:
        if f.session_type and self._type(s) != f.session_type:
            return False
        if f.root and s.data.created.get("instruments") and f.root not in s.data.created["instruments"]:
            return False
        if f.strategy_version and not any((i.payload.get("strategy") or {}).get("version") == f.strategy_version
                                          for i in s.data.identities):
            return False
        return True

    def journal_status(self) -> list[dict[str, Any]]:
        out = []
        for s in self.sources.values():
            out.append({"run": s.label, "run_id": s.data.run_id, "session_type": self._type(s), "rows": s.data.rows,
                        "first": s.data.first_ts, "last": s.data.last_ts,
                        "error": self.errors.get(s.label) or self.duplicates.get(s.label)})
        return out

    def account_rules(self) -> dict[str, Any] | None:
        try:
            from src.backtest.config import load_backtest_config

            bt = load_backtest_config()
        except ConfigError:
            return None
        r = bt.account_rules
        return {"enabled": r.enabled, "profile": getattr(r, "profile", None), "drawdown_type": r.drawdown_type,
                "max_drawdown": r.max_drawdown, "safety_buffer": r.safety_buffer, "flatten_buffer": r.flatten_buffer,
                "initial_balance": float(bt.initial_capital), "max_daily_loss": bt.risk.max_daily_loss or None,
                "risk_per_trade": bt.sizing.risk_per_trade if bt.sizing.method == "risk" else None,
                "max_tick_value": max((c.tick_value for c in _contracts().values()), default=0.0)}


def _contracts():
    from src.config import load_config

    return load_config().contracts


def _merge_exec(rows: list[dict]) -> dict[str, Any]:
    from collections import Counter

    from src.analytics.shadow.stats import summary

    entries = [r for r in rows if r["role"] == "entry"]
    stops = [r for r in rows if r["role"] == "stop_loss" and r.get("fill_price") is not None]
    return {"label": reports.SIMULATED, "orders": len(rows), "by_final_state": dict(Counter(r["final_state"] for r in rows)),
            "partial_fill_orders": sum(1 for r in rows if r["partial"]),
            "entry": {k: summary([r.get(k) for r in entries]) for k in
                      ("signal_to_order_s", "order_to_fill_s", "fill_vs_expected_ticks", "spread_at_signal", "spread_at_fill")},
            "exits": {"stop_fills": len(stops), "stop_slippage_ticks": summary([r.get("stop_slippage_ticks") for r in stops]),
                      "stop_gap_fills": sum(1 for r in stops if r.get("fill_reason") == "stop_gap_through"),
                      "target_fills": sum(1 for r in rows if r["role"] == "take_profit" and r.get("fill_price") is not None),
                      "spread_at_exit": summary([r.get("spread_at_fill") for r in rows if r["role"] in ("stop_loss", "take_profit", "exit")])},
            "rows": rows[-MAX_ROWS:]}


# ---------------------------------------------------------------------------------------------- exports
def _jsonable(v: Any) -> Any:
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.isoformat()
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "item"):
        try:
            return _jsonable(v.item())
        except (TypeError, ValueError):
            pass
    return v


def to_json(report: dict[str, Any], meta: dict[str, Any]) -> str:
    return json.dumps(_jsonable({"meta": meta, "report": report}), indent=2, default=str, allow_nan=False)


def safe_cell(v: Any) -> Any:
    """Neutralise spreadsheet formulas (CSV injection) in text cells."""
    v = _jsonable(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v, default=str)
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def to_csv(rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for k, v in meta.items():  # provenance header lines (commented)
        w.writerow([safe_cell(f"# {k}: {json.dumps(_jsonable(v), default=str)}")])
    cols: list[str] = []
    for r in rows[:MAX_ROWS]:
        for k in r:
            if k not in cols:
                cols.append(k)
    w.writerow(cols)
    for r in rows[:MAX_ROWS]:
        w.writerow([safe_cell(r.get(c)) for c in cols])
    return buf.getvalue()


EXPORTS = ("sessions", "trades", "execution", "reliability", "regimes", "account", "blocked", "costs")


def export_rows(lib: AnalyticsLibrary, kind: str, f: Filters) -> tuple[list[dict], dict]:
    if kind not in EXPORTS:
        raise ValueError("unknown export")
    if kind == "sessions":
        rep = lib.sessions(f)
        return rep["sessions"], rep
    if kind == "trades":
        rep = lib.performance(f)
        return rep.get("trades", []), rep
    if kind == "execution":
        rep = lib.execution(f)
        return rep.get("rows", []), rep
    if kind == "reliability":
        rep = lib.reliability(f)
        return [{"run": r["run"], "session_type": r["session_type"],
                 **{k: v for k, v in r.items() if not isinstance(v, dict) and k not in ("run", "session_type")}}
                for r in rep["runs"]], rep
    if kind == "regimes":
        rep = lib.regimes(f)
        return rep.get("buckets", []), rep
    if kind == "account":
        rep = lib.account(f)
        return [{k: v for k, v in r.items() if not isinstance(v, dict)} for r in rep["runs"]], rep
    if kind == "costs":
        rep = lib.costs(f)
        return rep.get("scenarios", []), rep
    rep = lib.blocked(f)
    return rep["by_reason"], rep


def export_meta(kind: str, f: Filters, rep: dict) -> dict[str, Any]:
    return {"report": kind, "generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "filters": f.describe(),
            "session_type": f.session_type or rep.get("session_type") or "as listed per row",
            "label": rep.get("label") or "SIMULATED / analytical results; not brokerage executions",
            "limitations": "Simulated fills; counterfactuals hypothetical; small samples are not evidence; see "
                           "docs/sprint8_shadow_analytics.md",
            "sample": sample_level(len(rep.get("trades") or [])) if kind == "trades" else None}
