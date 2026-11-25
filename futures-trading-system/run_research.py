"""Quant research engine (Sprint 11) -- research, validation, robustness and HUMAN promotion gates.

RESEARCH / SIMULATION ONLY. Every run uses the existing event-driven BacktestEngine with every protection
layer; nothing here can reach a broker, create an authorization token, change AccountGuard limits or
compliance rules, activate a prop firm profile or approve live trading.

    python run_research.py strategies
    python run_research.py versions [--strategy vwap_mean_reversion]
    python run_research.py register --strategy vwap_mean_reversion --version V1-C2-ATR2 --note "..."
    python run_research.py run --strategy vwap_mean_reversion --version V1-C2-SWING --csv data/MES.csv \\
        --purpose train --start 2024-01-02 --end 2024-06-28 [--cost-sensitivity] [--prop-profile ID@VER]
    python run_research.py sweep --strategy vwap_mean_reversion --version V1-C2-SWING --csv data/MES.csv \\
        --purpose train --start ... --end ... --grid indicators.band_k=1.5,2,2.5 --grid stop.atr_multiple=1,1.5
    python run_research.py walk-forward --strategy ... --version ... --csv ... --train 60 --test 20 --purge 1 [--grid ...]
    python run_research.py purged-cv --strategy ... --version ... --csv ... --folds 5 --purge 1 --embargo 2 [--grid ...]
    python run_research.py reveal-oos --strategy ... --version ... --csv ... --start 2025-01-02 --end 2025-06-30
    python run_research.py monte-carlo --run <run folder> [--paths 5000 --seed 7 --ruin-loss 2000]
    python run_research.py compare <run folder> <run folder> ...
    python run_research.py verify --run <run folder>
    python run_research.py experiments [--strategy ... --instrument MES]
    python run_research.py governance [--strategy ... --version ...]
    python run_research.py promote --strategy ... --version ... --to BACKTESTED --approved-by "Your Name" \\
        --confirm <sha16> --statement "..." --run <run folder>        (HUMAN action; there is no live state)

The validation ledger must exist (``python run_backtest.py --init-ledger``). Exit codes: 0 ok, 1 refused /
configuration error, 2 data validation error, 5 ledger missing or damaged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.backtest.config import DEFAULT_BACKTEST_CONFIG_PATH, load_backtest_config
from src.backtest.validation import LedgerError
from src.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, ConfigError, load_config
from src.data.errors import DataError, DataValidationError

DEFAULT_OUT = PROJECT_ROOT / "output" / "research"


def _common(s: argparse.ArgumentParser, *, csv: bool = True) -> None:
    s.add_argument("--strategy", required=True)
    s.add_argument("--version", required=True)
    if csv:
        g = s.add_mutually_exclusive_group(required=True)
        g.add_argument("--csv", type=Path, help="legacy CSV file (exploration; not a registered dataset)")
        g.add_argument("--dataset", default=None, metavar="ID@vN[#CONTRACT]",
                       help="Sprint 12 dataset (VALIDATED or better; verified on load)")
    s.add_argument("--prop-profile", default=None, metavar="ID@VERSION",
                   help="simulate a prop firm profile (UNVERIFIED profiles block every entry: fail closed)")
    s.add_argument("--regime", action="store_true", help="label trades with the regime engine (observe only)")
    s.add_argument("--news-calendar", type=Path, default=None,
                   help="historical news calendar: enables NewsGuard in the simulation")


def _grid(values: list[str]) -> dict[str, list]:
    out = {}
    for item in values:
        key, _, raw = item.partition("=")
        if not key or not raw:
            raise ConfigError(f"--grid needs key=v1,v2,... (got {item!r})")
        out[key.strip()] = [_num(x) for x in raw.split(",") if x.strip()]
    return out


def _num(x: str):
    x = x.strip()
    try:
        return int(x)
    except ValueError:
        return float(x)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Quant research engine (simulation only).")
    p.add_argument("--settings", type=Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument("--backtest-config", type=Path, default=DEFAULT_BACKTEST_CONFIG_PATH)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--ledger", type=Path, default=None, help="validation ledger (default: canonical project ledger)")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("strategies")
    v = sub.add_parser("versions")
    v.add_argument("--strategy", default=None)
    g = sub.add_parser("register")
    g.add_argument("--strategy", required=True)
    g.add_argument("--version", required=True)
    g.add_argument("--note", default="")
    r = sub.add_parser("run")
    _common(r)
    r.add_argument("--purpose", choices=("full", "train", "validation"), default="full")
    r.add_argument("--start", default=None)
    r.add_argument("--end", default=None)
    r.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="exploration override inside the declared bounds (the result is UNREGISTERED)")
    r.add_argument("--cost-sensitivity", action="store_true")
    r.add_argument("--allow-unregistered", action="store_true",
                   help="explore an unregistered version file (never revealable or promotable)")
    s = sub.add_parser("sweep")
    _common(s)
    s.add_argument("--purpose", choices=("full", "train", "validation"), default="train")
    s.add_argument("--start", default=None)
    s.add_argument("--end", default=None)
    s.add_argument("--grid", action="append", default=[], required=True, metavar="KEY=V1,V2")
    for name in ("walk-forward", "purged-cv"):
        w = sub.add_parser(name)
        _common(w)
        w.add_argument("--grid", action="append", default=[], metavar="KEY=V1,V2")
        w.add_argument("--purge", type=int, default=1, help="sessions removed between training and test data")
        w.add_argument("--start", default=None, help="first session used by the folds (e.g. a plan's train start)")
        w.add_argument("--end", default=None, help="last session used by the folds (e.g. a plan's validation end)")
        if name == "walk-forward":
            w.add_argument("--train", type=int, required=True)
            w.add_argument("--test", type=int, required=True)
            w.add_argument("--step", type=int, default=None)
        else:
            w.add_argument("--folds", type=int, required=True)
            w.add_argument("--embargo", type=int, default=1, help="sessions after each test block excluded from training")
    o = sub.add_parser("reveal-oos")
    _common(o)
    o.add_argument("--start", required=True)
    o.add_argument("--end", required=True)
    o.add_argument("--plan", default=None, help="the registered plan that reserved these OOS dates")
    for name in ("plan-check", "plan-register"):
        pc = sub.add_parser(name)
        pc.add_argument("plan_id")
    sub.add_parser("plans")
    m = sub.add_parser("monte-carlo")
    m.add_argument("--run", type=Path, required=True)
    m.add_argument("--paths", type=int, default=2000)
    m.add_argument("--seed", type=int, default=20261125)
    m.add_argument("--methods", nargs="*", default=None)
    m.add_argument("--ruin-loss", type=float, default=None, help="$ loss from the start that counts as ruin")
    m.add_argument("--drawdown-levels", nargs="*", type=float, default=[])
    m.add_argument("--block", type=int, default=None, help="moving-block length in trading days")
    c = sub.add_parser("compare")
    c.add_argument("runs", nargs="+", type=Path)
    vr = sub.add_parser("verify")
    vr.add_argument("--run", type=Path, required=True)
    e = sub.add_parser("experiments")
    e.add_argument("--strategy", default=None)
    e.add_argument("--instrument", default=None)
    gv = sub.add_parser("governance")
    gv.add_argument("--strategy", default=None)
    gv.add_argument("--version", default=None)
    pr = sub.add_parser("promote")
    pr.add_argument("--strategy", required=True)
    pr.add_argument("--version", required=True)
    pr.add_argument("--to", required=True)
    pr.add_argument("--approved-by", required=True)
    pr.add_argument("--confirm", required=True, help="first 16 characters of the version SHA-256")
    pr.add_argument("--statement", required=True)
    pr.add_argument("--run", type=Path, default=None)
    pr.add_argument("--monte-carlo", type=Path, default=None)
    pr.add_argument("--shadow-report", type=Path, default=None)
    return p.parse_args(argv)


def _env(a, app, bt):
    from src.ai.market_regime.config import load_regime_config
    from src.research.experiments import ExperimentRegistry
    from src.research.runs import ResearchEnv

    regime = load_regime_config() if getattr(a, "regime", False) else None
    if regime is not None and not regime.enabled:
        regime = None
    news = None
    if getattr(a, "news_calendar", None):
        from src.risk.news.backtest import load_setup
        from src.risk.news.config import load_news_config

        news = load_setup(load_news_config(), a.news_calendar)
    return ResearchEnv(app, bt, a.output_dir, a.settings, a.backtest_config, ledger_path=a.ledger,
                       registry=ExperimentRegistry(), regime=regime, news=news)


def _print(obj, as_json: bool) -> None:
    from src.research.runs import _jsonable

    if as_json:
        print(json.dumps(_jsonable(obj), indent=2, default=str))
        return
    if isinstance(obj, list):
        for row in obj:
            print(json.dumps(_jsonable(row), default=str))
    else:
        print(json.dumps(_jsonable(obj), indent=2, default=str))


def _run_summary(res) -> dict:
    ev = res["evaluation"]
    return {"folder": str(res["folder"]), "attempt": res["attempt"], "evidence": ev["evidence"],
            "scope": ev["scope_text"], "trades": ev["sample"]["trades"], "net_profit": ev["net_profit"],
            "mean_daily_pnl": ev["mean_daily_pnl"], "mean_daily_ci95": ev["mean_daily_pnl_ci95_day_bootstrap"],
            "p_raw": ev["tests"]["p_raw"], "p_holm": ev["tests"]["p_holm"], "family_size": ev["tests"]["family_size"]}


def main(argv=None) -> int:
    a = parse_args(argv)
    if getattr(a, "dataset", None):
        a.csv = f"dataset:{a.dataset}"
    try:
        app = load_config(a.settings)
        if a.cmd == "strategies":
            from src.research.registry import REGISTRY

            _print([s.info() for s in REGISTRY.values()], a.json)
            return 0
        if a.cmd == "versions":
            from src.research.governance import overview

            rows = overview(app)
            if a.strategy:
                rows = [r for r in rows if r["strategy"] == a.strategy]
            for r in rows:
                r["confirm_sha16"] = r["sha256"][:16]
            _print(rows, a.json)
            return 0
        if a.cmd == "register":
            from src.research.params import register_version

            _print(register_version(a.strategy, a.version, app, note=a.note), a.json)
            return 0
        if a.cmd == "verify":
            from src.research.runs import verify_manifest

            v = verify_manifest(a.run)
            _print({"ok": v["ok"], "problems": v["problems"]}, a.json)
            return 0 if v["ok"] else 1
        if a.cmd == "experiments":
            from src.research.experiments import ExperimentRegistry

            _print(ExperimentRegistry().attempts(strategy=a.strategy, instrument=a.instrument), a.json)
            return 0
        if a.cmd == "governance":
            from src.research.governance import overview

            rows = [r for r in overview(app) if (not a.strategy or r["strategy"] == a.strategy)
                    and (not a.version or r["version"] == a.version)]
            _print(rows, a.json)
            return 0
        if a.cmd == "promote":
            from src.research.governance import transition

            rec = transition(a.strategy, a.version, app, to=a.to.upper(), approved_by=a.approved_by,
                             confirm_sha=a.confirm, statement=a.statement,
                             evidence={"run": a.run, "monte_carlo": a.monte_carlo, "shadow_report": a.shadow_report},
                             ledger_path=a.ledger)
            print(f"recorded: {rec['strategy']}@{rec['version']} {rec['from']} -> {rec['to']} by {rec['approved_by']} "
                  f"(seq {rec['seq']}). Live trading is never approved by this system.")
            return 0
        if a.cmd in ("plan-check", "plan-register", "plans"):
            from src.histdata.store import DatasetStore
            from src.research import plans as P

            if a.cmd == "plans":
                _print([{"plan": k, **v} for k, v in P.load_index().items()], a.json)
                return 0
            path = P.plans_dir() / f"{a.plan_id}.toml"
            out = P.parse_plan(path, app, DatasetStore()) if a.cmd == "plan-check" else \
                P.register(a.plan_id, app, DatasetStore())
            _print(out, a.json)
            if a.cmd == "plan-register":
                print(f"plan {a.plan_id} registered: its OOS segment is now reserved until the one-time reveal")
            return 0
        if a.cmd == "compare":
            from src.research.runs import compare

            _print(compare(a.runs), a.json)
            return 0
        if a.cmd == "monte-carlo":
            return _monte_carlo(a, app)
        bt = load_backtest_config(a.backtest_config)
        env = _env(a, app, bt)
        from src.research import runs as R

        if a.cmd == "run":
            overrides = {k.strip(): _num(v) for k, _, v in (x.partition("=") for x in a.set)}
            res = R.run_research(env, a.strategy, a.version, a.csv, purpose=a.purpose, start=a.start, end=a.end,
                                 overrides=overrides or None, allow_unregistered=a.allow_unregistered,
                                 prop_profile=a.prop_profile, cost_sensitivity=a.cost_sensitivity)
            if res["md"].is_synthetic:
                print("*** SYNTHETIC DATA: results are meaningless for evaluating the strategy ***")
            _print(_run_summary(res), a.json)
            return 0
        if a.cmd == "sweep":
            out = R.sweep(env, a.strategy, a.version, a.csv, _grid(a.grid), purpose=a.purpose, start=a.start,
                          end=a.end, prop_profile=a.prop_profile)
            _write_json(a.output_dir, f"sweep_{a.strategy}_{a.version}", out)
            _print(out, a.json)
            return 0
        if a.cmd in ("walk-forward", "purged-cv"):
            rs = R.resolve(env, a.strategy, a.version)
            md, _ = env.market_data(a.csv)
            dates = R.selected_sessions(env, rs, md)
            if a.start or a.end:
                import pandas as pd

                lo, hi = pd.Timestamp(a.start or "1900-01-01"), pd.Timestamp(a.end or "2262-01-01")
                dates = [d for d in dates if lo <= pd.Timestamp(d) <= hi]
            if a.cmd == "walk-forward":
                folds = R.walk_forward_folds(dates, a.train, a.test, a.purge, a.step)
            else:
                folds = R.purged_kfold(dates, a.folds, a.purge, a.embargo)
            out = R.run_folds(env, a.strategy, a.version, a.csv, folds, mode=a.cmd.replace("-", "_"),
                              grid=_grid(a.grid) if a.grid else None, prop_profile=a.prop_profile)
            _write_json(a.output_dir, f"{a.cmd}_{a.strategy}_{a.version}", out)
            _print({k: out[k] for k in ("mode", "objective", "folds", "family_size")} |
                   {"aggregate_evidence": out["aggregate"]["evidence"],
                    "aggregate_mean_daily": out["aggregate"]["mean_daily_pnl"],
                    "aggregate_ci95": out["aggregate"]["mean_daily_pnl_ci95_day_bootstrap"]}, a.json)
            return 0
        if a.cmd == "reveal-oos":
            res = R.reveal_oos(env, a.strategy, a.version, a.csv, a.start, a.end, prop_profile=a.prop_profile,
                               plan=a.plan)
            s = _run_summary(res)
            if res.get("replay_identical") is not None:
                s["replay_identical"] = res["replay_identical"]
                print("REPLAY of an earlier reveal (same configuration and code): no new ledger record")
            _print(s, a.json)
            return 0
    except DataValidationError as exc:
        print(exc.report.summary(), file=sys.stderr)
        return 2
    except LedgerError as exc:
        print(f"Error: validation ledger problem: {exc}. Create it once with `python run_backtest.py --init-ledger`"
              " or restore it from backup; research never runs without it.", file=sys.stderr)
        return 5
    except (ConfigError, DataError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


def _write_json(out_dir: Path, name: str, obj) -> Path:
    import pandas as pd

    from src.research.runs import _jsonable

    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}_{pd.Timestamp.now(tz='UTC').strftime('%Y%m%dT%H%M%S%f')}.json"
    p.write_text(json.dumps(_jsonable(obj), indent=2, default=str), encoding="utf-8")
    return p


def _monte_carlo(a, app) -> int:
    import pandas as pd

    from src.research.monte_carlo import METHODS, simulate
    from src.research.runs import _jsonable, verify_manifest

    v = verify_manifest(a.run)
    if not v["ok"]:
        raise ConfigError(f"run folder failed verification: {', '.join(v['problems'])}")
    m = v["manifest"]
    trades = pd.read_csv(a.run / "trades.csv")
    root = m["instrument"].split(":")[0]
    spec = app.contracts[root]
    profile, operational = None, False
    if m.get("prop"):
        from src.risk.prop_firms.versions import Registry, operational_state

        pid, _, ver = m["prop"]["profile"].partition("@")
        profile = Registry().get(pid, ver)
        if profile.sha256 != m["prop"]["sha256"]:
            raise ConfigError("the run's prop profile file has changed since the run")
        operational = bool(operational_state(profile)["operational"])
    bt = load_backtest_config(a.backtest_config)
    rep = simulate(trades, capital=bt.initial_capital, tick_value=spec.tick_value,
                   methods=tuple(a.methods) if a.methods else METHODS, n_paths=a.paths, seed=a.seed,
                   ruin_loss=a.ruin_loss, drawdown_levels=tuple(a.drawdown_levels), block=a.block,
                   profile=profile, profile_operational=operational)
    rep["strategy"] = m["strategy"]
    rep["run"] = {"folder": str(a.run), "manifest_sha256": json.loads((a.run / "manifest.json").read_text())["manifest_sha256"],
                  "purpose": m["purpose"], "instrument": m["instrument"], "period": m["period"]}
    out = a.run / f"monte_carlo_seed{a.seed}_paths{a.paths}.json"
    out.write_text(json.dumps(_jsonable(rep), indent=2, default=str), encoding="utf-8")
    hist = rep.get("historical", {})
    print(f"Monte Carlo written to {out}")
    print(f"historical: final {hist.get('final_pnl')}, max DD {hist.get('max_drawdown')}")
    for meth, scen in rep.get("methods", {}).items():
        base = scen.get("as_backtested", {})
        print(f"{meth}: P(final<0) {base.get('prob_final_pnl_negative')}, max DD p50/p95 "
              f"{base.get('max_drawdown', {}).get('p50')}/{base.get('max_drawdown', {}).get('p95')}, "
              f"ruin {base.get('risk_of_ruin')}")
    print(rep.get("disclaimer", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
