"""Research runs with statistical safeguards (Sprint 11).

Every run goes through :func:`src.backtest.runner.run_spec` -- the same event-driven BacktestEngine, costs,
slippage, NewsGuard, RiskManager, AccountGuard and (optionally) prop firm compliance engine as every other
backtest. This module adds the research discipline around it:

* every attempted configuration is recorded in the experiment registry BEFORE it runs (failures included);
* dates already revealed out of sample (validation ledger) can never be used for research again;
* sweeps may only vary parameters declared in ``BOUNDS.toml``, inside their declared ranges; derived
  configurations are UNREGISTERED and can never be revealed out of sample or promoted;
* the one-time OOS reveal needs a registered, unmodified version, committed code, a verified ledger, and dates
  that no research attempt of the strategy on that instrument has touched; it is recorded in the ledger
  before any result is written or shown; a second reveal of the same dates under a different configuration
  is refused, the identical configuration can only REPLAY it;
* multiple-testing correction across every attempt; walk-forward with a purge gap; purged k-fold
  cross-validation with an embargo; declared (non-profit) selection objective; cost sensitivity by full re-runs;
* manifests hash the data, the parameter version, the configuration files, the code and the results.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.ai.market_regime.identity import instrument_key
from src.backtest.manifest import (code_identity, environment_info, file_sha256, results_fingerprint, text_sha256)
from src.backtest.validation import (Period, ValidationLedger, make_period, resolve_ledger_path, revealed_conflicts)
from src.config import ConfigError
from src.research.evaluation import daily_pnl, evaluate
from src.research.experiments import ExperimentRegistry, params_sha256
from src.research.params import check_bounds, load_version
from src.research.registry import get_spec
from src.research.stats import day_bootstrap_mean

MAX_GRID = 500
SCOPE = {"full": "full", "train": "train", "validation": "validation", "walk_forward": "wf_out_of_sample",
         "purged_cv": "purged_cv", "oos": "oos", "oos_replay": "oos"}
OBJECTIVE = ("maximise the LOWER bound of the 95% day-bootstrap interval of the mean daily net P&L on the training "
             "sessions (ties: first configuration in sorted parameter order); never net profit")
COST_SCENARIOS = (("as_configured", 1.0, 0), ("commission_x1.5", 1.5, 0), ("commission_x2", 2.0, 0),
                  ("slippage_+1_tick", 1.0, 1), ("slippage_+2_ticks", 1.0, 2), ("stress_x2_+2_ticks", 2.0, 2))


def is_dataset(source: str | Path) -> bool:
    return str(source).startswith("dataset:")


class ResearchError(ConfigError):
    """A research request was refused (the reason is in the message)."""


@dataclass
class ResearchEnv:
    app: Any
    bt: Any
    out_root: Path
    settings_path: Path
    backtest_path: Path
    ledger_path: Path | None = None
    registry: ExperimentRegistry = field(default_factory=ExperimentRegistry)
    regime: Any = None
    news: Any = None
    store: Any = None                          # Sprint 12 DatasetStore (default location when None)
    _md: dict[str, Any] = field(default_factory=dict)

    def market_data(self, csv: str | Path):
        """A CSV path, or ``dataset:<id>@v<N>[#CONTRACT]`` (Sprint 12 dataset, verified on every load)."""
        from src.pipeline import build_market_data

        if is_dataset(csv):
            key = str(csv)
            if key not in self._md:
                ds, contract = self.dataset(csv)
                from src.histdata.market import market_data_from_dataset

                self._md[key] = (market_data_from_dataset(ds, self.app, contract=contract),
                                 ds.manifest["manifest_sha256"])
            return self._md[key]
        key = str(Path(csv).resolve())
        if key not in self._md:
            self._md[key] = (build_market_data(csv, self.app), file_sha256(csv))
        return self._md[key]

    def dataset(self, source: str):
        """(Dataset, contract) for a dataset source; refuses datasets that are not VALIDATED."""
        from src.histdata.store import RANK, DatasetStore

        if self.store is None:
            self.store = DatasetStore()
        ref, _, contract = str(source)[len("dataset:"):].partition("#")
        ds = self.store.load(ref)
        if RANK[ds.status] < RANK["VALIDATED"]:
            raise ResearchError(f"dataset {ds.ref} is {ds.status}: research needs a VALIDATED dataset "
                                "(see `run_data.py show`)")
        return ds, contract or None

    def config_hashes(self) -> dict[str, str]:
        return {"settings_sha256": file_sha256(self.settings_path), "backtest_sha256": file_sha256(self.backtest_path)}

    def ledger(self) -> tuple[ValidationLedger, list[dict[str, Any]]]:
        path, _ = resolve_ledger_path(self.ledger_path)
        led = ValidationLedger(path)
        return led, led.records()          # raises LedgerMissingError / LedgerError: research needs the ledger


@dataclass
class Resolved:
    spec: Any
    pv: Any
    params: Any
    overrides: dict[str, Any]
    params_dict: dict[str, Any]
    params_sha256: str

    @property
    def label(self) -> str:
        return self.pv.version + (f"+{self.params_sha256[:8]}" if self.overrides else "")

    def identity(self) -> dict[str, Any]:
        return {"id": self.spec.id, "version": self.pv.version, "sha256": self.pv.sha256,
                "registered": self.pv.registered, "registered_on": self.pv.registered_on,
                "params_sha256": self.params_sha256, "overrides": self.overrides or None,
                "implementation": self.spec.implementation}


def resolve(env: ResearchEnv, strategy_id: str, version: str, overrides: dict[str, Any] | None = None,
            allow_unregistered: bool = False) -> Resolved:
    spec = get_spec(strategy_id)
    pv = load_version(strategy_id, version, env.app, allow_unregistered=allow_unregistered)
    params, ov = pv.params, dict(overrides or {})
    if ov:
        if spec.derive is None:
            raise ResearchError(f"{strategy_id} does not support parameter sweeps here")
        check_bounds(strategy_id, ov)
        params = spec.derive(pv.params, ov, env.app)
    pdict = spec.to_dict(params) if spec.to_dict else {}
    return Resolved(spec, pv, params, ov, pdict, params_sha256({"strategy": strategy_id, "params": pdict}))


def _period(purpose: str, start: str | None, end: str | None, fold: int | None = None) -> Period:
    kind = {"oos": "out_of_sample", "oos_replay": "out_of_sample"}.get(purpose, "in_sample")
    if purpose == "full" and not start and not end:
        return Period("full", "full", None, None)
    return make_period(f"{purpose}{'' if fold is None else f'_{fold:02d}'}", kind, start, end, fold)


def _span(period: Period, md) -> tuple[str, str]:
    d = period.to_dict()
    sd = pd.to_datetime(md.frames["1min"]["session_date"]).dropna()
    return (d["start"] or str(sd.min().date())), (d["end"] or str(sd.max().date()))


def _overlap(a0: str, a1: str, b0: str, b1: str) -> bool:
    return pd.Timestamp(a0) <= pd.Timestamp(b1) and pd.Timestamp(b0) <= pd.Timestamp(a1)


def _trace(run) -> pd.DataFrame:
    """Decision trace per signal: setup -> confirmation -> signal -> engine checks -> fill -> exit."""
    sig = run.signals if run.signals is not None else pd.DataFrame()
    if sig.empty or "signal_id" not in sig.columns:
        return pd.DataFrame(columns=["signal_id"])
    out = sig.copy()
    ec = run.result.entry_checks
    if len(ec) and "client_id" in ec.columns:
        first = ec.drop_duplicates("client_id", keep="first")[["client_id", "outcome", "reason", "fill_price",
                                                                 "actual_risk_dollars"]]
        out = out.merge(first.rename(columns={"client_id": "signal_id", "outcome": "fill_check",
                                              "reason": "fill_check_reason"}), on="signal_id", how="left")
    tr = run.result.trades
    if len(tr):
        cols = ["signal_id", "entry_time", "entry_price", "exit_time", "exit_price", "exit_reason", "net_pnl"]
        out = out.merge(tr[cols], on="signal_id", how="left")
    return out


def _execute(env: ResearchEnv, rs: Resolved, md, period: Period, *, prop_profile: str | None, bt=None):
    from src.ai.market_regime.performance import add_excursions
    from src.backtest.runner import run_spec

    bt = bt or env.bt
    prop = None
    if prop_profile:
        from src.risk.prop_firms.runtime import build_engine

        pid, _, ver = prop_profile.partition("@")
        if not ver:
            raise ResearchError("--prop-profile needs ID@VERSION (an exact version)")
        prop = build_engine(bt=bt, specs={md.contract.root: md.contract.spec}, profile_id=pid, version=ver)
    run = run_spec(md, env.app, bt, rs.spec, rs.params, label=rs.label,
                   period=None if period.kind == "full" else period, regime=env.regime, news=env.news,
                   prop_engine=prop)
    trades = run.result.trades
    if run.regime is not None and len(run.regime.get("trades", [])):
        trades = run.regime["trades"]
    elif len(trades):
        ex_tf = getattr(getattr(rs.params, "scope", None), "execution_timeframe", None) or \
            getattr(rs.params, "execution_timeframe", "1min")
        trades = add_excursions(trades, md.frames[ex_tf], md.contract.spec.point_value)
    return run, trades, prop


def _cost_sensitivity(env: ResearchEnv, rs: Resolved, md, period: Period, prop_profile: str | None) -> list[dict]:
    rows = []
    for name, cmul, ticks in COST_SCENARIOS:
        costs = {k: replace(v, commission_per_contract_per_side=v.commission_per_contract_per_side * cmul)
                 for k, v in env.bt.costs.items()}
        sl = replace(env.bt.slippage, market_ticks=env.bt.slippage.market_ticks + ticks,
                     stop_ticks=env.bt.slippage.stop_ticks + ticks)
        try:
            run, trades, _ = _execute(env, rs, md, period, prop_profile=prop_profile,
                                      bt=replace(env.bt, costs=costs, slippage=sl))
        except ConfigError as exc:
            rows.append({"scenario": name, "error": str(exc)[:200]})
            continue
        dv = daily_pnl(trades).tolist()
        rows.append({"scenario": name, "commission_multiplier": cmul, "extra_slippage_ticks": ticks,
                     "trades": len(trades), "net_profit": float(trades["net_pnl"].sum()) if len(trades) else 0.0,
                     "mean_daily_pnl": float(np.mean(dv)) if dv else None,
                     "mean_daily_ci95": day_bootstrap_mean(dv)})
    return rows


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, (np.floating, np.integer)):
        return _jsonable(o.item())
    if isinstance(o, (pd.Timestamp, pd.Timedelta)):
        return str(o)
    return o


def _family(env: ResearchEnv, strategy: str, instrument: str, attempt: str, p_raw: float | None
            ) -> tuple[list[float | None], int]:
    """p-values of every distinct configuration attempted on this instrument (latest finished result per
    configuration; failures / unfinished = None but counted). Returns (pvalues, index of this attempt)."""
    by_cfg: dict[str, float | None] = {}
    mine = None
    for a in env.registry.attempts(strategy=strategy, instrument=instrument):
        if a["purpose"] in ("oos", "oos_replay"):
            continue
        p = a["summary"].get("p_raw") if a["status"] == "ok" else None
        by_cfg[a["params_sha256"]] = p if a["attempt"] != attempt else p_raw
        if a["attempt"] == attempt:
            mine = a["params_sha256"]
    keys = list(by_cfg)
    if mine is None:
        keys.append("__this__")
        by_cfg["__this__"] = p_raw
        mine = "__this__"
    return [by_cfg[k] for k in keys], keys.index(mine)


def _refuse_revealed(env: ResearchEnv, root: str, p0: str, p1: str) -> None:
    """Dates revealed out of sample (any configuration) can never be used for research again."""
    _, records = env.ledger()
    burned = [r for r in records if r.get("type") == "oos_reveal" and r.get("instrument") == root
              and _overlap(p0, p1, r.get("start") or "1900-01-01", r.get("end") or "2262-01-01")]
    if burned:
        spans = sorted({f"{r.get('start')}..{r.get('end')}" for r in burned})
        raise ResearchError(f"{p0}..{p1} overlaps out-of-sample dates {spans} of {root} that were already "
                            "revealed: revealed data can never be used for research again (no override)")
    from src.research.plans import reserved_oos

    held = [r for r in reserved_oos() if r["instrument"] == root and _overlap(p0, p1, r["start"], r["end"])]
    if held:
        raise ResearchError(f"{p0}..{p1} overlaps the OOS segment {held[0]['start']}..{held[0]['end']} that plan "
                            f"{held[0]['plan']} reserved for {root}: it stays untouched until its one-time reveal")


def _no_synthetic_optimisation(env: ResearchEnv, source) -> None:
    """Sprint 12: parameters are never optimised on a SYNTHETIC dataset (legacy CSV paths are unchanged)."""
    if is_dataset(source) and env.dataset(source)[0].manifest["synthetic"]:
        raise ResearchError("parameter selection on a SYNTHETIC dataset is refused (not market data)")


def guard_period(env: ResearchEnv, csv: str | Path, purpose: str, start: str | None, end: str | None) -> None:
    """Whole-request check BEFORE a sweep / fold run starts anything."""
    md, _ = env.market_data(csv)
    p0, p1 = _span(_period(purpose, start, end), md)
    _refuse_revealed(env, instrument_key(md.contract.root, md.is_synthetic), p0, p1)


def run_research(env: ResearchEnv, strategy_id: str, version: str, csv: str | Path, *, purpose: str = "full",
                 start: str | None = None, end: str | None = None, overrides: dict[str, Any] | None = None,
                 allow_unregistered: bool = False, prop_profile: str | None = None, family: str = "",
                 cost_sensitivity: bool = False, fold: int | None = None, write: bool = True,
                 _oos: dict[str, Any] | None = None) -> dict[str, Any]:
    """One research run. Returns a dict with the evaluation, the run folder and the raw StrategyRun."""
    if purpose not in SCOPE:
        raise ResearchError(f"unknown purpose {purpose!r}")
    if purpose in ("oos", "oos_replay") and _oos is None:
        raise ResearchError("out-of-sample results are revealed only through reveal_oos()")
    rs = resolve(env, strategy_id, version, overrides, allow_unregistered)
    md, data_sha = env.market_data(csv)
    if md.contract.spec is None:
        raise ResearchError(f"no contract specification for {md.contract.root}")
    root = instrument_key(md.contract.root, md.is_synthetic)
    period = _period(purpose, start, end, fold)
    p0, p1 = _span(period, md)
    if purpose not in ("oos", "oos_replay"):
        _refuse_revealed(env, root, p0, p1)
    attempt = env.registry.start(strategy=strategy_id, version=rs.pv.version, version_sha256=rs.pv.sha256,
                                 registered=rs.pv.registered, params_sha256=rs.params_sha256, instrument=root,
                                 data_sha256=data_sha, period={"start": p0, "end": p1, "kind": period.kind,
                                                               "fold": fold},
                                 purpose=purpose, family=family or f"{strategy_id}:{root}",
                                 detail={"overrides": rs.overrides, "prop_profile": prop_profile,
                                         "dataset": str(csv)[len("dataset:"):] if is_dataset(csv) else None})
    try:
        run, trades, prop = _execute(env, rs, md, period, prop_profile=prop_profile)
        dv = daily_pnl(trades).tolist()
        from src.research.stats import one_sided_p

        p_raw = one_sided_p(dv)
        if purpose in ("oos", "oos_replay"):
            fam = _oos["family_pvalues"] + [p_raw]
            idx = len(fam) - 1
        else:
            fam, idx = _family(env, strategy_id, root, attempt, p_raw)
        sessions = pd.to_datetime(run.result.equity["session_date"]).dropna().nunique() if len(run.result.equity) else 0
        ev = evaluate(trades, capital=env.bt.initial_capital, scope=SCOPE[purpose], tz=env.app.session.timezone,
                      equity=run.result.equity, sessions_in_period=int(sessions), family_pvalues=fam, family_index=idx)
        costs = _cost_sensitivity(env, rs, md, period, prop_profile) if cost_sensitivity else None
    except Exception as exc:
        env.registry.finish(attempt, status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    summary = {"p_raw": ev["tests"]["p_raw"], "trades": ev["sample"]["trades"], "net": ev["net_profit"],
               "mean_daily": ev["mean_daily_pnl"], "daily_ci": ev["mean_daily_pnl_ci95_day_bootstrap"],
               "evidence": ev["evidence"]}
    out = {"attempt": attempt, "resolved": rs, "run": run, "trades": trades, "evaluation": ev, "root": root,
           "period": {"start": p0, "end": p1, "kind": period.kind, "fold": fold}, "data_sha256": data_sha,
           "md": md, "cost_sensitivity": costs, "folder": None, "prop": prop, "source": str(csv)}
    if write:
        out["folder"] = _write(env, out, purpose, prop, _oos)
    env.registry.finish(attempt, status="ok", summary=summary)
    return out


def _write(env: ResearchEnv, out: dict[str, Any], purpose: str, prop, oos: dict[str, Any] | None) -> Path:
    rs, run, md = out["resolved"], out["run"], out["md"]
    folder = Path(env.out_root) / rs.spec.id / rs.label / out["root"].replace(":", "_") / \
        f"{purpose}_{out['period']['start']}_{out['period']['end']}_{out['attempt']}"
    folder.mkdir(parents=True, exist_ok=False)
    r = run.result
    files = {"trades.csv": out["trades"], "equity.csv": r.equity, "orders.csv": r.orders,
             "entry_checks.csv": r.entry_checks, "engine_rejections.csv": r.rejections,
             "signal_diagnostics.csv": run.diagnostics, "decision_trace.csv": _trace(run)}
    if run.signals is not None:
        files["signals.csv"] = run.signals
    if r.compliance is not None:
        files["prop_decisions.csv"] = r.compliance["decisions"]
    hashes = {}
    for name, frame in files.items():
        p = folder / name
        frame.to_csv(p, index=(name == "equity.csv"))
        hashes[name] = file_sha256(p)
    ev = out["evaluation"]
    (folder / "evaluation.json").write_text(json.dumps(_jsonable(ev), indent=2, default=str), encoding="utf-8")
    hashes["evaluation.json"] = file_sha256(folder / "evaluation.json")
    cov = r.compliance["coverage"] if r.compliance is not None else None
    if cov is not None:
        (folder / "compliance_coverage.json").write_text(json.dumps(_jsonable(cov), indent=2, default=str),
                                                         encoding="utf-8")
        hashes["compliance_coverage.json"] = file_sha256(folder / "compliance_coverage.json")
    manifest = {
        "kind": "research_run", "schema": 1, "purpose": purpose, "attempt": out["attempt"],
        "strategy": rs.identity(), "params": rs.params_dict, "summary": rs.spec.summary(rs.params),
        "instrument": out["root"], "contract": md.contract.label, "synthetic_data": bool(md.is_synthetic),
        "data": {"sha256": out["data_sha256"], "source": md.report.source,
                 "dataset": env.dataset(out["source"])[0].identity() if is_dataset(out["source"]) else None},
        "period": out["period"],
        "config": env.config_hashes(), "code": code_identity(), "environment": environment_info(),
        "experiments_registry": env.registry.identity(),
        "ledger": {"path": str(resolve_ledger_path(env.ledger_path)[0]),
                   "canonical": resolve_ledger_path(env.ledger_path)[1]},
        "prop": None if prop is None else {"profile": prop.profile.key, "sha256": prop.profile.sha256,
                                          "operational": bool((prop.operational or {}).get("operational"))},
        "compliance_coverage": None if cov is None else {"verdict": cov["verdict"], "issues": cov["issues"],
                                                          "applied": cov["applied"], "unavailable": cov["unavailable"],
                                                          "not_simulatable": cov["not_simulatable"]},
        "regime": None if env.regime is None or not env.regime.enabled else {"enforcement": run.regime and
                                                                               run.regime["config"].get("enforcement")},
        "news": None if env.news is None else {"calendar_sha256": env.news.calendar_sha256, "mode": env.news.cfg.mode},
        "results_fingerprint": results_fingerprint(r.trades, r.equity),
        "evaluation": {k: ev[k] for k in ("evidence", "scope", "net_profit", "mean_daily_pnl",
                                          "mean_daily_pnl_ci95_day_bootstrap")} | {"tests": ev["tests"],
                                                                                    "sample": ev["sample"]},
        "cost_sensitivity": out["cost_sensitivity"], "oos": oos and {k: v for k, v in oos.items()
                                                                       if k != "family_pvalues"},
        "files": hashes,
        "disclaimer": "Research simulation only. Not evidence of future profitability; no order can reach a broker.",
    }
    manifest = _jsonable(manifest)
    manifest["manifest_sha256"] = text_sha256(json.dumps(manifest, sort_keys=True, default=str))
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (folder / "report.md").write_text(render_report(manifest, ev, cov), encoding="utf-8")
    return folder


def verify_manifest(folder: str | Path) -> dict[str, Any]:
    """Re-hash a run folder: the manifest itself and every listed file (corruption / edits are reported)."""
    folder = Path(folder)
    problems = []
    try:
        m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "problems": [f"manifest unreadable: {exc}"]}
    claimed = m.pop("manifest_sha256", None)
    if claimed != text_sha256(json.dumps(m, sort_keys=True, default=str)):
        problems.append("manifest.json was modified (its own hash does not match)")
    for name, sha in (m.get("files") or {}).items():
        p = folder / name
        if not p.is_file():
            problems.append(f"{name} is missing")
        elif file_sha256(p) != sha:
            problems.append(f"{name} was modified")
    return {"ok": not problems, "problems": problems, "manifest": m}


def render_report(m: dict[str, Any], ev: dict[str, Any], cov: dict[str, Any] | None) -> str:
    s = m["strategy"]
    lines = [f"# Research run {s['id']}@{s['version']}{' (overrides ' + json.dumps(s['overrides']) + ')' if s['overrides'] else ''}",
             "", "Research simulation only: no claim of profitability is made. No order can reach a broker.", "",
             f"* Purpose: **{m['purpose']}** -- {ev['scope_text']}",
             f"* Instrument: {m['instrument']} ({'SYNTHETIC DATA: meaningless for evaluation' if m['synthetic_data'] else 'historical data'})",
             f"* Period: {m['period']['start']} .. {m['period']['end']}",
             f"* Version SHA-256: `{s['sha256']}` ({'registered' if s['registered'] else 'UNREGISTERED exploration'})",
             f"* Code: {m['code']['commit'][:12]} ({'clean' if m['code']['trusted'] else 'UNTRUSTED: ' + ', '.join(m['code']['untrusted_reasons'])})",
             "", f"## Evidence: **{ev['evidence']}**", "",
             f"* Trades {ev['sample']['trades']} on {ev['sample']['trading_days_with_trades']} trading days ({ev['sample']['level']})",
             f"* Net {ev['net_profit']:.2f}; mean daily {ev['mean_daily_pnl']}; 95% day-bootstrap {ev['mean_daily_pnl_ci95_day_bootstrap']}",
             f"* p (one-sided, daily) raw {ev['tests']['p_raw']}; Holm {ev['tests']['p_holm']}; BH {ev['tests']['p_bh']} "
             f"(family of {ev['tests']['family_size']} attempted configurations)",
             f"* Win rate {ev['win_rate']} (Wilson {ev['win_rate_ci95_wilson']}); PF {ev['profit_factor']}; "
             f"max DD {ev['max_drawdown_equity']}", ""]
    lines.append("## Prop firm compliance coverage")
    if cov is None:
        lines.append("No prop firm profile in this run: firm rules were NOT simulated.")
    else:
        lines += [f"* Profile {cov['profile']}: **{cov['verdict']}**",
                  f"* Applied: {', '.join(cov['applied']) or '-'}",
                  f"* Verified: {', '.join(cov['verified']) or '-'}",
                  f"* Unavailable: {', '.join(cov['unavailable']) or '-'}",
                  f"* Not simulatable: {', '.join(cov['not_simulatable']) or '-'}"]
        lines += [f"  - {x}" for x in cov["issues"]]
        lines.append(f"* {cov['note']}")
    if m.get("cost_sensitivity"):
        lines += ["", "## Cost sensitivity (full re-runs)"]
        lines += [f"* {c['scenario']}: net {c.get('net_profit')}, mean daily {c.get('mean_daily_pnl')}, "
                  f"CI {c.get('mean_daily_ci95')}" for c in m["cost_sensitivity"]]
    lines += ["", "## Limitations"] + [f"* {x}" for x in ev["limitations"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------- sweeps
def grid_points(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = sorted(grid)
    pts = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]
    if not pts or len(pts) > MAX_GRID:
        raise ResearchError(f"a grid must have 1..{MAX_GRID} points (got {len(pts)})")
    return pts


def sweep(env: ResearchEnv, strategy_id: str, version: str, csv: str | Path, grid: dict[str, list[Any]], *,
          purpose: str = "train", start: str | None = None, end: str | None = None,
          prop_profile: str | None = None) -> dict[str, Any]:
    if purpose not in ("full", "train", "validation"):
        raise ResearchError("sweeps run on full / train / validation periods only (never out of sample)")
    pts = grid_points(grid)
    for p in pts:                      # every value checked BEFORE anything runs
        check_bounds(strategy_id, p)
    _no_synthetic_optimisation(env, csv)
    guard_period(env, csv, purpose, start, end)
    family = f"sweep:{strategy_id}@{version}:{hashlib.sha256(json.dumps(grid, sort_keys=True).encode()).hexdigest()[:12]}"
    rows = []
    for p in pts:
        try:
            r = run_research(env, strategy_id, version, csv, purpose=purpose, start=start, end=end, overrides=p,
                             prop_profile=prop_profile, family=family)
            ev = r["evaluation"]
            rows.append({"params": p, "params_sha256": r["resolved"].params_sha256, "attempt": r["attempt"],
                         "trades": ev["sample"]["trades"], "mean_daily": ev["mean_daily_pnl"],
                         "daily_ci": ev["mean_daily_pnl_ci95_day_bootstrap"], "p_raw": ev["tests"]["p_raw"],
                         "evidence": ev["evidence"], "folder": str(r["folder"]), "error": None})
        except ConfigError as exc:
            rows.append({"params": p, "error": str(exc)[:300], "trades": None, "mean_daily": None, "p_raw": None})
    # final family adjustment over every attempt on this instrument (including earlier sessions)
    md, _ = env.market_data(csv)
    root = instrument_key(md.contract.root, md.is_synthetic)
    attempts = [a for a in env.registry.attempts(strategy=strategy_id, instrument=root)
                if a["purpose"] not in ("oos", "oos_replay")]
    latest: dict[str, float | None] = {}
    for a in attempts:
        latest[a["params_sha256"]] = a["summary"].get("p_raw") if a["status"] == "ok" else None
    keys = list(latest)
    from src.research.stats import benjamini_hochberg, holm

    h, b = holm([latest[k] for k in keys]), benjamini_hochberg([latest[k] for k in keys])
    adj = {k: (h[i], b[i]) for i, k in enumerate(keys)}
    for row in rows:
        if row.get("params_sha256") in adj:
            row["p_holm"], row["p_bh"] = adj[row["params_sha256"]]
    stability(rows, grid)
    return {"strategy": strategy_id, "base_version": version, "purpose": purpose, "grid": grid, "family": family,
            "family_size": len(keys), "rows": rows,
            "note": "Rows are in parameter order, NOT ranked. Derived configurations are UNREGISTERED: to evaluate one "
                    "out of sample, register it as a new immutable version first."}


def stability(rows: list[dict[str, Any]], grid: dict[str, list[Any]]) -> None:
    """For each grid point: neighbours differ in exactly one parameter by one grid step. Adds the share of
    neighbours whose mean daily P&L has the same sign and the neighbours' range (parameter stability)."""
    axes = {k: sorted(v) for k, v in grid.items()}
    index = {json.dumps(r["params"], sort_keys=True): r for r in rows}
    for r in rows:
        nb = []
        for k, vals in axes.items():
            i = vals.index(r["params"][k])
            for j in (i - 1, i + 1):
                if 0 <= j < len(vals):
                    q = dict(r["params"], **{k: vals[j]})
                    o = index.get(json.dumps(q, sort_keys=True))
                    if o is not None and o.get("mean_daily") is not None:
                        nb.append(o["mean_daily"])
        m = r.get("mean_daily")
        r["stability"] = {"neighbours": len(nb),
                          "same_sign_share": (sum((x > 0) == (m > 0) for x in nb) / len(nb)) if nb and m is not None else None,
                          "neighbour_range": [min(nb), max(nb)] if nb else None}


# ---------------------------------------------------------------------------------------------- folds
def selected_sessions(env: ResearchEnv, rs: Resolved, md) -> list[pd.Timestamp]:
    prep = rs.spec.prepare(md, env.app, rs.params, None)
    return sorted(pd.to_datetime(prep.signal["session_date"]).dropna().unique())


def walk_forward_folds(dates: list[pd.Timestamp], train: int, test: int, purge: int = 0, step: int | None = None
                       ) -> list[tuple[list[tuple[pd.Timestamp, pd.Timestamp]], tuple[pd.Timestamp, pd.Timestamp]]]:
    """Rolling folds: ``train`` sessions, a PURGE gap of ``purge`` sessions, then ``test`` sessions; the window
    advances by ``step`` (default ``test``; never smaller, so test windows never overlap). Training data
    always precedes its test window (an embargo is therefore not needed in walk-forward)."""
    if train < 1 or test < 1 or purge < 0:
        raise ResearchError("walk-forward needs train >= 1, test >= 1, purge >= 0 sessions")
    step = step or test
    if step < test:
        raise ResearchError("step < test would make test windows overlap")
    folds, i = [], 0
    while i + train + purge + test <= len(dates):
        tr = dates[i:i + train]
        te = dates[i + train + purge:i + train + purge + test]
        folds.append(([(tr[0], tr[-1])], (te[0], te[-1])))
        i += step
    if not folds:
        raise ResearchError(f"not enough sessions ({len(dates)}) for walk-forward {train}+{purge}+{test}")
    return folds


def purged_kfold(dates: list[pd.Timestamp], k: int, purge: int = 0, embargo: int = 0
                 ) -> list[tuple[list[tuple[pd.Timestamp, pd.Timestamp]], tuple[pd.Timestamp, pd.Timestamp]]]:
    """Blocked k-fold cross-validation: each contiguous block is a test set once; training uses the sessions
    BEFORE the block minus the last ``purge`` sessions and AFTER the block minus the first ``embargo`` sessions
    (removing observations whose information overlaps the test block)."""
    if k < 2 or k > len(dates) or purge < 0 or embargo < 0:
        raise ResearchError("purged k-fold needs 2 <= k <= sessions, purge >= 0, embargo >= 0")
    bounds = np.linspace(0, len(dates), k + 1).astype(int)
    folds = []
    for j in range(k):
        a, b = bounds[j], bounds[j + 1]
        if a >= b:
            continue
        before = dates[:max(0, a - purge)]
        after = dates[min(len(dates), b + embargo):]
        segs = [(s[0], s[-1]) for s in (before, after) if len(s)]
        folds.append((segs, (dates[a], dates[b - 1])))
    return folds


def _objective(daily: list[float]) -> float:
    ci = day_bootstrap_mean(daily)
    return ci[0] if ci is not None else -math.inf


def run_folds(env: ResearchEnv, strategy_id: str, version: str, csv: str | Path, folds, *, mode: str,
              grid: dict[str, list[Any]] | None = None, prop_profile: str | None = None) -> dict[str, Any]:
    """Evaluate folds: with a grid, each fold SELECTS on its training segments by the declared objective and is
    then evaluated on its test block; without a grid the version is evaluated on each test block."""
    purpose = "walk_forward" if mode == "walk_forward" else "purged_cv"
    pts = grid_points(grid) if grid else [{}]
    for p in pts:
        if p:
            check_bounds(strategy_id, p)
    if grid:
        _no_synthetic_optimisation(env, csv)
    starts = [a for segs, _ in folds for a, _b in segs] + [t[0] for _, t in folds]
    ends = [b for segs, _ in folds for _a, b in segs] + [t[1] for _, t in folds]
    if starts:
        guard_period(env, csv, "full", str(min(starts).date()), str(max(ends).date()))
    fam = f"{mode}:{strategy_id}@{version}:{hashlib.sha256(json.dumps(grid or {}, sort_keys=True).encode()).hexdigest()[:12]}"
    fold_rows, test_trades = [], []
    for n, (train_segs, (t0, t1)) in enumerate(folds, 1):
        chosen, scores = pts[0], []
        if grid:
            for p in pts:
                daily: list[float] = []
                for s0, s1 in train_segs:
                    r = run_research(env, strategy_id, version, csv, purpose="train", start=str(s0.date()),
                                     end=str(s1.date()), overrides=p, prop_profile=prop_profile, family=fam,
                                     fold=n, write=False)
                    daily += daily_pnl(r["trades"]).tolist()
                scores.append(_objective(daily))
            chosen = pts[int(np.argmax(scores))] if scores and max(scores) > -math.inf else pts[0]
        r = run_research(env, strategy_id, version, csv, purpose=purpose, start=str(t0.date()), end=str(t1.date()),
                         overrides=chosen or None, prop_profile=prop_profile, family=fam, fold=n)
        test_trades.append(r["trades"])
        fold_rows.append({"fold": n, "train": [[str(a.date()), str(b.date())] for a, b in train_segs],
                          "test": [str(t0.date()), str(t1.date())], "selected": chosen,
                          "train_objective": max(scores) if scores else None, "test_trades": len(r["trades"]),
                          "test_mean_daily": r["evaluation"]["mean_daily_pnl"],
                          "test_evidence": r["evaluation"]["evidence"], "folder": str(r["folder"])})
    allt = pd.concat([t for t in test_trades if len(t)], ignore_index=True) if any(len(t) for t in test_trades) \
        else pd.DataFrame()
    md, _ = env.market_data(csv)
    root = instrument_key(md.contract.root, md.is_synthetic)
    m = len({a["params_sha256"] for a in env.registry.attempts(strategy=strategy_id, instrument=root)
             if a["purpose"] not in ("oos", "oos_replay")})
    agg = evaluate(allt, capital=env.bt.initial_capital, scope="wf_out_of_sample" if mode == "walk_forward" else "purged_cv",
                   tz=env.app.session.timezone, family_size=m)
    return {"mode": mode, "strategy": strategy_id, "version": version, "grid": grid, "objective": OBJECTIVE,
            "folds": fold_rows, "aggregate": agg, "family_size": m}


# ---------------------------------------------------------------------------------------------- OOS
def oos_fingerprint(env: ResearchEnv, rs: Resolved, code: dict[str, Any], prop_profile: str | None) -> str:
    return text_sha256(json.dumps({"strategy": rs.spec.id, "version": rs.pv.version, "version_sha256": rs.pv.sha256,
                                   "config": env.config_hashes(), "source_sha256": code["source_sha256"],
                                   "prop_profile": prop_profile or None,
                                   "regime": None if env.regime is None else env.regime.to_dict(),
                                   "news": None if env.news is None else env.news.calendar_sha256},
                                  sort_keys=True, default=str))


def _check_plan(env: ResearchEnv, plan: str | None, strategy_id: str, version: str, csv, root: str, start: str,
                end: str) -> dict[str, Any] | None:
    """Dates reserved by a registered plan are revealed only through that plan, exactly as frozen."""
    from src.research.plans import load_plan, reserved_oos

    held = [r for r in reserved_oos() if r["instrument"] == root and _overlap(start, end, r["start"], r["end"])]
    if plan is None:
        if held:
            raise ResearchError(f"{start}..{end} is the OOS segment reserved by plan {held[0]['plan']}: reveal it "
                                "with --plan, exactly as frozen")
        return None
    from src.histdata.store import DatasetStore

    p = load_plan(plan, env.app, env.store or DatasetStore())
    mine = [d for d in p["datasets"] if d["key"] == root]
    if not mine:
        raise ResearchError(f"plan {plan} has no dataset for {root}")
    if not is_dataset(csv) or str(csv)[len("dataset:"):].split("#")[0] != mine[0]["ref"]:
        raise ResearchError(f"plan {plan} freezes dataset {mine[0]['ref']} for {root}")
    if [start, end] != p["oos"]:
        raise ResearchError(f"plan {plan} freezes the OOS segment {p['oos'][0]}..{p['oos'][1]}")
    if f"{strategy_id}@{version}" not in p["strategies"]:
        raise ResearchError(f"{strategy_id}@{version} is not one of the strategy versions frozen by plan {plan}")
    return {"plan": plan, "plan_sha256": p["sha256"], "dataset": mine[0]["ref"]}


def reveal_oos(env: ResearchEnv, strategy_id: str, version: str, csv: str | Path, start: str, end: str, *,
               prop_profile: str | None = None, plan: str | None = None) -> dict[str, Any]:
    """The ONE-TIME out-of-sample reveal of a registered version (see the module docstring)."""
    if not start or not end:
        raise ResearchError("an out-of-sample reveal needs explicit --start and --end dates")
    rs = resolve(env, strategy_id, version)             # registered + unmodified, no overrides
    if not rs.pv.registered:
        raise ResearchError("only a registered version can be revealed out of sample")
    code = code_identity()
    if not code["trusted"]:
        raise ResearchError("refusing to reveal out-of-sample results from untrusted code: "
                            f"{', '.join(code['untrusted_reasons'])}. Commit your changes first.")
    md, data_sha = env.market_data(csv)
    root = instrument_key(md.contract.root, md.is_synthetic)
    if is_dataset(csv) and env.dataset(csv)[0].manifest["synthetic"]:
        raise ResearchError("a SYNTHETIC dataset is never revealed as out-of-sample evidence")
    plan_info = _check_plan(env, plan, strategy_id, version, csv, root, start, end)
    period = _period("oos", start, end)
    led, records = env.ledger()
    fp = oos_fingerprint(env, rs, code, prop_profile)
    conflicts = revealed_conflicts(records, root, period, fp)
    if conflicts:
        spans = sorted({f"{c['start']}..{c['end']}" for c in conflicts})
        raise ResearchError(f"{start}..{end} overlaps out-of-sample dates {spans} of {root} already revealed under "
                            "a different configuration or code: they are no longer out of sample (no override)")
    used = [a for a in env.registry.attempts(strategy=strategy_id, instrument=root)
            if a["purpose"] not in ("oos", "oos_replay")
            and _overlap(start, end, a["period"]["start"], a["period"]["end"])]
    if used:
        raise ResearchError(f"{start}..{end} was already used by {len(used)} research attempt(s) of {strategy_id} on "
                            f"{root} (e.g. {used[0]['period']['start']}..{used[0]['period']['end']}, "
                            f"{used[0]['purpose']}): it is not out of sample")
    previous = [r for r in records if r.get("type") == "oos_reveal" and r.get("instrument") == root
                and r.get("fingerprint") == fp and r.get("start") == period.to_dict()["start"]
                and r.get("end") == period.to_dict()["end"]]
    reveals = [r for r in records if r.get("type") == "oos_reveal" and r.get("instrument") == root
               and r.get("strategy") == strategy_id and r not in previous]
    fam = [r.get("p_raw") for r in reveals]
    if previous:
        rec = previous[-1]
        res = run_research(env, strategy_id, version, csv, purpose="oos_replay", start=start, end=end,
                           prop_profile=prop_profile, write=True,
                           _oos={"ledger_record_hash": rec["record_hash"], "ledger_seq": rec["seq"], "replay": True,
                                 "fingerprint": fp, "family_pvalues": fam})
        same = results_fingerprint(res["run"].result.trades, res["run"].result.equity) == rec.get("results_fingerprint")
        res["replay_identical"] = same
        return res
    # compute, then record the reveal in the ledger BEFORE anything is written or shown
    res = run_research(env, strategy_id, version, csv, purpose="oos", start=start, end=end, prop_profile=prop_profile,
                       write=False, _oos={"family_pvalues": fam})
    rec = led.append({"type": "oos_reveal", "source": "run_research", "instrument": root, **period.to_dict(),
                      "data_sha256": data_sha, "fingerprint": fp, "strategy": strategy_id, "version": rs.pv.version,
                      "version_sha256": rs.pv.sha256, "source_sha256": code["source_sha256"], "commit": code["commit"],
                      "results_fingerprint": results_fingerprint(res["run"].result.trades, res["run"].result.equity),
                      "p_raw": res["evaluation"]["tests"]["p_raw"], "evidence": res["evaluation"]["evidence"],
                      "experiments_registry": env.registry.identity(), "plan": plan_info})
    oos = {"ledger_record_hash": rec["record_hash"], "ledger_seq": rec["seq"], "replay": False, "fingerprint": fp,
           "family_size": len(fam) + 1, "plan": plan_info}
    res["folder"] = _write(env, res, "oos", res["prop"], oos)
    res["oos"] = oos
    return res


# ---------------------------------------------------------------------------------------------- comparison
def compare(folders: list[str | Path]) -> dict[str, Any]:
    """Side-by-side comparison of runs on EQUIVALENT data (same data, instrument, period, configuration files);
    refused otherwise. Nothing is ranked; pairwise differences of mean daily P&L come with a paired
    day-bootstrap interval."""
    runs = []
    for f in folders:
        v = verify_manifest(f)
        if not v["ok"]:
            raise ResearchError(f"{f}: {', '.join(v['problems'])}")
        runs.append((Path(f), v["manifest"]))
    if len(runs) < 2:
        raise ResearchError("compare needs at least two runs")
    keys = lambda m: (m["data"]["sha256"], m["instrument"], m["period"]["start"], m["period"]["end"],  # noqa: E731
                      m["config"]["settings_sha256"], m["config"]["backtest_sha256"], json.dumps(m.get("prop"),
                                                                                                 sort_keys=True))
    base = keys(runs[0][1])
    for f, m in runs[1:]:
        if keys(m) != base:
            raise ResearchError(f"{f} is not on equivalent data / period / configuration as {runs[0][0]}; "
                                "comparisons are only made like for like")
    rows, daily = [], {}
    for f, m in sorted(runs, key=lambda x: (x[1]["strategy"]["id"], x[1]["strategy"]["version"], str(x[0]))):
        ev = json.loads((f / "evaluation.json").read_text(encoding="utf-8"))
        t = pd.read_csv(f / "trades.csv")
        key = f"{m['strategy']['id']}@{m['strategy']['version']}" + ("*" if m["strategy"].get("overrides") else "")
        daily[key] = t.groupby("exit_session_date")["net_pnl"].sum() if len(t) else pd.Series(dtype=float)
        rows.append({"run": key, "folder": str(f), "evidence": ev["evidence"], "trades": ev["sample"]["trades"],
                     "mean_daily": ev["mean_daily_pnl"], "mean_daily_ci95": ev["mean_daily_pnl_ci95_day_bootstrap"],
                     "win_rate": ev["win_rate"], "win_rate_ci95": ev["win_rate_ci95_wilson"],
                     "profit_factor": ev["profit_factor"], "max_drawdown": ev["max_drawdown_equity"],
                     "p_holm": ev["tests"]["p_holm"], "family_size": ev["tests"]["family_size"],
                     "sharpe": ev["ratios"]["sharpe"]})
    pairs = []
    names = list(daily)
    for a, b in itertools.combinations(names, 2):
        idx = daily[a].index.union(daily[b].index)
        diff = (daily[a].reindex(idx, fill_value=0.0) - daily[b].reindex(idx, fill_value=0.0)).tolist()
        ci = day_bootstrap_mean(diff)
        verdict = "no detectable difference" if ci is None or (ci[0] <= 0 <= ci[1]) else \
            (f"{a} higher" if ci[0] > 0 else f"{b} higher")
        pairs.append({"a": a, "b": b, "days": len(idx), "mean_daily_difference": float(np.mean(diff)) if diff else None,
                      "ci95_paired_day_bootstrap": ci, "reading": verdict + " (unadjusted; exploratory)"})
    return {"runs": rows, "pairs": pairs, "equivalence": dict(zip(("data_sha256", "instrument", "start", "end",
                                                                   "settings_sha256", "backtest_sha256", "prop"), base)),
            "note": "Not ranked. Net profit alone is never a criterion; overlapping intervals mean the data cannot "
                    "tell the candidates apart."}


__all__ = ["ResearchEnv", "ResearchError", "resolve", "run_research", "sweep", "walk_forward_folds", "purged_kfold",
           "run_folds", "reveal_oos", "compare", "verify_manifest", "OBJECTIVE", "selected_sessions"]
