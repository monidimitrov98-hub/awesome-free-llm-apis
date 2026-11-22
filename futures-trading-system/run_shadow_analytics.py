"""Shadow Trading Validation & Analytics (Sprint 8) -- READ-ONLY research tooling.

Reads paper-session journals (the authoritative event logs) and reports. It never changes trading,
risk, NewsGuard, strategy or execution configuration, and it never trades.

    python run_shadow_analytics.py sessions
    python run_shadow_analytics.py report --session-type SHADOW --root MES
    python run_shadow_analytics.py compare --root MES                    (REPLAY vs SHADOW, compatible runs only)
    python run_shadow_analytics.py validation
    python run_shadow_analytics.py export trades --session-type REPLAY --format csv --out output/analytics
    python run_shadow_analytics.py sessions --journal data/analytics/journals/replay-20261122T100000/paper_state.db

Journals come from config/analytics.toml ([sources] journals) and/or --journal (repeatable, inside the
project). Exit codes: 0 ok, 1 configuration / input error, 2 report refused (e.g. mixed session types).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.analytics.shadow.library import (EXPORTS, AnalyticsLibrary, Filters, export_meta, export_rows,
                                          load_analytics_config, resolve_journals, to_csv, to_json)
from src.config import PROJECT_ROOT, ConfigError

SECTIONS = ("sessions", "performance", "execution", "costs", "regimes", "blocked", "reliability", "account", "validation")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Shadow trading analytics (read-only).")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--journal", action="append", default=[], help="journal path inside the project (repeatable)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("sessions", "report", "validation", "compare", "export"):
        s = sub.add_parser(name)
        if name == "export":
            s.add_argument("kind", choices=EXPORTS)
            s.add_argument("--format", choices=("csv", "json"), default="csv")
            s.add_argument("--out", type=Path, default=PROJECT_ROOT / "output" / "analytics")
        if name == "report":
            s.add_argument("--section", choices=SECTIONS, action="append")
        if name == "compare":
            s.add_argument("--a", default="REPLAY")
            s.add_argument("--b", default="SHADOW")
        s.add_argument("--root", choices=("MES", "MNQ"))
        s.add_argument("--start")
        s.add_argument("--end")
        s.add_argument("--session-type", choices=("MOCK", "REPLAY", "SHADOW", "MONITOR", "UNKNOWN"))
        s.add_argument("--strategy-version")
        s.add_argument("--regime")
        s.add_argument("--provider")
    return p.parse_args(argv)


def _library(a) -> AnalyticsLibrary:
    cfg = load_analytics_config(a.config)
    paths = resolve_journals(cfg["journals"], cfg["max_journals"])
    root = PROJECT_ROOT.resolve()
    for j in a.journal:
        p = (Path(j) if Path(j).is_absolute() else PROJECT_ROOT / j).resolve()
        if root not in p.parents or not p.is_file():
            raise ConfigError(f"--journal {j}: must be an existing file inside the project")
        if p not in paths:
            paths.append(p)
    if not paths:
        raise ConfigError("no journals found (run a session first, or pass --journal)")
    return AnalyticsLibrary(paths, cfg)


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        f = Filters(a.root, a.start, a.end, a.session_type, a.strategy_version, a.regime, a.provider)
        lib = _library(a)
    except (ConfigError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        if a.cmd == "sessions":
            rep = lib.sessions(f)
            print(f"{rep['count']} session(s); types {rep['session_types']}")
            for s in rep["sessions"]:
                print(f"  {s['session_id']}  {s['session_type']:7} {s['strategy_version'] or 'UNAVAILABLE'}  signals {s['signals']:3}"
                      f"  authorized {s['authorized_entries']:3}  trades {s['completed_trades']:3}  net {s['net_pnl']:9.2f}"
                      f"  status {s['status']}")
            for j in rep["journals"]:
                if j["error"]:
                    print(f"  EXCLUDED {j['run']}: {j['error']}")
            return 0
        if a.cmd == "validation":
            print(to_json(lib.validation(), {"report": "validation"}))
            return 0
        if a.cmd == "compare":
            if not a.root:
                print("Error: compare needs --root (MES and MNQ are compared separately)", file=sys.stderr)
                return 1
            print(to_json(lib.compare(a.root, a.a, a.b, f), {"report": "compare"}))
            return 0
        if a.cmd == "report":
            out = {}
            for sec in a.section or SECTIONS:
                out[sec] = getattr(lib, sec)(f) if sec != "validation" else lib.validation()
            refused = [k for k, v in out.items() if isinstance(v, dict) and v.get("refused")]
            print(to_json(out, {"report": "shadow analytics", "filters": f.describe()}))
            return 2 if refused and len(refused) == len(out) else 0
        rows, rep = export_rows(lib, a.kind, f)
        if rep.get("refused"):
            print(f"Refused: {rep['refused']}", file=sys.stderr)
            return 2
        meta = export_meta(a.kind, f, rep)
        a.out.mkdir(parents=True, exist_ok=True)
        stamp = meta["generated_at"][:19].replace(":", "").replace("-", "")
        path = a.out / f"{a.kind}_{f.session_type or 'all'}_{stamp}.{a.format}"
        path.write_text(to_csv(rows, meta) if a.format == "csv" else to_json({"rows": rows}, meta), encoding="utf-8")
        print(f"wrote {path} ({len(rows)} rows)")
        return 0
    finally:
        lib.close()


if __name__ == "__main__":
    sys.exit(main())
