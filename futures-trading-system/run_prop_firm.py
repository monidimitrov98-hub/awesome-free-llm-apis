"""Prop Firm Account Protection & Compliance (Sprint 9) -- profiles, verification, versions, reports.

READ-ONLY except for one explicit HUMAN action (``activate`` / ``revoke``), which only appends an
approval record. Nothing here places orders, changes an account at a firm or submits payouts.

    python run_prop_firm.py list
    python run_prop_firm.py show fundednext_futures_flex_50k_challenge 2026-11-22.1
    python run_prop_firm.py diff ID OLD_VERSION NEW_VERSION
    python run_prop_firm.py evaluate ID VERSION --balance 50000 --equity 49800 --position MES=2 --root MNQ --qty 1 \\
        --time 2026-11-20T15:00:00Z                        (what-if; nothing is stored)
    python run_prop_firm.py report --journal data/paper/paper_state.db [--format json|csv --out output/prop_firm]
    python run_prop_firm.py activate ID VERSION --approved-by "Your Name" --confirm <sha16> \\
        --statement "I read the official rules on <date> and verified every rule"
    python run_prop_firm.py revoke ID VERSION --by "Your Name" --reason "..."

Exit codes: 0 ok, 1 error / refused, 2 evaluation BLOCK.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Prop firm compliance (read-only; activation is a human action).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "activate", "revoke", "evaluate", "payout"):
        s = sub.add_parser(name)
        s.add_argument("profile")
        s.add_argument("version")
        if name == "activate":
            s.add_argument("--approved-by", required=True)
            s.add_argument("--confirm", required=True, help="first 16 characters of the profile SHA-256")
            s.add_argument("--statement", required=True)
        if name == "revoke":
            s.add_argument("--by", required=True)
            s.add_argument("--reason", required=True)
        if name == "evaluate":
            s.add_argument("--balance", type=float, required=True)
            s.add_argument("--equity", type=float, required=True)
            s.add_argument("--position", action="append", default=[], metavar="ROOT=QTY")
            s.add_argument("--root", required=True)
            s.add_argument("--qty", type=int, default=1)
            s.add_argument("--risk", type=float, default=None, help="$ risk of the trade if stopped")
            s.add_argument("--time", default=None, help="ISO timestamp (default: now)")
            s.add_argument("--eod-high", type=float, default=None, help="known end-of-day high-water mark")
    d = sub.add_parser("diff")
    d.add_argument("profile")
    d.add_argument("old")
    d.add_argument("new")
    r = sub.add_parser("report")
    r.add_argument("--journal", type=Path, required=True)
    r.add_argument("--format", choices=("text", "json", "csv"), default="text")
    r.add_argument("--out", type=Path, default=None)
    return p.parse_args(argv)


def main(argv=None) -> int:
    from src.risk.prop_firms.versions import Activations, Registry, affected_accounts, diff, operational_state

    a = parse_args(argv)
    try:
        reg = Registry()
        if a.cmd == "list":
            for key, prof in sorted(reg.profiles.items()):
                st = operational_state(prof)
                print(f"{key:58} {st['status']:11} {'OPERATIONAL' if st['operational'] else 'not operational'}"
                      f"  ({prof.firm} / {prof.product} / {prof.stage} / {prof.account_size:,.0f})")
            for f, err in reg.errors.items():
                print(f"INVALID {f}: {err}")
            return 0
        if a.cmd == "diff":
            print(json.dumps({**diff(reg.get(a.profile, a.old), reg.get(a.profile, a.new)),
                              "affected_accounts": affected_accounts(a.profile)}, indent=2, default=str))
            return 0
        if a.cmd == "report":
            return _report(a)
        prof = reg.get(a.profile, a.version)
        if a.cmd == "show":
            st = operational_state(prof)
            print(json.dumps({"profile": prof.key, "sha256": prof.sha256, "confirm_code": prof.sha256[:16],
                              "ruleset_sha256": prof.ruleset_sha256, "firm": prof.firm, "product": prof.product,
                              "stage": prof.stage, "account_size": prof.account_size, "status": st,
                              "verification": prof.verification, "internal_limits_not_firm_rules": vars(prof.internal),
                              "rules": [vars(r) for r in prof.rules], "sources": [vars(s) for s in prof.sources]},
                             indent=2, default=str))
            return 0
        if a.cmd == "activate":
            print("HUMAN ACTION: activating a profile makes its rules authoritative for entries of sessions that "
                  "select it. It does NOT approve live trading or any account at the firm.")
            rec = Activations().activate(prof, approved_by=a.approved_by, confirm_sha=a.confirm, statement=a.statement)
            print(json.dumps(rec, indent=2))
            return 0
        if a.cmd == "revoke":
            print(json.dumps(Activations().revoke(prof, by=a.by, reason=a.reason), indent=2))
            return 0
        if a.cmd == "evaluate":
            return _evaluate(a, prof)
        if a.cmd == "payout":
            print("Payout eligibility needs an account history: use `report --journal`.")
            return 1
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _evaluate(a, prof) -> int:
    from src.backtest.config import load_backtest_config
    from src.config import load_config
    from src.risk.prop_firms.engine import AccountSnapshot, ComplianceEngine
    from src.risk.prop_firms.versions import operational_state

    app = load_config()
    eng = ComplianceEngine(prof, operational=operational_state(prof), account_guard_rules=load_backtest_config().account_rules,
                           tick_values={r: s.tick_value for r, s in app.contracts.items()})
    if a.eod_high is not None:
        eng.eod_high = eng.intraday_high = max(prof.initial_balance, a.eod_high)
    pos = {}
    for x in a.position:
        r, _, q = x.partition("=")
        pos[r.upper()] = int(q)
    ts = pd.Timestamp(a.time) if a.time else pd.Timestamp.now(tz="UTC")
    if ts.tzinfo is None:
        raise ConfigError("--time needs a timezone (e.g. 2026-11-20T15:00:00Z)")
    snap = AccountSnapshot(balance=a.balance, equity=a.equity, positions=pos, initial_balance=prof.initial_balance)
    dec = eng.evaluate_entry(ts, a.root.upper(), a.qty, snap, trade_risk=a.risk)
    print(f"WHAT-IF evaluation (nothing stored) -- {prof.key}: {'PASS' if dec.allowed else 'BLOCK'}")
    for r in dec.results:
        print(f"  {r.result:11} {r.rule_id:28} {r.reason or ''}  {r.explanation}")
    print(f"official floor {dec.official_floor}  official buffer {dec.official_buffer}  "
          f"estimated buffer {dec.estimated_buffer}")
    return 0 if dec.allowed else 2


def _report(a) -> int:
    from src.risk.prop_firms.reports import journal_report, rows_for_export
    from src.analytics.shadow.library import to_csv, to_json

    p = a.journal if a.journal.is_absolute() else PROJECT_ROOT / a.journal
    rep = journal_report(p)
    if a.format == "text":
        print(f"profiles used: {rep['profiles']}")
        print(f"rule evaluations: {rep['counts']['evaluations']}  blocks: {rep['counts']['blocks']}  "
              f"end-of-day records: {rep['counts']['eod']}  breaches: {rep['counts']['breaches']}")
        for b, n in rep["block_reasons"].items():
            print(f"  {n:5}  {b}")
        return 0
    meta = {"report": "prop firm compliance", "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "timezone": "UTC timestamps", "journal": p.parent.name, "profiles": rep["profiles"]}
    out = a.out or PROJECT_ROOT / "output" / "prop_firm"
    out.mkdir(parents=True, exist_ok=True)
    stamp = meta["generated_at"][:19].replace(":", "").replace("-", "")
    if a.format == "json":
        f = out / f"prop_firm_report_{stamp}.json"
        f.write_text(to_json(rep, meta), encoding="utf-8")
    else:
        f = out / f"prop_firm_evaluations_{stamp}.csv"
        f.write_text(to_csv(rows_for_export(rep, "evaluations"), meta), encoding="utf-8")
    print(f"wrote {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
